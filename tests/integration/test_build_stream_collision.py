# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_build_stream_collision(pkg_util, scenario, repo, koji, mbs):
    """Build module which contains streams collision.

    Checks:
    * Verify that build.log contains info about conflict.
    * Verify that correct version is used in macro specfile.
    """
    error_msg = "Stream for collision is not available."
    assert mbs.get_builds(module=scenario['conflicting_module'],
                          stream=scenario['conflicting_stream']), error_msg
    repo.bump()
    builds = pkg_util.run(
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )
    pkg_util.watch(builds)
    build = builds[0]
    buildlog = koji.get_build_log(build.components()[0], "build.log")

    error_msg = "Conflict module was not found in build.log"
    assert f"Conflicts: {scenario['conflicting_module']}" in buildlog, error_msg

    spec = koji.get_macro_specfile(build)
    error_msg = "Expected version not found in macro specfile."
    assert f"Conflicts: {scenario['conflicting_module']} " \
           f"= 0:{scenario['expected_version']}" in spec, error_msg
