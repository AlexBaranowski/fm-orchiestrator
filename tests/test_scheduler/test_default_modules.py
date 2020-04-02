# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from collections import namedtuple
import errno

import dnf
from mock import call, Mock, patch, PropertyMock
import pytest

from module_build_service.common.config import conf
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.models import ModuleBuild
from module_build_service.common.utils import import_mmd, load_mmd, mmd_to_str
from module_build_service.scheduler import default_modules
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, make_module_in_db, read_staged_data


@patch("module_build_service.scheduler.default_modules.handle_collisions_with_base_module_rpms")
@patch("module_build_service.scheduler.default_modules._get_default_modules")
def test_add_default_modules(mock_get_dm, mock_hc):
    """
    Test that default modules present in the database are added, and the others are ignored.
    """
    clean_database()
    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))
    xmd_brs = mmd.get_xmd()["mbs"]["buildrequires"]
    assert set(xmd_brs.keys()) == {"platform"}

    platform = ModuleBuild.get_build_from_nsvc(
        db_session,
        "platform",
        xmd_brs["platform"]["stream"],
        xmd_brs["platform"]["version"],
        xmd_brs["platform"]["context"],
    )
    assert platform
    platform_mmd = platform.mmd()
    platform_xmd = mmd.get_xmd()
    platform_xmd["mbs"]["use_default_modules"] = True
    platform_mmd.set_xmd(platform_xmd)
    platform.modulemd = mmd_to_str(platform_mmd)

    dependencies = [
        {"requires": {"platform": ["f28"]},
         "buildrequires": {"platform": ["f28"]}}]
    make_module_in_db("python:3:12345:1", base_module=platform, dependencies=dependencies)
    make_module_in_db("nodejs:11:2345:2", base_module=platform, dependencies=dependencies)
    db_session.commit()

    mock_get_dm.return_value = {
        "nodejs": "11",
        "python": "3",
        "ruby": "2.6",
    }
    defaults_added = default_modules.add_default_modules(mmd)
    # Make sure that the default modules were added. ruby:2.6 will be ignored since it's not in
    # the database
    assert set(mmd.get_xmd()["mbs"]["buildrequires"].keys()) == {"nodejs", "platform", "python"}
    mock_get_dm.assert_called_once_with(
        "f28",
        "https://pagure.io/releng/fedora-module-defaults.git",
    )
    assert "ursine_rpms" not in mmd.get_xmd()["mbs"]
    assert defaults_added is True


@patch("module_build_service.scheduler.default_modules._get_default_modules")
def test_add_default_modules_not_linked(mock_get_dm):
    """
    Test that no default modules are added when they aren't linked from the base module.
    """
    clean_database()
    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))
    assert set(mmd.get_xmd()["mbs"]["buildrequires"].keys()) == {"platform"}
    default_modules.add_default_modules(mmd)
    assert set(mmd.get_xmd()["mbs"]["buildrequires"].keys()) == {"platform"}
    mock_get_dm.assert_not_called()


def test_add_default_modules_platform_not_available():
    """
    Test that an exception is raised when the platform module that is buildrequired is missing.

    This error should never occur in practice.
    """
    clean_database(False, False)
    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))

    expected_error = "Failed to retrieve the module platform:f28:3:00000000 from the database"
    with pytest.raises(RuntimeError, match=expected_error):
        default_modules.add_default_modules(mmd)


@patch("module_build_service.scheduler.default_modules._get_default_modules")
def test_add_default_modules_compatible_platforms(mock_get_dm):
    """
    Test that default modules built against compatible base module streams are added.
    """
    clean_database(add_platform_module=False)

    # Create compatible base modules.
    mmd = load_mmd(read_staged_data("platform"))
    for stream in ["f27", "f28"]:
        mmd = mmd.copy("platform", stream)

        # Set the virtual stream to "fedora" to make these base modules compatible.
        xmd = mmd.get_xmd()
        xmd["mbs"]["virtual_streams"] = ["fedora"]
        xmd["mbs"]["use_default_modules"] = True
        mmd.set_xmd(xmd)
        import_mmd(db_session, mmd)

    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))
    xmd_brs = mmd.get_xmd()["mbs"]["buildrequires"]
    assert set(xmd_brs.keys()) == {"platform"}

    platform_f27 = ModuleBuild.get_build_from_nsvc(
        db_session, "platform", "f27", "3", "00000000")
    assert platform_f27

    # Create python default module which requires platform:f27 and therefore cannot be used
    # as default module for platform:f28.
    dependencies = [
        {"requires": {"platform": ["f27"]},
         "buildrequires": {"platform": ["f27"]}}]
    make_module_in_db("python:3:12345:1", base_module=platform_f27, dependencies=dependencies)

    # Create nodejs default module which requries any platform stream and therefore can be used
    # as default module for platform:f28.
    dependencies[0]["requires"]["platform"] = []
    make_module_in_db("nodejs:11:2345:2", base_module=platform_f27, dependencies=dependencies)
    db_session.commit()

    mock_get_dm.return_value = {
        "nodejs": "11",
        "python": "3",
        "ruby": "2.6",
    }
    defaults_added = default_modules.add_default_modules(mmd)
    # Make sure that the default modules were added. ruby:2.6 will be ignored since it's not in
    # the database
    assert set(mmd.get_xmd()["mbs"]["buildrequires"].keys()) == {"nodejs", "platform"}
    mock_get_dm.assert_called_once_with(
        "f28",
        "https://pagure.io/releng/fedora-module-defaults.git",
    )
    assert defaults_added is True


@patch("module_build_service.scheduler.default_modules._get_default_modules")
def test_add_default_modules_request_failed(mock_get_dm):
    """
    Test that an exception is raised when the call to _get_default_modules failed.
    """
    clean_database()
    make_module_in_db("python:3:12345:1")
    make_module_in_db("nodejs:11:2345:2")
    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))
    xmd_brs = mmd.get_xmd()["mbs"]["buildrequires"]
    assert set(xmd_brs.keys()) == {"platform"}

    platform = ModuleBuild.get_build_from_nsvc(
        db_session,
        "platform",
        xmd_brs["platform"]["stream"],
        xmd_brs["platform"]["version"],
        xmd_brs["platform"]["context"],
    )
    assert platform
    platform_mmd = platform.mmd()
    platform_xmd = mmd.get_xmd()
    platform_xmd["mbs"]["use_default_modules"] = True
    platform_mmd.set_xmd(platform_xmd)
    platform.modulemd = mmd_to_str(platform_mmd)
    db_session.commit()

    expected_error = "some error"
    mock_get_dm.side_effect = ValueError(expected_error)

    with pytest.raises(ValueError, match=expected_error):
        default_modules.add_default_modules(mmd)


@pytest.mark.parametrize("is_rawhide", (True, False))
@patch("shutil.rmtree")
@patch("tempfile.mkdtemp")
@patch("module_build_service.scheduler.default_modules.Modulemd.ModuleIndex.new")
@patch("module_build_service.scheduler.default_modules.scm.SCM")
@patch("module_build_service.scheduler.default_modules._get_rawhide_version")
def test_get_default_modules(
    mock_get_rawhide, mock_scm, mock_mmd_new, mock_mkdtemp, mock_rmtree, is_rawhide,
):
    """
    Test that _get_default_modules returns the default modules.
    """
    mock_scm.return_value.sourcedir = "/some/path"
    if is_rawhide:
        mock_scm.return_value.checkout_ref.side_effect = [
            UnprocessableEntity("invalid branch"),
            None,
        ]
        mock_get_rawhide.return_value = "f32"

    expected = {"nodejs": "11"}
    mock_mmd_new.return_value.get_default_streams.return_value = expected

    rv = default_modules._get_default_modules("f32", conf.default_modules_scm_url)

    assert rv == expected
    if is_rawhide:
        mock_scm.return_value.checkout_ref.assert_has_calls(
            [call("f32"), call(conf.rawhide_branch)]
        )
    else:
        mock_scm.return_value.checkout_ref.assert_called_once_with("f32")


@pytest.mark.parametrize("uses_rawhide", (True, False))
@patch("shutil.rmtree")
@patch("tempfile.mkdtemp")
@patch(
    "module_build_service.scheduler.default_modules.conf.uses_rawhide",
    new_callable=PropertyMock,
)
@patch("module_build_service.scheduler.default_modules.Modulemd.ModuleIndex.new")
@patch("module_build_service.scheduler.default_modules.scm.SCM")
@patch("module_build_service.scheduler.default_modules._get_rawhide_version")
def test_get_default_modules_invalid_branch(
    mock_get_rawhide, mock_scm, mock_mmd_new, mock_uses_rawhide, mock_mkdtemp, mock_rmtree,
    uses_rawhide,
):
    """
    Test that _get_default_modules raises an exception with an invalid branch.
    """
    mock_uses_rawhide.return_value = uses_rawhide
    mock_scm.return_value.sourcedir = "/some/path"
    mock_scm.return_value.checkout_ref.side_effect = [
        UnprocessableEntity("invalid branch"),
        UnprocessableEntity("invalid branch"),
    ]
    if uses_rawhide:
        mock_get_rawhide.return_value = "f32"
    else:
        mock_get_rawhide.return_value = "something_else"

    with pytest.raises(RuntimeError, match="Failed to retrieve the default modules"):
        default_modules._get_default_modules("f32", conf.default_modules_scm_url)

    mock_mmd_new.assert_not_called()
    if uses_rawhide:
        mock_scm.return_value.checkout_ref.assert_has_calls(
            [call("f32"), call(conf.rawhide_branch)],
        )
    else:
        mock_scm.return_value.checkout_ref.assert_called_once_with("f32")


@patch("module_build_service.scheduler.default_modules.get_session")
def test_get_rawhide_version(mock_get_session):
    """
    Test that _get_rawhide_version will return rawhide Fedora version.
    """
    mock_get_session.return_value.getBuildTarget.return_value = {
        "build_tag_name": "f32-build",
    }
    assert default_modules._get_rawhide_version() == "f32"


@patch("module_build_service.scheduler.default_modules.get_session")
@patch("module_build_service.scheduler.default_modules._get_rpms_from_tags")
def test_handle_collisions_with_base_module_rpms(mock_grft, mock_get_session):
    """
    Test that handle_collisions_with_base_module_rpms will add conflicts for NEVRAs in the
    modulemd.
    """
    mmd = load_mmd(read_staged_data("formatted_testmodule.yaml"))
    xmd = mmd.get_xmd()
    xmd["mbs"]["buildrequires"]["platform"]["koji_tag"] = "module-el-build"
    xmd["mbs"]["buildrequires"]["python"] = {"koji_tag": "module-python27"}
    xmd["mbs"]["buildrequires"]["bash"] = {"koji_tag": "module-bash"}
    mmd.set_xmd(xmd)

    bm_rpms = {
        "bash-completion-1:2.7-5.el8.noarch",
        "bash-0:4.4.19-7.el8.aarch64",
        "python2-tools-0:2.7.16-11.el8.aarch64",
        "python2-tools-0:2.7.16-11.el8.x86_64",
        "python3-ldap-0:3.1.0-4.el8.aarch64",
        "python3-ldap-0:3.1.0-4.el8.x86_64",
    }
    non_bm_rpms = {
        "bash-0:4.4.20-1.el8.aarch64",
        "python2-tools-0:2.7.18-1.module+el8.1.0+3568+bbd875cb.aarch64",
        "python2-tools-0:2.7.18-1.module+el8.1.0+3568+bbd875cb.x86_64",
    }
    mock_grft.side_effect = [bm_rpms, non_bm_rpms]

    default_modules.handle_collisions_with_base_module_rpms(mmd, ["aarch64", "x86_64"])

    mock_get_session.assert_called_once()
    xmd_mbs = mmd.get_xmd()["mbs"]
    assert set(xmd_mbs["ursine_rpms"]) == {
        "bash-0:4.4.19-7.el8.aarch64",
        "python2-tools-0:2.7.16-11.el8.aarch64",
        "python2-tools-0:2.7.16-11.el8.x86_64",
    }
    assert mock_grft.call_count == 2
    # We can't check the calls directly because the second argument is a set converted to a list,
    # so the order can't be determined ahead of time.
    first_call = mock_grft.mock_calls[0][1]
    assert first_call[0] == mock_get_session.return_value
    assert first_call[1] == ["module-el-build"]
    assert first_call[2] == ["aarch64", "x86_64"]

    second_call = mock_grft.mock_calls[1][1]
    assert second_call[0] == mock_get_session.return_value
    assert set(second_call[1]) == {"module-bash", "module-python27"}
    assert second_call[2] == ["aarch64", "x86_64"]


@patch("module_build_service.scheduler.default_modules.koji_retrying_multicall_map")
@patch("module_build_service.scheduler.default_modules._get_rpms_in_external_repo")
def test_get_rpms_from_tags(mock_grier, mock_multicall_map):
    """
    Test the function queries Koji for the tags' and the tags' external repos' for RPMs.
    """
    mock_session = Mock()
    bash_tagged = [
        [
            {
                "arch": "aarch64",
                "epoch": 0,
                "name": "bash",
                "version": "4.4.20",
                "release": "1.module+el8.1.0+123+bbd875cb",
            },
            {
                "arch": "x86_64",
                "epoch": 0,
                "name": "bash",
                "version": "4.4.20",
                "release": "1.module+el8.1.0+123+bbd875cb",
            }
        ],
        None,
    ]
    python_tagged = [
        [
            {
                "arch": "aarch64",
                "epoch": 0,
                "name": "python2-tools",
                "version": "2.7.18",
                "release": "1.module+el8.1.0+3568+bbd875cb",
            },
            {
                "arch": "x86_64",
                "epoch": 0,
                "name": "python2-tools",
                "version": "2.7.18",
                "release": "1.module+el8.1.0+3568+bbd875cb",
            }
        ],
        None,
    ]
    bash_repos = []
    external_repo_url = "http://domain.local/repo/latest/$arch/"
    python_repos = [{
        "external_repo_id": "12",
        "tag_name": "module-python27",
        "url": external_repo_url,
    }]
    mock_multicall_map.side_effect = [
        [bash_tagged, python_tagged],
        [bash_repos, python_repos],
    ]
    mock_grier.return_value = {
        "python2-test-0:2.7.16-11.module+el8.1.0+3568+bbd875cb.aarch64",
        "python2-test-0:2.7.16-11.module+el8.1.0+3568+bbd875cb.x86_64",
    }

    tags = ["module-bash", "module-python27"]
    arches = ["aarch64", "x86_64"]
    rv = default_modules._get_rpms_from_tags(mock_session, tags, arches)

    expected = {
        "bash-0:4.4.20-1.module+el8.1.0+123+bbd875cb.aarch64",
        "bash-0:4.4.20-1.module+el8.1.0+123+bbd875cb.x86_64",
        "python2-tools-0:2.7.18-1.module+el8.1.0+3568+bbd875cb.aarch64",
        "python2-tools-0:2.7.18-1.module+el8.1.0+3568+bbd875cb.x86_64",
        "python2-test-0:2.7.16-11.module+el8.1.0+3568+bbd875cb.aarch64",
        "python2-test-0:2.7.16-11.module+el8.1.0+3568+bbd875cb.x86_64",
    }
    assert rv == expected
    assert mock_multicall_map.call_count == 2
    mock_grier.assert_called_once_with(external_repo_url, arches, "module-python27-12")


@patch("module_build_service.scheduler.default_modules.koji_retrying_multicall_map")
def test_get_rpms_from_tags_error_listTaggedRPMS(mock_multicall_map):
    """
    Test that an exception is raised if the listTaggedRPMS Koji query fails.
    """
    mock_session = Mock()
    mock_multicall_map.return_value = None

    tags = ["module-bash", "module-python27"]
    arches = ["aarch64", "x86_64"]
    expected = (
        "Getting the tagged RPMs of the following Koji tags failed: module-bash, module-python27"
    )
    with pytest.raises(RuntimeError, match=expected):
        default_modules._get_rpms_from_tags(mock_session, tags, arches)


@patch("module_build_service.scheduler.default_modules.koji_retrying_multicall_map")
def test_get_rpms_from_tags_error_getExternalRepoList(mock_multicall_map):
    """
    Test that an exception is raised if the getExternalRepoList Koji query fails.
    """
    mock_session = Mock()
    mock_multicall_map.side_effect = [[[[], []]], None]

    tags = ["module-bash", "module-python27"]
    arches = ["aarch64", "x86_64"]
    expected = (
        "Getting the external repos of the following Koji tags failed: module-bash, module-python27"
    )
    with pytest.raises(RuntimeError, match=expected):
        default_modules._get_rpms_from_tags(mock_session, tags, arches)


@patch("dnf.Base")
@patch("os.makedirs")
def test_get_rpms_in_external_repo(mock_makedirs, mock_dnf_base):
    """
    Test that DNF can query the external repos for the available packages.
    """
    RPM = namedtuple("RPM", ["arch", "epoch", "name", "release", "version"])
    mock_dnf_base.return_value.sack.query.return_value.available.return_value = [
        RPM("aarch64", 0, "python", "1.el8", "2.7"),
        RPM("aarch64", 0, "python", "1.el8", "3.7"),
        RPM("x86_64", 0, "python", "1.el8", "2.7"),
        RPM("x86_64", 0, "python", "1.el8", "3.7"),
        RPM("i686", 0, "python", "1.el8", "2.7"),
        RPM("i686", 0, "python", "1.el8", "3.7"),
    ]

    external_repo_url = "http://domain.local/repo/latest/$arch/"
    arches = ["aarch64", "x86_64", "i686"]
    cache_dir_name = "module-el-build-12"
    rv = default_modules._get_rpms_in_external_repo(external_repo_url, arches, cache_dir_name)

    expected = {
        "python-0:2.7-1.el8.aarch64",
        "python-0:3.7-1.el8.aarch64",
        "python-0:2.7-1.el8.x86_64",
        "python-0:3.7-1.el8.x86_64",
        "python-0:2.7-1.el8.i686",
        "python-0:3.7-1.el8.i686",
    }
    assert rv == expected

    # Test that i686 is mapped to i386 using the koji.canonArch().
    mock_dnf_base.return_value.repos.add_new_repo.assert_called_with(
        "repo_i386",
        mock_dnf_base.return_value.conf,
        baseurl=["http://domain.local/repo/latest/i386/"],
        minrate=conf.dnf_minrate,
    )


def test_get_rpms_in_external_repo_invalid_repo_url():
    """
    Test that an exception is raised when an invalid repo URL is passed in.
    """
    external_repo_url = "http://domain.local/repo/latest/"
    arches = ["aarch64", "x86_64"]
    cache_dir_name = "module-el-build-12"
    expected = (
        r"The external repo http://domain.local/repo/latest/ does not contain the \$arch variable"
    )
    with pytest.raises(ValueError, match=expected):
        default_modules._get_rpms_in_external_repo(external_repo_url, arches, cache_dir_name)


@patch("dnf.Base")
@patch("os.makedirs")
def test_get_rpms_in_external_repo_failed_to_load(mock_makedirs, mock_dnf_base):
    """
    Test that an exception is raised when an external repo can't be loaded.
    """
    class FakeRepo(dict):
        @staticmethod
        def add_new_repo(*args, **kwargs):
            pass

    mock_dnf_base.return_value.update_cache.side_effect = dnf.exceptions.RepoError("Failed")
    external_repo_url = "http://domain.local/repo/latest/$arch/"
    arches = ["aarch64", "x86_64"]
    cache_dir_name = "module-el-build-12"
    expected = "Failed to load the external repos"
    with pytest.raises(RuntimeError, match=expected):
        default_modules._get_rpms_in_external_repo(external_repo_url, arches, cache_dir_name)


@patch("os.makedirs")
def test_get_rpms_in_external_repo_failed_to_create_cache(mock_makedirs):
    """
    Test that an exception is raised when the cache can't be created.
    """
    exc = OSError()
    exc.errno = errno.EACCES
    mock_makedirs.side_effect = exc

    external_repo_url = "http://domain.local/repo/latest/$arch/"
    arches = ["aarch64", "x86_64"]
    cache_dir_name = "module-el-build-12"
    expected = "The MBS cache is not writeable."
    with pytest.raises(RuntimeError, match=expected):
        default_modules._get_rpms_in_external_repo(external_repo_url, arches, cache_dir_name)
