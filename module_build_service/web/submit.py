# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import copy
from datetime import datetime
import math
import os
import re
import time

from gi.repository import GLib
import requests

from module_build_service.common import conf, log, models
from module_build_service.common.errors import Conflict, Forbidden, ValidationError
from module_build_service.common.messaging import notify_on_module_state_change
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.submit import fetch_mmd
from module_build_service.common.utils import load_mmd, mmd_to_str, to_text_type
from module_build_service.web.mse import generate_expanded_mmds
from module_build_service.web.utils import deps_to_dict


def validate_mmd(mmd):
    """Validate module metadata

    If everything is ok, just keep quiet, otherwise error is raised for
    specific problem.

    :param mmd: modulemd object representing module metadata.
    :type mmd: Modulemd.Module
    :raises Forbidden: if metadata contains module repository but it is not
        allowed.
    :raise ValidationError: if the xmd has the "mbs" key set.
    """
    for modname in mmd.get_module_component_names():
        mod = mmd.get_module_component(modname)
        if mod.get_repository() and not conf.modules_allow_repository:
            raise Forbidden(
                "Custom module repositories aren't allowed.  "
                "%r bears repository %r" % (modname, mod.get_repository())
            )

    name = mmd.get_module_name()
    xmd = mmd.get_xmd()
    if "mbs" in xmd:
        if name not in conf.allowed_privileged_module_names:
            raise ValidationError('The "mbs" xmd field is reserved for MBS')

        allowed_keys = ["disttag_marking", "koji_tag_arches"]
        for key in xmd["mbs"].keys():
            if key not in allowed_keys:
                raise ValidationError('The "mbs" xmd field is reserved for MBS')

    if name in conf.base_module_names:
        raise ValidationError(
            'You cannot build a module named "{}" since it is a base module'.format(name))


def get_prefixed_version(mmd):
    """
    Return the prefixed version of the module based on the buildrequired base module stream.

    :param mmd: the Modulemd.ModuleStream object to format
    :return: the prefixed version
    :rtype: int
    """
    xmd = mmd.get_xmd()
    version = mmd.get_version()

    base_module_stream = None
    for base_module in conf.base_module_names:
        try:
            base_module_stream = xmd["mbs"]["buildrequires"].get(base_module, {}).get("stream")
            if base_module_stream:
                # Break after finding the first base module that is buildrequired
                break
        except KeyError:
            log.warning("The module's mmd is missing information in the xmd section")
            return version
    else:
        log.warning(
            "This module does not buildrequire a base module ({0})".format(
                " or ".join(conf.base_module_names)
            )
        )
        return version

    # The platform version (e.g. prefix1.2.0 => 010200)
    version_prefix = models.ModuleBuild.get_stream_version(base_module_stream, right_pad=False)

    if version_prefix is None:
        log.warning(
            'The "{0}" stream "{1}" couldn\'t be used to prefix the module\'s '
            "version".format(base_module, base_module_stream)
        )
        return version

    # Strip the stream suffix because Modulemd requires version to be an integer
    new_version = int(str(int(math.floor(version_prefix))) + str(version))
    if new_version > GLib.MAXUINT64:
        log.warning(
            'The "{0}" stream "{1}" caused the module\'s version prefix to be '
            "too long".format(base_module, base_module_stream)
        )
        return version
    return new_version


def submit_module_build_from_yaml(
    db_session, username, handle, params, stream=None, skiptests=False
):
    yaml_file = to_text_type(handle.read())
    mmd = load_mmd(yaml_file)
    dt = datetime.utcfromtimestamp(int(time.time()))
    if hasattr(handle, "filename"):
        def_name = str(os.path.splitext(os.path.basename(handle.filename))[0])
    elif not mmd.get_module_name():
        raise ValidationError(
            "The module's name was not present in the modulemd file. Please use the "
            '"module_name" parameter'
        )
    def_version = int(dt.strftime("%Y%m%d%H%M%S"))
    module_name = mmd.get_module_name() or def_name
    module_stream = stream or mmd.get_stream_name() or "master"
    if module_name != mmd.get_module_name() or module_stream != mmd.get_stream_name():
        # This is how you set the name and stream in the modulemd
        mmd = mmd.copy(module_name, module_stream)
    mmd.set_version(mmd.get_version() or def_version)
    if skiptests:
        buildopts = mmd.get_buildopts() or Modulemd.Buildopts()
        macros = buildopts.get_rpm_macros() or ""
        buildopts.set_rpm_macros(macros + "\n\n%__spec_check_pre exit 0\n")
        mmd.set_buildopts(buildopts)
    return submit_module_build(db_session, username, mmd, params)


_url_check_re = re.compile(r"^[^:/]+:.*$")


def submit_module_build_from_scm(db_session, username, params, allow_local_url=False):
    url = params["scmurl"]
    branch = params["branch"]
    # Translate local paths into file:// URL
    if allow_local_url and not _url_check_re.match(url):
        log.info("'{}' is not a valid URL, assuming local path".format(url))
        url = os.path.abspath(url)
        url = "file://" + url
    mmd, scm = fetch_mmd(url, branch, allow_local_url)

    return submit_module_build(db_session, username, mmd, params)


def _apply_dep_overrides(mmd, params):
    """
    Apply the dependency override parameters (if specified) on the input modulemd.

    :param Modulemd.ModuleStream mmd: the modulemd to apply the overrides on
    :param dict params: the API parameters passed in by the user
    :raises ValidationError: if one of the overrides doesn't apply
    """
    dep_overrides = {
        "buildrequires": copy.copy(params.get("buildrequire_overrides", {})),
        "requires": copy.copy(params.get("require_overrides", {})),
    }

    # Parse the module's branch to determine if it should override the stream of the buildrequired
    # module defined in conf.br_stream_override_module
    branch_search = None
    if params.get("branch") and conf.br_stream_override_module and conf.br_stream_override_regexes:
        # Only parse the branch for a buildrequire override if the user didn't manually specify an
        # override for the module specified in conf.br_stream_override_module
        if not dep_overrides["buildrequires"].get(conf.br_stream_override_module):
            branch_search = None
            for regex in conf.br_stream_override_regexes:
                branch_search = re.search(regex, params["branch"])
                if branch_search:
                    log.debug(
                        "The stream override regex `%s` matched the branch %s",
                        regex,
                        params["branch"],
                    )
                    break
            else:
                log.debug('No stream override regexes matched the branch "%s"', params["branch"])

    # If a stream was parsed from the branch, then add it as a stream override for the module
    # specified in conf.br_stream_override_module
    if branch_search:
        # Concatenate all the groups that are not None together to get the desired stream.
        # This approach is taken in case there are sections to ignore.
        # For instance, if we need to parse `el8.0.0` from `rhel-8.0.0`.
        parsed_stream = "".join(group for group in branch_search.groups() if group)
        if parsed_stream:
            dep_overrides["buildrequires"][conf.br_stream_override_module] = [parsed_stream]
            log.info(
                'The buildrequired stream of "%s" was overriden with "%s" based on the branch "%s"',
                conf.br_stream_override_module, parsed_stream, params["branch"],
            )
        else:
            log.warning(
                'The regex `%s` only matched empty capture groups on the branch "%s". The regex is '
                " invalid and should be rewritten.",
                regex, params["branch"],
            )

    unused_dep_overrides = {
        "buildrequires": set(dep_overrides["buildrequires"].keys()),
        "requires": set(dep_overrides["requires"].keys()),
    }

    deps = mmd.get_dependencies()
    for dep in deps:
        overridden = False
        new_dep = Modulemd.Dependencies()
        for dep_type, overrides in dep_overrides.items():
            if dep_type == "buildrequires":
                mmd_dep_type = "buildtime"
            else:
                mmd_dep_type = "runtime"
            # Get the existing streams
            reqs = deps_to_dict(dep, mmd_dep_type)
            # Get the method to add a new stream for this dependency type
            # (e.g. add_buildtime_stream)
            add_func = getattr(new_dep, "add_{}_stream".format(mmd_dep_type))
            add_empty_func = getattr(
                new_dep, "set_empty_{}_dependencies_for_module".format(mmd_dep_type))
            for name, streams in reqs.items():
                if name in dep_overrides[dep_type]:
                    streams_to_add = dep_overrides[dep_type][name]
                    unused_dep_overrides[dep_type].remove(name)
                    overridden = True
                else:
                    streams_to_add = reqs[name]

                if not streams_to_add:
                    add_empty_func(name)
                else:
                    for stream in streams_to_add:
                        add_func(name, stream)
        if overridden:
            # Set the overridden streams
            mmd.remove_dependencies(dep)
            mmd.add_dependencies(new_dep)

    for dep_type in unused_dep_overrides.keys():
        # If a stream override was applied from parsing the branch and it wasn't applicable,
        # just ignore it
        if branch_search and conf.br_stream_override_module in unused_dep_overrides[dep_type]:
            unused_dep_overrides[dep_type].remove(conf.br_stream_override_module)
        if unused_dep_overrides[dep_type]:
            raise ValidationError(
                "The {} overrides for the following modules aren't applicable: {}".format(
                    dep_type[:-1], ", ".join(sorted(unused_dep_overrides[dep_type])))
            )


def _modify_buildtime_streams(db_session, mmd, new_streams_func):
    """
    Modify buildtime streams using the input new_streams_func.

    :param Modulemd.ModuleStream mmd: the modulemd to apply the overrides on
    :param function new_streams: a function that takes the parameters (module_name, module_streams),
        and returns the streams that should be set on the buildtime dependency.
    """
    deps = mmd.get_dependencies()
    for dep in deps:
        overridden = False
        brs = deps_to_dict(dep, "buildtime")
        # There is no way to replace streams, so create a new Dependencies object that will end up
        # being a copy, but with the streams replaced if a virtual stream is detected
        new_dep = Modulemd.Dependencies()

        for name, streams in brs.items():
            new_streams = new_streams_func(db_session, name, streams)
            if streams != new_streams:
                overridden = True

            if not new_streams:
                new_dep.set_empty_buildtime_dependencies_for_module(name)
            else:
                for stream in new_streams:
                    new_dep.add_buildtime_stream(name, stream)

        if overridden:
            # Copy the runtime streams as is
            reqs = deps_to_dict(dep, "runtime")
            for name, streams in reqs.items():
                if not streams:
                    new_dep.set_empty_runtime_dependencies_for_module(name)
                else:
                    for stream in streams:
                        new_dep.add_runtime_stream(name, stream)
            # Replace the old Dependencies object with the new one with the overrides
            mmd.remove_dependencies(dep)
            mmd.add_dependencies(new_dep)


def resolve_base_module_virtual_streams(db_session, name, streams):
    """
    Resolve any base module virtual streams and return a copy of `streams` with the resolved values.

    :param str name: the module name
    :param str streams: the streams to resolve
    :return: the resolved streams
    :rtype: list
    """
    from module_build_service.resolver import GenericResolver
    resolver = GenericResolver.create(db_session, conf)

    if name not in conf.base_module_names:
        return streams

    new_streams = copy.deepcopy(streams)
    for i, stream in enumerate(streams):
        # Ignore streams that start with a minus sign, since those are handled in the
        # MSE code
        if stream.startswith("-"):
            continue

        # Check if the base module stream is available
        log.debug('Checking to see if the base module "%s:%s" is available', name, stream)
        if resolver.get_module_count(name=name, stream=stream) > 0:
            continue

        # If the base module stream is not available, check if there's a virtual stream
        log.debug(
            'Checking to see if there is a base module "%s" with the virtual stream "%s"',
            name, stream,
        )
        base_module_mmd = resolver.get_latest_with_virtual_stream(
            name=name, virtual_stream=stream
        )
        if not base_module_mmd:
            # If there isn't this base module stream or virtual stream available, skip it,
            # and let the dep solving code deal with it like it normally would
            log.warning(
                'There is no base module "%s" with stream/virtual stream "%s"',
                name, stream,
            )
            continue

        latest_stream = base_module_mmd.get_stream_name()
        log.info(
            'Replacing the buildrequire "%s:%s" with "%s:%s", since "%s" is a virtual '
            "stream",
            name, stream, name, latest_stream, stream
        )
        new_streams[i] = latest_stream

    return new_streams


def _process_support_streams(db_session, mmd, params):
    """
    Check if any buildrequired base modules require a support stream suffix.

    This checks the Red Hat Product Pages to see if the buildrequired base module stream has been
    released, if yes, then add the appropriate stream suffix.

    :param Modulemd.ModuleStream mmd: the modulemd to apply the overrides on
    :param dict params: the API parameters passed in by the user
    """
    config_msg = (
        'Skipping the release date checks for adding a stream suffix since "%s" '
        "is not configured"
    )
    if not conf.product_pages_url:
        log.debug(config_msg, "product_pages_url")
        return
    elif not conf.product_pages_module_streams:
        log.debug(config_msg, "product_pages_module_streams")
        return

    buildrequire_overrides = params.get("buildrequire_overrides", {})

    def is_released_as_per_schedule(pp_release):
        """
        Check if the specified scheduled task date has been reached. Returns True if it has.
        """
        if not conf.product_pages_schedule_task_name:
            log.debug(config_msg, "product_pages_schedule_task_name")
            return False

        schedule_url = "{}/api/v7/releases/{}/schedule-tasks/?fields=name,date_finish".format(
            conf.product_pages_url.rstrip("/"), pp_release)

        try:
            pp_rv = requests.get(schedule_url, timeout=15)
            pp_json = pp_rv.json()
            # Catch requests failures and JSON parsing errors
        except (requests.exceptions.RequestException, ValueError):
            log.exception(
                "The query to the Product Pages at %s failed. Assuming it is not available.",
                schedule_url,
            )
            return False

        name = conf.product_pages_schedule_task_name.lower().strip()
        for task in pp_json:
            if task['name'].lower().strip() == name:
                task_date = task['date_finish']
                if datetime.strptime(task_date, "%Y-%m-%d").date() >= datetime.utcnow().date():
                    log.debug(
                        "The task date %s hasn't been reached yet. Not adding a stream suffix.",
                        task_date
                    )
                    return False
                return True
        # Schedule task not available; rely on GA date
        return False

    def is_released(pp_release, url):
        """
        Check if the stream has been released. Return True if it has.
        """
        try:
            pp_rv = requests.get(url, timeout=15)
            pp_json = pp_rv.json()
        # Catch requests failures and JSON parsing errors
        except (requests.exceptions.RequestException, ValueError):
            log.exception(
                "The query to the Product Pages at %s failed. Assuming it is not yet released.",
                url,
            )
            return False

        ga_date = pp_json.get("ga_date")
        if not ga_date:
            log.debug("A release date for the release %s could not be determined", pp_release)
            return False

        if datetime.strptime(ga_date, "%Y-%m-%d").date() >= datetime.utcnow().date():
            log.debug(
                "The release %s hasn't been released yet. Not adding a stream suffix.",
                ga_date
            )
            return False
        return True

    def new_streams_func(db_session, name, streams):
        if name not in conf.base_module_names:
            log.debug("The module %s is not a base module. Skipping the release date check.", name)
            return streams
        elif name in buildrequire_overrides:
            log.debug(
                "The module %s is a buildrequire override. Skipping the release date check.", name)
            return streams

        new_streams = copy.deepcopy(streams)
        for i, stream in enumerate(streams):
            for regex, values in conf.product_pages_module_streams.items():
                if re.match(regex, stream):
                    log.debug(
                        'The regex `%s` from the configuration "product_pages_module_streams" '
                        "matched the stream %s",
                        regex, stream,
                    )
                    stream_suffix, pp_release_template, pp_major_release_template = values
                    break
            else:
                log.debug(
                    'No regexes in the configuration "product_pages_module_streams" matched the '
                    "stream %s. Skipping the release date check for this stream.",
                    stream,
                )
                continue

            if stream.endswith(stream_suffix):
                log.debug(
                    'The stream %s already contains the stream suffix of "%s". Skipping the '
                    "release date check.",
                    stream, stream_suffix
                )
                continue

            stream_version = models.ModuleBuild.get_stream_version(stream)
            if not stream_version:
                log.debug("A stream version couldn't be parsed from %s", stream)
                continue

            # Convert the stream_version float to an int to make the math below deal with only
            # integers
            stream_version_int = int(stream_version)
            # For example 80000 => 8
            x = stream_version_int // 10000
            # For example 80100 => 1
            y = (stream_version_int - x * 10000) // 100
            # For example 80104 => 4
            z = stream_version_int - x * 10000 - y * 100
            # Check if the stream version is x.0.0
            if stream_version_int % 10000 == 0 and pp_major_release_template:
                # For example, el8.0.0 => rhel-8-0
                pp_release = pp_major_release_template.format(x=x, y=y, z=z)
            else:
                # For example el8.0.1 => rhel-8-0.1
                pp_release = pp_release_template.format(x=x, y=y, z=z)

            url = "{}/api/v7/releases/{}/?fields=ga_date".format(
                conf.product_pages_url.rstrip("/"), pp_release)

            if is_released_as_per_schedule(pp_release):
                new_stream = stream + stream_suffix
                log.info(
                    'Replacing the buildrequire "%s:%s" with "%s:%s", since the date is met',
                    name, stream, name, new_stream
                )
                new_streams[i] = new_stream
            elif is_released(pp_release, url):
                new_stream = stream + stream_suffix
                log.info(
                    'Replacing the buildrequire "%s:%s" with "%s:%s", since the stream is released',
                    name, stream, name, new_stream
                )
                new_streams[i] = new_stream

        return new_streams

    _modify_buildtime_streams(db_session, mmd, new_streams_func)


def submit_module_build(db_session, username, mmd, params):
    """
    Submits new module build.

    :param db_session: SQLAlchemy session object.
    :param str username: Username of the build's owner.
    :param Modulemd.ModuleStream mmd: Modulemd defining the build.
    :param dict params: the API parameters passed in by the user
    :rtype: list with ModuleBuild
    :return: List with submitted module builds.
    """
    log.debug(
        "Submitted %s module build for %s:%s:%s",
        ("scratch" if params.get("scratch", False) else "normal"),
        mmd.get_module_name(),
        mmd.get_stream_name(),
        mmd.get_version(),
    )
    validate_mmd(mmd)

    raise_if_stream_ambigous = False
    default_streams = {}
    # For local builds, we want the user to choose the exact stream using the default_streams
    # in case there are multiple streams to choose from and raise an exception otherwise.
    if "local_build" in params:
        raise_if_stream_ambigous = True
    # Get the default_streams if set.
    if "default_streams" in params:
        default_streams = params["default_streams"]
    _apply_dep_overrides(mmd, params)
    _modify_buildtime_streams(db_session, mmd, resolve_base_module_virtual_streams)
    _process_support_streams(db_session, mmd, params)

    mmds = generate_expanded_mmds(db_session, mmd, raise_if_stream_ambigous, default_streams)
    if not mmds:
        raise ValidationError(
            "No dependency combination was satisfied. Please verify the "
            "buildrequires in your modulemd have previously been built."
        )
    modules = []

    # True if all module builds are skipped so MBS will actually not rebuild
    # anything. To keep the backward compatibility, we need to raise an exception
    # later in the end of this method.
    all_modules_skipped = True

    for mmd in mmds:
        # Prefix the version of the modulemd based on the base module it buildrequires
        version = get_prefixed_version(mmd)
        mmd.set_version(version)
        nsvc = mmd.get_nsvc()

        log.debug("Checking whether module build already exists: %s.", nsvc)
        module = models.ModuleBuild.get_build_from_nsvc(db_session, *nsvc.split(":"))
        if module and not params.get("scratch", False):
            if module.state != models.BUILD_STATES["failed"]:
                log.info(
                    "Skipping rebuild of %s, only rebuild of modules in failed state is allowed.",
                    nsvc,
                )
                modules.append(module)
                continue

            rebuild_strategy = params.get("rebuild_strategy")
            if rebuild_strategy and module.rebuild_strategy != rebuild_strategy:
                raise ValidationError(
                    'You cannot change the module\'s "rebuild_strategy" when '
                    "resuming a module build"
                )

            log.debug("Resuming existing module build %r" % module)
            # Reset all component builds that didn't complete
            for component in module.component_builds:
                if not component.is_waiting_for_build and not component.is_completed:
                    component.state = None
                    component.state_reason = None
                    db_session.add(component)
            module.username = username
            prev_state = module.previous_non_failed_state
            if prev_state == models.BUILD_STATES["init"]:
                transition_to = models.BUILD_STATES["init"]
            else:
                transition_to = models.BUILD_STATES["wait"]
                module.batch = 0
            module.transition(db_session, conf, transition_to, "Resubmitted by %s" % username)
            db_session.commit()
            log.info("Resumed existing module build in previous state %s" % module.state)
        else:
            # make NSVC unique for every scratch build
            context_suffix = ""
            if params.get("scratch", False):
                log.debug("Checking for existing scratch module builds by NSVC")
                scrmods = models.ModuleBuild.get_scratch_builds_from_nsvc(
                    db_session, *nsvc.split(":"))
                scrmod_contexts = [scrmod.context for scrmod in scrmods]
                log.debug(
                    "Found %d previous scratch module build context(s): %s",
                    len(scrmods), ",".join(scrmod_contexts),
                )
                # append incrementing counter to context
                context_suffix = "_" + str(len(scrmods) + 1)
                mmd.set_context(mmd.get_context() + context_suffix)
            else:
                # In case the branch is defined, check whether user is allowed to submit
                # non-scratch build from this branch. Note that the branch is always defined
                # for official builds from SCM, because it is requested in views.py.
                branch = params.get("branch")
                if branch:
                    for regex in conf.scratch_build_only_branches:
                        branch_search = re.search(regex, branch)
                        if branch_search:
                            raise ValidationError(
                                "Only scratch module builds can be built from this branch."
                            )

            log.debug("Creating new module build")
            module = models.ModuleBuild.create(
                db_session,
                conf,
                name=mmd.get_module_name(),
                stream=mmd.get_stream_name(),
                version=str(mmd.get_version()),
                modulemd=mmd_to_str(mmd),
                scmurl=params.get("scmurl"),
                username=username,
                rebuild_strategy=params.get("rebuild_strategy"),
                reused_module_id=params.get("reuse_components_from"),
                scratch=params.get("scratch"),
                srpms=params.get("srpms"),
            )
            module.build_context, module.runtime_context, module.context, \
                module.build_context_no_bms = module.contexts_from_mmd(module.modulemd)
            module.context += context_suffix
            db_session.commit()

            notify_on_module_state_change(
                # Note the state is "init" here...
                module.json(db_session, show_tasks=False)
            )

        all_modules_skipped = False
        modules.append(module)
        log.info('The user "%s" submitted the build "%s"', username, nsvc)

    if all_modules_skipped:
        err_msg = (
            "Module (state=%s) already exists. Only a new build, resubmission of "
            "a failed build or build against new buildrequirements is "
            "allowed." % module.state
        )
        log.error(err_msg)
        raise Conflict(err_msg)

    return modules
