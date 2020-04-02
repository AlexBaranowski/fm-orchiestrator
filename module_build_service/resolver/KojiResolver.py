# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from itertools import groupby

from module_build_service.common import conf, log, models
from module_build_service.common.koji import get_session, koji_multicall_map
from module_build_service.resolver.DBResolver import DBResolver


class KojiResolver(DBResolver):
    """
    Resolver using Koji server running in infrastructure.
    """

    backend = "koji"

    def _filter_inherited(self, koji_session, module_builds, top_tag, event):
        """
        Look at the tag inheritance and keep builds only from the topmost tag.

        For example, we have "foo:bar:1" and "foo:bar:2" builds. We also have "foo-tag" which
        inherits "foo-parent-tag". The "foo:bar:1" is tagged in the "foo-tag". The "foo:bar:2"
        is tagged in the "foo-parent-tag".

        In this case, this function filters out the foo:bar:2, because "foo:bar:1" is tagged
        lower in the inheritance tree in the "foo-tag".

        For normal RPMs, using latest=True for listTagged() call, Koji would automatically do
        this, but it does not understand streams, so we have to reimplement it here.

        :param KojiSession koji_session: Koji session.
        :param list module_builds: List of builds as returned by KojiSession.listTagged method.
        :param str top_tag: The top Koji tag.
        :param dict event: Koji event defining the time at which the `module_builds` have been
            fetched.
        :return list: Filtered list of builds.
        """
        inheritance = [
            tag["name"] for tag in koji_session.getFullInheritance(top_tag, event=event["id"])
        ]

        def keyfunc(mb):
            return (mb["name"], mb["version"])

        result = []

        # Group modules by Name-Stream
        for _, builds in groupby(sorted(module_builds, key=keyfunc), keyfunc):
            builds = list(builds)
            # For each N-S combination find out which tags it's in
            available_in = set(build["tag_name"] for build in builds)

            # And find out which is the topmost tag
            for tag in [top_tag] + inheritance:
                if tag in available_in:
                    break

            # And keep only builds from that topmost tag
            result.extend(build for build in builds if build["tag_name"] == tag)

        return result

    def _filter_based_on_real_stream_name(self, koji_session, module_builds, stream):
        """
        Query Koji for real stream name of each module and keep only those matching `stream`.

        This needs to be done, because MBS stores the stream name in the "version" field in Koji,
        but the "version" field cannot contain "-" character. Therefore MBS replaces all "-"
        with "_". This makes it impossible to reconstruct the original stream name from the
        "version" field.

        We therefore need to ask for real original stream name here and filter out modules based
        on this real stream name.

        :param KojiSession koji_session: Koji session.
        :param list module_builds: List of builds as returned by KojiSession.listTagged method.
        :param str stream: The requested stream name.
        :return list: Filtered list of builds.
        """
        # Return early if there are no module builds.
        if not module_builds:
            return []

        # Prepare list of build ids to pass them to Koji multicall later.
        build_ids = [b["build_id"] for b in module_builds]

        # Get the Koji builds from Koji.
        koji_builds = koji_multicall_map(koji_session, koji_session.getBuild, build_ids)
        if not koji_builds:
            raise RuntimeError("Error during Koji multicall when filtering KojiResolver builds.")

        # Filter out modules with different stream in the Koji build metadata.
        ret = []
        for module_build, koji_build in zip(module_builds, koji_builds):
            koji_build_stream = koji_build.get("extra", {}).get("typeinfo", {}).get("module", {}).\
                get("stream")
            if not koji_build_stream:
                log.warning(
                    "Not filtering out Koji build with id %d - it has no \"stream\" set in its "
                    "metadata." % koji_build["build_id"])
                ret.append(module_build)
                continue

            if koji_build_stream == stream:
                ret.append(module_build)
            else:
                log.info(
                    "Filtering out Koji build %d - its stream \"%s\" does not match the requested "
                    "stream \"%s\"" % (koji_build["build_id"], stream, koji_build_stream))

        return ret

    def get_buildrequired_koji_builds(self, name, stream, base_module_mmd):
        """
        Returns list of Koji build dicts of all module builds with `name` and `stream` which are
        tagged in the Koji tag defined in `base_module_mmd`.

        :param str name: Name of module to return.
        :param str stream: Stream of module to return.
        :param Modulemd base_module_mmd: Base module metadata.
        :return list: List of Koji build dicts.
        """
        # Get the `koji_tag_with_modules`. If the `koji_tag_with_modules` is not configured for
        # the base module, fallback to DBResolver.
        tag = base_module_mmd.get_xmd().get("mbs", {}).get("koji_tag_with_modules")
        if not tag:
            return []

        koji_session = get_session(conf, login=False)
        event = koji_session.getLastEvent()

        # List all the modular builds in the modular Koji tag.
        # We cannot use latest=True here, because we need to get all the
        # available streams of all modules. The stream is represented as
        # "version" in Koji build and with latest=True, Koji would return
        # only builds with the highest version.
        # We also cannot ask for particular `stream`, because Koji does not support that.
        module_builds = koji_session.listTagged(
            tag, inherit=True, type="module", package=name, event=event["id"])

        # Filter out different streams. Note that the stream name in the b["version"] is
        # normalized. This makes it impossible to find out its original value. We therefore
        # filter out only completely different stream names here to reduce the `module_builds`
        # dramatically, but the resulting `module_builds` list might still contain unwanted
        # streams. We will get rid of them using the `_filter_based_on_real_stream_name` method
        # later.
        # Example of such streams: "fedora-30" and "fedora_30". They will both be normalized to
        # "fedora_30".
        normalized_stream = stream.replace("-", "_")
        module_builds = [b for b in module_builds if b["version"] == normalized_stream]

        # Filter out builds inherited from non-top tag
        module_builds = self._filter_inherited(koji_session, module_builds, tag, event)

        # Filter out modules based on the real stream name.
        module_builds = self._filter_based_on_real_stream_name(koji_session, module_builds, stream)

        # Find the latest builds of all modules. This does the following:
        # - Sorts the module_builds descending by Koji NVR (which maps to NSV
        #   for modules). Split release into modular version and context, and
        #   treat version as numeric.
        # - Groups the sorted module_builds by NV (NS in modular world).
        #   In each resulting `ns_group`, the first item is actually build
        #   with the latest version (because the list is still sorted by NVR).
        # - Groups the `ns_group` again by "release" ("version" in modular
        #   world) to just get all the "contexts" of the given NSV. This is
        #   stored in `nsv_builds`.
        # - The `nsv_builds` contains the builds representing all the contexts
        #   of the latest version for give name-stream, so add them to
        #   `latest_builds`.
        def _key(build):
            ver, ctx = build["release"].split(".", 1)
            return build["name"], build["version"], int(ver), ctx

        latest_builds = []
        module_builds = sorted(module_builds, key=_key, reverse=True)
        for _, ns_builds in groupby(
                module_builds, key=lambda x: ":".join([x["name"], x["version"]])):
            for _, nsv_builds in groupby(
                    ns_builds, key=lambda x: x["release"].split(".")[0]):
                latest_builds += list(nsv_builds)
                break
        return latest_builds

    def get_buildrequired_modules(self, name, stream, base_module_mmd):
        """
        Returns ModuleBuild objects of all module builds with `name` and `stream` which are tagged
        in the Koji tag defined in `base_module_mmd`.

        :param str name: Name of module to return.
        :param str stream: Stream of module to return.
        :param Modulemd base_module_mmd: Base module metadata.
        :return list: List of ModuleBuilds.
        """
        latest_builds = self.get_buildrequired_koji_builds(name, stream, base_module_mmd)

        # For each latest module build, find the matching ModuleBuild and store it into `ret`.
        ret = []
        for build in latest_builds:
            version, context = build["release"].split(".")
            module = models.ModuleBuild.get_build_from_nsvc(
                self.db_session, name, stream, version, context)
            if not module:
                tag = base_module_mmd.get_xmd().get("mbs", {}).get("koji_tag_with_modules")
                raise ValueError(
                    "Module %s is tagged in the %s Koji tag, but does not exist "
                    "in MBS DB." % (":".join([name, stream, version, context]), tag))
            ret.append(module)

        return ret

    def get_buildrequired_modulemds(self, name, stream, base_module_mmd):
        """
        Returns modulemd metadata of all module builds with `name` and `stream` which are tagged
        in the Koji tag defined in `base_module_mmd`.

        :param str name: Name of module to return.
        :param str stream: Stream of module to return.
        :param Modulemd base_module_mmd: Base module metadata.
        :return list: List of modulemd metadata.
        """
        tag = base_module_mmd.get_xmd().get("mbs", {}).get("koji_tag_with_modules")
        if not tag:
            log.info(
                "The %s does not define 'koji_tag_with_modules'. Falling back to DBResolver." %
                (base_module_mmd.get_nsvc()))
            return DBResolver.get_buildrequired_modulemds(self, name, stream, base_module_mmd)

        modules = self.get_buildrequired_modules(name, stream, base_module_mmd)
        return [module.mmd() for module in modules]

    def get_compatible_base_module_modulemds(
            self, base_module_mmd, stream_version_lte, virtual_streams, states):
        """
        Returns the Modulemd metadata of base modules compatible with base module
        defined by `name` and `stream`.

        For base module which enables KojiResolver feature in its XMD section, this
        method always returns an empty list. The compatible modules are
        defined by the Koji tag inheritance, so there is no need to find out the compatible
        base modules on MBS side.

        If the base module does not enable KojiResolver, the compatibility is determined
        using DBResolver.

        :param base_module_mmd: Modulemd medatada defining the input base module.
        :param stream_version_lte: If True, the compatible streams are limited
             by the stream version computed from `stream`. If False, even the
             modules with higher stream version are returned.
        :param virtual_streams: List of virtual streams. If set, also modules
            with incompatible stream version are returned in case they share
            one of the virtual streams.
        :param states: List of states the returned compatible modules should
            be in.
        :return list: List of Modulemd objects.
        """
        tag = base_module_mmd.get_xmd().get("mbs", {}).get("koji_tag_with_modules")
        if not tag:
            log.info(
                "The %s does not define 'koji_tag_with_modules'. Falling back to DBResolver." %
                (base_module_mmd.get_nsvc()))
            return DBResolver.get_compatible_base_module_modulemds(
                self, base_module_mmd, stream_version_lte, virtual_streams, states)

        return []
