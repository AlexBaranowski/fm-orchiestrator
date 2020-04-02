# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
Allows MBS to run behind reverse proxy and to ensure redirects work with https.

WSGI Middleware!!

Source: http://flask.pocoo.org/snippets/35/ by Peter Hansen
"""
from __future__ import absolute_import


class ReverseProxy(object):
    """Wrap the application in this middleware and configure the
    front-end server to add these headers, to let you quietly bind
    this to a URL other than / and to an HTTP scheme that is
    different than what is used locally.

    :param app: the WSGI application
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        script_name = environ.get("HTTP_X_SCRIPT_NAME", "")
        if script_name:
            environ["SCRIPT_NAME"] = script_name
            path_info = environ["PATH_INFO"]
            if path_info.startswith(script_name):
                environ["PATH_INFO"] = path_info[len(script_name):]

        server = environ.get("HTTP_X_FORWARDED_HOST", "")
        if server:
            environ["HTTP_HOST"] = server

        scheme = environ.get("HTTP_X_SCHEME", "")
        if scheme:
            environ["wsgi.url_scheme"] = scheme
        return self.app(environ, start_response)
