# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import mock

from module_build_service.common.config import conf
from module_build_service.scheduler import celery_app
from module_build_service.scheduler.handlers import components, greenwave, modules, repos, tags
from module_build_service.scheduler.producer import fail_lost_builds
from tests import scheduler_init_data


@mock.patch.object(conf, "num_workers", create=True, new=3)
@mock.patch("celery.app.amqp.AMQP.send_task_message")
class TestCeleryRouteTask:
    def setup_method(self, test_method):
        self.old_task_always_eager = celery_app.conf.get("task_always_eager")
        celery_app.conf.update(task_always_eager=False)

    def teardown_method(self, test_method):
        celery_app.conf.update(task_always_eager=self.old_task_always_eager)

    def test_route_modules_init_task(self, send_task_message):
        modules.init.delay("fakemsg", 2, 0)
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    def test_route_modules_init_task_call_with_kwargs(self, send_task_message):
        kwargs = {
            "msg_id": "fakemsg",
            "module_build_id": 2,
            "module_build_state": 0,
        }
        modules.init.delay(**kwargs)
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    def test_route_modules_wait_task(self, send_task_message):
        modules.wait.delay("fakemsg", 3, 1)
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-0"

    def test_route_modules_done_task(self, send_task_message):
        modules.done.delay("fakemsg", 22, 3)
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-1"

    def test_route_modules_failed_task(self, send_task_message):
        modules.failed.delay("fakemsg", 23, 4)
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    def test_route_components_build_task_finalize_task(self, send_task_message):
        scheduler_init_data()
        components.build_task_finalize.delay(
            "fakemsg", 90276228, 1, "perl-Tangerine", "0.23", "1.module+f28+2+814cfa39")
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    def test_route_components_build_task_finalize_task_without_a_module(self, send_task_message):
        scheduler_init_data()
        components.build_task_finalize.delay(
            "fakemsg", 123456, 1, "hostname", "0.1", "1.module+f28+2+814cfa39")
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-default"

    def test_route_repos_done_task(self, send_task_message):
        scheduler_init_data()
        repos.done.delay("fakemsg", "module-testmodule-master-20170109091357-7c29193d-build")
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    def test_route_repos_done_task_without_a_module(self, send_task_message):
        scheduler_init_data()
        repos.done.delay("fakemsg", "no-module-build-exist")
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-default"

    def test_route_tags_tagged_task(self, send_task_message):
        scheduler_init_data()
        tags.tagged.delay(
            "fakemsg", "module-testmodule-master-20170109091357-7c29193d-build",
            "perl-Tangerine-0.23-1.module+f28+2+814cfa39")
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-2"

    @mock.patch("koji.ClientSession")
    def test_route_greenwave_decision_update_task(self, kojisession, send_task_message):
        kojisession.return_value.getBuild.return_value = {
            "extra": {"typeinfo": {"module": {"module_build_service_id": 1}}}
        }
        scheduler_init_data()
        greenwave.decision_update.delay(
            "fakemsg",
            decision_context="test_dec_context",
            subject_identifier="module-testmodule-master-20170109091357-7c29193d-build",
            policies_satisfied=False
        )
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-1"

    def test_route_fail_lost_builds_task(self, send_task_message):
        fail_lost_builds.delay()
        queue = send_task_message.call_args[1].get("queue")
        qname = queue.__dict__.get("name")
        assert qname == "mbs-default"
