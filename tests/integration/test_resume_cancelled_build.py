# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

import time


def test_resume_cancelled_build(pkg_util, scenario, repo, koji):
    """
    Run the  build with "rebuild_strategy=all".
    Wait until the module-build-macros build is submitted to Koji.
    Cancel module build.
    Resume the module with "rhpkg-stage module-build -w".

    Check that:
      * Check that the testmodule had actually been cancelled
      * Check that the testmodule build succeeded

    """
    repo.bump()
    builds = pkg_util.run(
        "--optional",
        "rebuild_strategy=all",
    )

    assert len(builds) == 1
    build = builds[0]
    build.wait_for_koji_task_id(package="module-build-macros", batch=1)
    pkg_util.cancel(build)
    # Behave like a human: restarting the build too quickly would lead to an error.
    time.sleep(10)
    builds = pkg_util.run("--watch")
    assert len(builds) == 1
    build = builds[0]
    assert build.state_name == "ready"
    assert build.was_cancelled()
