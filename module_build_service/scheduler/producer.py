# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import timedelta, datetime
import operator

import koji
from sqlalchemy.orm import lazyload, load_only


from module_build_service.common import conf, log, models
from module_build_service.builder import GenericBuilder
from module_build_service.common.koji import get_session
import module_build_service.scheduler
import module_build_service.scheduler.consumer
from module_build_service.scheduler import celery_app
from module_build_service.scheduler.consumer import ON_MODULE_CHANGE_HANDLERS
from module_build_service.scheduler.batches import (
    at_concurrent_component_threshold,
    start_next_batch_build,
)
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler.greenwave import greenwave
from module_build_service.scheduler.handlers.components import build_task_finalize
from module_build_service.scheduler.handlers.tags import tagged


@celery_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    tasks = (
        (log_summary, "Log summary of module builds and component builds"),
        (process_waiting_module_builds, "Process waiting module builds"),
        (fail_lost_builds, "Fail lost builds"),
        (process_paused_module_builds, "Process paused module builds"),
        (delete_old_koji_targets, "Delete old koji targets"),
        (cleanup_stale_failed_builds, "Cleanup stale failed builds"),
        (cancel_stuck_module_builds, "Cancel stuck module builds"),
        (sync_koji_build_tags, "Sync Koji build tags"),
        (poll_greenwave, "Gating module build to ready state"),
    )

    for task, name in tasks:
        sender.add_periodic_task(conf.polling_interval, task.s(), name=name)


@celery_app.task
def log_summary():
    states = sorted(models.BUILD_STATES.items(), key=operator.itemgetter(1))
    for name, code in states:
        query = db_session.query(models.ModuleBuild).filter_by(state=code)
        count = query.count()
        if count:
            log.info("  * %s module builds in the %s state", count, name)
        if name == "build":
            for module_build in query.all():
                log.info("    * %r", module_build)
                # First batch is number '1'.
                for i in range(1, module_build.batch + 1):
                    n = len([c for c in module_build.component_builds if c.batch == i])
                    log.info("      * %s components in batch %s", n, i)


@celery_app.task
def process_waiting_module_builds():
    for state in ["init", "wait"]:
        nudge_module_builds_in_state(state, 10)


def nudge_module_builds_in_state(state_name, older_than_minutes):
    """
    Finds all the module builds in the `state` with `time_modified` older
    than `older_than_minutes` and adds fake MBSModule message to the
    work queue.
    """
    log.info("Looking for module builds stuck in the %s state", state_name)
    builds = models.ModuleBuild.by_state(db_session, state_name)
    log.info(" %r module builds in the %s state...", len(builds), state_name)
    now = datetime.utcnow()
    time_modified_threshold = timedelta(minutes=older_than_minutes)
    for build in builds:

        # Only give builds a nudge if stuck for more than ten minutes
        if (now - build.time_modified) < time_modified_threshold:
            continue

        # Pretend the build is modified, so we don't tight spin.
        build.time_modified = now
        db_session.commit()

        # Fake a message to kickstart the build anew in the consumer
        state = module_build_service.common.models.BUILD_STATES[state_name]
        handler = ON_MODULE_CHANGE_HANDLERS[state]
        handler.delay("internal:mbs.module.state.change", build.id, state)


def process_open_component_builds():
    log.warning("process_open_component_builds is not yet implemented...")


@celery_app.task
def fail_lost_builds():
    # This function is supposed to be handling only the part which can't be
    # updated through messaging (e.g. srpm-build failures). Please keep it
    # fit `n` slim. We do want rest to be processed elsewhere
    # TODO re-use

    if conf.system == "koji":
        # We don't do this on behalf of users
        koji_session = get_session(conf, login=False)
        log.info("Querying tasks for statuses:")
        res = db_session.query(models.ComponentBuild).filter_by(
            state=koji.BUILD_STATES["BUILDING"]
        ).options(lazyload("module_build")).all()

        log.info("Checking status for %s tasks", len(res))
        for component_build in res:
            log.debug(component_build.json(db_session))
            # Don't check tasks which haven't been triggered yet
            if not component_build.task_id:
                continue

            # Don't check tasks for components which have been reused,
            # they may have BUILDING state temporarily before we tag them
            # to new module tag. Checking them would be waste of resources.
            if component_build.reused_component_id:
                log.debug(
                    'Skipping check for task "%s", the component has been reused ("%s").',
                    component_build.task_id, component_build.reused_component_id
                )
                continue

            task_id = component_build.task_id

            log.info('Checking status of task_id "%s"', task_id)
            task_info = koji_session.getTaskInfo(task_id)

            state_mapping = {
                # Cancelled and failed builds should be marked as failed.
                koji.TASK_STATES["CANCELED"]: koji.BUILD_STATES["FAILED"],
                koji.TASK_STATES["FAILED"]: koji.BUILD_STATES["FAILED"],
                # Completed tasks should be marked as complete.
                koji.TASK_STATES["CLOSED"]: koji.BUILD_STATES["COMPLETE"],
            }

            # If it is a closed/completed task, then we can extract the NVR
            build_version, build_release = None, None  # defaults
            if task_info["state"] == koji.TASK_STATES["CLOSED"]:
                builds = koji_session.listBuilds(taskID=task_id)
                if not builds:
                    log.warning(
                        "Task ID %r is closed, but we found no builds in koji.", task_id)
                elif len(builds) > 1:
                    log.warning(
                        "Task ID %r is closed, but more than one build is present!", task_id)
                else:
                    build_version = builds[0]["version"]
                    build_release = builds[0]["release"]

            log.info("  task %r is in state %r", task_id, task_info["state"])
            if task_info["state"] in state_mapping:
                build_task_finalize.delay(
                    msg_id="producer::fail_lost_builds fake msg",
                    task_id=component_build.task_id,
                    build_new_state=state_mapping[task_info["state"]],
                    build_name=component_build.package,
                    build_release=build_release,
                    build_version=build_version,
                )

    elif conf.system == "mock":
        pass


@celery_app.task
def process_paused_module_builds():
    log.info("Looking for paused module builds in the build state")
    if at_concurrent_component_threshold(conf):
        log.debug(
            "Will not attempt to start paused module builds due to "
            "the concurrent build threshold being met"
        )
        return

    ten_minutes = timedelta(minutes=10)
    # Check for module builds that are in the build state but don't have any active component
    # builds. Exclude module builds in batch 0. This is likely a build of a module without
    # components.
    module_builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.state == models.BUILD_STATES["build"],
        models.ModuleBuild.batch > 0,
    ).all()
    for module_build in module_builds:
        now = datetime.utcnow()
        # Only give builds a nudge if stuck for more than ten minutes
        if (now - module_build.time_modified) < ten_minutes:
            continue
        # If there are no components in the build state on the module build,
        # then no possible event will start off new component builds.
        # But do not try to start new builds when we are waiting for the
        # repo-regen.
        if not module_build.current_batch(koji.BUILD_STATES["BUILDING"]):
            # Initialize the builder...
            builder = GenericBuilder.create_from_module(
                db_session, module_build, conf)

            if has_missed_new_repo_message(module_build, builder.koji_session):
                log.info("  Processing the paused module build %r", module_build)
                start_next_batch_build(conf, module_build, builder)

        # Check if we have met the threshold.
        if at_concurrent_component_threshold(conf):
            break


@celery_app.task
def retrigger_new_repo_on_failure():
    """
    Retrigger failed new repo tasks for module builds in the build state.

    The newRepo task may fail for various reasons outside the scope of MBS.
    This method will detect this scenario and retrigger the newRepo task
    if needed to avoid the module build from being stuck in the "build" state.
    """
    if conf.system != "koji":
        return

    koji_session = get_session(conf)
    module_builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.state == models.BUILD_STATES["build"],
        models.ModuleBuild.new_repo_task_id.isnot(None),
    ).all()

    for module_build in module_builds:
        task_info = koji_session.getTaskInfo(module_build.new_repo_task_id)
        if task_info["state"] in [koji.TASK_STATES["CANCELED"], koji.TASK_STATES["FAILED"]]:
            log.info(
                "newRepo task %s for %r failed, starting another one",
                str(module_build.new_repo_task_id), module_build,
            )
            taginfo = koji_session.getTag(module_build.koji_tag + "-build")
            module_build.new_repo_task_id = koji_session.newRepo(taginfo["name"])

    db_session.commit()


@celery_app.task
def delete_old_koji_targets():
    """
    Deletes targets older than `config.koji_target_delete_time` seconds
    from Koji to cleanup after the module builds.
    """
    if conf.system != "koji":
        return

    log.info("Looking for module builds which Koji target can be removed")

    now = datetime.utcnow()

    koji_session = get_session(conf)
    for target in koji_session.getBuildTargets():
        module = db_session.query(models.ModuleBuild).filter(
            models.ModuleBuild.koji_tag == target["dest_tag_name"],
            models.ModuleBuild.name.notin_(conf.base_module_names),
            models.ModuleBuild.state.notin_([
                models.BUILD_STATES["init"],
                models.BUILD_STATES["wait"],
                models.BUILD_STATES["build"],
            ]),
        ).options(
            load_only("time_completed"),
        ).first()

        if module is None:
            continue

        # Double-check that the target we are going to remove is prefixed
        # by our prefix, so we won't remove f26 when there is some garbage
        # in DB or Koji.
        for allowed_prefix in conf.koji_tag_prefixes:
            if target["name"].startswith(allowed_prefix + "-"):
                break
        else:
            log.error("Module %r has Koji target with not allowed prefix.", module)
            continue

        delta = now - module.time_completed
        if delta.total_seconds() > conf.koji_target_delete_time:
            log.info("Removing target of module %r", module)
            koji_session.deleteBuildTarget(target["id"])


@celery_app.task
def cleanup_stale_failed_builds():
    """Does various clean up tasks on stale failed module builds"""

    if conf.system != "koji":
        return

    stale_date = datetime.utcnow() - timedelta(days=conf.cleanup_failed_builds_time)
    stale_module_builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.state == models.BUILD_STATES["failed"],
        models.ModuleBuild.time_modified <= stale_date,
    ).all()
    if stale_module_builds:
        log.info(
            "%s stale failed module build(s) will be cleaned up",
            len(stale_module_builds)
        )
    for module in stale_module_builds:
        log.info("%r is stale and is being cleaned up", module)
        # Find completed artifacts in the stale build
        artifacts = [c for c in module.component_builds if c.is_completed]
        # If there are no completed artifacts, then there is nothing to tag
        if artifacts:
            # Set buildroot_connect=False so it doesn't recreate the Koji target and etc.
            builder = GenericBuilder.create_from_module(
                db_session, module, conf, buildroot_connect=False
            )
            builder.untag_artifacts([c.nvr for c in artifacts])
            # Mark the artifacts as untagged in the database
            for c in artifacts:
                c.tagged = False
                c.tagged_in_final = False
                db_session.add(c)
        state_reason = (
            "The module was garbage collected since it has failed over {0}"
            " day(s) ago".format(conf.cleanup_failed_builds_time)
        )
        module.transition(
            db_session,
            conf,
            models.BUILD_STATES["garbage"],
            state_reason=state_reason,
            failure_type="user",
        )
        db_session.add(module)
        db_session.commit()


@celery_app.task
def cancel_stuck_module_builds():
    """
    Method transitions builds which are stuck in one state too long to the "failed" state.
    The states are defined with the "cleanup_stuck_builds_states" config option and the
    time is defined by the "cleanup_stuck_builds_time" config option.
    """
    log.info(
        'Looking for module builds stuck in the states "%s" more than %s days',
        " and ".join(conf.cleanup_stuck_builds_states),
        conf.cleanup_stuck_builds_time,
    )

    threshold = datetime.utcnow() - timedelta(days=conf.cleanup_stuck_builds_time)
    states = [
        module_build_service.common.models.BUILD_STATES[state]
        for state in conf.cleanup_stuck_builds_states
    ]

    module_builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.state.in_(states),
        models.ModuleBuild.time_modified < threshold
    ).all()

    log.info(" %s module builds are stuck...", len(module_builds))

    for build in module_builds:
        log.info(
            'Transitioning build "%s:%s:%s:%s" to "Failed" state.',
            build.name, build.stream, build.version, build.context
        )
        state_reason = "The module was in {} for more than {} days".format(
            build.state, conf.cleanup_stuck_builds_time
        )
        build.transition(
            db_session,
            conf,
            state=models.BUILD_STATES["failed"],
            state_reason=state_reason,
            failure_type="user",
        )
        db_session.commit()


@celery_app.task
def sync_koji_build_tags():
    """
    Method checking the "tagged" and "tagged_in_final" attributes of
    "complete" ComponentBuilds in the current batch of module builds
    in "building" state against the Koji.

    In case the Koji shows the build as tagged/tagged_in_final,
    fake "tagged" message is added to work queue.
    """
    if conf.system != "koji":
        return

    koji_session = get_session(conf, login=False)

    threshold = datetime.utcnow() - timedelta(minutes=10)
    module_builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.time_modified < threshold,
        models.ModuleBuild.state == models.BUILD_STATES["build"]
    ).all()
    for module_build in module_builds:
        complete_components = module_build.current_batch(koji.BUILD_STATES["COMPLETE"])
        for c in complete_components:
            # In case the component is tagged in the build tag and
            # also tagged in the final tag (or it is build_time_only
            # and therefore should not be tagged in final tag), skip it.
            if c.tagged and (c.tagged_in_final or c.build_time_only):
                continue

            log.info(
                "%r: Component %r is complete, but not tagged in the "
                "final and/or build tags.",
                module_build, c,
            )

            # Check in which tags the component is tagged.
            tag_dicts = koji_session.listTags(c.nvr)
            tags = [tag_dict["name"] for tag_dict in tag_dicts]

            # If it is tagged in final tag, but MBS does not think so,
            # schedule fake message.
            if not c.tagged_in_final and module_build.koji_tag in tags:
                log.info(
                    "Apply tag %s to module build %r",
                    module_build.koji_tag, module_build)
                tagged.delay("internal:sync_koji_build_tags", module_build.koji_tag, c.nvr)

            # If it is tagged in the build tag, but MBS does not think so,
            # schedule fake message.
            build_tag = module_build.koji_tag + "-build"
            if not c.tagged and build_tag in tags:
                log.info(
                    "Apply build tag %s to module build %r",
                    build_tag, module_build)
                tagged.delay("internal:sync_koji_build_tags", build_tag, c.nvr)


@celery_app.task
def poll_greenwave():
    """Polls Greenwave for all builds in done state"""
    if greenwave is None:
        return

    module_builds = db_session.query(models.ModuleBuild).filter_by(
        state=models.BUILD_STATES["done"],
        scratch=False
    ).all()

    log.info("Checking Greenwave for %d builds", len(module_builds))

    for build in module_builds:
        if greenwave.check_gating(build):
            build.transition(db_session, conf, state=models.BUILD_STATES["ready"])
        else:
            build.state_reason = "Gating failed (MBS will retry in {0} seconds)".format(
                conf.polling_interval
            )
            if greenwave.error_occurred:
                build.state_reason += " (Error occured while querying Greenwave)"
            build.time_modified = datetime.utcnow()
        db_session.commit()


def has_missed_new_repo_message(module_build, koji_session):
    """
    Returns whether or not a new repo message has probably been missed.
    """
    if not module_build.new_repo_task_id:
        # A newRepo task has incorrectly not been requested. Treat it as a missed
        # message so module build can recover.
        return True
    log.debug(
        'Checking status of newRepo task "%d" for %s',
        module_build.new_repo_task_id, module_build)
    task_info = koji_session.getTaskInfo(module_build.new_repo_task_id)
    # Other final states, FAILED and CANCELED, are handled by retrigger_new_repo_on_failure
    return task_info["state"] == koji.TASK_STATES["CLOSED"]
