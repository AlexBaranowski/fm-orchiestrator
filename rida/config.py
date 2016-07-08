# -*- coding: utf-8 -*-


# Copyright (c) 2016  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Petr Šabata <contyk@redhat.com>

"""Configuration handler functions."""

import os.path
import configparser
import json
from rida import logger

def from_file(filename=None):
    """Create the configuration instance from a file.

    The file name is optional and defaults to /etc/rida/rida.conf.

    :param str filename: The configuration file to load, optional.
    """
    if filename is None:
        filename = "/etc/rida/rida.conf"
    if not isinstance(filename, str):
        raise TypeError("The configuration filename must be a string.")
    if not os.path.isfile(filename):
        raise IOError("The configuration file doesn't exist.")
    cp = configparser.ConfigParser(allow_no_value=True)
    cp.read(filename)
    default = cp["DEFAULT"]
    conf = Config()
    conf.db = default.get("db")
    conf.system = default.get("system")
    conf.messaging = default.get("messaging")
    conf.pdc = default.get("pdc")
    conf.koji = default.get("koji")
    conf.scmurls = json.loads(default.get("scmurls"))
    conf.rpms_default_repository = default.get("rpms_default_repository")
    conf.rpms_allow_repository = default.getboolean("rpms_allow_repository")
    conf.rpms_default_cache = default.get("rpms_default_cache")
    conf.rpms_allow_cache = default.getboolean("rpms_allow_cache")

    conf.ssl_certificate_file = default.get("ssl_certificate_file")
    conf.ssl_certificate_key_file = default.get("ssl_certificate_key_file")
    conf.ssl_ca_certificate_file = default.get("ssl_ca_certificate_file")

    conf.pkgdb_api_url = default.get("pkgdb_api_url")

    conf.log_backend = default.get("log_backend")
    conf.log_file = default.get("log_file")
    conf.log_level = default.get("log_level")
    return conf

class Config(object):
    """Class representing the orchestrator configuration."""

    def __init__(self):
        """Initialize the Config object."""
        self._system = ""
        self._messaging = ""
        self._db = ""
        self._pdc = ""
        self._koji = ""
        self._rpms_default_repository = ""
        self._rpms_allow_repository = False
        self._rpms_default_cache = ""
        self._rpms_allow_cache = False
        self._ssl_certificate_file = ""
        self._ssl_certificate_key_file = ""
        self._ssl_ca_certificate_file = ""
        self._pkgdb_api_url = ""
        self._log_backend = ""
        self._log_file = ""
        self._log_level = 0

    @property
    def system(self):
        """The buildsystem to use."""
        return self._system

    @system.setter
    def system(self, s):
        s = str(s)
        if s not in ("koji"):
            raise ValueError("Unsupported buildsystem.")
        self._system = s

    @property
    def messaging(self):
        """The messaging system to use."""
        return self._messaging

    @messaging.setter
    def messaging(self, s):
        s = str(s)
        if s not in ("fedmsg"):
            raise ValueError("Unsupported messaging system.")
        self._messaging = s

    @property
    def db(self):
        """RDB URL."""
        return self._db

    @db.setter
    def db(self, s):
        self._db = str(s)

    @property
    def pdc(self):
        """PDC URL."""
        return self._pdc

    @pdc.setter
    def pdc(self, s):
        self._pdc = str(s)

    @property
    def koji(self):
        """Koji URL."""
        return self._koji

    @koji.setter
    def koji(self, s):
        self._koji = str(s)

    @property
    def scmurls(self):
        """Allowed SCM URLs."""
        return self._scmurls

    @scmurls.setter
    def scmurls(self, l):
        if not isinstance(l, list):
            raise TypeError("scmurls needs to be a list.")
        self._scmurls = [str(x) for x in l]

    @property
    def rpms_default_repository(self):
        return self._rpms_default_repository

    @rpms_default_repository.setter
    def rpms_default_repository(self, s):
        self._rpms_default_repository = str(s)

    @property
    def rpms_allow_repository(self):
        return self._rpms_allow_repository

    @rpms_allow_repository.setter
    def rpms_allow_repository(self, b):
        if not isinstance(b, bool):
            raise TypeError("rpms_allow_repository must be a bool.")
        self._rpms_allow_repository = b

    @property
    def rpms_default_cache(self):
        return self._rpms_default_cache

    @rpms_default_cache.setter
    def rpms_default_cache(self, s):
        self._rpms_default_cache = str(s)

    @property
    def rpms_allow_cache(self):
        return self._rpms_allow_cache

    @rpms_allow_cache.setter
    def rpms_allow_cache(self, b):
        if not isinstance(b, bool):
            raise TypeError("rpms_allow_cache must be a bool.")
        self._rpms_allow_cache = b

    @property
    def ssl_certificate_file(self):
        return self._ssl_certificate_file

    @ssl_certificate_file.setter
    def ssl_certificate_file(self, s):
        self._ssl_certificate_file = str(s)

    @property
    def ssl_ca_certificate_file(self):
        return self._ssl_ca_certificate_file

    @ssl_ca_certificate_file.setter
    def ssl_ca_certificate_file(self, s):
        self._ssl_ca_certificate_file = str(s)

    @property
    def ssl_certificate_key_file(self):
        return self._ssl_certificate_key_file

    @ssl_certificate_key_file.setter
    def ssl_certificate_key_file(self, s):
        self._ssl_certificate_key_file = str(s)

    @property
    def pkgdb_api_url(self):
        return self._pkgdb_api_url

    @pkgdb_api_url.setter
    def pkgdb_api_url(self, s):
        self._pkgdb_api_url = str(s)

    @property
    def log_backend(self):
        return self._log_backend

    @log_backend.setter
    def log_backend(self, s):
        if s == None:
            self._log_backend = "console"
        elif not s in logger.supported_log_backends():
            raise ValueError("Unsupported log backend")

        self._log_backend = str(s)

    @property
    def log_file(self):
        return self._log_file

    @log_file.setter
    def log_file(self, s):
        if s == None:
            self._log_file = ""
        else:
            self._log_file = str(s)

    @property
    def log_level(self):
        return self._log_level

    @log_level.setter
    def log_level(self, s):
        level = str(s).lower()
        self._log_level = logger.str_to_log_level(level)

