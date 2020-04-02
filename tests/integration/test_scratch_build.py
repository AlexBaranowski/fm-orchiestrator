# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_scratch_build(pkg_util, scenario, repo, koji):
    """
    Run a scratch build with "rebuild_strategy=all".

    Check that:
    * the module build is done with the correct components
    * the module build completes in the "done" state
      (as opposed to the "ready" state)
    * no content generator builds are created in Koji
    """
    builds = pkg_util.run(
        "--scratch",
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )

    assert len(builds) == 1
    pkg_util.watch(builds)
    build = builds[0]

    assert build.state_name == "done"
    assert sorted(build.component_names(state="COMPLETE")) == sorted(
        repo.components + ["module-build-macros"]
    )

    cg_build = koji.get_build(build.nvr())
    cg_devel_build = koji.get_build(build.nvr(name_suffix="-devel"))
    assert not (cg_build or cg_devel_build)
