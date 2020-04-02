# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

import utils


def test_reuse_components(pkg_util, test_env, scenario, repo, koji):
    """
    Bump the commit of one of the components that MBS uses.
    Bump the commit of the same testmodule that was mentioned in the preconditions.
    Submit a testmodule build again with `fedpkg module-build -w --optional
        rebuild_strategy=only-changed` (this is the default rebuild strategy).

    Checks:
    * Verify all the components were reused except for the component that had their commit change.
    * Verify that the component with the changed commit was rebuilt.
    """
    repo.bump()
    baseline_builds = pkg_util.run(
        "--watch",
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("baseline_build_id"),
    )
    assert len(baseline_builds) == 1

    baseline_build = baseline_builds[0]
    package = scenario.get("package")
    component = utils.Component(
        package,
        scenario.get("component_branch")
    )
    component.clone(test_env["packaging_utility"])
    component.bump()

    repo.bump()
    builds = pkg_util.run(
        "--watch",
        "--optional",
        "rebuild_strategy=only-changed",
        reuse=scenario.get("build_id"),
    )
    assert len(builds) == 1
    build = builds[0]
    comp_task_ids_base = baseline_build.component_task_ids()
    comp_task_ids = build.component_task_ids()
    comp_task_ids_base.pop('module-build-macros')
    comp_task_ids.pop('module-build-macros')
    changed_package_base_task_id = comp_task_ids_base.pop(package)
    changed_package_task_id = comp_task_ids.pop(package)
    assert changed_package_base_task_id != changed_package_task_id
    assert comp_task_ids == comp_task_ids_base

    assert build.components(package=package)[0]['state_name'] == 'COMPLETE'
    state_reason = build.components(package=package)[0]['state_reason']
    assert state_reason != "Reused component from previous module build"
