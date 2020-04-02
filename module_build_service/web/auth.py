# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Auth system based on the client certificate and FAS account"""

from __future__ import absolute_import
import json
import ssl

from dogpile.cache import make_region
from flask import g
import requests

from module_build_service import app
from module_build_service.common import conf, log
from module_build_service.common.errors import Unauthorized, Forbidden


try:
    import ldap3
except ImportError:
    log.warning("ldap3 import not found.  ldap/krb disabled.")


client_secrets = None
region = make_region().configure("dogpile.cache.memory")


def _json_loads(content):
    if not isinstance(content, str):
        content = content.decode("utf-8")
    return json.loads(content)


def _load_secrets():
    global client_secrets
    if client_secrets:
        return

    if "OIDC_CLIENT_SECRETS" not in app.config:
        raise Forbidden("OIDC_CLIENT_SECRETS must be set in server config.")

    secrets = _json_loads(open(app.config["OIDC_CLIENT_SECRETS"], "r").read())
    client_secrets = list(secrets.values())[0]


def _get_token_info(token):
    """
    Asks the token_introspection_uri for the validity of a token.
    """
    if not client_secrets:
        return None

    request = {
        "token": token,
        "token_type_hint": "Bearer",
        "client_id": client_secrets["client_id"],
        "client_secret": client_secrets["client_secret"],
    }
    headers = {"Content-type": "application/x-www-form-urlencoded"}

    resp = requests.post(client_secrets["token_introspection_uri"], data=request, headers=headers)
    return resp.json()


def _get_user_info(token):
    """
    Asks the userinfo_uri for more information on a user.
    """
    if not client_secrets:
        return None

    headers = {"authorization": "Bearer " + token}
    resp = requests.get(client_secrets["userinfo_uri"], headers=headers)
    return resp.json()


def get_user_oidc(request):
    """
    Returns the client's username and groups based on the OIDC token provided.
    """
    _load_secrets()

    if "authorization" not in request.headers:
        raise Unauthorized("No 'authorization' header found.")

    header = request.headers["authorization"].strip()
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise Unauthorized("Authorization headers must start with %r" % prefix)

    token = header[len(prefix):].strip()
    try:
        data = _get_token_info(token)
    except Exception as e:
        error = "Cannot verify OIDC token: %s" % str(e)
        log.exception(error)
        raise Exception(error)

    if not data or "active" not in data or not data["active"]:
        raise Unauthorized("OIDC token invalid or expired.")

    if "OIDC_REQUIRED_SCOPE" not in app.config:
        raise Forbidden("OIDC_REQUIRED_SCOPE must be set in server config.")

    presented_scopes = data["scope"].split(" ")
    required_scopes = [
        "openid",
        "https://id.fedoraproject.org/scope/groups",
        app.config["OIDC_REQUIRED_SCOPE"],
    ]
    for scope in required_scopes:
        if scope not in presented_scopes:
            raise Unauthorized("Required OIDC scope %r not present: %r" % (scope, presented_scopes))

    try:
        extended_data = _get_user_info(token)
    except Exception:
        error = "OpenIDC auth error: Cannot determine the user's groups"
        log.exception(error)
        raise Unauthorized(error)

    username = data["username"]
    # If the user is part of the whitelist, then the group membership check is skipped
    if username in conf.allowed_users:
        groups = set()
    else:
        try:
            groups = set(extended_data["groups"])
        except Exception:
            error = "Could not find groups in UserInfo from OIDC"
            log.exception("%s (extended_data: %s)", error, extended_data)
            raise Unauthorized(error)

    return username, groups


def get_user_kerberos(request):
    remote_name = request.environ.get("REMOTE_USER")
    if not remote_name:
        # When Kerberos authentication is enabled, MBS expects the
        # authentication is done by a specific Apache module which sets
        # REMOTE_USER properly.
        raise Unauthorized("No REMOTE_USER is set.")

    try:
        username, realm = remote_name.split("@")
    except ValueError:
        raise Unauthorized("Value of REMOTE_NAME is not in format username@REALM")

    # Currently, MBS does not handle the realm to authorize user. Just keep it
    # here for any possible further use.

    # If the user is part of the whitelist, then the group membership check is skipped
    if username in conf.allowed_users:
        groups = []
    else:
        groups = get_ldap_group_membership(username)
    return username, set(groups)


@region.cache_on_arguments()
def get_ldap_group_membership(uid):
    """ Small wrapper on getting the group membership so that we can use caching
    :param uid: a string of the uid of the user
    :return: a list of groups the user is a member of
    """
    ldap_con = Ldap()
    return ldap_con.get_user_membership(uid)


class Ldap(object):
    """ A class that handles LDAP connections and queries
    """

    connection = None
    base_dn = None

    def __init__(self):
        if not conf.ldap_uri:
            raise Forbidden("LDAP_URI must be set in server config.")
        if conf.ldap_groups_dn:
            self.base_dn = conf.ldap_groups_dn
        else:
            raise Forbidden("LDAP_GROUPS_DN must be set in server config.")

        if conf.ldap_uri.startswith("ldaps://"):
            tls = ldap3.Tls(
                ca_certs_file="/etc/pki/tls/certs/ca-bundle.crt", validate=ssl.CERT_REQUIRED)
            server = ldap3.Server(conf.ldap_uri, use_ssl=True, tls=tls)
        else:
            server = ldap3.Server(conf.ldap_uri)
        self.connection = ldap3.Connection(server)
        try:
            self.connection.open()
        except ldap3.core.exceptions.LDAPSocketOpenError as error:
            log.error(
                'The connection to "{0}" failed. The following error was raised: {1}'.format(
                    conf.ldap_uri, str(error)))
            raise Forbidden(
                "The connection to the LDAP server failed. Group membership couldn't be obtained.")

    def get_user_membership(self, uid):
        """ Gets the group membership of a user
        :param uid: a string of the uid of the user
        :return: a list of common names of the posixGroups the user is a member of
        """
        ldap_filter = "(memberUid={0})".format(uid)
        # Only get the groups in the base container/OU
        self.connection.search(
            self.base_dn, ldap_filter, search_scope=ldap3.LEVEL, attributes=["cn"])
        groups = self.connection.response
        try:
            return [group["attributes"]["cn"][0] for group in groups]
        except KeyError:
            log.exception(
                "The LDAP groups could not be determined based on the search results "
                'of "{0}"'.format(str(groups)))
            return []


def get_user(request):
    """ Authenticates the user and returns the username and group name
    :param request: a Flask request
    :return: a tuple with a string representing the user name and a set with the user's group
    membership such as ('mprahl', {'factory2', 'devel'})
    """
    if conf.no_auth is True:
        log.debug("Authorization is disabled.")
        return "anonymous", {"packager"}

    if "user" not in g and "groups" not in g:
        get_user_func_name = "get_user_{0}".format(conf.auth_method)
        get_user_func = globals().get(get_user_func_name)
        if not get_user_func:
            raise RuntimeError('The function "{0}" is not implemented'.format(get_user_func_name))
        g.user, g.groups = get_user_func(request)
    return g.user, g.groups
