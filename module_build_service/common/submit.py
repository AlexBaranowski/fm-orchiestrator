# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
import shutil
import tempfile

import requests

import module_build_service.scm
from module_build_service import conf, log
from module_build_service.common.utils import load_mmd_file
from module_build_service.common.errors import ValidationError


def _is_eol_in_pdc(name, stream):
    """ Check PDC if the module name:stream is no longer active. """

    params = {"type": "module", "global_component": name, "name": stream}
    url = conf.pdc_url + "/component-branches/"

    response = requests.get(url, params=params)
    if not response:
        raise ValidationError("Failed to talk to PDC {}{}".format(response, response.text))

    data = response.json()
    results = data["results"]
    if not results:
        raise ValidationError(
            "No such module {}:{} found at {}".format(name, stream, response.request.url))

    # If the module is active, then it is not EOL and vice versa.
    return not results[0]["active"]


def fetch_mmd(url, branch=None, allow_local_url=False, whitelist_url=False, mandatory_checks=True):
    td = None
    scm = None
    try:
        log.debug("Verifying modulemd")
        td = tempfile.mkdtemp()
        if whitelist_url:
            scm = module_build_service.scm.SCM(url, branch, [url], allow_local_url)
        else:
            scm = module_build_service.scm.SCM(url, branch, conf.scmurls, allow_local_url)
        scm.checkout(td)
        if not whitelist_url and mandatory_checks:
            scm.verify()
        cofn = scm.get_module_yaml()
        mmd = load_mmd_file(cofn)
    finally:
        try:
            if td is not None:
                shutil.rmtree(td)
        except Exception as e:
            log.warning("Failed to remove temporary directory {!r}: {}".format(td, str(e)))

    if conf.check_for_eol:
        if _is_eol_in_pdc(scm.name, scm.branch):
            raise ValidationError(
                "Module {}:{} is marked as EOL in PDC.".format(scm.name, scm.branch))

    if not mandatory_checks:
        return mmd, scm

    # If the name was set in the modulemd, make sure it matches what the scmurl
    # says it should be
    if mmd.get_module_name() and mmd.get_module_name() != scm.name:
        if not conf.allow_name_override_from_scm:
            raise ValidationError(
                'The name "{0}" that is stored in the modulemd is not valid'
                .format(mmd.get_module_name())
            )
    else:
        # Set the module name
        mmd = mmd.copy(scm.name)

    # If the stream was set in the modulemd, make sure it matches what the repo
    # branch is
    if mmd.get_stream_name() and mmd.get_stream_name() != scm.branch:
        if not conf.allow_stream_override_from_scm:
            raise ValidationError(
                'The stream "{0}" that is stored in the modulemd does not match the branch "{1}"'
                .format(mmd.get_stream_name(), scm.branch)
            )
    else:
        # Set the module stream
        mmd = mmd.copy(mmd.get_module_name(), scm.branch)

    # If the version is in the modulemd, throw an exception since the version
    # since the version is generated by MBS
    if mmd.get_version():
        raise ValidationError(
            'The version "{0}" is already defined in the modulemd but it shouldn\'t be since the '
            "version is generated based on the commit time".format(mmd.get_version())
        )
    else:
        mmd.set_version(int(scm.version))

    return mmd, scm
