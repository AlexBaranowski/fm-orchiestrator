# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import requests
from requests.packages.urllib3.util.retry import Retry


def get_requests_session(auth=False):
    """
    Create a requests session with retries configured.

    :return: the configured requests session
    :rtype: requests.Session
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
    )
    retry.BACKOFF_MAX = 2
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# TODO: Use this instead of requests.get directly throughout the codebase
requests_session = get_requests_session()
