# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import koji
import mock
from mock import patch
import pytest

from module_build_service.common.config import conf
from module_build_service.builder import GenericBuilder
from module_build_service.builder.KojiModuleBuilder import KojiModuleBuilder
from module_build_service.builder.utils import validate_koji_tag
from module_build_service.common import models
from module_build_service.scheduler import events
from module_build_service.scheduler.batches import start_build_component, start_next_batch_build
from module_build_service.scheduler.db_session import db_session


class DummyModuleBuilder(GenericBuilder):
    """
    Dummy module builder
    """

    backend = "koji"
    _build_id = 0

    TAGGED_COMPONENTS = []

    @validate_koji_tag("tag_name")
    def __init__(self, db_session, owner, module, config, tag_name, components):
        self.db_session = db_session
        self.module_str = module
        self.tag_name = tag_name
        self.config = config

    def buildroot_connect(self, groups):
        pass

    def buildroot_prep(self):
        pass

    def buildroot_resume(self):
        pass

    def buildroot_ready(self, artifacts=None):
        return True

    def buildroot_add_dependency(self, dependencies):
        pass

    def buildroot_add_artifacts(self, artifacts, install=False):
        DummyModuleBuilder.TAGGED_COMPONENTS += artifacts

    def buildroot_add_repos(self, dependencies):
        pass

    def tag_artifacts(self, artifacts):
        pass

    def recover_orphaned_artifact(self, component_build):
        return []

    @property
    def module_build_tag(self):
        return {"name": self.tag_name + "-build"}

    def build(self, artifact_name, source):
        DummyModuleBuilder._build_id += 1
        state = koji.BUILD_STATES["COMPLETE"]
        reason = "Submitted %s to Koji" % (artifact_name)
        return DummyModuleBuilder._build_id, state, reason, None

    @staticmethod
    def get_disttag_srpm(disttag, module_build):
        # @FIXME
        return KojiModuleBuilder.get_disttag_srpm(disttag, module_build)

    def cancel_build(self, task_id):
        pass

    def list_tasks_for_components(self, component_builds=None, state="active"):
        pass

    def repo_from_tag(self, config, tag_name, arch):
        pass

    def finalize(self, succeeded=True):
        pass


@pytest.mark.usefixtures("reuse_component_init_data")
@patch(
    "module_build_service.builder.GenericBuilder.default_buildroot_groups",
    return_value={"build": [], "srpm-build": []},
)
class TestBatches:
    def setup_method(self, test_method):
        GenericBuilder.register_backend_class(DummyModuleBuilder)
        events.scheduler.reset()

    def teardown_method(self, test_method):
        # clean_database()
        DummyModuleBuilder.TAGGED_COMPONENTS = []
        GenericBuilder.register_backend_class(KojiModuleBuilder)
        events.scheduler.reset()

    def test_start_next_batch_build_reuse(self, default_buildroot_groups):
        """
        Tests that start_next_batch_build:
           1) Increments module.batch.
           2) Can reuse all components in batch
           3) Returns proper further_work messages for reused components.
           4) Returns the fake Repo change message
           5) Handling the further_work messages lead to proper tagging of
              reused components.
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 1

        builder = mock.MagicMock()
        builder.module_build_tag = {"name": "module-fedora-27-build"}
        start_next_batch_build(
            conf, module_build, builder)

        # Batch number should increase.
        assert module_build.batch == 2

        # buildsys.build.state.change messages in further_work should have
        # build_new_state set to COMPLETE, but the current component build
        # state should be set to BUILDING, so KojiBuildChange message handler
        # handles the change properly.
        for event in events.scheduler.queue:
            event_info = event[3]
            if event_info[0].startswith("reuse_component"):
                assert event_info[2] == koji.BUILD_STATES["COMPLETE"]
                component_build = models.ComponentBuild.from_component_event(
                    db_session,
                    task_id=event_info[1],
                    module_id=event_info[6])
                assert component_build.state == koji.BUILD_STATES["BUILDING"]

        # When we handle these KojiBuildChange messages, MBS should tag all
        # the components just once.
        events.scheduler.run()

        # Check that packages have been tagged just once.
        assert len(DummyModuleBuilder.TAGGED_COMPONENTS) == 2

    @patch("module_build_service.scheduler.batches.start_build_component")
    def test_start_next_batch_build_reuse_some(
        self, mock_sbc, default_buildroot_groups
    ):
        """
        Tests that start_next_batch_build:
           1) Increments module.batch.
           2) Can reuse all components in the batch that it can.
           3) Returns proper further_work messages for reused components.
           4) Builds the remaining components
           5) Handling the further_work messages lead to proper tagging of
              reused components.
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 1

        plc_component = models.ComponentBuild.from_component_name(
            db_session, "perl-List-Compare", 3)
        plc_component.ref = "5ceea46add2366d8b8c5a623a2fb563b625b9abd"

        builder = mock.MagicMock()
        builder.recover_orphaned_artifact.return_value = []

        start_next_batch_build(conf, module_build, builder)

        # Batch number should increase.
        assert module_build.batch == 2

        # Make sure we only have one message returned for the one reused component
        assert len(events.scheduler.queue) == 1
        # The KojiBuildChange message in further_work should have build_new_state
        # set to COMPLETE, but the current component build state in the DB should be set
        # to BUILDING, so KojiBuildChange message handler handles the change
        # properly.
        event_info = events.scheduler.queue[0][3]
        assert event_info == ('reuse_component: fake msg', 90276227, 1, 'perl-Tangerine',
                              '0.23', '1.module+0+d027b723', 3,
                              'Reused component from previous module build')
        component_build = models.ComponentBuild.from_component_event(
            db_session,
            task_id=event_info[1],
            module_id=event_info[6],
        )
        assert component_build.state == koji.BUILD_STATES["BUILDING"]
        assert component_build.package == "perl-Tangerine"
        assert component_build.reused_component_id is not None
        # Make sure perl-List-Compare is set to the build state as well but not reused
        assert plc_component.state == koji.BUILD_STATES["BUILDING"]
        assert plc_component.reused_component_id is None
        mock_sbc.assert_called_once()

    @patch("module_build_service.scheduler.batches.start_build_component")
    @patch(
        "module_build_service.common.config.Config.rebuild_strategy",
        new_callable=mock.PropertyMock,
        return_value="all",
    )
    def test_start_next_batch_build_rebuild_strategy_all(
        self, mock_rm, mock_sbc, default_buildroot_groups
    ):
        """
        Tests that start_next_batch_build can't reuse any components in the batch because the
        rebuild method is set to "all".
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.rebuild_strategy = "all"
        module_build.batch = 1

        builder = mock.MagicMock()
        builder.recover_orphaned_artifact.return_value = []
        start_next_batch_build(conf, module_build, builder)

        # Batch number should increase.
        assert module_build.batch == 2
        # No component reuse messages should be returned
        assert len(events.scheduler.queue) == 0
        # Make sure that both components in the batch were submitted
        assert len(mock_sbc.mock_calls) == 2

    def test_start_build_component_failed_state(self, default_buildroot_groups):
        """
        Tests whether exception occured while building sets the state to failed
        """
        builder = mock.MagicMock()
        builder.build.side_effect = Exception("Something have gone terribly wrong")
        component = mock.MagicMock()

        start_build_component(db_session, builder, component)

        assert component.state == koji.BUILD_STATES["FAILED"]

    @patch("module_build_service.scheduler.batches.start_build_component")
    @patch(
        "module_build_service.common.config.Config.rebuild_strategy",
        new_callable=mock.PropertyMock,
        return_value="only-changed",
    )
    def test_start_next_batch_build_rebuild_strategy_only_changed(
        self, mock_rm, mock_sbc, default_buildroot_groups
    ):
        """
        Tests that start_next_batch_build reuses all unchanged components in the batch because the
        rebuild method is set to "only-changed". This means that one component is reused in batch
        2, and even though the other component in batch 2 changed and was rebuilt, the component
        in batch 3 can be reused.
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.rebuild_strategy = "only-changed"
        module_build.batch = 1
        # perl-List-Compare changed
        plc_component = models.ComponentBuild.from_component_name(
            db_session, "perl-List-Compare", 3)
        plc_component.ref = "5ceea46add2366d8b8c5a623a2fb563b625b9abd"

        builder = mock.MagicMock()
        builder.recover_orphaned_artifact.return_value = []
        start_next_batch_build(conf, module_build, builder)

        # Batch number should increase
        assert module_build.batch == 2

        # Make sure we only have one message returned for the one reused component
        assert len(events.scheduler.queue) == 1
        # The buildsys.build.state.change message in further_work should have
        # build_new_state set to COMPLETE, but the current component build state
        # in the DB should be set to BUILDING, so the build state change handler
        # handles the change properly.
        event_info = events.scheduler.queue[0][3]
        assert event_info == ('reuse_component: fake msg', 90276227, 1,
                              'perl-Tangerine', '0.23', '1.module+0+d027b723', 3,
                              'Reused component from previous module build')
        component_build = models.ComponentBuild.from_component_event(
            db_session,
            task_id=event_info[1],
            module_id=event_info[6],
        )
        assert component_build.state == koji.BUILD_STATES["BUILDING"]
        assert component_build.package == "perl-Tangerine"
        assert component_build.reused_component_id is not None
        # Make sure perl-List-Compare is set to the build state as well but not reused
        assert plc_component.state == koji.BUILD_STATES["BUILDING"]
        assert plc_component.reused_component_id is None
        mock_sbc.assert_called_once()
        mock_sbc.reset_mock()

        # Complete the build
        plc_component.state = koji.BUILD_STATES["COMPLETE"]
        pt_component = models.ComponentBuild.from_component_name(
            db_session, "perl-Tangerine", 3)
        pt_component.state = koji.BUILD_STATES["COMPLETE"]

        events.scheduler.reset()

        # Start the next build batch
        start_next_batch_build(conf, module_build, builder)
        # Batch number should increase
        assert module_build.batch == 3
        # Verify that tangerine was reused even though perl-Tangerine was rebuilt in the previous
        # batch
        event_info = events.scheduler.queue[0][3]
        assert event_info == ('reuse_component: fake msg', 90276315, 1, 'tangerine', '0.22',
                              '3.module+0+d027b723', 3,
                              'Reused component from previous module build')
        component_build = models.ComponentBuild.from_component_event(
            db_session,
            task_id=event_info[1],
            module_id=event_info[6],
        )
        assert component_build.state == koji.BUILD_STATES["BUILDING"]
        assert component_build.package == "tangerine"
        assert component_build.reused_component_id is not None
        mock_sbc.assert_not_called()

    @patch("module_build_service.scheduler.batches.start_build_component")
    def test_start_next_batch_build_smart_scheduling(
        self, mock_sbc, default_buildroot_groups
    ):
        """
        Tests that components with the longest build time will be scheduled first
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 1
        pt_component = models.ComponentBuild.from_component_name(
            db_session, "perl-Tangerine", 3)
        pt_component.ref = "6ceea46add2366d8b8c5a623b2fb563b625bfabe"
        plc_component = models.ComponentBuild.from_component_name(
            db_session, "perl-List-Compare", 3)
        plc_component.ref = "5ceea46add2366d8b8c5a623a2fb563b625b9abd"

        # Components are by default built by component id. To find out that weight is respected,
        # we have to set bigger weight to component with lower id.
        pt_component.weight = 3 if pt_component.id < plc_component.id else 4
        plc_component.weight = 4 if pt_component.id < plc_component.id else 3

        builder = mock.MagicMock()
        builder.recover_orphaned_artifact.return_value = []
        start_next_batch_build(conf, module_build, builder)

        # Batch number should increase.
        assert module_build.batch == 2

        # Make sure we don't have any messages returned since no components should be reused
        assert len(events.scheduler.queue) == 0
        # Make sure both components are set to the build state but not reused
        assert pt_component.state == koji.BUILD_STATES["BUILDING"]
        assert pt_component.reused_component_id is None
        assert plc_component.state == koji.BUILD_STATES["BUILDING"]
        assert plc_component.reused_component_id is None

        # Test the order of the scheduling
        expected_calls = [
            mock.call(db_session, builder, plc_component),
            mock.call(db_session, builder, pt_component)
        ]
        assert mock_sbc.mock_calls == expected_calls

    @patch("module_build_service.scheduler.batches.start_build_component")
    def test_start_next_batch_continue(self, mock_sbc, default_buildroot_groups):
        """
        Tests that start_next_batch_build does not start new batch when
        there are unbuilt components in the current one.
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 2

        # The component was reused when the batch first started
        building_component = module_build.current_batch()[0]
        building_component.state = koji.BUILD_STATES["BUILDING"]
        db_session.commit()

        builder = mock.MagicMock()
        start_next_batch_build(conf, module_build, builder)

        # Batch number should not increase.
        assert module_build.batch == 2
        # Make sure start build was called for the second component which wasn't reused
        mock_sbc.assert_called_once()
        # No further work should be returned

        assert len(events.scheduler.queue) == 0

    def test_start_next_batch_build_repo_building(self, default_buildroot_groups):
        """
        Test that start_next_batch_build does not start new batch when
        builder.buildroot_ready() returns False.
        """
        module_build = models.ModuleBuild.get_by_id(db_session, 3)
        module_build.batch = 1
        db_session.commit()

        builder = mock.MagicMock()
        builder.buildroot_ready.return_value = False

        # Batch number should not increase.
        assert module_build.batch == 1
