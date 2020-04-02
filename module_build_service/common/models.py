# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" SQLAlchemy Database models for the Flask app
"""

from __future__ import absolute_import
from collections import OrderedDict, namedtuple
from datetime import datetime
import hashlib
import json
import re

import kobo.rpmlib
import koji
import sqlalchemy
from sqlalchemy import func, and_
from sqlalchemy.orm import lazyload
from sqlalchemy.orm import validates, load_only
from sqlalchemy.schema import Index


from module_build_service import db, get_url_for
from module_build_service.common import conf, log
from module_build_service.common.errors import UnprocessableEntity
from module_build_service.common.messaging import module_build_state_change_out_queue
from module_build_service.common.messaging import notify_on_module_state_change
from module_build_service.common.utils import load_mmd
from module_build_service.scheduler import events

DEFAULT_MODULE_CONTEXT = "00000000"


# Just like koji.BUILD_STATES, except our own codes for modules.
BUILD_STATES = {
    # This is (obviously) the first state a module build enters.
    #
    # When a user first submits a module build, it enters this state. We parse
    # the modulemd file, learn the NVR, create a record for the module build.
    # and publish the message.
    #
    # Then, we validate that the components are available, and that we can
    # fetch them. If this is all good, then we set the build to the 'wait'
    # state. If anything goes wrong, we jump immediately to the 'failed' state.
    "init": 0,
    # Here, the scheduler picks up tasks in wait and switches to build
    # immediately. Eventually, we'll add throttling logic here so we don't
    # submit too many builds for the build system to handle
    "wait": 1,
    # The scheduler works on builds in this state. We prepare the buildroot,
    # submit builds for all the components, and wait for the results to come
    # back.
    "build": 2,
    # Once all components have succeeded, we set the top-level module build
    # to 'done'.
    "done": 3,
    # If any of the component builds fail, then we set the top-level module
    # build to 'failed' also.
    "failed": 4,
    # This is a state to be set when a module is ready to be part of a
    # larger compose. perhaps it is set by an external service that knows
    # about the Grand Plan.
    "ready": 5,
    # If the module has failed and was garbage collected by MBS
    "garbage": 6,
}

INVERSE_BUILD_STATES = {v: k for k, v in BUILD_STATES.items()}
FAILED_STATES = (BUILD_STATES["failed"], BUILD_STATES["garbage"])

STATE_TRANSITION_FAILURE_TYPES = ["unspec", "user", "infra"]


Contexts = namedtuple(
    "Contexts", "build_context runtime_context context build_context_no_bms")


def _utc_datetime_to_iso(datetime_object):
    """
    Takes a UTC datetime object and returns an ISO formatted string
    :param datetime_object: datetime.datetime
    :return: string with datetime in ISO format
    """
    if datetime_object:
        # Converts the datetime to ISO 8601
        return datetime_object.strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


class MBSBase(db.Model):
    # TODO -- we can implement functionality here common to all our model classes
    __abstract__ = True


module_builds_to_module_buildrequires = db.Table(
    "module_builds_to_module_buildrequires",
    db.Column("module_id", db.Integer, db.ForeignKey("module_builds.id"), nullable=False),
    db.Column(
        "module_buildrequire_id", db.Integer, db.ForeignKey("module_builds.id"), nullable=False),
    db.UniqueConstraint("module_id", "module_buildrequire_id", name="unique_buildrequires"),
)


module_builds_to_virtual_streams = db.Table(
    "module_builds_to_virtual_streams",
    db.Column("module_build_id", db.Integer, db.ForeignKey("module_builds.id"), nullable=False),
    db.Column("virtual_stream_id", db.Integer, db.ForeignKey("virtual_streams.id"), nullable=False),
    db.UniqueConstraint(
        "module_build_id", "virtual_stream_id", name="unique_module_to_virtual_stream"),
)


module_builds_to_arches = db.Table(
    "module_builds_to_arches",
    db.Column("module_build_id", db.Integer, db.ForeignKey("module_builds.id"), nullable=False),
    db.Column(
        "module_arch_id", db.Integer, db.ForeignKey("module_arches.id"),
        nullable=False),
    db.UniqueConstraint(
        "module_build_id", "module_arch_id", name="unique_module_to_arch"),
)


class ModuleBuild(MBSBase):
    __tablename__ = "module_builds"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, index=True)
    stream = db.Column(db.String, nullable=False)
    version = db.Column(db.String, nullable=False)
    build_context = db.Column(db.String)
    build_context_no_bms = db.Column(db.String)
    runtime_context = db.Column(db.String)
    context = db.Column(db.String, nullable=False, server_default=DEFAULT_MODULE_CONTEXT)
    state = db.Column(db.Integer, nullable=False, index=True)
    state_reason = db.Column(db.String)
    modulemd = db.Column(db.String, nullable=False)
    koji_tag = db.Column(db.String, index=True)  # This gets set after 'wait'
    # Koji tag to which tag the Content Generator Koji build.
    cg_build_koji_tag = db.Column(db.String)  # This gets set after wait
    scmurl = db.Column(db.String)
    scratch = db.Column(db.Boolean, default=False)
    # JSON encoded list of links of custom SRPMs uploaded to Koji
    srpms = db.Column(db.String)
    owner = db.Column(db.String, nullable=False)
    time_submitted = db.Column(db.DateTime, nullable=False)
    time_modified = db.Column(db.DateTime)
    time_completed = db.Column(db.DateTime)
    new_repo_task_id = db.Column(db.Integer)
    rebuild_strategy = db.Column(db.String, nullable=False)
    virtual_streams = db.relationship(
        "VirtualStream", secondary=module_builds_to_virtual_streams, back_populates="module_builds")
    reused_module_id = db.Column(db.Integer, db.ForeignKey("module_builds.id"))
    reused_module = db.relationship("ModuleBuild", remote_side="ModuleBuild.id")
    log_messages = db.relationship("LogMessage", backref="module_build", lazy="dynamic")

    # List of arches against which the module is built.
    # NOTE: It is not filled for imported modules, because imported module builds have not been
    # built by MBS.
    arches = db.relationship(
        "ModuleArch", secondary=module_builds_to_arches, back_populates="module_builds",
        order_by="ModuleArch.name")

    # A monotonically increasing integer that represents which batch or
    # iteration this module is currently on for successive rebuilds of its
    # components.  Think like 'mockchain --recurse'
    batch = db.Column(db.Integer, default=0)

    # This is only used for base modules for ordering purposes (f27.0.1 => 270001)
    stream_version = db.Column(db.Float)
    buildrequires = db.relationship(
        "ModuleBuild",
        secondary=module_builds_to_module_buildrequires,
        primaryjoin=module_builds_to_module_buildrequires.c.module_id == id,
        secondaryjoin=module_builds_to_module_buildrequires.c.module_buildrequire_id == id,
        backref="buildrequire_for",
    )

    __table_args__ = (
        Index(
            "idx_module_builds_name_stream_version_context",
            "name", "stream", "version", "context", unique=True
        ),
    )

    rebuild_strategies = {
        "all": "All components will be rebuilt",
        "changed-and-after": (
            "All components that have changed and those in subsequent batches will be rebuilt"
        ),
        "only-changed": "All changed components will be rebuilt",
    }

    def current_batch(self, state=None):
        """ Returns all components of this module in the current batch. """

        if not self.batch:
            raise ValueError("No batch is in progress: %r" % self.batch)

        if state is not None:
            return [
                component for component in self.component_builds
                if component.batch == self.batch and component.state == state
            ]
        else:
            return [
                component for component in self.component_builds
                if component.batch == self.batch
            ]

    def last_batch_id(self):
        """ Returns the id of the last batch """
        return max([build.batch for build in self.component_builds])

    def up_to_current_batch(self, state=None):
        """
        Returns all components of this module in the current batch and
        in the previous batches.
        """

        if not self.batch:
            raise ValueError("No batch is in progress: %r" % self.batch)

        if state is not None:
            return [
                component for component in self.component_builds
                if component.batch <= self.batch and component.state == state
            ]
        else:
            return [
                component for component in self.component_builds if component.batch <= self.batch
            ]

    @staticmethod
    def get_by_id(db_session, module_build_id):
        """Find out a module build by id and return

        :param db_session: SQLAlchemy database session object.
        :param int module_build_id: the module build id to find out.
        :return: the found module build. None is returned if no module build
            with specified id in database.
        :rtype: :class:`ModuleBuild`
        """
        return db_session.query(ModuleBuild).filter(ModuleBuild.id == module_build_id).first()

    @staticmethod
    def get_last_build_in_all_streams(db_session, name):
        """
        Returns list of all latest ModuleBuilds in "ready" state for all
        streams for given module `name`.
        """
        # Prepare the subquery to find out all unique name:stream records.
        subq = (
            db_session.query(
                func.max(ModuleBuild.id).label("maxid"),
                func.max(sqlalchemy.cast(ModuleBuild.version, db.BigInteger)),
            )
            .group_by(ModuleBuild.stream)
            .filter_by(name=name, state=BUILD_STATES["ready"])
            .subquery("t2")
        )

        # Use the subquery to actually return all the columns for its results.
        query = db_session.query(ModuleBuild).join(
            subq, and_(ModuleBuild.id == subq.c.maxid))
        return query.all()

    @staticmethod
    def _get_last_builds_in_stream_query(db_session, name, stream, **kwargs):
        # Prepare the subquery to find out all unique name:stream records.
        subq = (
            db_session.query(
                func.max(sqlalchemy.cast(ModuleBuild.version, db.BigInteger)).label("maxversion")
            )
            .filter_by(name=name, state=BUILD_STATES["ready"], stream=stream, **kwargs)
            .subquery("t2")
        )

        # Use the subquery to actually return all the columns for its results.
        query = db_session.query(ModuleBuild).join(
            subq,
            and_(
                ModuleBuild.name == name,
                ModuleBuild.stream == stream,
                sqlalchemy.cast(ModuleBuild.version, db.BigInteger) == subq.c.maxversion,
            ),
        )
        return query

    @staticmethod
    def get_last_builds_in_stream(db_session, name, stream, virtual_streams=None, **kwargs):
        """
        Returns the latest builds in "ready" state for given name:stream.

        :param db_session: SQLAlchemy session.
        :param str name: Name of the module to search builds for.
        :param str stream: Stream of the module to search builds for.
        :param list virtual_streams: a list of the virtual streams to filter on. The filtering uses
            "or" logic. When falsy, no filtering occurs.
        :param dict kwargs: Key/value pairs passed to SQLAlchmey filter_by method
            allowing to set additional filter for results.

        """
        # Prepare the subquery to find out all unique name:stream records.

        query = ModuleBuild._get_last_builds_in_stream_query(db_session, name, stream, **kwargs)
        query = ModuleBuild._add_virtual_streams_filter(db_session, query, virtual_streams)
        return query.all()

    @staticmethod
    def get_last_build_in_stream(db_session, name, stream, **kwargs):
        """
        Returns the latest build in "ready" state for given name:stream.

        :param db_session: SQLAlchemy session.
        :param str name: Name of the module to search builds for.
        :param str stream: Stream of the module to search builds for.
        :param dict kwargs: Key/value pairs passed to SQLAlchmey filter_by method
            allowing to set additional filter for results.
        """
        return ModuleBuild._get_last_builds_in_stream_query(
            db_session, name, stream, **kwargs
        ).first()

    @staticmethod
    def get_build_from_nsvc(db_session, name, stream, version, context, **kwargs):
        """
        Returns build defined by NSVC. Optional kwargs are passed to SQLAlchemy
        filter_by method.
        """
        return (
            db_session.query(ModuleBuild)
            .filter_by(name=name, stream=stream, version=str(version), context=context, **kwargs)
            .first()
        )

    @staticmethod
    def get_scratch_builds_from_nsvc(db_session, name, stream, version, context, **kwargs):
        """
        Returns all scratch builds defined by NSVC. This is done by using the supplied `context`
        as a match prefix. Optional kwargs are passed to SQLAlchemy filter_by method.
        """
        return (
            db_session.query(ModuleBuild)
            .filter_by(name=name, stream=stream, version=str(version), scratch=True, **kwargs)
            .filter(ModuleBuild.context.like(context + "%"))
            .all()
        )

    @staticmethod
    def _add_stream_version_lte_filter(db_session, query, stream_version):
        """
        Adds a less than or equal to filter for stream versions based on x.y.z versioning.

        In essence, the filter does `XX0000 <= stream_version <= XXYYZZ`

        :param db_session: a SQLAlchemy session
        :param query: a SQLAlchemy query to add the filtering to
        :param int stream_version: the stream version to filter on
        :return: the query with the added stream version filter
        """
        if not stream_version:
            return query

        # Compute the minimal stream_version. For example, for `stream_version` 281234,
        # the minimal `stream_version` is 280000.
        min_stream_version = (stream_version // 10000) * 10000
        return query.filter(ModuleBuild.stream_version <= stream_version).filter(
            ModuleBuild.stream_version >= min_stream_version)

    @staticmethod
    def _add_virtual_streams_filter(db_session, query, virtual_streams):
        """
        Adds a filter on ModuleBuild.virtual_streams to an existing query.

        :param db_session: a SQLAlchemy session
        :param query: a SQLAlchemy query to add the filtering to
        :param list virtual_streams: a list of the virtual streams to filter on. The filtering uses
            "or" logic. When falsy, no filtering occurs.
        :return: the query with the added virtual stream filters
        """
        if not virtual_streams:
            return query

        # Create a subquery that filters down all the module builds that contain the virtual
        # streams. Using distinct is necessary since a module build may contain multiple virtual
        # streams that are desired.
        modules_with_virtual_streams = (
            db_session.query(ModuleBuild)
            .join(VirtualStream, ModuleBuild.virtual_streams)
            .filter(VirtualStream.name.in_(virtual_streams))
            .order_by(ModuleBuild.id)
            .distinct(ModuleBuild.id)
        ).subquery()

        # Join the original query with the subquery so that only module builds with the desired
        # virtual streams remain
        return query.join(
            modules_with_virtual_streams, ModuleBuild.id == modules_with_virtual_streams.c.id)

    @staticmethod
    def get_last_builds_in_stream_version_lte(
            db_session, name, stream_version=None, virtual_streams=None, states=None):
        """
        Returns the latest builds in "ready" state for given name:stream limited by
        `stream_version`. The `stream_version` is int generated by `get_stream_version(...)`
        method from "x.y.z" version string.
        The builds returned by this method are limited by stream_version XX.YY.ZZ like this:
            "XX0000 <= build.stream_version <= XXYYZZ".

        :param db_session: SQLAlchemy session.
        :param str name: Name of the module to search builds for.
        :param int stream_version: Maximum stream_version to search builds for. When None,
            the stream_version is not limited.
        :param list virtual_streams: A list of the virtual streams to filter on. The filtering uses
            "or" logic. When falsy, no filtering occurs.
        """
        states = states or [BUILD_STATES["ready"]]
        query = (
            db_session.query(ModuleBuild)
            .filter(ModuleBuild.name == name)
            .filter(ModuleBuild.state.in_(states))
            .order_by(sqlalchemy.cast(ModuleBuild.version, db.BigInteger).desc())
        )

        query = ModuleBuild._add_stream_version_lte_filter(db_session, query, stream_version)
        query = ModuleBuild._add_virtual_streams_filter(db_session, query, virtual_streams)

        builds = query.all()

        # In case there are multiple versions of single name:stream build, we want to return
        # the latest version only. The `builds` are ordered by "version" desc, so we
        # can just get the first (greatest) version of name:stream.
        # TODO: Is there a way how to do that nicely in the SQL query itself?
        seen = {}  # {"n:s": v, ...}
        ret = []
        for build in builds:
            ns = "%s:%s" % (build.name, build.stream)
            if ns in seen and seen[ns] != build.version:
                # Skip the builds if we already handled this nsv before.
                continue
            elif ns in seen and seen[ns] == build.version:
                # Different context of the NSV
                ret.append(build)
                continue

            seen[ns] = build.version
            ret.append(build)
        return ret

    @staticmethod
    def get_module_count(db_session, **kwargs):
        """
        Determine the number of modules that match the provided filter.

        :param db_session: SQLAlchemy session
        :return: the number of modules that match the provided filter
        :rtype: int
        """
        return db_session.query(func.count(ModuleBuild.id)).filter_by(**kwargs).scalar()

    @staticmethod
    def get_build_by_koji_tag(db_session, tag):
        """Get build by its koji_tag"""
        return db_session.query(ModuleBuild).filter_by(koji_tag=tag).first()

    def mmd(self):
        try:
            return load_mmd(self.modulemd)
        except UnprocessableEntity:
            log.exception("An error occurred while trying to parse the modulemd")
            raise ValueError("Invalid modulemd")

    @property
    def previous_non_failed_state(self):
        for trace in reversed(self.module_builds_trace):
            if trace.state != BUILD_STATES["failed"]:
                return trace.state

    @validates("state")
    def validate_state(self, key, field):
        if field in BUILD_STATES.values():
            return field
        if field in BUILD_STATES:
            return BUILD_STATES[field]
        raise ValueError("%s: %s, not in %r" % (key, field, BUILD_STATES))

    @validates("rebuild_strategy")
    def validate_rebuild_strategy(self, key, rebuild_strategy):
        if rebuild_strategy not in self.rebuild_strategies.keys():
            choices = ", ".join(self.rebuild_strategies.keys())
            raise ValueError(
                'The rebuild_strategy of "{0}" is invalid. Choose from: {1}'.format(
                    rebuild_strategy, choices)
            )
        return rebuild_strategy

    @classmethod
    def from_module_event(cls, db_session, event):
        if type(event) == events.MBSModule:
            return db_session.query(cls).filter(cls.id == event.module_build_id).first()
        else:
            raise ValueError("%r is not a module message." % type(event).__name__)

    @classmethod
    def contexts_from_mmd(cls, mmd_str):
        """
        Returns tuple (build_context, runtime_context, context)
        with hashes:
            - build_context - Hash of stream names of expanded buildrequires.
            - runtime_context - Hash of stream names of expanded runtime requires.
            - context - Hash of combined hashes of build_context and runtime_context.

        :param str mmd_str: String with Modulemd metadata.
        :rtype: Contexts
        :return: Named tuple with build_context, runtime_context and context hashes.
        """
        try:
            mmd = load_mmd(mmd_str)
        except UnprocessableEntity:
            raise ValueError("Invalid modulemd")
        mbs_xmd_buildrequires = mmd.get_xmd()["mbs"]["buildrequires"]
        mmd_deps = mmd.get_dependencies()

        build_context = cls.calculate_build_context(mbs_xmd_buildrequires)
        build_context_no_bms = cls.calculate_build_context(mbs_xmd_buildrequires, True)
        runtime_context = cls.calculate_runtime_context(mmd_deps)

        return Contexts(
            build_context,
            runtime_context,
            cls.calculate_module_context(build_context, runtime_context),
            build_context_no_bms
        )

    @staticmethod
    def calculate_build_context(mbs_xmd_buildrequires, filter_base_modules=False):
        """
        Returns the hash of stream names of expanded buildrequires
        :param mbs_xmd_buildrequires: xmd["mbs"]["buildrequires"] from Modulemd
        :param bool filter_base_modules: When True, base modules are not used to compute
            the build context.
        :rtype: str
        :return: build_context hash
        """
        deps_to_filter = conf.base_module_names if filter_base_modules else []
        mmd_formatted_buildrequires = {
            dep: info["stream"] for dep, info in mbs_xmd_buildrequires.items()
            if dep not in deps_to_filter
        }
        property_json = json.dumps(OrderedDict(sorted(mmd_formatted_buildrequires.items())))
        return hashlib.sha1(property_json.encode("utf-8")).hexdigest()

    @staticmethod
    def calculate_runtime_context(mmd_dependencies):
        """
        Returns the hash of stream names of expanded runtime requires
        :param mmd_dependencies: dependencies from Modulemd
        :rtype: str
        :return: runtime_context hash
        """
        mmd_requires = {}
        for deps in mmd_dependencies:
            for name in deps.get_runtime_modules():
                streams = deps.get_runtime_streams(name)
                mmd_requires[name] = mmd_requires.get(name, set()).union(streams)

        # Sort the streams for each module name and also sort the module names.
        mmd_requires = {dep: sorted(list(streams)) for dep, streams in mmd_requires.items()}
        property_json = json.dumps(OrderedDict(sorted(mmd_requires.items())))
        return hashlib.sha1(property_json.encode("utf-8")).hexdigest()

    @staticmethod
    def calculate_module_context(build_context, runtime_context):
        """
        Returns the hash of combined hashes of build_context and runtime_context
        :param build_context: hash returned by calculate_build_context
        :type build_context: str
        :param runtime_context: hash returned by calculate_runtime_context
        :type runtime_context: str
        :rtype: str
        :return: module context hash
        """
        combined_hashes = "{0}:{1}".format(build_context, runtime_context)
        return hashlib.sha1(combined_hashes.encode("utf-8")).hexdigest()[:8]

    def siblings(self, db_session):
        query = db_session.query(ModuleBuild).filter(
            ModuleBuild.name == self.name,
            ModuleBuild.stream == self.stream,
            ModuleBuild.version == self.version,
            ModuleBuild.scratch == self.scratch,
            ModuleBuild.id != self.id,
        ).options(load_only("id"))
        siblings_ids = [build.id for build in query.all()]
        return siblings_ids

    @property
    def nvr(self):
        return {
            u"name": self.name,
            u"version": self.stream.replace("-", "_"),
            u"release": "{0}.{1}".format(self.version, self.context)
        }

    @property
    def nvr_string(self):
        return kobo.rpmlib.make_nvr(self.nvr)

    @classmethod
    def create(
        cls,
        db_session,
        conf,
        name,
        stream,
        version,
        modulemd,
        scmurl,
        username,
        context=None,
        rebuild_strategy=None,
        scratch=False,
        srpms=None,
        **kwargs
    ):
        now = datetime.utcnow()
        module = cls(
            name=name,
            stream=stream,
            version=version,
            context=context,
            state="init",
            modulemd=modulemd,
            scmurl=scmurl,
            owner=username,
            time_submitted=now,
            time_modified=now,
            # If the rebuild_strategy isn't specified, use the default
            rebuild_strategy=rebuild_strategy or conf.rebuild_strategy,
            scratch=scratch,
            srpms=json.dumps(srpms or []),
            **kwargs
        )
        # Add a state transition to "init"
        mbt = ModuleBuildTrace(state_time=now, state=module.state)
        module.module_builds_trace.append(mbt)

        # Record the base modules this module buildrequires
        for base_module in module.get_buildrequired_base_modules(db_session):
            module.buildrequires.append(base_module)

        db_session.add(module)
        db_session.commit()
        return module

    def transition(self, db_session, conf, state, state_reason=None, failure_type="unspec"):
        """Record that a build has transitioned state.

        The history of state transitions are recorded in model
        ``ModuleBuildTrace``. If transform to a different state, for example
        from ``build`` to ``done``, message will be sent to configured message
        bus.

        :param db_session: SQLAlchemy session object.
        :param conf: MBS config object returned from function :func:`init_config`
            which contains loaded configs.
        :type conf: :class:`Config`
        :param int state: the state value to transition to. Refer to ``BUILD_STATES``.
        :param str state_reason: optional reason of why to transform to ``state``.
        :param str failure_type: optional failure type. Refer to constant
            ``STATE_TRANSITION_FAILURE_TYPES`` for valid values.
        """
        assert failure_type in STATE_TRANSITION_FAILURE_TYPES

        now = datetime.utcnow()
        old_state = self.state
        self.state = state
        self.time_modified = now

        from module_build_service.common.monitor import (
            builder_success_counter, builder_failed_counter
        )

        new_state_name = INVERSE_BUILD_STATES[self.state]
        if new_state_name in ["done", "failed"]:
            self.time_completed = now
            if new_state_name == "done":
                builder_success_counter.inc()
            else:
                builder_failed_counter.labels(reason=failure_type).inc()

        if state_reason:
            self.state_reason = state_reason

        # record module's state change
        mbt = ModuleBuildTrace(state_time=now, state=self.state, state_reason=state_reason)
        self.module_builds_trace.append(mbt)

        log.info(
            "State transition: %r -> %r, %r",
            INVERSE_BUILD_STATES[old_state], new_state_name, self)

        if old_state != self.state:
            # Do not send a message now until the data changes are committed
            # into database.
            module_build_state_change_out_queue.put(
                self.json(db_session, show_tasks=False))

    @classmethod
    def local_modules(cls, db_session, name=None, stream=None):
        """
        Returns list of local module builds added by
        load_local_builds(...). When `name` or `stream` is set,
        it is used to further limit the result set.

        If conf.system is not set to "mock" or "test", returns empty
        list everytime, because local modules make sense only when
        building using Mock backend or during tests.
        """
        if conf.system in ["koji"]:
            return []

        filters = {}
        if name:
            filters["name"] = name
        if stream:
            filters["stream"] = stream
        local_modules = db_session.query(ModuleBuild).filter_by(**filters).all()
        if not local_modules:
            return []

        local_modules = [
            m for m in local_modules if m.koji_tag and m.koji_tag.startswith(conf.mock_resultsdir)
        ]
        return local_modules

    @classmethod
    def by_state(cls, db_session, state):
        """Get module builds by state

        :param db_session: SQLAlchemy session object.
        :param str state: state name. Refer to key names of ``models.BUILD_STATES``.
        :return: a list of module builds in the specified state.
        :rtype: list[:class:`ModuleBuild`]
        """
        return db_session.query(ModuleBuild).filter_by(state=BUILD_STATES[state]).all()

    @classmethod
    def get_by_tag(cls, db_session, tag_name):
        tag = tag_name[:-6] if tag_name.endswith("-build") else tag_name
        query = db_session.query(cls).filter(
            cls.koji_tag == tag,
            cls.state == BUILD_STATES["build"]
        )
        count = query.count()
        if count > 1:
            raise RuntimeError("%r module builds in flight for %r" % (count, tag))
        return query.first()

    def short_json(self, show_stream_version=False, show_scratch=True):
        rv = {
            "id": self.id,
            "state": self.state,
            "state_name": INVERSE_BUILD_STATES[self.state],
            "stream": self.stream,
            "version": self.version,
            "name": self.name,
            "context": self.context,
        }
        if show_stream_version:
            rv["stream_version"] = self.stream_version
        if show_scratch:
            rv["scratch"] = self.scratch
        return rv

    def json(self, db_session, show_tasks=True):
        mmd = self.mmd()
        xmd = mmd.get_xmd()
        buildrequires = xmd.get("mbs", {}).get("buildrequires", {})
        rv = self.short_json()
        rv.update({
            "component_builds": [build.id for build in self.component_builds],
            "koji_tag": self.koji_tag,
            "owner": self.owner,
            "rebuild_strategy": self.rebuild_strategy,
            "scmurl": self.scmurl,
            "srpms": json.loads(self.srpms or "[]"),
            "siblings": self.siblings(db_session),
            "state_reason": self.state_reason,
            "time_completed": _utc_datetime_to_iso(self.time_completed),
            "time_modified": _utc_datetime_to_iso(self.time_modified),
            "time_submitted": _utc_datetime_to_iso(self.time_submitted),
            "buildrequires": buildrequires,
        })
        if show_tasks:
            rv["tasks"] = self.tasks(db_session)
        return rv

    def extended_json(self, db_session, show_state_url=False, api_version=1):
        """
        :kwarg show_state_url: this will determine if `get_url_for` should be run to determine
        what the `state_url` is. This should be set to `False` when extended_json is called from
        the backend because it forces an app context to be created, which causes issues with
        SQLAlchemy sessions.
        :kwarg api_version: the API version to use when building the state URL
        """
        rv = self.json(db_session, show_tasks=True)
        state_url = None
        if show_state_url:
            state_url = get_url_for("module_build", api_version=api_version, id=self.id)

        rv.update({
            "base_module_buildrequires": [br.short_json(True, False) for br in self.buildrequires],
            "build_context": self.build_context,
            "modulemd": self.modulemd,
            "reused_module_id": self.reused_module_id,
            "runtime_context": self.runtime_context,
            "state_trace": [
                {
                    "time": _utc_datetime_to_iso(record.state_time),
                    "state": record.state,
                    "state_name": INVERSE_BUILD_STATES[record.state],
                    "reason": record.state_reason,
                }
                for record in self.state_trace(db_session, self.id)
            ],
            "state_url": state_url,
            "stream_version": self.stream_version,
            "virtual_streams": [virtual_stream.name for virtual_stream in self.virtual_streams],
            "arches": [arch.name for arch in self.arches],
        })

        return rv

    def log_message(self, session, message):
        log.info(message)
        log_msg = LogMessage(
            component_build_id=None,
            module_build_id=self.id,
            message=message,
            time_created=datetime.utcnow(),
        )
        session.add(log_msg)
        session.commit()

    def tasks(self, db_session):
        """
        :return: dictionary containing the tasks associated with the build
        """
        tasks = dict()
        if self.id and self.state != "init":
            for build in (
                db_session.query(ComponentBuild).filter_by(module_id=self.id)
                .options(lazyload("module_build"))
                .all()
            ):
                tasks[build.format] = tasks.get(build.format, {})
                tasks[build.format][build.package] = dict(
                    task_id=build.task_id,
                    state=build.state,
                    state_reason=build.state_reason,
                    nvr=build.nvr,
                    # TODO -- it would be really nice from a UX PoV to get a
                    # link to the remote task here.
                )

        return tasks

    def state_trace(self, db_session, module_id):
        return (
            db_session.query(ModuleBuildTrace).filter_by(module_id=module_id)
            .order_by(ModuleBuildTrace.state_time)
            .all()
        )

    @staticmethod
    def get_stream_version(stream, right_pad=True):
        """
        Parse the supplied stream to find its version.

        This will parse a stream such as "f27" and return 270000. Another example would be a stream
        of "f27.0.1" and return 270001.
        :param str stream: the module stream
        :kwarg bool right_pad: determines if the right side of the stream version should be padded
            with zeroes (e.g. `f27` => `27` vs `270000`)
        :return: a stream version represented as a float. Stream suffix could
            be added according to config ``stream_suffixes``.
        :rtype: float or None if the stream doesn't have a valid version
        """
        # The platform version (e.g. prefix1.2.0 => 010200)
        version = ""
        for char in stream:
            # See if the current character is an integer, signifying the version has started
            if char.isdigit():
                version += char
            # If version isn't set, then a digit hasn't been encountered
            elif version:
                # If the character is a period and the version is set, then
                # the loop is still processing the version part of the stream
                if char == ".":
                    version += "."
                # If the version is set and the character is not a period or
                # digit, then the remainder of the stream is a suffix like "-beta"
                else:
                    break

        # Remove the periods and pad the numbers if necessary
        version = "".join([section.zfill(2) for section in version.rstrip(".").split(".")])

        if version:
            if right_pad:
                version += (6 - len(version)) * "0"

            result = float(version)

            for regexp, suffix in conf.stream_suffixes.items():
                if re.match(regexp, stream):
                    result += suffix
                    break

            return result

    def get_buildrequired_base_modules(self, db_session):
        """
        Find the base modules in the modulemd's xmd/mbs/buildrequires section.

        :param db_session: the SQLAlchemy database session to use to query
        :return: a list of ModuleBuild objects of the base modules that are buildrequired with the
            ordering in conf.base_module_names preserved
        :rtype: list
        :raises RuntimeError: when the xmd section isn't properly filled out by MBS
        """
        rv = []
        xmd = self.mmd().get_xmd()
        for bm in conf.base_module_names:
            try:
                bm_dict = xmd["mbs"]["buildrequires"].get(bm)
            except KeyError:
                raise RuntimeError("The module's mmd is missing xmd/mbs or xmd/mbs/buildrequires.")

            if not bm_dict:
                continue
            base_module = self.get_build_from_nsvc(
                db_session, bm, bm_dict["stream"], bm_dict["version"], bm_dict["context"]
            )
            if not base_module:
                log.error(
                    'Module #{} buildrequires "{}" but it wasn\'t found in the database'.format(
                        self.id, repr(bm_dict))
                )
                continue
            rv.append(base_module)

        return rv

    def __repr__(self):
        return (
            "<ModuleBuild %s, id=%d, stream=%s, version=%s, scratch=%r,"
            " state %r, batch %r, state_reason %r>"
        ) % (
            self.name,
            self.id,
            self.stream,
            self.version,
            self.scratch,
            INVERSE_BUILD_STATES[self.state],
            self.batch,
            self.state_reason,
        )

    def update_virtual_streams(self, db_session, virtual_streams):
        """Add and remove virtual streams to and from this build

        If a virtual stream is only associated with this build, remove it from
        database as well.

        :param db_session: SQLAlchemy session object.
        :param virtual_streams: list of virtual streams names used to update
            this build's virtual streams.
        :type virtual_streams: list[str]
        """
        orig_virtual_streams = set(item.name for item in self.virtual_streams)
        new_virtual_streams = set(virtual_streams)

        dropped_virtual_streams = orig_virtual_streams - new_virtual_streams
        newly_added_virtual_streams = new_virtual_streams - orig_virtual_streams

        for stream_name in newly_added_virtual_streams:
            virtual_stream = VirtualStream.get_by_name(db_session, stream_name)
            if not virtual_stream:
                virtual_stream = VirtualStream(name=stream_name)
            self.virtual_streams.append(virtual_stream)

        for stream_name in dropped_virtual_streams:
            virtual_stream = VirtualStream.get_by_name(db_session, stream_name)
            only_associated_with_self = (
                len(virtual_stream.module_builds) == 1
                and virtual_stream.module_builds[0].id == self.id
            )

            self.virtual_streams.remove(virtual_stream)
            if only_associated_with_self:
                db_session.delete(virtual_stream)


class VirtualStream(MBSBase):
    __tablename__ = "virtual_streams"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, unique=True)
    module_builds = db.relationship(
        "ModuleBuild", secondary=module_builds_to_virtual_streams, back_populates="virtual_streams"
    )

    def __repr__(self):
        return "<VirtualStream id={} name={}>".format(self.id, self.name)

    @classmethod
    def get_by_name(cls, db_session, name):
        return db_session.query(cls).filter_by(name=name).first()


class ModuleArch(MBSBase):
    __tablename__ = "module_arches"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, unique=True)
    module_builds = db.relationship(
        "ModuleBuild", secondary=module_builds_to_arches, back_populates="arches"
    )

    def __repr__(self):
        return "<ModuleArch id={} name={}>".format(self.id, self.name)


class ModuleBuildTrace(MBSBase):
    __tablename__ = "module_builds_trace"
    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, db.ForeignKey("module_builds.id"), nullable=False)
    state_time = db.Column(db.DateTime, nullable=False)
    state = db.Column(db.Integer, nullable=True)
    state_reason = db.Column(db.String, nullable=True)

    module_build = db.relationship("ModuleBuild", backref="module_builds_trace", lazy=False)

    def json(self, db_session):
        retval = {
            "id": self.id,
            "module_id": self.module_id,
            "state_time": _utc_datetime_to_iso(self.state_time),
            "state": self.state,
            "state_reason": self.state_reason,
        }

        return retval

    def __repr__(self):
        return (
            "<ModuleBuildTrace %s, module_id: %s, state_time: %r, state: %s, state_reason: %s>"
            % (self.id, self.module_id, self.state_time, self.state, self.state_reason)
        )


class ComponentBuild(MBSBase):
    __tablename__ = "component_builds"
    id = db.Column(db.Integer, primary_key=True)
    package = db.Column(db.String, nullable=False)
    scmurl = db.Column(db.String, nullable=False)
    # XXX: Consider making this a proper ENUM
    format = db.Column(db.String, nullable=False)
    task_id = db.Column(db.Integer)  # This is the id of the build in koji
    # This is the commit hash that component was built with
    ref = db.Column(db.String, nullable=True)
    # XXX: Consider making this a proper ENUM (or an int)
    state = db.Column(db.Integer)
    # Reason why the build failed
    state_reason = db.Column(db.String)
    # This stays as None until the build completes.
    nvr = db.Column(db.String)
    # True when this component build is tagged into buildroot (-build tag).
    tagged = db.Column(db.Boolean, default=False)
    # True when this component build is tagged into final tag.
    tagged_in_final = db.Column(db.Boolean, default=False)
    # True when this component build is build-time only (should be tagged only
    # to -build tag)
    build_time_only = db.Column(db.Boolean, default=False)
    # True if buildonly is True in modulemd
    buildonly = db.Column(db.Boolean, default=False)

    # A monotonically increasing integer that represents which batch or
    # iteration this *component* is currently in.  This relates to the owning
    # module's batch.  This one defaults to None, which means that this
    # component is not currently part of a batch.
    batch = db.Column(db.Integer, default=0, index=True)

    module_id = db.Column(db.Integer, db.ForeignKey("module_builds.id"), nullable=False)
    module_build = db.relationship("ModuleBuild", backref="component_builds", lazy=False)
    reused_component_id = db.Column(db.Integer, db.ForeignKey("component_builds.id"))
    log_messages = db.relationship("LogMessage", backref="component_build", lazy="dynamic")

    # Weight defines the complexity of the component build as calculated by the builder's
    # get_build_weights function
    weight = db.Column(db.Float, default=0)

    __table_args__ = (
        Index("idx_component_builds_build_id_task_id", "module_id", "task_id", unique=True),
        Index("idx_component_builds_build_id_nvr", "module_id", "nvr", unique=True),
    )

    @classmethod
    def from_component_event(cls, db_session, task_id, module_id=None):
        _filter = db_session.query(cls).filter
        if module_id is None:
            return _filter(cls.task_id == task_id).first()
        else:
            return _filter(cls.task_id == task_id, cls.module_id == module_id).one()

    @classmethod
    def from_component_name(cls, db_session, component_name, module_id):
        return db_session.query(cls).filter_by(package=component_name, module_id=module_id).first()

    @classmethod
    def from_component_nvr(cls, db_session, nvr, module_id):
        return db_session.query(cls).filter_by(nvr=nvr, module_id=module_id).first()

    def state_trace(self, db_session):
        return (
            db_session.query(ComponentBuildTrace).filter_by(component_id=self.id)
            .order_by(ComponentBuildTrace.state_time)
            .all()
        )

    def json(self, db_session):
        retval = {
            "id": self.id,
            "package": self.package,
            "format": self.format,
            "task_id": self.task_id,
            "state": self.state,
            "state_reason": self.state_reason,
            "module_build": self.module_id,
            "nvr": self.nvr,
        }

        try:
            # Koji is py2 only, so this fails if the main web process is
            # running on py3.
            import koji

            retval["state_name"] = koji.BUILD_STATES.get(self.state)
        except ImportError:
            pass

        return retval

    def extended_json(self, db_session, show_state_url=False, api_version=1):
        """
        :kwarg show_state_url: this will determine if `get_url_for` should be run to determine
        what the `state_url` is. This should be set to `False` when extended_json is called from
        the backend because it forces an app context to be created, which causes issues with
        SQLAlchemy sessions.
        :kwarg api_version: the API version to use when building the state URL
        """
        json = self.json(db_session)
        state_url = None
        if show_state_url:
            state_url = get_url_for("component_build", api_version=api_version, id=self.id)
        json.update({
            "batch": self.batch,
            "state_trace": [
                {
                    "time": _utc_datetime_to_iso(record.state_time),
                    "state": record.state,
                    "state_name": INVERSE_BUILD_STATES.get(record.state),
                    "reason": record.state_reason,
                }
                for record in self.state_trace(db_session)
            ],
            "state_url": state_url,
        })

        return json

    def log_message(self, session, message):
        log.info(message)
        log_msg = LogMessage(
            component_build_id=self.id,
            module_build_id=self.module_id,
            message=message,
            time_created=datetime.utcnow(),
        )

        session.add(log_msg)
        session.commit()

    def __repr__(self):
        return "<ComponentBuild %s, %r, state: %r, task_id: %r, batch: %r, state_reason: %s>" % (
            self.package,
            self.module_id,
            self.state,
            self.task_id,
            self.batch,
            self.state_reason,
        )

    @property
    def is_completed(self):
        return self.state == koji.BUILD_STATES["COMPLETE"]

    @property
    def is_building(self):
        return self.state == koji.BUILD_STATES["BUILDING"]

    @property
    def is_failed(self):
        return self.state == koji.BUILD_STATES["FAILED"]

    @property
    def is_waiting_for_build(self):
        return self.state is None

    @property
    def is_unbuilt(self):
        return self.is_waiting_for_build or self.is_building

    @property
    def is_tagged(self):
        return self.tagged and (self.tagged_in_final or self.build_time_only)

    @property
    def is_unsuccessful(self):
        return (
            self.state == koji.BUILD_STATES["FAILED"]
            or self.state == koji.BUILD_STATES["CANCELED"]
        )


class ComponentBuildTrace(MBSBase):
    __tablename__ = "component_builds_trace"
    id = db.Column(db.Integer, primary_key=True)
    component_id = db.Column(db.Integer, db.ForeignKey("component_builds.id"), nullable=False)
    state_time = db.Column(db.DateTime, nullable=False)
    state = db.Column(db.Integer, nullable=True)
    state_reason = db.Column(db.String, nullable=True)
    task_id = db.Column(db.Integer, nullable=True)

    component_build = db.relationship(
        "ComponentBuild", backref="component_builds_trace", lazy=False
    )

    def json(self, db_session):
        retval = {
            "id": self.id,
            "component_id": self.component_id,
            "state_time": _utc_datetime_to_iso(self.state_time),
            "state": self.state,
            "state_reason": self.state_reason,
            "task_id": self.task_id,
        }

        return retval

    def __repr__(self):
        return (
            "<ComponentBuildTrace %s, component_id: %s, state_time: %r, state: %s,"
            " state_reason: %s, task_id: %s>"
        ) % (
            self.id,
            self.component_id,
            self.state_time,
            self.state,
            self.state_reason,
            self.task_id,
        )


class LogMessage(MBSBase):
    __tablename__ = "log_messages"
    id = db.Column(db.Integer, primary_key=True)
    component_build_id = db.Column(db.Integer, db.ForeignKey("component_builds.id"), nullable=True)
    module_build_id = db.Column(db.Integer, db.ForeignKey("module_builds.id"), nullable=False)
    message = db.Column(db.String, nullable=False)
    time_created = db.Column(db.DateTime, nullable=False)

    def json(self):
        retval = {
            "id": self.id,
            "time_created": _utc_datetime_to_iso(self.time_created),
            "message": self.message,
        }

        return retval

    def __repr__(self):
        return (
            "<LogMessage %s, component_build_id: %s, module_build_id: %r, message: %s,"
            " time_created: %s>"
        ) % (
            self.id,
            self.component_build_id,
            self.module_build_id,
            self.message,
            self.time_created,
        )


def session_before_commit_handlers(session):
    # new and updated items
    for item in set(session.new) | set(session.dirty):
        # handlers for component builds
        if isinstance(item, ComponentBuild):
            cbt = ComponentBuildTrace(
                state_time=datetime.utcnow(),
                state=item.state,
                state_reason=item.state_reason,
                task_id=item.task_id,
            )
            # To fully support append, the hook must be tied to the session
            item.component_builds_trace.append(cbt)


@sqlalchemy.event.listens_for(ModuleBuild, "before_insert")
@sqlalchemy.event.listens_for(ModuleBuild, "before_update")
def new_and_update_module_handler(mapper, db_session, target):
    # Only modify time_modified if it wasn't explicitly set
    if not db.inspect(target).get_history("time_modified", True).has_changes():
        target.time_modified = datetime.utcnow()


def send_message_after_module_build_state_change(db_session):
    """Hook of SQLAlchemy ORM event after_commit to send messages"""
    queue = module_build_state_change_out_queue
    # Generally, the changes will be committed immediately after
    # ModuleBuild.transition is called. In this case, queue should have only
    # one message body to be sent. This while loop also ensures that messages
    # are sent correctly if the commit happens after more than one call of
    # ModuleBuild.transition.
    while not queue.empty():
        notify_on_module_state_change(queue.get())
