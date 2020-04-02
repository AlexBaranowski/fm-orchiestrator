# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime

import mock
import pytest

from module_build_service import app
from module_build_service.common import conf, models
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import load_mmd, load_mmd_file, mmd_to_str
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.handlers.components
from module_build_service.scheduler.submit import (
    get_build_arches, format_mmd, record_component_builds, record_module_build_arches
)
from tests import (
    clean_database,
    init_data,
    read_staged_data,
    staged_data_filename,
    scheduler_init_data,
)


class TestSubmit:
    def setup_method(self, test_method):
        clean_database()

    def teardown_method(self, test_method):
        clean_database()

    @mock.patch("koji.ClientSession")
    def test_get_build_arches(self, ClientSession):
        session = ClientSession.return_value
        session.getTag.return_value = {"arches": "ppc64le"}
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        r = get_build_arches(mmd, conf)
        assert r == ["ppc64le"]

    @mock.patch("koji.ClientSession")
    def test_get_build_arches_no_arch_set(self, ClientSession):
        """
        When no architecture is set in Koji tag, fallback to conf.arches.
        """
        session = ClientSession.return_value
        session.getTag.return_value = {"arches": ""}
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        r = get_build_arches(mmd, conf)
        assert set(r) == set(conf.arches)

    @mock.patch(
        "module_build_service.common.config.Config.allowed_privileged_module_names",
        new_callable=mock.PropertyMock,
        return_value=["testmodule"],
    )
    def test_get_build_arches_koji_tag_arches(self, cfg):
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        xmd = mmd.get_xmd()
        xmd["mbs"]["koji_tag_arches"] = ["ppc64", "ppc64le"]
        mmd.set_xmd(xmd)

        r = get_build_arches(mmd, conf)
        assert r == ["ppc64", "ppc64le"]

    @mock.patch.object(conf, "base_module_arches", new={"platform:xx": ["x86_64", "i686"]})
    def test_get_build_arches_base_module_override(self):
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        xmd = mmd.get_xmd()
        mbs_options = xmd["mbs"] if "mbs" in xmd.keys() else {}
        mbs_options["buildrequires"] = {"platform": {"stream": "xx"}}
        xmd["mbs"] = mbs_options
        mmd.set_xmd(xmd)

        r = get_build_arches(mmd, conf)
        assert r == ["x86_64", "i686"]

    @mock.patch.object(conf, "base_module_arches", new={"platform:xx": ["x86_64", "i686"]})
    def test_get_build_arches_set_in_mmd(self):
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        xmd = mmd.get_xmd()
        mbs_options = xmd.get("mbs", {})
        mbs_options["buildrequires"] = {"platform": {"stream": "xx"}}
        xmd["mbs"] = mbs_options
        mmd.set_xmd(xmd)
        try:
            opts = Modulemd.Buildopts()
            opts.add_arch("x86_64")
            mmd.set_buildopts(opts)
            expected_result = ["x86_64"]
        except AttributeError:
            # libmodulemd version < 2.8.3
            expected_result = ["x86_64", "i686"]

        r = get_build_arches(mmd, conf)
        assert r == expected_result

    @mock.patch("module_build_service.scheduler.submit.get_build_arches")
    def test_record_module_build_arches(self, get_build_arches):
        get_build_arches.return_value = ["x86_64", "i686"]
        scheduler_init_data(1)
        build = models.ModuleBuild.get_by_id(db_session, 2)
        build.arches = []
        record_module_build_arches(build.mmd(), build)

        arches = {arch.name for arch in build.arches}
        assert arches == set(get_build_arches.return_value)

        # Ensure the function is idempotent
        record_module_build_arches(build.mmd(), build)
        assert len(build.arches) == len(get_build_arches.return_value)

    @pytest.mark.parametrize(
        "scmurl",
        [
            (
                "https://src.stg.fedoraproject.org/modules/testmodule.git"
                "?#620ec77321b2ea7b0d67d82992dda3e1d67055b4"
            ),
            None,
        ],
    )
    @pytest.mark.parametrize(
        "srpm_overrides",
        [
            {"perl-List-Compare": "/path/to/perl-List-Compare.src.rpm"},
            None,
        ],
    )
    @mock.patch("module_build_service.common.scm.SCM")
    def test_format_mmd(self, mocked_scm, srpm_overrides, scmurl):
        mocked_scm.return_value.commit = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        # For all the RPMs in testmodule, get_latest is called
        hashes_returned = {
            "master": "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
            "f28": "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            "f27": "5deef23acd2367d8b8d5a621a2fc568b695bc3bd",
        }

        def mocked_get_latest(ref="master"):
            if ref in hashes_returned:
                return hashes_returned[ref]
            raise RuntimeError("ref %s not found." % ref)

        mocked_scm.return_value.get_latest = mocked_get_latest
        mmd = load_mmd(read_staged_data("testmodule"))
        # Modify the component branches so we can identify them later on
        mmd.get_rpm_component("perl-Tangerine").set_ref("f28")
        mmd.get_rpm_component("tangerine").set_ref("f27")
        if srpm_overrides:
            # Set a bogus ref that will raise an exception if not properly ignored.
            mmd.get_rpm_component("perl-List-Compare").set_ref("bogus")
        format_mmd(mmd, scmurl, srpm_overrides=srpm_overrides)

        # Make sure that original refs are not changed.
        mmd_pkg_refs = [
            mmd.get_rpm_component(pkg_name).get_ref()
            for pkg_name in mmd.get_rpm_component_names()
        ]
        if srpm_overrides:
            assert set(mmd_pkg_refs) == {'f27', 'f28', 'bogus'}
        else:
            assert set(mmd_pkg_refs) == {'f27', 'f28', 'master'}
        deps = mmd.get_dependencies()[0]
        assert deps.get_buildtime_modules() == ["platform"]
        assert deps.get_buildtime_streams("platform") == ["f28"]
        match_anything = type('eq_any', (), {"__eq__": lambda left, right: True})()
        xmd = {
            "mbs": {
                "commit": "",
                "rpms": {
                    "perl-List-Compare": {"ref": "fbed359411a1baa08d4a88e0d12d426fbf8f602c"},
                    "perl-Tangerine": {"ref": "4ceea43add2366d8b8c5a622a2fb563b625b9abf"},
                    "tangerine": {"ref": "5deef23acd2367d8b8d5a621a2fc568b695bc3bd"},
                },
                "scmurl": "",
            }
        }
        if scmurl:
            xmd["mbs"]["commit"] = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
            xmd["mbs"]["scmurl"] = scmurl
        if srpm_overrides:
            xmd["mbs"]["rpms"]["perl-List-Compare"]["ref"] = match_anything
        mmd_xmd = mmd.get_xmd()
        assert mmd_xmd == xmd

    @mock.patch("module_build_service.common.scm.SCM")
    def test_record_component_builds_duplicate_components(self, mocked_scm):
        # Mock for format_mmd to get components' latest ref
        mocked_scm.return_value.commit = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        mocked_scm.return_value.get_latest.side_effect = [
            "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
        ]

        mmd = load_mmd(read_staged_data("testmodule"))
        mmd = mmd.copy("testmodule-variant", "master")
        module_build = module_build_service.common.models.ModuleBuild()
        module_build.name = "testmodule-variant"
        module_build.stream = "master"
        module_build.version = 20170109091357
        module_build.state = models.BUILD_STATES["init"]
        module_build.scmurl = \
            "https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79"
        module_build.batch = 1
        module_build.owner = "Tom Brady"
        module_build.time_submitted = datetime(2017, 2, 15, 16, 8, 18)
        module_build.time_modified = datetime(2017, 2, 15, 16, 19, 35)
        module_build.rebuild_strategy = "changed-and-after"
        module_build.modulemd = mmd_to_str(mmd)
        db_session.add(module_build)
        db_session.commit()
        # Rename the the modulemd to include
        mmd = mmd.copy("testmodule")
        # Remove perl-Tangerine and tangerine from the modulemd to include so only one
        # component conflicts
        mmd.remove_rpm_component("perl-Tangerine")
        mmd.remove_rpm_component("tangerine")

        error_msg = (
            'The included module "testmodule" in "testmodule-variant" have '
            "the following conflicting components: perl-List-Compare"
        )
        format_mmd(mmd, module_build.scmurl)
        with pytest.raises(UnprocessableEntity) as e:
            record_component_builds(mmd, module_build, main_mmd=module_build.mmd())

        assert str(e.value) == error_msg

    @mock.patch("module_build_service.common.scm.SCM")
    def test_record_component_builds_set_weight(self, mocked_scm):
        # Mock for format_mmd to get components' latest ref
        mocked_scm.return_value.commit = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        mocked_scm.return_value.get_latest.side_effect = [
            "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
            "dbed259411a1baa08d4a88e0d12d426fbf8f6037",
        ]

        mmd = load_mmd(read_staged_data("testmodule"))
        # Set the module name and stream
        mmd = mmd.copy("testmodule", "master")

        module_build = module_build_service.common.models.ModuleBuild()
        module_build.name = "testmodule"
        module_build.stream = "master"
        module_build.version = 20170109091357
        module_build.state = models.BUILD_STATES["init"]
        module_build.scmurl = \
            "https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79"
        module_build.batch = 1
        module_build.owner = "Tom Brady"
        module_build.time_submitted = datetime(2017, 2, 15, 16, 8, 18)
        module_build.time_modified = datetime(2017, 2, 15, 16, 19, 35)
        module_build.rebuild_strategy = "changed-and-after"
        module_build.modulemd = mmd_to_str(mmd)

        db_session.add(module_build)
        db_session.commit()

        format_mmd(mmd, module_build.scmurl)
        record_component_builds(mmd, module_build)
        db_session.commit()

        assert module_build.state == models.BUILD_STATES["init"]
        db_session.refresh(module_build)
        for c in module_build.component_builds:
            assert c.weight == 1.5

    @mock.patch("module_build_service.common.scm.SCM")
    def test_record_component_builds_component_exists_already(self, mocked_scm):
        mocked_scm.return_value.commit = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        mocked_scm.return_value.get_latest.side_effect = [
            "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
            "dbed259411a1baa08d4a88e0d12d426fbf8f6037",

            "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            # To simulate that when a module is resubmitted, some ref of
            # its components is changed, which will cause MBS stops
            # recording component to database and raise an error.
            "abcdefg",
            "dbed259411a1baa08d4a88e0d12d426fbf8f6037",
        ]

        original_mmd = load_mmd(read_staged_data("testmodule"))

        # Set the module name and stream
        mmd = original_mmd.copy("testmodule", "master")
        module_build = module_build_service.common.models.ModuleBuild()
        module_build.name = "testmodule"
        module_build.stream = "master"
        module_build.version = 20170109091357
        module_build.state = models.BUILD_STATES["init"]
        module_build.scmurl = \
            "https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79"
        module_build.batch = 1
        module_build.owner = "Tom Brady"
        module_build.time_submitted = datetime(2017, 2, 15, 16, 8, 18)
        module_build.time_modified = datetime(2017, 2, 15, 16, 19, 35)
        module_build.rebuild_strategy = "changed-and-after"
        module_build.modulemd = mmd_to_str(mmd)
        db_session.add(module_build)
        db_session.commit()

        format_mmd(mmd, module_build.scmurl)
        record_component_builds(mmd, module_build)
        db_session.commit()

        mmd = original_mmd.copy("testmodule", "master")

        from module_build_service.common.errors import ValidationError
        with pytest.raises(
                ValidationError,
                match=r"Component build .+ of module build .+ already exists in database"):
            format_mmd(mmd, module_build.scmurl)
            record_component_builds(mmd, module_build)

    @mock.patch("module_build_service.common.scm.SCM")
    def test_format_mmd_arches(self, mocked_scm):
        with app.app_context():
            clean_database()
            mocked_scm.return_value.commit = "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
            mocked_scm.return_value.get_latest.side_effect = [
                "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
                "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
                "dbed259411a1baa08d4a88e0d12d426fbf8f6037",
                "4ceea43add2366d8b8c5a622a2fb563b625b9abf",
                "fbed359411a1baa08d4a88e0d12d426fbf8f602c",
                "dbed259411a1baa08d4a88e0d12d426fbf8f6037",
            ]

            testmodule_mmd_path = staged_data_filename("testmodule.yaml")
            test_archs = ["powerpc", "i486"]

            mmd1 = load_mmd_file(testmodule_mmd_path)
            format_mmd(mmd1, None)

            for pkg_name in mmd1.get_rpm_component_names():
                pkg = mmd1.get_rpm_component(pkg_name)
                assert set(pkg.get_arches()) == set(conf.arches)

            mmd2 = load_mmd_file(testmodule_mmd_path)

            for pkg_name in mmd2.get_rpm_component_names():
                pkg = mmd2.get_rpm_component(pkg_name)
                pkg.reset_arches()
                for arch in test_archs:
                    pkg.add_restricted_arch(arch)

            format_mmd(mmd2, None)

            for pkg_name in mmd2.get_rpm_component_names():
                pkg = mmd2.get_rpm_component(pkg_name)
                assert set(pkg.get_arches()) == set(test_archs)

    @mock.patch("module_build_service.common.scm.SCM")
    @mock.patch("module_build_service.scheduler.submit.ThreadPool")
    def test_format_mmd_update_time_modified(self, tp, mocked_scm):
        init_data()
        build = models.ModuleBuild.get_by_id(db_session, 2)

        async_result = mock.MagicMock()
        async_result.ready.side_effect = [False, False, False, True]
        tp.return_value.map_async.return_value = async_result

        test_datetime = datetime(2019, 2, 14, 11, 11, 45, 42968)

        mmd = load_mmd(read_staged_data("testmodule"))

        with mock.patch("module_build_service.scheduler.submit.datetime") as dt:
            dt.utcnow.return_value = test_datetime
            format_mmd(mmd, None, build, db_session)

        assert build.time_modified == test_datetime
