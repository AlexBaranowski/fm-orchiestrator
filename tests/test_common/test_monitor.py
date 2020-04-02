# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os

import mock
import pytest
import requests
from six.moves import reload_module

from module_build_service import app
from module_build_service.common import conf, models
import module_build_service.common.monitor
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, init_data, make_module_in_db

num_of_metrics = 18


class TestViews:
    def setup_method(self, test_method):
        self.client = app.test_client()
        init_data(2)

    def test_metrics(self):
        rv = self.client.get("/module-build-service/1/monitor/metrics")

        count = len([
            l for l in rv.get_data(as_text=True).splitlines()
            if (l.startswith("# TYPE") and "_created " not in l)
        ])
        assert count == num_of_metrics


def test_standalone_metrics_server_disabled_by_default():
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("http://127.0.0.1:10040/metrics")


def test_standalone_metrics_server():
    with mock.patch.dict(os.environ, {"MONITOR_STANDALONE_METRICS_SERVER_ENABLE": "true"}):
        reload_module(module_build_service.common.monitor)

        r = requests.get("http://127.0.0.1:10040/metrics")
        count = len([
            l for l in r.text.splitlines()
            if (l.startswith("# TYPE") and "_created " not in l)
        ])
        assert count == num_of_metrics


@mock.patch("module_build_service.common.monitor.builder_failed_counter.labels")
@mock.patch("module_build_service.common.monitor.builder_success_counter.inc")
def test_monitor_state_changing_success(succ_cnt, failed_cnt):
    clean_database(add_platform_module=False, add_default_arches=False)
    b = make_module_in_db(
        "pkg:0.1:1:c1",
        [
            {
                "requires": {"platform": ["el8"]},
                "buildrequires": {"platform": ["el8"]},
            }
        ],
    )
    b.transition(db_session, conf, models.BUILD_STATES["wait"])
    b.transition(db_session, conf, models.BUILD_STATES["build"])
    b.transition(db_session, conf, models.BUILD_STATES["done"])
    db_session.commit()
    succ_cnt.assert_called_once()
    failed_cnt.assert_not_called()


@mock.patch("module_build_service.common.monitor.builder_failed_counter.labels")
@mock.patch("module_build_service.common.monitor.builder_success_counter.inc")
def test_monitor_state_changing_failure(succ_cnt, failed_cnt):
    clean_database(add_platform_module=False, add_default_arches=False)
    failure_type = "user"
    b = make_module_in_db(
        "pkg:0.1:1:c1",
        [
            {
                "requires": {"platform": ["el8"]},
                "buildrequires": {"platform": ["el8"]},
            }
        ],
    )
    b.transition(db_session, conf, models.BUILD_STATES["wait"])
    b.transition(db_session, conf, models.BUILD_STATES["build"])
    b.transition(db_session, conf, models.BUILD_STATES["failed"], failure_type=failure_type)
    db_session.commit()
    succ_cnt.assert_not_called()
    failed_cnt.assert_called_once_with(reason=failure_type)
