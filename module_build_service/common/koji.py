# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import os

import koji
import munch
import six.moves.xmlrpc_client as xmlrpclib

from module_build_service.common import log
from module_build_service.common.retry import retry
from module_build_service.common.errors import ProgrammingError


def koji_multicall_map(koji_session, koji_session_fnc, list_of_args=None, list_of_kwargs=None):
    """
    Calls the `koji_session_fnc` using Koji multicall feature N times based on the list of
    arguments passed in `list_of_args` and `list_of_kwargs`.
    Returns list of responses sorted the same way as input args/kwargs. In case of error,
    the error message is logged and None is returned.

    For example to get the package ids of "httpd" and "apr" packages:
        ids = koji_multicall_map(session, session.getPackageID, ["httpd", "apr"])
        # ids is now [280, 632]

    :param KojiSessions koji_session: KojiSession to use for multicall.
    :param object koji_session_fnc: Python object representing the KojiSession method to call.
    :param list list_of_args: List of args which are passed to each call of koji_session_fnc.
    :param list list_of_kwargs: List of kwargs which are passed to each call of koji_session_fnc.
    """
    if list_of_args is None and list_of_kwargs is None:
        raise ProgrammingError("One of list_of_args or list_of_kwargs must be set.")

    if (
        type(list_of_args) not in [type(None), list]
        or type(list_of_kwargs) not in [type(None), list]
    ):
        raise ProgrammingError("list_of_args and list_of_kwargs must be list or None.")

    if list_of_kwargs is None:
        list_of_kwargs = [{}] * len(list_of_args)
    if list_of_args is None:
        list_of_args = [[]] * len(list_of_kwargs)

    if len(list_of_args) != len(list_of_kwargs):
        raise ProgrammingError("Length of list_of_args and list_of_kwargs must be the same.")

    koji_session.multicall = True
    for args, kwargs in zip(list_of_args, list_of_kwargs):
        if type(args) != list:
            args = [args]
        if type(kwargs) != dict:
            raise ProgrammingError("Every item in list_of_kwargs must be a dict")
        koji_session_fnc(*args, **kwargs)

    try:
        responses = koji_session.multiCall(strict=True)
    except Exception:
        log.exception(
            "Exception raised for multicall of method %r with args %r, %r:",
            koji_session_fnc, args, kwargs,
        )
        return None

    if not responses:
        log.error("Koji did not return response for multicall of %r", koji_session_fnc)
        return None
    if type(responses) != list:
        log.error(
            "Fault element was returned for multicall of method %r: %r", koji_session_fnc, responses
        )
        return None

    results = []

    # For the response specification, see
    # https://web.archive.org/web/20060624230303/http://www.xmlrpc.com/discuss/msgReader$1208?mode=topic
    # Relevant part of this:
    # Multicall returns an array of responses. There will be one response for each call in
    # the original array. The result will either be a one-item array containing the result value,
    # or a struct of the form found inside the standard <fault> element.
    for response, args, kwargs in zip(responses, list_of_args, list_of_kwargs):
        if type(response) == list:
            if not response:
                log.error(
                    "Empty list returned for multicall of method %r with args %r, %r",
                    koji_session_fnc, args, kwargs
                )
                return None
            results.append(response[0])
        else:
            log.error(
                "Unexpected data returned for multicall of method %r with args %r, %r: %r",
                koji_session_fnc, args, kwargs, response
            )
            return None

    return results


@retry(wait_on=(xmlrpclib.ProtocolError, koji.GenericError))
def koji_retrying_multicall_map(*args, **kwargs):
    """
    Retrying version of koji_multicall_map. This tries to retry the Koji call
    in case of koji.GenericError or xmlrpclib.ProtocolError.

    Please refer to koji_multicall_map for further specification of arguments.
    """
    return koji_multicall_map(*args, **kwargs)


@retry(wait_on=(xmlrpclib.ProtocolError, koji.GenericError))
def get_session(config, login=True):
    """Create and return a koji.ClientSession object

    :param config: the config object returned from :meth:`init_config`.
    :type config: :class:`Config`
    :param bool login: whether to log into the session. To login if True
        is passed, otherwise not to log into session.
    :return: the Koji session object.
    :rtype: :class:`koji.ClientSession`
    """
    koji_config = munch.Munch(
        koji.read_config(profile_name=config.koji_profile, user_config=config.koji_config))
    # Timeout after 10 minutes.  The default is 12 hours.
    koji_config["timeout"] = 60 * 10

    address = koji_config.server
    log.info("Connecting to koji %r.", address)
    koji_session = koji.ClientSession(address, opts=koji_config)

    if not login:
        return koji_session

    authtype = koji_config.authtype
    log.info("Authenticate session with %r.", authtype)
    if authtype == "kerberos":
        try:
            import krbV
            # We want to create a context per thread to avoid Kerberos cache corruption
            ctx = krbV.Context()
        except ImportError:
            # If no krbV, we can assume GSSAPI auth is available
            ctx = None
        keytab = getattr(config, "krb_keytab", None)
        principal = getattr(config, "krb_principal", None)
        if not keytab and principal:
            raise ValueError(
                "The Kerberos keytab and principal aren't set for Koji authentication")
        log.debug("  keytab: %r, principal: %r" % (keytab, principal))
        # We want to use the thread keyring for the ccache to ensure we have one cache per
        # thread to avoid Kerberos cache corruption
        ccache = "KEYRING:thread:mbs"
        koji_session.krb_login(principal=principal, keytab=keytab, ctx=ctx, ccache=ccache)
    elif authtype == "ssl":
        koji_session.ssl_login(
            os.path.expanduser(koji_config.cert), None, os.path.expanduser(koji_config.serverca)
        )
    else:
        raise ValueError("Unrecognized koji authtype %r" % authtype)

    return koji_session
