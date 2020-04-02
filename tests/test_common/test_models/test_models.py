# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from mock import patch
import pytest

from module_build_service.common.config import conf
from module_build_service.common.models import ComponentBuild, ComponentBuildTrace, ModuleBuild
from module_build_service.common.utils import load_mmd, mmd_to_str
from module_build_service.scheduler.db_session import db_session
from tests import (
    clean_database,
    init_data as init_data_contexts,
    make_module_in_db,
    module_build_from_modulemd,
    read_staged_data,
)


@pytest.mark.usefixtures("model_tests_init_data")
class TestModels:

    def test_app_sqlalchemy_events(self):
        component_build = ComponentBuild(
            package="before_models_committed",
            scmurl="git://pkgs.domain.local/rpms/before_models_committed?"
                   "#9999999999999999999999999999999999999999",
            format="rpms",
            task_id=999999999,
            state=1,
            nvr="before_models_committed-0.0.0-0.module_before_models_committed_0_0",
            batch=1,
            module_id=1,
            buildonly=True,
        )

        db_session.add(component_build)
        db_session.commit()

        component_builds_trace = db_session.query(ComponentBuildTrace).filter(
            ComponentBuildTrace.component_id == component_build.id).one()
        db_session.commit()

        assert component_builds_trace.id == 1
        assert component_builds_trace.component_id == 1
        assert component_builds_trace.state == 1
        assert component_builds_trace.state_reason is None
        assert component_builds_trace.task_id == 999999999

    def test_context_functions(self):
        """ Test that the build_context, runtime_context, and context hashes are correctly
        determined"""
        db_session.commit()

        build = ModuleBuild.get_by_id(db_session, 1)
        db_session.commit()
        build.modulemd = read_staged_data("testmodule_dependencies")
        build.build_context, build.runtime_context, build.context, build.build_context_no_bms = \
            ModuleBuild.contexts_from_mmd(build.modulemd)
        assert build.build_context == "089df24993c037e10174f3fa7342ab4dc191a4d4"
        assert build.runtime_context == "bbc84c7b817ab3dd54916c0bcd6c6bdf512f7f9c"
        assert build.context == "3ee22b28"
        assert build.build_context_no_bms == "089df24993c037e10174f3fa7342ab4dc191a4d4"

    def test_siblings_property(self):
        """ Tests that the siblings property returns the ID of all modules with
        the same name:stream:version
        """
        clean_database()
        mmd = load_mmd(read_staged_data("formatted_testmodule"))
        for i in range(3):
            build = module_build_from_modulemd(mmd_to_str(mmd))
            build.context = "f6e2aec" + str(i)
            build.build_context = "f6e2aeec7576196241b9afa0b6b22acf2b6873d" + str(i)
            build.runtime_context = "bbc84c7b817ab3dd54916c0bcd6c6bdf512f7f9c" + str(i)
            db_session.add(build)
        db_session.commit()

        build_one = ModuleBuild.get_by_id(db_session, 2)
        sibling_ids = build_one.siblings(db_session)
        db_session.commit()

        assert sorted(sibling_ids) == [3, 4]

    @pytest.mark.parametrize(
        "stream,right_pad,expected",
        [
            ["f27", True, 270000.0],
            ["f27.02.30", True, 270230.0],
            ["f27", False, 27.0],
            ["f27.02.30", False, 270230.0],
            ["el8", True, 080000.0],
            ["el8.1.0", True, 080100.0],
            ["el8.z", True, 080000.2],
            ["el8.1.0.z", True, 080100.3],
        ],
    )
    @patch.object(conf, "stream_suffixes", new={r"el\d+\.z": 0.2, r"el\d+\.\d+\.\d+\.z": 0.3})
    def test_get_stream_version(self, stream, right_pad, expected):
        assert expected == ModuleBuild.get_stream_version(stream, right_pad)


class TestModelsGetStreamsContexts:
    def test_get_last_build_in_all_streams(self):
        init_data_contexts(contexts=True)
        builds = ModuleBuild.get_last_build_in_all_streams(db_session, "nginx")
        builds = sorted([
            "%s:%s:%s" % (build.name, build.stream, str(build.version)) for build in builds
        ])
        db_session.commit()
        assert builds == ["nginx:%d:%d" % (i, i + 2) for i in range(10)]

    def test_get_last_build_in_all_stream_last_version(self):
        init_data_contexts(contexts=False)
        builds = ModuleBuild.get_last_build_in_all_streams(db_session, "nginx")
        builds = [
            "%s:%s:%s" % (build.name, build.stream, str(build.version)) for build in builds
        ]
        db_session.commit()
        assert builds == ["nginx:1:11"]

    def test_get_last_builds_in_stream(self):
        init_data_contexts(contexts=True)
        builds = ModuleBuild.get_last_builds_in_stream(db_session, "nginx", "1")
        builds = [
            "%s:%s:%s:%s" % (build.name, build.stream, str(build.version), build.context)
            for build in builds
        ]
        db_session.commit()
        assert sorted(builds) == ["nginx:1:3:795e97c1", "nginx:1:3:d5a6c0fa"]

    def test_get_last_builds_in_stream_version_lte(self):
        init_data_contexts(1, multiple_stream_versions=True)
        builds = ModuleBuild.get_last_builds_in_stream_version_lte(db_session, "platform", 290100)
        builds = {
            "%s:%s:%s:%s" % (build.name, build.stream, str(build.version), build.context)
            for build in builds
        }
        db_session.commit()
        assert builds == {"platform:f29.0.0:3:00000000", "platform:f29.1.0:3:00000000"}

    def test_get_last_builds_in_stream_version_lte_different_versions(self):
        """
        Tests that get_last_builds_in_stream_version_lte works in case the
        name:stream_ver modules have different versions.
        """
        clean_database(False)

        make_module_in_db(
            "platform:f29.1.0:10:old_version", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.1.0:15:c11.another", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.1.0:15:c11", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.2.0:0:old_version", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.2.0:1:c11", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.3.0:15:old_version", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.3.0:20:c11", virtual_streams=["f29"])

        builds = ModuleBuild.get_last_builds_in_stream_version_lte(
            db_session, "platform", 290200)
        builds = {
            "%s:%s:%s:%s" % (build.name, build.stream, str(build.version), build.context)
            for build in builds
        }
        db_session.commit()
        assert builds == {
            "platform:f29.1.0:15:c11",
            "platform:f29.1.0:15:c11.another",
            "platform:f29.2.0:1:c11",
        }

    def test_get_module_count(self):
        clean_database(False)
        make_module_in_db("platform:f29.1.0:10:c11")
        make_module_in_db("platform:f29.1.0:10:c12")

        count = ModuleBuild.get_module_count(db_session, name="platform")
        db_session.commit()
        assert count == 2

    def test_add_virtual_streams_filter(self):
        clean_database(False)

        make_module_in_db(
            "platform:f29.1.0:10:c1", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.1.0:15:c1", virtual_streams=["f29"])
        make_module_in_db(
            "platform:f29.3.0:15:old_version", virtual_streams=["f28", "f29"])
        make_module_in_db(
            "platform:f29.3.0:20:c11", virtual_streams=["f30"])

        query = db_session.query(ModuleBuild).filter_by(name="platform")
        query = ModuleBuild._add_virtual_streams_filter(db_session, query, ["f28", "f29"])
        count = query.count()
        db_session.commit()
        assert count == 3
