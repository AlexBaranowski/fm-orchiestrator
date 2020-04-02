# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import errno
import os
import shutil
import tempfile

import dnf
import kobo.rpmlib
import koji
import six.moves.xmlrpc_client as xmlrpclib

from module_build_service.common import conf, log, models, scm
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.koji import get_session, koji_retrying_multicall_map
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.resolve import (
    expand_single_mse_streams, get_compatible_base_module_mmds
)
from module_build_service.resolver.base import GenericResolver
from module_build_service.common.retry import retry
from module_build_service.scheduler.db_session import db_session


def add_default_modules(mmd):
    """
    Add default modules as buildrequires to the input modulemd.

    The base modules that are buildrequired can optionally link their default modules by specifying
    a URL to a text file in xmd.mbs.default_modules_url. Any default module that isn't in the
    database will be logged and ignored.

    :param Modulemd.ModuleStream mmd: the modulemd of the module to add the module defaults to
    :raises RuntimeError: if the buildrequired base module isn't in the database or the default
        modules list can't be downloaded
    """
    log.info("Finding the default modules to include as buildrequires")
    xmd = mmd.get_xmd()
    buildrequires = xmd["mbs"]["buildrequires"]
    defaults_added = False

    for module_name in conf.base_module_names:
        bm_info = buildrequires.get(module_name)
        if bm_info is None:
            log.debug(
                "The base module %s is not a buildrequire of the submitted module %s",
                module_name, mmd.get_nsvc(),
            )
            continue

        bm = models.ModuleBuild.get_build_from_nsvc(
            db_session, module_name, bm_info["stream"], bm_info["version"], bm_info["context"],
        )
        bm_nsvc = ":".join([
            module_name, bm_info["stream"], bm_info["version"], bm_info["context"],
        ])
        if not bm:
            raise RuntimeError("Failed to retrieve the module {} from the database".format(bm_nsvc))

        bm_mmd = bm.mmd()
        bm_xmd = bm_mmd.get_xmd()
        use_default_modules = bm_xmd.get("mbs", {}).get("use_default_modules", False)
        default_modules_scm_url = bm_xmd.get("mbs", {}).get("default_modules_scm_url")
        if not (use_default_modules or default_modules_scm_url):
            log.info('The base module %s has no default modules', bm_mmd.get_nsvc())
            continue

        # If the base module does not provide a default_modules_scm_url, use the default that is
        # configured
        default_modules_scm_url = default_modules_scm_url or conf.default_modules_scm_url
        default_modules = _get_default_modules(bm.stream, default_modules_scm_url)
        for name, stream in default_modules.items():
            ns = "{}:{}".format(name, stream)
            if name in buildrequires:
                conflicting_stream = buildrequires[name]["stream"]
                if stream == conflicting_stream:
                    log.info("The default module %s is already a buildrequire", ns)
                    continue

                log.info(
                    "The default module %s will not be added as a buildrequire since %s:%s "
                    "is already a buildrequire",
                    ns, name, conflicting_stream,
                )
                continue

            # Query for the latest default module that was built against this base module
            resolver = GenericResolver.create(db_session, conf)
            base_mmds = get_compatible_base_module_mmds(resolver, bm_mmd)
            base_mmds = base_mmds["ready"] + base_mmds["garbage"]
            base_mmds.sort(
                key=lambda mmd: models.ModuleBuild.get_stream_version(mmd.get_stream_name(), False),
                reverse=True)
            for base_mmd in base_mmds:
                default_module_mmds = resolver.get_buildrequired_modulemds(name, stream, base_mmd)
                if not default_module_mmds:
                    continue

                # We need to ensure that module built against compatible base module stream
                # really contains runtime-dependency on the current base module stream.
                # For example in Fedora, we can have platform:f30 and platform:f31 base module
                # streams. There can be foo:1 module built against platform:f30 which can work with
                # any platform ("requires: platform: []"). This module can be configured as default
                # module for platform:f28 and we need to support this case, but in the same time we
                # cannot simply add any platform:f27 based module to platform:f28.
                module_found = False
                for default_module_mmd in default_module_mmds:
                    for deps in default_module_mmd.get_dependencies():
                        streams = deps.get_runtime_streams(module_name)
                        if streams is None:
                            continue
                        streams = expand_single_mse_streams(db_session, module_name, streams)
                        if bm_info["stream"] in streams:
                            module_found = True
                            break
                    else:
                        log.info(
                            "Not using module %s as default module, because it does not "
                            "contain runtime dependency on %s", default_module_mmd.get_nsvc(),
                            bm_nsvc)
                if module_found:
                    break
            else:
                log.warning(
                    "The default module %s from %s is not in the database and couldn't be added as "
                    "a buildrequire",
                    ns, bm_nsvc,
                )
                continue
            # Use resolve_requires since it provides the exact format that is needed for
            # mbs.xmd.buildrequires
            resolved = resolver.resolve_requires([default_module_mmd.get_nsvc()])

            nsvc = ":".join([name, stream, resolved[name]["version"], resolved[name]["context"]])
            log.info("Adding the default module %s as a buildrequire", nsvc)
            buildrequires.update(resolved)
            defaults_added = True

    if defaults_added:
        mmd.set_xmd(xmd)
    return defaults_added


def _get_default_modules(stream, default_modules_scm_url):
    """
    Get the base module's default modules.

    :param str stream: the stream of the base module
    :param str default_modules_scm_url: the SCM URL to the default modules
    :return: a dictionary where the keys are default module names and the values are default module
        streams
    :rtype: dict
    :raise RuntimeError: if no default modules can be retrieved for that stream
    """
    scm_obj = scm.SCM(default_modules_scm_url)
    temp_dir = tempfile.mkdtemp()
    try:
        log.debug("Cloning the default modules repo at %s", default_modules_scm_url)
        scm_obj.clone(temp_dir)
        log.debug("Checking out the branch %s", stream)
        try:
            scm_obj.checkout_ref(stream)
        except UnprocessableEntity:
            # If the checkout fails, try seeing if this is a rawhide build. In this case, the branch
            # should actually be conf.rawhide_branch. The check to see if this is a rawhide build
            # is done after the first checkout failure for performance reasons, since it avoids an
            # unnecessary connection and query to Koji.
            if conf.uses_rawhide:
                log.debug(
                    "Checking out the branch %s from the default modules repo failed. Trying to "
                    "determine if this stream represents rawhide.",
                    stream,
                )
                if _get_rawhide_version() == stream:
                    log.debug(
                        "The stream represents rawhide, will try checking out %s",
                        conf.rawhide_branch,
                    )
                    # There's no try/except here because we want the outer except block to
                    # catch this in the event the rawhide branch doesn't exist
                    scm_obj.checkout_ref(conf.rawhide_branch)
                else:
                    # If it's not a rawhide build, then the branch should have existed
                    raise
            else:
                # If it's not a rawhide build, then the branch should have existed
                raise

        idx = Modulemd.ModuleIndex.new()
        idx.update_from_defaults_directory(
            path=scm_obj.sourcedir,
            overrides_path=os.path.join(scm_obj.sourcedir, "overrides"),
            strict=True,
        )
        return idx.get_default_streams()
    except:  # noqa: E722
        msg = "Failed to retrieve the default modules"
        log.exception(msg)
        raise RuntimeError(msg)
    finally:
        shutil.rmtree(temp_dir)


@retry(wait_on=(xmlrpclib.ProtocolError, koji.GenericError))
def _get_rawhide_version():
    """
    Query Koji to find the rawhide version from the build target.

    :return: the rawhide version (e.g. "f32")
    :rtype: str
    """
    koji_session = get_session(conf, login=False)
    build_target = koji_session.getBuildTarget("rawhide")
    if build_target:
        return build_target["build_tag_name"].partition("-build")[0]


def handle_collisions_with_base_module_rpms(mmd, arches):
    """
    Find any RPMs in the buildrequired base modules that collide with the buildrequired modules.

    If a buildrequired module contains RPMs that overlap with RPMs in the buildrequired base
    modules, then the NEVRAs of the overlapping RPMs in the base modules will be added as conflicts
    in the input modulemd.

    :param Modulemd.ModuleStream mmd: the modulemd to find the collisions
    :param list arches: the arches to limit the external repo queries to
    :raise RuntimeError: when a Koji query fails
    """
    log.info("Finding any buildrequired modules that collide with the RPMs in the base modules")
    bm_tags = set()
    non_bm_tags = set()
    xmd = mmd.get_xmd()
    buildrequires = xmd["mbs"]["buildrequires"]
    for name, info in buildrequires.items():
        if not info["koji_tag"]:
            continue

        if name in conf.base_module_names:
            bm_tags.add(info["koji_tag"])
        else:
            non_bm_tags.add(info["koji_tag"])

    if not (bm_tags and non_bm_tags):
        log.info(
            "Skipping the collision check since collisions are not possible with these "
            "buildrequires"
        )
        return

    log.debug(
        "Querying Koji for the latest RPMs from the buildrequired base modules from the tags: %s",
        ", ".join(bm_tags),
    )
    koji_session = get_session(conf, login=False)
    bm_rpms = _get_rpms_from_tags(koji_session, list(bm_tags), arches)
    # The keys are base module RPM names and the values are sets of RPM NEVRAs with that name
    name_to_nevras = {}
    for bm_rpm in bm_rpms:
        rpm_name = kobo.rpmlib.parse_nvra(bm_rpm)["name"]
        name_to_nevras.setdefault(rpm_name, set())
        name_to_nevras[rpm_name].add(bm_rpm)
    # Clear this out of RAM as soon as possible since this value can be huge
    del bm_rpms

    log.debug(
        "Querying Koji for the latest RPMs from the other buildrequired modules from the tags: %s",
        ", ".join(non_bm_tags),
    )
    # This will contain any NEVRAs of RPMs in the base module tag with the same name as those in the
    # buildrequired modules
    conflicts = set()
    non_bm_rpms = _get_rpms_from_tags(koji_session, list(non_bm_tags), arches)
    for rpm in non_bm_rpms:
        rpm_name = kobo.rpmlib.parse_nvra(rpm)["name"]
        if rpm_name in name_to_nevras:
            conflicts = conflicts | name_to_nevras[rpm_name]

    # Add the conflicting NEVRAs to `ursine_rpms` so the Conflicts are later generated for them
    # in the KojiModuleBuilder.
    xmd["mbs"]["ursine_rpms"] = list(conflicts)
    mmd.set_xmd(xmd)


def _get_rpms_from_tags(koji_session, tags, arches):
    """
    Get the RPMs in NEVRA form (tagged or external repos) of the input tags.

    :param koji.ClientSession koji_session: the Koji session to use to query
    :param list tags: the list of tags to get the RPMs from
    :param list arches: the arches to limit the external repo queries to
    :return: the set of RPMs in NEVRA form of the input tags
    :rtype: set
    :raises RuntimeError: if the Koji query fails
    """
    log.debug("Get the latest RPMs from the tags: %s", ", ".join(tags))
    kwargs = [{"latest": True, "inherit": True}] * len(tags)
    tagged_results = koji_retrying_multicall_map(
        koji_session, koji_session.listTaggedRPMS, tags, kwargs,
    )
    if not tagged_results:
        raise RuntimeError(
            "Getting the tagged RPMs of the following Koji tags failed: {}"
            .format(", ".join(tags))
        )

    nevras = set()
    for tagged_result in tagged_results:
        rpms, _ = tagged_result
        for rpm_dict in rpms:
            nevra = kobo.rpmlib.make_nvra(rpm_dict, force_epoch=True)
            nevras.add(nevra)

    repo_results = koji_retrying_multicall_map(koji_session, koji_session.getExternalRepoList, tags)
    if not repo_results:
        raise RuntimeError(
            "Getting the external repos of the following Koji tags failed: {}"
            .format(", ".join(tags)),
        )
    for repos in repo_results:
        for repo in repos:
            # Use the repo ID in the cache directory name in case there is more than one external
            # repo associated with the tag
            cache_dir_name = "{}-{}".format(repo["tag_name"], repo["external_repo_id"])
            nevras = nevras | _get_rpms_in_external_repo(repo["url"], arches, cache_dir_name)

    return nevras


def _get_rpms_in_external_repo(repo_url, arches, cache_dir_name):
    """
    Get the available RPMs in the external repo for the provided arches.

    :param str repo_url: the URL of the external repo with the "$arch" variable included
    :param list arches: the list of arches to query the external repo for
    :param str cache_dir_name: the cache directory name under f"{conf.cache_dir}/dnf"
    :return: a set of the RPM NEVRAs
    :rtype: set
    :raise RuntimeError: if the cache is not writeable or the external repo couldn't be loaded
    :raises ValueError: if there is no "$arch" variable in repo URL
    """
    if "$arch" not in repo_url:
        raise ValueError(
            "The external repo {} does not contain the $arch variable".format(repo_url)
        )

    base = dnf.Base()
    try:
        dnf_conf = base.conf
        # Expire the metadata right away so that when a repo is loaded, it will always check to
        # see if the external repo has been updated
        dnf_conf.metadata_expire = 0

        cache_location = os.path.join(conf.cache_dir, "dnf", cache_dir_name)
        try:
            # exist_ok=True can't be used in Python 2
            os.makedirs(cache_location, mode=0o0770)
        except OSError as e:
            # Don't fail if the directories already exist
            if e.errno != errno.EEXIST:
                log.exception("Failed to create the cache directory %s", cache_location)
                raise RuntimeError("The MBS cache is not writeable.")

        # Tell DNF to use the cache directory
        dnf_conf.cachedir = cache_location
        # Don't skip repos that can't be synchronized
        dnf_conf.skip_if_unavailable = False
        dnf_conf.timeout = conf.dnf_timeout
        # Get rid of everything to be sure it's a blank slate. This doesn't delete the cached repo
        # data.
        base.reset(repos=True, goal=True, sack=True)

        # Add a separate repo for each architecture
        for arch in arches:
            # Convert arch to canon_arch. This handles cases where Koji "i686" arch is mapped to
            # "i386" when generating RPM repository.
            canon_arch = koji.canonArch(arch)
            repo_name = "repo_{}".format(canon_arch)
            repo_arch_url = repo_url.replace("$arch", canon_arch)
            base.repos.add_new_repo(
                repo_name, dnf_conf, baseurl=[repo_arch_url], minrate=conf.dnf_minrate,
            )

        try:
            # Load the repos in parallel
            base.update_cache()
        except dnf.exceptions.RepoError:
            msg = "Failed to load the external repos"
            log.exception(msg)
            raise RuntimeError(msg)

        # dnf will not always raise an error on repo failures, so we check explicitly
        for repo_name in base.repos:
            if not base.repos[repo_name].metadata:
                msg = "Failed to load metadata for repo %s" % repo_name
                log.exception(msg)
                raise RuntimeError(msg)

        base.fill_sack(load_system_repo=False)

        # Return all the available RPMs
        nevras = set()
        for rpm in base.sack.query().available():
            rpm_dict = {
                "arch": rpm.arch,
                "epoch": rpm.epoch,
                "name": rpm.name,
                "release": rpm.release,
                "version": rpm.version,
            }
            nevra = kobo.rpmlib.make_nvra(rpm_dict, force_epoch=True)
            nevras.add(nevra)
    finally:
        base.close()

    return nevras
