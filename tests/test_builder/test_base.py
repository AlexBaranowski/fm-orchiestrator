# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import mock
from mock import patch

import module_build_service.builder
from module_build_service.builder import GenericBuilder
import module_build_service.common.models
import module_build_service.resolver
from module_build_service.scheduler.db_session import db_session
from tests import init_data


class TestGenericBuilder:
    def setup_method(self, test_method):
        init_data(1)

    @patch("module_build_service.resolver.DBResolver")
    @patch("module_build_service.builder.base.GenericResolver")
    def test_default_buildroot_groups_cache(self, generic_resolver, resolver):
        mbs_groups = {"buildroot": [], "srpm-buildroot": []}

        resolver = mock.MagicMock()
        resolver.backend = "mbs"
        resolver.resolve_profiles.return_value = mbs_groups

        expected_groups = {"build": [], "srpm-build": []}

        module = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 1)

        generic_resolver.create.return_value = resolver
        # Call default_buildroot_groups, the result should be cached.
        ret = GenericBuilder.default_buildroot_groups(db_session, module)
        assert ret == expected_groups
        resolver.resolve_profiles.assert_called_once()
        resolver.resolve_profiles.reset_mock()

        # Now try calling it again to verify resolve_profiles is not called,
        # because it is cached.
        generic_resolver.create.return_value = resolver
        ret = GenericBuilder.default_buildroot_groups(db_session, module)
        assert ret == expected_groups
        resolver.resolve_profiles.assert_not_called()
        resolver.resolve_profiles.reset_mock()

        # And now try clearing the cache and call it again.
        generic_resolver.create.return_value = resolver
        GenericBuilder.clear_cache(module)
        ret = GenericBuilder.default_buildroot_groups(db_session, module)
        assert ret == expected_groups
        resolver.resolve_profiles.assert_called_once()

    def test_get_build_weights(self):
        weights = GenericBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 1.5, "apr": 1.5}
