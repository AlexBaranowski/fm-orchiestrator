# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

import re
import sys
import time

from kobo import rpmlib
import koji
import yaml
import requests
import tempfile
import sh
import os

our_sh = sh(_out=sys.stdout, _err=sys.stderr, _tee=True)
from our_sh import Command, git, pushd  # noqa


class Koji:
    """Wrapper class to work with Koji

    :attribute string _server: URL of the Koji hub
    :attribute string _topurl: URL of the top-level Koji download location
    :attribute koji.ClientSession _session: Koji session
    :attribute koji.PathInfo _pathinfo: Koji path
    """

    def __init__(self, server, topurl):
        self._server = server
        self._topurl = topurl
        self._session = koji.ClientSession(self._server)
        self._pathinfo = koji.PathInfo(self._topurl)

    def get_build(self, nvr_dict):
        """Koji build data for NVR

        :param dict nvr_dict: NVR dictionary as expected by kobo.rpmlib.make_nvr()
        :return: Dictionary with Koji build data or None, if build is not found
        :rtype: dict or None
        """
        nvr_string = rpmlib.make_nvr(nvr_dict)
        return self._session.getBuild(nvr_string)

    def get_modulemd(self, cg_build):
        """Modulemd file of the build from koji archive

        :param cg_build: koji build object
        :return: Modulemd file
        :rtype: dict
        """
        url = self._pathinfo.typedir(cg_build, 'module')
        r = requests.get(f"{url}/modulemd.txt")
        r.raise_for_status()
        return yaml.safe_load(r.content)

    def get_build_log(self, component, log_name):
        """Log file related to a build.

        :param dict component: Item produced with build.components()
        :param str log_name: Filename of log e.g 'build.log'
        :return Content of a log file
        :rtype str
        """
        nvr = component['nvr']
        build_logs = self._session.getBuildLogs(nvr)

        for log in build_logs:
            if log['name'] == log_name:
                log_path = log['path']
                break

        url = self._topurl + '/' + log_path
        r = requests.get(url)
        r.raise_for_status()
        return r.text

    def get_macro_specfile(self, build):
        """
        Download macro src.rpm and extract spec file .

        :param build: build object
        :return: content of module-build-macros.spec
        :rtype: str
        """
        parent_id = build.component_task_ids()['module-build-macros']
        child_id = next(child['id'] for child in
                        self._session.getTaskChildren(parent_id)
                        if child['method'] == 'buildArch')
        nvr = next(component['nvr'] for component in build.components()
                   if component['package'] == 'module-build-macros')
        file_name = nvr + ".src.rpm"
        src_rpm = self._session.downloadTaskOutput(child_id, file_name)
        with tempfile.NamedTemporaryFile() as temp_file:
            temp_file.write(src_rpm)
            return os.popen(f"rpm2cpio {temp_file.name} | "
                            f"cpio -ci --to-stdout '*.spec'").read()


class Repo:
    """Wrapper class to work with module git repositories

    :attribute string module_name: name of the module stored in this repo
    :attribute dict _modulemd: Modulemd file as read from the repo
    """

    def __init__(self, module_name):
        self.module_name = module_name
        self._modulemd = None
        self._version = None

    @property
    def modulemd(self):
        """Modulemd file as read from the repo

        :return: Modulemd file as read from the repo
        :rtype: dict
        """
        if self._modulemd is None:
            modulemd_file = self.module_name + ".yaml"
            with open(modulemd_file, "r") as f:
                self._modulemd = yaml.safe_load(f)
        return self._modulemd

    @property
    def components(self):
        """List of components as defined in the modulemd file

        :return: List of components as defined in the modulemd file
        :rtype: list of strings
        """
        return list(self.modulemd["data"]["components"]["rpms"])

    @property
    def platform(self):
        """
        List of platforms in the modulemd file, obtaining values differs on version

        :return: List of platforms in the modulemd file
        :rtype: list of strings
        """
        if self._version is None:
            self._version = self._modulemd["version"]
        if self._version == 1:
            return [self._modulemd["data"]["dependencies"]["buildrequires"].get("platform")]
        elif self._version == 2:
            return self._modulemd["data"]["dependencies"][0]["buildrequires"].get("platform")

    def bump(self):
        """Create a "bump" commit"""
        args = [
            "--allow-empty",
            "-m",
            "Bump"
        ]
        git("commit", *args)
        git("push")


class PackagingUtility:
    """Wrapper class to work with the packaging utility configured for the tests

    :attribute sh.Command _packaging_utility: packaging utility command used to
        kick off this build
    :attribute string _mbs_api: URL of the MBS API (including trailing '/')
    """

    def __init__(self, packaging_utility, mbs_api):
        self._packaging_utility = Command(packaging_utility).bake(
            _out=sys.stdout,
            _err=sys.stderr,
            _tee=True
        )
        self._mbs_api = mbs_api

    def run(self, *args, reuse=None, **kwargs):
        """Run one or more module builds

        :param args: Options and arguments for the build command
        :param int reuse: An optional MBS build id or a list of MBS build
            ids to be reused for this run.
            When specified, the corresponding module build(s) will be used,
            instead of triggering and waiting for new one(s) to finish.
            Intended to be used while developing the tests.
        :return: list of Build objects for the MBS builds created
        :rtype: list of Build objects
        """
        build_ids = []

        if reuse is not None:
            if isinstance(reuse, list):
                build_ids = reuse
            else:
                build_ids = [reuse]
        else:
            stdout = self._packaging_utility("module-build", *args, **kwargs).stdout.decode("utf-8")
            build_ids = re.findall(self._mbs_api + r"module-builds/(\d+)", stdout)
        return [Build(self._mbs_api, int(build_id)) for build_id in build_ids]

    def watch(self, builds):
        """Watch one or more builds till the finish

        :param list builds: list of Build objects of the builds to be watched.
        :return: Stdout of the watch command
        :rtype: string
        """
        stdout = self._packaging_utility(
            "module-build-watch", [str(build.id) for build in builds]
        ).stdout.decode("utf-8")

        return stdout

    def cancel(self, build):
        """Cancel the module build

        :param list build: the Build object of the module build to be cancelled.
        :return: Standard output of the "module-build-cancel <build id=""> command
        :rtype: str
        """
        stdout = self._packaging_utility("module-build-cancel", build.id).stdout.decode(
            "utf-8")
        return stdout


class Build:
    """Wrapper class to work with module builds

    :attribute string _mbs_api: URL of the MBS API (including trailing '/')
    :attribute int _build_id: id of this MBS module build
    :attribute string _data: Module build data cache for this build fetched from MBS
    :attribute string _module_build_data: Verbose module build data cache for this build
    """

    def __init__(self, mbs_api, build_id):
        self._mbs_api = mbs_api
        self._data = None
        self._component_data = None
        self._build_id = build_id
        self._module_build_data = None

    @property
    def id(self):
        return self._build_id

    @property
    def data(self):
        """Module build data cache for this build fetched from MBS"""
        if self._data is None and self._build_id:
            r = requests.get(f"{self._mbs_api}module-builds/{self._build_id}")
            r.raise_for_status()
            self._data = r.json()
        return self._data

    @property
    def component_data(self):
        """Component data for the module build"""
        if self._component_data is None and self._build_id:
            params = {
                "module_build": self._build_id,
                "verbose": True,
            }
            r = requests.get(f"{self._mbs_api}component-builds/", params=params)
            r.raise_for_status()
            self._component_data = r.json()
        return self._component_data

    @property
    def module_build_data(self):
        """Verbose module build

        :return: Dictionary of the verbose module build parameters
        :rtype: dict
        """
        if self._build_id:
            params = {
                "verbose": True,
            }
            r = requests.get(f"{self._mbs_api}module-builds/{self._build_id}", params=params)
            r.raise_for_status()
            self._module_build_data = r.json()
        return self._module_build_data

    @property
    def state_name(self):
        """Name of the state of this module build"""
        return self.data["state_name"]

    def components(self, state=None, batch=None, package=None):
        """Components of this module build, optionally filtered based on properties

        :param string state: Koji build state the components should be in
        :param int batch: the number of the batch the components should be in
        :param string package: name of the component (package)
        :return: List of filtered components
        :rtype: list of dict
        """
        filtered = self.component_data["items"]
        if batch is not None:
            filtered = filter(lambda x: x["batch"] == batch, filtered)
        if state is not None:
            filtered = filter(lambda x: x["state_name"] == state, filtered)
        if package is not None:
            filtered = filter(lambda x: x["package"] == package, filtered)

        return list(filtered)

    def component_names(self, state=None, batch=None, package=None):
        """Component names of this module build, optionally filtered based on properties

        :param string state: Koji build state the components should be in
        :param int batch: the number of the batch the components should be in
        :param string package: name of component (package):
        :return: List of components packages
        :rtype: list of strings
        """
        components = self.components(state, batch, package)
        return [item["package"] for item in components]

    def component_task_ids(self):
        """Dictionary containing all names of packages from build and appropriate task ids

        :return: Dictionary containing name of packages and their task id
        :rtype: dict
        """
        return {comp["package"]: comp["task_id"] for comp in self.components()}

    def batches(self):
        """
        Components of the module build separated in sets according to batches

        :return: list of components according to batches
        :rtype: list of sets
        """
        comps_data = sorted(self.component_data["items"], key=lambda x: x["batch"])
        batch_count = comps_data[-1]["batch"]
        batches = batch_count * [set()]
        for data in comps_data:
            batch = data["batch"]
            package = data["package"]
            batches[batch - 1] = batches[batch - 1].union({package})

        return batches

    def wait_for_koji_task_id(self, package, batch, timeout=300, sleep=10):
        """Wait until the component is submitted to Koji (has a task_id)

        :param string package: name of component (package)
        :param int batch: the number of the batch the components should be in
        :param int timeout: time in seconds
        :param int sleep: time in seconds
        """
        start = time.time()
        while time.time() - start <= timeout:
            # Clear cached data
            self._component_data = None
            components = self.components(package=package, batch=batch)
            # Wait until the right component appears and has a task_id
            if components and components[0]["task_id"]:
                return components[0]["task_id"]
            time.sleep(sleep)

        raise RuntimeError(
            f'Koji task for "{package}" did not start in {timeout} seconds'
        )

    def nvr(self, name_suffix=""):
        """NVR dictionary of this module build

        :param string name_suffix: an optional suffix for the name component of the NVR
        :return: dictionary with NVR components
        :rtype: dict
        """
        return {
            "name": f'{self.data["name"]}{name_suffix}',
            "version": self.data["stream"].replace("-", "_"),
            "release": f'{self.data["version"]}.{self.data["context"]}',
        }

    def was_cancelled(self):
        """Checking in the status trace if module was canceled

        :return: Whether exists required status
        :rtype: bool
        """
        for item in self.module_build_data["state_trace"]:
            if (
                    item["reason"] is not None
                    and "Canceled" in item["reason"]
                    and item["state_name"] == "failed"
            ):
                return True
        return False


class Component:
    """Wrapper class to work with git repositories of components

    :attribute string module_name: name of the module stored in this repo
    :attribute string branch: branch of the git repo that will be used
    :attribute TemporaryDirectory _clone_dir: directory where is the clone of the repo
    """
    def __init__(self, module_name, branch):
        self._module_name = module_name
        self._branch = branch
        self._clone_dir = None

    def __del__(self):
        self._clone_dir.cleanup()

    def clone(self, packaging_utility):
        """Clone the git repo of the component to be used by the test in a temporary location

        Directory of the clone is stored in self._clone_dir.
        :param string packaging_utility: packaging utility as defined in test.env.yaml
        """
        tempdir = tempfile.TemporaryDirectory()
        args = [
            "--branch",
            self._branch,
            f'rpms/{self._module_name}',
            tempdir.name
        ]
        packaging_util = Command(packaging_utility)
        packaging_util("clone", *args)
        self._clone_dir = tempdir

    def bump(self):
        """Create a "bump" commit and push it in git"""
        args = [
            "--allow-empty",
            "-m",
            "Bump"
        ]
        with pushd(self._clone_dir.name):
            git("commit", *args)
            git("push")


class MBS:
    """Wrapper class to work with MBS requests.

    :attribute string _mbs_api: URL of the MBS API (including trailing '/')
    """

    def __init__(self, mbs_api):
        self._mbs_api = mbs_api

    def get_builds(self, module, stream, order_desc_by=None):
        """Get list of Builds objects via mbs api.

        :attribute string module: Module name
        :attribute string stream: Stream name
        :attribute string order_desc_by: Optional sorting parameter e.g. "version"
        :return: list of Build objects
        :rtype: list
        """
        url = f"{self._mbs_api}module-builds/"
        payload = {'name': module, "stream": stream}
        if order_desc_by:
            payload.update({"order_desc_by": order_desc_by})
        r = requests.get(url, params=payload)
        r.raise_for_status()
        return [Build(self._mbs_api, build["id"]) for build in r.json()["items"]]
