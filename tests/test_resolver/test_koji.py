# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime

from mock import patch, MagicMock
import pytest

from module_build_service.common.config import conf
from module_build_service.common.models import ModuleBuild, BUILD_STATES
from module_build_service.common.utils import import_mmd, load_mmd, mmd_to_str
import module_build_service.resolver as mbs_resolver
from module_build_service.scheduler.db_session import db_session
import tests


@pytest.mark.usefixtures("reuse_component_init_data")
class TestLocalResolverModule:

    def _create_test_modules(self, koji_tag_with_modules="foo-test"):
        mmd = load_mmd(tests.read_staged_data("platform"))
        mmd = mmd.copy(mmd.get_module_name(), "f30.1.3")

        import_mmd(db_session, mmd)
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()

        if koji_tag_with_modules:
            platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
            platform_mmd = platform.mmd()
            platform_xmd = platform_mmd.get_xmd()
            platform_xmd["mbs"]["koji_tag_with_modules"] = koji_tag_with_modules
            platform_mmd.set_xmd(platform_xmd)
            platform.modulemd = mmd_to_str(platform_mmd)

        for context in ["7c29193d", "7c29193e"]:
            mmd = tests.make_module("testmodule:master:20170109091357:" + context)
            build = ModuleBuild(
                name="testmodule",
                stream="master",
                version=20170109091357,
                state=5,
                build_context="dd4de1c346dcf09ce77d38cd4e75094ec1c08ec3",
                runtime_context="ec4de1c346dcf09ce77d38cd4e75094ec1c08ef7",
                context=context,
                koji_tag="module-testmodule-master-20170109091357-" + context,
                scmurl="https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79",
                batch=3,
                owner="Dr. Pepper",
                time_submitted=datetime(2018, 11, 15, 16, 8, 18),
                time_modified=datetime(2018, 11, 15, 16, 19, 35),
                rebuild_strategy="changed-and-after",
                modulemd=mmd_to_str(mmd),
            )
            build.buildrequires.append(platform)
            db_session.add(build)
        db_session.commit()

    def test_get_buildrequired_modulemds_fallback_to_db_resolver(self):
        self._create_test_modules(koji_tag_with_modules=None)
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        result = resolver.get_buildrequired_modulemds("testmodule", "master", platform.mmd())

        nsvcs = {m.get_nsvc() for m in result}
        assert nsvcs == {
            "testmodule:master:20170109091357:7c29193d",
            "testmodule:master:20170109091357:7c29193e"}

    @patch("koji.ClientSession")
    def test_get_buildrequired_modulemds_name_not_tagged(self, ClientSession):
        koji_session = ClientSession.return_value
        koji_session.getLastEvent.return_value = {"id": 123}

        # No package with such name tagged.
        koji_session.listTagged.return_value = []
        koji_session.multiCall.return_value = [[]]

        self._create_test_modules()
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        result = resolver.get_buildrequired_modulemds("testmodule", "master", platform.mmd())

        assert result == []
        koji_session.listTagged.assert_called_with(
            "foo-test", inherit=True, package="testmodule", type="module", event=123)

    @patch("koji.ClientSession")
    def test_get_buildrequired_modulemds_multiple_streams(self, ClientSession):
        koji_session = ClientSession.return_value

        # We will ask for testmodule:master, but there is also testmodule:2 in a tag.
        koji_session.listTagged.return_value = [
            {
                "build_id": 123, "name": "testmodule", "version": "2",
                "release": "820181219174508.9edba152", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170109091357.7c29193d", "tag_name": "foo-test"
            }]

        koji_session.multiCall.return_value = [
            [build] for build in koji_session.listTagged.return_value]

        self._create_test_modules()
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        result = resolver.get_buildrequired_modulemds("testmodule", "master", platform.mmd())

        nsvcs = {m.get_nsvc() for m in result}
        assert nsvcs == {"testmodule:master:20170109091357:7c29193d"}

    @patch("koji.ClientSession")
    def test_get_buildrequired_modulemds_tagged_but_not_in_db(self, ClientSession):
        koji_session = ClientSession.return_value

        # We will ask for testmodule:2, but it is not in database, so it should raise
        # ValueError later.
        koji_session.listTagged.return_value = [
            {
                "build_id": 123, "name": "testmodule", "version": "2",
                "release": "820181219174508.9edba152", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170109091357.7c29193d", "tag_name": "foo-test"
            }]

        koji_session.multiCall.return_value = [
            [build] for build in koji_session.listTagged.return_value]

        self._create_test_modules()
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        expected_error = ("Module testmodule:2:820181219174508:9edba152 is tagged in the "
                          "foo-test Koji tag, but does not exist in MBS DB.")
        with pytest.raises(ValueError, match=expected_error):
            resolver.get_buildrequired_modulemds("testmodule", "2", platform.mmd())

    @patch("koji.ClientSession")
    def test_get_buildrequired_modulemds_multiple_versions_contexts(self, ClientSession):
        koji_session = ClientSession.return_value

        # We will ask for testmodule:2, but it is not in database, so it should raise
        # ValueError later.
        koji_session.listTagged.return_value = [
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20160110091357.7c29193d", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170109091357.7c29193d", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170109091357.7c29193e", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20160109091357.7c29193d", "tag_name": "foo-test"
            }]

        koji_session.multiCall.return_value = [
            [build] for build in koji_session.listTagged.return_value]

        self._create_test_modules()
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        result = resolver.get_buildrequired_modulemds("testmodule", "master", platform.mmd())

        nsvcs = {m.get_nsvc() for m in result}
        assert nsvcs == {
            "testmodule:master:20170109091357:7c29193d",
            "testmodule:master:20170109091357:7c29193e"}

    @patch("koji.ClientSession")
    def test_get_buildrequired_modules(self, ClientSession):
        koji_session = ClientSession.return_value

        # We will ask for testmodule:master, but there is also testmodule:2 in a tag.
        koji_session.listTagged.return_value = [
            {
                "build_id": 123, "name": "testmodule", "version": "2",
                "release": "820181219174508.9edba152", "tag_name": "foo-test"
            },
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170109091357.7c29193d", "tag_name": "foo-test"
            }]

        koji_session.multiCall.return_value = [
            [build] for build in koji_session.listTagged.return_value]

        self._create_test_modules()
        platform = db_session.query(ModuleBuild).filter_by(stream="f30.1.3").one()
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        result = resolver.get_buildrequired_modules("testmodule", "master", platform.mmd())

        nvrs = {m.nvr_string for m in result}
        assert nvrs == {"testmodule-master-20170109091357.7c29193d"}

    @patch("koji.ClientSession")
    def test_filter_inherited(self, ClientSession):
        koji_session = ClientSession.return_value

        koji_session.getFullInheritance.return_value = [
            {"name": "foo-test"},
            {"name": "foo-test-parent"},
        ]

        builds = [
            {
                "build_id": 124, "name": "testmodule", "version": "master",
                "release": "20170110091357.7c29193d", "tag_name": "foo-test"
            },
            {
                "build_id": 125, "name": "testmodule", "version": "master",
                "release": "20180109091357.7c29193d", "tag_name": "foo-test-parent"
            },
            {
                "build_id": 126, "name": "testmodule", "version": "2",
                "release": "20180109091357.7c29193d", "tag_name": "foo-test-parent"
            }]

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        new_builds = resolver._filter_inherited(koji_session, builds, "foo-test", {"id": 123})

        nvrs = {"{name}-{version}-{release}".format(**b) for b in new_builds}
        assert nvrs == {
            "testmodule-master-20170110091357.7c29193d",
            "testmodule-2-20180109091357.7c29193d"}

    @patch("module_build_service.resolver.KojiResolver.koji_multicall_map")
    def test_filter_based_on_real_stream_name(self, koji_multicall_map):
        koji_session = MagicMock()
        koji_multicall_map.return_value = [
            {"build_id": 124, "extra": {"typeinfo": {"module": {"stream": "foo-test"}}}},
            {"build_id": 125, "extra": {"typeinfo": {"module": {"stream": "foo_test"}}}},
            {"build_id": 126, "extra": {"typeinfo": {"module": {"stream": "foo-test"}}}},
            {"build_id": 127, "extra": {"typeinfo": {"module": {}}}},
        ]

        builds = [
            {"build_id": 124, "name": "testmodule", "version": "foo_test"},
            {"build_id": 125, "name": "testmodule", "version": "foo_test"},
            {"build_id": 126, "name": "testmodule", "version": "foo_test"},
            {"build_id": 127, "name": "testmodule", "version": "foo_test"},
        ]

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        new_builds = resolver._filter_based_on_real_stream_name(koji_session, builds, "foo-test")

        build_ids = {b["build_id"] for b in new_builds}
        assert build_ids == {124, 126, 127}

    @patch("module_build_service.builder.KojiModuleBuilder.koji_multicall_map")
    def test_filter_based_on_real_stream_name_multicall_error(self, koji_multicall_map):
        koji_session = MagicMock()
        koji_multicall_map.return_value = None

        builds = [
            {"build_id": 124, "name": "testmodule", "version": "foo_test"},
        ]

        expected_error = "Error during Koji multicall when filtering KojiResolver builds."
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        with pytest.raises(RuntimeError, match=expected_error):
            resolver._filter_based_on_real_stream_name(koji_session, builds, "foo-test")

    def test_get_compatible_base_module_modulemds_fallback_to_dbresolver(self):
        tests.init_data(1, multiple_stream_versions=True)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")
        platform = db_session.query(ModuleBuild).filter_by(name="platform", stream="f29.1.0").one()
        platform_mmd = platform.mmd()
        result = resolver.get_compatible_base_module_modulemds(
            platform_mmd, stream_version_lte=True, virtual_streams=["f29"],
            states=[BUILD_STATES["ready"]])

        assert len(result) == 2

    def test_get_compatible_base_module_modulemds(self):
        tests.init_data(1, multiple_stream_versions=True)
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="koji")

        platform = db_session.query(ModuleBuild).filter_by(name="platform", stream="f29.1.0").one()
        platform_mmd = platform.mmd()
        platform_xmd = platform_mmd.get_xmd()
        platform_xmd["mbs"]["koji_tag_with_modules"] = "module-f29-build"
        platform_mmd.set_xmd(platform_xmd)
        platform.modulemd = mmd_to_str(platform_mmd)
        db_session.commit()

        result = resolver.get_compatible_base_module_modulemds(
            platform_mmd, stream_version_lte=True, virtual_streams=["f29"],
            states=[BUILD_STATES["ready"]])

        assert len(result) == 0

    def test_supported_builders(self):
        ret = mbs_resolver.KojiResolver.KojiResolver.supported_builders()
        assert set(ret) == {"koji", "test", "testlocal"}
