# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import functools
import time

from module_build_service.common import conf, log


def retry(timeout=conf.net_timeout, interval=conf.net_retry_interval, wait_on=Exception):
    """ A decorator that allows to retry a section of code...
    ...until success or timeout.
    """

    def wrapper(function):
        @functools.wraps(function)
        def inner(*args, **kwargs):
            start = time.time()
            while True:
                try:
                    return function(*args, **kwargs)
                except wait_on as e:
                    log.warning(
                        "Exception %r raised from %r.  Retry in %rs" % (e, function, interval)
                    )
                    time.sleep(interval)
                    if (time.time() - start) >= timeout:
                        raise  # This re-raises the last exception.

        return inner

    return wrapper
