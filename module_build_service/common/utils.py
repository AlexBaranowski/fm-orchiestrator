# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime
from functools import partial
import os

from gi.repository.GLib import Error as ModuleMDError
from six import string_types, text_type

from module_build_service.common import conf, log
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.modulemd import Modulemd


def to_text_type(s):
    """
    Converts `s` to `text_type`. In case it fails, returns `s`.
    """
    try:
        return text_type(s, "utf-8")
    except TypeError:
        return s


def load_mmd(yaml, is_file=False):
    if not yaml:
        raise UnprocessableEntity('The input modulemd was empty')

    target_mmd_version = Modulemd.ModuleStreamVersionEnum.TWO
    try:
        if is_file:
            mmd = Modulemd.ModuleStream.read_file(yaml, True)
        else:
            mmd = Modulemd.ModuleStream.read_string(to_text_type(yaml), True)
        mmd.validate()
        if mmd.get_mdversion() < target_mmd_version:
            mmd = mmd.upgrade(target_mmd_version)
        elif mmd.get_mdversion() > target_mmd_version:
            log.error("Encountered a modulemd file with the version %d", mmd.get_mdversion())
            raise UnprocessableEntity(
                "The modulemd version cannot be greater than {}".format(target_mmd_version))
    except ModuleMDError as e:
        not_found = False
        if is_file:
            error = "The modulemd {} is invalid.".format(os.path.basename(yaml))
            if os.path.exists(yaml):
                with open(yaml, "rt") as yaml_hdl:
                    log.debug("Modulemd that failed to load:\n%s", yaml_hdl.read())
            else:
                not_found = True
                error = "The modulemd file {} was not found.".format(os.path.basename(yaml))
                log.error("The modulemd file %s was not found.", yaml)
        else:
            error = "The modulemd is invalid."
            log.debug("Modulemd that failed to load:\n%s", yaml)

        if "modulemd-error-quark: " in str(e):
            error = "{} The error was '{}'.".format(
                error, str(e).split("modulemd-error-quark: ")[-1])
        elif "Unknown ModuleStream version" in str(e):
            error = (
                "{}. The modulemd version can't be greater than {}."
                .format(error, target_mmd_version)
            )
        elif not_found is False:
            error = "{} Please verify the syntax is correct.".format(error)

        log.exception(error)
        raise UnprocessableEntity(error)

    return mmd


load_mmd_file = partial(load_mmd, is_file=True)


def import_mmd(db_session, mmd, check_buildrequires=True):
    """
    Imports new module build defined by `mmd` to MBS database using `session`.
    If it already exists, it is updated.

    The ModuleBuild.koji_tag is set according to xmd['mbs]['koji_tag'].
    The ModuleBuild.state is set to "ready".
    The ModuleBuild.rebuild_strategy is set to "all".
    The ModuleBuild.owner is set to "mbs_import".

    :param db_session: SQLAlchemy session object.
    :param mmd: module metadata being imported into database.
    :type mmd: Modulemd.ModuleStream
    :param bool check_buildrequires: When True, checks that the buildrequires defined in the MMD
        have matching records in the `mmd["xmd"]["mbs"]["buildrequires"]` and also fills in
        the `ModuleBuild.buildrequires` according to this data.
    :return: module build (ModuleBuild),
             log messages collected during import (list)
    :rtype: tuple
    """
    from module_build_service.common import models

    xmd = mmd.get_xmd()
    # Set some defaults in xmd["mbs"] if they're not provided by the user
    if "mbs" not in xmd:
        xmd["mbs"] = {"mse": True}

    if not mmd.get_context():
        mmd.set_context(models.DEFAULT_MODULE_CONTEXT)

    # NSVC is used for logging purpose later.
    nsvc = mmd.get_nsvc()
    if nsvc is None:
        msg = "Both the name and stream must be set for the modulemd being imported."
        log.error(msg)
        raise UnprocessableEntity(msg)

    name = mmd.get_module_name()
    stream = mmd.get_stream_name()
    version = str(mmd.get_version())
    context = mmd.get_context()

    xmd_mbs = xmd["mbs"]

    disttag_marking = xmd_mbs.get("disttag_marking")

    # If it is a base module, then make sure the value that will be used in the RPM disttags
    # doesn't contain a dash since a dash isn't allowed in the release field of the NVR
    if name in conf.base_module_names:
        if disttag_marking and "-" in disttag_marking:
            msg = "The disttag_marking cannot contain a dash"
            log.error(msg)
            raise UnprocessableEntity(msg)
        if not disttag_marking and "-" in stream:
            msg = "The stream cannot contain a dash unless disttag_marking is set"
            log.error(msg)
            raise UnprocessableEntity(msg)

    virtual_streams = xmd_mbs.get("virtual_streams", [])

    # Verify that the virtual streams are the correct type
    if virtual_streams and (
        not isinstance(virtual_streams, list)
        or any(not isinstance(vs, string_types) for vs in virtual_streams)
    ):
        msg = "The virtual streams must be a list of strings"
        log.error(msg)
        raise UnprocessableEntity(msg)

    if check_buildrequires:
        deps = mmd.get_dependencies()
        if len(deps) > 1:
            raise UnprocessableEntity(
                "The imported module's dependencies list should contain just one element")

        if "buildrequires" not in xmd_mbs:
            # Always set buildrequires if it is not there, because
            # get_buildrequired_base_modules requires xmd/mbs/buildrequires exists.
            xmd_mbs["buildrequires"] = {}
            mmd.set_xmd(xmd)

        if len(deps) > 0:
            brs = set(deps[0].get_buildtime_modules())
            xmd_brs = set(xmd_mbs["buildrequires"].keys())
            if brs - xmd_brs:
                raise UnprocessableEntity(
                    "The imported module buildrequires other modules, but the metadata in the "
                    'xmd["mbs"]["buildrequires"] dictionary is missing entries'
                )

    if "koji_tag" not in xmd_mbs:
        log.warning("'koji_tag' is not set in xmd['mbs'] for module {}".format(nsvc))
        log.warning("koji_tag will be set to None for imported module build.")

    # Log messages collected during import
    msgs = []

    # Get the ModuleBuild from DB.
    build = models.ModuleBuild.get_build_from_nsvc(db_session, name, stream, version, context)
    if build:
        msg = "Updating existing module build {}.".format(nsvc)
        log.info(msg)
        msgs.append(msg)
    else:
        build = models.ModuleBuild()
        db_session.add(build)

    build.name = name
    build.stream = stream
    build.version = version
    build.koji_tag = xmd_mbs.get("koji_tag")
    build.state = models.BUILD_STATES["ready"]
    build.modulemd = mmd_to_str(mmd)
    build.context = context
    build.owner = "mbs_import"
    build.rebuild_strategy = "all"
    now = datetime.utcnow()
    build.time_submitted = now
    build.time_modified = now
    build.time_completed = now
    if build.name in conf.base_module_names:
        build.stream_version = models.ModuleBuild.get_stream_version(stream)

    # Record the base modules this module buildrequires
    if check_buildrequires:
        for base_module in build.get_buildrequired_base_modules(db_session):
            if base_module not in build.buildrequires:
                build.buildrequires.append(base_module)

    build.update_virtual_streams(db_session, virtual_streams)

    db_session.commit()

    msg = "Module {} imported".format(nsvc)
    log.info(msg)
    msgs.append(msg)

    return build, msgs


def mmd_to_str(mmd):
    """
    Helper method to convert a Modulemd.ModuleStream object to a YAML string.

    :param Modulemd.ModuleStream mmd: the modulemd to convert
    :return: the YAML string of the modulemd
    :rtype: str
    """
    index = Modulemd.ModuleIndex()
    index.add_module_stream(mmd)
    return to_text_type(index.dump_to_string())
