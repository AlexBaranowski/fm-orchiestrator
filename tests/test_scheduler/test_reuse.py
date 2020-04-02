# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import mock
import pytest
from sqlalchemy.orm.session import make_transient

from module_build_service.common import models
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import import_mmd, load_mmd, mmd_to_str
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler.reuse import get_reusable_component, get_reusable_module
from tests import clean_database, read_staged_data


@pytest.mark.usefixtures("reuse_component_init_data")
class TestUtilsComponentReuse:
    @pytest.mark.parametrize(
        "changed_component", ["perl-List-Compare", "perl-Tangerine", "tangerine", None]
    )
    def test_get_reusable_component_different_component(self, changed_component):
        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)
        if changed_component:
            mmd = second_module_build.mmd()
            mmd.get_rpm_component("tangerine").set_ref("00ea1da4192a2030f9ae023de3b3143ed647bbab")
            second_module_build.modulemd = mmd_to_str(mmd)

            second_module_changed_component = models.ComponentBuild.from_component_name(
                db_session, changed_component, second_module_build.id)
            second_module_changed_component.ref = "00ea1da4192a2030f9ae023de3b3143ed647bbab"
            db_session.add(second_module_changed_component)
            db_session.commit()

        plc_rv = get_reusable_component(second_module_build, "perl-List-Compare")
        pt_rv = get_reusable_component(second_module_build, "perl-Tangerine")
        tangerine_rv = get_reusable_component(second_module_build, "tangerine")

        if changed_component == "perl-List-Compare":
            # perl-Tangerine can be reused even though a component in its batch has changed
            assert plc_rv is None
            assert pt_rv.package == "perl-Tangerine"
            assert tangerine_rv is None
        elif changed_component == "perl-Tangerine":
            # perl-List-Compare can be reused even though a component in its batch has changed
            assert plc_rv.package == "perl-List-Compare"
            assert pt_rv is None
            assert tangerine_rv is None
        elif changed_component == "tangerine":
            # perl-List-Compare and perl-Tangerine can be reused since they are in an earlier
            # buildorder than tangerine
            assert plc_rv.package == "perl-List-Compare"
            assert pt_rv.package == "perl-Tangerine"
            assert tangerine_rv is None
        elif changed_component is None:
            # Nothing has changed so everthing can be used
            assert plc_rv.package == "perl-List-Compare"
            assert pt_rv.package == "perl-Tangerine"
            assert tangerine_rv.package == "tangerine"

    def test_get_reusable_component_different_rpm_macros(self):
        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)
        mmd = second_module_build.mmd()
        buildopts = Modulemd.Buildopts()
        buildopts.set_rpm_macros("%my_macro 1")
        mmd.set_buildopts(buildopts)
        second_module_build.modulemd = mmd_to_str(mmd)
        db_session.commit()

        plc_rv = get_reusable_component(second_module_build, "perl-List-Compare")
        assert plc_rv is None

        pt_rv = get_reusable_component(second_module_build, "perl-Tangerine")
        assert pt_rv is None

    @pytest.mark.parametrize("set_current_arch", [True, False])
    @pytest.mark.parametrize("set_database_arch", [True, False])
    def test_get_reusable_component_different_arches(
        self, set_database_arch, set_current_arch
    ):
        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)

        if set_current_arch:  # set architecture for current build
            mmd = second_module_build.mmd()
            component = mmd.get_rpm_component("tangerine")
            component.reset_arches()
            component.add_restricted_arch("i686")
            second_module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        if set_database_arch:  # set architecture for build in database
            second_module_changed_component = models.ComponentBuild.from_component_name(
                db_session, "tangerine", 2)
            mmd = second_module_changed_component.module_build.mmd()
            component = mmd.get_rpm_component("tangerine")
            component.reset_arches()
            component.add_restricted_arch("i686")
            second_module_changed_component.module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        tangerine = get_reusable_component(second_module_build, "tangerine")
        assert bool(tangerine is None) != bool(set_current_arch == set_database_arch)

    @pytest.mark.parametrize(
        "reuse_component",
        ["perl-Tangerine", "perl-List-Compare", "tangerine"])
    @pytest.mark.parametrize(
        "changed_component",
        ["perl-Tangerine", "perl-List-Compare", "tangerine"])
    def test_get_reusable_component_different_batch(
        self, changed_component, reuse_component
    ):
        """
        Test that we get the correct reuse behavior for the changed-and-after strategy. Changes
        to earlier batches should prevent reuse, but changes to later batches should not.
        For context, see https://pagure.io/fm-orchestrator/issue/1298
        """

        if changed_component == reuse_component:
            # we're only testing the cases where these are different
            # this case is already covered by test_get_reusable_component_different_component
            return

        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)

        # update batch for changed component
        changed_component = models.ComponentBuild.from_component_name(
            db_session, changed_component, second_module_build.id)
        orig_batch = changed_component.batch
        changed_component.batch = orig_batch + 1
        db_session.commit()

        reuse_component = models.ComponentBuild.from_component_name(
            db_session, reuse_component, second_module_build.id)

        reuse_result = get_reusable_component(second_module_build, reuse_component.package)
        # Component reuse should only be blocked when an earlier batch has been changed.
        # In this case, orig_batch is the earliest batch that has been changed (the changed
        # component has been removed from it and added to the following one).
        assert bool(reuse_result is None) == bool(reuse_component.batch > orig_batch)

    @pytest.mark.parametrize(
        "reuse_component",
        ["perl-Tangerine", "perl-List-Compare", "tangerine"])
    @pytest.mark.parametrize(
        "changed_component",
        ["perl-Tangerine", "perl-List-Compare", "tangerine"])
    def test_get_reusable_component_different_arch_in_batch(
        self, changed_component, reuse_component
    ):
        """
        Test that we get the correct reuse behavior for the changed-and-after strategy. Changes
        to the architectures in earlier batches should prevent reuse, but such changes to later
        batches should not.
        For context, see https://pagure.io/fm-orchestrator/issue/1298
        """
        if changed_component == reuse_component:
            # we're only testing the cases where these are different
            # this case is already covered by test_get_reusable_component_different_arches
            return

        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)

        # update arch for changed component
        mmd = second_module_build.mmd()
        component = mmd.get_rpm_component(changed_component)
        component.reset_arches()
        component.add_restricted_arch("i686")
        second_module_build.modulemd = mmd_to_str(mmd)
        db_session.commit()

        changed_component = models.ComponentBuild.from_component_name(
            db_session, changed_component, second_module_build.id)
        reuse_component = models.ComponentBuild.from_component_name(
            db_session, reuse_component, second_module_build.id)

        reuse_result = get_reusable_component(second_module_build, reuse_component.package)
        # Changing the arch of a component should prevent reuse only when the changed component
        # is in a batch earlier than the component being considered for reuse.
        assert bool(reuse_result is None) == bool(reuse_component.batch > changed_component.batch)

    @pytest.mark.parametrize("rebuild_strategy", models.ModuleBuild.rebuild_strategies.keys())
    def test_get_reusable_component_different_buildrequires_stream(self, rebuild_strategy):
        first_module_build = models.ModuleBuild.get_by_id(db_session, 2)
        first_module_build.rebuild_strategy = rebuild_strategy
        db_session.commit()

        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)
        mmd = second_module_build.mmd()
        xmd = mmd.get_xmd()
        xmd["mbs"]["buildrequires"]["platform"]["stream"] = "different"
        deps = Modulemd.Dependencies()
        deps.add_buildtime_stream("platform", "different")
        deps.add_runtime_stream("platform", "different")
        mmd.clear_dependencies()
        mmd.add_dependencies(deps)

        mmd.set_xmd(xmd)
        second_module_build.modulemd = mmd_to_str(mmd)
        second_module_build.build_context = \
            models.ModuleBuild.contexts_from_mmd(second_module_build.modulemd).build_context
        second_module_build.rebuild_strategy = rebuild_strategy
        db_session.commit()

        plc_rv = get_reusable_component(second_module_build, "perl-List-Compare")
        pt_rv = get_reusable_component(second_module_build, "perl-Tangerine")
        tangerine_rv = get_reusable_component(second_module_build, "tangerine")

        assert plc_rv is None
        assert pt_rv is None
        assert tangerine_rv is None

    def test_get_reusable_component_different_buildrequires(self):
        second_module_build = models.ModuleBuild.get_by_id(db_session, 3)
        mmd = second_module_build.mmd()
        mmd.get_dependencies()[0].add_buildtime_stream("some_module", "master")
        xmd = mmd.get_xmd()
        xmd["mbs"]["buildrequires"] = {
            "some_module": {
                "ref": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
                "stream": "master",
                "version": "20170123140147",
            }
        }
        mmd.set_xmd(xmd)
        second_module_build.modulemd = mmd_to_str(mmd)
        second_module_build.build_context = models.ModuleBuild.calculate_build_context(
            xmd["mbs"]["buildrequires"])
        db_session.commit()

        plc_rv = get_reusable_component(second_module_build, "perl-List-Compare")
        assert plc_rv is None

        pt_rv = get_reusable_component(second_module_build, "perl-Tangerine")
        assert pt_rv is None

        tangerine_rv = get_reusable_component(second_module_build, "tangerine")
        assert tangerine_rv is None


class TestReuseSharedUserSpace:
    def setup_method(self, test_method):
        clean_database()

    def teardown_method(self, test_method):
        clean_database()

    @pytest.mark.usefixtures("reuse_shared_userspace_init_data")
    def test_get_reusable_component_shared_userspace_ordering(self):
        """
        For modules with lot of components per batch, there is big chance that
        the database will return them in different order than what we have for
        current `new_module`. In this case, reuse code should still be able to
        reuse the components.
        """
        old_module = models.ModuleBuild.get_by_id(db_session, 2)
        new_module = models.ModuleBuild.get_by_id(db_session, 3)
        rv = get_reusable_component(new_module, "llvm", previous_module_build=old_module)
        assert rv.package == "llvm"


@pytest.mark.usefixtures("reuse_component_init_data")
class TestUtilsModuleReuse:

    def test_get_reusable_module_when_reused_module_not_set(self):
        module = db_session.query(models.ModuleBuild)\
                           .filter_by(name="testmodule")\
                           .order_by(models.ModuleBuild.id.desc())\
                           .first()
        module.state = models.BUILD_STATES["build"]
        db_session.commit()

        assert not module.reused_module

        reusable_module = get_reusable_module(module)

        assert module.reused_module
        assert reusable_module.id == module.reused_module_id

    def test_get_reusable_module_when_reused_module_already_set(self):
        modules = db_session.query(models.ModuleBuild)\
                            .filter_by(name="testmodule")\
                            .order_by(models.ModuleBuild.id.desc())\
                            .limit(2).all()
        build_module = modules[0]
        reused_module = modules[1]
        build_module.state = models.BUILD_STATES["build"]
        build_module.reused_module_id = reused_module.id
        db_session.commit()

        assert build_module.reused_module
        assert reused_module == build_module.reused_module

        reusable_module = get_reusable_module(build_module)

        assert build_module.reused_module
        assert reusable_module.id == build_module.reused_module_id
        assert reusable_module.id == reused_module.id

    @pytest.mark.parametrize("allow_ocbm", (True, False))
    @mock.patch(
        "module_build_service.common.config.Config.allow_only_compatible_base_modules",
        new_callable=mock.PropertyMock,
    )
    def test_get_reusable_module_use_latest_build(self, cfg, allow_ocbm):
        """
        Test that the `get_reusable_module` tries to reuse the latest module in case when
        multiple modules can be reused allow_only_compatible_base_modules is True.
        """
        cfg.return_value = allow_ocbm
        # Set "fedora" virtual stream to platform:f28.
        platform_f28 = db_session.query(models.ModuleBuild).filter_by(name="platform").one()
        mmd = platform_f28.mmd()
        xmd = mmd.get_xmd()
        xmd["mbs"]["virtual_streams"] = ["fedora"]
        mmd.set_xmd(xmd)
        platform_f28.modulemd = mmd_to_str(mmd)
        platform_f28.update_virtual_streams(db_session, ["fedora"])

        # Create platform:f29 with "fedora" virtual stream.
        mmd = load_mmd(read_staged_data("platform"))
        mmd = mmd.copy("platform", "f29")
        xmd = mmd.get_xmd()
        xmd["mbs"]["virtual_streams"] = ["fedora"]
        mmd.set_xmd(xmd)
        platform_f29 = import_mmd(db_session, mmd)[0]

        # Create another copy of `testmodule:master` which should be reused, because its
        # stream version will be higher than the previous one. Also set its buildrequires
        # to platform:f29.
        latest_module = db_session.query(models.ModuleBuild).filter_by(
            name="testmodule", state=models.BUILD_STATES["ready"]).one()
        # This is used to clone the ModuleBuild SQLAlchemy object without recreating it from
        # scratch.
        db_session.expunge(latest_module)
        make_transient(latest_module)

        # Change the platform:f28 buildrequirement to platform:f29 and recompute the build_context.
        mmd = latest_module.mmd()
        xmd = mmd.get_xmd()
        xmd["mbs"]["buildrequires"]["platform"]["stream"] = "f29"
        mmd.set_xmd(xmd)
        latest_module.modulemd = mmd_to_str(mmd)
        contexts = models.ModuleBuild.contexts_from_mmd(
            latest_module.modulemd
        )
        latest_module.build_context = contexts.build_context
        latest_module.context = contexts.context
        latest_module.buildrequires = [platform_f29]

        # Set the `id` to None, so new one is generated by SQLAlchemy.
        latest_module.id = None
        db_session.add(latest_module)
        db_session.commit()

        module = db_session.query(models.ModuleBuild)\
                           .filter_by(name="testmodule")\
                           .filter_by(state=models.BUILD_STATES["build"])\
                           .one()
        db_session.commit()

        reusable_module = get_reusable_module(module)

        if allow_ocbm:
            assert reusable_module.id == latest_module.id
        else:
            # There are two testmodules in ready state, the first one with
            # lower id is what we want.
            first_module = db_session.query(models.ModuleBuild).filter_by(
                name="testmodule", state=models.BUILD_STATES["ready"]
            ).order_by(models.ModuleBuild.id).first()

            assert reusable_module.id == first_module.id

    @pytest.mark.parametrize("allow_ocbm", (True, False))
    @mock.patch(
        "module_build_service.common.config.Config.allow_only_compatible_base_modules",
        new_callable=mock.PropertyMock,
    )
    @mock.patch("koji.ClientSession")
    @mock.patch(
        "module_build_service.common.config.Config.resolver",
        new_callable=mock.PropertyMock, return_value="koji"
    )
    def test_get_reusable_module_koji_resolver(
            self, resolver, ClientSession, cfg, allow_ocbm):
        """
        Test that get_reusable_module works with KojiResolver.
        """
        cfg.return_value = allow_ocbm

        # Mock the listTagged so the testmodule:master is listed as tagged in the
        # module-fedora-27-build Koji tag.
        koji_session = ClientSession.return_value
        koji_session.listTagged.return_value = [
            {
                "build_id": 123, "name": "testmodule", "version": "master",
                "release": "20170109091357.78e4a6fd", "tag_name": "module-fedora-27-build"
            }]

        koji_session.multiCall.return_value = [
            [build] for build in koji_session.listTagged.return_value]

        # Mark platform:f28 as KojiResolver ready by defining "koji_tag_with_modules".
        # Also define the "virtual_streams" to possibly confuse the get_reusable_module.
        platform_f28 = db_session.query(models.ModuleBuild).filter_by(name="platform").one()
        mmd = platform_f28.mmd()
        xmd = mmd.get_xmd()
        xmd["mbs"]["virtual_streams"] = ["fedora"]
        xmd["mbs"]["koji_tag_with_modules"] = "module-fedora-27-build"
        mmd.set_xmd(xmd)
        platform_f28.modulemd = mmd_to_str(mmd)
        platform_f28.update_virtual_streams(db_session, ["fedora"])

        # Create platform:f27 without KojiResolver support.
        mmd = load_mmd(read_staged_data("platform"))
        mmd = mmd.copy("platform", "f27")
        xmd = mmd.get_xmd()
        xmd["mbs"]["virtual_streams"] = ["fedora"]
        mmd.set_xmd(xmd)
        platform_f27 = import_mmd(db_session, mmd)[0]

        # Change the reusable testmodule:master to buildrequire platform:f27.
        latest_module = db_session.query(models.ModuleBuild).filter_by(
            name="testmodule", state=models.BUILD_STATES["ready"]).one()
        mmd = latest_module.mmd()
        xmd = mmd.get_xmd()
        xmd["mbs"]["buildrequires"]["platform"]["stream"] = "f27"
        mmd.set_xmd(xmd)
        latest_module.modulemd = mmd_to_str(mmd)
        latest_module.buildrequires = [platform_f27]

        # Recompute the build_context and ensure that `build_context` changed while
        # `build_context_no_bms` did not change.
        contexts = models.ModuleBuild.contexts_from_mmd(latest_module.modulemd)

        assert latest_module.build_context_no_bms == contexts.build_context_no_bms
        assert latest_module.build_context != contexts.build_context

        latest_module.build_context = contexts.build_context
        latest_module.build_context_no_bms = contexts.build_context_no_bms
        db_session.commit()

        # Get the module we want to build.
        module = db_session.query(models.ModuleBuild)\
                           .filter_by(name="testmodule")\
                           .filter_by(state=models.BUILD_STATES["build"])\
                           .one()

        reusable_module = get_reusable_module(module)

        assert reusable_module.id == latest_module.id
