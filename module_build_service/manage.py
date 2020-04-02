# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import, print_function
from functools import wraps
import getpass
import logging
import os
import textwrap

import flask_migrate
from flask_script import Manager, prompt_bool
from werkzeug.datastructures import FileStorage

from module_build_service import app, db
from module_build_service.builder.MockModuleBuilder import (
    import_builds_from_local_dnf_repos, load_local_builds
)
from module_build_service.common import conf, models
from module_build_service.common.errors import StreamAmbigous
from module_build_service.common.logger import level_flags
from module_build_service.common.utils import load_mmd_file, import_mmd
import module_build_service.scheduler.consumer
from module_build_service.scheduler.db_session import db_session
import module_build_service.scheduler.local
from module_build_service.web.submit import submit_module_build_from_yaml


def create_app(debug=False, verbose=False, quiet=False):
    # logging (intended for flask-script, see manage.py)
    log = logging.getLogger(__name__)
    if debug:
        log.setLevel(level_flags["debug"])
    elif verbose:
        log.setLevel(level_flags["verbose"])
    elif quiet:
        log.setLevel(level_flags["quiet"])

    return app


manager = Manager(create_app)
help_args = ("-?", "--help")
manager.help_args = help_args
migrations_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                              'migrations')
migrate = flask_migrate.Migrate(app, db, directory=migrations_dir)
manager.add_command("db", flask_migrate.MigrateCommand)
manager.add_option("-d", "--debug", dest="debug", action="store_true")
manager.add_option("-v", "--verbose", dest="verbose", action="store_true")
manager.add_option("-q", "--quiet", dest="quiet", action="store_true")


def console_script_help(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        import sys

        if any([arg in help_args for arg in sys.argv[1:]]):
            command = os.path.basename(sys.argv[0])
            print(textwrap.dedent(
                """\
                    {0}

                    Usage: {0} [{1}]

                    See also:
                    mbs-manager(1)
                """).strip().format(command, "|".join(help_args))
            )
            sys.exit(2)
        r = f(*args, **kwargs)
        return r

    return wrapped


@console_script_help
@manager.command
def upgradedb():
    """ Upgrades the database schema to the latest revision
    """
    app.config["SERVER_NAME"] = "localhost"
    # TODO: configurable?
    migrations_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "migrations")
    with app.app_context():
        flask_migrate.upgrade(directory=migrations_dir)


@manager.command
def cleardb():
    """ Clears the database
    """
    models.ModuleBuild.query.delete()
    models.ComponentBuild.query.delete()


@console_script_help
@manager.command
def import_module(mmd_file):
    """ Imports the module from mmd_file
    """
    mmd = load_mmd_file(mmd_file)
    import_mmd(db.session, mmd)


@manager.option("--stream", action="store", dest="stream")
@manager.option("--file", action="store", dest="yaml_file")
@manager.option("--srpm", action="append", default=[], dest="srpms", metavar="SRPM")
@manager.option("--skiptests", action="store_true", dest="skiptests")
@manager.option("--offline", action="store_true", dest="offline")
@manager.option("-d", "--debug", action="store_true", dest="log_debug")
@manager.option("-l", "--add-local-build", action="append", default=None, dest="local_build_nsvs")
@manager.option("-s", "--set-stream", action="append", default=[], dest="default_streams")
@manager.option(
    "-r", "--platform-repo-file", action="append", default=[], dest="platform_repofiles"
)
@manager.option("-p", "--platform-id", action="store", default=None, dest="platform_id")
def build_module_locally(
    local_build_nsvs=None,
    yaml_file=None,
    srpms=None,
    stream=None,
    skiptests=False,
    default_streams=None,
    offline=False,
    platform_repofiles=None,
    platform_id=None,
    log_debug=False,
):
    """ Performs local module build using Mock
    """
    # if debug is not specified, set log level of console to INFO
    if not log_debug:
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.INFO)

    if "SERVER_NAME" not in app.config or not app.config["SERVER_NAME"]:
        app.config["SERVER_NAME"] = "localhost"

        if conf.resolver == "db":
            raise ValueError(
                "Please set RESOLVER to 'mbs' in your configuration for local builds.")

    conf.set_item("system", "mock")
    conf.set_item("base_module_repofiles", platform_repofiles)

    # Use our own local SQLite3 database.
    confdir = os.path.abspath(os.getcwd())
    dbdir = \
        os.path.abspath(os.path.join(confdir, "..")) if confdir.endswith("conf") else confdir
    dbpath = "/{0}".format(os.path.join(dbdir, ".mbs_local_build.db"))
    dburi = "sqlite://" + dbpath
    app.config["SQLALCHEMY_DATABASE_URI"] = dburi
    conf.set_item("sqlalchemy_database_uri", dburi)
    if os.path.exists(dbpath):
        os.remove(dbpath)

    db.create_all()
    # Reconfigure the backend database session registry to use the new the database location
    db_session.remove()
    db_session.configure(bind=db.session.bind)

    params = {
        "local_build": True,
        "default_streams": dict(ns.split(":") for ns in default_streams)
    }
    if srpms:
        params["srpms"] = srpms

    username = getpass.getuser()
    if not yaml_file or not yaml_file.endswith(".yaml"):
        raise IOError("Provided modulemd file is not a yaml file.")

    yaml_file_path = os.path.abspath(yaml_file)

    if offline:
        import_builds_from_local_dnf_repos(platform_id)
    load_local_builds(local_build_nsvs)

    with open(yaml_file_path) as fd:
        filename = os.path.basename(yaml_file)
        handle = FileStorage(fd)
        handle.filename = filename
        try:
            module_builds = submit_module_build_from_yaml(
                db_session, username, handle, params,
                stream=str(stream), skiptests=skiptests
            )
        except StreamAmbigous as e:
            logging.error(str(e))
            logging.error("Use '-s module_name:module_stream' to choose the stream")
            return

        module_build_ids = [build.id for build in module_builds]

    module_build_service.scheduler.local.main(module_build_ids)

    has_failed_module = db_session.query(models.ModuleBuild).filter(
        models.ModuleBuild.id.in_(module_build_ids),
        models.ModuleBuild.state == models.BUILD_STATES["failed"],
    ).count() > 0

    if has_failed_module:
        raise RuntimeError("Module build failed")


@manager.option(
    "identifier",
    metavar="NAME:STREAM[:VERSION[:CONTEXT]]",
    help="Identifier for selecting module builds to retire",
)
@manager.option(
    "--confirm",
    action="store_true",
    default=False,
    help="Perform retire operation without prompting",
)
def retire(identifier, confirm=False):
    """ Retire module build(s) by placing them into 'garbage' state.
    """
    # Parse identifier and build query
    parts = identifier.split(":")
    if len(parts) < 2:
        raise ValueError("Identifier must contain at least NAME:STREAM")
    if len(parts) >= 5:
        raise ValueError("Too many parts in identifier")

    filter_by_kwargs = {"state": models.BUILD_STATES["ready"], "name": parts[0], "stream": parts[1]}

    if len(parts) >= 3:
        filter_by_kwargs["version"] = parts[2]
    if len(parts) >= 4:
        filter_by_kwargs["context"] = parts[3]

    # Find module builds to retire
    module_builds = db_session.query(models.ModuleBuild).filter_by(**filter_by_kwargs).all()

    if not module_builds:
        logging.info("No module builds found.")
        return

    logging.info("Found %d module builds:", len(module_builds))
    for build in module_builds:
        logging.info("\t%s", ":".join((build.name, build.stream, build.version, build.context)))

    # Prompt for confirmation
    is_confirmed = confirm or prompt_bool("Retire {} module builds?".format(len(module_builds)))
    if not is_confirmed:
        logging.info("Module builds were NOT retired.")
        return

    # Retire module builds
    for build in module_builds:
        build.transition(
            db_session, conf, models.BUILD_STATES["garbage"], "Module build retired")

    db_session.commit()

    logging.info("Module builds retired.")


@console_script_help
@manager.command
def run(host=None, port=None, debug=None):
    """ Runs the Flask app, locally.
    """
    host = host or conf.host
    port = port or conf.port
    debug = debug or conf.debug

    logging.info("Starting Module Build Service frontend")

    app.run(host=host, port=port, debug=debug)


def manager_wrapper():
    manager.run()


if __name__ == "__main__":
    manager_wrapper()
