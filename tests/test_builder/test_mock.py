# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os
import tempfile
import shutil
from textwrap import dedent

import kobo.rpmlib
import koji
import mock
import pytest

from module_build_service.common.config import conf
from module_build_service.builder.MockModuleBuilder import (
    import_fake_base_module,
    import_builds_from_local_dnf_repos,
    load_local_builds,
    MockModuleBuilder,
)
from module_build_service.common import models
from module_build_service.common.models import ModuleBuild, ComponentBuild
from module_build_service.common.utils import load_mmd, mmd_to_str
from module_build_service.scheduler import events
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, make_module_in_db, read_staged_data, staged_data_filename


class TestMockModuleBuilder:
    def setup_method(self, test_method):
        clean_database()
        self.resultdir = tempfile.mkdtemp()

    def teardown_method(self, test_method):
        clean_database()
        shutil.rmtree(self.resultdir)

    def _create_module_with_filters(self, db_session, batch, state):
        mmd = load_mmd(read_staged_data("testmodule-with-filters"))
        # Set the name and stream
        mmd = mmd.copy("mbs-testmodule", "test")
        mmd.set_xmd({
            "mbs": {
                "rpms": {
                    "ed": {"ref": "01bf8330812fea798671925cc537f2f29b0bd216"},
                    "mksh": {"ref": "f70fd11ddf96bce0e2c64309706c29156b39141d"},
                },
                "buildrequires": {
                    "host": {
                        "version": "20171024133034",
                        "filtered_rpms": [],
                        "stream": "master",
                        "ref": "6df253bb3c53e84706c01b8ab2d5cac24f0b6d45",
                        "context": "00000000",
                    },
                    "platform": {
                        "version": "20171028112959",
                        "filtered_rpms": [],
                        "stream": "master",
                        "ref": "4f7787370a931d57421f9f9555fc41c3e31ff1fa",
                        "context": "00000000",
                    },
                },
                "scmurl": "file:///testdir",
                "commit": "5566bc792ec7a03bb0e28edd1b104a96ba342bd8",
                "requires": {
                    "platform": {
                        "version": "20171028112959",
                        "filtered_rpms": [],
                        "stream": "master",
                        "ref": "4f7787370a931d57421f9f9555fc41c3e31ff1fa",
                        "context": "00000000",
                    }
                },
            }
        })
        module = ModuleBuild.create(
            db_session,
            conf,
            name="mbs-testmodule",
            stream="test",
            version="20171027111452",
            modulemd=mmd_to_str(mmd),
            scmurl="file:///testdir",
            username="test",
        )
        module.koji_tag = "module-mbs-testmodule-test-20171027111452"
        module.batch = batch
        db_session.add(module)
        db_session.commit()

        comp_builds = [
            {
                "module_id": module.id,
                "state": state,
                "package": "ed",
                "format": "rpms",
                "scmurl": (
                    "https://src.fedoraproject.org/rpms/ed"
                    "?#01bf8330812fea798671925cc537f2f29b0bd216"
                ),
                "batch": 2,
                "ref": "01bf8330812fea798671925cc537f2f29b0bd216",
            },
            {
                "module_id": module.id,
                "state": state,
                "package": "mksh",
                "format": "rpms",
                "scmurl": (
                    "https://src.fedoraproject.org/rpms/mksh"
                    "?#f70fd11ddf96bce0e2c64309706c29156b39141d"
                ),
                "batch": 3,
                "ref": "f70fd11ddf96bce0e2c64309706c29156b39141d",
            },
        ]

        for build in comp_builds:
            db_session.add(ComponentBuild(**build))
        db_session.commit()

        return module

    @mock.patch("module_build_service.common.conf.system", new="mock")
    def test_createrepo_filter_last_batch(self):
        module = self._create_module_with_filters(db_session, 3, koji.BUILD_STATES["COMPLETE"])

        builder = MockModuleBuilder(
            db_session, "mcurlej", module, conf, module.koji_tag, module.component_builds
        )
        builder.resultsdir = self.resultdir
        rpms = [
            "ed-1.14.1-4.module+24957a32.x86_64.rpm",
            "mksh-56b-1.module+24957a32.x86_64.rpm",
            "module-build-macros-0.1-1.module+24957a32.noarch.rpm",
        ]
        rpm_qf_output = dedent("""\
            ed 0 1.14.1 4.module+24957a32 x86_64
            mksh 0 56b-1 module+24957a32 x86_64
            module-build-macros 0 0.1 1.module+24957a32 noarch
        """)
        with mock.patch("os.listdir", return_value=rpms):
            with mock.patch("subprocess.check_output", return_value=rpm_qf_output):
                builder._createrepo()

        with open(os.path.join(self.resultdir, "pkglist"), "r") as fd:
            pkglist = fd.read().strip()
            rpm_names = [kobo.rpmlib.parse_nvr(rpm)["name"] for rpm in pkglist.split("\n")]
            assert "ed" not in rpm_names

    @mock.patch("module_build_service.common.conf.system", new="mock")
    def test_createrepo_not_last_batch(self):
        module = self._create_module_with_filters(db_session, 2, koji.BUILD_STATES["COMPLETE"])

        builder = MockModuleBuilder(
            db_session, "mcurlej", module, conf, module.koji_tag, module.component_builds
        )
        builder.resultsdir = self.resultdir
        rpms = [
            "ed-1.14.1-4.module+24957a32.x86_64.rpm",
            "mksh-56b-1.module+24957a32.x86_64.rpm",
        ]
        rpm_qf_output = dedent("""\
            ed 0 1.14.1 4.module+24957a32 x86_64
            mksh 0 56b-1 module+24957a32 x86_64
        """)
        with mock.patch("os.listdir", return_value=rpms):
            with mock.patch("subprocess.check_output", return_value=rpm_qf_output):
                builder._createrepo()

        with open(os.path.join(self.resultdir, "pkglist"), "r") as fd:
            pkglist = fd.read().strip()
            rpm_names = [kobo.rpmlib.parse_nvr(rpm)["name"] for rpm in pkglist.split("\n")]
            assert "ed" in rpm_names

    @mock.patch("module_build_service.common.conf.system", new="mock")
    def test_createrepo_empty_rmp_list(self):
        module = self._create_module_with_filters(db_session, 3, koji.BUILD_STATES["COMPLETE"])

        builder = MockModuleBuilder(
            db_session, "mcurlej", module, conf, module.koji_tag, module.component_builds)
        builder.resultsdir = self.resultdir
        rpms = []
        with mock.patch("os.listdir", return_value=rpms):
            builder._createrepo()

        with open(os.path.join(self.resultdir, "pkglist"), "r") as fd:
            pkglist = fd.read().strip()
            assert not pkglist


class TestMockModuleBuilderAddRepos:
    def setup_method(self, test_method):
        clean_database(add_platform_module=False)

    @mock.patch("module_build_service.common.conf.system", new="mock")
    @mock.patch(
        "module_build_service.common.config.Config.base_module_repofiles",
        new_callable=mock.PropertyMock,
        return_value=["/etc/yum.repos.d/bar.repo", "/etc/yum.repos.d/bar-updates.repo"],
        create=True,
    )
    @mock.patch("module_build_service.builder.MockModuleBuilder.open", create=True)
    @mock.patch(
        "module_build_service.builder.MockModuleBuilder.MockModuleBuilder._load_mock_config"
    )
    @mock.patch(
        "module_build_service.builder.MockModuleBuilder.MockModuleBuilder._write_mock_config"
    )
    def test_buildroot_add_repos(
        self, write_config, load_config, patched_open, base_module_repofiles
    ):
        import_fake_base_module("platform:f29:1:000000")

        platform = ModuleBuild.get_last_build_in_stream(db_session, "platform", "f29")
        module_deps = [{
            "requires": {"platform": ["f29"]},
            "buildrequires": {"platform": ["f29"]},
        }]
        foo = make_module_in_db("foo:1:1:1", module_deps)
        app = make_module_in_db("app:1:1:1", module_deps)

        patched_open.side_effect = [
            mock.mock_open(read_data="[fake]\nrepofile 1\n").return_value,
            mock.mock_open(read_data="[fake]\nrepofile 2\n").return_value,
            mock.mock_open(read_data="[fake]\nrepofile 3\n").return_value,
        ]

        builder = MockModuleBuilder(db_session, "user", app, conf, "module-app", [])

        dependencies = {
            "repofile://": [platform.mmd()],
            "repofile:///etc/yum.repos.d/foo.repo": [foo.mmd(), app.mmd()],
        }

        builder.buildroot_add_repos(dependencies)

        assert "repofile 1" in builder.yum_conf
        assert "repofile 2" in builder.yum_conf
        assert "repofile 3" in builder.yum_conf

        assert set(builder.enabled_modules) == {"foo:1", "app:1"}


class TestOfflineLocalBuilds:
    def setup_method(self):
        clean_database()

    def teardown_method(self):
        clean_database()

    def test_import_fake_base_module(self):
        import_fake_base_module("platform:foo:1:000000")
        module_build = models.ModuleBuild.get_build_from_nsvc(
            db_session, "platform", "foo", 1, "000000")
        assert module_build

        mmd = module_build.mmd()
        xmd = mmd.get_xmd()
        assert xmd == {
            "mbs": {
                "buildrequires": {},
                "commit": "ref_000000",
                "koji_tag": "repofile://",
                "mse": "true",
                "requires": {},
            }
        }

        assert set(mmd.get_profile_names()) == {"buildroot", "srpm-buildroot"}

    @mock.patch(
        "module_build_service.builder.MockModuleBuilder.open",
        create=True,
        new_callable=mock.mock_open,
    )
    def test_import_builds_from_local_dnf_repos(self, patched_open):
        with mock.patch("dnf.Base") as dnf_base:
            repo = mock.MagicMock()
            repo.repofile = "/etc/yum.repos.d/foo.repo"
            mmd = load_mmd(read_staged_data("formatted_testmodule"))
            repo.get_metadata_content.return_value = mmd_to_str(mmd)
            base = dnf_base.return_value
            base.repos = {"reponame": repo}
            patched_open.return_value.readlines.return_value = ("FOO=bar", "PLATFORM_ID=platform:x")

            import_builds_from_local_dnf_repos()

            base.read_all_repos.assert_called_once()
            repo.load.assert_called_once()
            repo.get_metadata_content.assert_called_once_with("modules")

            module_build = models.ModuleBuild.get_build_from_nsvc(
                db_session, "testmodule", "master", 20180205135154, "9c690d0e")
            assert module_build
            assert module_build.koji_tag == "repofile:///etc/yum.repos.d/foo.repo"

            module_build = models.ModuleBuild.get_build_from_nsvc(
                db_session, "platform", "x", 1, "000000")
            assert module_build

    def test_import_builds_from_local_dnf_repos_platform_id(self):
        with mock.patch("dnf.Base"):
            import_builds_from_local_dnf_repos("platform:y")

            module_build = models.ModuleBuild.get_build_from_nsvc(
                db_session, "platform", "y", 1, "000000")
            assert module_build


@mock.patch(
    "module_build_service.common.config.Config.mock_resultsdir",
    new_callable=mock.PropertyMock,
    return_value=staged_data_filename("local_builds")
)
@mock.patch(
    "module_build_service.common.config.Config.system",
    new_callable=mock.PropertyMock,
    return_value="mock",
)
class TestLocalBuilds:
    def setup_method(self):
        clean_database()
        events.scheduler.reset()

    def teardown_method(self):
        clean_database()
        events.scheduler.reset()

    def test_load_local_builds_name(self, conf_system, conf_resultsdir):
        load_local_builds("testmodule")
        local_modules = models.ModuleBuild.local_modules(db_session)

        assert len(local_modules) == 1
        assert local_modules[0].koji_tag.endswith(
            "/module-testmodule-master-20170816080816/results")

    def test_load_local_builds_name_stream(self, conf_system, conf_resultsdir):
        load_local_builds("testmodule:master")
        local_modules = models.ModuleBuild.local_modules(db_session)

        assert len(local_modules) == 1
        assert local_modules[0].koji_tag.endswith(
            "/module-testmodule-master-20170816080816/results")

    def test_load_local_builds_name_stream_non_existing(
        self, conf_system, conf_resultsdir
    ):
        with pytest.raises(RuntimeError):
            load_local_builds("testmodule:x")
            models.ModuleBuild.local_modules(db_session)

    def test_load_local_builds_name_stream_version(self, conf_system, conf_resultsdir):
        load_local_builds("testmodule:master:20170816080815")
        local_modules = models.ModuleBuild.local_modules(db_session)

        assert len(local_modules) == 1
        assert local_modules[0].koji_tag.endswith(
            "/module-testmodule-master-20170816080815/results")

    def test_load_local_builds_name_stream_version_non_existing(
        self, conf_system, conf_resultsdir
    ):
        with pytest.raises(RuntimeError):
            load_local_builds("testmodule:master:123")
            models.ModuleBuild.local_modules(db_session)

    def test_load_local_builds_platform(self, conf_system, conf_resultsdir):
        load_local_builds("platform:f30")
        local_modules = models.ModuleBuild.local_modules(db_session)

        assert len(local_modules) == 1
        assert local_modules[0].koji_tag.endswith("/module-platform-f30-3/results")

    def test_load_local_builds_platform_f28(self, conf_system, conf_resultsdir):
        load_local_builds("platform:f30")
        local_modules = models.ModuleBuild.local_modules(db_session)

        assert len(local_modules) == 1
        assert local_modules[0].koji_tag.endswith("/module-platform-f30-3/results")
