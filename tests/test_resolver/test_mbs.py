# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from mock import patch, PropertyMock, Mock, call

from module_build_service import app
from module_build_service.builder.MockModuleBuilder import load_local_builds
import module_build_service.common.models
from module_build_service.common import conf
from module_build_service.common.utils import load_mmd, mmd_to_str
import module_build_service.resolver as mbs_resolver
from module_build_service.scheduler.db_session import db_session
import tests


class TestMBSModule:
    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_module_modulemds_nsvc(self, mock_session, testmodule_mmd_9c690d0e):
        """ Tests for querying a module from mbs """
        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.return_value = {
            "items": [
                {
                    "name": "testmodule",
                    "stream": "master",
                    "version": "20180205135154",
                    "context": "9c690d0e",
                    "modulemd": testmodule_mmd_9c690d0e,
                }
            ],
            "meta": {"next": None},
        }

        mock_session.get.return_value = mock_res

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        module_mmds = resolver.get_module_modulemds(
            "testmodule", "master", "20180205135154", "9c690d0e", virtual_streams=["f28"]
        )
        nsvcs = set(m.get_nsvc() for m in module_mmds)
        expected = {"testmodule:master:20180205135154:9c690d0e"}
        mbs_url = conf.mbs_url
        expected_query = {
            "name": "testmodule",
            "stream": "master",
            "version": "20180205135154",
            "context": "9c690d0e",
            "verbose": True,
            "order_desc_by": "version",
            "page": 1,
            "per_page": 10,
            "state": ["ready"],
            "virtual_stream": ["f28"],
        }
        mock_session.get.assert_called_once_with(mbs_url, params=expected_query)
        assert nsvcs == expected

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_module_modulemds_partial(
        self, mock_session, testmodule_mmd_9c690d0e, testmodule_mmd_c2c572ed
    ):
        """ Test for querying MBS without the context of a module """

        version = "20180205135154"

        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.return_value = {
            "items": [
                {
                    "name": "testmodule",
                    "stream": "master",
                    "version": version,
                    "context": "9c690d0e",
                    "modulemd": testmodule_mmd_9c690d0e,
                },
                {
                    "name": "testmodule",
                    "stream": "master",
                    "version": version,
                    "context": "c2c572ed",
                    "modulemd": testmodule_mmd_c2c572ed,
                },
            ],
            "meta": {"next": None},
        }

        mock_session.get.return_value = mock_res
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        ret = resolver.get_module_modulemds("testmodule", "master", version)
        nsvcs = set(m.get_nsvc() for m in ret)
        expected = {
            "testmodule:master:20180205135154:9c690d0e",
            "testmodule:master:20180205135154:c2c572ed",
        }
        mbs_url = conf.mbs_url
        expected_query = {
            "name": "testmodule",
            "stream": "master",
            "version": version,
            "verbose": True,
            "order_desc_by": "version",
            "page": 1,
            "per_page": 10,
            "state": ["ready"],
        }
        mock_session.get.assert_called_once_with(mbs_url, params=expected_query)
        assert nsvcs == expected

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_module_build_dependencies(
        self, mock_session, platform_mmd, testmodule_mmd_9c690d0e
    ):
        """
        Tests that we return just direct build-time dependencies of testmodule.
        """
        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.side_effect = [
            {
                "items": [
                    {
                        "name": "testmodule",
                        "stream": "master",
                        "version": "20180205135154",
                        "context": "9c690d0e",
                        "modulemd": testmodule_mmd_9c690d0e,
                    }
                ],
                "meta": {"next": None},
            },
            {
                "items": [
                    {
                        "name": "platform",
                        "stream": "f28",
                        "version": "3",
                        "context": "00000000",
                        "modulemd": platform_mmd,
                        "koji_tag": "module-f28-build",
                    }
                ],
                "meta": {"next": None},
            },
        ]

        mock_session.get.return_value = mock_res
        expected = {"module-f28-build"}
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        result = resolver.get_module_build_dependencies(
            "testmodule", "master", "20180205135154", "9c690d0e").keys()

        expected_queries = [
            {
                "name": "testmodule",
                "stream": "master",
                "version": "20180205135154",
                "context": "9c690d0e",
                "verbose": True,
                "order_desc_by": "version",
                "page": 1,
                "per_page": 10,
                "state": ["ready"],
            },
            {
                "name": "platform",
                "stream": "f28",
                "version": "3",
                "context": "00000000",
                "verbose": True,
                "order_desc_by": "version",
                "page": 1,
                "per_page": 10,
                "state": ["ready"],
            },
        ]

        mbs_url = conf.mbs_url
        expected_calls = [
            call(mbs_url, params=expected_queries[0]),
            call(mbs_url, params=expected_queries[1]),
        ]
        mock_session.get.mock_calls = expected_calls
        assert mock_session.get.call_count == 2
        assert set(result) == expected

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_module_build_dependencies_empty_buildrequires(
        self, mock_session, testmodule_mmd_9c690d0e
    ):

        mmd = load_mmd(testmodule_mmd_9c690d0e)
        # Wipe out the dependencies
        for deps in mmd.get_dependencies():
            mmd.remove_dependencies(deps)
        xmd = mmd.get_xmd()
        xmd["mbs"]["buildrequires"] = {}
        mmd.set_xmd(xmd)

        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.side_effect = [
            {
                "items": [
                    {
                        "name": "testmodule",
                        "stream": "master",
                        "version": "20180205135154",
                        "context": "9c690d0e",
                        "modulemd": mmd_to_str(mmd),
                        "build_deps": [],
                    }
                ],
                "meta": {"next": None},
            }
        ]

        mock_session.get.return_value = mock_res

        expected = set()

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        result = resolver.get_module_build_dependencies(
            "testmodule", "master", "20180205135154", "9c690d0e"
        ).keys()
        mbs_url = conf.mbs_url
        expected_query = {
            "name": "testmodule",
            "stream": "master",
            "version": "20180205135154",
            "context": "9c690d0e",
            "verbose": True,
            "order_desc_by": "version",
            "page": 1,
            "per_page": 10,
            "state": ["ready"],
        }
        mock_session.get.assert_called_once_with(mbs_url, params=expected_query)
        assert set(result) == expected

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_resolve_profiles(
        self, mock_session, formatted_testmodule_mmd, platform_mmd
    ):

        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.return_value = {
            "items": [
                {
                    "name": "platform",
                    "stream": "f28",
                    "version": "3",
                    "context": "00000000",
                    "modulemd": platform_mmd,
                }
            ],
            "meta": {"next": None},
        }

        mock_session.get.return_value = mock_res
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        result = resolver.resolve_profiles(
            formatted_testmodule_mmd, ("buildroot", "srpm-buildroot")
        )
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

        mbs_url = conf.mbs_url
        expected_query = {
            "name": "platform",
            "stream": "f28",
            "version": "3",
            "context": "00000000",
            "verbose": True,
            "order_desc_by": "version",
            "page": 1,
            "per_page": 10,
            "state": ["ready"],
        }

        mock_session.get.assert_called_once_with(mbs_url, params=expected_query)
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
    def test_resolve_profiles_local_module(
        self, local_builds, conf_system, formatted_testmodule_mmd
    ):
        tests.clean_database(add_platform_module=False)
        load_local_builds(["platform:f28"])

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        result = resolver.resolve_profiles(
            formatted_testmodule_mmd, ("buildroot", "srpm-buildroot"))
        expected = {"buildroot": {"foo"}, "srpm-buildroot": {"bar"}}
        assert result == expected

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_empty_buildrequired_modulemds(self, request_session):
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        request_session.get.return_value = Mock(ok=True)
        request_session.get.return_value.json.return_value = {"items": [], "meta": {"next": None}}

        platform = db_session.query(
            module_build_service.common.models.ModuleBuild).filter_by(id=1).one()
        result = resolver.get_buildrequired_modulemds("nodejs", "10", platform.mmd())
        assert [] == result

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_buildrequired_modulemds(self, mock_session):
        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        mock_session.get.return_value = Mock(ok=True)
        mock_session.get.return_value.json.return_value = {
            "items": [
                {
                    "name": "nodejs",
                    "stream": "10",
                    "version": 1,
                    "context": "c1",
                    "modulemd": mmd_to_str(
                        tests.make_module("nodejs:10:1:c1"),
                    ),
                },
                {
                    "name": "nodejs",
                    "stream": "10",
                    "version": 2,
                    "context": "c1",
                    "modulemd": mmd_to_str(
                        tests.make_module("nodejs:10:2:c1"),
                    ),
                },
            ],
            "meta": {"next": None},
        }

        platform = db_session.query(
            module_build_service.common.models.ModuleBuild).filter_by(id=1).one()
        result = resolver.get_buildrequired_modulemds("nodejs", "10", platform.mmd())

        assert 1 == len(result)
        mmd = result[0]
        assert "nodejs" == mmd.get_module_name()
        assert "10" == mmd.get_stream_name()
        assert 1 == mmd.get_version()
        assert "c1" == mmd.get_context()

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_module_count(self, mock_session):
        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.return_value = {
            "items": [{"name": "platform", "stream": "f28", "version": "3", "context": "00000000"}],
            "meta": {"total": 5},
        }
        mock_session.get.return_value = mock_res

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        count = resolver.get_module_count(name="platform", stream="f28")

        assert count == 5
        mock_session.get.assert_called_once_with(
            "https://mbs.fedoraproject.org/module-build-service/1/module-builds/",
            params={"name": "platform", "page": 1, "per_page": 1, "short": True, "stream": "f28"},
        )

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_latest_with_virtual_stream(self, mock_session, platform_mmd):
        mock_res = Mock()
        mock_res.ok.return_value = True
        mock_res.json.return_value = {
            "items": [
                {
                    "context": "00000000",
                    "modulemd": platform_mmd,
                    "name": "platform",
                    "stream": "f28",
                    "version": "3",
                }
            ],
            "meta": {"total": 5},
        }
        mock_session.get.return_value = mock_res

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
        mmd = resolver.get_latest_with_virtual_stream("platform", "virtualf28")

        assert mmd.get_module_name() == "platform"
        mock_session.get.assert_called_once_with(
            "https://mbs.fedoraproject.org/module-build-service/1/module-builds/",
            params={
                "name": "platform",
                "order_desc_by": ["stream_version", "version"],
                "page": 1,
                "per_page": 1,
                "verbose": True,
                "virtual_stream": "virtualf28",
            },
        )

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
    def test_get_buildrequired_modulemds_local_builds(
        self, local_builds, conf_system
    ):
        tests.clean_database()
        with app.app_context():
            load_local_builds(["testmodule"])

            resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")
            result = resolver.get_buildrequired_modulemds(
                "testmodule", "master", "platform:f28:1:00000000")
            assert 1 == len(result)
            mmd = result[0]
            assert "testmodule" == mmd.get_module_name()
            assert "master" == mmd.get_stream_name()
            assert 20170816080816 == mmd.get_version()
            assert "321" == mmd.get_context()

    @patch("module_build_service.resolver.MBSResolver.requests_session")
    def test_get_buildrequired_modulemds_kojiresolver(self, mock_session):
        """
        Test that MBSResolver uses KojiResolver as input when KojiResolver is enabled for
        the base module.
        """
        mock_session.get.return_value = Mock(ok=True)
        mock_session.get.return_value.json.return_value = {
            "items": [
                {
                    "name": "nodejs",
                    "stream": "10",
                    "version": 2,
                    "context": "c1",
                    "modulemd": mmd_to_str(
                        tests.make_module("nodejs:10:2:c1"),
                    ),
                },
            ],
            "meta": {"next": None},
        }

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="mbs")

        platform = db_session.query(
            module_build_service.common.models.ModuleBuild).filter_by(id=1).one()
        platform_mmd = platform.mmd()
        platform_xmd = platform_mmd.get_xmd()
        platform_xmd["mbs"]["koji_tag_with_modules"] = "module-f29-build"
        platform_mmd.set_xmd(platform_xmd)

        with patch.object(
                resolver, "get_buildrequired_koji_builds") as get_buildrequired_koji_builds:
            get_buildrequired_koji_builds.return_value = [{
                "build_id": 124, "name": "nodejs", "version": "10",
                "release": "2.c1", "tag_name": "foo-test"}]
            result = resolver.get_buildrequired_modulemds("nodejs", "10", platform_mmd)
            get_buildrequired_koji_builds.assert_called_once()

        assert 1 == len(result)
        mmd = result[0]
        assert "nodejs" == mmd.get_module_name()
        assert "10" == mmd.get_stream_name()
        assert 2 == mmd.get_version()
        assert "c1" == mmd.get_context()
