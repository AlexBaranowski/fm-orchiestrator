# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from mock import patch, PropertyMock
import pytest

from module_build_service.common import models
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import load_mmd
from module_build_service.common.resolve import get_base_module_mmds
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, make_module_in_db, init_data, read_staged_data


class TestResolve:
    def setup_method(self, test_method):
        clean_database(False)

    def teardown_method(self, test_method):
        clean_database()

    def test__get_base_module_mmds(self):
        """Ensure the correct results are returned without duplicates."""
        init_data(data_size=1, multiple_stream_versions=True)
        mmd = load_mmd(read_staged_data("testmodule_v2.yaml"))
        deps = mmd.get_dependencies()[0]
        new_deps = Modulemd.Dependencies()
        for stream in deps.get_runtime_streams("platform"):
            new_deps.add_runtime_stream("platform", stream)
        new_deps.add_buildtime_stream("platform", "f29.1.0")
        new_deps.add_buildtime_stream("platform", "f29.2.0")
        mmd.remove_dependencies(deps)
        mmd.add_dependencies(new_deps)

        mmds = get_base_module_mmds(db_session, mmd)
        expected = {"platform:f29.0.0", "platform:f29.1.0", "platform:f29.2.0"}
        # Verify no duplicates were returned before doing set operations
        assert len(mmds["ready"]) == len(expected)
        # Verify the expected ones were returned
        actual = set()
        for mmd_ in mmds["ready"]:
            actual.add("{}:{}".format(mmd_.get_module_name(), mmd_.get_stream_name()))
        assert actual == expected

    @pytest.mark.parametrize("virtual_streams", (None, ["f29"], ["lp29"]))
    def test__get_base_module_mmds_virtual_streams(self, virtual_streams):
        """Ensure the correct results are returned without duplicates."""
        init_data(data_size=1, multiple_stream_versions=True)
        mmd = load_mmd(read_staged_data("testmodule_v2"))
        deps = mmd.get_dependencies()[0]
        new_deps = Modulemd.Dependencies()
        for stream in deps.get_runtime_streams("platform"):
            new_deps.add_runtime_stream("platform", stream)
        new_deps.add_buildtime_stream("platform", "f29.2.0")
        mmd.remove_dependencies(deps)
        mmd.add_dependencies(new_deps)

        make_module_in_db("platform:lp29.1.1:12:c11", virtual_streams=virtual_streams)

        mmds = get_base_module_mmds(db_session, mmd)
        if virtual_streams == ["f29"]:
            expected = {
                "platform:f29.0.0",
                "platform:f29.1.0",
                "platform:f29.2.0",
                "platform:lp29.1.1"
            }
        else:
            expected = {"platform:f29.0.0", "platform:f29.1.0", "platform:f29.2.0"}
        # Verify no duplicates were returned before doing set operations
        assert len(mmds["ready"]) == len(expected)
        # Verify the expected ones were returned
        actual = set()
        for mmd_ in mmds["ready"]:
            actual.add("{}:{}".format(mmd_.get_module_name(), mmd_.get_stream_name()))
        assert actual == expected

    @patch(
        "module_build_service.common.config.Config.allow_only_compatible_base_modules",
        new_callable=PropertyMock, return_value=False
    )
    def test__get_base_module_mmds_virtual_streams_only_major_versions(self, cfg):
        """Ensure the correct results are returned without duplicates."""
        init_data(data_size=1, multiple_stream_versions=["foo28", "foo29", "foo30"])

        # Mark platform:foo28 as garbage to test that it is still considered as compatible.
        platform = db_session.query(models.ModuleBuild).filter_by(
            name="platform", stream="foo28").first()
        platform.state = "garbage"
        db_session.add(platform)
        db_session.commit()

        mmd = load_mmd(read_staged_data("testmodule_v2"))
        deps = mmd.get_dependencies()[0]
        new_deps = Modulemd.Dependencies()
        for stream in deps.get_runtime_streams("platform"):
            new_deps.add_runtime_stream("platform", stream)
        new_deps.add_buildtime_stream("platform", "foo29")
        mmd.remove_dependencies(deps)
        mmd.add_dependencies(new_deps)

        mmds = get_base_module_mmds(db_session, mmd)
        expected = {}
        expected["ready"] = {"platform:foo29", "platform:foo30"}
        expected["garbage"] = {"platform:foo28"}

        # Verify no duplicates were returned before doing set operations
        assert len(mmds) == len(expected)
        for k in expected.keys():
            assert len(mmds[k]) == len(expected[k])
            # Verify the expected ones were returned
            actual = set()
            for mmd_ in mmds[k]:
                actual.add("{}:{}".format(mmd_.get_module_name(), mmd_.get_stream_name()))
            assert actual == expected[k]
