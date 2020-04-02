# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_reuse_all_components(pkg_util, scenario, repo, koji):
    """Rebuild the test module again, without changing any of the components with:

    `fedpkg module-build -w --optional rebuild_strategy=only-changed`

    Checks:
    * Verify that all the components are reused from the first build.
    * Verify that module-build-macros is not built in the second build.
    """
    repo.bump()
    builds = pkg_util.run(
        "--watch",
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )
    assert len(builds) == 1

    build = builds[0]
    task_ids = build.component_task_ids()
    task_ids.pop("module-build-macros")

    repo.bump()
    builds = pkg_util.run(
        "-w",
        "--optional",
        "rebuild_strategy=only-changed",
        reuse=scenario.get("build_id_reused"))

    assert len(builds) == 1
    build = builds[0]
    reused_task_ids = build.component_task_ids()

    assert not build.components(package="module-build-macros")
    assert task_ids == reused_task_ids
