# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import pkg_resources

from module_build_service.resolver.base import GenericResolver

# NOTE: if you are adding a new resolver to MBS please note that you also have to add
# a new resolver to your setup.py and update you egg-info
for entrypoint in pkg_resources.iter_entry_points("mbs.resolver_backends"):
    GenericResolver.register_backend_class(entrypoint.load())

if not GenericResolver.backends:
    raise ValueError("No resolver plugins are installed or available.")
