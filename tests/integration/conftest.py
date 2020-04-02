# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

import os
import sys
import tempfile

import pytest
import sh
import yaml

import utils

our_sh = sh(_out=sys.stdout, _err=sys.stderr, _tee=True)
from our_sh import pushd, Command  # noqa


@pytest.fixture(scope="session")
def test_env():
    """Load test environment configuration

    :return: Test environment configuration.
    :rtype:  dict
    """
    config_file = os.getenv("MBS_TEST_CONFIG", "test.env.yaml")
    with open(config_file) as f:
        env = yaml.safe_load(f)
    return env


@pytest.fixture(scope="session")
def pkg_util(test_env):
    """Fixture to interact with the packaging utility

    :return: Packaging utility configured for the tests
    :rtype: object of utils.PackagingUtility
    """
    return utils.PackagingUtility(test_env["packaging_utility"], test_env["mbs_api"])


@pytest.fixture(scope="function")
def scenario(request, test_env):
    """Configuration data for the scenario

    Find out the name of the scenario (anything that follows "test_"),
    and return the corresponding configuration.

    This is a convenience fixture to serve as a shortcut to access
    scenario configuration.
    """
    scenario_name = request.function.__name__.split("test_", 1)[1]
    return test_env["testdata"][scenario_name]


@pytest.fixture(scope="function")
def repo(scenario, test_env):
    """Clone the module repo to be used by the scenario

    Get the module repo from the scenario configuration.

    Clone the repo in a temporary location and switch the current working
    directory into it.

    :param pytest.FixtureRequest request: request object giving access
        to the requesting test context
    :param pytest.fixture test_env: test environment fixture
    :return: repository object the tests can work with
    :rtype: utils.Repo
    """
    with tempfile.TemporaryDirectory() as tempdir:
        packaging_util = Command(test_env["packaging_utility"]).bake(
            _out=sys.stdout, _err=sys.stderr, _tee=True
        )
        args = [
            "--branch",
            scenario["branch"],
            f"modules/{scenario['module']}",
            tempdir,
        ]
        packaging_util("clone", *args)
        with pushd(tempdir):
            yield utils.Repo(scenario["module"])


@pytest.fixture(scope="session")
def koji(test_env):
    """Koji session for the instance MBS is configured to work with."""
    return utils.Koji(**test_env["koji"])


@pytest.fixture(scope="session")
def mbs(test_env):
    """MBS instance session."""
    return utils.MBS(test_env["mbs_api"])
