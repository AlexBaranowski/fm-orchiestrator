# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_failed_build(pkg_util, scenario, repo, koji):
    """
    Run the build with "rebuild_strategy=all".

    Check that:
      * Check that the module build eventually fails
      * Check that any other components in the same batch as the failed component are
        cancelled, if not completed.
    """
    repo.bump()
    builds = pkg_util.run(
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )
    assert len(builds) == 1
    build = builds[0]
    pkg_util.watch(builds)

    assert build.state_name == "failed"
    batch = scenario["batch"]
    failing_components = scenario["failing_components"]
    canceled_components = scenario["canceled_components"]
    assert sorted(failing_components) == sorted(build.component_names(state="FAILED", batch=batch))
    assert sorted(canceled_components) == sorted(
        build.component_names(state="COMPLETE", batch=batch)
        + build.component_names(state="CANCELED", batch=batch)
    )
