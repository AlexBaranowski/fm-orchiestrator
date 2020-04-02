# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime, timedelta
import re

import koji
import mock
from mock import call, patch
import pytest

from module_build_service.common.config import conf
from module_build_service.common import models
from module_build_service.scheduler import producer
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, make_module_in_db


@pytest.mark.usefixtures("reuse_component_init_data")
@patch(
    "module_build_service.builder.GenericBuilder.default_buildroot_groups",
    return_value={"build": [], "srpm-build": []},
)
@patch("module_build_service.builder.GenericBuilder.create_from_module")
class TestPoller:
    def setup_method(self, test_method):
        self.p_read_config = patch(
            "koji.read_config",
            return_value={
                "authtype": "kerberos",
                "timeout": 60,
                "server": "http://koji.example.com/",
            },
        )
        self.mock_read_config = self.p_read_config.start()

    def teardown_method(self, test_method):
        self.p_read_config.stop()
        clean_database()

    @pytest.mark.parametrize("fresh", [True, False])
    @patch("module_build_service.scheduler.batches.start_build_component")
    def test_process_paused_module_builds(
        self, start_build_component, create_builder, dbg, fresh
    ):
        """
        Tests general use-case of process_paused_module_builds.
        """
        builder = mock.MagicMock()
        create_builder.return_value = builder

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2
        # If fresh is set, then we simulate that activity just occurred 2 minutes ago on the build
        if fresh:
            module_build.time_modified = datetime.utcnow() - timedelta(minutes=2)
        else:
            module_build.time_modified = datetime.utcnow() - timedelta(days=5)
        db_session.commit()

        # Poll :)
        producer.process_paused_module_builds()

        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        # If fresh is set, we expect the poller to not touch the module build since it's been less
        # than 10 minutes of inactivity
        if fresh:
            expected_state = None
            expected_build_calls = 0
        else:
            expected_state = koji.BUILD_STATES["BUILDING"]
            expected_build_calls = 2

        components = module_build.current_batch()
        for component in components:
            assert component.state == expected_state

        assert len(start_build_component.mock_calls) == expected_build_calls

    @pytest.mark.parametrize('task_state, expect_start_build_component', (
        (None, True),  # Indicates a newRepo task has not been triggered yet.
        (koji.TASK_STATES["CLOSED"], True),
        (koji.TASK_STATES["OPEN"], False),
    ))
    @patch("module_build_service.scheduler.batches.start_build_component")
    def test_process_paused_module_builds_with_new_repo_task(
        self, start_build_component, create_builder, dbg, task_state,
        expect_start_build_component
    ):
        """
        Tests general use-case of process_paused_module_builds.
        """
        builder = mock.MagicMock()
        create_builder.return_value = builder

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2
        module_build.time_modified = datetime.utcnow() - timedelta(days=5)
        if task_state:
            koji_session = mock.MagicMock()
            koji_session.getTaskInfo.return_value = {"state": task_state}
            builder.koji_session = koji_session
            module_build.new_repo_task_id = 123
        db_session.commit()

        # Poll :)
        producer.process_paused_module_builds()

        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        if expect_start_build_component:
            expected_state = koji.BUILD_STATES["BUILDING"]
            expected_build_calls = 2
        else:
            expected_state = None
            expected_build_calls = 0

        components = module_build.current_batch()
        for component in components:
            assert component.state == expected_state

        assert len(start_build_component.mock_calls) == expected_build_calls

    @patch("koji.ClientSession")
    def test_retrigger_new_repo_on_failure(self, ClientSession, create_builder, dbg):
        """
        Tests that we call koji_sesion.newRepo when newRepo task failed.
        """
        koji_session = ClientSession.return_value
        koji_session.getTag = lambda tag_name: {"name": tag_name}
        koji_session.getTaskInfo.return_value = {"state": koji.TASK_STATES["FAILED"]}
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.buildroot_ready.return_value = False
        create_builder.return_value = builder

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2
        module_build.new_repo_task_id = 123456
        db_session.commit()

        producer.retrigger_new_repo_on_failure()

        koji_session.newRepo.assert_called_once_with(
            "module-testmodule-master-20170219191323-c40c156c-build")

    @patch("koji.ClientSession")
    def test_trigger_new_repo_when_succeeded(self, ClientSession, create_builder, dbg):
        """
        Tests that we do not call koji_sesion.newRepo when newRepo task
        succeeded.
        """
        koji_session = ClientSession.return_value
        koji_session.getTag = lambda tag_name: {"name": tag_name}
        koji_session.getTaskInfo.return_value = {"state": koji.TASK_STATES["CLOSED"]}
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.buildroot_ready.return_value = False
        create_builder.return_value = builder

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2
        module_build.new_repo_task_id = 123456
        db_session.commit()

        producer.retrigger_new_repo_on_failure()

        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        assert not koji_session.newRepo.called
        assert module_build.new_repo_task_id == 123456

    def test_process_paused_module_builds_waiting_for_repo(self, create_builder, dbg):
        """
        Tests that process_paused_module_builds does not start new batch
        when we are waiting for repo.
        """
        builder = mock.MagicMock()
        create_builder.return_value = builder

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2
        module_build.new_repo_task_id = 123456
        db_session.commit()

        # Poll :)
        producer.process_paused_module_builds()

        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        # Components should not be in building state
        components = module_build.current_batch()
        for component in components:
            assert component.state is None

    @patch("koji.ClientSession")
    def test_old_build_targets_are_not_associated_with_any_module_builds(
        self, ClientSession, create_builder, dbg
    ):
        koji_session = ClientSession.return_value
        # No created module build has any of these tags.
        koji_session.getBuildTargets.return_value = [
            {"dest_tag_name": "module-xxx-1"},
            {"dest_tag_name": "module-yyy-2"},
        ]

        producer.delete_old_koji_targets()

        koji_session.deleteBuildTarget.assert_not_called()

    @patch("koji.ClientSession")
    def test_dont_delete_base_module_build_target(
        self, ClientSession, create_builder, dbg
    ):
        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        koji_session = ClientSession.return_value
        # No created module build has any of these tags.
        koji_session.getBuildTargets.return_value = [{"dest_tag_name": module_build.koji_tag}]

        # If module build's name is one of base module names, build target
        # should not be deleted.
        with patch.object(conf, "base_module_names", new=[module_build.name]):
            producer.delete_old_koji_targets()

        koji_session.deleteBuildTarget.assert_not_called()

    @patch("koji.ClientSession")
    def test_dont_delete_build_target_for_unfinished_module_builds(
        self, ClientSession, create_builder, dbg
    ):
        module_build = models.ModuleBuild.get_by_id(db_session, 3)

        koji_session = ClientSession.return_value
        # No created module build has any of these tags.
        koji_session.getBuildTargets.return_value = [{"dest_tag_name": module_build.koji_tag}]

        # Each time when a module build is in one of these state, build target
        # should not be deleted.
        for state in ["init", "wait", "build"]:
            module_build.state = state
            db_session.commit()

            producer.delete_old_koji_targets()

            koji_session.deleteBuildTarget.assert_not_called()

    @patch("koji.ClientSession")
    def test_only_delete_build_target_with_allowed_koji_tag_prefix(
        self, ClientSession, create_builder, dbg
    ):
        module_build_2 = models.ModuleBuild.get_by_id(db_session, 2)
        # Only module build 1's build target should be deleted.
        module_build_2.koji_tag = "module-tag1"
        module_build_2.state = models.BUILD_STATES["done"]
        # Ensure to exceed the koji_target_delete_time easily later for deletion
        module_build_2.time_completed = datetime.utcnow() - timedelta(hours=24)

        module_build_3 = models.ModuleBuild.get_by_id(db_session, 3)
        module_build_3.koji_tag = "f28"

        db_session.commit()
        db_session.refresh(module_build_2)
        db_session.refresh(module_build_3)

        koji_session = ClientSession.return_value
        # No created module build has any of these tags.
        koji_session.getBuildTargets.return_value = [
            {"id": 1, "dest_tag_name": module_build_2.koji_tag, "name": module_build_2.koji_tag},
            {"id": 2, "dest_tag_name": module_build_3.koji_tag, "name": module_build_3.koji_tag},
        ]

        with patch.object(conf, "koji_tag_prefixes", new=["module", "another-prefix"]):
            with patch.object(conf, "koji_target_delete_time", new=60):
                producer.delete_old_koji_targets()

            koji_session.deleteBuildTarget.assert_called_once_with(1)
            koji_session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_cant_delete_build_target_if_not_reach_delete_time(
        self, ClientSession, create_builder, dbg
    ):
        module_build_2 = models.ModuleBuild.get_by_id(db_session, 2)
        # Only module build 1's build target should be deleted.
        module_build_2.koji_tag = "module-tag1"
        module_build_2.state = models.BUILD_STATES["done"]
        # Ensure to exceed the koji_target_delete_time easily later for deletion
        module_build_2.time_completed = datetime.utcnow() - timedelta(minutes=5)

        db_session.commit()
        db_session.refresh(module_build_2)

        koji_session = ClientSession.return_value
        # No created module build has any of these tags.
        koji_session.getBuildTargets.return_value = [
            {"id": 1, "dest_tag_name": module_build_2.koji_tag, "name": module_build_2.koji_tag}
        ]

        with patch.object(conf, "koji_tag_prefixes", new=["module"]):
            # Use default koji_target_delete_time in config. That time is long
            # enough for test.
            producer.delete_old_koji_targets()

            koji_session.deleteBuildTarget.assert_not_called()

    @pytest.mark.parametrize("state", ["init", "wait"])
    @patch.dict(producer.ON_MODULE_CHANGE_HANDLERS, clear=True, values={
        models.BUILD_STATES["init"]: mock.Mock(),
        models.BUILD_STATES["wait"]: mock.Mock(),
    })
    def test_process_waiting_module_build(self, create_builder, dbg, state):
        """ Test that processing old waiting module builds works. """

        handler = producer.ON_MODULE_CHANGE_HANDLERS[models.BUILD_STATES[state]]

        # Change the batch to 2, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.state = models.BUILD_STATES[state]
        original = datetime.utcnow() - timedelta(minutes=11)
        module_build.time_modified = original

        db_session.commit()
        db_session.refresh(module_build)

        # Poll :)
        producer.process_waiting_module_builds()

        handler.delay.assert_called_once_with(
            "internal:mbs.module.state.change",
            module_build.id,
            module_build.state
        )

        db_session.refresh(module_build)
        # ensure the time_modified was changed.
        assert module_build.time_modified > original

    @pytest.mark.parametrize("state", ["init", "wait"])
    @patch.dict(producer.ON_MODULE_CHANGE_HANDLERS, clear=True, values={
        models.BUILD_STATES["init"]: mock.Mock(),
        models.BUILD_STATES["wait"]: mock.Mock(),
    })
    def test_process_waiting_module_build_not_old_enough(
        self, create_builder, dbg, state
    ):
        """ Test that we do not process young waiting builds. """

        handler = producer.ON_MODULE_CHANGE_HANDLERS[models.BUILD_STATES[state]]

        # Change the batch to build, so the module build is in state where
        # it is not building anything, but the state is "build".
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.state = models.BUILD_STATES[state]
        original = datetime.utcnow() - timedelta(minutes=9)
        module_build.time_modified = original

        db_session.commit()
        db_session.refresh(module_build)

        # Poll :)
        producer.process_waiting_module_builds()

        handler.assert_not_called()

    @patch.dict(producer.ON_MODULE_CHANGE_HANDLERS, clear=True, values={
        models.BUILD_STATES["init"]: mock.Mock(),
        models.BUILD_STATES["wait"]: mock.Mock(),
    })
    def test_process_waiting_module_build_none_found(self, create_builder, dbg):
        """ Test nothing happens when no module builds are waiting. """
        # Poll :)
        producer.process_waiting_module_builds()

        # Ensure we did *not* process any of the non-waiting builds.
        for handler in producer.ON_MODULE_CHANGE_HANDLERS.values():
            handler.assert_not_called()

    def test_cleanup_stale_failed_builds(self, create_builder, dbg):
        """ Test that one of the two module builds gets to the garbage state when running
        cleanup_stale_failed_builds.
        """
        builder = mock.MagicMock()
        create_builder.return_value = builder

        module_build_one = models.ModuleBuild.get_by_id(db_session, 2)
        module_build_one.state = models.BUILD_STATES["failed"]
        module_build_one.time_modified = datetime.utcnow() - timedelta(
            days=conf.cleanup_failed_builds_time + 1)

        module_build_two = models.ModuleBuild.get_by_id(db_session, 3)
        module_build_two.time_modified = datetime.utcnow()
        module_build_two.state = models.BUILD_STATES["failed"]

        failed_component = db_session.query(models.ComponentBuild).filter_by(
            package="tangerine", module_id=3).one()
        failed_component.state = koji.BUILD_STATES["FAILED"]
        failed_component.tagged = False
        failed_component.tagged_in_final = False

        db_session.commit()

        producer.cleanup_stale_failed_builds()

        db_session.refresh(module_build_two)
        # Make sure module_build_one was transitioned to garbage
        assert module_build_one.state == models.BUILD_STATES["garbage"]
        state_reason = (
            "The module was garbage collected since it has failed over {0} day(s) ago"
            .format(conf.cleanup_failed_builds_time)
        )
        assert module_build_one.state_reason == state_reason
        # Make sure all the components are marked as untagged in the database
        for component in module_build_one.component_builds:
            assert not component.tagged
            assert not component.tagged_in_final
        # Make sure module_build_two stayed the same
        assert module_build_two.state == models.BUILD_STATES["failed"]
        # Make sure the builds were untagged
        builder.untag_artifacts.assert_called_once()
        args, _ = builder.untag_artifacts.call_args
        expected = [
            "module-build-macros-0.1-1.module+0+d027b723",
            "perl-List-Compare-0.53-5.module+0+d027b723",
            "perl-Tangerine-0.23-1.module+0+d027b723",
            "tangerine-0.22-3.module+0+d027b723",
        ]
        assert expected == sorted(args[0])

    def test_cleanup_stale_failed_builds_no_components(self, create_builder, dbg):
        """ Test that a module build without any components built gets to the garbage state when
        running cleanup_stale_failed_builds.
        """
        module_build_one = models.ModuleBuild.get_by_id(db_session, 1)
        module_build_one.state = models.BUILD_STATES["failed"]
        module_build_one.time_modified = datetime.utcnow()

        module_build_two = models.ModuleBuild.get_by_id(db_session, 2)
        module_build_two.state = models.BUILD_STATES["failed"]
        module_build_two.time_modified = datetime.utcnow() - timedelta(
            days=conf.cleanup_failed_builds_time + 1)
        module_build_two.koji_tag = None
        module_build_two.cg_build_koji_tag = None

        for c in module_build_two.component_builds:
            c.state = None

        db_session.commit()

        producer.cleanup_stale_failed_builds()

        db_session.refresh(module_build_two)
        # Make sure module_build_two was transitioned to garbage
        assert module_build_two.state == models.BUILD_STATES["garbage"]
        state_reason = (
            "The module was garbage collected since it has failed over {0} day(s) ago"
            .format(conf.cleanup_failed_builds_time)
        )
        assert module_build_two.state_reason == state_reason
        # Make sure module_build_one stayed the same
        assert module_build_one.state == models.BUILD_STATES["failed"]
        # Make sure that the builder was never instantiated
        create_builder.assert_not_called()

    @pytest.mark.parametrize(
        "test_state", [models.BUILD_STATES[state] for state in conf.cleanup_stuck_builds_states]
    )
    def test_cancel_stuck_module_builds(self, create_builder, dbg, test_state):

        module_build1 = models.ModuleBuild.get_by_id(db_session, 1)
        module_build1.state = test_state
        under_thresh = conf.cleanup_stuck_builds_time - 1
        module_build1.time_modified = datetime.utcnow() - timedelta(
            days=under_thresh, hours=23, minutes=59)

        module_build2 = models.ModuleBuild.get_by_id(db_session, 2)
        module_build2.state = test_state
        module_build2.time_modified = datetime.utcnow() - timedelta(
            days=conf.cleanup_stuck_builds_time)

        module_build2 = models.ModuleBuild.get_by_id(db_session, 3)
        module_build2.state = test_state
        module_build2.time_modified = datetime.utcnow()

        db_session.commit()

        producer.cancel_stuck_module_builds()

        module = models.ModuleBuild.by_state(db_session, "failed")
        assert len(module) == 1
        assert module[0].id == 2

    @pytest.mark.parametrize("tagged", (True, False))
    @pytest.mark.parametrize("tagged_in_final", (True, False))
    @pytest.mark.parametrize("btime", (True, False))
    @patch("koji.ClientSession")
    @patch("module_build_service.scheduler.producer.tagged")
    def test_sync_koji_build_tags(
        self, tagged_handler, ClientSession, create_builder, dbg,
        tagged, tagged_in_final, btime
    ):
        module_build_2 = models.ModuleBuild.get_by_id(db_session, 2)
        # Only module build 1's build target should be deleted.
        module_build_2.koji_tag = "module-tag1"
        module_build_2.state = models.BUILD_STATES["build"]
        if btime:
            module_build_2.time_modified = datetime.utcnow() - timedelta(minutes=12)

        c = module_build_2.current_batch()[0]
        c.state = koji.BUILD_STATES["COMPLETE"]
        c.tagged_in_final = False
        c.tagged = False

        db_session.commit()
        db_session.refresh(module_build_2)

        koji_session = ClientSession.return_value
        # No created module build has any of these tags.

        listtags_return_value = []
        expected_tagged_calls = []

        if btime:
            if tagged:
                listtags_return_value.append(
                    {"id": 1, "name": module_build_2.koji_tag + "-build"})
                expected_tagged_calls.append(call(
                    "internal:sync_koji_build_tags",
                    module_build_2.koji_tag + "-build", c.nvr
                ))
            if tagged_in_final:
                listtags_return_value.append(
                    {"id": 2, "name": module_build_2.koji_tag})
                expected_tagged_calls.append(call(
                    "internal:sync_koji_build_tags",
                    module_build_2.koji_tag, c.nvr
                ))
        koji_session.listTags.return_value = listtags_return_value

        producer.sync_koji_build_tags()

        tagged_handler.delay.assert_has_calls(
            expected_tagged_calls, any_order=True)

    @pytest.mark.parametrize("greenwave_result", [True, False])
    @patch("module_build_service.scheduler.greenwave.Greenwave.check_gating")
    def test_poll_greenwave(self, mock_gw, create_builder, dbg, greenwave_result):

        module_build1 = models.ModuleBuild.get_by_id(db_session, 1)
        module_build1.state = models.BUILD_STATES["ready"]

        module_build2 = models.ModuleBuild.get_by_id(db_session, 2)
        module_build2.state = models.BUILD_STATES["done"]

        module_build3 = models.ModuleBuild.get_by_id(db_session, 3)
        module_build3.state = models.BUILD_STATES["init"]

        module_build4 = make_module_in_db("foo:1:1:1", {})
        module_build4.state = models.BUILD_STATES["done"]
        module_build4.scratch = True

        db_session.commit()

        mock_gw.return_value = greenwave_result

        producer.poll_greenwave()

        mock_gw.assert_called_once()
        modules = models.ModuleBuild.by_state(db_session, "ready")

        if greenwave_result:
            assert len(modules) == 2
            assert {m.id for m in modules} == {1, 2}
        else:
            assert len(modules) == 1
            assert modules[0].id == 1
            modules = models.ModuleBuild.by_state(db_session, "done")
            assert len(modules) == 2
            for module in modules:
                assert module.id in [2, 4]
                if module.id == 2:
                    assert re.match("Gating failed.*", module.state_reason)
                else:
                    assert module.state_reason is None
