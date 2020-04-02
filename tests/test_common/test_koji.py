# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import mock

from module_build_service.common.koji import get_session


@mock.patch("koji.ClientSession")
def test_get_anonymous_session(mock_session):
    mbs_config = mock.Mock(koji_profile="koji", koji_config="conf/koji.conf")
    session = get_session(mbs_config, login=False)
    assert mock_session.return_value == session
    assert mock_session.return_value.krb_login.assert_not_called
