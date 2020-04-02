# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from module_build_service.common.config import conf
from module_build_service.common.errors import StreamAmbigous
from module_build_service.resolver import GenericResolver


def expand_single_mse_streams(
        db_session, name, streams, default_streams=None, raise_if_stream_ambigous=False):
    """
    Helper method for `expand_mse_stream()` expanding single name:[streams].
    Returns list of expanded streams.

    :param db_session: SQLAlchemy DB session.
    :param str name: Name of the module which will be expanded.
    :param streams: List of streams to expand.
    :type streams: list[str]
    :param dict default_streams: Dict in {module_name: module_stream, ...} format defining
        the default stream to choose for module in case when there are multiple streams to
        choose from.
    :param bool raise_if_stream_ambigous: When True, raises a StreamAmbigous exception in case
        there are multiple streams for some dependency of module and the module name is not
        defined in `default_streams`, so it is not clear which stream should be used.
    """
    from module_build_service.common import models

    default_streams = default_streams or {}
    # Stream can be prefixed with '-' sign to define that this stream should
    # not appear in a resulting list of streams. There can be two situations:
    # a) all streams have '-' prefix. In this case, we treat list of streams
    #    as blacklist and we find all the valid streams and just remove those with
    #    '-' prefix.
    # b) there is at least one stream without '-' prefix. In this case, we can
    #    ignore all the streams with '-' prefix and just add those without
    #    '-' prefix to the list of valid streams.
    streams_is_blacklist = all(stream.startswith("-") for stream in streams)
    if streams_is_blacklist or len(streams) == 0:
        if name in default_streams:
            expanded_streams = [default_streams[name]]
        elif raise_if_stream_ambigous:
            raise StreamAmbigous("There are multiple streams to choose from for module %s." % name)
        else:
            builds = models.ModuleBuild.get_last_build_in_all_streams(db_session, name)
            expanded_streams = [build.stream for build in builds]
    else:
        expanded_streams = []
    for stream in streams:
        if stream.startswith("-"):
            if streams_is_blacklist and stream[1:] in expanded_streams:
                expanded_streams.remove(stream[1:])
        else:
            expanded_streams.append(stream)

    if len(expanded_streams) > 1:
        if name in default_streams:
            expanded_streams = [default_streams[name]]
        elif raise_if_stream_ambigous:
            raise StreamAmbigous(
                "There are multiple streams %r to choose from for module %s."
                % (expanded_streams, name)
            )

    return expanded_streams


def get_compatible_base_module_mmds(resolver, base_mmd, ignore_ns=None):
    """
    Returns dict containing the base modules compatible with `base_mmd` grouped by their state.

    :param GenericResolver resolver: GenericResolver instance.
    :param Modulemd base_mmd: Modulemd instant to return compatible modules for.
    :param set ignore_ns: When set, defines the name:stream of base modules which will be ignored
        by this function and therefore not returned.
    :return dict: Dictionary with module's state name as a key and list of Modulemd objects for
        each compatible base module in that state. For example:
            {
                "ready": [base_mmd_1, base_mmd_2]
                "garbage": [base_mmd_3]
            }
        The input `base_mmd` is always included in the result in "ready" state.
    """
    from module_build_service.common import models

    # Add the module to `seen` and `ret`.
    ret = {"ready": [], "garbage": []}
    ret["ready"].append(base_mmd)
    ns = ":".join([base_mmd.get_module_name(), base_mmd.get_stream_name()])
    seen = set() if not ignore_ns else set(ignore_ns)
    seen.add(ns)

    # Get the list of compatible virtual streams.
    xmd = base_mmd.get_xmd()
    virtual_streams = xmd.get("mbs", {}).get("virtual_streams")

    # In case there are no virtual_streams in the buildrequired name:stream,
    # it is clear that there are no compatible streams, so return just this
    # `base_mmd`.
    if not virtual_streams:
        return ret

    if conf.allow_only_compatible_base_modules:
        stream_version_lte = True
        states = ["ready"]
    else:
        stream_version_lte = False
        states = ["ready", "garbage"]

    for state in states:
        mmds = resolver.get_compatible_base_module_modulemds(
            base_mmd, stream_version_lte, virtual_streams,
            [models.BUILD_STATES[state]])
        ret_chunk = []
        # Add the returned mmds to the `seen` set to avoid querying those
        # individually if they are part of the buildrequire streams for this
        # base module.
        for mmd_ in mmds:
            mmd_ns = ":".join([mmd_.get_module_name(), mmd_.get_stream_name()])
            # An extra precaution to ensure there are no duplicates. This can happen when there
            # are two modules with the same name:stream - one in ready state and one in garbage
            # state.
            if mmd_ns not in seen:
                ret_chunk.append(mmd_)
                seen.add(mmd_ns)
        ret[state] += ret_chunk

    return ret


def get_base_module_mmds(db_session, mmd):
    """
    Returns list of MMDs of base modules buildrequired by `mmd` including the compatible
    old versions of the base module based on the stream version.

    :param Modulemd mmd: Input modulemd metadata.
    :rtype: dict with lists of Modulemd
    :return: Dict with "ready" or "garbage" state name as a key and list of MMDs of base modules
        buildrequired by `mmd` as a value.
    """
    from module_build_service.common import models

    seen = set()
    ret = {"ready": [], "garbage": []}

    resolver = GenericResolver.create(db_session, conf)
    for deps in mmd.get_dependencies():
        buildrequires = {
            module: deps.get_buildtime_streams(module)
            for module in deps.get_buildtime_modules()
        }
        for name in conf.base_module_names:
            if name not in buildrequires.keys():
                continue

            # Since the query below uses stream_version_lte, we can sort the streams by stream
            # version in descending order to not perform unnecessary queries. Otherwise, if the
            # streams are f29.1.0 and f29.2.0, then two queries will occur, causing f29.1.0 to be
            # returned twice. Only one query for that scenario is necessary.
            sorted_desc_streams = sorted(
                buildrequires[name], reverse=True, key=models.ModuleBuild.get_stream_version)
            for stream in sorted_desc_streams:
                ns = ":".join([name, stream])
                if ns in seen:
                    continue

                # Get the MMD for particular buildrequired name:stream to find out the virtual
                # streams according to which we can find the compatible streams later.
                # The returned MMDs are all the module builds for name:stream in the highest
                # version. Given the base module does not depend on other modules, it can appear
                # only in single context and therefore the `mmds` should always contain just
                # zero or one module build.
                mmds = resolver.get_module_modulemds(name, stream)
                if not mmds:
                    continue
                base_mmd = mmds[0]

                new_ret = get_compatible_base_module_mmds(resolver, base_mmd, ignore_ns=seen)
                for state in new_ret.keys():
                    for mmd_ in new_ret[state]:
                        mmd_ns = ":".join([mmd_.get_module_name(), mmd_.get_stream_name()])
                        seen.add(mmd_ns)
                    ret[state] += new_ret[state]
            break
    return ret
