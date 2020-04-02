# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from mock import patch
import pytest

from module_build_service import app
from module_build_service.common import models
from module_build_service.common.models import BUILD_STATES, ModuleBuild
from module_build_service.manage import manager_wrapper, retire
from module_build_service.scheduler.db_session import db_session
from module_build_service.web.utils import deps_to_dict
from tests import clean_database, staged_data_filename


@pytest.mark.usefixtures("model_tests_init_data")
class TestMBSManage:

    @pytest.mark.parametrize(
        ("identifier", "is_valid"),
        (
            ("", False),
            ("spam", False),
            ("spam:bacon", True),
            ("spam:bacon:eggs", True),
            ("spam:bacon:eggs:ham", True),
            ("spam:bacon:eggs:ham:sausage", False),
        ),
    )
    def test_retire_identifier_validation(self, identifier, is_valid):
        if is_valid:
            retire(identifier)
        else:
            with pytest.raises(ValueError):
                retire(identifier)

    @pytest.mark.parametrize(
        ("overrides", "identifier", "changed_count"),
        (
            ({"name": "pickme"}, "pickme:eggs", 1),
            ({"stream": "pickme"}, "spam:pickme", 1),
            ({"version": "pickme"}, "spam:eggs:pickme", 1),
            ({"context": "pickme"}, "spam:eggs:ham:pickme", 1),
            ({}, "spam:eggs", 3),
            ({"version": "pickme"}, "spam:eggs", 3),
            ({"context": "pickme"}, "spam:eggs:ham", 3),
        ),
    )
    @patch("module_build_service.manage.prompt_bool")
    def test_retire_build(self, prompt_bool, overrides, identifier, changed_count):
        prompt_bool.return_value = True

        module_builds = (
            db_session.query(ModuleBuild)
            .filter_by(state=BUILD_STATES["ready"])
            .order_by(ModuleBuild.id.desc())
            .all()
        )
        # Verify our assumption of the amount of ModuleBuilds in database
        assert len(module_builds) == 3

        for x, build in enumerate(module_builds):
            build.name = "spam"
            build.stream = "eggs"
            build.version = "ham"
            build.context = str(x)

        for attr, value in overrides.items():
            setattr(module_builds[0], attr, value)

        db_session.commit()

        retire(identifier)
        retired_module_builds = (
            db_session.query(ModuleBuild)
            .filter_by(state=BUILD_STATES["garbage"])
            .order_by(ModuleBuild.id.desc())
            .all()
        )

        assert len(retired_module_builds) == changed_count
        for x in range(changed_count):
            assert retired_module_builds[x].id == module_builds[x].id
            assert retired_module_builds[x].state == BUILD_STATES["garbage"]

    @pytest.mark.parametrize(
        ("confirm_prompt", "confirm_arg", "confirm_expected"),
        (
            (True, False, True),
            (True, True, True),
            (False, False, False),
            (False, True, True)
        ),
    )
    @patch("module_build_service.manage.prompt_bool")
    def test_retire_build_confirm_prompt(
        self, prompt_bool, confirm_prompt, confirm_arg, confirm_expected
    ):
        prompt_bool.return_value = confirm_prompt

        module_builds = db_session.query(ModuleBuild).filter_by(state=BUILD_STATES["ready"]).all()
        # Verify our assumption of the amount of ModuleBuilds in database
        assert len(module_builds) == 3

        for x, build in enumerate(module_builds):
            build.name = "spam" + str(x) if x > 0 else "spam"
            build.stream = "eggs"

        db_session.commit()

        retire("spam:eggs", confirm_arg)
        retired_module_builds = (
            db_session.query(ModuleBuild).filter_by(state=BUILD_STATES["garbage"]).all()
        )

        expected_changed_count = 1 if confirm_expected else 0
        assert len(retired_module_builds) == expected_changed_count


class TestCommandBuildModuleLocally:
    """Test mbs-manager subcommand build_module_locally"""

    def setup_method(self, test_method):
        clean_database()

        # Do not allow flask_script exits by itself because we have to assert
        # something after the command finishes.
        self.sys_exit_patcher = patch("sys.exit")
        self.mock_sys_exit = self.sys_exit_patcher.start()

        # The consumer is not required to run actually, so it does not make
        # sense to publish message after creating a module build.
        self.publish_patcher = patch("module_build_service.common.messaging.publish")
        self.mock_publish = self.publish_patcher.start()

        # Don't allow conf.set_item call to modify conf actually inside command
        self.set_item_patcher = patch("module_build_service.manage.conf.set_item")
        self.mock_set_item = self.set_item_patcher.start()

        # Avoid to create the local sqlite database for the command, which is
        # useless for running tests here.
        self.create_all_patcher = patch("module_build_service.manage.db.create_all")
        self.mock_create_all = self.create_all_patcher.start()

    def teardown_method(self, test_method):
        self.create_all_patcher.stop()
        self.mock_set_item.stop()
        self.publish_patcher.stop()
        self.sys_exit_patcher.stop()

    def _run_manager_wrapper(self, cli_cmd):
        # build_module_locally changes database uri to a local SQLite database file.
        # Restore the uri to original one in order to not impact the database
        # session in subsequent tests.
        original_db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
        try:
            with patch("sys.argv", new=cli_cmd):
                manager_wrapper()
        finally:
            app.config["SQLALCHEMY_DATABASE_URI"] = original_db_uri

    @patch("module_build_service.scheduler.local.main")
    def test_set_stream(self, main):
        cli_cmd = [
            "mbs-manager", "build_module_locally",
            "--set-stream", "platform:f28",
            "--file", staged_data_filename("testmodule-local-build.yaml")
        ]

        self._run_manager_wrapper(cli_cmd)

        # Since module_build_service.scheduler.local.main is mocked, MBS does
        # not really build the testmodule for this test. Following lines assert
        # the fact:
        # Module testmodule-local-build is expanded and stored into database,
        # and this build has buildrequires platform:f28 and requires
        # platform:f28.
        # Please note that, the f28 is specified from command line option
        # --set-stream, which is the point this test tests.

        builds = db_session.query(models.ModuleBuild).filter_by(
            name="testmodule-local-build").all()
        assert 1 == len(builds)

        testmodule_build = builds[0]
        mmd_deps = testmodule_build.mmd().get_dependencies()

        deps_dict = deps_to_dict(mmd_deps[0], "buildtime")
        assert ["f28"] == deps_dict["platform"]
        deps_dict = deps_to_dict(mmd_deps[0], "runtime")
        assert ["f28"] == deps_dict["platform"]

    @patch("module_build_service.manage.logging")
    def test_ambiguous_stream(self, logging):
        cli_cmd = [
            "mbs-manager", "build_module_locally",
            "--file", staged_data_filename("testmodule-local-build.yaml")
        ]

        self._run_manager_wrapper(cli_cmd)

        args, _ = logging.error.call_args_list[0]
        assert "There are multiple streams to choose from for module platform." == args[0]
        args, _ = logging.error.call_args_list[1]
        assert "Use '-s module_name:module_stream' to choose the stream" == args[0]

    def test_module_build_failed(self):
        cli_cmd = [
            "mbs-manager", "build_module_locally",
            "--set-stream", "platform:f28",
            "--file", staged_data_filename("testmodule-local-build.yaml")
        ]

        def main_side_effect(module_build_ids):
            build = db_session.query(models.ModuleBuild).filter(
                models.ModuleBuild.name == "testmodule-local-build"
            ).first()
            build.state = models.BUILD_STATES["failed"]
            db_session.commit()

        # We don't run consumer actually, but it could be patched to mark some
        # module build failed for test purpose.

        with patch("module_build_service.scheduler.local.main",
                   side_effect=main_side_effect):
            with pytest.raises(RuntimeError, match="Module build failed"):
                self._run_manager_wrapper(cli_cmd)
