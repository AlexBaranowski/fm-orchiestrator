# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" Handlers for module change events on the message bus. """

from __future__ import absolute_import
from datetime import datetime
import logging
import os

import koji
from requests.exceptions import ConnectionError
import six.moves.xmlrpc_client as xmlrpclib

from module_build_service.builder import GenericBuilder
from module_build_service.builder.KojiModuleBuilder import KojiModuleBuilder
from module_build_service.builder.utils import get_rpm_release
from module_build_service.common import models
from module_build_service.common import build_logs, conf, log
from module_build_service.common.errors import UnprocessableEntity, Forbidden, ValidationError
from module_build_service.common.utils import mmd_to_str
from module_build_service.common.retry import retry
import module_build_service.resolver
from module_build_service.scheduler.submit import (
    record_component_builds,
    record_filtered_rpms,
    record_module_build_arches
)
from module_build_service.scheduler import celery_app, events
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler.default_modules import (
    add_default_modules, handle_collisions_with_base_module_rpms)
from module_build_service.scheduler.greenwave import greenwave
from module_build_service.scheduler.reuse import attempt_to_reuse_all_components
from module_build_service.scheduler.submit import format_mmd, get_module_srpm_overrides
from module_build_service.scheduler.ursine import handle_stream_collision_modules

logging.basicConfig(level=logging.DEBUG)


def get_artifact_from_srpm(srpm_path):
    return os.path.basename(srpm_path).replace(".src.rpm", "")


@celery_app.task
@events.mbs_event_handler
def failed(msg_id, module_build_id, module_build_state):
    """Called whenever a module enters the 'failed' state.

    We cancel all the remaining component builds of a module
    and stop the building.

    :param str msg_id: the original id of the message being handled, which is
        received from the message bus.
    :param int module_build_id: the module build id.
    :param int module_build_state: the module build state.
    """
    build = models.ModuleBuild.get_by_id(db_session, module_build_id)

    if build.state != module_build_state:
        log.warning(
            "Note that retrieved module state %r doesn't match message module state %r",
            build.state, module_build_state,
        )
        # This is ok.. it's a race condition we can ignore.
        pass

    if build.koji_tag:
        builder = GenericBuilder.create_from_module(db_session, build, conf)

        if build.new_repo_task_id:
            builder.cancel_build(build.new_repo_task_id)

        for component in (c for c in build.component_builds if c.is_unbuilt):
            if component.task_id:
                builder.cancel_build(component.task_id)
            component.state = koji.BUILD_STATES["FAILED"]
            component.state_reason = build.state_reason
            db_session.add(component)

        # Tell the external buildsystem to wrap up
        builder.finalize(succeeded=False)
    else:
        # Do not overwrite state_reason set by Frontend if any.
        if not build.state_reason:
            reason = "Missing koji tag. Assuming previously failed module lookup."
            log.error(reason)
            build.transition(
                db_session, conf,
                state=models.BUILD_STATES["failed"],
                state_reason=reason, failure_type="infra")
            db_session.commit()
            return

    # Don't transition it again if it's already been transitioned
    if build.state != models.BUILD_STATES["failed"]:
        build.transition(
            db_session, conf, state=models.BUILD_STATES["failed"], failure_type="user")

    db_session.commit()

    build_logs.stop(build)
    GenericBuilder.clear_cache(build)


@celery_app.task
@events.mbs_event_handler
def done(msg_id, module_build_id, module_build_state):
    """Called whenever a module enters the 'done' state.

    We currently don't do anything useful, so moving to ready.
    Except for scratch module builds, which remain in the done state.
    Otherwise the done -> ready state should happen when all
    dependent modules were re-built, at least that's the current plan.

    :param str msg_id: the original id of the message being handled, which is
        received from the message bus.
    :param int module_build_id: the module build id.
    :param int module_build_state: the module build state.
    """
    build = models.ModuleBuild.get_by_id(db_session, module_build_id)
    if build.state != module_build_state:
        log.warning(
            "Note that retrieved module state %r doesn't match message module state %r",
            build.state, module_build_state,
        )
        # This is ok.. it's a race condition we can ignore.
        pass

    # Scratch builds stay in 'done' state
    if not build.scratch:
        if greenwave is None or greenwave.check_gating(build):
            build.transition(db_session, conf, state=models.BUILD_STATES["ready"])
        else:
            build.state_reason = "Gating failed"
            if greenwave.error_occurred:
                build.state_reason += " (Error occured while querying Greenwave)"
            build.time_modified = datetime.utcnow()
        db_session.commit()

    build_logs.stop(build)
    GenericBuilder.clear_cache(build)


@celery_app.task
@events.mbs_event_handler
def init(msg_id, module_build_id, module_build_state):
    """Called whenever a module enters the 'init' state.

    :param str msg_id: the original id of the message being handled, which is
        received from message bus.
    :param int module_build_id: the module build id.
    :param int module_build_state: the module build state.
    """
    build = models.ModuleBuild.get_by_id(db_session, module_build_id)

    state_init = models.BUILD_STATES["init"]
    if module_build_state == state_init and build.state != state_init:
        log.warning(
            "Module build %r has moved to %s state already.",
            build, models.INVERSE_BUILD_STATES[build.state])
        log.warning(
            "Ignore this message %s. Is there something wrong with the frontend"
            " that sends duplicate messages?", msg_id)
        return

    # for MockModuleBuilder, set build logs dir to mock results dir
    # before build_logs start
    if conf.system == "mock":
        build_tag_name = generate_module_build_koji_tag(build)
        mock_resultsdir = os.path.join(conf.mock_resultsdir, build_tag_name)
        if not os.path.exists(mock_resultsdir):
            os.makedirs(mock_resultsdir)
        build_logs.build_logs_dir = mock_resultsdir

    build_logs.start(db_session, build)
    log.info("Start to handle %s which is in init state.", build.mmd().get_nsvc())

    error_msg = ""
    failure_reason = "unspec"
    try:
        mmd = build.mmd()
        record_module_build_arches(mmd, build)
        arches = [arch.name for arch in build.arches]
        defaults_added = add_default_modules(mmd)

        # Get map of packages that have SRPM overrides
        srpm_overrides = get_module_srpm_overrides(build)
        # Format the modulemd by putting in defaults and replacing streams that
        # are branches with commit hashes
        format_mmd(mmd, build.scmurl, build, db_session, srpm_overrides)
        record_component_builds(mmd, build)

        # The ursine.handle_stream_collision_modules is Koji specific.
        # It is also run only when Ursa Prime is not enabled for the base
        # module (`if not defaults_added`).
        if conf.system in ["koji", "test"] and not defaults_added:
            handle_stream_collision_modules(mmd)

        # Sets xmd["mbs"]["ursine_rpms"] with RPMs from the buildrequired base modules which
        # conflict with the RPMs from other buildrequired modules. This is done to prefer modular
        # RPMs over base module RPMs even if their NVR is lower.
        if conf.system in ("koji", "test"):
            handle_collisions_with_base_module_rpms(mmd, arches)
        else:
            log.warning(
                "The necessary conflicts could not be generated due to RHBZ#1693683. "
                "Some RPMs from the base modules (%s) may end up being used over modular RPMs. "
                "This may result in different behavior than a production build.",
                ", ".join(conf.base_module_names)
            )

        mmd = record_filtered_rpms(mmd)
        build.modulemd = mmd_to_str(mmd)
        build.transition(db_session, conf, models.BUILD_STATES["wait"])
    # Catch custom exceptions that we can expose to the user
    except (UnprocessableEntity, Forbidden, ValidationError, RuntimeError) as e:
        log.exception(str(e))
        error_msg = str(e)
        failure_reason = "user"
    except (xmlrpclib.ProtocolError, koji.GenericError) as e:
        log.exception(str(e))
        error_msg = 'Koji communication error: "{0}"'.format(str(e))
        failure_reason = "infra"
    except Exception as e:
        log.exception(str(e))
        error_msg = "An unknown error occurred while validating the modulemd"
        failure_reason = "user"
    else:
        db_session.add(build)
        db_session.commit()
    finally:
        if error_msg:
            # Rollback changes underway
            db_session.rollback()
            build.transition(
                db_session,
                conf,
                models.BUILD_STATES["failed"],
                state_reason=error_msg,
                failure_type=failure_reason,
            )
            db_session.commit()


def generate_module_build_koji_tag(build):
    """Used by wait handler to get module build koji tag

    :param build: a module build.
    :type build: :class:`ModuleBuild`
    :return: generated koji tag.
    :rtype: str
    """
    log.info("Getting tag for %s:%s:%s", build.name, build.stream, build.version)
    if conf.system in ["koji", "test"]:
        return KojiModuleBuilder.generate_koji_tag(
            build.name,
            build.stream,
            build.version,
            build.context,
            scratch=build.scratch,
            scratch_id=build.id,
        )
    else:
        return "-".join(["module", build.name, build.stream, build.version])


@retry(
    interval=10, timeout=120, wait_on=(ValueError, RuntimeError, ConnectionError)
)
def get_module_build_dependencies(build):
    """Used by wait handler to get module's build dependencies

    :param build: a module build.
    :type build: :class:`ModuleBuild`
    :return: the value returned from :meth:`get_module_build_dependencies`
        according to the configured resolver.
    :rtype: dict[str, Modulemd.Module]
    """
    resolver = module_build_service.resolver.GenericResolver.create(db_session, conf)
    if conf.system in ["koji", "test"]:
        # For Koji backend, query for the module we are going to
        # build to get the koji_tag and deps from it.
        log.info("Getting tag for %s:%s:%s", build.name, build.stream, build.version)
        return resolver.get_module_build_dependencies(
            build.name, build.stream, build.version, build.context, strict=True)
    else:
        # In case of non-koji backend, we want to get the dependencies
        # of the local module build based on Modulemd.Module, because the
        # local build is not stored in the external MBS and therefore we
        # cannot query it using the `query` as for Koji below.
        return resolver.get_module_build_dependencies(mmd=build.mmd(), strict=True)


def get_content_generator_build_koji_tag(module_deps):
    """Used by wait handler to get koji tag for import by content generator

    :param module_deps: a mapping from module's koji tag to its module
        metadata.
    :type: dict[str, Modulemd.Module]
    :return: the koji tag.
    :rtype: str
    """
    if conf.system in ["koji", "test"]:
        # Find out the name of Koji tag to which the module's Content
        # Generator build should be tagged once the build finishes.
        module_names_streams = {
            mmd.get_module_name(): mmd.get_stream_name()
            for mmds in module_deps.values()
            for mmd in mmds
        }
        for base_module_name in conf.base_module_names:
            if base_module_name in module_names_streams:
                return conf.koji_cg_build_tag_template.format(
                    module_names_streams[base_module_name])

        log.debug(
            "No configured base module is a buildrequire. Hence use"
            " default content generator build koji tag %s",
            conf.koji_cg_default_build_tag,
        )
        return conf.koji_cg_default_build_tag
    else:
        return conf.koji_cg_default_build_tag


@celery_app.task
@events.mbs_event_handler
def wait(msg_id, module_build_id, module_build_state):
    """ Called whenever a module enters the 'wait' state.

    We transition to this state shortly after a modulebuild is first requested.

    All we do here is request preparation of the buildroot.
    The kicking off of individual component builds is handled elsewhere,
    in module_build_service.schedulers.handlers.repos.

    :param str msg_id: the original id of the message being handled which is
        received from the message bus.
    :param int module_build_id: the module build id.
    :param int module_build_state: the module build state.
    """
    build = models.ModuleBuild.get_by_id(db_session, module_build_id)

    log.info("Found build=%r from message" % build)
    log.debug("%r", build.modulemd)

    if build.state != module_build_state:
        log.warning(
            "Note that retrieved module state %r doesn't match message module state %r",
            build.state, module_build_state,
        )
        # This is ok.. it's a race condition we can ignore.
        pass

    try:
        build_deps = get_module_build_dependencies(build)
    except ValueError:
        reason = "Failed to get module info from MBS. Max retries reached."
        log.exception(reason)
        build.transition(
            db_session, conf,
            state=models.BUILD_STATES["failed"],
            state_reason=reason, failure_type="infra")
        db_session.commit()
        raise

    tag = generate_module_build_koji_tag(build)
    log.debug("Found tag=%s for module %r" % (tag, build))
    # Hang on to this information for later.  We need to know which build is
    # associated with which koji tag, so that when their repos are regenerated
    # in koji we can figure out which for which module build that event is
    # relevant.
    log.debug("Assigning koji tag=%s to module build" % tag)
    build.koji_tag = tag

    if build.scratch:
        log.debug(
            "Assigning Content Generator build koji tag is skipped for scratch module build.")
    elif conf.koji_cg_tag_build:
        cg_build_koji_tag = get_content_generator_build_koji_tag(build_deps)
        log.debug(
            "Assigning Content Generator build koji tag=%s to module build", cg_build_koji_tag)
        build.cg_build_koji_tag = cg_build_koji_tag
    else:
        log.debug(
            "It is disabled to tag module build during importing into Koji by Content Generator.")
        log.debug("Skip to assign Content Generator build koji tag to module build.")

    builder = GenericBuilder.create_from_module(db_session, build, conf)

    log.debug(
        "Adding dependencies %s into buildroot for module %s:%s:%s",
        build_deps.keys(), build.name, build.stream, build.version,
    )
    builder.buildroot_add_repos(build_deps)

    if not build.component_builds:
        log.info("There are no components in module %r, skipping build" % build)
        build.transition(db_session, conf, state=models.BUILD_STATES["build"])
        db_session.add(build)
        db_session.commit()
        # Return a KojiRepoChange message so that the build can be transitioned to done
        # in the repos handler
        from module_build_service.scheduler.handlers.repos import done as repos_done_handler
        events.scheduler.add(repos_done_handler, ("fake_msg", builder.module_build_tag["name"]))
        return

    # If all components in module build will be reused, we don't have to build
    # module-build-macros, because there won't be any build done.
    if attempt_to_reuse_all_components(builder, build):
        log.info("All components have been reused for module %r, skipping build" % build)
        build.transition(db_session, conf, state=models.BUILD_STATES["build"])
        db_session.add(build)
        db_session.commit()
        return []

    log.debug("Starting build batch 1")
    build.batch = 1
    db_session.commit()

    artifact_name = "module-build-macros"

    component_build = models.ComponentBuild.from_component_name(db_session, artifact_name, build.id)
    srpm = builder.get_disttag_srpm(
        disttag=".%s" % get_rpm_release(db_session, build),
        module_build=build)
    if not component_build:
        component_build = models.ComponentBuild(
            module_id=build.id,
            package=artifact_name,
            format="rpms",
            scmurl=srpm,
            batch=1,
            build_time_only=True,
        )
        db_session.add(component_build)
        # Commit and refresh so that the SQLAlchemy relationships are available
        db_session.commit()
        db_session.refresh(component_build)
        recovered = builder.recover_orphaned_artifact(component_build)
        if recovered:
            log.info("Found an existing module-build-macros build")
        # There was no existing artifact found, so lets submit the build instead
        else:
            task_id, state, reason, nvr = builder.build(artifact_name=artifact_name, source=srpm)
            component_build.task_id = task_id
            component_build.state = state
            component_build.reason = reason
            component_build.nvr = nvr
    elif not component_build.is_completed:
        # It's possible that the build succeeded in the builder but some other step failed which
        # caused module-build-macros to be marked as failed in MBS, so check to see if it exists
        # first
        recovered = builder.recover_orphaned_artifact(component_build)
        if recovered:
            log.info("Found an existing module-build-macros build")
        else:
            task_id, state, reason, nvr = builder.build(artifact_name=artifact_name, source=srpm)
            component_build.task_id = task_id
            component_build.state = state
            component_build.reason = reason
            component_build.nvr = nvr

    db_session.add(component_build)
    build.transition(db_session, conf, state=models.BUILD_STATES["build"])
    db_session.add(build)
    db_session.commit()

    # We always have to regenerate the repository.
    if conf.system == "koji":
        log.info("Regenerating the repository")
        task_id = builder.koji_session.newRepo(builder.module_build_tag["name"])
        build.new_repo_task_id = task_id
        db_session.commit()
    else:
        from module_build_service.scheduler.handlers.repos import done as repos_done_handler
        events.scheduler.add(repos_done_handler, ("fake_msg", builder.module_build_tag["name"]))
