# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" Handlers for koji component build events on the message bus. """

from __future__ import absolute_import
import logging

import koji

from module_build_service.builder import GenericBuilder
from module_build_service.common import conf, log, models
from module_build_service.common.koji import get_session
from module_build_service.common.utils import mmd_to_str
from module_build_service.scheduler import celery_app, events
from module_build_service.scheduler.batches import continue_batch_build
from module_build_service.scheduler.db_session import db_session

logging.basicConfig(level=logging.DEBUG)


@celery_app.task
@events.mbs_event_handler
def build_task_finalize(
        msg_id, task_id, build_new_state,
        build_name, build_version, build_release,
        module_build_id=None, state_reason=None
):
    """Called when corresponding Koji build task of a component build finishes

    When a task finishes, the task could be in state COMPLETE, FAILED or CANCELED.

    :param str msg_id: the original id of the message being handled which is
        received from the message bus.
    :param int task_id: the Koji build task id.
    :param int build_new_state: the state of the build. Refer to
        ``koji.BUILD_STATES`` for details. For this handler, values could be
        the corresponding integer value of COMPLETE, FAILED or CANCELED.
    :param str build_name: the build name.
    :param str build_version: the build version.
    :param str build_release: the build release.
    :param int module_build_id: optionally set when this event handler is
        scheduled from internal rather than just handling the received message.
        When set, the value should be the id of module build having the
        component build just built by the finished task.
    :param str state_reason: optional. When set a reason explicitly, the
        corresponding component build will have this reason as the
        ``state_reason``. Otherwise, a custom reason will be set for a failed
        build.
    """

    # First, find our ModuleBuild associated with this component, if any.
    component_build = models.ComponentBuild.from_component_event(
        db_session, task_id, module_id=module_build_id)
    nvr = "{}-{}-{}".format(build_name, build_version, build_release)

    if not component_build:
        log.debug("We have no record of %s", nvr)
        return

    log.info("Saw relevant component build of %r from %r.", nvr, msg_id)

    if state_reason:
        state_reason = state_reason
    elif build_new_state != koji.BUILD_STATES["COMPLETE"]:
        state_reason = "Failed to build artifact {} in Koji".format(build_name)
    else:
        state_reason = ""

    # Mark the state in the db.
    component_build.state = build_new_state
    component_build.nvr = nvr
    component_build.state_reason = state_reason
    db_session.commit()

    parent = component_build.module_build

    # If the macro build failed, then the module is doomed.
    if (component_build.package == "module-build-macros"
            and build_new_state != koji.BUILD_STATES["COMPLETE"]):
        parent.transition(
            db_session,
            conf,
            state=models.BUILD_STATES["failed"],
            state_reason=state_reason,
            failure_type="user",
        )
        db_session.commit()
        return

    if (
        component_build.buildonly
        and conf.system in ["koji", "test"]
        and build_new_state == koji.BUILD_STATES["COMPLETE"]
    ):
        koji_session = get_session(conf, login=False)
        rpms = koji_session.listBuildRPMs(component_build.nvr)
        mmd = parent.mmd()
        for artifact in rpms:
            mmd.add_rpm_filter(artifact["name"])
        parent.modulemd = mmd_to_str(mmd)
        db_session.commit()

    parent_current_batch = parent.current_batch()

    # If there are no other components still building in a batch,
    # we can tag all successfully built components in the batch.
    unbuilt_components_in_batch = [
        c for c in parent_current_batch
        if c.is_waiting_for_build or c.is_building
    ]
    if not unbuilt_components_in_batch:
        failed_components_in_batch = [c for c in parent_current_batch if c.is_unsuccessful]
        built_components_in_batch = [c for c in parent_current_batch if c.is_completed]

        builder = GenericBuilder.create_from_module(db_session, parent, conf)

        if failed_components_in_batch:
            log.info(
                "Batch done, but not tagging because of failed component builds. Will "
                'transition the module to "failed"'
            )
            state_reason = "Component(s) {} failed to build.".format(
                ", ".join(c.package for c in failed_components_in_batch))
            parent.transition(
                db_session,
                conf,
                state=models.BUILD_STATES["failed"],
                state_reason=state_reason,
                failure_type="user",
            )
            db_session.commit()
            return []
        elif not built_components_in_batch:
            # If there are no successfully built components in a batch, there is nothing to tag.
            # The repository won't be regenerated in this case and therefore we generate fake repo
            # change message here.
            log.info("Batch done. No component to tag")
            from module_build_service.scheduler.handlers.repos import done as repos_done_handler
            events.scheduler.add(
                repos_done_handler, ("fake_msg", builder.module_build_tag["name"]))
        else:
            built_component_nvrs_in_batch = [c.nvr for c in built_components_in_batch]
            # tag && add to srpm-build group if neccessary
            log.info(
                "Batch done.  Tagging %i component(s) in the build tag."
                % len(built_component_nvrs_in_batch)
            )
            log.debug("%r" % built_component_nvrs_in_batch)
            # TODO: install=component_build.build_time_only works here because module-build-macros
            # is alone in its batch and the only component with build_time_only set. All other
            # batches will have install=False. If we expand to have batches with a mix of
            # build_time_only and not components, then this logic will need to change.
            builder.buildroot_add_artifacts(
                built_component_nvrs_in_batch, install=component_build.build_time_only)

            # Do not tag packages which only belong to the build tag to the dest tag
            component_nvrs_to_tag_in_dest = [
                c.nvr for c in built_components_in_batch
                if c.build_time_only is False
            ]
            log.info(
                "Tagging %i component(s) in the dest tag." % len(component_nvrs_to_tag_in_dest))
            if component_nvrs_to_tag_in_dest:
                builder.tag_artifacts(component_nvrs_to_tag_in_dest)

        db_session.commit()

    elif any(not c.is_building for c in unbuilt_components_in_batch):
        # We are not in the middle of the batch building and
        # we have some unbuilt components in this batch. We might hit the
        # concurrent builds threshold in previous call of continue_batch_build
        # done in repos.py:done(...), but because we have just finished one
        # build, try to call continue_batch_build again so in case we hit the
        # threshold previously, we will submit another build from this batch.
        builder = GenericBuilder.create_from_module(db_session, parent, conf)
        continue_batch_build(conf, parent, builder)
