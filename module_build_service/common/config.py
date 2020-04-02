# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import imp
import logging
import os
import re
import sys
import tempfile

import pkg_resources
from six import string_types

from module_build_service.common import logger
# This avoids creating a circular import by importing log from module_build_service
log = logging.getLogger('module_build_service')


# TODO: It'd be nice to reuse this from models.ModuleBuild.rebuild_strategies but models.py
# currently relies on this file, so we can't import it
SUPPORTED_STRATEGIES = ["changed-and-after", "only-changed", "all"]

SUPPORTED_RESOLVERS = {
    "mbs": {"builders": ["mock"]},
    "db": {"builders": ["koji", "mock", "copr"]},
    "local": {"builders": ["mock"]},
    "koji": {"builders": ["koji"]},
}


class BaseConfiguration(object):
    DEBUG = False
    SECRET_KEY = os.urandom(16)
    SQLALCHEMY_DATABASE_URI = "sqlite:///{0}".format(os.path.join(
        os.getcwd(), "module_build_service.db"))
    SQLALCHEMY_TRACK_MODIFICATIONS = True
    # Where we should run when running "manage.py run" directly.
    HOST = "0.0.0.0"
    PORT = 5000


class TestConfiguration(BaseConfiguration):
    LOG_LEVEL = "debug"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URI", "sqlite:///:memory:")
    DEBUG = True
    MESSAGING = "in_memory"
    PDC_URL = "https://pdc.fedoraproject.org/rest_api/v1"

    # Global network-related values, in seconds
    NET_TIMEOUT = 3
    NET_RETRY_INTERVAL = 1
    # SCM network-related values, in seconds
    SCM_NET_TIMEOUT = 0.1
    SCM_NET_RETRY_INTERVAL = 0.1

    KOJI_CONFIG = "./conf/koji.conf"
    KOJI_PROFILE = "staging"
    SERVER_NAME = "localhost"

    KOJI_REPOSITORY_URL = "https://kojipkgs.stg.fedoraproject.org/repos"
    SCMURLS = ["https://src.stg.fedoraproject.org/modules/"]

    ALLOWED_GROUPS_TO_IMPORT_MODULE = {"mbs-import-module"}

    # Greenwave configuration
    GREENWAVE_URL = "https://greenwave.example.local/api/v1.0/"
    GREENWAVE_DECISION_CONTEXT = "test_dec_context"
    GREENWAVE_SUBJECT_TYPE = "some-module"

    STREAM_SUFFIXES = {r"^el\d+\.\d+\.\d+\.z$": 0.1}

    # Ensures task.delay executes locally instead of scheduling a task to a queue.
    CELERY_TASK_ALWAYS_EAGER = True


class ProdConfiguration(BaseConfiguration):
    pass


class LocalBuildConfiguration(BaseConfiguration):
    CACHE_DIR = "~/modulebuild/cache"
    LOG_LEVEL = "debug"
    MESSAGING = "in_memory"

    ALLOW_CUSTOM_SCMURLS = True
    RESOLVER = "mbs"
    RPMS_ALLOW_REPOSITORY = True
    MODULES_ALLOW_REPOSITORY = True

    # Celery tasks will be executed locally for local builds
    CELERY_TASK_ALWAYS_EAGER = True


class OfflineLocalBuildConfiguration(LocalBuildConfiguration):
    RESOLVER = "local"


class DevConfiguration(LocalBuildConfiguration):
    DEBUG = True


def init_config():
    """
    Create the global MBS configuration.

    :return: a tuple with the first index being the configuration and second index as the
        configuration class that was used to initialize the configuration. This can be useful
        to configure Flask with.
    :rtype: tuple(Config, object)
    """
    config_file = os.environ.get("MBS_CONFIG_FILE", "/etc/module-build-service/config.py")

    try:
        config_module = imp.load_source("mbs_runtime_config", config_file)
        log.info("Using the configuration file at %s", config_file)
    except Exception:
        log.warning("The configuration file at %s was not present", config_file)
        # Default to this file as the configuration module
        config_module = sys.modules[__name__]

    true_options = ("1", "on", "true", "y", "yes")
    if any(["py.test" in arg or "pytest" in arg for arg in sys.argv]):
        config_section = "TestConfiguration"
        # Get the configuration from this module
        config_module = sys.modules[__name__]
    elif os.environ.get("MODULE_BUILD_SERVICE_DEVELOPER_ENV", "").lower() in true_options:
        config_section = "DevConfiguration"
        # Get the configuration from this module
        config_module = sys.modules[__name__]
    elif "build_module_locally" in sys.argv:
        if "--offline" in sys.argv:
            config_section = "OfflineLocalBuildConfiguration"
        else:
            config_section = "LocalBuildConfiguration"
    else:
        config_section = "ProdConfiguration"

    config_section = os.environ.get("MBS_CONFIG_SECTION", config_section)
    if hasattr(config_module, config_section):
        log.info("Using the configuration section %s", config_section)
        config_section_obj = getattr(config_module, config_section)
    else:
        log.error(
            "The configuration class of %s is not present. Falling back to the default "
            "ProdConfiguration class.",
            config_section,
        )
        config_section_obj = ProdConfiguration
    conf = Config(config_section_obj)
    return conf, config_section_obj


class Path:
    """
    Config type for paths. Expands the users home directory.
    """

    pass


class Config(object):
    """Class representing the orchestrator configuration."""

    _defaults = {
        "debug": {"type": bool, "default": False, "desc": "Debug mode"},
        "system": {"type": str, "default": "koji", "desc": "The buildsystem to use."},
        "db": {"type": str, "default": "", "desc": "RDB URL."},
        "default_dist_tag_prefix": {
            "type": str,
            "default": "module+",
            "desc": "Default dist-tag prefix for built modules.",
        },
        "polling_interval": {"type": int, "default": 600, "desc": "Polling interval, in seconds."},
        "cache_dir": {
            "type": Path,
            "default": os.path.join(tempfile.gettempdir(), "mbs"),
            "desc": "Cache directory"
        },
        "mbs_url": {
            "type": str,
            "default": "https://mbs.fedoraproject.org/module-build-service/1/module-builds/",
            "desc": "MBS instance url for MBSResolver",
        },
        "check_for_eol": {
            "type": bool,
            "default": False,
            "desc": "Flag to determine whether or not MBS should block EOL modules from building.",
        },
        "pdc_url": {
            "type": str,
            "default": "https://pdc.fedoraproject.org/rest_api/v1",
            "desc": "PDC URL, used for checking stream EOL.",
        },
        "koji_config": {
            "type": str,
            "default": "/etc/module-build-service/koji.conf",
            "desc": "Koji config file."
        },
        "koji_profile": {"type": str, "default": "koji", "desc": "Koji config profile."},
        "arches": {"type": list, "default": ["x86_64"], "desc": "Koji architectures."},
        "allow_arch_override": {
            "type": bool,
            "default": False,
            "desc": "Allow to support a custom architecture set",
        },
        "koji_build_priority": {"type": int, "default": 10, "desc": ""},
        "koji_repository_url": {
            "type": str,
            "default": "https://kojipkgs.fedoraproject.org/repos",
            "desc": "Koji repository URL."
        },
        "koji_build_macros_target": {
            "type": str,
            "default": "",
            "desc": 'Target to build "module-build-macros" RPM in.',
        },
        "koji_tag_prefixes": {
            "type": list,
            "default": ["module", "scrmod"],
            "desc": "List of allowed koji tag prefixes.",
        },
        "koji_tag_permission": {
            "type": str,
            "default": "admin",
            "desc": "Permission name to require for newly created Koji tags.",
        },
        "koji_tag_extra_opts": {
            "type": dict,
            "default": {
                "mock.package_manager": "dnf",
                # This is needed to include all the Koji builds (and therefore
                # all the packages) from all inherited tags into this tag.
                # See https://pagure.io/koji/issue/588 and
                # https://pagure.io/fm-orchestrator/issue/660 for background.
                "repo_include_all": True,
                # Has been requested by Fedora infra in
                # https://pagure.io/fedora-infrastructure/issue/7620.
                # Disables systemd-nspawn for chroot.
                "mock.new_chroot": 0,
                # Works around fail-safe mechanism added in DNF 4.2.7
                # https://pagure.io/fedora-infrastructure/issue/8410
                "mock.yum.module_hotfixes": 1,
            },
            "desc": "Extra options set for newly created Koji tags.",
        },
        "koji_target_delete_time": {
            "type": int,
            "default": 24 * 3600,
            "desc": "Time in seconds after which the Koji target of built module is deleted",
        },
        "koji_enable_content_generator": {
            "type": bool,
            "default": True,
            "desc": "Enable or disable imports to koji using content generator api",
        },
        "allow_name_override_from_scm": {
            "type": bool,
            "default": False,
            "desc": "Allow modulemd files to override the module name "
            "if different from the scm repo name.",
        },
        "allow_stream_override_from_scm": {
            "type": bool,
            "default": False,
            "desc": "Allow modulemd files to override the module stream "
            "if different from the scm repo branch.",
        },
        "allow_custom_scmurls": {"type": bool, "default": False, "desc": "Allow custom scmurls."},
        "rpms_default_repository": {
            "type": str,
            "default": "https://src.fedoraproject.org/rpms/",
            "desc": "RPMs default repository URL.",
        },
        "rpms_allow_repository": {
            "type": bool,
            "default": False,
            "desc": "Allow custom RPMs repositories.",
        },
        "rpms_default_cache": {
            "type": str,
            "default": "http://pkgs.fedoraproject.org/repo/pkgs/",
            "desc": "RPMs default cache URL.",
        },
        "rpms_allow_cache": {"type": bool, "default": False, "desc": "Allow custom RPMs cache."},
        "modules_default_repository": {
            "type": str,
            "default": "https://src.fedoraproject.org/modules/",
            "desc": "Included modules default repository URL.",
        },
        "modules_allow_repository": {
            "type": bool,
            "default": False,
            "desc": "Allow custom included modules repositories.",
        },
        "allowed_groups": {
            "type": set,
            "default": {"packager"},
            "desc": "The set of groups allowed to submit builds.",
        },
        "allowed_groups_to_import_module": {
            "type": set,
            "default": set(),
            "desc": "The set of groups allowed to import module builds.",
        },
        "log_backend": {"type": str, "default": None, "desc": "Log backend"},
        "log_file": {"type": str, "default": "", "desc": "Path to log file"},
        "log_level": {"type": str, "default": "info", "desc": "Log level"},
        "build_logs_dir": {
            "type": Path,
            "default": tempfile.gettempdir(),
            "desc": "Directory to store module build logs to.",
        },
        "build_logs_name_format": {
            "type": str,
            "default": "build-{id}.log",
            "desc": (
                "Format of a module build log's name. Use `Build` attributes as formatting "
                "kwargs"
            ),
        },
        "krb_keytab": {"type": None, "default": None, "desc": ""},
        "krb_principal": {"type": None, "default": None, "desc": ""},
        "messaging": {"type": str, "default": "fedmsg", "desc": "The messaging system to use."},
        "messaging_topic_prefix": {
            "type": list,
            "default": ["org.fedoraproject.prod"],
            "desc": "The messaging system topic prefixes which we are interested in.",
        },
        "distgits": {
            "type": dict,
            "default": {
                "https://src.fedoraproject.org": (
                    "fedpkg clone --anonymous {}",
                    "fedpkg --release module sources",
                ),
                "file://": ("git clone {repo_path}", None),
            },
            "desc": "Mapping between dist-git and command to ",
        },
        "mock_config": {"type": str, "default": "fedora-25-x86_64", "desc": ""},
        "mock_config_file": {
            "type": list,
            "default": ["/etc/module-build-service/mock.cfg", "conf/mock.cfg"],
            "desc": "List of mock config file paths in order of preference.",
        },
        "mock_build_srpm_cmd": {"type": str, "default": "fedpkg --release f26 srpm", "desc": ""},
        "mock_resultsdir": {
            "type": Path,
            "default": "~/modulebuild/builds",
            "desc": "Directory for Mock build results.",
        },
        "mock_purge_useless_logs": {
            "type": bool,
            "default": True,
            "desc": "Remove empty or otherwise useless log files.",
        },
        "arch_autodetect": {
            "type": bool,
            "default": True,
            "desc": "Auto-detect machine arch when configuring builder.",
        },
        "arch_fallback": {
            "type": str,
            "default": "x86_64",
            "desc": "Fallback arch if auto-detection is off or unable to determine it.",
        },
        "scmurls": {
            "type": list,
            "default": ["https://src.fedoraproject.org/modules/"],
            "desc": "Allowed SCM URLs for submitted module.",
        },
        "yaml_submit_allowed": {
            "type": bool,
            "default": False,
            "desc": "Is it allowed to directly submit build by modulemd yaml file?",
        },
        "num_concurrent_builds": {
            "type": int,
            "default": 5,
            "desc": "Number of concurrent component builds.",
        },
        "net_timeout": {
            "type": int,
            "default": 120,
            "desc": "Global network timeout for read/write operations, in seconds.",
        },
        "net_retry_interval": {
            "type": int,
            "default": 30,
            "desc": "Global network retry interval for read/write operations, in seconds.",
        },
        "scm_net_timeout": {
            "type": int,
            "default": 60,
            "desc": "Network timeout for SCM operations, in seconds.",
        },
        "scm_net_retry_interval": {
            "type": int,
            "default": 15,
            "desc": "Network retry interval for SCM operations, in seconds.",
        },
        "no_auth": {"type": bool, "default": False, "desc": "Disable client authentication."},
        "admin_groups": {
            "type": set,
            "default": set(),
            "desc": "The set of groups allowed to manage MBS.",
        },
        "yum_config_file": {
            "type": list,
            "default": ["/etc/module-build-service/yum.conf", "conf/yum.conf"],
            "desc": "List of yum config file paths in order of preference.",
        },
        "auth_method": {
            "type": str,
            "default": "oidc",
            "desc": "Authentiation method to MBS. Options are oidc or kerberos",
        },
        "ldap_uri": {
            "type": str,
            "default": "",
            "desc": "LDAP URI to query for group information when using Kerberos authentication",
        },
        "ldap_groups_dn": {
            "type": str,
            "default": "",
            "desc": (
                "The distinguished name of the container or organizational unit containing "
                "the groups in LDAP"
            ),
        },
        "base_module_names": {
            "type": list,
            "default": ["platform"],
            "desc": (
                "List of base module names which define the product version "
                "(by their stream) of modules depending on them."
            ),
        },
        "base_module_arches": {
            "type": dict,
            "default": {},
            "desc": "Per base-module name:stream Koji arches list.",
        },
        "allow_only_compatible_base_modules": {
            "type": bool,
            "default": True,
            "desc": "When True, only modules built on top of compatible base modules are "
                    "considered by MBS as possible buildrequirement. When False, modules "
                    "built against any base module stream can be used as a buildrequire.",
        },
        "koji_cg_tag_build": {
            "type": bool,
            "default": True,
            "desc": "Indicate whether tagging build is enabled during importing module to Koji.",
        },
        "koji_cg_devel_module": {
            "type": bool,
            "default": True,
            "desc": "Indicate whether a devel module should be imported into Koji.",
        },
        "koji_cg_build_tag_template": {
            "type": str,
            "default": "{}-modular-updates-candidate",
            "desc": "Name of a Koji tag where the top-level Content Generator "
            "build is tagged to. The '{}' string is replaced by a "
            "stream name of a base module on top of which the "
            "module is built.",
        },
        "koji_cg_default_build_tag": {
            "type": str,
            "default": "modular-updates-candidate",
            "desc": "The name of Koji tag which should be used as fallback "
            "when koji_cg_build_tag_template tag is not found in "
            "Koji.",
        },
        "rebuild_strategy": {
            "type": str,
            "default": "changed-and-after",
            "desc": "The module rebuild strategy to use by default.",
        },
        "rebuild_strategy_allow_override": {
            "type": bool,
            "default": False,
            "desc": (
                "Allows a user to specify the rebuild strategy they want to use when "
                "submitting a module build."
            ),
        },
        "rebuild_strategies_allowed": {
            "type": list,
            "default": SUPPORTED_STRATEGIES,
            "desc": (
                "The allowed module rebuild strategies. This is only used when "
                "REBUILD_STRATEGY_ALLOW_OVERRIDE is True."
            ),
        },
        "cleanup_failed_builds_time": {
            "type": int,
            "default": 180,
            "desc": (
                "Time in days when to cleanup failed module builds and transition them to "
                'the "garbage" state.'
            ),
        },
        "cleanup_stuck_builds_time": {
            "type": int,
            "default": 7,
            "desc": (
                "Time in days when to cleanup stuck module builds and transition them to "
                'the "failed" state. The module has to be in a state defined by the '
                '"cleanup_stuck_builds_states" option.'
            ),
        },
        "cleanup_stuck_builds_states": {
            "type": list,
            "default": ["init", "build"],
            "desc": (
                "States of builds which will be considered to move to failed state when a"
                " build is in one of those states longer than the value configured in the "
                '"cleanup_stuck_builds_time"'
            ),
        },
        "resolver": {
            "type": str,
            "default": "db",
            "desc": "Where to look up for modules. Note that this can (and "
            "probably will) be builder-specific.",
        },
        "koji_external_repo_url_prefix": {
            "type": str,
            "default": "https://kojipkgs.fedoraproject.org/",
            "desc": "URL prefix of base module's external repo.",
        },
        "allowed_users": {
            "type": set,
            "default": set(),
            "desc": "The users/service accounts that don't require to be part of a group",
        },
        "br_stream_override_module": {
            "type": str,
            "default": "platform",
            "desc": (
                "The module name to override in the buildrequires based on the branch name. "
                '"br_stream_override_regexes" must also be set for this to take '
                "effect."
            ),
        },
        "br_stream_override_regexes": {
            "type": list,
            "default": [],
            "desc": (
                "The list of regexes used to parse the stream override from the branch name. "
                '"br_stream_override_module" must also be set for this to take '
                "effect. The regexes can contain multiple capture groups that will be "
                "concatenated. Any null capture groups will be ignored. The first regex that "
                "matches the branch will be used."
            ),
        },
        "default_buildroot_packages": {
            "type": list,
            "default": [
                "bash",
                "bzip2",
                "coreutils",
                "cpio",
                "diffutils",
                "findutils",
                "gawk",
                "gcc",
                "gcc-c++",
                "grep",
                "gzip",
                "info",
                "make",
                "patch",
                "fedora-release",
                "redhat-rpm-config",
                "rpm-build",
                "sed",
                "shadow-utils",
                "tar",
                "unzip",
                "util-linux",
                "which",
                "xz",
            ],
            "desc": ("The list packages for offline module build RPM buildroot."),
        },
        "default_srpm_buildroot_packages": {
            "type": list,
            "default": [
                "bash",
                "gnupg2",
                "fedora-release",
                "redhat-rpm-config",
                "fedpkg-minimal",
                "rpm-build",
                "shadow-utils",
            ],
            "desc": ("The list packages for offline module build RPM buildroot."),
        },
        "greenwave_decision_context": {
            "type": str,
            "default": "",
            "desc": "The Greenwave decision context that determines a module's gating status.",
        },
        "allowed_privileged_module_names": {
            "type": list,
            "default": [],
            "desc": (
                "List of modules that are allowed to influence the RPM buildroot when "
                "buildrequired. These modules can set xmd.mbs.disttag_marking to do change "
                "the RPM disttag, or set the xmd.mbs.koji_tag_arches to set the arches "
                "for which the modules are built. MBS will use this list order to determine "
                "which modules take precedence."
            ),
        },
        "stream_suffixes": {
            "type": dict,
            "default": {},
            "desc": "A mapping of platform stream regular expressions and the "
            "corresponding suffix added to formatted stream version. "
            'For example, {r"regexp": 0.1, ...}',
        },
        "greenwave_url": {
            "type": str,
            "default": "",
            "desc": "The URL of the server where Greenwave is running (should include "
                    "the root of the API)"
        },
        "greenwave_subject_type": {
            "type": str,
            "default": "",
            "desc": "Subject type for Greenwave requests"
        },
        "greenwave_timeout": {
            "type": int,
            "default": 60,
            "desc": "Greenwave response timeout"
        },
        "modules_allow_scratch": {
            "type": bool,
            "default": False,
            "desc": "Allow module scratch builds",
        },
        "scratch_build_only_branches": {
            "type": list,
            "default": [],
            "desc": "The list of regexes used to identify branches from which only the module "
                    "scratch builds can be built",
        },
        "product_pages_url": {
            "type": str,
            "default": "",
            "desc": "The URL to the Product Pages. This is queried to determine if a base module "
                    "stream has been released. If it has, the stream may be modified automatically "
                    "to use a different support stream.",
        },
        "product_pages_module_streams": {
            "type": dict,
            "default": {},
            "desc": "The keys are regexes of base module streams that should be checked in the Red "
                    "Hat Product Pages. The values are tuples. The first value is a string that "
                    "should be appended to the stream if there is a match and the release the "
                    "stream represents has been released. The second value is a template string "
                    "that represents the release in Product Pages and can accept format kwargs of "
                    "x, y, and z (represents the version). The third value is an optional template "
                    "string that represent the Product Pages release for major releases "
                    "(e.g. 8.0.0). After the first match, the rest will be ignored."
        },
        "product_pages_schedule_task_name": {
            "type": str,
            "default": "",
            "desc": "A schedule task name. This is used to check Product schedules for cases where "
                    "the stream should be used before release."
        },
        "num_threads_for_build_submissions": {
            "type": int,
            "default": 5,
            "desc": "The number of threads when submitting component builds to an external build "
                    "system.",
        },
        "default_modules_scm_url": {
            "type": str,
            "default": "https://pagure.io/releng/fedora-module-defaults.git",
            "desc": "The SCM URL to the default modules repo, which will be used to determine "
                    "which buildrequires to automatically include. This can be overridden with "
                    "the xmd.mbs.default_modules_scm_url key in the base module's modulemd.",
        },
        "uses_rawhide": {
            "type": bool,
            "default": True,
            "desc": "Denotes if the concept of rawhide exists in the infrastructure of this "
                    "MBS deployment.",
        },
        "rawhide_branch": {
            "type": str,
            "default": "master",
            "desc": "Denotes the branch used for rawhide.",
        },
        "dnf_minrate": {
            "type": int,
            "default": 1024 * 100,  # 100KB
            "desc": "The minrate configuration on a DNF repo. This configuration will cause DNF to "
                    "timeout loading a repo if the download speed is below minrate for the "
                    "duration of the timeout."
        },
        "dnf_timeout": {
            "type": int,
            "default": 30,
            "desc": "The timeout configuration for dnf operations, in seconds."
        },
        "num_workers": {"type": int, "default": 1, "desc": "Number of Celery workers"},
        "celery_task_always_eager": {
            "type": bool,
            "default": False,
            "desc": "All Celery tasks will be executed locally by blocking until the task returns "
                    "when this is True",
        },
        "celery_task_routes": {
            "type": list,
            "default": ["module_build_service.scheduler.route.route_task"],
            "desc": "A list of Celery routers. When deciding the final destination queue of a "
                    "Celery task the routers are consulted in order",
        },
        "celery_worker_prefetch_multiplier": {
            "type": int,
            "default": 1,
            "desc": "This defaults to 1 so that the worker doesn't fetch more messages than it can "
                    "handle at a time. This so that general tasks aren't starved when running "
                    "a long handler.",
        },
        "celery_broker_url": {
            "type": str,
            "default": "",
            "desc": "The broker URL used by the Celery application instance.",
        },
        "celery_imports": {
            "type": list,
            "default": [
                "module_build_service.scheduler.handlers.components",
                "module_build_service.scheduler.handlers.modules",
                "module_build_service.scheduler.handlers.repos",
                "module_build_service.scheduler.handlers.tags",
                "module_build_service.scheduler.handlers.greenwave",
                "module_build_service.scheduler.producer",
            ],
            "desc": "The list Python paths for the Celery application to import.",
        },
    }

    def __init__(self, conf_section_obj):
        """
        Initialize the Config object with defaults and then override them
        with runtime values.
        """

        # set defaults
        for name, values in self._defaults.items():
            self.set_item(name, values["default"], values["type"])

        # override defaults
        for key in dir(conf_section_obj):
            # skip keys starting with underscore
            if key.startswith("_"):
                continue
            # set item (lower key)
            self.set_item(key.lower(), getattr(conf_section_obj, key))

    def set_item(self, key, value, value_type=None):
        """
        Set value for configuration item. Creates the self._key = value
        attribute and self.key property to set/get/del the attribute.
        """
        if key == "set_item" or key.startswith("_"):
            raise Exception("Configuration item's name is not allowed: %s" % key)

        # Create the empty self._key attribute, so we can assign to it.
        if not hasattr(self, "_" + key):
            setattr(self, "_" + key, None)

            # Create self.key property to access the self._key attribute.
            # Use the setifok_func if available for the attribute.
            setifok_func = "_setifok_{}".format(key)
            if hasattr(self, setifok_func):
                setx = lambda self, val: getattr(self, setifok_func)(val)
            elif value_type == Path:
                # For paths, expanduser.
                setx = lambda self, val: setattr(self, "_" + key, os.path.expanduser(val))
            else:
                setx = lambda self, val: setattr(self, "_" + key, val)
            getx = lambda self: getattr(self, "_" + key)
            delx = lambda self: delattr(self, "_" + key)
            setattr(Config, key, property(getx, setx, delx))

        # managed/registered configuration items
        if key in self._defaults:
            # type conversion for configuration item
            convert = self._defaults[key]["type"]
            if convert in [bool, int, list, str, set, dict]:
                try:
                    # Do no try to convert None...
                    if value is not None:
                        value = convert(value)
                except Exception:
                    raise TypeError("Configuration value conversion failed for name: %s" % key)
            # unknown type/unsupported conversion, or conversion not needed
            elif convert is not None and convert not in [Path]:
                raise TypeError(
                    "Unsupported type %s for configuration item name: %s" % (convert, key))

        # Set the attribute to the correct value
        setattr(self, key, value)

    #
    # Register your _setifok_* handlers here
    #

    def _setifok_system(self, s):
        s = str(s)
        if s not in ("koji", "mock"):
            raise ValueError("Unsupported buildsystem: %s." % s)
        self._system = s

    def _setifok_polling_interval(self, i):
        if not isinstance(i, int):
            raise TypeError("polling_interval needs to be an int")
        if i < 0:
            raise ValueError("polling_interval must be >= 0")
        self._polling_interval = i

    def _setifok_rpms_default_repository(self, s):
        rpm_repo = str(s)
        rpm_repo = rpm_repo.rstrip("/") + "/"
        self._rpms_default_repository = rpm_repo

    def _setifok_rpms_default_cache(self, s):
        rpm_cache = str(s)
        rpm_cache = rpm_cache.rstrip("/") + "/"
        self._rpms_default_cache = rpm_cache

    def _setifok_log_backend(self, s):
        if s is None:
            s = "console"
        elif s not in logger.supported_log_backends():
            raise ValueError("Unsupported log backend")
        self._log_backend = str(s)

    def _setifok_log_file(self, s):
        if s is None:
            self._log_file = ""
        else:
            self._log_file = str(s)

    def _setifok_log_level(self, s):
        level = str(s).lower()
        self._log_level = logger.str_to_log_level(level)

    def _setifok_messaging(self, s):
        """ Validate that the specified messaging backend corresponds with one
        of the installed plugins.  The MBS core provides two such plugins, but
        a third-party could install another usable one.
        """
        entrypoints = pkg_resources.iter_entry_points("mbs.messaging_backends")
        installed_backends = [e.name for e in entrypoints]
        s = str(s)
        if s not in installed_backends:
            raise ValueError(
                'The messaging plugin for "{0}" is not installed.'
                " The following are installed: {1}".format(s, ", ".join(installed_backends))
            )
        self._messaging = s

    def _setifok_amq_recv_addresses(self, l):
        assert isinstance(l, list) or isinstance(l, tuple)
        self._amq_recv_addresses = list(l)

    def _setifok_scmurls(self, l):
        if not isinstance(l, list):
            raise TypeError("scmurls needs to be a list.")
        self._scmurls = [str(x) for x in l]

    def _setifok_num_concurrent_builds(self, i):
        if not isinstance(i, int):
            raise TypeError("NUM_CONCURRENT_BUILDS needs to be an int")
        if i < 0:
            raise ValueError("NUM_CONCURRENT_BUILDS must be >= 0")
        self._num_concurrent_builds = i

    def _setifok_auth_method(self, s):
        s = str(s)
        if s.lower() not in ("oidc", "kerberos"):
            raise ValueError("Unsupported authentication method")
        if s.lower() == "kerberos":
            try:
                import ldap3  # noqa
            except ImportError:
                raise ValueError("ldap3 is required for kerberos authz")
        self._auth_method = s.lower()

    def _setifok_ldap_uri(self, s):
        ldap_uri = str(s)

        if ldap_uri and not re.match(r"^(?:ldap(?:s)?:\/\/.+)$", ldap_uri):
            raise ValueError('LDAP_URI is invalid. It must start with "ldap://" or "ldaps://"')

        self._ldap_uri = ldap_uri

    def _setifok_rebuild_strategy(self, strategy):
        if strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(
                'The strategy "{0}" is not supported. Choose from: {1}'.format(
                    strategy, ", ".join(SUPPORTED_STRATEGIES)
                )
            )
        self._rebuild_strategy = strategy

    def _setifok_base_module_arches(self, data):
        if not isinstance(data, dict):
            raise ValueError("BASE_MODULE_ARCHES must be a dict")
        for ns, arches in data.items():
            if len(ns.split(":")) != 2:
                raise ValueError("BASE_MODULE_ARCHES keys must be in 'name:stream' format")
            if not isinstance(arches, list):
                raise ValueError("BASE_MODULE_ARCHES values must be lists")
        self._base_module_arches = data

    def _setifok_rebuild_strategies_allowed(self, strategies):
        if not isinstance(strategies, list):
            raise ValueError("REBUILD_STRATEGIES_ALLOWED must be a list")
        elif not strategies:
            raise ValueError(
                "REBUILD_STRATEGIES_ALLOWED must contain at least one rebuild strategy")
        for strategy in strategies:
            if strategy not in SUPPORTED_STRATEGIES:
                raise ValueError(
                    "REBUILD_STRATEGIES_ALLOWED must be one of: {0}".format(
                        ", ".join(SUPPORTED_STRATEGIES))
                )

        self._rebuild_strategies_allowed = strategies

    def _setifok_cleanup_failed_builds_time(self, num_days):
        if num_days < 1:
            raise ValueError("CLEANUP_FAILED_BUILDS_TIME must be set to 1 or more days")
        self._cleanup_failed_builds_time = num_days

    def _setifok_resolver(self, s):
        if s not in SUPPORTED_RESOLVERS.keys():
            raise ValueError(
                'The resolver "{0}" is not supported. Choose from: {1}'.format(
                    s, ", ".join(SUPPORTED_RESOLVERS.keys()))
            )
        self._resolver = s

    def _setifok_product_pages_module_streams(self, d):
        if not isinstance(d, dict):
            raise ValueError("PRODUCT_PAGES_MODULE_STREAMS must be a dict")

        for regex, values in d.items():
            try:
                re.compile(regex)
            except (TypeError, re.error):
                raise ValueError(
                    'The regex `%r` in the configuration "PRODUCT_PAGES_MODULE_STREAMS" is invalid'
                    % regex
                )

            if not isinstance(values, list) and not isinstance(values, tuple):
                raise ValueError(
                    'The values in the configured dictionary for "PRODUCT_PAGES_MODULE_STREAMS" '
                    "must be a list or tuple"
                )

            if len(values) != 3:
                raise ValueError(
                    "There must be three entries in each value in the dictionary configured for "
                    '"PRODUCT_PAGES_MODULE_STREAMS"'
                )

            for i, value in enumerate(values):
                if not isinstance(value, string_types):
                    # The last value is optional
                    if value is None and i == 2:
                        continue

                    raise ValueError(
                        'The value in the %i index of the values in "PRODUCT_PAGES_MODULE_STREAMS" '
                        "must be a string"
                        % i
                    )

        self._product_pages_module_streams = d

    def _setifok_num_threads_for_build_submissions(self, i):
        if not isinstance(i, int):
            raise TypeError("NUM_THREADS_FOR_BUILD_SUBMISSIONS needs to be an int")
        if i < 1:
            raise ValueError("NUM_THREADS_FOR_BUILD_SUBMISSIONS must be >= 1")
        self._num_threads_for_build_submissions = i


conf, config_section = init_config()
