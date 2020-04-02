# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import pytest

from module_build_service.common import models
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.utils import import_mmd, load_mmd
from module_build_service.scheduler.db_session import db_session
from tests import clean_database, read_staged_data


@pytest.mark.parametrize("context", ["c1", None])
def test_import_mmd_contexts(context):
    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    mmd.set_context(context)

    xmd = mmd.get_xmd()
    xmd["mbs"]["koji_tag"] = "foo"
    mmd.set_xmd(xmd)

    build, msgs = import_mmd(db_session, mmd)

    mmd_context = build.mmd().get_context()
    if context:
        assert mmd_context == context
        assert build.context == context
    else:
        assert mmd_context == models.DEFAULT_MODULE_CONTEXT
        assert build.context == models.DEFAULT_MODULE_CONTEXT


def test_import_mmd_multiple_dependencies():
    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    mmd.add_dependencies(mmd.get_dependencies()[0].copy())

    expected_error = "The imported module's dependencies list should contain just one element"
    with pytest.raises(UnprocessableEntity) as e:
        import_mmd(db_session, mmd)
        assert str(e.value) == expected_error


def test_import_mmd_no_xmd_buildrequires():
    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    xmd = mmd.get_xmd()
    del xmd["mbs"]["buildrequires"]
    mmd.set_xmd(xmd)

    expected_error = (
        "The imported module buildrequires other modules, but the metadata in the "
        'xmd["mbs"]["buildrequires"] dictionary is missing entries'
    )
    with pytest.raises(UnprocessableEntity) as e:
        import_mmd(db_session, mmd)
        assert str(e.value) == expected_error


def test_import_mmd_minimal_xmd_from_local_repository():
    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    xmd = mmd.get_xmd()
    xmd["mbs"] = {}
    xmd["mbs"]["koji_tag"] = "repofile:///etc/yum.repos.d/fedora-modular.repo"
    xmd["mbs"]["mse"] = True
    xmd["mbs"]["commit"] = "unknown"
    mmd.set_xmd(xmd)

    build, msgs = import_mmd(db_session, mmd, False)
    assert build.name == mmd.get_module_name()


@pytest.mark.parametrize(
    "stream, disttag_marking, error_msg",
    (
        ("f28", None, None),
        ("f28", "fedora28", None),
        ("f-28", "f28", None),
        ("f-28", None, "The stream cannot contain a dash unless disttag_marking is set"),
        ("f28", "f-28", "The disttag_marking cannot contain a dash"),
        ("f-28", "fedora-28", "The disttag_marking cannot contain a dash"),
    ),
)
def test_import_mmd_base_module(stream, disttag_marking, error_msg):
    clean_database(add_platform_module=False)
    mmd = load_mmd(read_staged_data("platform"))
    mmd = mmd.copy(mmd.get_module_name(), stream)

    if disttag_marking:
        xmd = mmd.get_xmd()
        xmd["mbs"]["disttag_marking"] = disttag_marking
        mmd.set_xmd(xmd)

    if error_msg:
        with pytest.raises(UnprocessableEntity, match=error_msg):
            import_mmd(db_session, mmd)
    else:
        import_mmd(db_session, mmd)


def test_import_mmd_remove_dropped_virtual_streams():
    mmd = load_mmd(read_staged_data("formatted_testmodule"))

    # Add some virtual streams
    xmd = mmd.get_xmd()
    xmd["mbs"]["virtual_streams"] = ["f28", "f29", "f30"]
    mmd.set_xmd(xmd)

    # Import mmd into database to simulate the next step to reimport a module
    import_mmd(db_session, mmd)

    # Now, remove some virtual streams from module metadata
    xmd = mmd.get_xmd()
    xmd["mbs"]["virtual_streams"] = ["f28", "f29"]  # Note that, f30 is removed
    mmd.set_xmd(xmd)

    # Test import modulemd again and the f30 should be removed from database.
    module_build, _ = import_mmd(db_session, mmd)

    db_session.refresh(module_build)
    assert ["f28", "f29"] == sorted(item.name for item in module_build.virtual_streams)
    assert 0 == db_session.query(models.VirtualStream).filter_by(name="f30").count()


def test_import_mmd_dont_remove_dropped_virtual_streams_associated_with_other_modules():
    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    # Add some virtual streams to this module metadata
    xmd = mmd.get_xmd()
    xmd["mbs"]["virtual_streams"] = ["f28", "f29", "f30"]
    mmd.set_xmd(xmd)
    import_mmd(db_session, mmd)

    # Import another module which has overlapping virtual streams
    another_mmd = load_mmd(read_staged_data("formatted_testmodule-more-components"))
    # Add some virtual streams to this module metadata
    xmd = another_mmd.get_xmd()
    xmd["mbs"]["virtual_streams"] = ["f29", "f30"]
    another_mmd.set_xmd(xmd)
    another_module_build, _ = import_mmd(
        db_session, another_mmd)

    # Now, remove f30 from mmd
    xmd = mmd.get_xmd()
    xmd["mbs"]["virtual_streams"] = ["f28", "f29"]
    mmd.set_xmd(xmd)

    # Reimport formatted_testmodule again
    module_build, _ = import_mmd(db_session, mmd)

    db_session.refresh(module_build)
    assert ["f28", "f29"] == sorted(item.name for item in module_build.virtual_streams)

    # The overlapped f30 should be still there.
    db_session.refresh(another_module_build)
    assert ["f29", "f30"] == sorted(item.name for item in another_module_build.virtual_streams)
