# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_no_components(pkg_util, scenario, repo, koji):
    """
    Submit the testmodule build with `fedpkg module-build`

    Checks:
    * Verify that no components were built when no components are defined in modulemd
    * Verify that the testmodule build succeeds

    """
    repo.bump()
    builds = pkg_util.run(reuse=scenario.get("build_id"))
    assert len(builds) == 1

    pkg_util.watch(builds)
    build = builds[0]

    assert build.state_name == "ready"
    assert not build.data["component_builds"]
