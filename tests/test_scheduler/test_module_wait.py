# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os

import koji
import mock
from mock import patch
import pytest

from module_build_service.common import build_logs, conf
from module_build_service.common.models import ComponentBuild, ModuleBuild
from module_build_service.common.modulemd import Modulemd
import module_build_service.resolver
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.handlers.modules
from tests import scheduler_init_data

base_dir = os.path.dirname(os.path.dirname(__file__))


class TestModuleWait:
    def setup_method(self, test_method):
        scheduler_init_data()

        self.config = conf
        self.session = mock.Mock()

    def teardown_method(self, test_method):
        try:
            path = build_logs.path(db_session, 1)
            os.remove(path)
        except Exception:
            pass

    @patch("module_build_service.builder.GenericBuilder.create_from_module")
    def test_init_basic(self, create_builder):
        builder = mock.Mock()
        builder.get_disttag_srpm.return_value = "some srpm disttag"
        builder.build.return_value = 1234, 1, "", None
        builder.module_build_tag = {"name": "some-tag-build"}
        create_builder.return_value = builder

        module_build_id = db_session.query(ModuleBuild).first().id
        with patch("module_build_service.resolver.GenericResolver.create"):
            module_build_service.scheduler.handlers.modules.wait(
                msg_id="msg-id-1",
                module_build_id=module_build_id,
                module_build_state="some state")

    @patch(
        "module_build_service.builder.GenericBuilder.default_buildroot_groups",
        return_value={"build": [], "srpm-build": []},
    )
    @patch("module_build_service.builder.GenericBuilder.create_from_module")
    @patch("module_build_service.resolver.DBResolver")
    @patch("module_build_service.resolver.GenericResolver")
    def test_new_repo_called_when_macros_reused(
        self, generic_resolver, resolver, create_builder, dbg
    ):
        """
        Test that newRepo is called when module-build-macros build is reused.
        """
        koji_session = mock.MagicMock()
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.koji_session = koji_session
        builder.module_build_tag = {"name": "module-123-build"}
        builder.get_disttag_srpm.return_value = "some srpm disttag"
        builder.build.return_value = (
            1234,
            koji.BUILD_STATES["COMPLETE"],
            "",
            "module-build-macros-1-1",
        )
        create_builder.return_value = builder

        resolver = mock.MagicMock()
        resolver.backend = "db"
        resolver.get_module_tag.return_value = "module-testmodule-master-20170109091357"

        generic_resolver.create.return_value = resolver

        module_build_service.scheduler.handlers.modules.wait(
            msg_id="msg-id-1",
            module_build_id=2, module_build_state="some state")

        koji_session.newRepo.assert_called_once_with("module-123-build")

        # When module-build-macros is reused, it still has to appear only
        # once in database.
        builds_count = db_session.query(ComponentBuild).filter_by(
            package="module-build-macros", module_id=2).count()
        assert builds_count == 1

    @patch(
        "module_build_service.builder.GenericBuilder.default_buildroot_groups",
        return_value={"build": [], "srpm-build": []},
    )
    @patch("module_build_service.builder.GenericBuilder.create_from_module")
    @patch("module_build_service.resolver.DBResolver")
    @patch("module_build_service.resolver.GenericResolver")
    def test_new_repo_not_called_when_macros_not_reused(
        self, generic_resolver, resolver, create_builder, dbg
    ):
        """
        Test that newRepo is called everytime for module-build-macros
        """
        koji_session = mock.MagicMock()
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.koji_session = koji_session
        builder.module_build_tag = {"name": "module-123-build"}
        builder.get_disttag_srpm.return_value = "some srpm disttag"
        builder.build.return_value = (
            1234,
            koji.BUILD_STATES["BUILDING"],
            "",
            "module-build-macros-1-1",
        )
        create_builder.return_value = builder

        resolver = mock.MagicMock()
        resolver.backend = "db"
        resolver.get_module_tag.return_value = "module-testmodule-master-20170109091357"

        generic_resolver.create.return_value = resolver

        module_build_service.scheduler.handlers.modules.wait(
            msg_id="msg-id-1",
            module_build_id=2,
            module_build_state="some state")

        assert koji_session.newRepo.called

    @patch(
        "module_build_service.builder.GenericBuilder.default_buildroot_groups",
        return_value={"build": [], "srpm-build": []},
    )
    @patch("module_build_service.builder.GenericBuilder.create_from_module")
    @patch("module_build_service.resolver.DBResolver")
    @patch("module_build_service.resolver.GenericResolver")
    def test_set_cg_build_koji_tag_fallback_to_default(
        self, generic_resolver, resolver, create_builder, dbg
    ):
        """
        Test that build.cg_build_koji_tag fallbacks to default tag.
        """
        base_mmd = Modulemd.ModuleStreamV2.new("base-runtime", "f27")

        koji_session = mock.MagicMock()
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.koji_session = koji_session
        builder.module_build_tag = {"name": "module-123-build"}
        builder.get_disttag_srpm.return_value = "some srpm disttag"
        builder.build.return_value = (
            1234,
            koji.BUILD_STATES["BUILDING"],
            "",
            "module-build-macros-1-1",
        )
        create_builder.return_value = builder

        resolver = mock.MagicMock()
        resolver.backend = "db"
        resolver.get_module_tag.return_value = "module-testmodule-master-20170109091357"
        resolver.get_module_build_dependencies.return_value = {
            "module-bootstrap-tag": [base_mmd]
        }

        generic_resolver.create.return_value = resolver

        module_build_service.scheduler.handlers.modules.wait(
            msg_id="msg-id-1",
            module_build_id=2,
            module_build_state="some state")

        module_build = ModuleBuild.get_by_id(db_session, 2)
        assert module_build.cg_build_koji_tag == "modular-updates-candidate"

    @pytest.mark.parametrize(
        "koji_cg_tag_build,expected_cg_koji_build_tag",
        [
            [True, "f27-modular-updates-candidate"],
            [False, None]
        ],
    )
    @patch(
        "module_build_service.builder.GenericBuilder.default_buildroot_groups",
        return_value={"build": [], "srpm-build": []},
    )
    @patch("module_build_service.builder.GenericBuilder.create_from_module")
    @patch("module_build_service.resolver.DBResolver")
    @patch("module_build_service.resolver.GenericResolver")
    @patch(
        "module_build_service.common.config.Config.base_module_names",
        new_callable=mock.PropertyMock,
        return_value=["base-runtime", "platform"],
    )
    def test_set_cg_build_koji_tag(
        self,
        cfg,
        generic_resolver,
        resolver,
        create_builder,
        dbg,
        koji_cg_tag_build,
        expected_cg_koji_build_tag,
    ):
        """
        Test that build.cg_build_koji_tag is set.
        """
        base_mmd = Modulemd.ModuleStreamV2.new("base-runtime", "f27")

        koji_session = mock.MagicMock()
        koji_session.newRepo.return_value = 123456

        builder = mock.MagicMock()
        builder.koji_session = koji_session
        builder.module_build_tag = {"name": "module-123-build"}
        builder.get_disttag_srpm.return_value = "some srpm disttag"
        builder.build.return_value = (
            1234,
            koji.BUILD_STATES["BUILDING"],
            "",
            "module-build-macros-1-1",
        )
        create_builder.return_value = builder

        resolver = mock.MagicMock()
        resolver.backend = "db"
        resolver.get_module_tag.return_value = "module-testmodule-master-20170109091357"
        resolver.get_module_build_dependencies.return_value = {
            "module-bootstrap-tag": [base_mmd]
        }

        with patch.object(
            module_build_service.scheduler.handlers.modules.conf,
            "koji_cg_tag_build",
            new=koji_cg_tag_build,
        ):
            generic_resolver.create.return_value = resolver
            module_build_service.scheduler.handlers.modules.wait(
                msg_id="msg-id-1",
                module_build_id=2,
                module_build_state="some state")
            module_build = ModuleBuild.get_by_id(db_session, 2)
            assert module_build.cg_build_koji_tag == expected_cg_koji_build_tag
