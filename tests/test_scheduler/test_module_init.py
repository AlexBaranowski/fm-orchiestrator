# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os

from mock import patch, PropertyMock

from module_build_service.common import build_logs, conf
from module_build_service.common.models import BUILD_STATES, ModuleBuild
from module_build_service.common.utils import load_mmd, mmd_to_str
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.handlers.modules
from tests import clean_database, read_staged_data
from tests.test_web.test_views import FakeSCM


class TestModuleInit:
    def setup_method(self, test_method):
        self.fn = module_build_service.scheduler.handlers.modules.init
        testmodule_yml_path = read_staged_data("testmodule_init")
        mmd = load_mmd(testmodule_yml_path)
        # Set the name and stream
        mmd = mmd.copy("testmodule", "1")
        scmurl = "git://pkgs.domain.local/modules/testmodule?#620ec77"
        clean_database()
        ModuleBuild.create(
            db_session, conf, "testmodule", "1", 3, mmd_to_str(mmd), scmurl, "mprahl")

    def teardown_method(self, test_method):
        try:
            path = build_logs.path(db_session, 1)
            os.remove(path)
        except Exception:
            pass

    @patch(
        "module_build_service.builder.KojiModuleBuilder.KojiModuleBuilder."
        "get_built_rpms_in_module_build"
    )
    @patch("module_build_service.common.scm.SCM")
    @patch("module_build_service.scheduler.handlers.modules.handle_stream_collision_modules")
    @patch(
        "module_build_service.scheduler.handlers.modules.handle_collisions_with_base_module_rpms"
    )
    @patch("module_build_service.scheduler.submit.get_build_arches", return_value=["x86_64"])
    def init_basic(self, db_session, get_build_arches, hcwbmr, rscm, mocked_scm, built_rpms):
        FakeSCM(
            mocked_scm,
            "testmodule",
            "testmodule_init.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )

        built_rpms.return_value = [
            "foo-0:2.4.48-3.el8+1308+551bfa71",
            "foo-debuginfo-0:2.4.48-3.el8+1308+551bfa71",
            "bar-0:2.5.48-3.el8+1308+551bfa71",
            "bar-debuginfo-0:2.5.48-3.el8+1308+551bfa71",
            "x-0:2.5.48-3.el8+1308+551bfa71",
            "x-debuginfo-0:2.5.48-3.el8+1308+551bfa71",
        ]

        platform_build = ModuleBuild.get_by_id(db_session, 1)
        mmd = platform_build.mmd()
        for rpm in mmd.get_rpm_filters():
            mmd.remove_rpm_filter(rpm)
        mmd.add_rpm_filter("foo")
        mmd.add_rpm_filter("bar")

        platform_build.modulemd = mmd_to_str(mmd)
        db_session.commit()

        self.fn(msg_id="msg-id-1", module_build_id=2, module_build_state="init")

        build = ModuleBuild.get_by_id(db_session, 2)
        # Make sure the module entered the wait state
        assert build.state == 1, build.state
        # Make sure format_mmd was run properly
        xmd_mbs = build.mmd().get_xmd()["mbs"]
        assert xmd_mbs["buildrequires"]["platform"]["filtered_rpms"] == [
            "foo-0:2.4.48-3.el8+1308+551bfa71",
            "bar-0:2.5.48-3.el8+1308+551bfa71",
        ]
        hcwbmr.assert_called_once()
        return build

    def test_init_called_twice(self):
        build = self.init_basic(db_session)
        old_component_builds = len(build.component_builds)
        old_mmd = load_mmd(build.modulemd)

        build.state = 4
        db_session.commit()
        build = self.init_basic(db_session)
        db_session.refresh(build)

        assert build.state == 1
        assert old_component_builds == len(build.component_builds)

        new_mmd = load_mmd(build.modulemd)
        # Compare only lengths, because `mmd_to_str` can shuffle the fields randomly.
        assert len(mmd_to_str(old_mmd)) == len(mmd_to_str(new_mmd))

    @patch("module_build_service.common.scm.SCM")
    @patch("module_build_service.scheduler.submit.get_build_arches", return_value=["x86_64"])
    def test_init_scm_not_available(self, get_build_arches, mocked_scm):
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            get_latest_raise=True,
            get_latest_error=RuntimeError("Failed in mocked_scm_get_latest")
        )

        self.fn(msg_id="msg-id-1", module_build_id=2, module_build_state="init")

        build = ModuleBuild.get_by_id(db_session, 2)
        # Make sure the module entered the failed state
        # since the git server is not available
        assert build.state == 4, build.state

    @patch(
        "module_build_service.common.config.Config.modules_allow_repository",
        new_callable=PropertyMock,
        return_value=True,
    )
    @patch("module_build_service.common.scm.SCM")
    @patch("module_build_service.scheduler.submit.get_build_arches", return_value=["x86_64"])
    def test_init_includedmodule(
        self, get_build_arches, mocked_scm, mocked_mod_allow_repo
    ):
        FakeSCM(mocked_scm, "includedmodules", ["testmodule_init.yaml"])
        includedmodules_yml_path = read_staged_data("includedmodules")
        mmd = load_mmd(includedmodules_yml_path)
        # Set the name and stream
        mmd = mmd.copy("includedmodules", "1")
        scmurl = "git://pkgs.domain.local/modules/includedmodule?#da95886"
        ModuleBuild.create(
            db_session, conf, "includemodule", "1", 3, mmd_to_str(mmd), scmurl, "mprahl")
        self.fn(msg_id="msg-id-1", module_build_id=3, module_build_state="init")
        build = ModuleBuild.get_by_id(db_session, 3)
        assert build.state == 1
        assert build.name == "includemodule"
        batches = {}
        for comp_build in build.component_builds:
            batches[comp_build.package] = comp_build.batch
        assert batches["perl-List-Compare"] == 2
        assert batches["perl-Tangerine"] == 2
        assert batches["foo"] == 2
        assert batches["tangerine"] == 3
        assert batches["file"] == 4
        # Test that the RPMs are properly merged in xmd
        xmd_rpms = {
            "perl-List-Compare": {"ref": "4f26aeafdb"},
            "perl-Tangerine": {"ref": "4f26aeafdb"},
            "tangerine": {"ref": "4f26aeafdb"},
            "foo": {"ref": "93dea37599"},
            "file": {"ref": "a2740663f8"},
        }
        assert build.mmd().get_xmd()["mbs"]["rpms"] == xmd_rpms

    @patch("module_build_service.common.models.ModuleBuild.from_module_event")
    @patch("module_build_service.common.scm.SCM")
    @patch("module_build_service.scheduler.submit.get_build_arches", return_value=["x86_64"])
    def test_init_when_get_latest_raises(
            self, get_build_arches, mocked_scm, mocked_from_module_event):
        FakeSCM(
            mocked_scm,
            "testmodule",
            "testmodule.yaml",
            "7035bd33614972ac66559ac1fdd019ff6027ad22",
            get_latest_raise=True,
        )

        build = ModuleBuild.get_by_id(db_session, 2)
        mocked_from_module_event.return_value = build

        self.fn(msg_id="msg-id-1", module_build_id=2, module_build_state="init")

        # Query the database again to make sure the build object is updated
        db_session.refresh(build)
        # Make sure the module entered the failed state
        assert build.state == 4, build.state
        assert "Failed to get the latest commit for" in build.state_reason

    def test_do_not_handle_a_duplicate_late_init_message(self):
        build = db_session.query(ModuleBuild).filter(
            ModuleBuild.name == "testmodule").one()
        build.state = BUILD_STATES["wait"]
        db_session.commit()

        with patch.object(module_build_service.scheduler.handlers.modules, "log") as log:
            self.fn("msg-id-123", build.id, BUILD_STATES["init"])
            assert 2 == log.warning.call_count
