# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import logging
import os
import pipes
import re
import subprocess
import threading

import dnf
import koji
import kobo.rpmlib
import platform

from module_build_service.builder import GenericBuilder
from module_build_service.builder.KojiModuleBuilder import KojiModuleBuilder
from module_build_service.builder.utils import (
    create_local_repo_from_koji_tag,
    execute_cmd,
    find_srpm,
    get_koji_config,
    validate_koji_tag,
)
from module_build_service.common import conf, log, models
from module_build_service.common.koji import get_session
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import import_mmd, load_mmd_file, mmd_to_str
from module_build_service.scheduler import events
from module_build_service.scheduler.db_session import db_session

logging.basicConfig(level=logging.DEBUG)


def detect_arch():
    """
    Helper method to detect the local host architecture. Fallbacks to `conf.arch_fallback`.
    """
    if conf.arch_autodetect:
        arch_detected = platform.machine()
        if arch_detected:
            return arch_detected

        log.warning("Couldn't determine machine arch. Falling back to configured arch.")

    return conf.arch_fallback


def import_fake_base_module(nsvc):
    """
    Creates and imports new fake base module to be used with offline local builds.

    :param str nsvc: name:stream:version:context of a module.
    """
    name, stream, version, context = nsvc.split(":")
    mmd = Modulemd.ModuleStreamV2.new(name, stream)
    mmd.set_version(int(version))
    mmd.set_context(context)
    mmd.set_summary("fake base module")
    mmd.set_description("fake base module")
    mmd.add_module_license("GPL")

    buildroot = Modulemd.Profile.new("buildroot")
    for rpm in conf.default_buildroot_packages:
        buildroot.add_rpm(rpm)
    mmd.add_profile(buildroot)

    srpm_buildroot = Modulemd.Profile.new("srpm-buildroot")
    for rpm in conf.default_srpm_buildroot_packages:
        srpm_buildroot.add_rpm(rpm)
    mmd.add_profile(srpm_buildroot)

    xmd = {"mbs": {}}
    xmd_mbs = xmd["mbs"]
    xmd_mbs["buildrequires"] = {}
    xmd_mbs["requires"] = {}
    xmd_mbs["commit"] = "ref_%s" % context
    xmd_mbs["mse"] = "true"
    # Use empty "repofile://" URI for base module. The base module will use the
    # `conf.base_module_names` list as list of default repositories.
    xmd_mbs["koji_tag"] = "repofile://"
    mmd.set_xmd(xmd)

    import_mmd(db_session, mmd, False)


def load_local_builds(local_build_nsvs):
    """
    Loads previously finished local module builds from conf.mock_resultsdir
    and imports them to database.

    :param local_build_nsvs: List of NSV separated by ':' defining the modules
        to load from the mock_resultsdir.
    """
    if not local_build_nsvs:
        return

    if type(local_build_nsvs) != list:
        local_build_nsvs = [local_build_nsvs]

    # Get the list of all available local module builds.
    builds = []
    try:
        for d in os.listdir(conf.mock_resultsdir):
            m = re.match("^module-(.*)-([^-]*)-([0-9]+)$", d)
            if m:
                builds.append((m.group(1), m.group(2), int(m.group(3)), d))
    except OSError:
        pass

    # Sort with the biggest version first
    try:
        # py27
        builds.sort(lambda a, b: -cmp(a[2], b[2]))  # noqa: F821
    except TypeError:
        # py3
        builds.sort(key=lambda a: a[2], reverse=True)

    for nsv in local_build_nsvs:
        parts = nsv.split(":")
        if len(parts) < 1 or len(parts) > 3:
            raise RuntimeError(
                'The local build "{0}" couldn\'t be be parsed into NAME[:STREAM[:VERSION]]'
                .format(nsv)
            )

        name = parts[0]
        stream = parts[1] if len(parts) > 1 else None
        version = int(parts[2]) if len(parts) > 2 else None

        found_build = None
        for build in builds:
            if name != build[0]:
                continue
            if stream is not None and stream != build[1]:
                continue
            if version is not None and version != build[2]:
                continue

            found_build = build
            break

        if not found_build:
            raise RuntimeError(
                'The local build "{0}" couldn\'t be found in "{1}"'.format(
                    nsv, conf.mock_resultsdir)
            )

        # Load the modulemd metadata.
        path = os.path.join(conf.mock_resultsdir, found_build[3], "results")
        mmd = load_mmd_file(os.path.join(path, "modules.yaml"))

        # Create ModuleBuild in database.
        module = models.ModuleBuild.create(
            db_session,
            conf,
            name=mmd.get_module_name(),
            stream=mmd.get_stream_name(),
            version=str(mmd.get_version()),
            context=mmd.get_context(),
            modulemd=mmd_to_str(mmd),
            scmurl="",
            username="mbs",
        )
        module.koji_tag = path
        module.state = models.BUILD_STATES["ready"]
        db_session.commit()

        if (
            found_build[0] != module.name
            or found_build[1] != module.stream
            or str(found_build[2]) != module.version
        ):
            raise RuntimeError(
                'Parsed metadata results for "{0}" don\'t match the directory name'.format(
                    found_build[3])
            )
        log.info("Loaded local module build %r", module)


def get_local_releasever():
    """
    Returns the $releasever variable used in the system when expanding .repo files.
    """
    dnf_base = dnf.Base()
    return dnf_base.conf.releasever


def import_builds_from_local_dnf_repos(platform_id=None):
    """
    Imports the module builds from all available local repositories to MBS DB.

    This is used when building modules locally without any access to MBS infra.
    This method also generates and imports the base module according to /etc/os-release.

    :param str platform_id: The `name:stream` of a fake platform module to generate in this
        method. When not set, the /etc/os-release is parsed to get the PLATFORM_ID.
    """
    log.info("Loading available RPM repositories.")
    dnf_base = dnf.Base()
    dnf_base.read_all_repos()

    log.info("Importing available modules to MBS local database.")
    for repo in dnf_base.repos.values():
        try:
            repo.load()
        except Exception as e:
            log.warning(str(e))
            continue
        mmd_data = repo.get_metadata_content("modules")
        mmd_index = Modulemd.ModuleIndex.new()
        ret, _ = mmd_index.update_from_string(mmd_data, True)
        if not ret:
            log.warning("Loading the repo '%s' failed", repo.name)
            continue

        for module_name in mmd_index.get_module_names():
            for mmd in mmd_index.get_module(module_name).get_all_streams():
                xmd = mmd.get_xmd()
                xmd["mbs"] = {}
                xmd["mbs"]["koji_tag"] = "repofile://" + repo.repofile
                xmd["mbs"]["mse"] = True
                xmd["mbs"]["commit"] = "unknown"
                mmd.set_xmd(xmd)

                import_mmd(db_session, mmd, False)

    if not platform_id:
        # Parse the /etc/os-release to find out the local platform:stream.
        with open("/etc/os-release", "r") as fd:
            for l in fd.readlines():
                if not l.startswith("PLATFORM_ID"):
                    continue
                platform_id = l.split("=")[1].strip("\"' \n")
    if not platform_id:
        raise ValueError("Cannot get PLATFORM_ID from /etc/os-release.")

    # Create the fake platform:stream:1:000000 module to fulfill the
    # dependencies for local offline build and also to define the
    # srpm-buildroot and buildroot.
    import_fake_base_module("%s:1:000000" % platform_id)


class MockModuleBuilder(GenericBuilder):
    backend = "mock"
    # Global build_id/task_id we increment when new build is executed.
    _build_id_lock = threading.Lock()
    _build_id = 1
    _config_lock = threading.Lock()

    # Load mock config file template
    for cf in conf.mock_config_file:
        try:
            with open(cf) as f:
                mock_config_template = f.read()
            break
        except IOError:
            pass
    else:
        raise IOError("None of {} mock config files found.".format(conf.mock_config_file))

    # Load yum config file template
    for cf in conf.yum_config_file:
        try:
            with open(cf) as f:
                yum_config_template = f.read()
            break
        except IOError:
            pass
    else:
        raise IOError("None of {} yum config files found.".format(conf.yum_config_file))

    @validate_koji_tag("tag_name")
    def __init__(self, db_session, owner, module, config, tag_name, components):
        self.db_session = db_session
        self.module_str = module.name
        self.module = module
        self.tag_name = tag_name
        self.config = config
        self.groups = []
        self.enabled_modules = []
        self.releasever = get_local_releasever()
        self.yum_conf = MockModuleBuilder.yum_config_template
        self.koji_session = None

        # Auto-detect arch (if possible) or fallback to the configured one
        self.arch = detect_arch()
        log.info("Machine arch setting: {}".format(self.arch))

        # Create main directory for this tag
        self.tag_dir = os.path.join(self.config.mock_resultsdir, tag_name)
        if not os.path.exists(self.tag_dir):
            os.makedirs(self.tag_dir)

        # Create "results" sub-directory for this tag to store build results
        # and local repository.
        self.resultsdir = os.path.join(self.tag_dir, "results")
        if not os.path.exists(self.resultsdir):
            os.makedirs(self.resultsdir)

        # Create "config" sub-directory.
        self.configdir = os.path.join(self.tag_dir, "config")
        if not os.path.exists(self.configdir):
            os.makedirs(self.configdir)

        # Generate path to mock config and add local repository there.
        # Set skip_if_unavailable=True since the repo isn't available until after
        # module-build-macros is built.
        self._add_repo(
            "localrepo",
            "file://" + self.resultsdir,
            "metadata_expire=1\nskip_if_unavailable=True\n",
        )

        # Remove old files from the previous build of this tag but only
        # before the first build is done, otherwise we would remove files
        # which we already build in this module build.
        if MockModuleBuilder._build_id == 1:
            # Remove all RPMs from the results directory, but keep old logs.
            for name in os.listdir(self.resultsdir):
                if name.endswith(".rpm"):
                    os.remove(os.path.join(self.resultsdir, name))

            # Remove the old RPM repository from the results directory.
            if os.path.exists(os.path.join(self.resultsdir, "repodata/repomd.xml")):
                os.remove(os.path.join(self.resultsdir, "repodata/repomd.xml"))

            # Remove old config files from config directory.
            for name in os.listdir(self.configdir):
                os.remove(os.path.join(self.configdir, name))

        log.info(
            "MockModuleBuilder initialized, tag_name=%s, tag_dir=%s" % (tag_name, self.tag_dir))

    @property
    def module_build_tag(self):
        # Workaround koji specific code in modules.py
        return {"name": self.tag_name}

    @classmethod
    def get_module_build_arches(cls, module):
        """
        :param ModuleBuild module: Get the list of architectures associated with
            the module build in the build system.
        :return: list of architectures
        """
        # Return local architecture, because all the modules built locally are built
        # just against this architecture.
        return [detect_arch()]

    def _createrepo(self, include_module_yaml=False):
        """
        Creates the repository using "createrepo_c" command in the resultsdir.
        """
        log.debug("Creating repository in %s" % self.resultsdir)
        path = self.resultsdir
        repodata_path = os.path.join(path, "repodata")

        # Remove old repodata files
        if os.path.exists(repodata_path):
            for name in os.listdir(repodata_path):
                os.remove(os.path.join(repodata_path, name))

        # We pass an explicit package list to createrepo_c, otherwise, it will
        # walk the target directory recursively, instead of just finding the
        # files at the toplevel.
        pkglist = os.path.join(path, "pkglist")
        pkglist_f = open(pkglist, "w")

        # Generate the mmd the same way as pungi does.
        m1_mmd = self.module.mmd()
        artifacts = set()

        rpm_files = [f for f in os.listdir(self.resultsdir) if f.endswith(".rpm")]

        if rpm_files:
            output = subprocess.check_output(
                [
                    "rpm",
                    "--queryformat",
                    "%{NAME} %{EPOCHNUM} %{VERSION} %{RELEASE} %{ARCH}\n",
                    "-qp",
                ]
                + rpm_files,
                cwd=self.resultsdir,
                universal_newlines=True,
            )
            nevras = output.strip().split("\n")
            if len(nevras) != len(rpm_files):
                raise RuntimeError("rpm -qp returned an unexpected number of lines")

            for rpm_file, nevra in zip(rpm_files, nevras):
                name, epoch, version, release, arch = nevra.split()

                if self.module.last_batch_id() == self.module.batch:
                    # If RPM is filtered-out, do not add it to artifacts list.
                    if name in m1_mmd.get_rpm_filters():
                        continue

                pkglist_f.write(rpm_file + "\n")
                artifacts.add("{}-{}:{}-{}.{}".format(name, epoch, version, release, arch))

        pkglist_f.close()
        # There is no way to replace the RPM artifacts, so remove any extra RPM artifacts
        for artifact_to_remove in (set(m1_mmd.get_rpm_artifacts()) - artifacts):
            m1_mmd.remove_rpm_artifact(artifact_to_remove)
        for artifact_to_add in (artifacts - set(m1_mmd.get_rpm_artifacts())):
            m1_mmd.add_rpm_artifact(artifact_to_add)

        # Generate repo.
        execute_cmd(["/usr/bin/createrepo_c", "--pkglist", pkglist, path])

        # ...and inject modules.yaml there if asked.
        if include_module_yaml:
            mmd_path = os.path.join(path, "modules.yaml")
            with open(mmd_path, "w") as f:
                f.write(mmd_to_str(m1_mmd))
            execute_cmd(["/usr/bin/modifyrepo_c", "--mdtype=modules", mmd_path, repodata_path])

    def _add_repo(self, name, baseurl, extra=""):
        """
        Adds repository to Mock config file. Call _write_mock_config() to
        actually write the config file to filesystem.
        """
        self.yum_conf += "[%s]\n" % name
        self.yum_conf += "name=%s\n" % name
        self.yum_conf += "baseurl=%s\n" % baseurl
        # See https://dnf.readthedocs.io/en/latest/modularity.html#hotfix-repositories
        self.yum_conf += "module_hotfixes=true\n"
        self.yum_conf += extra
        self.yum_conf += "enabled=1\n\n"

    def _add_repo_from_path(self, path):
        """
        Adds repository stored in `path` to Mock config file. Call _write_mock_config() to
        actually write the config file to filesystem.
        """
        with open(path) as fd:
            self.yum_conf += fd.read()

    def _load_mock_config(self):
        """
        Loads the variables which are generated only during the first
        initialization of mock config. This should be called before
        every _write_mock_config otherwise we overwrite Mock
        repositories or groups ...
        """

        # We do not want to load old file from previous builds here, so if
        # this is the first build in this module, skip the load completely.
        if MockModuleBuilder._build_id == 1:
            return

        with MockModuleBuilder._config_lock:
            infile = os.path.join(self.configdir, "mock.cfg")
            with open(infile, "r") as f:
                # This looks scary, but it is the way how mock itself loads the
                # config file ...
                config_opts = {}
                code = compile(f.read(), infile, "exec")
                # pylint: disable=exec-used
                exec(code)

                self.groups = config_opts["chroot_setup_cmd"].split(" ")[1:]
                self.yum_conf = config_opts["yum.conf"]
                self.enabled_modules = config_opts["module_enable"]
                self.releasever = config_opts["releasever"]

    def _write_mock_config(self):
        """
        Writes Mock config file to local file.
        """

        with MockModuleBuilder._config_lock:
            config = str(MockModuleBuilder.mock_config_template)
            config = config.replace(
                "$root", "%s-%s" % (self.tag_name, str(threading.current_thread().name)))
            config = config.replace("$arch", self.arch)
            config = config.replace("$group", " ".join(self.groups))
            config = config.replace("$yum_conf", self.yum_conf)
            config = config.replace("$enabled_modules", str(self.enabled_modules))
            config = config.replace("$releasever", str(self.releasever))

            # We write the most recent config to "mock.cfg", so thread-related
            # configs can be later (re-)generated from it using _load_mock_config.
            outfile = os.path.join(self.configdir, "mock.cfg")
            with open(outfile, "w") as f:
                f.write(config)

            # Write the config to thread-related configuration file.
            outfile = os.path.join(
                self.configdir, "mock-%s.cfg" % str(threading.current_thread().name))
            with open(outfile, "w") as f:
                f.write(config)

    def buildroot_connect(self, groups):
        self._load_mock_config()
        self.groups = list(set().union(groups["build"], self.groups))
        log.debug("Mock builder groups: %s" % self.groups)
        self._write_mock_config()

    def buildroot_prep(self):
        pass

    def buildroot_resume(self):
        pass

    def buildroot_ready(self, artifacts=None):
        return True

    def buildroot_add_dependency(self, dependencies):
        pass

    def buildroot_add_artifacts(self, artifacts, install=False):
        from module_build_service.scheduler.handlers.repos import done as repos_done_handler
        self._createrepo()

        # TODO: This is just hack to install module-build-macros into the
        # buildroot. We should really install the RPMs belonging to the
        # right source RPM into the buildroot here, but we do not track
        # what RPMs are output of particular SRPM build yet.
        for artifact in artifacts:
            if artifact and artifact.startswith("module-build-macros"):
                self._load_mock_config()
                self.groups.append("module-build-macros")
                self._write_mock_config()

        events.scheduler.add(repos_done_handler, ("fake_msg", self.tag_name + "-build"))

    def tag_artifacts(self, artifacts):
        pass

    def buildroot_add_repos(self, dependencies):
        self._load_mock_config()
        for source, mmds in dependencies.items():
            # If source starts with mock_resultdir, it means it is path to local
            # module build repository.
            if source.startswith(conf.mock_resultsdir):
                repo_name = os.path.basename(source)
                if repo_name.startswith("module-"):
                    repo_name = repo_name[7:]
                repo_dir = source
                baseurl = "file://" + repo_dir
            # If source starts with "repofile://", it is path to local /etc/yum.repos.d
            # repo file.
            elif source.startswith("repofile://"):
                # For the base module, we want to include all the `conf.base_module_repofiles`.
                if len(mmds) == 1 and mmds[0].get_module_name() in conf.base_module_names:
                    for repofile in conf.base_module_repofiles:
                        self._add_repo_from_path(repofile)
                    # Also set the platform_id.
                    mmd = mmds[0]
                    self.yum_conf = self.yum_conf.replace(
                        "$module_platform_id",
                        "%s:%s" % (mmd.get_module_name(), mmd.get_stream_name())
                    )
                else:
                    # Add repositories defined in repofile to mock config.
                    repofile = source[len("repofile://"):]
                    self._add_repo_from_path(repofile)
                    # Enabled all the modular dependencies by default in Mock.
                    for mmd in mmds:
                        self.enabled_modules.append(
                            "%s:%s" % (mmd.get_module_name(), mmd.get_stream_name()))
                continue
            else:
                repo_name = tag = source
                koji_config = get_koji_config(self.config)
                koji_session = koji.ClientSession(koji_config.server, opts=koji_config)
                # Check to see if there are any external repos tied to the tag
                for ext_repo in koji_session.getTagExternalRepos(tag):
                    self._add_repo(ext_repo["external_repo_name"], ext_repo["url"])

                repo = koji_session.getRepo(repo_name)
                if repo:
                    baseurl = koji.PathInfo(topdir=koji_config.topurl).repo(repo["id"], repo_name)
                    baseurl = "{0}/{1}/".format(baseurl, self.arch)
                else:
                    repo_dir = os.path.join(self.config.cache_dir, "koji_tags", tag)
                    should_add_repo = create_local_repo_from_koji_tag(
                        self.config, tag, repo_dir, [self.arch, "noarch"])
                    if not should_add_repo:
                        continue
                    baseurl = "file://" + repo_dir

            self._add_repo(repo_name, baseurl)
        self._write_mock_config()

    def _send_build_change(self, state, source, build_id):
        from module_build_service.scheduler.handlers.components import (
            build_task_finalize as build_task_finalize_handler)
        try:
            nvr = kobo.rpmlib.parse_nvr(source)
        except ValueError:
            nvr = {"name": source, "release": "unknown", "version": "unknown"}

        # use build_id as task_id
        args = (
            "a faked internal message", build_id, state, nvr["name"], nvr["version"],
            nvr["release"], None, None)
        events.scheduler.add(build_task_finalize_handler, args)

    def _save_log(self, resultsdir, log_name, artifact_name):
        old_log = os.path.join(resultsdir, log_name)
        new_log = os.path.join(resultsdir, artifact_name + "-" + log_name)
        if os.path.exists(old_log):
            os.rename(old_log, new_log)

    def _purge_useless_logs(self):
        """
        Remove empty or otherwise useless log files
        """
        for logf in os.listdir(self.resultsdir):

            log_path = os.path.join(self.resultsdir, logf)

            # Remove empty files
            if os.path.isfile(log_path) and os.path.getsize(log_path) == 0:
                os.remove(log_path)

            # Remove other files containing useless information
            elif logf.endswith("-srpm-stdout.log"):
                with open(log_path) as f:
                    data = f.read(4096)
                    if re.match("Downloading [^\n]*\n\n\nWrote: [^\n]", data):
                        os.remove(log_path)

    def build_srpm(self, artifact_name, source, build_id, builder):
        """
        Builds the artifact from the SRPM.
        """
        state = koji.BUILD_STATES["BUILDING"]

        # Use the mock config associated with this thread.
        mock_config = os.path.join(
            self.configdir, "mock-%s.cfg" % str(threading.current_thread().name))

        # Open the logs to which we will forward mock stdout/stderr.
        mock_stdout_log = open(
            os.path.join(self.resultsdir, artifact_name + "-mock-stdout.log"), "w")
        mock_stderr_log = open(
            os.path.join(self.resultsdir, artifact_name + "-mock-stderr.log"), "w")

        srpm = artifact_name
        resultsdir = builder.resultsdir
        try:
            # Initialize mock.
            execute_cmd(
                ["mock", "-v", "-r", mock_config, "--init"],
                stdout=mock_stdout_log,
                stderr=mock_stderr_log,
            )

            # Start the build and store results to resultsdir
            builder.build(mock_stdout_log, mock_stderr_log)
            srpm = find_srpm(resultsdir)

            # Emit messages simulating complete build. These messages
            # are put in the scheduler's work queue and are handled
            # by MBS after the build_srpm() method returns and scope gets
            # back to scheduler.main.main() method.
            state = koji.BUILD_STATES["COMPLETE"]
            self._send_build_change(state, srpm, build_id)

            with open(os.path.join(resultsdir, "status.log"), "w") as f:
                f.write("complete\n")
        except Exception as e:
            log.error("Error while building artifact %s: %s" % (artifact_name, str(e)))

            # Emit messages simulating complete build. These messages
            # are put in the scheduler's work queue and are handled
            # by MBS after the build_srpm() method returns and scope gets
            # back to scheduler.main.main() method.
            state = koji.BUILD_STATES["FAILED"]
            self._send_build_change(state, srpm, build_id)
            with open(os.path.join(resultsdir, "status.log"), "w") as f:
                f.write("failed\n")

        mock_stdout_log.close()
        mock_stderr_log.close()

        self._save_log(resultsdir, "state.log", artifact_name)
        self._save_log(resultsdir, "root.log", artifact_name)
        self._save_log(resultsdir, "build.log", artifact_name)
        self._save_log(resultsdir, "status.log", artifact_name)

        # Copy files from thread-related resultsdire to the main resultsdir.
        for name in os.listdir(resultsdir):
            os.rename(os.path.join(resultsdir, name), os.path.join(self.resultsdir, name))

        # Depending on the configuration settings, remove/keep useless log files
        if conf.mock_purge_useless_logs:
            self._purge_useless_logs()

        # We return BUILDING state here even when we know it is already
        # completed or failed, because otherwise utils.start_build_batch
        # would think this component is already built and also tagged, but
        # we have just built it - tagging will happen as result of build
        # change message we are sending above using _send_build_change.
        # It is just to make this backend compatible with other backends,
        # which return COMPLETE here only in case the resulting build is
        # already in repository ready to be used. This is not a case for Mock
        # backend in the time we return here.
        reason = "Building %s in Mock" % (artifact_name)
        return build_id, koji.BUILD_STATES["BUILDING"], reason, None

    def build(self, artifact_name, source):
        log.info("Starting building artifact %s: %s" % (artifact_name, source))

        # Load global mock config for this module build from mock.cfg and
        # generate the thread-specific mock config by writing it to fs again.
        self._load_mock_config()
        self._write_mock_config()
        mock_config = os.path.join(
            self.configdir, "mock-%s.cfg" % str(threading.current_thread().name))

        # Get the build-id in thread-safe manner.
        build_id = None
        with MockModuleBuilder._build_id_lock:
            MockModuleBuilder._build_id += 1
            build_id = int(MockModuleBuilder._build_id)

        # Clear resultsdir associated with this thread or in case it does not
        # exist, create it.
        resultsdir = os.path.join(self.resultsdir, str(threading.current_thread().name))
        if os.path.exists(resultsdir):
            for name in os.listdir(resultsdir):
                os.remove(os.path.join(resultsdir, name))
        else:
            os.makedirs(resultsdir)

        if source.endswith(".src.rpm"):
            builder = SRPMBuilder(mock_config, resultsdir, source)
        else:
            # Otherwise, assume we're building from some scm repo
            builder = SCMBuilder(mock_config, resultsdir, source, artifact_name)
        return self.build_srpm(artifact_name, source, build_id, builder)

    @staticmethod
    def get_disttag_srpm(disttag, module_build):
        # @FIXME
        return KojiModuleBuilder.get_disttag_srpm(disttag, module_build)

    def cancel_build(self, task_id):
        pass

    def list_tasks_for_components(self, component_builds=None, state="active"):
        pass

    def repo_from_tag(cls, config, tag_name, arch):
        pass

    def finalize(self, succeeded=True):
        # For successful builds, do one last createrepo, to include
        # the module metadata. We don't want to do this for failed builds,
        # since that makes it impossible to retry a build manually.
        if succeeded:
            self._createrepo(include_module_yaml=True)

    @classmethod
    def get_built_rpms_in_module_build(cls, mmd):
        """
        :param Modulemd mmd: Modulemd to get the built RPMs from.
        :return: list of NVRs
        """
        build = models.ModuleBuild.get_build_from_nsvc(
            db_session,
            mmd.get_module_name(),
            mmd.get_stream_name(),
            mmd.get_version(),
            mmd.get_context()
        )
        if build.koji_tag.startswith("repofile://"):
            # Modules from local repository have already the RPMs filled in mmd.
            return mmd.get_rpm_artifacts()
        else:
            koji_session = get_session(conf, login=False)
            rpms = koji_session.listTaggedRPMS(build.koji_tag, latest=True)[0]
            nvrs = set(kobo.rpmlib.make_nvr(rpm, force_epoch=True) for rpm in rpms)
            return list(nvrs)


class BaseBuilder(object):
    def __init__(self, config, resultsdir):
        self.config = config
        self.resultsdir = resultsdir
        self.cmd = ["mock", "-v", "-r", config, "--no-clean", "--resultdir=%s" % resultsdir]

    def build(self, stdout, stderr):
        execute_cmd(self.cmd, stdout=stdout, stderr=stderr)


class SRPMBuilder(BaseBuilder):
    def __init__(self, config, resultsdir, source):
        super(SRPMBuilder, self).__init__(config, resultsdir)
        self.cmd.extend(["--rebuild", source])


class SCMBuilder(BaseBuilder):
    def __init__(self, config, resultsdir, source, artifact_name):
        super(SCMBuilder, self).__init__(config, resultsdir)
        with open(config, "a") as f:
            repo_path, branch = source.split("?#")
            distgit_cmds = self._get_distgit_commands(source)
            # Supply the artifact name for "{0}" and the full path to the repo for "{repo_path}"
            distgit_get = distgit_cmds[0].format(artifact_name, repo_path=repo_path)

            # mock-scm cannot checkout particular commit hash, but only branch.
            # We therefore use a command that combines the distgit-command with
            # checking out a particular commit hash.
            # See https://bugzilla.redhat.com/show_bug.cgi?id=1459437 for
            # more info. Once mock-scm supports this feature, we can remove
            # this code.
            distgit_get_branch = "sh -c {}'; git -C {} checkout {}'".format(
                pipes.quote(distgit_get), artifact_name, branch)

            f.writelines([
                "config_opts['scm'] = True\n",
                "config_opts['scm_opts']['method'] = 'distgit'\n",
                "config_opts['scm_opts']['package'] = '{}'\n".format(artifact_name),
                "config_opts['scm_opts']['distgit_get'] = {!r}\n".format(distgit_get_branch),
            ])

            # Set distgit_src_get only if it's defined.
            if distgit_cmds[1]:
                f.write(
                    "config_opts['scm_opts']['distgit_src_get'] = '{}'\n".format(distgit_cmds[1]))

            # The local git repositories cloned by `fedpkg clone` typically do not have
            # the tarballs with sources committed in a git repo. They normally live in lookaside
            # cache on remote server, but we should not try getting them from there for true
            # local builds.
            # Instead, get them from local path with git repository by passing that path to Mock
            # using the `ext_src_dir`.
            if repo_path.startswith("file://"):
                src_dir = repo_path[len("file://"):]
                f.write("config_opts['scm_opts']['ext_src_dir'] = '{}'\n".format(src_dir))

    def _make_executable(self, path):
        mode = os.stat(path).st_mode
        mode |= (mode & 0o444) >> 2  # copy R bits to X
        os.chmod(path, mode)

    def _get_distgit_commands(self, source):
        for host, cmds in conf.distgits.items():
            if source.startswith(host):
                return cmds
        raise KeyError("No defined commands for {}".format(source))

    def get_average_build_time(self, component):
        """
        Get the average build time of the component from Koji
        :param component: a ComponentBuild object
        :return: a float of the average build time in seconds
        """
        # We currently don't track build times in MBS directly, so we can use Koji to get a decent
        # estimate
        if not self.koji_session:
            # If Koji is not configured on the system, then just return 0.0 for components
            try:
                self.koji_session = get_session(self.config, login=False)
                # If the component has not been built before, then None is returned. Instead,
                # let's return 0.0 so the type is consistent
                return self.koji_session.getAverageBuildDuration(component.package) or 0.0
            except Exception:
                log.debug(
                    "The Koji call to getAverageBuildDuration failed. Is Koji properly configured?")
                return 0.0
