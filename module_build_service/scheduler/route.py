# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" Define the router used to route Celery tasks to queues."""

from __future__ import absolute_import
import inspect

from module_build_service.common import conf, log, models
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler.handlers.greenwave import get_corresponding_module_build


def route_task(name, args, kwargs, options, task=None, **kw):
    """
    Figure out module build id from task args and route task to queue
    per the module build id.

    Each celery worker will listens on two queues:
        1. mbs-default
        2. mbs-{number}  # where number is "module_build_id % conf.num_workers"
    If a task is associated with a module build, route it to the queue
    named "mbs-{number}", otherwise, route it to "mbs-default", this is to ensure
    tasks for a module build can run on the same worker serially.
    """
    queue_name = "mbs-default"

    module_build_id = None
    num_workers = conf.num_workers

    module, handler_name = name.rsplit(".", 1)
    handler = getattr(__import__(module, fromlist=[handler_name]), handler_name)
    # handlers can be decorated, inspect the original function
    while getattr(handler, "__wrapped__", None):
        handler = handler.__wrapped__
    handler_args = inspect.getargspec(handler).args

    def _get_handler_arg(name):
        index = handler_args.index(name)
        arg_value = kwargs.get(name, None)
        if arg_value is None and len(args) > index:
            arg_value = args[index]
        return arg_value

    if "module_build_id" in handler_args:
        module_build_id = _get_handler_arg("module_build_id")

    # if module_build_id is not found, we may be able to figure it out
    # by checking other arguments
    if module_build_id is None:
        if "task_id" in handler_args:
            task_id = _get_handler_arg("task_id")
            component_build = models.ComponentBuild.from_component_event(db_session, task_id)
            if component_build:
                module_build_id = component_build.module_build.id
        elif "tag_name" in handler_args:
            tag_name = _get_handler_arg("tag_name")
            module_build = models.ModuleBuild.get_by_tag(db_session, tag_name)
            if module_build:
                module_build_id = module_build.id
        elif "subject_identifier" in handler_args:
            module_build_nvr = _get_handler_arg("subject_identifier")
            module_build = get_corresponding_module_build(module_build_nvr)
            if module_build is not None:
                module_build_id = module_build.id

    if module_build_id is not None:
        queue_name = "mbs-{}".format(module_build_id % num_workers)

    taskinfo = {"name": name, "args": args, "kwargs": kwargs, "options": options, "kw": kw}
    log.debug("Routing task '{}' to queue '{}'. Task info:\n{}".format(name, queue_name, taskinfo))
    return {"queue": queue_name}
