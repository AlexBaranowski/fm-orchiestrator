# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_normal_build(pkg_util, scenario, repo, koji):
    """
    Run build with `rhpkg-stage module-build --optional rebuild_strategy=all`

    Checks:
    * Check that MBS will submit all the component builds
    * Check that buildorder of components is respected
    * Check that MBS will create two content generator builds representing the module:
        - [module]
        - [module]-devel
    * Check that MBS changed the buildrequired platform to have a suffix of “z”
        if a Platform stream is representing a GA RHEL release.
    """
    repo.bump()
    builds = pkg_util.run(
        "--optional",
        "rebuild_strategy=all",
        reuse=scenario.get("build_id"),
    )
    assert len(builds) == 1
    pkg_util.watch(builds)
    build = builds[0]

    assert sorted(build.component_names()) == sorted(repo.components + ["module-build-macros"])

    expected_buildorder = scenario["buildorder"]
    expected_buildorder = [set(batch) for batch in expected_buildorder]
    actual_buildorder = build.batches()
    assert actual_buildorder == expected_buildorder

    cg_build = koji.get_build(build.nvr())
    cg_devel_build = koji.get_build(build.nvr(name_suffix="-devel"))
    assert cg_build and cg_devel_build
    assert cg_devel_build['extra']['typeinfo']['module']['module_build_service_id'] == int(build.id)

    modulemd = koji.get_modulemd(cg_build)
    actual_platforms = modulemd["data"]["dependencies"][0]["buildrequires"]["platform"]
    expected_platforms = repo.platform
    platform_ga = scenario.get("platform_is_ga")
    if platform_ga:
        expected_platforms = [f"{pf}.z" for pf in expected_platforms]
    assert expected_platforms == actual_platforms
