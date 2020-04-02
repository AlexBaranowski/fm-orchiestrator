# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" Defines custom exceptions and error handling functions """

from __future__ import absolute_import


class ValidationError(ValueError):
    pass


class Unauthorized(ValueError):
    pass


class Forbidden(ValueError):
    pass


class UnprocessableEntity(ValueError):
    pass


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class ProgrammingError(ValueError):
    pass


class StreamAmbigous(ValueError):
    pass


class GreenwaveError(RuntimeError):
    pass


class IgnoreMessage(Exception):
    """Raise if message received from message bus should be ignored"""
