# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

import kobo.rpmlib

from module_build_service.common import log, conf, models
from module_build_service.common.resolve import get_base_module_mmds
from module_build_service.resolver import GenericResolver
from module_build_service.scheduler import events
from module_build_service.scheduler.db_session import db_session


def reuse_component(component, previous_component_build, change_state_now=False,
                    schedule_fake_events=True):
    """
    Reuses component build `previous_component_build` instead of building
    component `component`

    Please remember to commit the changes where the function is called.
    This allows callers to reuse multiple component builds and commit them all
    at once.

    :param ComponentBuild component: Component whihch will reuse previous module build.
    :param ComponentBuild previous_component_build: Previous component build to reuse.
    :param bool change_state_now: When True, the component.state will be set to
        previous_component_build.state. Otherwise, the component.state will be set to BUILDING.
    :param bool schedule_fake_events: When True, the `events.scheduler.add` will be used to
        schedule handlers.component.build_task_finalize handler call.
    """

    import koji
    from module_build_service.scheduler.handlers.components import (
        build_task_finalize as build_task_finalize_handler)

    log.info(
        'Reusing component "{0}" from a previous module '
        'build with the nvr "{1}"'.format(component.package, previous_component_build.nvr)
    )
    component.reused_component_id = previous_component_build.id
    component.task_id = previous_component_build.task_id
    if change_state_now:
        component.state = previous_component_build.state
    else:
        # Use BUILDING state here, because we want the state to change to
        # COMPLETE by scheduling a internal buildsys.build.state.change message
        # we are generating few lines below.
        # If we would set it to the right state right here, we would miss the
        # code path handling that event which works only when switching from
        # BUILDING to COMPLETE.
        component.state = koji.BUILD_STATES["BUILDING"]
    component.state_reason = "Reused component from previous module build"
    component.nvr = previous_component_build.nvr
    nvr_dict = kobo.rpmlib.parse_nvr(component.nvr)
    # Add this event to scheduler so that the reused component will be tagged properly.
    if schedule_fake_events:
        args = (
            "reuse_component: fake msg", component.task_id, previous_component_build.state,
            nvr_dict["name"], nvr_dict["version"], nvr_dict["release"], component.module_id,
            component.state_reason)
        events.scheduler.add(build_task_finalize_handler, args)


def get_reusable_module(module):
    """
    Returns previous module build of the module `module` in case it can be
    used as a source module to get the components to reuse from.

    In case there is no such module, returns None.

    :param module: the ModuleBuild object of module being built.
    :return: ModuleBuild object which can be used for component reuse.
    """

    if module.reused_module:
        return module.reused_module

    mmd = module.mmd()
    previous_module_build = None

    # The `base_mmds` will contain the list of base modules against which the possible modules
    # to reuse are built. There are three options how these base modules are found:
    #
    # 1) The `conf.allow_only_compatible_base_modules` is False. This means that MBS should
    #    not try to find any compatible base modules in its DB and simply use the buildrequired
    #    base module as it is.
    # 2) The `conf.allow_only_compatible_base_modules` is True and DBResolver is used. This means
    #    that MBS should try to find the compatible modules using its database.
    #    The `get_base_module_mmds` finds out the list of compatible modules and returns mmds of
    #    all of them.
    # 3) The `conf.allow_only_compatible_base_modules` is True and KojiResolver is used. This
    #    means that MBS should *not* try to find any compatible base modules in its DB, but
    #    instead just query Koji using KojiResolver later to find out the module to
    #    reuse. The list of compatible base modules is defined by Koji tag inheritance directly
    #    in Koji.
    #    The `get_base_module_mmds` in this case returns just the buildrequired base module.
    if conf.allow_only_compatible_base_modules:
        log.debug("Checking for compatible base modules")
        base_mmds = get_base_module_mmds(db_session, mmd)["ready"]
        # Sort the base_mmds based on the stream version, higher version first.
        base_mmds.sort(
            key=lambda mmd: models.ModuleBuild.get_stream_version(mmd.get_stream_name(), False),
            reverse=True)
    else:
        log.debug("Skipping the check for compatible base modules")
        base_mmds = []
        for br in module.buildrequires:
            if br.name in conf.base_module_names:
                base_mmds.append(br.mmd())

    for base_mmd in base_mmds:
        previous_module_build = (
            db_session.query(models.ModuleBuild)
                      .filter_by(name=mmd.get_module_name())
                      .filter_by(stream=mmd.get_stream_name())
                      .filter_by(state=models.BUILD_STATES["ready"])
                      .filter(models.ModuleBuild.scmurl.isnot(None))
                      .order_by(models.ModuleBuild.time_completed.desc()))

        koji_resolver_enabled = base_mmd.get_xmd().get("mbs", {}).get("koji_tag_with_modules")
        if koji_resolver_enabled:
            # Find ModuleBuilds tagged in the Koji tag using KojiResolver.
            resolver = GenericResolver.create(db_session, conf, backend="koji")
            possible_modules_to_reuse = resolver.get_buildrequired_modules(
                module.name, module.stream, base_mmd)

            # Limit the query to these modules.
            possible_module_ids = [m.id for m in possible_modules_to_reuse]
            previous_module_build = previous_module_build.filter(
                models.ModuleBuild.id.in_(possible_module_ids))

            # Limit the query to modules sharing the same `build_context_no_bms`. That means they
            # have the same buildrequirements.
            previous_module_build = previous_module_build.filter_by(
                build_context_no_bms=module.build_context_no_bms)
        else:
            # Recompute the build_context with compatible base module stream.
            mbs_xmd = mmd.get_xmd()["mbs"]
            if base_mmd.get_module_name() not in mbs_xmd["buildrequires"]:
                previous_module_build = None
                continue
            mbs_xmd["buildrequires"][base_mmd.get_module_name()]["stream"] \
                = base_mmd.get_stream_name()
            build_context = module.calculate_build_context(mbs_xmd["buildrequires"])

            # Limit the query to find only modules sharing the same build_context.
            previous_module_build = previous_module_build.filter_by(build_context=build_context)

        # If we are rebuilding with the "changed-and-after" option, then we can't reuse
        # components from modules that were built more liberally
        if module.rebuild_strategy == "changed-and-after":
            previous_module_build = previous_module_build.filter(
                models.ModuleBuild.rebuild_strategy.in_(["all", "changed-and-after"])
            )

        previous_module_build = previous_module_build.first()

        if previous_module_build:
            break

    # The component can't be reused if there isn't a previous build in the done
    # or ready state
    if not previous_module_build:
        log.info("Cannot re-use.  %r is the first module build." % module)
        return None

    module.reused_module_id = previous_module_build.id
    db_session.commit()

    return previous_module_build


def attempt_to_reuse_all_components(builder, module):
    """
    Tries to reuse all the components in a build. The components are also
    tagged to the tags using the `builder`.

    Returns True if all components could be reused, otherwise False. When
    False is returned, no component has been reused.
    """

    previous_module_build = get_reusable_module(module)
    if not previous_module_build:
        return False

    mmd = module.mmd()
    old_mmd = previous_module_build.mmd()

    # [(component, component_to_reuse), ...]
    component_pairs = []

    # Find out if we can reuse all components and cache component and
    # component to reuse pairs.
    for c in module.component_builds:
        if c.package == "module-build-macros":
            continue
        component_to_reuse = get_reusable_component(
            module,
            c.package,
            previous_module_build=previous_module_build,
            mmd=mmd,
            old_mmd=old_mmd,
        )
        if not component_to_reuse:
            return False

        component_pairs.append((c, component_to_reuse))

    # Stores components we will tag to buildroot and final tag.
    components_to_tag = []

    # Reuse all components.
    for c, component_to_reuse in component_pairs:
        # Set the module.batch to the last batch we have.
        if c.batch > module.batch:
            module.batch = c.batch

        # Reuse the component
        reuse_component(c, component_to_reuse, True, False)
        components_to_tag.append(c.nvr)

    # Tag them
    builder.buildroot_add_artifacts(components_to_tag, install=False)
    builder.tag_artifacts(components_to_tag, dest_tag=True)

    return True


def get_reusable_components(module, component_names, previous_module_build=None):
    """
    Returns the list of ComponentBuild instances belonging to previous module
    build which can be reused in the build of module `module`.

    The ComponentBuild instances in returned list are in the same order as
    their names in the component_names input list.

    In case some component cannot be reused, None is used instead of a
    ComponentBuild instance in the returned list.

    :param module: the ModuleBuild object of module being built.
    :param component_names: List of component names to be reused.
    :kwarg previous_module_build: the ModuleBuild instance of a module build
        which contains the components to reuse. If not passed, get_reusable_module
        is called to get the ModuleBuild instance.
    :return: List of ComponentBuild instances to reuse in the same
             order as `component_names`
    """
    # We support components reusing only for koji and test backend.
    if conf.system not in ["koji", "test"]:
        return [None] * len(component_names)

    if not previous_module_build:
        previous_module_build = get_reusable_module(module)
    if not previous_module_build:
        return [None] * len(component_names)

    mmd = module.mmd()
    old_mmd = previous_module_build.mmd()

    ret = []
    for component_name in component_names:
        ret.append(
            get_reusable_component(
                module, component_name, previous_module_build, mmd, old_mmd)
        )

    return ret


def get_reusable_component(
    module, component_name, previous_module_build=None, mmd=None, old_mmd=None
):
    """
    Returns the component (RPM) build of a module that can be reused
    instead of needing to rebuild it

    :param module: the ModuleBuild object of module being built with a formatted
        mmd
    :param component_name: the name of the component (RPM) that you'd like to
        reuse a previous build of
    :param previous_module_build: the ModuleBuild instances of a module build
        which contains the components to reuse. If not passed, get_reusable_module
        is called to get the ModuleBuild instance. Consider passing the ModuleBuild
        instance in case you plan to call get_reusable_component repeatedly for the
        same module to make this method faster.
    :param mmd: Modulemd.ModuleStream of `module`. If not passed, it is taken from
        module.mmd(). Consider passing this arg in case you plan to call
        get_reusable_component repeatedly for the same module to make this method faster.
    :param old_mmd: Modulemd.ModuleStream of `previous_module_build`. If not passed,
        it is taken from previous_module_build.mmd(). Consider passing this arg in
        case you plan to call get_reusable_component repeatedly for the same
        module to make this method faster.
    :return: the component (RPM) build SQLAlchemy object, if one is not found,
        None is returned
    """

    # We support component reusing only for koji and test backend.
    if conf.system not in ["koji", "test"]:
        return None

    # If the rebuild strategy is "all", that means that nothing can be reused
    if module.rebuild_strategy == "all":
        message = ("Cannot reuse the component {component_name} because the module "
                   "rebuild strategy is \"all\".").format(
                       component_name=component_name)
        module.log_message(db_session, message)
        return None

    if not previous_module_build:
        previous_module_build = get_reusable_module(module)
        if not previous_module_build:
            message = ("Cannot reuse because no previous build of "
                       "module {module_name} found!").format(
                module_name=module.name)
            module.log_message(db_session, message)
            return None

    if not mmd:
        mmd = module.mmd()
    if not old_mmd:
        old_mmd = previous_module_build.mmd()

    # If the chosen component for some reason was not found in the database,
    # or the ref is missing, something has gone wrong and the component cannot
    # be reused
    new_module_build_component = models.ComponentBuild.from_component_name(
        db_session, component_name, module.id)
    if (
        not new_module_build_component
        or not new_module_build_component.batch
        or not new_module_build_component.ref
    ):
        message = ("Cannot reuse the component {} because it can't be found in the "
                   "database").format(component_name)
        module.log_message(db_session, message)
        return None

    prev_module_build_component = models.ComponentBuild.from_component_name(
        db_session, component_name, previous_module_build.id
    )
    # If the component to reuse for some reason was not found in the database,
    # or the ref is missing, something has gone wrong and the component cannot
    # be reused
    if (
        not prev_module_build_component
        or not prev_module_build_component.batch
        or not prev_module_build_component.ref
    ):
        message = ("Cannot reuse the component {} because a previous build of "
                   "it can't be found in the database").format(component_name)
        new_module_build_component.log_message(db_session, message)
        return None

    # Make sure the ref for the component that is trying to be reused
    # hasn't changed since the last build
    if prev_module_build_component.ref != new_module_build_component.ref:
        message = ("Cannot reuse the component because the commit hash changed"
                   " since the last build")
        new_module_build_component.log_message(db_session, message)
        return None

    # At this point we've determined that both module builds contain the component
    # and the components share the same commit hash
    if module.rebuild_strategy == "changed-and-after":
        # Make sure the batch number for the component that is trying to be reused
        # hasn't changed since the last build
        if prev_module_build_component.batch != new_module_build_component.batch:
            message = ("Cannot reuse the component because it is being built in "
                       "a different batch than in the compatible module build")
            new_module_build_component.log_message(db_session, message)
            return None

        # If the mmd.buildopts.macros.rpms changed, we cannot reuse
        buildopts = mmd.get_buildopts()
        if buildopts:
            modulemd_macros = buildopts.get_rpm_macros()
        else:
            modulemd_macros = None

        old_buildopts = old_mmd.get_buildopts()
        if old_buildopts:
            old_modulemd_macros = old_buildopts.get_rpm_macros()
        else:
            old_modulemd_macros = None

        if modulemd_macros != old_modulemd_macros:
            message = ("Cannot reuse the component because the modulemd's macros are"
                       " different than those of the compatible module build")
            new_module_build_component.log_message(db_session, message)
            return None

        # At this point we've determined that both module builds contain the component
        # with the same commit hash and they are in the same batch. We've also determined
        # that both module builds depend(ed) on the same exact module builds. Now it's time
        # to determine if the components before it have changed.
        #
        # Convert the component_builds to a list and sort them by batch
        new_component_builds = list(module.component_builds)
        new_component_builds.sort(key=lambda x: x.batch)
        prev_component_builds = list(previous_module_build.component_builds)
        prev_component_builds.sort(key=lambda x: x.batch)

        new_module_build_components = []
        previous_module_build_components = []
        # Create separate lists for the new and previous module build. These lists
        # will have an entry for every build batch *before* the component's
        # batch except for 1, which is reserved for the module-build-macros RPM.
        # Each batch entry will contain a set of "(name, ref, arches)" with the name,
        # ref (commit), and arches of the component.
        for i in range(new_module_build_component.batch - 1):
            # This is the first batch which we want to skip since it will always
            # contain only the module-build-macros RPM and it gets built every time
            if i == 0:
                continue

            new_module_build_components.append({
                (value.package, value.ref,
                    tuple(sorted(mmd.get_rpm_component(value.package).get_arches())))
                for value in new_component_builds
                if value.batch == i + 1
            })

            previous_module_build_components.append({
                (value.package, value.ref,
                    tuple(sorted(old_mmd.get_rpm_component(value.package).get_arches())))
                for value in prev_component_builds
                if value.batch == i + 1
            })

        # If the previous batches don't have the same ordering, hashes, and arches, then the
        # component can't be reused
        if previous_module_build_components != new_module_build_components:
            message = ("Cannot reuse the component because a component in a previous"
                       " batch has been added, removed, or rebuilt")
            new_module_build_component.log_message(db_session, message)
            return None

    # check that arches have not changed
    pkg = mmd.get_rpm_component(component_name)
    if set(pkg.get_arches()) != set(old_mmd.get_rpm_component(component_name).get_arches()):
        message = ("Cannot reuse the component because its architectures"
                   " have changed since the compatible module build").format(component_name)
        new_module_build_component.log_message(db_session, message)
        return None

    reusable_component = db_session.query(models.ComponentBuild).filter_by(
        package=component_name, module_id=previous_module_build.id).one()
    log.debug("Found reusable component!")
    return reusable_component
