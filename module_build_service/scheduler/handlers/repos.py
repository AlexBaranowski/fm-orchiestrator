# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" Handlers for repo change events on the message bus. """

from __future__ import absolute_import
from datetime import datetime
import logging

from module_build_service.builder import GenericBuilder
from module_build_service.common import conf, log, models
from module_build_service.scheduler import celery_app, events
from module_build_service.scheduler.batches import start_next_batch_build
from module_build_service.scheduler.db_session import db_session

logging.basicConfig(level=logging.DEBUG)


@celery_app.task
@events.mbs_event_handler
def done(msg_id, tag_name):
    """Called whenever koji rebuilds a repo, any repo.

    :param str msg_id: the original id of the message being handled which is
        received from the message bus.
    :param str tag_name: the tag name from which the repo is generated.
    """

    # First, find our ModuleBuild associated with this repo, if any.
    if conf.system in ("koji", "test") and not tag_name.endswith("-build"):
        log.debug("Tag %r does not end with '-build' suffix, ignoring", tag_name)
        return
    tag = tag_name[:-6] if tag_name.endswith("-build") else tag_name
    module_build = models.ModuleBuild.get_by_tag(db_session, tag_name)
    if not module_build:
        log.debug("No module build found associated with koji tag %r" % tag)
        return

    # It is possible that we have already failed.. but our repo is just being
    # routinely regenerated.  Just ignore that.  If module_build_service says the module is
    # dead, then the module is dead.
    if module_build.state == models.BUILD_STATES["failed"]:
        log.info("Ignoring repo regen for already failed %r" % module_build)
        return

    # If there are no components in this module build, then current_batch will be empty
    if module_build.component_builds:
        current_batch = module_build.current_batch()
    else:
        current_batch = []

    # Get the list of untagged components in current/previous batches which
    # have been built successfully
    if conf.system in ("koji", "test") and current_batch:
        if any(c.is_completed and not c.is_tagged for c in module_build.up_to_current_batch()):
            log.info("Ignoring repo regen, because not all components are tagged.")
            return
        if all(c.is_waiting_for_build for c in current_batch):
            log.info("Ignoring repo regen because no components have started in the batch.")
            return

    # If any in the current batch are still running.. just wait.
    running = [c for c in current_batch if c.is_building]
    if running:
        log.info(
            "%r has %r of %r components still building in this batch (%r total)",
            module_build, len(running), len(current_batch), len(module_build.component_builds)
        )
        return

    # Assemble the list of all successful components in the batch.
    good = [c for c in current_batch if c.is_completed]

    # If *none* of the components completed for this batch, then obviously the
    # module fails.  However!  We shouldn't reach this scenario.  There is
    # logic over in the component handler which should fail the module build
    # first before we ever get here.  This is here as a race condition safety
    # valve.
    if module_build.component_builds and not good:
        state_reason = "Component(s) {} failed to build.".format(
            ", ".join(c.package for c in current_batch if c.is_unsuccessful))
        module_build.transition(
            db_session, conf, models.BUILD_STATES["failed"], state_reason, failure_type="infra")
        db_session.commit()
        log.warning("Odd!  All components in batch failed for %r." % module_build)
        return

    groups = GenericBuilder.default_buildroot_groups(db_session, module_build)
    builder = GenericBuilder.create(
        db_session,
        module_build.owner,
        module_build,
        conf.system,
        conf,
        tag_name=tag,
        components=[c.package for c in module_build.component_builds],
    )
    builder.buildroot_connect(groups)

    # If we have reached here then we know the following things:
    #
    # - All components in this batch have finished (failed or succeeded)
    # - One or more succeeded.
    # - They have been regenerated back into the buildroot.
    #
    # So now we can either start a new batch if there are still some to build
    # or, if everything is built successfully, then we can bless the module as
    # complete.
    has_unbuilt_components = any(c.is_unbuilt for c in module_build.component_builds)
    has_failed_components = any(c.is_unsuccessful for c in module_build.component_builds)

    if has_unbuilt_components and not has_failed_components:
        # Ok, for the subset of builds that did complete successfully, check to
        # see if they are in the buildroot before starting new batch.
        artifacts = [component_build.nvr for component_build in good]
        if not builder.buildroot_ready(artifacts):
            log.info("Not all of %r are in the buildroot.  Waiting." % artifacts)
            return

        # Try to start next batch build, because there are still unbuilt
        # components in a module.
        start_next_batch_build(conf, module_build, builder)
    else:
        if has_failed_components:
            state_reason = "Component(s) {} failed to build.".format(
                ", ".join(
                    c.package for c in module_build.component_builds if c.is_unsuccessful
                )
            )
            module_build.transition(
                db_session,
                conf,
                state=models.BUILD_STATES["failed"],
                state_reason=state_reason,
                failure_type="user",
            )
        else:
            # Tell the external buildsystem to wrap up (CG import, createrepo, etc.)
            module_build.time_completed = datetime.utcnow()
            builder.finalize(succeeded=True)

            module_build.transition(db_session, conf, state=models.BUILD_STATES["done"])
        db_session.commit()
