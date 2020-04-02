# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import mock

from module_build_service.common.submit import _is_eol_in_pdc


@mock.patch("module_build_service.common.submit.requests")
def test_pdc_eol_check(requests):
    """ Push mock pdc responses through the eol check function. """

    response = mock.Mock()
    response.json.return_value = {
        "results": [{
            "id": 347907,
            "global_component": "mariadb",
            "name": "10.1",
            "slas": [{"id": 694207, "sla": "security_fixes", "eol": "2019-12-01"}],
            "type": "module",
            "active": True,
            "critical_path": False,
        }]
    }
    requests.get.return_value = response

    is_eol = _is_eol_in_pdc("mariadb", "10.1")
    assert not is_eol

    response.json.return_value["results"][0]["active"] = False

    is_eol = _is_eol_in_pdc("mariadb", "10.1")
    assert is_eol
