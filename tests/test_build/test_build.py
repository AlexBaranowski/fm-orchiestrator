# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime, timedelta
import hashlib
import itertools
import json
import os
from os import path, mkdir
from os.path import dirname
import re
import sched
from random import randint
from shutil import copyfile

import fedmsg
import kobo
import koji
from mock import patch, PropertyMock, Mock, MagicMock
import moksha.hub
import pytest
from werkzeug.datastructures import FileStorage

from module_build_service import app
from module_build_service.builder import GenericBuilder
from module_build_service.builder.KojiModuleBuilder import KojiModuleBuilder
from module_build_service.builder.MockModuleBuilder import load_local_builds
from module_build_service.builder.utils import get_rpm_release, validate_koji_tag
from module_build_service.common import conf, log, build_logs, models
from module_build_service.common.errors import Forbidden
from module_build_service.common.utils import load_mmd, import_mmd
from module_build_service.scheduler import events
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.consumer
from module_build_service.scheduler.handlers.components import (
    build_task_finalize as build_task_finalize_handler
)
import module_build_service.scheduler.handlers.repos
from module_build_service.scheduler.handlers.repos import done as repos_done_handler
from module_build_service.scheduler.handlers.tags import tagged as tagged_handler
from tests import clean_database, read_staged_data, staged_data_filename

base_dir = dirname(dirname(__file__))

user = ("Homer J. Simpson", {"packager"})


def make_simple_stop_condition():
    """ Return a simple stop_condition callable.

    Intended to be used with the main() function here in manage.py and tests.

    The stop_condition returns true when the latest module build enters the any
    of the finished states.
    """
    def stop_condition(message):
        # XXX - We ignore the message here and instead just query the DB.

        # Grab the latest module build.
        module = (
            db_session.query(models.ModuleBuild)
            .order_by(models.ModuleBuild.id.desc())
            .first()
        )
        done = (
            models.BUILD_STATES["failed"],
            models.BUILD_STATES["ready"],
            models.BUILD_STATES["done"],
        )
        result = module.state in done
        log.debug("stop_condition checking %r, got %r" % (module, result))

        # moksha.hub.main starts the hub and runs it in a separate thread. When
        # the result is True, remove the db_session from that thread local so
        # that any pending queries in the transaction will not block other
        # queries made from other threads.
        # This is useful for testing particularly.
        if result:
            db_session.remove()

        return result

    return stop_condition


def main(initial_messages, stop_condition):
    """ Run the consumer until some condition is met.

    Setting stop_condition to None will run the consumer forever.
    """

    config = fedmsg.config.load_config()
    config["mbsconsumer"] = True
    config["mbsconsumer.stop_condition"] = stop_condition
    config["mbsconsumer.initial_messages"] = initial_messages

    # Moksha requires that we subscribe to *something*, so tell it /dev/null
    # since we'll just be doing in-memory queue-based messaging for this single
    # build.
    config["zmq_enabled"] = True
    config["zmq_subscribe_endpoints"] = "ipc:///dev/null"

    # Lazy import consumer to avoid potential import cycle.
    # For example, in some cases, importing event message from events.py would
    # cause importing the consumer module, which then starts to import relative
    # code inside handlers module, and the original code is imported eventually.
    import module_build_service.scheduler.consumer

    consumers = [module_build_service.scheduler.consumer.MBSConsumer]

    # Note that the hub we kick off here cannot send any message.  You
    # should use fedmsg.publish(...) still for that.
    moksha.hub.main(
        # Pass in our config dict
        options=config,
        # Only run the specified consumers if any are so specified.
        consumers=consumers,
        # Do not run default producers.
        producers=[],
        # Tell moksha to quiet its logging.
        framework=False,
    )


class FakeSCM(object):
    def __init__(self, mocked_scm, name, mmd_filename, commit=None, version=20180205135154):
        self.mocked_scm = mocked_scm
        self.name = name
        self.commit = commit
        self.version = version
        self.mmd_filename = mmd_filename
        self.sourcedir = None

        self.mocked_scm.return_value.checkout = self.checkout
        self.mocked_scm.return_value.name = self.name
        self.mocked_scm.return_value.branch = "master"
        self.mocked_scm.return_value.get_latest = self.get_latest
        self.mocked_scm.return_value.commit = self.commit
        self.mocked_scm.return_value.version = self.version
        self.mocked_scm.return_value.repository_root = "https://src.stg.fedoraproject.org/modules/"
        self.mocked_scm.return_value.sourcedir = self.sourcedir
        self.mocked_scm.return_value.get_module_yaml = self.get_module_yaml
        self.mocked_scm.return_value.is_full_commit_hash.return_value = commit and len(commit) == 40
        self.mocked_scm.return_value.get_full_commit_hash.return_value = self.get_full_commit_hash

    def checkout(self, temp_dir):
        self.sourcedir = path.join(temp_dir, self.name)
        mkdir(self.sourcedir)
        copyfile(staged_data_filename(self.mmd_filename), self.get_module_yaml())

        return self.sourcedir

    def get_latest(self, ref="master"):
        return ref

    def get_module_yaml(self):
        return path.join(self.sourcedir, self.name + ".yaml")

    def get_full_commit_hash(self, commit_hash=None):
        if not commit_hash:
            commit_hash = self.commit
        sha1_hash = hashlib.sha1("random").hexdigest()
        return commit_hash + sha1_hash[len(commit_hash):]


class FakeModuleBuilder(GenericBuilder):
    """
    Fake module builder which succeeds for every build.
    """

    backend = "test"
    BUILD_STATE = "COMPLETE"
    # Simulates a situation when a component is already built in Koji
    INSTANT_COMPLETE = False
    DEFAULT_GROUPS = None

    on_build_cb = None
    on_cancel_cb = None
    on_finalize_cb = None
    on_buildroot_add_artifacts_cb = None
    on_tag_artifacts_cb = None
    on_buildroot_add_repos_cb = None
    on_get_task_info_cb = None

    @validate_koji_tag("tag_name")
    def __init__(self, db_session, owner, module, config, tag_name, components):
        self.db_session = db_session
        self.module_str = module
        self.tag_name = tag_name
        self.config = config

    @classmethod
    def reset(cls):
        FakeModuleBuilder.BUILD_STATE = "COMPLETE"
        FakeModuleBuilder.INSTANT_COMPLETE = False
        FakeModuleBuilder.on_build_cb = None
        FakeModuleBuilder.on_cancel_cb = None
        FakeModuleBuilder.on_finalize_cb = None
        FakeModuleBuilder.on_buildroot_add_artifacts_cb = None
        FakeModuleBuilder.on_tag_artifacts_cb = None
        FakeModuleBuilder.on_buildroot_add_repos_cb = None
        FakeModuleBuilder.on_get_task_info_cb = None
        FakeModuleBuilder.DEFAULT_GROUPS = None
        FakeModuleBuilder.backend = "test"

    @classmethod
    def get_module_build_arches(cls, module):
        return ["x86_64"]

    def buildroot_connect(self, groups):
        default_groups = FakeModuleBuilder.DEFAULT_GROUPS or {
            "srpm-build": {
                "shadow-utils",
                "fedora-release",
                "redhat-rpm-config",
                "rpm-build",
                "fedpkg-minimal",
                "gnupg2",
                "bash",
            },
            "build": {
                "unzip",
                "fedora-release",
                "tar",
                "cpio",
                "gawk",
                "gcc",
                "xz",
                "sed",
                "findutils",
                "util-linux",
                "bash",
                "info",
                "bzip2",
                "grep",
                "redhat-rpm-config",
                "diffutils",
                "make",
                "patch",
                "shadow-utils",
                "coreutils",
                "which",
                "rpm-build",
                "gzip",
                "gcc-c++",
            },
        }
        if groups != default_groups:
            raise ValueError("Wrong groups in FakeModuleBuilder.buildroot_connect()")

    def buildroot_prep(self):
        pass

    def buildroot_resume(self):
        pass

    def buildroot_ready(self, artifacts=None):
        return True

    def buildroot_add_dependency(self, dependencies):
        pass

    def repo_from_tag(self, config, tag_name, arch):
        pass

    def buildroot_add_artifacts(self, artifacts, install=False):
        if FakeModuleBuilder.on_buildroot_add_artifacts_cb:
            FakeModuleBuilder.on_buildroot_add_artifacts_cb(self, artifacts, install)
        if self.backend == "test":
            for nvr in artifacts:
                # When INSTANT_COMPLETE is on, the components are already in the build tag
                if self.INSTANT_COMPLETE is False:
                    self._send_tag(nvr, dest_tag=False)
        elif self.backend == "testlocal":
            self._send_repo_done()

    def buildroot_add_repos(self, dependencies):
        if FakeModuleBuilder.on_buildroot_add_repos_cb:
            FakeModuleBuilder.on_buildroot_add_repos_cb(self, dependencies)

    def tag_artifacts(self, artifacts, dest_tag=True):
        if FakeModuleBuilder.on_tag_artifacts_cb:
            FakeModuleBuilder.on_tag_artifacts_cb(self, artifacts, dest_tag=dest_tag)

        if self.backend == "test":
            for nvr in artifacts:
                # tag_artifacts received a list of NVRs, but the tag message expects the
                # component name
                self._send_tag(nvr, dest_tag=dest_tag)

    @property
    def koji_session(self):
        session = Mock()

        def _newRepo(tag):
            session.newRepo = self._send_repo_done()
            return 123

        session.newRepo = _newRepo
        if self.on_get_task_info_cb:
            session.getTaskInfo = self.on_get_task_info_cb
        return session

    @property
    def module_build_tag(self):
        return {"name": self.tag_name + "-build"}

    def _send_repo_done(self):
        events.scheduler.add(repos_done_handler, ("fake_msg", self.tag_name + "-build"))

    def _send_tag(self, nvr, dest_tag=True):
        if dest_tag:
            tag = self.tag_name
        else:
            tag = self.tag_name + "-build"
        events.scheduler.add(tagged_handler, ("a faked internal message", tag, nvr))

    def _send_build_change(self, state, name, build_id):
        # build_id=1 and task_id=1 are OK here, because we are building just
        # one RPM at the time.
        args = ("a faked internal message", build_id, state, name, "1", "1", None, None)
        events.scheduler.add(build_task_finalize_handler, args)

    def build(self, artifact_name, source):
        print("Starting building artifact %s: %s" % (artifact_name, source))
        build_id = randint(1000, 9999999)

        if FakeModuleBuilder.on_build_cb:
            FakeModuleBuilder.on_build_cb(self, artifact_name, source)

        if FakeModuleBuilder.BUILD_STATE != "BUILDING":
            self._send_build_change(
                koji.BUILD_STATES[FakeModuleBuilder.BUILD_STATE], artifact_name, build_id)

        reason = "Submitted %s to Koji" % (artifact_name)
        return build_id, koji.BUILD_STATES["BUILDING"], reason, None

    @staticmethod
    def get_disttag_srpm(disttag, module_build):
        # @FIXME
        return KojiModuleBuilder.get_disttag_srpm(disttag, module_build)

    def cancel_build(self, task_id):
        if FakeModuleBuilder.on_cancel_cb:
            FakeModuleBuilder.on_cancel_cb(self, task_id)

    def list_tasks_for_components(self, component_builds=None, state="active"):
        pass

    def recover_orphaned_artifact(self, component_build):
        if self.INSTANT_COMPLETE:
            disttag = get_rpm_release(self.db_session, component_build.module_build)
            # We don't know the version or release, so just use a random one here
            nvr = "{0}-1.0-1.{1}".format(component_build.package, disttag)
            component_build.state = koji.BUILD_STATES["COMPLETE"]
            component_build.nvr = nvr
            component_build.task_id = component_build.id + 51234
            component_build.state_reason = "Found existing build"
            nvr_dict = kobo.rpmlib.parse_nvr(component_build.nvr)
            # Send a message stating the build is complete
            args = ("recover_orphaned_artifact: fake message",
                    component_build.task_id, koji.BUILD_STATES["COMPLETE"],
                    component_build.package, nvr_dict["version"], nvr_dict["release"],
                    component_build.module_build.id, None)
            events.scheduler.add(build_task_finalize_handler, args)
            # Send a message stating that the build was tagged in the build tag
            args = ("recover_orphaned_artifact: fake message",
                    component_build.module_build.koji_tag + "-build",
                    component_build.nvr)
            events.scheduler.add(tagged_handler, args)
            return True

    def finalize(self, succeeded=None):
        if FakeModuleBuilder.on_finalize_cb:
            FakeModuleBuilder.on_finalize_cb(self, succeeded)


def cleanup_moksha():
    # Necessary to restart the twisted reactor for the next test.
    import sys

    del sys.modules["twisted.internet.reactor"]
    del sys.modules["moksha.hub.reactor"]
    del sys.modules["moksha.hub"]
    import moksha.hub.reactor  # noqa


class BaseTestBuild:

    def run_scheduler(self, msgs=None, stop_condition=None):
        main(
            msgs or [],
            stop_condition or make_simple_stop_condition()
        )


@patch("module_build_service.scheduler.handlers.modules.handle_stream_collision_modules")
@patch.object(
    module_build_service.common.config.Config, "system",
    new_callable=PropertyMock,
    return_value="test",
)
@patch(
    "module_build_service.builder.GenericBuilder.default_buildroot_groups",
    return_value={
        "srpm-build": {
            "shadow-utils",
            "fedora-release",
            "redhat-rpm-config",
            "rpm-build",
            "fedpkg-minimal",
            "gnupg2",
            "bash",
        },
        "build": {
            "unzip",
            "fedora-release",
            "tar",
            "cpio",
            "gawk",
            "gcc",
            "xz",
            "sed",
            "findutils",
            "util-linux",
            "bash",
            "info",
            "bzip2",
            "grep",
            "redhat-rpm-config",
            "diffutils",
            "make",
            "patch",
            "shadow-utils",
            "coreutils",
            "which",
            "rpm-build",
            "gzip",
            "gcc-c++",
        },
    },
)
class TestBuild(BaseTestBuild):
    # Global variable used for tests if needed
    _global_var = None

    def setup_method(self, test_method):
        GenericBuilder.register_backend_class(FakeModuleBuilder)
        self.client = app.test_client()
        clean_database()

        def on_get_task_info_cb(cls, task_id):
            return {"state": koji.TASK_STATES["CLOSED"]}

        FakeModuleBuilder.on_get_task_info_cb = on_get_task_info_cb

        self.p_check_gating = patch(
            "module_build_service.scheduler.greenwave.Greenwave.check_gating",
            return_value=True)
        self.mock_check_gating = self.p_check_gating.start()

        self.patch_config_broker = patch.object(
            module_build_service.common.config.Config,
            "celery_broker_url",
            create=True,
            new_callable=PropertyMock,
            return_value=False,
        )
        self.patch_config_broker.start()

    def teardown_method(self, test_method):
        self.p_check_gating.stop()
        self.patch_config_broker.stop()
        FakeModuleBuilder.reset()
        cleanup_moksha()
        for i in range(20):
            try:
                os.remove(build_logs.path(db_session, i))
            except Exception:
                pass

    @pytest.mark.parametrize("mmd_version", [1, 2])
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_normal(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc, mmd_version
    ):
        """
        Tests the build of testmodule.yaml using FakeModuleBuilder which
        succeeds everytime.
        """
        if mmd_version == 1:
            yaml_file = "testmodule.yaml"
        else:
            yaml_file = "testmodule_v2.yaml"
        FakeSCM(mocked_scm, "testmodule", yaml_file, "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        # Check that components are tagged after the batch is built.
        tag_groups = [
            {"perl-Tangerine-1-1", "perl-List-Compare-1-1"},
            {"tangerine-1-1"},
        ]

        def on_finalize_cb(cls, succeeded):
            assert succeeded is True

        def on_tag_artifacts_cb(cls, artifacts, dest_tag=True):
            assert tag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_finalize_cb = on_finalize_cb
        FakeModuleBuilder.on_tag_artifacts_cb = on_tag_artifacts_cb

        # Check that the components are added to buildroot after the batch
        # is built.
        buildroot_groups = [
            {"module-build-macros-1-1"},
            {"perl-Tangerine-1-1", "perl-List-Compare-1-1"},
            {"tangerine-1-1"},
        ]

        def on_buildroot_add_artifacts_cb(cls, artifacts, install):
            assert buildroot_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_buildroot_add_artifacts_cb = on_buildroot_add_artifacts_cb

        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in db_session.query(models.ComponentBuild).filter_by(
                module_id=module_build_id).all():
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

        # All components has to be tagged, so tag_groups and buildroot_groups are empty...
        assert tag_groups == []
        assert buildroot_groups == []
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        assert module_build.module_builds_trace[0].state == models.BUILD_STATES["init"]
        assert module_build.module_builds_trace[1].state == models.BUILD_STATES["wait"]
        assert module_build.module_builds_trace[2].state == models.BUILD_STATES["build"]
        assert module_build.module_builds_trace[3].state == models.BUILD_STATES["done"]
        assert module_build.module_builds_trace[4].state == models.BUILD_STATES["ready"]
        assert len(module_build.module_builds_trace) == 5

    @patch("module_build_service.builder.KojiModuleBuilder.get_session")
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_buildonly(
        self, mocked_scm, mocked_get_user, mocked_get_session, conf_system, dbg, hmsc
    ):
        def list_build_rpms(nvr):
            assert nvr == "perl-List-Compare-1-1"
            return [{"name": "perl-List-Compare"}]
        koji_session = MagicMock()
        koji_session.listBuildRPMs = list_build_rpms
        mocked_get_session.return_value = koji_session

        yaml_file = "testmodule_v2_buildonly.yaml"
        FakeSCM(mocked_scm, "testmodule", yaml_file, "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        # Check that components are tagged after the batch is built.
        tag_groups = [
            {"perl-Tangerine-1-1", "perl-List-Compare-1-1"},
            {"tangerine-1-1"},
        ]

        def on_finalize_cb(cls, succeeded):
            assert succeeded is True

        def on_tag_artifacts_cb(cls, artifacts, dest_tag=True):
            assert tag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_finalize_cb = on_finalize_cb
        FakeModuleBuilder.on_tag_artifacts_cb = on_tag_artifacts_cb

        # Check that the components are added to buildroot after the batch
        # is built.
        buildroot_groups = [
            {"module-build-macros-1-1"},
            {"perl-Tangerine-1-1", "perl-List-Compare-1-1"},
            {"tangerine-1-1"},
        ]

        def on_buildroot_add_artifacts_cb(cls, artifacts, install):
            assert buildroot_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_buildroot_add_artifacts_cb = on_buildroot_add_artifacts_cb

        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in db_session.query(models.ComponentBuild).filter_by(
                module_id=module_build_id).all():
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @pytest.mark.parametrize("gating_result", (True, False))
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_no_components(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc, gating_result
    ):
        """
        Tests the build of a module with no components
        """
        self.mock_check_gating.return_value = gating_result
        FakeSCM(
            mocked_scm,
            "python3",
            "python3-no-components.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        self.run_scheduler()

        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)

        # Make sure no component builds were registered
        assert len(module_build.component_builds) == 0
        # Make sure the build is done
        if gating_result:
            assert module_build.state == models.BUILD_STATES["ready"]
        else:
            assert module_build.state == models.BUILD_STATES["done"]
            assert module_build.state_reason == "Gating failed"

    @patch(
        "module_build_service.common.config.Config.check_for_eol",
        new_callable=PropertyMock,
        return_value=True,
    )
    @patch("module_build_service.common.submit._is_eol_in_pdc", return_value=True)
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_eol_module(
        self, mocked_scm, mocked_get_user, is_eol, check, conf_system, dbg, hmsc
    ):
        """ Tests the build of a module with an eol stream.  """
        FakeSCM(
            mocked_scm,
            "python3",
            "python3-no-components.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )
        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        assert rv.status_code == 400
        data = json.loads(rv.data)
        assert data["status"] == 400
        assert data["message"] == u"Module python3:master is marked as EOL in PDC."

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_from_yaml_not_allowed(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        FakeSCM(mocked_scm, "testmodule", "testmodule.yaml")

        testmodule_filename = staged_data_filename("testmodule.yaml")
        yaml = read_staged_data("testmodule")

        with patch.object(
            module_build_service.common.config.Config,
            "yaml_submit_allowed",
            new_callable=PropertyMock,
            return_value=False,
        ):
            rv = self.client.post(
                "/module-build-service/1/module-builds/",
                content_type="multipart/form-data",
                data={"yaml": (testmodule_filename, yaml)},
            )
            data = json.loads(rv.data)
            assert data["status"] == 403
            assert data["message"] == "YAML submission is not enabled"

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_from_yaml_allowed(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        with patch.object(
            module_build_service.common.config.Config,
            "yaml_submit_allowed",
            new_callable=PropertyMock,
            return_value=True,
        ):
            with open(staged_data_filename("testmodule.yaml"), "rb") as f:
                yaml_file = FileStorage(f)
                rv = self.client.post(
                    "/module-build-service/1/module-builds/",
                    content_type="multipart/form-data",
                    data={"yaml": yaml_file},
                )
            data = json.loads(rv.data)
            module_build_id = data["id"]
            assert module_build_id == 2

        self.run_scheduler()

        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        assert module_build.state == models.BUILD_STATES["ready"]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_cancel(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Submit all builds for a module and cancel the module build later.
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        # This callback is called before return of FakeModuleBuilder.build()
        # method. We just cancel the build here using the web API to simulate
        # user cancelling the build in the middle of building.
        def on_build_cb(cls, artifact_name, source):
            self.client.patch(
                "/module-build-service/1/module-builds/" + str(module_build_id),
                data=json.dumps({"state": "failed"}),
            )

        cancelled_tasks = []

        def on_cancel_cb(cls, task_id):
            cancelled_tasks.append(task_id)

        def on_finalize_cb(cls, succeeded):
            assert succeeded is False

        # We do not want the builds to COMPLETE, but instead we want them
        # to be in the BULDING state after the FakeModuleBuilder.build().
        FakeModuleBuilder.BUILD_STATE = "BUILDING"
        FakeModuleBuilder.on_build_cb = on_build_cb
        FakeModuleBuilder.on_cancel_cb = on_cancel_cb
        FakeModuleBuilder.on_finalize_cb = on_finalize_cb

        self.run_scheduler()

        # Because we did not finished single component build and canceled the
        # module build, all components and even the module itself should be in
        # failed state with state_reason se to cancellation message.
        for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            assert build.state == koji.BUILD_STATES["FAILED"]
            assert build.state_reason == "Canceled by Homer J. Simpson."
            assert build.module_build.state == models.BUILD_STATES["failed"]
            assert build.module_build.state_reason == "Canceled by Homer J. Simpson."

            # Check that cancel_build has been called for this build
            if build.task_id:
                assert build.task_id in cancelled_tasks

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_instant_complete(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests the build of testmodule.yaml using FakeModuleBuilder which
        succeeds everytime.
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]
        FakeModuleBuilder.INSTANT_COMPLETE = True

        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.num_concurrent_builds",
        new_callable=PropertyMock,
        return_value=1,
    )
    def test_submit_build_concurrent_threshold(
        self, conf_num_concurrent_builds, mocked_scm, mocked_get_user,
        conf_system, dbg, hmsc
    ):
        """
        Tests the build of testmodule.yaml using FakeModuleBuilder with
        num_concurrent_builds set to 1.
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        def stop(message):
            """
            Stop the scheduler when the module is built or when we try to build
            more components than the num_concurrent_builds.
            """
            main_stop = make_simple_stop_condition()
            build_count = (
                db_session.query(models.ComponentBuild).filter_by(
                    state=koji.BUILD_STATES["BUILDING"]
                ).count()
            )
            over_threshold = conf.num_concurrent_builds < build_count
            result = main_stop(message) or over_threshold
            db_session.remove()
            return result

        self.run_scheduler(stop_condition=stop)

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            # When this fails, it can mean that num_concurrent_builds
            # threshold has been met.
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.num_concurrent_builds",
        new_callable=PropertyMock,
        return_value=2,
    )
    @patch('module_build_service.scheduler.events.Scheduler.run', autospec=True)
    def test_try_to_reach_concurrent_threshold(
        self, scheduler_run, conf_num_concurrent_builds, mocked_scm, mocked_get_user,
        conf_system, dbg, hmsc
    ):
        """
        Tests that we try to submit new component build right after
        the previous one finished without waiting for all
        the num_concurrent_builds to finish.
        """
        FakeSCM(
            mocked_scm,
            "testmodule-more-components",
            "testmodule-more-components.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )
        self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        # Holds the number of concurrent component builds during
        # the module build.
        TestBuild._global_var = []

        def mocked_scheduler_run(self):
            """
            Store the number of concurrent builds between each handler call to global list so we
            can examine it later.
            """
            num_building = (
                db_session.query(models.ComponentBuild)
                .filter_by(state=koji.BUILD_STATES["BUILDING"])
                .count()
            )
            TestBuild._global_var.append(num_building)
            sched.scheduler.run(self)

        scheduler_run.side_effect = mocked_scheduler_run
        self.run_scheduler()

        # _global_var looks similar to this: [0, 1, 0, 0, 2, 2, 1, 0, 0, 0]
        # It shows the number of concurrent builds in the time. At first we
        # want to remove adjacent duplicate entries, because we only care
        # about changes.
        # We are building two batches, so there should be just two situations
        # when we should be building just single component:
        #   1) module-base-macros in first batch.
        #   2) The last component of second batch.
        # If we are building single component more often, num_concurrent_builds
        # does not work correctly.
        num_builds = [k for k, g in itertools.groupby(TestBuild._global_var)]
        assert num_builds.count(1) == 2

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.num_concurrent_builds",
        new_callable=PropertyMock,
        return_value=1,
    )
    def test_build_in_batch_fails(
        self, conf_num_concurrent_builds, mocked_scm, mocked_get_user,
        conf_system, dbg, hmsc
    ):
        """
        Tests that if the build in batch fails, other components in a batch
        are still build, but next batch is not started.
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        )

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        def on_build_cb(cls, artifact_name, source):
            # fail perl-Tangerine build
            if artifact_name.startswith("perl-Tangerine"):
                FakeModuleBuilder.BUILD_STATE = "FAILED"
            else:
                FakeModuleBuilder.BUILD_STATE = "COMPLETE"

        FakeModuleBuilder.on_build_cb = on_build_cb

        # Check that no components are tagged when single component fails
        # in batch.
        def on_tag_artifacts_cb(cls, artifacts, dest_tag=True):
            raise ValueError("No component should be tagged.")

        FakeModuleBuilder.on_tag_artifacts_cb = on_tag_artifacts_cb

        self.run_scheduler()

        for c in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            # perl-Tangerine is expected to fail as configured in on_build_cb.
            if c.package == "perl-Tangerine":
                assert c.state == koji.BUILD_STATES["FAILED"]
            # tangerine is expected to fail, because it is in batch 3, but
            # we had a failing component in batch 2.
            elif c.package == "tangerine":
                assert c.state == koji.BUILD_STATES["FAILED"]
                assert c.state_reason == "Component(s) perl-Tangerine failed to build."
            else:
                assert c.state == koji.BUILD_STATES["COMPLETE"]

            # Whole module should be failed.
            assert c.module_build.state == models.BUILD_STATES["failed"]
            assert c.module_build.state_reason == "Component(s) perl-Tangerine failed to build."

            # We should end up with batch 2 and never start batch 3, because
            # there were failed components in batch 2.
            assert c.module_build.batch == 2

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.num_concurrent_builds",
        new_callable=PropertyMock,
        return_value=1,
    )
    def test_all_builds_in_batch_fail(
        self, conf_num_concurrent_builds, mocked_scm, mocked_get_user,
        conf_system, dbg, hmsc
    ):
        """
        Tests that if the build in batch fails, other components in a batch
        are still build, but next batch is not started.
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        def on_build_cb(cls, artifact_name, source):
            # Next components *after* the module-build-macros will fail
            # to build.
            if not artifact_name.startswith("module-build-macros"):
                FakeModuleBuilder.BUILD_STATE = "FAILED"

        FakeModuleBuilder.on_build_cb = on_build_cb

        self.run_scheduler()

        for c in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            # perl-Tangerine is expected to fail as configured in on_build_cb.
            if c.package == "module-build-macros":
                assert c.state == koji.BUILD_STATES["COMPLETE"]
            else:
                assert c.state == koji.BUILD_STATES["FAILED"]

            # Whole module should be failed.
            assert c.module_build.state == models.BUILD_STATES["failed"]
            assert re.match(
                r"Component\(s\) (perl-Tangerine|perl-List-Compare), "
                "(perl-Tangerine|perl-List-Compare) failed to build.",
                c.module_build.state_reason,
            )

            # We should end up with batch 2 and never start batch 3, because
            # there were failed components in batch 2.
            assert c.module_build.batch == 2

    @pytest.mark.usefixtures("reuse_component_init_data")
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_reuse_all(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that we do not try building module-build-macros when reusing all
        components in a module build.
        """
        def on_build_cb(cls, artifact_name, source):
            raise ValueError("All components should be reused, not build.")

        FakeModuleBuilder.on_build_cb = on_build_cb

        # Check that components are tagged after the batch is built.
        tag_groups = []
        tag_groups.append(
            {
                "perl-Tangerine-0.23-1.module+0+d027b723",
                "perl-List-Compare-0.53-5.module+0+d027b723",
                "tangerine-0.22-3.module+0+d027b723",
            }
        )

        def on_tag_artifacts_cb(cls, artifacts, dest_tag=True):
            if dest_tag is True:
                assert tag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_tag_artifacts_cb = on_tag_artifacts_cb

        buildtag_groups = []
        buildtag_groups.append({
            "perl-Tangerine-0.23-1.module+0+d027b723",
            "perl-List-Compare-0.53-5.module+0+d027b723",
            "tangerine-0.22-3.module+0+d027b723",
        })

        def on_buildroot_add_artifacts_cb(cls, artifacts, install):
            assert buildtag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_buildroot_add_artifacts_cb = on_buildroot_add_artifacts_cb

        from module_build_service.scheduler.db_session import db_session

        # Create a dedicated database session for scheduler to avoid hang
        self.run_scheduler(
            msgs=[{
                "msg_id": "local module build",
                "event": events.MBS_MODULE_STATE_CHANGE,
                "module_build_id": 3,
                "module_build_state": 1
            }]
        )

        reused_component_ids = {
            "module-build-macros": None,
            "tangerine": 3,
            "perl-Tangerine": 1,
            "perl-List-Compare": 2,
        }

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, 3).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]
            assert build.reused_component_id == reused_component_ids[build.package]

    @pytest.mark.usefixtures("reuse_component_init_data")
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_reuse_all_without_build_macros(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that we can reuse components even when the reused module does
        not have module-build-macros component.
        """
        # Firstly, remove all existing module-build-macros component builds

        from module_build_service.scheduler.db_session import db_session

        macros_cb_query = db_session.query(models.ComponentBuild).filter_by(
            package="module-build-macros")
        db_session.query(models.ComponentBuildTrace).filter(
            models.ComponentBuildTrace.component_id.in_(
                [cb.id for cb in macros_cb_query.all()]
            )
        ).delete(synchronize_session=False)
        macros_cb_query.delete(synchronize_session=False)
        db_session.commit()

        # Just ensure the success of removal
        assert 0 == macros_cb_query.count()

        def on_build_cb(cls, artifact_name, source):
            raise ValueError("All components should be reused, not build.")

        FakeModuleBuilder.on_build_cb = on_build_cb

        # Check that components are tagged after the batch is built.
        tag_groups = []
        tag_groups.append(
            {
                "perl-Tangerine-0.23-1.module+0+d027b723",
                "perl-List-Compare-0.53-5.module+0+d027b723",
                "tangerine-0.22-3.module+0+d027b723",
            }
        )

        def on_tag_artifacts_cb(cls, artifacts, dest_tag=True):
            if dest_tag is True:
                assert tag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_tag_artifacts_cb = on_tag_artifacts_cb

        buildtag_groups = []
        buildtag_groups.append({
            "perl-Tangerine-0.23-1.module+0+d027b723",
            "perl-List-Compare-0.53-5.module+0+d027b723",
            "tangerine-0.22-3.module+0+d027b723",
        })

        def on_buildroot_add_artifacts_cb(cls, artifacts, install):
            assert buildtag_groups.pop(0) == set(artifacts)

        FakeModuleBuilder.on_buildroot_add_artifacts_cb = on_buildroot_add_artifacts_cb

        self.run_scheduler(
            msgs=[{
                "msg_id": "local module build",
                "event": events.MBS_MODULE_STATE_CHANGE,
                "module_build_id": 3,
                "module_build_state": 1
            }]
        )

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, 3).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]
            assert build.package != "module-build-macros"

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_resume(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that resuming the build works even when previous batches
        are already built.
        """
        now = datetime.utcnow()
        submitted_time = now - timedelta(minutes=3)
        # Create a module in the failed state
        build_one = models.ModuleBuild()
        build_one.name = "testmodule"
        build_one.stream = "master"
        build_one.version = "2820180205135154"
        build_one.build_context = "return_runtime_context"
        build_one.runtime_context = "9c690d0e"
        build_one.context = "9c690d0e"
        build_one.state = models.BUILD_STATES["failed"]
        build_one.modulemd = read_staged_data("formatted_testmodule")
        build_one.koji_tag = "module-testmodule-master-20180205135154-9c690d0e"
        build_one.scmurl = "https://src.stg.fedoraproject.org/modules/testmodule.git?#7fea453"
        build_one.batch = 2
        build_one.owner = "Homer J. Simpson"
        build_one.time_submitted = submitted_time
        build_one.time_modified = now
        build_one.rebuild_strategy = "changed-and-after"

        db_session.add(build_one)
        db_session.commit()

        # It went from init, to wait, to build, and then failed
        mbt_one = models.ModuleBuildTrace(
            state_time=submitted_time, state=models.BUILD_STATES["init"]
        )
        mbt_two = models.ModuleBuildTrace(
            state_time=now - timedelta(minutes=2), state=models.BUILD_STATES["wait"]
        )
        mbt_three = models.ModuleBuildTrace(
            state_time=now - timedelta(minutes=1), state=models.BUILD_STATES["build"]
        )
        mbt_four = models.ModuleBuildTrace(state_time=now, state=build_one.state)
        build_one.module_builds_trace.append(mbt_one)
        build_one.module_builds_trace.append(mbt_two)
        build_one.module_builds_trace.append(mbt_three)
        build_one.module_builds_trace.append(mbt_four)

        db_session.commit()

        # Successful component
        db_session.add_all([
            models.ComponentBuild(
                module_id=build_one.id,
                package="perl-Tangerine",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/perl-Tangerine.git?#master",
                state=koji.BUILD_STATES["COMPLETE"],
                nvr="perl-Tangerine-0:0.22-2.module+0+d027b723",
                batch=2,
                ref="7e96446223f1ad84a26c7cf23d6591cd9f6326c6",
                tagged=True,
                tagged_in_final=True),
            # Failed component
            models.ComponentBuild(
                module_id=build_one.id,
                package="perl-List-Compare",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/perl-List-Compare.git?#master",
                state=koji.BUILD_STATES["FAILED"],
                batch=2),
            # Component that isn't started yet
            models.ComponentBuild(
                module_id=build_one.id,
                package="tangerine",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/tangerine.git?#master",
                batch=3),
            # module-build-macros
            models.ComponentBuild(
                module_id=build_one.id,
                package="module-build-macros",
                format="rpms",
                state=koji.BUILD_STATES["COMPLETE"],
                scmurl=(
                    "/tmp/module_build_service-build-macrosqr4AWH/SRPMS/module-build-macros-0.1-1."
                    "module_testmodule_master_20170109091357.src.rpm"
                ),
                batch=1,
                tagged=True,
                build_time_only=True),
        ])
        db_session.commit()
        db_session.expire_all()

        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")
        # Resubmit the failed module
        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        components = (
            db_session.query(models.ComponentBuild)
            .filter_by(module_id=module_build_id, batch=2)
            .order_by(models.ComponentBuild.id)
            .all()
        )
        # Make sure the build went from failed to wait
        assert module_build.state == models.BUILD_STATES["wait"]
        assert module_build.state_reason == "Resubmitted by Homer J. Simpson"
        # Make sure the state was reset on the failed component
        assert components[1].state is None
        db_session.expire_all()

        # Run the backend
        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in module_build.component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_resume_recover_orphaned_macros(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that resuming the build works when module-build-macros is orphaned but marked as
        failed in the database
        """
        FakeModuleBuilder.INSTANT_COMPLETE = True
        now = datetime.utcnow()
        submitted_time = now - timedelta(minutes=3)

        # Create a module in the failed state
        build_one = models.ModuleBuild()
        build_one.name = "testmodule"
        build_one.stream = "master"
        build_one.version = "2820180205135154"
        build_one.build_context = "return_runtime_context"
        build_one.runtime_context = "9c690d0e"
        build_one.state = models.BUILD_STATES["failed"]
        # this is not calculated by real but just a value to
        # match the calculated context from expanded test mmd
        build_one.context = "9c690d0e"
        build_one.modulemd = read_staged_data("formatted_testmodule")
        build_one.koji_tag = "module-testmodule-master-20180205135154-6ef9a711"
        build_one.scmurl = "https://src.stg.fedoraproject.org/modules/testmodule.git?#7fea453"
        build_one.batch = 2
        build_one.owner = "Homer J. Simpson"
        build_one.time_submitted = submitted_time
        build_one.time_modified = now
        build_one.rebuild_strategy = "changed-and-after"
        db_session.add(build_one)
        db_session.commit()

        # It went from init, to wait, to build, and then failed
        mbt_one = models.ModuleBuildTrace(
            state_time=submitted_time, state=models.BUILD_STATES["init"])
        mbt_two = models.ModuleBuildTrace(
            state_time=now - timedelta(minutes=2), state=models.BUILD_STATES["wait"])
        mbt_three = models.ModuleBuildTrace(
            state_time=now - timedelta(minutes=1), state=models.BUILD_STATES["build"])
        mbt_four = models.ModuleBuildTrace(state_time=now, state=build_one.state)
        build_one.module_builds_trace.append(mbt_one)
        build_one.module_builds_trace.append(mbt_two)
        build_one.module_builds_trace.append(mbt_three)
        build_one.module_builds_trace.append(mbt_four)

        db_session.commit()
        db_session.expire_all()

        # Components that haven't started yet
        db_session.add_all([
            models.ComponentBuild(
                module_id=build_one.id,
                package="perl-Tangerine",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/perl-Tangerine.git?#master",
                batch=2),
            models.ComponentBuild(
                module_id=build_one.id,
                package="perl-List-Compare",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/perl-List-Compare.git?#master",
                batch=2),
            models.ComponentBuild(
                module_id=build_one.id,
                package="tangerine",
                format="rpms",
                scmurl="https://src.stg.fedoraproject.org/rpms/tangerine.git?#master",
                batch=3),
            # Failed module-build-macros
            models.ComponentBuild(
                module_id=build_one.id,
                package="module-build-macros",
                format="rpms",
                state=koji.BUILD_STATES["FAILED"],
                scmurl=(
                    "/tmp/module_build_service-build-macrosqr4AWH/SRPMS/module-build-macros-0.1-1."
                    "module_testmodule_master_20180205135154.src.rpm"
                ),
                batch=1,
                build_time_only=True)
        ])

        db_session.commit()

        FakeSCM(mocked_scm, "testmodule", "testmodule.yaml", "7fea453")
        # Resubmit the failed module
        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#7fea453",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # Make sure the build went from failed to wait
        assert module_build.state == models.BUILD_STATES["wait"]
        assert module_build.state_reason == "Resubmitted by Homer J. Simpson"
        # Make sure the state was reset on the failed component
        for c in module_build.component_builds:
            assert c.state is None
        db_session.expire_all()

        # Run the backend
        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_resume_failed_init(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that resuming the build works when the build failed during the init step
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        with patch("module_build_service.scheduler.handlers.modules.format_mmd") as mock_format_mmd:
            mock_format_mmd.side_effect = Forbidden("Custom component repositories aren't allowed.")
            rv = self.client.post(
                "/module-build-service/1/module-builds/",
                data=json.dumps({
                    "branch": "master",
                    "scmurl": "https://src.stg.fedoraproject.org/modules/"
                    "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
                }),
            )
            # Run the backend so that it fails in the "init" handler
            self.run_scheduler()
            cleanup_moksha()

        module_build_id = json.loads(rv.data)["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        db_session.refresh(module_build)
        assert module_build.state == models.BUILD_STATES["failed"]
        assert module_build.state_reason == "Custom component repositories aren't allowed."
        assert len(module_build.module_builds_trace) == 2
        assert module_build.module_builds_trace[0].state == models.BUILD_STATES["init"]
        assert module_build.module_builds_trace[1].state == models.BUILD_STATES["failed"]

        # Resubmit the failed module
        self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": (
                    "https://src.stg.fedoraproject.org/modules/testmodule.git?"
                    "#620ec77321b2ea7b0d67d82992dda3e1d67055b4"
                ),
            }),
        )

        db_session.refresh(module_build)
        components = (
            db_session.query(models.ComponentBuild)
            .filter_by(module_id=module_build_id, batch=2)
            .order_by(models.ComponentBuild.id)
            .all()
        )
        # Make sure the build went from failed to init
        assert module_build.state == models.BUILD_STATES["init"]
        assert module_build.state_reason == "Resubmitted by Homer J. Simpson"
        # Make sure there are no components
        assert components == []

        # Run the backend again
        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        db_session.refresh(module_build)
        # for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
        for build in module_build.component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_resume_init_fail(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that resuming the build fails when the build is in init state
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")
        # Post so a module is in the init phase
        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )
        assert rv.status_code == 201
        # Run the backend
        self.run_scheduler()
        # Post again and make sure it fails
        rv2 = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )
        data = json.loads(rv2.data)
        expected = {
            "error": "Conflict",
            "message": (
                "Module (state=5) already exists. Only a new build, resubmission of a "
                "failed build or build against new buildrequirements is allowed."
            ),
            "status": 409,
        }
        assert data == expected

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.modules_allow_scratch",
        new_callable=PropertyMock,
        return_value=True,
    )
    def test_submit_scratch_vs_normal(
        self, mocked_allow_scratch, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that submitting a scratch build with the same NSV as a previously
        completed normal build succeeds and both have expected contexts
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")
        # Post so a module is in the init phase
        post_url = "/module-build-service/1/module-builds/"
        post_data = {
            "branch": "master",
            "scmurl": "https://src.stg.fedoraproject.org/modules/"
            "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            "scratch": False,
        }
        rv = self.client.post(post_url, data=json.dumps(post_data))
        assert rv.status_code == 201
        data = json.loads(rv.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure normal build has expected context without a suffix
        assert module_build.context == "9c690d0e"
        # Run the backend
        self.run_scheduler()
        # Post again as a scratch build and make sure it succeeds
        post_data["scratch"] = True
        rv2 = self.client.post(post_url, data=json.dumps(post_data))
        assert rv2.status_code == 201
        data = json.loads(rv2.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure scratch build has expected context with unique suffix
        assert module_build.context == "9c690d0e_1"

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.modules_allow_scratch",
        new_callable=PropertyMock,
        return_value=True,
    )
    def test_submit_normal_vs_scratch(
        self, mocked_allow_scratch, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that submitting a normal build with the same NSV as a previously
        completed scratch build succeeds and both have expected contexts
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4"
        )
        # Post so a scratch module build is in the init phase
        post_url = "/module-build-service/1/module-builds/"
        post_data = {
            "branch": "master",
            "scmurl": "https://src.stg.fedoraproject.org/modules/"
            "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            "scratch": True,
        }
        rv = self.client.post(post_url, data=json.dumps(post_data))
        assert rv.status_code == 201
        data = json.loads(rv.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure scratch build has expected context with unique suffix
        assert module_build.context == "9c690d0e_1"
        # Run the backend
        self.run_scheduler()
        # Post again as a non-scratch build and make sure it succeeds
        post_data["scratch"] = False
        rv2 = self.client.post(post_url, data=json.dumps(post_data))
        assert rv2.status_code == 201
        data = json.loads(rv2.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure normal build has expected context without suffix
        assert module_build.context == "9c690d0e"

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.modules_allow_scratch",
        new_callable=PropertyMock,
        return_value=True,
    )
    def test_submit_scratch_vs_scratch(
        self, mocked_allow_scratch, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that submitting a scratch build with the same NSV as a previously
        completed scratch build succeeds and both have expected contexts
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")
        # Post so a scratch module build is in the init phase
        post_url = "/module-build-service/1/module-builds/"
        post_data = {
            "branch": "master",
            "scmurl": "https://src.stg.fedoraproject.org/modules/"
            "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            "scratch": True,
        }
        rv = self.client.post(post_url, data=json.dumps(post_data))
        assert rv.status_code == 201
        data = json.loads(rv.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure first scratch build has expected context with unique suffix
        assert module_build.context == "9c690d0e_1"
        # Run the backend
        self.run_scheduler()
        # Post scratch build again and make sure it succeeds
        rv2 = self.client.post(post_url, data=json.dumps(post_data))
        assert rv2.status_code == 201
        data = json.loads(rv2.data)
        module_build_id = data["id"]
        module_build = models.ModuleBuild.get_by_id(db_session, module_build_id)
        # make sure second scratch build has expected context with unique suffix
        assert module_build.context == "9c690d0e_2"

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_build_repo_regen_not_started_batch(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Tests that if MBS starts a new batch, the concurrent component threshold is met before a
        build can start, and an unexpected repo regen occurs, the build will not fail.

        See: https://pagure.io/fm-orchestrator/issue/864
        """
        FakeSCM(
            mocked_scm, "testmodule", "testmodule.yaml", "620ec77321b2ea7b0d67d82992dda3e1d67055b4")

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        def _stop_condition(message):
            # Stop the backend if the module batch is 2 (where we simulate the concurrent threshold
            # being met). For safety, also stop the backend if the module erroneously completes.
            module = models.ModuleBuild.get_by_id(db_session, module_build_id)
            result = module.batch == 2 or module.state >= models.BUILD_STATES["done"]
            if result:
                db_session.remove()
            return result

        with patch(
            "module_build_service.scheduler.batches.at_concurrent_component_threshold"
        ) as mock_acct:
            # Once we get to batch 2, then simulate the concurrent threshold being met
            def _at_concurrent_component_threshold(config):
                return models.ModuleBuild.get_by_id(db_session, module_build_id).batch == 2

            mock_acct.side_effect = _at_concurrent_component_threshold
            self.run_scheduler(stop_condition=_stop_condition)

        # Only module-build-macros should be built
        for build in (
            db_session.query(models.ComponentBuild).filter_by(module_id=module_build_id).all()
        ):
            if build.package == "module-build-macros":
                assert build.state == koji.BUILD_STATES["COMPLETE"]
            else:
                assert build.state is None
            assert build.module_build.state == models.BUILD_STATES["build"]

        # Simulate a random repo regen message that MBS didn't expect
        cleanup_moksha()
        module = models.ModuleBuild.get_by_id(db_session, module_build_id)
        events_info = [{
            "msg_id": "a faked internal message",
            "event": events.KOJI_REPO_CHANGE,
            "tag_name": module.koji_tag + "-build"
        }]
        db_session.expire_all()
        # Stop after processing the seeded message

        def stop(message):
            db_session.remove()
            return True

        self.run_scheduler(events_info, stop_condition=stop)

        # Make sure the module build didn't fail so that the poller can resume it later
        module = models.ModuleBuild.get_by_id(db_session, module_build_id)
        assert module.state == models.BUILD_STATES["build"]

    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    def test_submit_br_metadata_only_module(
        self, mocked_scm, mocked_get_user, conf_system, dbg, hmsc
    ):
        """
        Test that when a build is submitted with a buildrequire without a Koji tag,
        MBS doesn't supply it as a dependency to the builder.
        """
        metadata_mmd = load_mmd(read_staged_data("build_metadata_module"))
        import_mmd(db_session, metadata_mmd)

        FakeSCM(
            mocked_scm,
            "testmodule",
            "testmodule_br_metadata_module.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )
        post_url = "/module-build-service/1/module-builds/"
        post_data = {
            "branch": "master",
            "scmurl": "https://src.stg.fedoraproject.org/modules/"
            "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        }
        rv = self.client.post(post_url, data=json.dumps(post_data))
        assert rv.status_code == 201

        data = json.loads(rv.data)
        module_build_id = data["id"]

        def on_buildroot_add_repos_cb(cls, dependencies):
            # Make sure that the metadata module is not present since it doesn't have a Koji tag
            assert set(dependencies.keys()) == {"module-f28-build"}

        FakeModuleBuilder.on_buildroot_add_repos_cb = on_buildroot_add_repos_cb
        self.run_scheduler()

        module = models.ModuleBuild.get_by_id(db_session, module_build_id)
        assert module.state == models.BUILD_STATES["ready"]


@patch(
    "module_build_service.common.config.Config.system",
    new_callable=PropertyMock,
    return_value="testlocal",
)
class TestLocalBuild(BaseTestBuild):
    def setup_method(self, test_method):
        FakeModuleBuilder.on_build_cb = None
        FakeModuleBuilder.backend = "testlocal"
        GenericBuilder.register_backend_class(FakeModuleBuilder)
        self.client = app.test_client()
        clean_database()

    def teardown_method(self, test_method):
        FakeModuleBuilder.reset()
        cleanup_moksha()
        for i in range(20):
            try:
                os.remove(build_logs.path(db_session, i))
            except Exception:
                pass

    @patch("module_build_service.scheduler.handlers.modules.handle_stream_collision_modules")
    @patch("module_build_service.web.auth.get_user", return_value=user)
    @patch("module_build_service.common.scm.SCM")
    @patch(
        "module_build_service.common.config.Config.mock_resultsdir",
        new_callable=PropertyMock,
        return_value=staged_data_filename('local_builds'),
    )
    def test_submit_build_local_dependency(
        self, resultsdir, mocked_scm, mocked_get_user, conf_system, hmsc
    ):
        """
        Tests local module build dependency.
        """
        load_local_builds(["platform:f30"])
        FakeSCM(
            mocked_scm,
            "testmodule",
            "testmodule-buildrequires-f30.yaml",
            "620ec77321b2ea7b0d67d82992dda3e1d67055b4",
        )

        rv = self.client.post(
            "/module-build-service/1/module-builds/",
            data=json.dumps({
                "branch": "master",
                "scmurl": "https://src.stg.fedoraproject.org/modules/"
                "testmodule.git?#620ec77321b2ea7b0d67d82992dda3e1d67055b4",
            }),
        )

        data = json.loads(rv.data)
        module_build_id = data["id"]

        # Local base-runtime has changed profiles, so we can detect we use
        # the local one and not the main one.
        FakeModuleBuilder.DEFAULT_GROUPS = {"srpm-build": {"bar"}, "build": {"foo"}}

        self.run_scheduler()

        # All components should be built and module itself should be in "done"
        # or "ready" state.
        for build in models.ModuleBuild.get_by_id(db_session, module_build_id).component_builds:
            assert build.state == koji.BUILD_STATES["COMPLETE"]
            assert build.module_build.state in [
                models.BUILD_STATES["done"],
                models.BUILD_STATES["ready"],
            ]
