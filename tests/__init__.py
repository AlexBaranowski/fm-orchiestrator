# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
from datetime import datetime, timedelta
import functools
import hashlib
import itertools
import os
import re
import six
import time
from traceback import extract_stack

import koji
from mock import patch
from six import string_types

import module_build_service
from module_build_service import db
from module_build_service.builder.utils import get_rpm_release
from module_build_service.common.models import (
    BUILD_STATES,
    ComponentBuild,
    ModuleArch,
    ModuleBuild,
    VirtualStream,
)
from module_build_service.common.modulemd import Modulemd
from module_build_service.common.utils import load_mmd, import_mmd, mmd_to_str, to_text_type
from module_build_service.scheduler.db_session import db_session


base_dir = os.path.dirname(__file__)


def staged_data_filename(filename):
    return os.path.join(base_dir, "staged_data", filename)


def read_staged_data(yaml_name):
    """Read module YAML content from staged_data directory

    :param str yaml_name: name of YAML file which could be with or without
        extension ``.yaml``. ``.yaml`` will be added if extension is omitted.
    :return: module YAML file's content.
    :rtype: str
    :raises ValueError: if specified module YAML file does not exist in
        staged_data directory.
    """
    filename = staged_data_filename(
        yaml_name if '.' in yaml_name else "{}.yaml".format(yaml_name))
    if not os.path.exists(filename):
        raise ValueError("Staged data {}.yaml does not exist.".format(yaml_name))
    with open(filename, "r") as mmd:
        return to_text_type(mmd.read())


def patch_config():
    # add test builders for all resolvers
    with_test_builders = dict()
    for k, v in module_build_service.common.config.SUPPORTED_RESOLVERS.items():
        v["builders"].extend(["test", "testlocal"])
        with_test_builders[k] = v
    patch("module_build_service.common.config.SUPPORTED_RESOLVERS", with_test_builders)


patch_config()


def patch_zeromq_time_sleep():
    """
    We use moksha.hub in some tests. We used dummy zerombq backend which
    connects to /dev/null, but zeromq.py contains time.sleep(1) to ensure
    that sockets are listening properly. This is not needed for our dummy
    use-case and it slows down tests.

    This method patches time.sleep called from "zeromq.py" file to be noop,
    but calls the real time.sleep otherwise.
    """
    global _orig_time_sleep
    _orig_time_sleep = time.sleep

    def mocked_time_sleep(n):
        global _orig_time_sleep
        if n == 1:
            tb = extract_stack()
            try:
                if tb[-4][0].endswith("zeromq.py"):
                    return
            except IndexError:
                pass
        _orig_time_sleep(n)

    ts = patch("time.sleep").start()
    ts.side_effect = mocked_time_sleep


patch_zeromq_time_sleep()


def clean_database(add_platform_module=True, add_default_arches=True):
    """Initialize the test database

    This function is responsible for dropping all the data in the database and
    recreating all the tables from scratch.

    Please note that, this function relies on database objects managed by
    Flask-SQLAlchemy.
    """

    # Helpful for writing tests if any changes were made using the database
    # session but the test didn't commit or rollback.
    #
    # clean_database is usually called before a test run. So, it makes no sense
    # to keep any changes in the transaction made by previous test.
    db_session.remove()
    db_session.configure(bind=db.session.get_bind())

    db.drop_all()
    db.create_all()

    if add_default_arches:
        arch_obj = module_build_service.common.models.ModuleArch(name="x86_64")
        db.session.add(arch_obj)
        db.session.commit()

    if add_platform_module:
        mmd = load_mmd(read_staged_data("platform"))
        import_mmd(db.session, mmd)


def init_data(data_size=10, contexts=False, multiple_stream_versions=None, scratch=False):
    """
    Creates data_size * 3 modules in database in different states and
    with different component builds. See _populate_data for more info.

    :param bool contexts: If True, multiple streams and contexts in each stream
        are generated for 'nginx' module.
    :param list/bool multiple_stream_versions: If true, multiple base modules with
        difference stream versions are generated. If set to list, the list defines
        the generated base module streams.
    """
    clean_database()

    if multiple_stream_versions:
        if multiple_stream_versions is True:
            multiple_stream_versions = ["f28.0.0", "f29.0.0", "f29.1.0", "f29.2.0"]
        mmd = load_mmd(read_staged_data("platform"))
        for stream in multiple_stream_versions:
            mmd = mmd.copy("platform", stream)

            # Set the virtual_streams based on "fXY" to mark the platform streams
            # with the same major stream_version compatible.
            xmd = mmd.get_xmd()
            xmd["mbs"]["virtual_streams"] = [stream[:3]]
            mmd.set_xmd(xmd)
            import_mmd(db.session, mmd)

            # Just to possibly confuse tests by adding another base module.
            mmd = mmd.copy("bootstrap", stream)
            import_mmd(db.session, mmd)

    _populate_data(data_size, contexts=contexts, scratch=scratch)


def _populate_data(data_size=10, contexts=False, scratch=False):
    # Query arch from passed database session, otherwise there will be an error
    # like "Object '<ModuleBuild at 0x7f4ccc805c50>' is already attached to
    # session '275' (this is '276')" when add new module build object to passed
    # session.
    task_id_counter = itertools.count(1)
    arch = db_session.query(module_build_service.common.models.ModuleArch).get(1)
    num_contexts = 2 if contexts else 1
    for index in range(data_size):
        for context in range(num_contexts):
            build_one = ModuleBuild(
                name="nginx",
                stream="1",
                version=2 + index,
                state=BUILD_STATES["ready"],
                scratch=scratch,
                modulemd=read_staged_data("nginx_mmd"),
                koji_tag="scrmod-nginx-1.2" if scratch else "module-nginx-1.2",
                scmurl="git://pkgs.domain.local/modules/nginx"
                       "?#ba95886c7a443b36a9ce31abda1f9bef22f2f8c9",
                batch=2,
                # https://www.youtube.com/watch?v=iQGwrK_yDEg,
                owner="Moe Szyslak",
                time_submitted=datetime(2016, 9, 3, 11, 23, 20) + timedelta(minutes=(index * 10)),
                time_modified=datetime(2016, 9, 3, 11, 25, 32) + timedelta(minutes=(index * 10)),
                time_completed=datetime(2016, 9, 3, 11, 25, 32) + timedelta(minutes=(index * 10)),
                rebuild_strategy="changed-and-after",
            )
            build_one.arches.append(arch)

            if contexts:
                build_one.stream = str(index)
                nsvc = "{}:{}:{}:{}".format(
                    build_one.name,
                    build_one.stream,
                    build_one.version,
                    context
                )
                unique_hash = hashlib.sha1(nsvc.encode('utf-8')).hexdigest()
                build_one.build_context = unique_hash
                build_one.runtime_context = unique_hash
                combined_hashes = "{0}:{1}".format(unique_hash, unique_hash)
                build_one.context = hashlib.sha1(combined_hashes.encode("utf-8")).hexdigest()[:8]

            db_session.add(build_one)
            db_session.commit()

            build_one_component_release = get_rpm_release(db_session, build_one)

            db_session.add_all([
                ComponentBuild(
                    package="nginx",
                    scmurl="git://pkgs.domain.local/rpms/nginx?"
                           "#ga95886c8a443b36a9ce31abda1f9bed22f2f8c3",
                    format="rpms",
                    task_id=six.next(task_id_counter),
                    state=koji.BUILD_STATES["COMPLETE"],
                    nvr="nginx-1.10.1-2.{0}".format(build_one_component_release),
                    batch=1,
                    module_id=2 + index * 3,
                    tagged=True,
                    tagged_in_final=True),
                ComponentBuild(
                    package="module-build-macros",
                    scmurl="/tmp/module_build_service-build-macrosWZUPeK/SRPMS/"
                           "module-build-macros-0.1-1.module_nginx_1_2.src.rpm",
                    format="rpms",
                    task_id=six.next(task_id_counter),
                    state=koji.BUILD_STATES["COMPLETE"],
                    nvr="module-build-macros-01-1.{0}".format(build_one_component_release),
                    batch=2,
                    module_id=2 + index * 3,
                    tagged=True,
                    tagged_in_final=True)
            ])
            db_session.commit()

        build_two = ModuleBuild(
            name="postgressql",
            stream="1",
            version=2 + index,
            state=BUILD_STATES["done"],
            scratch=scratch,
            modulemd=read_staged_data("testmodule"),
            koji_tag="scrmod-postgressql-1.2" if scratch else "module-postgressql-1.2",
            scmurl="git://pkgs.domain.local/modules/postgressql"
                   "?#aa95886c7a443b36a9ce31abda1f9bef22f2f8c9",
            batch=2,
            owner="some_user",
            time_submitted=datetime(2016, 9, 3, 12, 25, 33) + timedelta(minutes=(index * 10)),
            time_modified=datetime(2016, 9, 3, 12, 27, 19) + timedelta(minutes=(index * 10)),
            time_completed=datetime(2016, 9, 3, 11, 27, 19) + timedelta(minutes=(index * 10)),
            rebuild_strategy="changed-and-after",
        )
        build_two.arches.append(arch)

        db_session.add(build_two)
        db_session.commit()

        build_two_component_release = get_rpm_release(db_session, build_two)

        db_session.add_all([
            ComponentBuild(
                package="postgresql",
                scmurl="git://pkgs.domain.local/rpms/postgresql"
                       "?#dc95586c4a443b26a9ce38abda1f9bed22f2f8c3",
                format="rpms",
                task_id=2433433 + index,
                state=koji.BUILD_STATES["COMPLETE"],
                nvr="postgresql-9.5.3-4.{0}".format(build_two_component_release),
                batch=2,
                module_id=3 + index * 3,
                tagged=True,
                tagged_in_final=True),
            ComponentBuild(
                package="module-build-macros",
                scmurl="/tmp/module_build_service-build-macrosWZUPeK/SRPMS/"
                       "module-build-macros-0.1-1.module_postgresql_1_2.src.rpm",
                format="rpms",
                task_id=47383993 + index,
                state=koji.BUILD_STATES["COMPLETE"],
                nvr="module-build-macros-01-1.{0}".format(build_two_component_release),
                batch=1,
                module_id=3 + index * 3)
        ])
        db_session.commit()

        build_three = ModuleBuild(
            name="testmodule",
            stream="4.3.43",
            version=6 + index,
            state=BUILD_STATES["wait"],
            scratch=scratch,
            modulemd=read_staged_data("testmodule"),
            koji_tag=None,
            scmurl="git://pkgs.domain.local/modules/testmodule"
                   "?#ca95886c7a443b36a9ce31abda1f9bef22f2f8c9",
            batch=0,
            owner="some_other_user",
            time_submitted=datetime(2016, 9, 3, 12, 28, 33) + timedelta(minutes=(index * 10)),
            time_modified=datetime(2016, 9, 3, 12, 28, 40) + timedelta(minutes=(index * 10)),
            time_completed=None,
            rebuild_strategy="changed-and-after",
        )
        db_session.add(build_three)
        db_session.commit()

        build_three_component_release = get_rpm_release(db_session, build_three)

        db_session.add_all([
            ComponentBuild(
                package="rubygem-rails",
                scmurl="git://pkgs.domain.local/rpms/rubygem-rails"
                       "?#dd55886c4a443b26a9ce38abda1f9bed22f2f8c3",
                format="rpms",
                task_id=2433433 + index,
                state=koji.BUILD_STATES["FAILED"],
                nvr="postgresql-9.5.3-4.{0}".format(build_three_component_release),
                batch=2,
                module_id=4 + index * 3),
            ComponentBuild(
                package="module-build-macros",
                scmurl="/tmp/module_build_service-build-macrosWZUPeK/SRPMS/"
                       "module-build-macros-0.1-1.module_testmodule_1_2.src.rpm",
                format="rpms",
                task_id=47383993 + index,
                state=koji.BUILD_STATES["COMPLETE"],
                nvr="module-build-macros-01-1.{0}".format(build_three_component_release),
                batch=1,
                module_id=4 + index * 3,
                tagged=True,
                build_time_only=True)
        ])
        db_session.commit()


def scheduler_init_data(tangerine_state=None, scratch=False):
    """ Creates a testmodule in the building state with all the components in the same batch
    """
    clean_database()

    mmd = load_mmd(read_staged_data("formatted_testmodule"))
    mmd.get_rpm_component("tangerine").set_buildorder(0)

    module_build = module_build_service.common.models.ModuleBuild(
        name="testmodule",
        stream="master",
        version='20170109091357',
        state=BUILD_STATES["build"],
        scratch=scratch,
        build_context="ac4de1c346dcf09ce77d38cd4e75094ec1c08eb0",
        runtime_context="ac4de1c346dcf09ce77d38cd4e75094ec1c08eb0",
        context="7c29193d",
        koji_tag="scrmod-testmodule-master-20170109091357-7c29193d"
                 if scratch
                 else "module-testmodule-master-20170109091357-7c29193d",
        scmurl="https://src.stg.fedoraproject.org/modules/testmodule.git?#ff1ea79",
        batch=3 if tangerine_state else 2,
        # https://www.youtube.com/watch?v=iOKymYVSaJE
        owner="Buzz Lightyear",
        time_submitted=datetime(2017, 2, 15, 16, 8, 18),
        time_modified=datetime(2017, 2, 15, 16, 19, 35),
        rebuild_strategy="changed-and-after",
        modulemd=mmd_to_str(mmd),
    )

    db_session.add(module_build)
    db_session.commit()

    platform_br = module_build_service.common.models.ModuleBuild.get_by_id(db_session, 1)
    module_build.buildrequires.append(platform_br)

    arch = db_session.query(module_build_service.common.models.ModuleArch).get(1)
    module_build.arches.append(arch)

    build_one_component_release = get_rpm_release(db_session, module_build)

    db_session.add_all([
        module_build_service.common.models.ComponentBuild(
            module_id=module_build.id,
            package="perl-Tangerine",
            scmurl="https://src.fedoraproject.org/rpms/perl-Tangerine"
                   "?#4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            format="rpms",
            task_id=90276227,
            state=koji.BUILD_STATES["COMPLETE"],
            nvr="perl-Tangerine-0.23-1.{0}".format(build_one_component_release),
            batch=2,
            ref="4ceea43add2366d8b8c5a622a2fb563b625b9abf",
            tagged=True,
            tagged_in_final=True,
        ),
        module_build_service.common.models.ComponentBuild(
            module_id=module_build.id,
            package="perl-List-Compare",
            scmurl="https://src.fedoraproject.org/rpms/perl-List-Compare"
                   "?#76f9d8c8e87eed0aab91034b01d3d5ff6bd5b4cb",
            format="rpms",
            task_id=90276228,
            state=koji.BUILD_STATES["COMPLETE"],
            nvr="perl-List-Compare-0.53-5.{0}".format(build_one_component_release),
            batch=2,
            ref="76f9d8c8e87eed0aab91034b01d3d5ff6bd5b4cb",
            tagged=True,
            tagged_in_final=True,
        ),
        module_build_service.common.models.ComponentBuild(
            module_id=module_build.id,
            package="tangerine",
            scmurl="https://src.fedoraproject.org/rpms/tangerine"
                   "?#fbed359411a1baa08d4a88e0d12d426fbf8f602c",
            format="rpms",
            batch=3,
            ref="fbed359411a1baa08d4a88e0d12d426fbf8f602c",
            state=tangerine_state,
            task_id=90276315 if tangerine_state else None,
            nvr="tangerine-0.22-3.{}".format(build_one_component_release)
            if tangerine_state
            else None,
            tagged=tangerine_state == koji.BUILD_STATES["COMPLETE"],
            tagged_in_final=tangerine_state == koji.BUILD_STATES["COMPLETE"],
        ),
        module_build_service.common.models.ComponentBuild(
            module_id=module_build.id,
            package="module-build-macros",
            scmurl="/tmp/module_build_service-build-macrosqr4AWH/SRPMS/module-build-"
                   "macros-0.1-1.module_testmodule_master_20170109091357.src.rpm",
            format="rpms",
            task_id=90276181,
            state=koji.BUILD_STATES["COMPLETE"],
            nvr="module-build-macros-0.1-1.{}".format(build_one_component_release),
            batch=1,
            tagged=True,
            build_time_only=True,
        ),
    ])
    db_session.commit()


def make_module(
    nsvc,
    dependencies=None,
    base_module=None,
    filtered_rpms=None,
    xmd=None,
    store_to_db=False,
    virtual_streams=None,
    arches=None,
):
    """
    Creates new models.ModuleBuild defined by `nsvc` string with requires
    and buildrequires set according to ``requires_list`` and ``build_requires_list``.

    :param str nsvc: name:stream:version:context of a module.
    :param dependencies: list of groups of dependencies (requires and buildrequires).
        For example, [
        {"requires": {"platform": ["f30"]}, "buildrequires": {"platform": ["f30"]}},
        ...
        ]
    :type dependencies: list[dict]
    :param base_module: a base module build required by the new module created.
    :type base_module: :class:`ModuleBuild`
    :param filtered_rpms: list of filtered RPMs which are added to filter
        section in module metadata.
    :type filtered_rpms: list[str]
    :param dict xmd: a mapping representing XMD section in module metadata. A
        custom xmd could be passed for testing a particular scenario and some
        default key/value pairs are added if not present.
    :param bool store_to_db: whether to store created module metadata to the
        database. If set to True, ``db_session`` is required.
    :param virtual_streams: List of virtual streams provided by this module.
        If set, This requires ``db_session`` and ``store_to_db`` to be set to a
        session object and True.
    :type virtual_streams: list[str]
    :param arches: List of architectures this module is built against. If set
        to None, ``["x86_64"]`` is used as a default. If set, likewise
        ``virtual_stream``.
    :type arches: list[str]
    :return: New Module Build if set to store module metadata to database,
        otherwise the module metadata is returned.
    :rtype: ModuleBuild or Modulemd.Module
    """
    if store_to_db:
        assert db_session is not None
    if base_module:
        assert db_session is not None
    if virtual_streams:
        assert store_to_db
    if arches:
        assert store_to_db

    nsvc_regex = re.compile(r"([^:]+):([^:]+):([^:]+)(?::(.+))?")

    match = nsvc_regex.match(nsvc)
    if not match:
        raise ValueError('Argument nsvc is not in format N:S:V or N:S:V:C')

    name, stream, version, context = match.groups()

    mmd = Modulemd.ModuleStreamV2.new(name, stream)
    mmd.set_version(int(version))
    if context:
        mmd.set_context(context)
    mmd.set_summary("foo")
    # Test unicode in mmd.
    mmd.set_description(u"foo \u2019s")
    mmd.add_module_license("GPL")

    if filtered_rpms:
        for rpm in filtered_rpms:
            mmd.add_rpm_filter(rpm)

    def _add_require(mmd_deps, require_type, name, streams):
        assert isinstance(mmd_deps, Modulemd.Dependencies)
        assert require_type in ("requires", "buildrequires")
        assert isinstance(streams, (list, tuple))

        if require_type == "requires":
            add_stream = mmd_deps.add_runtime_stream
            set_empty_deps = mmd_deps.set_empty_runtime_dependencies_for_module
        else:
            add_stream = mmd_deps.add_buildtime_stream
            set_empty_deps = mmd_deps.set_empty_buildtime_dependencies_for_module

        for stream in streams:
            add_stream(name, stream)
        else:
            set_empty_deps(name)

    for dep_group in dependencies or []:
        mmd_deps = Modulemd.Dependencies()
        # A deps could be {"platform": ["f30"], "python": []}
        for require_type, deps in dep_group.items():
            for req_name, req_streams in deps.items():
                _add_require(mmd_deps, require_type, req_name, req_streams)
        mmd.add_dependencies(mmd_deps)

    # Caller could pass whole xmd including mbs, but if something is missing,
    # default values are given here.
    xmd = xmd or {"mbs": {}}
    xmd_mbs = xmd["mbs"]
    if "buildrequires" not in xmd_mbs:
        xmd_mbs["buildrequires"] = {}
    if "requires" not in xmd_mbs:
        xmd_mbs["requires"] = {}
    if "commit" not in xmd_mbs:
        xmd_mbs["commit"] = "ref_%s" % context
    if "mse" not in xmd_mbs:
        xmd_mbs["mse"] = "true"

    if virtual_streams:
        xmd_mbs["virtual_streams"] = virtual_streams

    mmd.set_xmd(xmd)

    if not store_to_db:
        return mmd

    module_build = ModuleBuild(
        name=name,
        stream=stream,
        stream_version=ModuleBuild.get_stream_version(stream),
        version=version,
        context=context,
        state=BUILD_STATES["ready"],
        scmurl="https://src.stg.fedoraproject.org/modules/unused.git?#ff1ea79",
        batch=1,
        owner="Tom Brady",
        time_submitted=datetime(2017, 2, 15, 16, 8, 18),
        time_modified=datetime(2017, 2, 15, 16, 19, 35),
        rebuild_strategy="changed-and-after",
        build_context=context,
        runtime_context=context,
        modulemd=mmd_to_str(mmd),
        koji_tag=xmd["mbs"]["koji_tag"] if "koji_tag" in xmd["mbs"] else None,
    )
    if base_module:
        module_build.buildrequires.append(base_module)
    db_session.add(module_build)
    db_session.commit()

    if virtual_streams:
        for virtual_stream in virtual_streams:
            vs_obj = db_session.query(VirtualStream).filter_by(name=virtual_stream).first()
            if not vs_obj:
                vs_obj = VirtualStream(name=virtual_stream)
                db_session.add(vs_obj)
                db_session.commit()

            if vs_obj not in module_build.virtual_streams:
                module_build.virtual_streams.append(vs_obj)
                db_session.commit()

    for arch in arches or ["x86_64"]:
        arch_obj = db_session.query(ModuleArch).filter_by(name=arch).first()
        if not arch_obj:
            arch_obj = ModuleArch(name=arch)
            db_session.add(arch_obj)
            db_session.commit()

        if arch_obj not in module_build.arches:
            module_build.arches.append(arch_obj)
            db_session.commit()

    return module_build


make_module_in_db = functools.partial(make_module, store_to_db=True)


def module_build_from_modulemd(yaml):
    """
    Create a ModuleBuild object and return. It is not written into database.
    Please commit by yourself if necessary.
    """
    mmd = load_mmd(yaml)
    build = ModuleBuild()
    build.name = mmd.get_module_name()
    build.stream = mmd.get_stream_name()
    build.version = mmd.get_version()
    build.state = BUILD_STATES["ready"]
    build.modulemd = yaml
    build.koji_tag = None
    build.batch = 0
    build.owner = "some_other_user"
    build.time_submitted = datetime(2016, 9, 3, 12, 28, 33)
    build.time_modified = datetime(2016, 9, 3, 12, 28, 40)
    build.time_completed = None
    build.rebuild_strategy = "changed-and-after"
    return build


def time_assert(*args, **kwargs):
    """
    check if delta between times in args not exceeding max_delta (part of kwargs)
    :param args: times to be compared
    :param kwargs: arguments for comparing:
               max_delta - value in seconds which shouldn't be exceeded (default: 2 sec, numeric)
               format_str - if values in args are of type str it should be formated as this
                            (default: '%Y-%m-%dT%H:%M:%SZ', str)
    :return: true if all times are within the range
    :rtype: bool
    """
    times_list = []
    format_str = kwargs.get("format_str", "%Y-%m-%dT%H:%M:%SZ")
    max_delta = kwargs.get("max_delta", 2)
    for t in args:
        if isinstance(t, string_types):
            dt = datetime.strptime(t, format_str)
        elif isinstance(t, datetime):
            dt = t
        else:
            raise TypeError(
                '"{}" is not supported for time_assert function'.format(type(t).__name__)
            )
        times_list.append(dt)
    return bool(abs((max(times_list) - min(times_list)).total_seconds()) <= max_delta)
