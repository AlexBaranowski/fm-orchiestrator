# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from module_build_service.resolver.DBResolver import DBResolver


class LocalResolver(DBResolver):
    """
    Resolver using DNF and local repositories.

    It is subclass of DBResolver with small changes to DBResolver logic to fit
    the offline local module builds. See particular methods for more information.
    """

    backend = "local"

    def get_buildrequired_modulemds(self, name, stream, base_module_mmd):
        """
        Returns modulemd metadata of all module builds with `name` and `stream`.

        For LocalResolver which is used only for Offline local builds,
        the `base_module_mmd` is ignored. Normally, the `base_module_mmd is used
        to filter out platform:streams which are not compatible with currently used
        stream version. But during offline local builds, we always have just single
        platform:stream derived from PLATFORM_ID in /etc/os-release.

        Because we have just single platform stream, there is no reason to filter
        incompatible streams. This platform stream is also expected to not follow
        the "X.Y.Z" formatting which is needed for stream versions.

        :param str name: Name of module to return.
        :param str stream: Stream of module to return.
        :param Modulemd base_module_mmd: Ignored in LocalResolver.
        :rtype: list
        :return: List of modulemd metadata.
        """
        return self.get_module_modulemds(name, stream)
