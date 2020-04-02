# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os

from datetime import datetime
from mock import patch, PropertyMock
import pytest

from module_build_service.builder.MockModuleBuilder import load_local_builds
from module_build_service.common import models
from module_build_service.common.config import conf
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.models import ModuleBuild
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import import_mmd, load_mmd, mmd_to_str
import module_build_service.resolver as mbs_resolver
from module_build_service.scheduler.db_session import db_session
import tests


@pytest.mark.usefixtures("reuse_component_init_data")
class TestDBModule:

    def test_get_buildrequired_modulemds(self):
        mmd = load_mmd(tests.read_staged_data("platform"))
        mmd = mmd.copy(mmd.get_module_name(), "f30.1.3")

        import_mmd(db_session, mmd)
        platform_f300103 = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        mmd = tests.make_module("testmodule:master:20170109091357:123")
        build = ModuleBuild(
            name="testmodule",
            stream="master",
            version=20170109091357,
            state=5,
            build_context="dd4de1c346dcf09ce77d38cd4e75094ec1c08ec3",
            runtime_context="ec4de1c346dcf09ce77d38cd4e75094ec1c08ef7",
            context="7c29193d",
            koji_tag="module-testmodule-master-20170109091357-7c29193d",
            scmurl="https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79",
            batch=3,
            owner="Dr. Pepper",
            time_submitted=datetime(2018, 11, 15, 16, 8, 18),
            time_modified=datetime(2018, 11, 15, 16, 19, 35),
            rebuild_strategy="changed-and-after",
            modulemd=mmd_to_str(mmd),
        )
        build.buildrequires.append(platform_f300103)
        db_session.add(build)
        db_session.commit()

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.get_buildrequired_modulemds(
            "testmodule", "master", platform_f300103.mmd())
        nsvcs = {m.get_nsvc() for m in result}
        assert nsvcs == {"testmodule:master:20170109091357:123"}

    @pytest.mark.parametrize("stream_versions", [False, True])
    def test_get_compatible_base_module_modulemds_stream_versions(self, stream_versions):
        tests.init_data(1, multiple_stream_versions=True)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        platform = db_session.query(ModuleBuild).filter_by(name="platform", stream="f29.1.0").one()
        platform_mmd = platform.mmd()
        result = resolver.get_compatible_base_module_modulemds(
            platform_mmd, stream_version_lte=stream_versions, virtual_streams=["f29"],
            states=[models.BUILD_STATES["ready"]])
        nsvcs = {mmd.get_nsvc() for mmd in result}
        if stream_versions:
            assert nsvcs == {"platform:f29.1.0:3:00000000", "platform:f29.0.0:3:00000000"}
        else:
            assert nsvcs == {
                "platform:f29.1.0:3:00000000",
                "platform:f29.0.0:3:00000000",
                "platform:f29.2.0:3:00000000"
            }

    @pytest.mark.parametrize("empty_buildrequires", [False, True])
    def test_get_module_build_dependencies(self, empty_buildrequires):
        """
        Tests that the buildrequires of testmodule are returned
        """
        expected = {"module-f28-build"}
        module = models.ModuleBuild.get_by_id(db_session, 2)
        if empty_buildrequires:
            expected = set()
            module = models.ModuleBuild.get_by_id(db_session, 2)
            mmd = module.mmd()
            # Wipe out the dependencies
            mmd.clear_dependencies()
            xmd = mmd.get_xmd()
            xmd["mbs"]["buildrequires"] = {}
            mmd.set_xmd(xmd)
            module.modulemd = mmd_to_str(mmd)
            db_session.commit()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.get_module_build_dependencies(
            "testmodule", "master", "20170109091357", "78e4a6fd").keys()
        assert set(result) == expected

    def test_get_module_build_dependencies_recursive(self):
        """
        Tests that the buildrequires are returned when it is two layers deep
        """
        # Add testmodule2 that requires testmodule
        module = models.ModuleBuild.get_by_id(db_session, 3)
        mmd = module.mmd()
        # Rename the module
        mmd = mmd.copy("testmodule2")
        mmd.set_version(20180123171545)
        deps = Modulemd.Dependencies()
        deps.add_runtime_stream("testmodule", "master")
        mmd.add_dependencies(deps)
        xmd = mmd.get_xmd()
        xmd["mbs"]["requires"]["testmodule"] = {
            "filtered_rpms": [],
            "ref": "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            "stream": "master",
            "version": "20180205135154",
        }
        mmd.set_xmd(xmd)
        module.modulemd = mmd_to_str(mmd)
        module.name = "testmodule2"
        module.version = str(mmd.get_version())
        module.koji_tag = "module-ae2adf69caf0e1b6"

        db_session.commit()

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.get_module_build_dependencies(
            "testmodule2", "master", "20180123171545", "c40c156c").keys()
        assert set(result) == {"module-f28-build"}

    @patch(
        "module_build_service.common.config.Config.system",
        new_callable=PropertyMock,
        return_value="test",
    )
    @patch(
        "module_build_service.common.config.Config.mock_resultsdir",
        new_callable=PropertyMock,
        return_value=tests.staged_data_filename("local_builds"),
    )
    def test_get_module_build_dependencies_recursive_requires(self, resultdir, conf_system):
        """
        Tests that it returns the requires of the buildrequires recursively
        """
        load_local_builds(["platform:f30", "parent", "child", "testmodule"])

        build = models.ModuleBuild.local_modules(db_session, "child", "master")
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.get_module_build_dependencies(mmd=build[0].mmd()).keys()

        local_path = tests.staged_data_filename("local_builds")

        expected = [os.path.join(local_path, "module-parent-master-20170816080815/results")]
        assert set(result) == set(expected)

    def test_resolve_requires(self):
        build = models.ModuleBuild.get_by_id(db_session, 2)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.resolve_requires(
            [":".join([build.name, build.stream, build.version, build.context])]
        )

        assert result == {
            "testmodule": {
                "stream": "master",
                "version": "20170109091357",
                "context": u"78e4a6fd",
                "ref": "ff1ea79fc952143efeed1851aa0aa006559239ba",
                "koji_tag": "module-testmodule-master-20170109091357-78e4a6fd",
            }
        }

    def test_resolve_requires_exception(self):
        build = models.ModuleBuild.get_by_id(db_session, 2)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        with pytest.raises(UnprocessableEntity):
            resolver.resolve_requires(
                [":".join(["abcdefghi", build.stream, build.version, build.context])]
            )

    def test_resolve_requires_siblings(self):
        tests.clean_database()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        mmd = load_mmd(tests.read_staged_data("formatted_testmodule"))
        for i in range(3):
            build = tests.module_build_from_modulemd(mmd_to_str(mmd))
            build.context = "f6e2ae" + str(i)
            build.build_context = "f6e2aeec7576196241b9afa0b6b22acf2b6873d" + str(i)
            build.runtime_context = "bbc84c7b817ab3dd54916c0bcd6c6bdf512f7f9c" + str(i)
            build.state = models.BUILD_STATES["ready"]
            db_session.add(build)
        db_session.commit()

        build_one = ModuleBuild.get_by_id(db_session, 2)
        nsvc = ":".join([build_one.name, build_one.stream, build_one.version, build_one.context])
        result = resolver.resolve_requires([nsvc])
        assert result == {
            "testmodule": {
                "stream": build_one.stream,
                "version": build_one.version,
                "context": build_one.context,
                "ref": "65a7721ee4eff44d2a63fb8f3a8da6e944ab7f4d",
                "koji_tag": None
            }
        }

        db_session.commit()

    def test_resolve_profiles(self):
        """
        Tests that the profiles get resolved recursively
        """
        mmd = models.ModuleBuild.get_by_id(db_session, 2).mmd()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        result = resolver.resolve_profiles(mmd, ("buildroot", "srpm-buildroot"))
        expected = {
            "buildroot": {
                "unzip",
                "tar",
                "cpio",
                "gawk",
                "gcc",
                "xz",
                "sed",
                "findutils",
                "util-linux",
                "bash",
                "info",
                "bzip2",
                "grep",
                "redhat-rpm-config",
                "fedora-release",
                "diffutils",
                "make",
                "patch",
                "shadow-utils",
                "coreutils",
                "which",
                "rpm-build",
                "gzip",
                "gcc-c++",
            },
            "srpm-buildroot": {
                "shadow-utils",
                "redhat-rpm-config",
                "rpm-build",
                "fedora-release",
                "fedpkg-minimal",
                "gnupg2",
                "bash",
            },
        }
        assert result == expected

    @patch(
        "module_build_service.common.config.Config.system",
        new_callable=PropertyMock,
        return_value="test",
    )
    @patch(
        "module_build_service.common.config.Config.mock_resultsdir",
        new_callable=PropertyMock,
        return_value=tests.staged_data_filename("local_builds")
    )
    def test_resolve_profiles_local_module(self, local_builds, conf_system):
        """
        Test that profiles get resolved recursively on local builds
        """
        # This test requires a platform module loaded from local rather than
        # the one added to database.
        platform = db_session.query(models.ModuleBuild).filter(
            models.ModuleBuild.name == "platform"
        ).one()
        db_session.delete(platform)
        db_session.commit()

        load_local_builds(["platform:f28"])
        mmd = models.ModuleBuild.get_by_id(db_session, 2).mmd()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        result = resolver.resolve_profiles(mmd, ("buildroot", "srpm-buildroot"))
        expected = {"buildroot": {"foo"}, "srpm-buildroot": {"bar"}}
        assert result == expected

    def test_get_latest_with_virtual_stream(self):
        tests.init_data(1, multiple_stream_versions=True)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        mmd = resolver.get_latest_with_virtual_stream("platform", "f29")
        assert mmd
        assert mmd.get_stream_name() == "f29.2.0"

    def test_get_latest_with_virtual_stream_none(self):
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        mmd = resolver.get_latest_with_virtual_stream("platform", "doesnotexist")
        assert not mmd

    def test_get_module_count(self):
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="db")
        count = resolver.get_module_count(name="platform", stream="f28")
        assert count == 1
