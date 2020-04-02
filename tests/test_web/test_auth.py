# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from os import path

import mock
from mock import patch, PropertyMock, Mock
import pytest
import requests

from module_build_service import app
import module_build_service.common.config as mbs_config
import module_build_service.common.errors
import module_build_service.web.auth


class TestAuthModule:
    def test_get_user_no_token(self):
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict(
            "module_build_service.app.config",
            {"OIDC_CLIENT_SECRETS": client_secrets, "OIDC_REQUIRED_SCOPE": "mbs-scope"},
        ):
            request = mock.MagicMock()
            request.cookies.return_value = {}

            with pytest.raises(module_build_service.common.errors.Unauthorized) as cm:
                with app.app_context():
                    module_build_service.web.auth.get_user(request)
                assert str(cm.value) == "No 'authorization' header found."

    @patch("module_build_service.web.auth._get_token_info")
    @patch("module_build_service.web.auth._get_user_info")
    def test_get_user_failure(self, get_user_info, get_token_info):
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict(
            "module_build_service.app.config",
            {"OIDC_CLIENT_SECRETS": client_secrets, "OIDC_REQUIRED_SCOPE": "mbs-scope"},
        ):
            # https://www.youtube.com/watch?v=G-LtddOgUCE
            name = "Joey Jo Jo Junior Shabadoo"
            mocked_get_token_info = {
                "active": False,
                "username": name,
                "scope": "openid https://id.fedoraproject.org/scope/groups mbs-scope"
            }
            get_token_info.return_value = mocked_get_token_info

            get_user_info.return_value = {"groups": ["group"]}

            headers = {"authorization": "Bearer foobar"}
            request = mock.MagicMock()
            request.headers.return_value = mock.MagicMock(spec_set=dict)
            request.headers.__getitem__.side_effect = headers.__getitem__
            request.headers.__setitem__.side_effect = headers.__setitem__
            request.headers.__contains__.side_effect = headers.__contains__

            with pytest.raises(module_build_service.common.errors.Unauthorized) as cm:
                with app.app_context():
                    module_build_service.web.auth.get_user(request)
                assert str(cm.value) == "OIDC token invalid or expired."

    @patch("module_build_service.web.auth._get_token_info")
    @patch("module_build_service.web.auth._get_user_info")
    def test_get_user_not_in_groups(self, get_user_info, get_token_info):
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict(
            "module_build_service.app.config",
            {"OIDC_CLIENT_SECRETS": client_secrets, "OIDC_REQUIRED_SCOPE": "mbs-scope"},
        ):
            # https://www.youtube.com/watch?v=G-LtddOgUCE
            name = "Joey Jo Jo Junior Shabadoo"
            mocked_get_token_info = {
                "active": True,
                "username": name,
                "scope": "openid https://id.fedoraproject.org/scope/groups mbs-scope"
            }
            get_token_info.return_value = mocked_get_token_info

            get_user_info.side_effect = requests.Timeout("It happens...")

            headers = {"authorization": "Bearer foobar"}
            request = mock.MagicMock()
            request.headers.return_value = mock.MagicMock(spec_set=dict)
            request.headers.__getitem__.side_effect = headers.__getitem__
            request.headers.__setitem__.side_effect = headers.__setitem__
            request.headers.__contains__.side_effect = headers.__contains__

            with pytest.raises(module_build_service.common.errors.Unauthorized) as cm:
                with app.app_context():
                    module_build_service.web.auth.get_user(request)
                assert str(cm.value) == "OpenIDC auth error: Cannot determine the user's groups"

    @pytest.mark.parametrize("allowed_users", (set(), {"Joey Jo Jo Junior Shabadoo"}))
    @patch.object(mbs_config.Config, "allowed_users", new_callable=PropertyMock)
    @patch("module_build_service.web.auth._get_token_info")
    @patch("module_build_service.web.auth._get_user_info")
    def test_get_user_good(self, get_user_info, get_token_info, m_allowed_users, allowed_users):
        m_allowed_users.return_value = allowed_users
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict(
            "module_build_service.app.config",
            {"OIDC_CLIENT_SECRETS": client_secrets, "OIDC_REQUIRED_SCOPE": "mbs-scope"},
        ):
            # https://www.youtube.com/watch?v=G-LtddOgUCE
            name = "Joey Jo Jo Junior Shabadoo"
            mocked_get_token_info = {
                "active": True,
                "username": name,
                "scope": ("openid https://id.fedoraproject.org/scope/groups mbs-scope"),
            }
            get_token_info.return_value = mocked_get_token_info

            get_user_info.return_value = {"groups": ["group"]}

            headers = {"authorization": "Bearer foobar"}
            request = mock.MagicMock()
            request.headers.return_value = mock.MagicMock(spec_set=dict)
            request.headers.__getitem__.side_effect = headers.__getitem__
            request.headers.__setitem__.side_effect = headers.__setitem__
            request.headers.__contains__.side_effect = headers.__contains__

            with app.app_context():
                username, groups = module_build_service.web.auth.get_user(request)
                username_second_call, groups_second_call = module_build_service.web.auth.get_user(
                    request)
            assert username == name
            if allowed_users:
                assert groups == set()
            else:
                assert groups == set(get_user_info.return_value["groups"])

            # Test the real auth method has been called just once.
            get_user_info.assert_called_once()
            assert username_second_call == username
            assert groups_second_call == groups

    @patch.object(mbs_config.Config, "no_auth", new_callable=PropertyMock, return_value=True)
    def test_disable_authentication(self, conf_no_auth):
        request = mock.MagicMock()
        username, groups = module_build_service.web.auth.get_user(request)
        assert username == "anonymous"
        assert groups == {"packager"}

    @patch("module_build_service.web.auth.client_secrets", None)
    def test_misconfiguring_oidc_client_secrets_should_be_failed(self):
        request = mock.MagicMock()
        with pytest.raises(module_build_service.common.errors.Forbidden) as cm:
            with app.app_context():
                module_build_service.web.auth.get_user(request)
            assert str(cm.value) == "OIDC_CLIENT_SECRETS must be set in server config."

    @patch("module_build_service.web.auth._get_token_info")
    @patch("module_build_service.web.auth._get_user_info")
    def test_get_required_scope_not_present(self, get_user_info, get_token_info):
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict(
            "module_build_service.app.config",
            {"OIDC_CLIENT_SECRETS": client_secrets, "OIDC_REQUIRED_SCOPE": "mbs-scope"},
        ):
            # https://www.youtube.com/watch?v=G-LtddOgUCE
            name = "Joey Jo Jo Junior Shabadoo"
            mocked_get_token_info = {
                "active": True,
                "username": name,
                "scope": "openid https://id.fedoraproject.org/scope/groups",
            }
            get_token_info.return_value = mocked_get_token_info

            get_user_info.return_value = {"groups": ["group"]}

            headers = {"authorization": "Bearer foobar"}
            request = mock.MagicMock()
            request.headers.return_value = mock.MagicMock(spec_set=dict)
            request.headers.__getitem__.side_effect = headers.__getitem__
            request.headers.__setitem__.side_effect = headers.__setitem__
            request.headers.__contains__.side_effect = headers.__contains__

            with pytest.raises(module_build_service.common.errors.Unauthorized) as cm:
                with app.app_context():
                    module_build_service.web.auth.get_user(request)
                assert str(cm.value) == (
                    "Required OIDC scope 'mbs-scope' not present: "
                    "['openid', 'https://id.fedoraproject.org/scope/groups']"
                )

    @patch("module_build_service.web.auth._get_token_info")
    @patch("module_build_service.web.auth._get_user_info")
    def test_get_required_scope_not_set_in_cfg(self, get_user_info, get_token_info):
        base_dir = path.abspath(path.dirname(__file__))
        client_secrets = path.join(base_dir, "client_secrets.json")
        with patch.dict("module_build_service.app.config", {"OIDC_CLIENT_SECRETS": client_secrets}):
            # https://www.youtube.com/watch?v=G-LtddOgUCE
            name = "Joey Jo Jo Junior Shabadoo"
            mocked_get_token_info = {
                "active": True,
                "username": name,
                "scope": "openid https://id.fedoraproject.org/scope/groups",
            }
            get_token_info.return_value = mocked_get_token_info

            get_user_info.return_value = {"groups": ["group"]}

            headers = {"authorization": "Bearer foobar"}
            request = mock.MagicMock()
            request.headers.return_value = mock.MagicMock(spec_set=dict)
            request.headers.__getitem__.side_effect = headers.__getitem__
            request.headers.__setitem__.side_effect = headers.__setitem__
            request.headers.__contains__.side_effect = headers.__contains__

            with pytest.raises(module_build_service.common.errors.Forbidden) as cm:
                with app.app_context():
                    module_build_service.web.auth.get_user(request)
                assert str(cm.value) == "OIDC_REQUIRED_SCOPE must be set in server config."

    @pytest.mark.parametrize("remote_name", ["", None, "someone"])
    def test_get_user_kerberos_unauthorized(self, remote_name):
        request = Mock()
        request.environ.get.return_value = remote_name

        with pytest.raises(module_build_service.common.errors.Unauthorized):
            module_build_service.web.auth.get_user_kerberos(request)

    @patch.object(module_build_service.web.auth.conf, "allowed_users", new=["someone", "somebody"])
    def test_get_user_kerberos_user_is_in_allowed_users_group(self):
        request = Mock()
        request.environ.get.return_value = "someone@realm"

        username, groups = module_build_service.web.auth.get_user_kerberos(request)
        assert "someone" == username
        assert set() == groups

    @patch.object(module_build_service.web.auth.conf, "allowed_users", new=["someone", "somebody"])
    @patch(
        "module_build_service.web.auth.get_ldap_group_membership",
        return_value=["group1", "group2"],
    )
    def test_get_user_kerberos_user_is_not_in_allowed_users_group(self, get_ldap_group_membership):
        request = Mock()
        request.environ.get.return_value = "x-man@realm"

        username, groups = module_build_service.web.auth.get_user_kerberos(request)
        assert "x-man" == username
        assert {"group1", "group2"} == groups
