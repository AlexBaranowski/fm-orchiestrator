# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os.path

from module_build_service.common.config import conf


class TestConfig:
    def test_path_expanduser(self):
        test_dir = "~/modulebuild/builds"
        conf.mock_resultsdir = test_dir
        assert conf.mock_resultsdir == os.path.expanduser(test_dir)

        test_dir = "~/modulebuild/builds"
        conf.cache_dir = test_dir
        assert conf.cache_dir == os.path.expanduser(test_dir)
