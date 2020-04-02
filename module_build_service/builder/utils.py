# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import errno
import hashlib
import logging
import inspect
from multiprocessing.dummy import Pool as ThreadPool
import os
import shutil
import subprocess

import munch
import requests

from module_build_service.common import conf, log, models
from module_build_service.common.errors import ValidationError, ProgrammingError


logging.basicConfig(level=logging.DEBUG)


def find_srpm(cod):
    for f in os.listdir(cod):
        if f.endswith(".src.rpm"):
            return os.path.join(cod, f)


def execute_cmd(args, stdout=None, stderr=None, cwd=None):
    """
    Executes command defined by `args`. If `stdout` or `stderr` is set to
    Python file object, the stderr/stdout output is redirecter to that file.
    If `cwd` is set, current working directory is set accordingly for the
    executed command.

    :param args: list defining the command to execute.
    :param stdout: Python file object to redirect the stdout to.
    :param stderr: Python file object to redirect the stderr to.
    :param cwd: string defining the current working directory for command.
    :raises RuntimeError: Raised when command exits with non-zero exit code.
    """
    out_log_msg = ""
    if stdout and hasattr(stdout, "name"):
        out_log_msg += ", stdout log: %s" % stdout.name
    if stderr and hasattr(stderr, "name"):
        out_log_msg += ", stderr log: %s" % stderr.name

    log.info("Executing the command \"%s\"%s" % (" ".join(args), out_log_msg))
    proc = subprocess.Popen(args, stdout=stdout, stderr=stderr, cwd=cwd)
    out, err = proc.communicate()

    if proc.returncode != 0:
        err_msg = "Command '%s' returned non-zero value %d%s" % (args, proc.returncode, out_log_msg)
        raise RuntimeError(err_msg)
    return out, err


def get_koji_config(mbs_config):
    """
    Get the Koji config needed for MBS
    :param mbs_config: an MBS config object
    :return: a Munch object of the Koji config
    """
    # Placed here to avoid py2/py3 conflicts...
    import koji

    koji_config = munch.Munch(
        koji.read_config(profile_name=mbs_config.koji_profile, user_config=mbs_config.koji_config))
    # Timeout after 10 minutes.  The default is 12 hours.
    koji_config["timeout"] = 60 * 10
    return koji_config


def create_local_repo_from_koji_tag(config, tag, repo_dir, archs=None):
    """
    Downloads the packages build for one of `archs` (defaults to ['x86_64',
    'noarch']) in Koji tag `tag` to `repo_dir` and creates repository in that
    directory. Needs config.koji_profile and config.koji_config to be set.

    If the there are no builds associated with the tag, False is returned.
    """

    # Placed here to avoid py2/py3 conflicts...
    import koji

    if not archs:
        archs = ["x86_64", "noarch"]

    # Load koji config and create Koji session.
    koji_config = get_koji_config(config)
    address = koji_config.server
    log.info("Connecting to koji %r" % address)
    session = koji.ClientSession(address, opts=koji_config)

    # Get the list of all RPMs and builds in a tag.
    try:
        rpms, builds = session.listTaggedRPMS(tag, latest=True)
    except koji.GenericError:
        log.exception("Failed to list rpms in tag %r" % tag)

    if not builds:
        log.debug("No builds are associated with the tag %r", tag)
        return False

    # Reformat builds so they are dict with build_id as a key.
    builds = {build["build_id"]: build for build in builds}

    # Prepare pathinfo we will use to generate the URL.
    pathinfo = koji.PathInfo(topdir=session.opts["topurl"])

    # When True, we want to run the createrepo_c.
    repo_changed = False

    # Prepare the list of URLs to download
    download_args = []
    for rpm in rpms:
        build_info = builds[rpm["build_id"]]

        # We do not download debuginfo packages or packages built for archs
        # we are not interested in.
        if koji.is_debuginfo(rpm["name"]) or not rpm["arch"] in archs:
            continue

        fname = pathinfo.rpm(rpm)
        relpath = os.path.basename(fname)
        local_fn = os.path.join(repo_dir, relpath)
        # Download only when the RPM is not downloaded or the size does not match.
        if not os.path.exists(local_fn) or os.path.getsize(local_fn) != rpm["size"]:
            if os.path.exists(local_fn):
                os.remove(local_fn)
            repo_changed = True
            url = pathinfo.build(build_info) + "/" + fname
            download_args.append((url, local_fn))

    log.info("Downloading %d packages from Koji tag %s to %s" % (len(download_args), tag, repo_dir))

    # Create the output directory
    try:
        os.makedirs(repo_dir)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise

    def _download_file(url_and_dest):
        """
        Download a file in a memory efficient manner
        :param url_and_dest: a tuple containing the URL and the destination to download to
        :return: None
        """
        log.info("Downloading {0}...".format(url_and_dest[0]))
        if len(url_and_dest) != 2:
            raise ValueError("url_and_dest must have two values")

        rv = requests.get(url_and_dest[0], stream=True, timeout=60)
        with open(url_and_dest[1], "wb") as f:
            for chunk in rv.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)

    # Download the RPMs four at a time.
    pool = ThreadPool(4)
    try:
        pool.map(_download_file, download_args)
    finally:
        pool.close()

    # If we downloaded something, run the createrepo_c.
    if repo_changed:
        repodata_path = os.path.join(repo_dir, "repodata")
        if os.path.exists(repodata_path):
            shutil.rmtree(repodata_path)

        log.info("Creating local repository in %s" % repo_dir)
        execute_cmd(["/usr/bin/createrepo_c", repo_dir])

    return True


def get_rpm_release(db_session, module_build):
    """
    Generates the dist tag for the specified module

    :param db_session: SQLAlchemy session object.
    :param module_build: a models.ModuleBuild object
    :return: a string of the module's dist tag
    """
    dist_str = ".".join([
        module_build.name,
        module_build.stream,
        str(module_build.version),
        str(module_build.context),
    ]).encode("utf-8")
    dist_hash = hashlib.sha1(dist_str).hexdigest()[:8]

    # We need to share the same auto-incrementing index in dist tag between all MSE builds.
    # We can achieve that by using the lowest build ID of all the MSE siblings including
    # this module build.
    mse_build_ids = module_build.siblings(db_session) + [module_build.id or 0]
    mse_build_ids.sort()
    index = mse_build_ids[0]
    try:
        buildrequires = module_build.mmd().get_xmd()["mbs"]["buildrequires"]
    except (ValueError, KeyError):
        log.warning(
            "Module build {0} does not have buildrequires in its xmd".format(module_build.id))
        buildrequires = None

    # Determine which buildrequired module will influence the disttag
    br_module_marking = ""
    # If the buildrequires are recorded in the xmd then we can try to find the base module that
    # is buildrequired
    if buildrequires:
        # Looping through all the non-base modules that are allowed to set the disttag_marking
        # and the base modules to see what the disttag marking should be. Doing it this way
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

            try:
                marking = module_obj.mmd().get_xmd()["mbs"]["disttag_marking"]
            # We must check for a KeyError because a Variant object doesn't support the `get`
            # method
            except KeyError:
                if module not in conf.base_module_names:
                    continue
                # If we've made it past all the modules in
                # conf.allowed_privileged_module_names, and the base module doesn't have
                # the disttag_marking set, then default to the stream of the first base module
                marking = module_obj.stream
            br_module_marking = marking + "+"
            break
        else:
            log.warning(
                "Module build {0} does not buildrequire a base module ({1})".format(
                    module_build.id, " or ".join(conf.base_module_names))
            )

    # use alternate prefix for scratch module build components so they can be identified
    prefix = "scrmod+" if module_build.scratch else conf.default_dist_tag_prefix

    return "{prefix}{base_module_marking}{index}+{dist_hash}".format(
        prefix=prefix, base_module_marking=br_module_marking, index=index, dist_hash=dist_hash
    )


def validate_koji_tag(tag_arg_names, pre="", post="-", dict_key="name"):
    """
    Used as a decorator validates koji tag arg(s)' value(s)
    against configurable list of koji tag prefixes.
    Supported arg value types are: dict, list, str

    :param tag_arg_names: Str or list of parameters to validate.
    :param pre: Prepend this optional string (e.g. '.' in case of disttag
    validation) to each koji tag prefix.
    :param post: Append this string/delimiter ('-' by default) to each koji
    tag prefix.
    :param dict_key: In case of a dict arg, inspect this key ('name' by default).
    """

    if not isinstance(tag_arg_names, list):
        tag_arg_names = [tag_arg_names]

    def validation_decorator(function):
        def wrapper(*args, **kwargs):
            call_args = inspect.getcallargs(function, *args, **kwargs)

            # if module name is in allowed_privileged_module_names or base_module_names lists
            # we don't have to validate it since they could use an arbitrary Koji tag
            try:
                if call_args['self'].module_str in \
                        conf.allowed_privileged_module_names + conf.base_module_names:
                    # skip validation
                    return function(*args, **kwargs)
            except (AttributeError, KeyError):
                pass

            for tag_arg_name in tag_arg_names:
                err_subject = "Koji tag validation:"

                # If any of them don't appear in the function, then fail.
                if tag_arg_name not in call_args:
                    raise ProgrammingError(
                        "{} Inspected argument {} is not within function args."
                        " The function was: {}.".format(
                            err_subject, tag_arg_name, function.__name__
                        )
                    )

                tag_arg_val = call_args[tag_arg_name]

                # First, check that we have some value
                if not tag_arg_val:
                    raise ValidationError(
                        "{} Can not validate {}. No value provided.".format(
                            err_subject, tag_arg_name)
                    )

                # If any of them are a dict, then use the provided dict_key
                if isinstance(tag_arg_val, dict):
                    if dict_key not in tag_arg_val:
                        raise ProgrammingError(
                            "{} Inspected dict arg {} does not contain {} key."
                            " The function was: {}.".format(
                                err_subject, tag_arg_name, dict_key, function.__name__)
                        )
                    tag_list = [tag_arg_val[dict_key]]
                elif isinstance(tag_arg_val, list):
                    tag_list = tag_arg_val
                else:
                    tag_list = [tag_arg_val]

                # Check to make sure the provided values match our whitelist.
                for allowed_prefix in conf.koji_tag_prefixes:
                    if all([t.startswith(pre + allowed_prefix + post) for t in tag_list]):
                        break
                else:
                    # Only raise this error if the given tags don't start with
                    # *any* of our allowed prefixes.
                    raise ValidationError(
                        "Koji tag validation: {} does not satisfy any of allowed prefixes: {}"
                        .format(tag_list, [pre + p + post for p in conf.koji_tag_prefixes])
                    )

            # Finally.. after all that validation, call the original function
            # and return its value.
            return function(*args, **kwargs)

        # We're replacing the original function with our synthetic wrapper,
        # but dress it up to make it look more like the original function.
        wrapper.__name__ = function.__name__
        wrapper.__doc__ = function.__doc__
        return wrapper

    return validation_decorator
