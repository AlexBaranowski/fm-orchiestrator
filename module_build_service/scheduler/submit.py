# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime
import json
from multiprocessing.dummy import Pool as ThreadPool
import os

import kobo.rpmlib

import module_build_service.common.scm
from module_build_service.common import conf, log, models
from module_build_service.common.errors import ValidationError, UnprocessableEntity, Forbidden
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.submit import fetch_mmd
from module_build_service.common.utils import to_text_type
from module_build_service.scheduler.db_session import db_session


def get_build_arches(mmd, config):
    """
    Returns the list of architectures for which the module `mmd` should be built.

    :param mmd: Module MetaData
    :param config: config (module_build_service.common.config.Config instance)
    :return: list of architectures
    """
    # Imported here to allow import of utils in GenericBuilder.
    from module_build_service.builder import GenericBuilder

    nsvc = mmd.get_nsvc()

    def _conditional_log(msg, arches, new_arches):
        # Checks if the arch list returned by _check_buildopts_arches is the same one passed to it
        # If it is, it outputs the message
        if arches is new_arches:
            log.info(msg)

    # At first, handle BASE_MODULE_ARCHES - this overrides any other option.
    # Find out the base modules in buildrequires section of XMD and
    # set the Koji tag arches according to it.
    if "mbs" in mmd.get_xmd():
        for req_name, req_data in mmd.get_xmd()["mbs"]["buildrequires"].items():
            ns = ":".join([req_name, req_data["stream"]])
            if ns in config.base_module_arches:
                arches = config.base_module_arches[ns]
                new_arches = _check_buildopts_arches(mmd, arches)
                msg = "Setting build arches of %s to %r based on the BASE_MODULE_ARCHES." % (
                    nsvc, new_arches)
                _conditional_log(msg, arches, new_arches)
                return new_arches

    # Check whether the module contains the `koji_tag_arches`. This is used only
    # by special modules defining the layered products.
    try:
        arches = mmd.get_xmd()["mbs"]["koji_tag_arches"]
        new_arches = _check_buildopts_arches(mmd, arches)
        msg = "Setting build arches of %s to %r based on the koji_tag_arches." % (
            nsvc, new_arches)
        _conditional_log(msg, arches, new_arches)
        return new_arches
    except KeyError:
        pass

    # Check the base/layered-product module this module buildrequires and try to get the
    # list of arches from there.
    try:
        buildrequires = mmd.get_xmd()["mbs"]["buildrequires"]
    except (ValueError, KeyError):
        log.warning(
            "Module {0} does not have buildrequires in its xmd".format(mmd.get_nsvc()))
        buildrequires = None
    if buildrequires:
        # Looping through all the privileged modules that are allowed to set koji tag arches
        # and the base modules to see what the koji tag arches should be. Doing it this way
        # preserves the order in the configurations.
        for module in conf.allowed_privileged_module_names + conf.base_module_names:
            module_in_xmd = buildrequires.get(module)

            if not module_in_xmd:
                continue

            module_obj = models.ModuleBuild.get_build_from_nsvc(
                db_session,
                module,
                module_in_xmd["stream"],
                module_in_xmd["version"],
                module_in_xmd["context"],
            )
            if not module_obj:
                continue
            arches = GenericBuilder.get_module_build_arches(module_obj)
            if arches:
                new_arches = _check_buildopts_arches(mmd, arches)
                msg = "Setting build arches of %s to %r based on the buildrequired module %r." % (
                    nsvc, new_arches, module_obj)
                _conditional_log(msg, arches, new_arches)
                return new_arches

    # As a last resort, return just the preconfigured list of arches.
    arches = config.arches
    new_arches = _check_buildopts_arches(mmd, arches)
    msg = "Setting build arches of %s to %r based on default ARCHES." % (nsvc, new_arches)
    _conditional_log(msg, arches, new_arches)
    return new_arches


def _check_buildopts_arches(mmd, arches):
    """
    Returns buildopts arches if valid, or otherwise the arches provided.

    :param mmd: Module MetaData
    :param arches: list of architectures
    :return: list of architectures
    """
    buildopts = mmd.get_buildopts()
    if not buildopts:
        return arches
    try:
        buildopts_arches = buildopts.get_arches()
    except AttributeError:
        # libmodulemd version < 2.8.3
        return arches
    # Must be a subset of the input module arches
    unsupported_arches = set(buildopts_arches) - set(arches)
    if unsupported_arches:
        raise ValidationError("The following buildopts arches are not supported with these "
                              "buildrequires: %r" % unsupported_arches)
    if buildopts_arches:
        log.info("Setting build arches of %s to %r based on the buildopts arches." % (
            mmd.get_nsvc(), buildopts_arches))
        return buildopts_arches
    return arches


def record_module_build_arches(mmd, build):
    """
    Finds out the list of build arches against which the ModuleBuld `build` should be built
    and records them to `build.arches`.

    :param Modulemd mmd: The MMD file associated with a ModuleBuild.
    :param ModuleBuild build: The ModuleBuild.
    """
    arches = get_build_arches(mmd, conf)
    for arch in arches:
        arch_obj = db_session.query(models.ModuleArch).filter_by(name=arch).first()
        if not arch_obj:
            arch_obj = models.ModuleArch(name=arch)
        if arch_obj not in build.arches:
            build.arches.append(arch_obj)

    db_session.commit()


def record_filtered_rpms(mmd):
    """Record filtered RPMs that should not be installed into buildroot

    These RPMs are filtered:

    * Reads the mmd["xmd"]["buildrequires"] and extends it with "filtered_rpms"
      list containing the NVRs of filtered RPMs in a buildrequired module.

    :param Modulemd mmd: Modulemd that will be built next.
    :rtype: Modulemd.Module
    :return: Modulemd extended with the "filtered_rpms" in XMD section.
    """
    # Imported here to allow import of utils in GenericBuilder.
    from module_build_service.builder import GenericBuilder
    from module_build_service.resolver import GenericResolver

    resolver = GenericResolver.create(db_session, conf)
    builder = GenericBuilder.backends[conf.system]

    new_buildrequires = {}
    for req_name, req_data in mmd.get_xmd()["mbs"]["buildrequires"].items():
        # In case this is module resubmit or local build, the filtered_rpms
        # will already be there, so there is no point in generating them again.
        if "filtered_rpms" in req_data:
            new_buildrequires[req_name] = req_data
            continue

        # We can just get the first modulemd data from result right here thanks to
        # strict=True, so in case the module cannot be found, get_module_modulemds
        # raises an exception.
        req_mmd = resolver.get_module_modulemds(
            req_name, req_data["stream"], req_data["version"], req_data["context"], True)[0]

        # Find out the particular NVR of filtered packages
        filtered_rpms = []
        rpm_filter = req_mmd.get_rpm_filters()
        if rpm_filter:
            built_nvrs = builder.get_built_rpms_in_module_build(req_mmd)
            for nvr in built_nvrs:
                parsed_nvr = kobo.rpmlib.parse_nvr(nvr)
                if parsed_nvr["name"] in rpm_filter:
                    filtered_rpms.append(nvr)
        req_data["filtered_rpms"] = filtered_rpms

        new_buildrequires[req_name] = req_data

    # Replace the old buildrequires with new ones.
    xmd = mmd.get_xmd()
    xmd["mbs"]["buildrequires"] = new_buildrequires
    mmd.set_xmd(xmd)
    return mmd


def _scm_get_latest(pkg):
    try:
        # If the modulemd specifies that the 'f25' branch is what
        # we want to pull from, we need to resolve that f25 branch
        # to the specific commit available at the time of
        # submission (now).
        repo = pkg.get_repository()
        ref = pkg.get_ref()
        log.debug("Getting the commit hash for the ref %s on the repo %s", ref, repo)
        pkgref = module_build_service.common.scm.SCM(repo).get_latest(ref)
    except Exception as e:
        log.exception(e)
        return {
            "error": "Failed to get the latest commit for %s#%s"
            % (pkg.get_repository(), pkg.get_ref())
        }

    return {"pkg_name": pkg.get_name(), "pkg_ref": pkgref, "error": None}


def format_mmd(mmd, scmurl, module=None, db_session=None, srpm_overrides=None):
    """
    Prepares the modulemd for the MBS. This does things such as replacing the
    branches of components with commit hashes and adding metadata in the xmd
    dictionary.
    :param mmd: the Modulemd.ModuleStream object to format
    :param scmurl: the url to the modulemd
    :param module: When specified together with `session`, the time_modified
        of a module is updated regularly in case this method takes lot of time.
    :param db_session: Database session to update the `module`.
    :param dict srpm_overrides: Mapping of package names to SRPM links for all
        component packages which have custom SRPM overrides specified.
    """
    srpm_overrides = srpm_overrides or {}

    xmd = mmd.get_xmd()
    if "mbs" not in xmd:
        xmd["mbs"] = {}
    if "scmurl" not in xmd["mbs"]:
        xmd["mbs"]["scmurl"] = scmurl or ""
    if "commit" not in xmd["mbs"]:
        xmd["mbs"]["commit"] = ""

    # If module build was submitted via yaml file, there is no scmurl
    if scmurl:
        scm = module_build_service.common.scm.SCM(scmurl)
        # We want to make sure we have the full commit hash for consistency
        if module_build_service.common.scm.SCM.is_full_commit_hash(scm.scheme, scm.commit):
            full_scm_hash = scm.commit
        else:
            full_scm_hash = scm.get_full_commit_hash()

        xmd["mbs"]["commit"] = full_scm_hash

    if mmd.get_rpm_component_names() or mmd.get_module_component_names():
        if "rpms" not in xmd["mbs"]:
            xmd["mbs"]["rpms"] = {}
        # Add missing data in RPM components
        for pkgname in mmd.get_rpm_component_names():
            pkg = mmd.get_rpm_component(pkgname)
            # In case of resubmit of existing module which have been
            # cancelled/failed during the init state, the package
            # was maybe already handled by MBS, so skip it in this case.
            if pkgname in xmd["mbs"]["rpms"]:
                continue
            if pkg.get_repository() and not conf.rpms_allow_repository:
                raise Forbidden(
                    "Custom component repositories aren't allowed.  "
                    "%r bears repository %r" % (pkgname, pkg.get_repository())
                )
            if pkg.get_cache() and not conf.rpms_allow_cache:
                raise Forbidden(
                    "Custom component caches aren't allowed.  "
                    "%r bears cache %r" % (pkgname, pkg.get_cache())
                )
            if pkg.get_buildafter():
                raise ValidationError('The usage of "buildafter" is not yet supported')
            if not pkg.get_repository():
                pkg.set_repository(conf.rpms_default_repository + pkgname)
            if not pkg.get_cache():
                pkg.set_cache(conf.rpms_default_cache + pkgname)
            if not pkg.get_ref():
                pkg.set_ref("master")
            if not pkg.get_arches():
                for arch in conf.arches:
                    pkg.add_restricted_arch(arch)

        # Add missing data in included modules components
        for modname in mmd.get_module_component_names():
            mod = mmd.get_module_component(modname)
            if mod.get_repository() and not conf.modules_allow_repository:
                raise Forbidden(
                    "Custom module repositories aren't allowed.  "
                    "%r bears repository %r" % (modname, mod.get_repository())
                )
            if not mod.get_repository():
                mod.set_repository(conf.modules_default_repository + modname)
            if not mod.get_ref():
                mod.set_ref("master")

        # Check that SCM URL is valid and replace potential branches in pkg refs
        # by real SCM hash and store the result to our private xmd place in modulemd.
        pool = ThreadPool(20)
        try:
            # Filter out the packages which we have already resolved in possible
            # previous runs of this method (can be caused by module build resubmition)
            # or which have custom SRPMs and shouldn't be resolved.
            pkgs_to_resolve = []
            for name in mmd.get_rpm_component_names():
                if name not in xmd["mbs"]["rpms"]:
                    if name in srpm_overrides:
                        # If this package has a custom SRPM, store an empty
                        # ref entry so no further verification takes place.
                        xmd["mbs"]["rpms"][name] = {"ref": None}
                    else:
                        pkgs_to_resolve.append(mmd.get_rpm_component(name))

            async_result = pool.map_async(_scm_get_latest, pkgs_to_resolve)

            # For modules with lot of components, the _scm_get_latest can take a lot of time.
            # We need to bump time_modified from time to time, otherwise poller could think
            # that module is stuck in "init" state and it would send fake "init" message.
            while not async_result.ready():
                async_result.wait(60)
                if module and db_session:
                    module.time_modified = datetime.utcnow()
                    db_session.commit()
            pkg_dicts = async_result.get()
        finally:
            pool.close()

        err_msg = ""
        for pkg_dict in pkg_dicts:
            if pkg_dict["error"]:
                err_msg += pkg_dict["error"] + "\n"
            else:
                pkg_name = pkg_dict["pkg_name"]
                pkg_ref = pkg_dict["pkg_ref"]
                xmd["mbs"]["rpms"][pkg_name] = {"ref": pkg_ref}
        if err_msg:
            raise UnprocessableEntity(err_msg)

    # Set the modified xmd back to the modulemd
    mmd.set_xmd(xmd)


def merge_included_mmd(mmd, included_mmd):
    """
    Merges two modulemds. This merges only metadata which are needed in
    the `main` when it includes another module defined by `included_mmd`
    """
    included_xmd = included_mmd.get_xmd()
    if "rpms" in included_xmd["mbs"]:
        xmd = mmd.get_xmd()
        if "rpms" not in xmd["mbs"]:
            xmd["mbs"]["rpms"] = included_xmd["mbs"]["rpms"]
        else:
            xmd["mbs"]["rpms"].update(included_xmd["mbs"]["rpms"])
    # Set the modified xmd back to the modulemd
    mmd.set_xmd(xmd)


def get_module_srpm_overrides(module):
    """
    Make necessary preparations to use any provided custom SRPMs.

    :param module: ModuleBuild object representing the module being submitted.
    :type module: :class:`models.ModuleBuild`
    :return: mapping of package names to SRPM links for all packages which
             have custom SRPM overrides specified
    :rtype: dict[str, str]

    """
    overrides = {}

    if not module.srpms:
        return overrides

    try:
        # Make sure we can decode the custom SRPM list
        srpms = json.loads(module.srpms)
        assert isinstance(srpms, list)
    except Exception:
        raise ValueError("Invalid srpms list encountered: {}".format(module.srpms))

    for source in srpms:
        if source.startswith("cli-build/") and source.endswith(".src.rpm"):
            # This is a custom srpm that has been uploaded to koji by rpkg
            # using the package name as the basename suffixed with .src.rpm
            rpm_name = os.path.basename(source)[: -len(".src.rpm")]
        else:
            # This should be a local custom srpm path
            if not os.path.exists(source):
                raise IOError("Provided srpm is missing: {}".format(source))
            # Get package name from rpm headers
            try:
                rpm_hdr = kobo.rpmlib.get_rpm_header(source)
                rpm_name = to_text_type(kobo.rpmlib.get_header_field(rpm_hdr, "name"))
            except Exception:
                raise ValueError("Provided srpm is invalid: {}".format(source))

        if rpm_name in overrides:
            log.warning(
                'Encountered duplicate custom SRPM "{0}" for package {1}'
                .format(source, rpm_name)
            )
            continue

        log.debug('Using custom SRPM "{0}" for package {1}'.format(source, rpm_name))
        overrides[rpm_name] = source

    return overrides


def record_component_builds(
    mmd, module, initial_batch=1, previous_buildorder=None, main_mmd=None
):
    # Imported here to allow import of utils in GenericBuilder.
    from module_build_service.builder import GenericBuilder

    # When main_mmd is set, merge the metadata from this mmd to main_mmd,
    # otherwise our current mmd is main_mmd.
    if main_mmd:
        # Check for components that are in both MMDs before merging since MBS
        # currently can't handle that situation.
        main_mmd_rpms = main_mmd.get_rpm_component_names()
        mmd_rpms = mmd.get_rpm_component_names()
        duplicate_components = [
            rpm for rpm in main_mmd_rpms
            if rpm in mmd_rpms
        ]
        if duplicate_components:
            error_msg = (
                'The included module "{0}" in "{1}" have the following '
                "conflicting components: {2}".format(
                    mmd.get_module_name(), main_mmd.get_module_name(),
                    ", ".join(duplicate_components)
                )
            )
            raise UnprocessableEntity(error_msg)
        merge_included_mmd(main_mmd, mmd)
    else:
        main_mmd = mmd

    # If the modulemd yaml specifies components, then submit them for build
    rpm_components = [
        mmd.get_rpm_component(name)
        for name in mmd.get_rpm_component_names()
    ]
    module_components = [
        mmd.get_module_component(name)
        for name in mmd.get_module_component_names()
    ]
    all_components = list(rpm_components) + list(module_components)
    if not all_components:
        return

    # Get map of packages that have SRPM overrides
    srpm_overrides = get_module_srpm_overrides(module)

    rpm_weights = GenericBuilder.get_build_weights(
        [c.get_name() for c in rpm_components]
    )
    all_components.sort(key=lambda x: x.get_buildorder())
    # We do not start with batch = 0 here, because the first batch is
    # reserved for module-build-macros. First real components must be
    # planned for batch 2 and following.
    batch = initial_batch

    for component in all_components:
        # Increment the batch number when buildorder increases.
        if previous_buildorder != component.get_buildorder():
            previous_buildorder = component.get_buildorder()
            batch += 1

        # If the component is another module, we fetch its modulemd file
        # and record its components recursively with the initial_batch
        # set to our current batch, so the components of this module
        # are built in the right global order.
        if isinstance(component, Modulemd.ComponentModule):
            full_url = component.get_repository() + "?#" + component.get_ref()
            # It is OK to whitelist all URLs here, because the validity
            # of every URL have been already checked in format_mmd(...).
            included_mmd = fetch_mmd(full_url, whitelist_url=True)[0]
            format_mmd(included_mmd, module.scmurl, module, db_session, srpm_overrides)
            batch = record_component_builds(
                included_mmd, module, batch, previous_buildorder, main_mmd)
            continue

        package = component.get_name()
        if package in srpm_overrides:
            component_ref = None
            full_url = srpm_overrides[package]
            log.info('Building custom SRPM "{0}"' " for package {1}".format(full_url, package))
        else:
            component_ref = mmd.get_xmd()["mbs"]["rpms"][package]["ref"]
            full_url = component.get_repository() + "?#" + component_ref

        # Skip the ComponentBuild if it already exists in database. This can happen
        # in case of module build resubmition.
        existing_build = models.ComponentBuild.from_component_name(db_session, package, module.id)
        if existing_build:
            # Check that the existing build has the same most important attributes.
            # This should never be a problem, but it's good to be defensive here so
            # we do not mess things during resubmition.
            if (
                existing_build.batch != batch
                or existing_build.scmurl != full_url
                or existing_build.ref != component_ref
            ):
                raise ValidationError(
                    "Component build %s of module build %s (id: %d) already "
                    "exists in database, but its attributes are different from"
                    " resubmitted one." % (
                        component.get_name(), module.name, module.id)
                )
            continue

        build = models.ComponentBuild(
            module_id=module.id,
            package=package,
            format="rpms",
            scmurl=full_url,
            batch=batch,
            ref=component_ref,
            weight=rpm_weights[package],
            buildonly=component.get_buildonly()
        )
        db_session.add(build)

    return batch
