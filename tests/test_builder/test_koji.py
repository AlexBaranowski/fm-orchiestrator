# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from collections import OrderedDict
import os
import shutil
import tempfile

import koji
import mock
from mock import patch, MagicMock
import pytest

from module_build_service.builder import GenericBuilder
from module_build_service.builder.KojiModuleBuilder import KojiModuleBuilder
import module_build_service.common.models
from module_build_service.common.config import conf
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import mmd_to_str
from module_build_service.scheduler import events
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.handlers.repos
from tests import init_data, clean_database, make_module_in_db


@pytest.fixture(scope="function")
def mock_get_session():
    koji_session = MagicMock()
    koji_session.getRepo.return_value = {"create_event": "fake event"}

    FakeKojiModuleBuilder.tags = {
        "module-foo": {
            "name": "module-foo",
            "id": 1,
            "arches": "x86_64",
            "locked": False,
            "perm": "admin",
        },
        "module-foo-build": {
            "name": "module-foo-build",
            "id": 2,
            "arches": "x86_64",
            "locked": False,
            "perm": "admin",
        },
    }

    def _get_tag(name):
        return FakeKojiModuleBuilder.tags.get(name, {})

    koji_session.getTag = _get_tag

    def _createTag(name):
        FakeKojiModuleBuilder.tags[name] = {
            "name": name,
            "id": len(FakeKojiModuleBuilder.tags) + 1,
            "arches": "x86_64",
            "locked": False,
            "perm": "admin",
        }

    koji_session.createTag = _createTag

    def _getBuildTarget(name):
        return {
            "build_tag_name": "module-foo-build",
            "dest_tag_name": "module-foo",
        }

    koji_session.getBuildTarget = _getBuildTarget

    def _getAllPerms(*args, **kwargs):
        return [{"id": 1, "name": "admin"}]

    koji_session.getAllPerms = _getAllPerms

    with patch("module_build_service.builder.KojiModuleBuilder.get_session") as mock_get_session:
        mock_get_session.return_value = koji_session
        yield


class FakeKojiModuleBuilder(KojiModuleBuilder):
    @classmethod
    def get_module_build_arches(cls, module):
        return ["x86_64"]


class TestKojiBuilder:
    def setup_method(self, test_method):
        init_data(1)
        events.scheduler.reset()
        self.config = mock.Mock()
        self.config.koji_profile = conf.koji_profile
        self.config.koji_repository_url = conf.koji_repository_url

        self.p_read_config = patch(
            "koji.read_config",
            return_value={
                "authtype": "kerberos",
                "timeout": 60,
                "server": "http://koji.example.com/",
            },
        )
        self.p_read_config.start()

    def teardown_method(self, test_method):
        self.p_read_config.stop()
        events.scheduler.reset()

    def test_tag_to_repo(self):
        """ Test that when a repo msg hits us and we have no match,
        that we do nothing gracefully.
        """
        repo = GenericBuilder.tag_to_repo(
            "koji", self.config, "module-base-runtime-0.25-9", "x86_64"
        )
        assert repo == (
            "https://kojipkgs.stg.fedoraproject.org/repos"
            "/module-base-runtime-0.25-9/latest/x86_64"
        )

    def test_recover_orphaned_artifact_when_tagged(self, mock_get_session):
        """ Test recover_orphaned_artifact when the artifact is found and tagged in both tags
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=[],
        )

        builder.module_tag = {"name": "module-foo", "id": 1}
        builder.module_build_tag = {"name": "module-foo-build", "id": 2}

        # Set listTagged to return test data
        build_tagged = [{"nvr": "foo-1.0-1.module+e0095747", "task_id": 12345, "build_id": 91}]
        dest_tagged = [{"nvr": "foo-1.0-1.module+e0095747", "task_id": 12345, "build_id": 91}]
        builder.koji_session.listTagged.side_effect = [build_tagged, dest_tagged]
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 4)
        module_build.component_builds.sort(key=lambda item: item.id)
        component_build = module_build.component_builds[0]
        component_build.task_id = None
        component_build.state = None
        component_build.nvr = None

        recovered = builder.recover_orphaned_artifact(component_build)
        # recover_orphaned_artifact modifies a component build, but doesn't
        # commit the changes.
        db_session.commit()

        assert recovered

        event_info = events.scheduler.queue[0][3]
        assert event_info == ('recover_orphaned_artifact: fake message', 12345, 1,
                              'rubygem-rails', '1.0', '1.module+e0095747', 4, None)

        event_info = events.scheduler.queue[1][3]
        assert event_info == ('recover_orphaned_artifact: fake message', 'module-foo-build',
                              'foo-1.0-1.module+e0095747')

        event_info = events.scheduler.queue[2][3]
        assert event_info == ('recover_orphaned_artifact: fake message', 'module-foo',
                              'foo-1.0-1.module+e0095747')

        assert component_build.state == koji.BUILD_STATES["COMPLETE"]
        assert component_build.task_id == 12345
        assert component_build.state_reason == "Found existing build"
        assert builder.koji_session.tagBuild.call_count == 0

    def test_recover_orphaned_artifact_when_untagged(self, mock_get_session):
        """ Tests recover_orphaned_artifact when the build is found but untagged
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=[],
        )

        builder.module_tag = {"name": "module-foo", "id": 1}
        builder.module_build_tag = {"name": "module-foo-build", "id": 2}
        dist_tag = "module+2+b8661ee4"
        # Set listTagged to return test data
        builder.koji_session.listTagged.side_effect = [[], [], []]
        untagged = [
            {"id": 9000, "name": "foo", "version": "1.0", "release": "1.{0}".format(dist_tag)}
        ]
        builder.koji_session.untaggedBuilds.return_value = untagged
        build_info = {"nvr": "foo-1.0-1.{0}".format(dist_tag), "task_id": 12345, "build_id": 91}
        builder.koji_session.getBuild.return_value = build_info
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 4)
        module_build.component_builds.sort(key=lambda item: item.id)
        component_build = module_build.component_builds[0]
        component_build.task_id = None
        component_build.nvr = None
        component_build.state = None
        db_session.commit()

        recovered = builder.recover_orphaned_artifact(component_build)
        db_session.commit()

        assert recovered
        event_info = events.scheduler.queue[0][3]
        assert event_info == ('recover_orphaned_artifact: fake message', 12345, 1,
                              'rubygem-rails', '1.0', '1.module+2+b8661ee4', 4, None)

        assert component_build.state == koji.BUILD_STATES["COMPLETE"]
        assert component_build.task_id == 12345
        assert component_build.state_reason == "Found existing build"
        builder.koji_session.tagBuild.assert_called_once_with(2, "foo-1.0-1.{0}".format(dist_tag))

    def test_recover_orphaned_artifact_when_module_build_macros_untagged(self, mock_get_session):
        """ Tests recover_orphaned_artifact when module-build-macros is found but untagged
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=[],
        )

        builder.module_tag = {"name": "module-foo", "id": 1}
        builder.module_build_tag = {"name": "module-foo-build", "id": 2}
        dist_tag = "module+2+b8661ee4"
        # Set listTagged to return test data
        builder.koji_session.listTagged.side_effect = [[], [], []]
        untagged = [
            {"id": 9000,
             "name": "module-build-macros",
             "version": "1.0",
             "release": "1.{0}".format(dist_tag)}
        ]
        builder.koji_session.untaggedBuilds.return_value = untagged
        build_info = {"nvr": "module-build-macros-1.0-1.{0}".format(dist_tag),
                      "task_id": 12345,
                      "build_id": 91}
        builder.koji_session.getBuild.return_value = build_info
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 4)
        module_build.component_builds.sort(key=lambda item: item.id)
        component_build = module_build.component_builds[1]
        component_build.task_id = None
        component_build.nvr = None
        component_build.state = None
        db_session.commit()

        recovered = builder.recover_orphaned_artifact(component_build)
        db_session.commit()

        assert recovered
        event_info = events.scheduler.queue[0][3]
        assert event_info == ('recover_orphaned_artifact: fake message', 12345, 1,
                              'module-build-macros', '1.0', "1.{0}".format(dist_tag), 4, None)

        assert component_build.state == koji.BUILD_STATES["COMPLETE"]
        assert component_build.task_id == 12345
        assert component_build.state_reason == "Found existing build"
        builder.koji_session.tagBuild.assert_called_once_with(
            2, "module-build-macros-1.0-1.{0}".format(dist_tag))
        builder.koji_session.groupPackageListAdd.call_list
        assert builder.koji_session.groupPackageListAdd.call_count == 2
        builder.koji_session.groupPackageListAdd.assert_has_calls(
            [mock.call(2, "srpm-build", "module-build-macros"),
             mock.call(2, "build", "module-build-macros")])

    def test_recover_orphaned_artifact_when_nothing_exists(self, mock_get_session):
        """ Test recover_orphaned_artifact when the build is not found
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=[],
        )

        builder.module_tag = {"name": "module-foo", "id": 1}
        builder.module_build_tag = {"name": "module-foo-build", "id": 2}

        # Set listTagged to return nothing...
        tagged = []
        builder.koji_session.listTagged.return_value = tagged
        untagged = [{"nvr": "foo-1.0-1.nope", "release": "nope"}]
        builder.koji_session.untaggedBuilds.return_value = untagged
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 4)
        component_build = module_build.component_builds[0]
        component_build.task_id = None
        component_build.nvr = None
        component_build.state = None
        db_session.commit()

        recovered = builder.recover_orphaned_artifact(component_build)
        db_session.commit()

        assert not recovered
        # Make sure nothing erroneous gets tag
        assert builder.koji_session.tagBuild.call_count == 0

    @patch("koji.util")
    def test_buildroot_ready(self, mocked_kojiutil, mock_get_session):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        attrs = {"checkForBuilds.return_value": None, "checkForBuilds.side_effect": IOError}
        mocked_kojiutil.configure_mock(**attrs)
        fake_kmb = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-nginx-1.2",
            components=[],
        )
        fake_kmb.module_target = {"build_tag": "module-fake_tag"}

        with pytest.raises(IOError):
            fake_kmb.buildroot_ready()
        assert mocked_kojiutil.checkForBuilds.call_count == 3

    @pytest.mark.parametrize("blocklist", [False, True])
    def test_tagging_already_tagged_artifacts(self, blocklist, mock_get_session):
        """
        Tests that buildroot_add_artifacts and tag_artifacts do not try to
        tag already tagged artifacts
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        if blocklist:
            mmd = module_build.mmd()
            xmd = mmd.get_xmd()
            xmd["mbs_options"] = {"blocked_packages": ["foo", "bar", "new"]}
            mmd.set_xmd(xmd)
            module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-nginx-1.2",
            components=[],
        )

        builder.module_tag = {"name": "module-foo", "id": 1}
        builder.module_build_tag = {"name": "module-foo-build", "id": 2}

        # Set listTagged to return test data
        tagged = [{"nvr": "foo-1.0-1.module_e0095747"}, {"nvr": "bar-1.0-1.module_e0095747"}]
        builder.koji_session.listTagged.return_value = tagged

        # Try to tag one artifact which is already tagged and one new ...
        to_tag = ["foo-1.0-1.module_e0095747", "new-1.0-1.module_e0095747"]
        builder.buildroot_add_artifacts(to_tag)

        if blocklist:
            # "foo" and "new" packages should be unblocked before tagging.
            expected_calls = [
                mock.call("module-foo-build", "foo"),
                mock.call("module-foo-build", "new"),
            ]
        else:
            expected_calls = []
        assert builder.koji_session.packageListUnblock.mock_calls == expected_calls

        # ... only new one should be added.
        builder.koji_session.tagBuild.assert_called_once_with(
            builder.module_build_tag["id"], "new-1.0-1.module_e0095747"
        )

        # Try the same for tag_artifacts(...).
        builder.koji_session.tagBuild.reset_mock()
        builder.tag_artifacts(to_tag)
        builder.koji_session.tagBuild.assert_called_once_with(
            builder.module_tag["id"], "new-1.0-1.module_e0095747"
        )

    @patch.object(FakeKojiModuleBuilder, "_get_tagged_nvrs")
    def test_untagged_artifacts(self, mock_get_tagged_nvrs,):
        """
        Tests that only tagged artifacts will be untagged
        """
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        mock_session = mock.Mock()
        mock_session.getTag.side_effect = [
            {"name": "foobar", "id": 1},
            {"name": "foobar-build", "id": 2},
        ]
        mock_get_tagged_nvrs.side_effect = [["foo", "bar"], ["foo"]]
        path = "module_build_service.builder.KojiModuleBuilder.get_session"
        with patch(path) as mock_get_session:
            mock_get_session.return_value = mock_session
            builder = FakeKojiModuleBuilder(
                db_session=db_session,
                owner=module_build.owner,
                module=module_build,
                config=conf,
                tag_name="module-foo",
                components=[],
            )
            builder.untag_artifacts(["foo", "bar"])

        assert mock_session.untagBuild.call_count == 3
        expected_calls = [mock.call(1, "foo"), mock.call(2, "foo"), mock.call(1, "bar")]
        assert mock_session.untagBuild.mock_calls == expected_calls

    @patch("koji.ClientSession")
    def test_get_build_weights(self, ClientSession):
        session = ClientSession.return_value
        session.getLoggedInUser.return_value = {"id": 123}
        session.multiCall.side_effect = [
            # getPackageID response
            [[1], [2]],
            # listBuilds response
            [[[{"task_id": 456}]], [[{"task_id": 789}]]],
            # getTaskDescendents response
            [
                [{"1": [], "2": [], "3": [{"weight": 1.0}, {"weight": 1.0}]}],
                [{"1": [], "2": [], "3": [{"weight": 1.0}, {"weight": 1.0}]}],
            ],
        ]

        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 2, "apr": 2}

        expected_calls = [mock.call(456), mock.call(789)]
        assert session.getTaskDescendents.mock_calls == expected_calls

        # getLoggedInUser requires to a logged-in session
        session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_build_weights_no_task_id(self, ClientSession):
        session = ClientSession.return_value
        session.getLoggedInUser.return_value = {"id": 123}
        session.multiCall.side_effect = [
            # getPackageID response
            [[1], [2]],
            # listBuilds response
            [[[{"task_id": 456}]], [[{"task_id": None}]]],
            # getTaskDescendents response
            [[{"1": [], "2": [], "3": [{"weight": 1.0}, {"weight": 1.0}]}]],
        ]
        session.getAverageBuildDuration.return_value = None

        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 2, "apr": 1.5}

        expected_calls = [mock.call(456)]
        assert session.getTaskDescendents.mock_calls == expected_calls
        session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_build_weights_no_build(self, ClientSession):
        session = ClientSession.return_value
        session.getLoggedInUser.return_value = {"id": 123}
        session.multiCall.side_effect = [
            # getPackageID response
            [[1], [2]],
            # listBuilds response
            [[[{"task_id": 456}]], [[]]],
            # getTaskDescendents response
            [[{"1": [], "2": [], "3": [{"weight": 1.0}, {"weight": 1.0}]}]],
        ]
        session.getAverageBuildDuration.return_value = None

        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 2, "apr": 1.5}

        expected_calls = [mock.call(456)]
        assert session.getTaskDescendents.mock_calls == expected_calls
        session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_build_weights_listBuilds_failed(self, ClientSession):
        session = ClientSession.return_value
        session.getLoggedInUser.return_value = {"id": 123}
        session.multiCall.side_effect = [[[1], [2]], []]
        session.getAverageBuildDuration.return_value = None

        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 1.5, "apr": 1.5}

        expected_calls = [
            mock.call(
                packageID=1, userID=123, state=1, queryOpts={"limit": 1, "order": "-build_id"}),
            mock.call(
                packageID=2, userID=123, state=1, queryOpts={"limit": 1, "order": "-build_id"}),
        ]
        assert session.listBuilds.mock_calls == expected_calls
        session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_build_weights_getPackageID_failed(self, ClientSession):
        session = ClientSession.return_value
        session.getLoggedInUser.return_value = {"id": 123}
        session.multiCall.side_effect = [[], []]
        session.getAverageBuildDuration.return_value = None

        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 1.5, "apr": 1.5}

        expected_calls = [mock.call("httpd"), mock.call("apr")]
        assert session.getPackageID.mock_calls == expected_calls

        session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_build_weights_getLoggedInUser_failed(self, ClientSession):
        session = ClientSession.return_value
        session.getAverageBuildDuration.return_value = None
        weights = KojiModuleBuilder.get_build_weights(["httpd", "apr"])
        assert weights == {"httpd": 1.5, "apr": 1.5}
        session.krb_login.assert_called_once()

    @pytest.mark.parametrize("blocklist", [False, True])
    @pytest.mark.parametrize("custom_whitelist", [False, True])
    @pytest.mark.parametrize("repo_include_all", [False, True])
    def test_buildroot_connect(
        self, custom_whitelist, blocklist, repo_include_all, mock_get_session
    ):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        db_session.refresh(module_build)

        if blocklist:
            mmd = module_build.mmd()
            xmd = mmd.get_xmd()
            xmd["mbs_options"] = {"blocked_packages": ["foo", "nginx"]}
            mmd.set_xmd(xmd)
            module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        if custom_whitelist:
            mmd = module_build.mmd()
            opts = Modulemd.Buildopts()
            opts.add_rpm_to_whitelist("custom1")
            opts.add_rpm_to_whitelist("custom2")
            mmd.set_buildopts(opts)
            module_build.modulemd = mmd_to_str(mmd)
        else:
            # Set some irrelevant buildopts options to test that KojiModuleBuilder
            # is not confused by this.
            mmd = module_build.mmd()
            opts = Modulemd.Buildopts()
            opts.set_rpm_macros("%my_macro 1")
            mmd.set_buildopts(opts)
            module_build.modulemd = mmd_to_str(mmd)
        db_session.commit()

        if repo_include_all is False:
            mmd = module_build.mmd()
            xmd = mmd.get_xmd()
            mbs_options = xmd["mbs_options"] if "mbs_options" in xmd.keys() else {}
            mbs_options["repo_include_all"] = False
            xmd["mbs_options"] = mbs_options
            mmd.set_xmd(xmd)
            module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        module_build.arches.append(module_build_service.common.models.ModuleArch(name="i686"))
        db_session.commit()

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=["nginx"],
        )
        session = builder.koji_session

        groups = OrderedDict()
        groups["build"] = {"unzip"}
        groups["srpm-build"] = {"fedora-release"}
        builder.buildroot_connect(groups)

        if custom_whitelist:
            expected_calls = [
                mock.call("module-foo", "custom1", "Moe Szyslak"),
                mock.call("module-foo", "custom2", "Moe Szyslak"),
                mock.call("module-foo-build", "custom1", "Moe Szyslak"),
                mock.call("module-foo-build", "custom2", "Moe Szyslak"),
            ]
        else:
            expected_calls = [
                mock.call("module-foo", "nginx", "Moe Szyslak"),
                mock.call("module-foo-build", "nginx", "Moe Szyslak"),
            ]
        assert session.packageListAdd.mock_calls == expected_calls

        expected_calls = [
            mock.call("module-foo-build", "build"),
            mock.call("module-foo-build", "srpm-build"),
        ]
        assert session.groupListAdd.mock_calls == expected_calls

        expected_calls = [
            mock.call("module-foo-build", "build", "unzip"),
            mock.call("module-foo-build", "srpm-build", "fedora-release"),
        ]
        assert session.groupPackageListAdd.mock_calls == expected_calls

        # packageListBlock should not be called, because we set the block list only when creating
        # new Koji tag to prevent overriding it on each buildroot_connect.
        expected_calls = []
        assert session.packageListBlock.mock_calls == expected_calls

        expected_arches = "i686 x86_64"

        expected_calls = [
            mock.call(
                "module-foo",
                arches=expected_arches,
                extra={
                    "mock.package_manager": "dnf",
                    "repo_include_all": repo_include_all,
                    "mock.new_chroot": 0,
                    'mock.yum.module_hotfixes': 1,
                },
            ),
            mock.call(
                "module-foo-build",
                arches=expected_arches,
                extra={
                    "mock.package_manager": "dnf",
                    "repo_include_all": repo_include_all,
                    "mock.new_chroot": 0,
                    'mock.yum.module_hotfixes': 1,
                },
            ),
        ]
        assert session.editTag2.mock_calls == expected_calls

    @pytest.mark.parametrize("blocklist", [False, True])
    def test_buildroot_connect_create_tag(self, blocklist, mock_get_session):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        db_session.refresh(module_build)

        if blocklist:
            mmd = module_build.mmd()
            xmd = mmd.get_xmd()
            xmd["mbs_options"] = {"blocked_packages": ["foo", "nginx"]}
            mmd.set_xmd(xmd)
            module_build.modulemd = mmd_to_str(mmd)
            db_session.commit()

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=["nginx"],
        )
        session = builder.koji_session
        FakeKojiModuleBuilder.tags = {}

        groups = OrderedDict()
        groups["build"] = {"unzip"}
        groups["srpm-build"] = {"fedora-release"}
        builder.buildroot_connect(groups)

        if blocklist:
            expected_calls = [
                mock.call("module-foo-build", "foo"),
                mock.call("module-foo-build", "nginx"),
            ]
        else:
            expected_calls = []
        assert session.packageListBlock.mock_calls == expected_calls

    @pytest.mark.parametrize("scratch", [False, True])
    def test_buildroot_connect_create_target(self, scratch, mock_get_session):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        if scratch:
            module_build.scratch = scratch
            db_session.commit()

        builder = FakeKojiModuleBuilder(
            db_session=db_session,
            owner=module_build.owner,
            module=module_build,
            config=conf,
            tag_name="module-foo",
            components=["nginx"],
        )
        session = builder.koji_session
        session.getBuildTarget = MagicMock()
        session.getBuildTarget.return_value = {}

        groups = OrderedDict()
        groups["build"] = {"unzip"}
        groups["srpm-build"] = {"fedora-release"}
        builder.buildroot_connect(groups)

        if scratch:
            expected_calls = [
                mock.call("scrmod-nginx-1-2-00000000+2", "module-foo-build", "module-foo")
            ]
        else:
            expected_calls = [
                mock.call("module-nginx-1-2-00000000", "module-foo-build", "module-foo")
            ]
        assert session.createBuildTarget.mock_calls == expected_calls

    @patch("koji.ClientSession")
    def test_get_built_rpms_in_module_build(self, ClientSession):
        session = ClientSession.return_value
        session.listTaggedRPMS.return_value = (
            [
                {
                    "build_id": 735939,
                    "name": "tar",
                    "extra": None,
                    "arch": "ppc64le",
                    "buildtime": 1533299221,
                    "id": 6021394,
                    "epoch": 2,
                    "version": "1.30",
                    "metadata_only": False,
                    "release": "4.el8+1308+551bfa71",
                    "buildroot_id": 4321122,
                    "payloadhash": "0621ab2091256d21c47dcac868e7fc2a",
                    "size": 878684,
                },
                {
                    "build_id": 735939,
                    "name": "bar",
                    "extra": None,
                    "arch": "ppc64le",
                    "buildtime": 1533299221,
                    "id": 6021394,
                    "epoch": 2,
                    "version": "1.30",
                    "metadata_only": False,
                    "release": "4.el8+1308+551bfa71",
                    "buildroot_id": 4321122,
                    "payloadhash": "0621ab2091256d21c47dcac868e7fc2a",
                    "size": 878684,
                },
            ],
            [],
        )

        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)

        # Module builds generated by init_data uses generic modulemd file and
        # the module's name/stream/version/context does not have to match it.
        # But for this test, we need it to match.
        mmd = module_build.mmd()
        module_build.name = mmd.get_module_name()
        module_build.stream = mmd.get_stream_name()
        module_build.version = mmd.get_version()
        module_build.context = mmd.get_context()
        db_session.commit()

        ret = KojiModuleBuilder.get_built_rpms_in_module_build(mmd)
        assert set(ret) == {"bar-2:1.30-4.el8+1308+551bfa71", "tar-2:1.30-4.el8+1308+551bfa71"}
        session.assert_not_called()

    @pytest.mark.usefixtures("reuse_component_init_data")
    @pytest.mark.parametrize(
        "br_filtered_rpms,expected",
        (
            (
                ["perl-Tangerine-0.23-1.module+0+d027b723", "not-in-tag-5.0-1.module+0+d027b723"],
                ["not-in-tag-5.0-1.module+0+d027b723"],
            ),
            (
                [
                    "perl-Tangerine-0.23-1.module+0+d027b723",
                    "perl-List-Compare-0.53-5.module+0+d027b723",
                ],
                [],
            ),
            (
                [
                    "perl-Tangerine-0.23-1.module+0+d027b723",
                    "perl-List-Compare-0.53-5.module+0+d027b723",
                    "perl-Tangerine-0.23-1.module+0+d027b723",
                ],
                [],
            ),
            (
                [
                    "perl-Tangerine-0.23-1.module+0+diff_module",
                    "not-in-tag-5.0-1.module+0+d027b723",
                ],
                [
                    "perl-Tangerine-0.23-1.module+0+diff_module",
                    "not-in-tag-5.0-1.module+0+d027b723",
                ],
            ),
            ([], []),
        ),
    )
    @patch("koji.ClientSession")
    def test_get_filtered_rpms_on_self_dep(
        self, ClientSession, br_filtered_rpms, expected
    ):
        session = ClientSession.return_value
        session.listTaggedRPMS.return_value = (
            [
                {
                    "build_id": 12345,
                    "epoch": None,
                    "name": "perl-Tangerine",
                    "release": "1.module+0+d027b723",
                    "version": "0.23",
                },
                {
                    "build_id": 23456,
                    "epoch": None,
                    "name": "perl-List-Compare",
                    "release": "5.module+0+d027b723",
                    "version": "0.53",
                },
                {
                    "build_id": 34567,
                    "epoch": None,
                    "name": "tangerine",
                    "release": "3.module+0+d027b723",
                    "version": "0.22",
                },
            ],
            [
                {
                    "build_id": 12345,
                    "name": "perl-Tangerine",
                    "nvr": "perl-Tangerine-0.23-1.module+0+d027b723",
                },
                {
                    "build_id": 23456,
                    "name": "perl-List-Compare",
                    "nvr": "perl-List-Compare-0.53-5.module+0+d027b723",
                },
                {
                    "build_id": 34567,
                    "name": "tangerine",
                    "nvr": "tangerine-0.22-3.module+0+d027b723",
                },
            ],
        )
        current_module = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 3)
        with patch.object(module_build_service.common.models.ModuleBuild, 'log_message'):
            rv = KojiModuleBuilder._get_filtered_rpms_on_self_dep(current_module, br_filtered_rpms)
        assert set(rv) == set(expected)
        session.assert_not_called()

    @pytest.mark.parametrize(
        "cg_enabled,cg_devel_enabled", [(False, False), (True, False), (True, True)]
    )
    @mock.patch("module_build_service.builder.KojiModuleBuilder.KojiContentGenerator")
    def test_finalize(self, mock_koji_cg_cls, cg_enabled, cg_devel_enabled, mock_get_session):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        db_session.refresh(module_build)
        module_build.state = 2
        db_session.commit()

        with patch(
            "module_build_service.common.config.Config.koji_enable_content_generator",
            new_callable=mock.PropertyMock,
            return_value=cg_enabled,
        ):
            with patch(
                "module_build_service.common.config.Config.koji_cg_devel_module",
                new_callable=mock.PropertyMock,
                return_value=cg_devel_enabled,
            ):
                builder = FakeKojiModuleBuilder(
                    db_session=db_session,
                    owner=module_build.owner,
                    module=module_build,
                    config=conf,
                    tag_name="module-nginx-1.2",
                    components=[],
                )
                builder.finalize()

        mock_koji_cg = mock_koji_cg_cls.return_value
        if cg_enabled:
            if cg_devel_enabled:
                assert mock_koji_cg.koji_import.call_count == 2
                mock_koji_cg.koji_import.assert_has_calls([mock.call(), mock.call(devel=True)])
            else:
                mock_koji_cg.koji_import.assert_called_once_with()
        else:
            mock_koji_cg.koji_import.assert_not_called()

    @patch("koji.ClientSession")
    def test_ensure_builder_use_a_logged_in_koji_session(self, ClientSession):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        builder = KojiModuleBuilder(db_session, "owner", module_build, conf, "module-tag", [])
        builder.koji_session.krb_login.assert_called_once()

    @patch("koji.ClientSession")
    def test_get_module_build_arches(self, ClientSession):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        arches = "x86_64 i686 ppc64le aarch64 s390x"
        session = ClientSession.return_value
        session.getTag.return_value = {"arches": arches}
        ret = KojiModuleBuilder.get_module_build_arches(module_build)
        assert " ".join(ret) == arches

    @patch("koji.ClientSession")
    def test_get_module_build_arches_with_archless_tag(self, ClientSession):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        session = ClientSession.return_value
        session.getTag.return_value = {"arches": ""}
        ret = KojiModuleBuilder.get_module_build_arches(module_build)
        assert ret == []

    @patch("koji.ClientSession")
    def test_get_module_build_arches_without_tag(self, ClientSession):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        module_build.koji_tag = None
        session = ClientSession.return_value
        ret = KojiModuleBuilder.get_module_build_arches(module_build)
        assert ret == []
        session.getTag.assert_not_called()
        session.assert_not_called()

    @patch("koji.ClientSession")
    def test_get_module_build_arches_with_unknown_tag(self, ClientSession):
        module_build = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 2)
        session = ClientSession.return_value
        session.getTag.return_value = None
        with pytest.raises(ValueError, match="Unknown Koji tag .*"):
            KojiModuleBuilder.get_module_build_arches(module_build)


class TestGetDistTagSRPM:
    """Test KojiModuleBuilder.get_disttag_srpm"""

    def setup_method(self):
        clean_database()

        self.tmp_srpm_build_dir = tempfile.mkdtemp(prefix="test-koji-builder-")
        self.spec_file = os.path.join(self.tmp_srpm_build_dir, "module-build-macros.spec")
        self.srpms_dir = os.path.join(self.tmp_srpm_build_dir, "SRPMS")
        os.mkdir(self.srpms_dir)
        self.expected_srpm_file = os.path.join(self.srpms_dir, "module-build-macros.src.rpm")

        # Don't care about the content, just assert the existence.
        with open(self.expected_srpm_file, "w") as f:
            f.write("")

        self.module_nsvc = dict(
            name="testmodule",
            stream="master",
            version="1",
            context=module_build_service.common.models.DEFAULT_MODULE_CONTEXT,
        )

        self.xmd = {
            "mbs": {
                "buildrequires": {
                    "modulea": {
                        "filtered_rpms": ["baz-devel-0:0.1-6.fc28", "baz-doc-0:0.1-6.fc28"]
                    },
                    "platform": {
                        "filtered_rpms": [],
                        "stream_collision_modules": ["modulefoo-s-v-c"],
                        "ursine_rpms": ["foo-0:1.0-1.fc28", "bar-0:2.0-1.fc28"],
                    },
                },
                "ursine_rpms": ["pizza-0:4.0-1.fc32", "spaghetti-0:3.0-1.fc32"],
                "koji_tag": "module-{name}-{stream}-{version}-{context}".format(**self.module_nsvc),
            }
        }

    def teardown_method(self):
        shutil.rmtree(self.tmp_srpm_build_dir)
        clean_database()

    @patch("tempfile.mkdtemp")
    @patch("module_build_service.builder.KojiModuleBuilder.execute_cmd")
    def _build_srpm(self, execute_cmd, mkdtemp):
        module_build = make_module_in_db(
            "{name}:{stream}:{version}:{context}".format(**self.module_nsvc),
            xmd=self.xmd)

        mkdtemp.return_value = self.tmp_srpm_build_dir
        return KojiModuleBuilder.get_disttag_srpm("disttag", module_build)

    def test_return_srpm_file(self):
        srpm_file = self._build_srpm()
        assert self.expected_srpm_file == srpm_file

    def test_filtered_rpms_are_added(self):
        self._build_srpm()

        with open(self.spec_file, "r") as f:
            content = f.read()
        for nevr in ["baz-devel-0:0.1-6.fc28", "baz-doc-0:0.1-6.fc28"]:
            assert KojiModuleBuilder.format_conflicts_line(nevr) + "\n" in content

    def test_ursine_rpms_are_added(self):
        self._build_srpm()

        with open(self.spec_file, "r") as f:
            content = f.read()

        # Stream collision ursine RPMs
        assert "# modulefoo-s-v-c\n" in content
        for nevr in ["foo-0:1.0-1.fc28", "bar-0:2.0-1.fc28"]:
            assert KojiModuleBuilder.format_conflicts_line(nevr) + "\n" in content

        # Conflicting ursine RPMs
        for nevr in ["pizza-0:4.0-1.fc32", "spaghetti-0:3.0-1.fc32"]:
            assert KojiModuleBuilder.format_conflicts_line(nevr) + "\n" in content


def test_generate_koji_tag_in_nsvc_format():
    name, stream, version, context = ("testmodule", "master", "20170816080815", "37c6c57")

    tag = KojiModuleBuilder.generate_koji_tag(name, stream, version, context)

    assert tag == "module-testmodule-master-20170816080815-37c6c57"


def test_generate_koji_tag_in_hash_format():
    name, version, context = ("testmodule", "20170816080815", "37c6c57")
    stream = "this-is-a-stream-with-very-looooong-name" + "-blah" * 50
    nsvc_list = [name, stream, version, context]

    tag = KojiModuleBuilder.generate_koji_tag(*nsvc_list)
    expected_tag = "module-1cf457d452e54dda"
    assert tag == expected_tag


def test_generate_koji_tag_for_scratch_build():
    tag = KojiModuleBuilder.generate_koji_tag(
        "testmodule", "master", "20170816080815", "37c6c57", scratch=True)
    assert tag == "scrmod-testmodule-master-20170816080815-37c6c57+0"

    tag = KojiModuleBuilder.generate_koji_tag(
        "testmodule", "master", "20170816080815", "37c6c57",
        scratch=True, scratch_id=1000)
    assert tag == "scrmod-testmodule-master-20170816080815-37c6c57+1000"
