# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import logging

from module_build_service.common import models
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler.handlers.modules import init as modules_init_handler
from module_build_service.scheduler.handlers.modules import wait as modules_wait_handler
from module_build_service.scheduler.handlers.modules import done as modules_done_handler
from module_build_service.scheduler.handlers.modules import failed as modules_failed_handler

log = logging.getLogger(__name__)


"""
This module contains functions to control fedmsg-hub running locally for local
module build and running tests within test_build.py particularly.
"""

__all__ = ["main"]


def raise_for_failed_build(module_build_ids):
    """
    Raises an exception if any module build from `module_build_ids` list is in failed state.
    This function also calls "failed" handler before raises an exception.

    :param list module_build_ids: List of module build IDs (int) to build locally.
    """
    builds = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.id.in_(module_build_ids)).all()
    has_failed_build = False
    for build in builds:
        if build.state == models.BUILD_STATES["failed"]:
            modules_failed_handler("fake_msg_id", build.id, "failed")
            has_failed_build = True
    if has_failed_build:
        raise ValueError("Local module build failed.")


def main(module_build_ids):
    """
    Build modules locally. The modules have to be stored in the local database before
    calling this function.

    :param list module_build_ids: List of module build IDs (int) to build locally.
    """
    # The transition between states is normally handled by ModuleBuild.transition, which sends
    # a message to message bus. The message is then received by the Consumer and handler is called.
    # But for local builds, we do not have any message bus, so we just call the handlers in
    # the right order manually.
    # We only need to ensure that we won't call futher handlers in case the module build failed.
    for module_build_id in module_build_ids:
        modules_init_handler("fake_msg_id", module_build_id, "init")

    raise_for_failed_build(module_build_ids)
    for module_build_id in module_build_ids:
        modules_wait_handler("fake_msg_id", module_build_id, "wait")

    raise_for_failed_build(module_build_ids)
    for module_build_id in module_build_ids:
        modules_done_handler("fake_msg_id", module_build_id, "done")

    raise_for_failed_build(module_build_ids)
