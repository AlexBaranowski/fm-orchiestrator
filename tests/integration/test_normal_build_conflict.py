# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_normal_build_conflict(pkg_util, scenario, repo, koji, mbs):
    """Build module which contains conflict component in buildroot.

    Checks:
    * Verify that component contains conflict package
    * Verify that build.log contains info about conflict.
    """
    conflicting_build = mbs.get_builds(module=scenario["buildrequires"]["module"],
                                       stream=scenario["buildrequires"]["branch"],
                                       order_desc_by='version')
    components = conflicting_build[0].component_names()
    error_msg = "Stream does not contain expected package."
    assert scenario["conflicting_package"] in \
        components, error_msg

    repo.bump()
    builds = pkg_util.run(
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )
    pkg_util.watch(builds)
    build = builds[0]
    buildlog = koji.get_build_log(build.components()[0], "build.log")
    error_msg = "Conflict package was not found in build.log"
    assert f"Conflicts: {scenario['conflicting_package']}" in buildlog, error_msg
