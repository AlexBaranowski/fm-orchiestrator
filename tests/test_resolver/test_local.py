# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime

import pytest

from module_build_service.common.config import conf
from module_build_service.common.models import ModuleBuild
from module_build_service.common.utils import import_mmd, load_mmd, mmd_to_str
from module_build_service.scheduler.db_session import db_session
import module_build_service.resolver as mbs_resolver
import tests


@pytest.mark.usefixtures("reuse_component_init_data")
class TestLocalResolverModule:

    def test_get_buildrequired_modulemds(self):
        mmd = load_mmd(tests.read_staged_data("platform"))
        mmd = mmd.copy(mmd.get_module_name(), "f8")
        import_mmd(db_session, mmd)
        platform_f8 = db_session.query(ModuleBuild).filter_by(stream="f8").one()
        mmd = mmd.copy("testmodule", "master")
        mmd.set_version(20170109091357)
        mmd.set_context("123")
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
        db_session.add(build)
        db_session.commit()

        resolver = mbs_resolver.GenericResolver.create(db_session, conf, backend="local")
        result = resolver.get_buildrequired_modulemds(
            "testmodule", "master", platform_f8.mmd().get_nsvc())
        nsvcs = {m.get_nsvc() for m in result}
        assert nsvcs == {
            "testmodule:master:20170109091357:9c690d0e",
            "testmodule:master:20170109091357:123"
        }
