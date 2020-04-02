# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
import pytest
from sh import ErrorReturnCode


def test_buildrequire_invalid_module(pkg_util, scenario, repo, koji):
    """
    Run a build with with an invalid 'build_require' module.
    I.e.: required module is picked in such a way,
    that it is not tagged according to the the base module (platform) requirements,
    see platform's modulemd file and its 'koji_tag_with_modules' attribute
    (e.g.: platform: el-8.1.0 --> rhel-8.1.0-modules-build).

    Koji resolver is expected to not be able to satisfy this build requirement
    and hence fail the build.

    Assert that:
    * the module build hasn't been accepted by MBS:
      rhpkg utility returns something else than 0
    * "Cannot find any module build..." found on STDERR

    If assert fails:
    * cancel all triggered module builds.

    """

    repo.bump()

    expected_error = "Cannot find any module builds"
    with pytest.raises(ErrorReturnCode) as excinfo:
        # Override 'baked' (_err=sys.stderr) stderr redirect:
        #   Here we are fine with what plain sh.Command gives us
        #   (otherwise ErrorReturnCode.stderr is incomplete).
        builds = pkg_util.run("--optional", "rebuild_strategy=all", _err=None)
        try:
            for build in builds:
                print("Canceling module-build {}...".format(build.id))
                pkg_util.cancel(build)
        except ErrorReturnCode:
            # Do nothing, this is just a clean-up of accidentally started builds
            # in case that the test-case fails
            pass
    assert expected_error in excinfo.value.stderr.decode("utf-8")
