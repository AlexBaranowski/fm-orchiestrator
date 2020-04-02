# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import shutil
import tempfile

from mock import call, MagicMock, Mock, patch, PropertyMock
import pytest

from module_build_service.builder import utils
from module_build_service.common import models
from module_build_service.common.config import conf
from module_build_service.common.errors import ProgrammingError, ValidationError
from module_build_service.common.utils import load_mmd, import_mmd, mmd_to_str
from module_build_service.scheduler.db_session import db_session
from tests import init_data, read_staged_data, scheduler_init_data


@patch("requests.get")
@patch("koji.ClientSession")
@patch("module_build_service.builder.utils.execute_cmd")
def test_create_local_repo_from_koji_tag(mock_exec_cmd, mock_koji_session, mock_get):
    session = Mock()
    rpms = [
        {
            "arch": "src",
            "build_id": 875991,
            "name": "module-build-macros",
            "release": "1.module_92011fe6",
            "size": 6890,
            "version": "0.1",
        },
        {
            "arch": "noarch",
            "build_id": 875991,
            "name": "module-build-macros",
            "release": "1.module_92011fe6",
            "size": 6890,
            "version": "0.1",
        },
        {
            "arch": "x86_64",
            "build_id": 875636,
            "name": "ed-debuginfo",
            "release": "2.module_bd6e0eb1",
            "size": 81438,
            "version": "1.14.1",
        },
        {
            "arch": "x86_64",
            "build_id": 875636,
            "name": "ed",
            "release": "2.module_bd6e0eb1",
            "size": 80438,
            "version": "1.14.1",
        },
        {
            "arch": "x86_64",
            "build_id": 875640,
            "name": "mksh-debuginfo",
            "release": "2.module_bd6e0eb1",
            "size": 578774,
            "version": "54",
        },
        {
            "arch": "x86_64",
            "build_id": 875640,
            "name": "mksh",
            "release": "2.module_bd6e0eb1",
            "size": 267042,
            "version": "54",
        },
    ]

    builds = [
        {
            "build_id": 875640,
            "name": "mksh",
            "release": "2.module_bd6e0eb1",
            "version": "54",
            "volume_name": "prod",
        },
        {
            "build_id": 875636,
            "name": "ed",
            "release": "2.module_bd6e0eb1",
            "version": "1.14.1",
            "volume_name": "prod",
        },
        {
            "build_id": 875991,
            "name": "module-build-macros",
            "release": "1.module_92011fe6",
            "version": "0.1",
            "volume_name": "prod",
        },
    ]

    session.listTaggedRPMS.return_value = (rpms, builds)
    session.opts = {"topurl": "https://kojipkgs.stg.fedoraproject.org/"}
    mock_koji_session.return_value = session

    tag = "module-testmodule-master-20170405123740-build"
    temp_dir = tempfile.mkdtemp()
    try:
        utils.create_local_repo_from_koji_tag(conf, tag, temp_dir)
    finally:
        shutil.rmtree(temp_dir)

    url_one = (
        "https://kojipkgs.stg.fedoraproject.org//vol/prod/packages/module-build-macros/"
        "0.1/1.module_92011fe6/noarch/module-build-macros-0.1-1.module_92011fe6.noarch.rpm"
    )
    url_two = (
        "https://kojipkgs.stg.fedoraproject.org//vol/prod/packages/ed/1.14.1/"
        "2.module_bd6e0eb1/x86_64/ed-1.14.1-2.module_bd6e0eb1.x86_64.rpm"
    )
    url_three = (
        "https://kojipkgs.stg.fedoraproject.org//vol/prod/packages/mksh/54/"
        "2.module_bd6e0eb1/x86_64/mksh-54-2.module_bd6e0eb1.x86_64.rpm"
    )

    expected_calls = [
        call(url_one, stream=True, timeout=60),
        call(url_two, stream=True, timeout=60),
        call(url_three, stream=True, timeout=60),
    ]
    for expected_call in expected_calls:
        assert expected_call in mock_get.call_args_list
    assert len(mock_get.call_args_list) == len(expected_calls)


def test_validate_koji_tag_wrong_tag_arg_during_programming():
    """ Test that we fail on a wrong param name (non-existing one) due to
    programming error. """

    @utils.validate_koji_tag("wrong_tag_arg")
    def validate_koji_tag_programming_error(good_tag_arg, other_arg):
        pass

    with pytest.raises(ProgrammingError):
        validate_koji_tag_programming_error("dummy", "other_val")


def test_validate_koji_tag_bad_tag_value():
    """ Test that we fail on a bad tag value. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_bad_tag_value(tag_arg):
        pass

    with pytest.raises(ValidationError):
        validate_koji_tag_bad_tag_value("forbiddentagprefix-foo")


def test_validate_koji_tag_bad_tag_value_in_list():
    """ Test that we fail on a list containing bad tag value. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_bad_tag_value_in_list(tag_arg):
        pass

    with pytest.raises(ValidationError):
        validate_koji_tag_bad_tag_value_in_list(["module-foo", "forbiddentagprefix-bar"])


def test_validate_koji_tag_good_tag_value():
    """ Test that we pass on a good tag value. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_good_tag_value(tag_arg):
        return True

    assert validate_koji_tag_good_tag_value("module-foo") is True


def test_validate_koji_tag_good_tag_values_in_list():
    """ Test that we pass on a list of good tag values. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_good_tag_values_in_list(tag_arg):
        return True

    assert validate_koji_tag_good_tag_values_in_list(["module-foo", "module-bar"]) is True


def test_validate_koji_tag_good_tag_value_in_dict():
    """ Test that we pass on a dict arg with default key
    and a good value. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_good_tag_value_in_dict(tag_arg):
        return True

    assert validate_koji_tag_good_tag_value_in_dict({"name": "module-foo"}) is True


def test_validate_koji_tag_good_tag_value_in_dict_nondefault_key():
    """ Test that we pass on a dict arg with non-default key
    and a good value. """

    @utils.validate_koji_tag("tag_arg", dict_key="nondefault")
    def validate_koji_tag_good_tag_value_in_dict_nondefault_key(tag_arg):
        return True

    assert (
        validate_koji_tag_good_tag_value_in_dict_nondefault_key({"nondefault": "module-foo"})
        is True
    )


def test_validate_koji_tag_double_trouble_good():
    """ Test that we pass on a list of tags that are good. """

    expected = "foo"

    @utils.validate_koji_tag(["tag_arg1", "tag_arg2"])
    def validate_koji_tag_double_trouble(tag_arg1, tag_arg2):
        return expected

    actual = validate_koji_tag_double_trouble("module-1", "module-2")
    assert actual == expected


def test_validate_koji_tag_double_trouble_bad():
    """ Test that we fail on a list of tags that are bad. """

    @utils.validate_koji_tag(["tag_arg1", "tag_arg2"])
    def validate_koji_tag_double_trouble(tag_arg1, tag_arg2):
        pass

    with pytest.raises(ValidationError):
        validate_koji_tag_double_trouble("module-1", "BADNEWS-2")


def test_validate_koji_tag_is_None():
    """ Test that we fail on a tag which is None. """

    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_is_None(tag_arg):
        pass

    with pytest.raises(ValidationError) as cm:
        validate_koji_tag_is_None(None)
        assert str(cm.value).endswith(" No value provided.") is True


@patch(
    "module_build_service.common.config.Config.allowed_privileged_module_names",
    new_callable=PropertyMock,
    return_value=["testmodule"],
)
def test_validate_koji_tag_previleged_module_name(conf_apmn):
    @utils.validate_koji_tag("tag_arg")
    def validate_koji_tag_priv_mod_name(self, tag_arg):
        pass

    builder = MagicMock()
    builder.module_str = 'testmodule'
    validate_koji_tag_priv_mod_name(builder, "abc")


def test_get_rpm_release_mse():
    init_data(contexts=True)

    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    release_one = utils.get_rpm_release(db_session, build_one)
    assert release_one == "module+2+b8645bbb"

    build_two = models.ModuleBuild.get_by_id(db_session, 3)
    release_two = utils.get_rpm_release(db_session, build_two)
    assert release_two == "module+2+17e35784"


def test_get_rpm_release_platform_stream():
    scheduler_init_data(1)
    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    release = utils.get_rpm_release(db_session, build_one)
    assert release == "module+f28+2+814cfa39"


def test_get_rpm_release_platform_stream_override():
    scheduler_init_data(1)

    # Set the disttag_marking override on the platform
    platform = (
        db_session.query(models.ModuleBuild)
        .filter_by(name="platform", stream="f28")
        .first()
    )
    platform_mmd = platform.mmd()
    platform_xmd = platform_mmd.get_xmd()
    platform_xmd["mbs"]["disttag_marking"] = "fedora28"
    platform_mmd.set_xmd(platform_xmd)
    platform.modulemd = mmd_to_str(platform_mmd)
    db_session.add(platform)
    db_session.commit()

    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    release = utils.get_rpm_release(db_session, build_one)
    assert release == "module+fedora28+2+814cfa39"


@patch(
    "module_build_service.common.config.Config.allowed_privileged_module_names",
    new_callable=PropertyMock,
    return_value=["build"],
)
def test_get_rpm_release_metadata_br_stream_override(mock_admmn):
    """
    Test that when a module buildrequires a module in conf.allowed_privileged_module_names,
    and that module has the xmd.mbs.disttag_marking field set, it should influence the disttag.
    """
    scheduler_init_data(1)
    metadata_mmd = load_mmd(read_staged_data("build_metadata_module"))
    import_mmd(db_session, metadata_mmd)

    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    mmd = build_one.mmd()
    deps = mmd.get_dependencies()[0]
    deps.add_buildtime_stream("build", "product1.2")
    xmd = mmd.get_xmd()
    xmd["mbs"]["buildrequires"]["build"] = {
        "filtered_rpms": [],
        "ref": "virtual",
        "stream": "product1.2",
        "version": "1",
        "context": "00000000",
    }
    mmd.set_xmd(xmd)
    build_one.modulemd = mmd_to_str(mmd)
    db_session.add(build_one)
    db_session.commit()

    release = utils.get_rpm_release(db_session, build_one)
    assert release == "module+product12+2+814cfa39"


def test_get_rpm_release_mse_scratch():
    init_data(contexts=True, scratch=True)

    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    release_one = utils.get_rpm_release(db_session, build_one)
    assert release_one == "scrmod+2+b8645bbb"

    build_two = models.ModuleBuild.get_by_id(db_session, 3)
    release_two = utils.get_rpm_release(db_session, build_two)
    assert release_two == "scrmod+2+17e35784"


def test_get_rpm_release_platform_stream_scratch():
    scheduler_init_data(1, scratch=True)
    build_one = models.ModuleBuild.get_by_id(db_session, 2)
    release = utils.get_rpm_release(db_session, build_one)
    assert release == "scrmod+f28+2+814cfa39"
