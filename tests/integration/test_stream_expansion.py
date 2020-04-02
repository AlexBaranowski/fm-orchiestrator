# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT


def test_stream_expansion(pkg_util, scenario, repo, koji):
    """

    Submit the testmodule2 build with `rhpkg-stage module-build`
        The produced builds can be cancelled after the test cases have been verified to save on
        resources and time

    Checks:
     * Verify two module builds were generated from this build submission

    """
    repo.bump()
    builds = pkg_util.run()

    assert len(builds) == 2
    for build in builds:
        pkg_util.cancel(build)
