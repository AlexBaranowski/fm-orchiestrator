# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from flask import request
from flask.json import dumps

from module_build_service import app


def jsonify(*args, **kwargs):
    # This is `flask.jsonify` version which supports Python list as an input.
    # We cannot use real `jsonify`, because it can handle Python lists as
    # input only since 0.11, but RHEL7 contains 0.10.1.
    # https://github.com/pallets/flask/commit/daceb3e3a028b4b408c4bbdbdef0047f1de3a7c9
    indent = None
    separators = (",", ":")

    if app.config["JSONIFY_PRETTYPRINT_REGULAR"] and not request.is_xhr:
        indent = 2
        separators = (", ", ": ")

    if args and kwargs:
        raise TypeError("jsonify() behavior undefined when passed both args and kwargs")
    elif len(args) == 1:  # single args are passed directly to dumps()
        data = args[0]
    elif args:  # convert multiple args into an array
        data = list(args)
    else:  # convert kwargs to a dict
        data = dict(kwargs)

    # Note that we add '\n' to end of response
    # (see https://github.com/mitsuhiko/flask/pull/1262)
    rv = app.response_class(
        (dumps(data, indent=indent, separators=separators), "\n"), mimetype="application/json")
    return rv
