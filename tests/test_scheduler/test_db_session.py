# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

from mock import patch

from module_build_service.common import models
from module_build_service.common.config import conf
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, make_module_in_db


@patch('module_build_service.common.messaging.publish')
def test_send_messages_after_several_state_transitions(mock_publish):
    """
    Ensure all module build state change messages are sent after multiple
    ModuleBuild.transitions are committed at once
    """
    clean_database()

    build = make_module_in_db("testmodule:1:2:c3")

    build.transition(db_session, conf, models.BUILD_STATES["wait"])
    build.transition(db_session, conf, models.BUILD_STATES["done"])

    assert 0 == mock_publish.call_count
    db_session.commit()
    assert 2 == mock_publish.call_count
