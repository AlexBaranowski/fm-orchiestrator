# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" The module build orchestrator for Modularity, API.
This is the implementation of the orchestrator's public RESTful API.
"""

from __future__ import absolute_import
from io import BytesIO
import json
import sqlalchemy.event

from flask import request, url_for, Blueprint, Response
from flask.views import MethodView
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from six import string_types

from module_build_service import app, db, version, api_version as max_api_version
from module_build_service.common import conf, log, models
from module_build_service.common.errors import (
    ValidationError, Forbidden, NotFound, ProgrammingError,
    Unauthorized, UnprocessableEntity, Conflict
)
from module_build_service.common.models import send_message_after_module_build_state_change
from module_build_service.common.monitor import registry
from module_build_service.common.submit import fetch_mmd
from module_build_service.common.utils import import_mmd
import module_build_service.web.auth
from module_build_service.web.backports import jsonify
from module_build_service.web.submit import (
    submit_module_build_from_scm, submit_module_build_from_yaml
)
from module_build_service.web.utils import (
    cors_header,
    filter_component_builds,
    filter_module_builds,
    get_scm_url_re,
    pagination_metadata,
    str_to_bool,
    validate_api_version,
)


api_routes = {
    "module_builds": {
        "url": "/module-build-service/<int:api_version>/module-builds/",
        "options": {"methods": ["POST"]},
    },
    "module_builds_list": {
        "url": "/module-build-service/<int:api_version>/module-builds/",
        "options": {"defaults": {"id": None}, "methods": ["GET"]},
    },
    "module_build": {
        "url": "/module-build-service/<int:api_version>/module-builds/<int:id>",
        "options": {"methods": ["GET", "PATCH"]},
    },
    "component_builds_list": {
        "url": "/module-build-service/<int:api_version>/component-builds/",
        "options": {"defaults": {"id": None}, "methods": ["GET"]},
    },
    "component_build": {
        "url": "/module-build-service/<int:api_version>/component-builds/<int:id>",
        "options": {"methods": ["GET"]},
    },
    "about": {
        "url": "/module-build-service/<int:api_version>/about/",
        "options": {"methods": ["GET"]},
    },
    "rebuild_strategies_list": {
        "url": "/module-build-service/<int:api_version>/rebuild-strategies/",
        "options": {"methods": ["GET"]},
    },
    "import_module": {
        "url": "/module-build-service/<int:api_version>/import-module/",
        "options": {"methods": ["POST"]},
    },
    "log_messages_module_build": {
        "url": "/module-build-service/<int:api_version>/module-builds/<int:id>/messages",
        "options": {"methods": ["GET"], "defaults": {"model": models.ModuleBuild}},
    },
    "log_messages_component_build": {
        "url": "/module-build-service/<int:api_version>/component-builds/<int:id>/messages",
        "options": {"methods": ["GET"], "defaults": {"model": models.ComponentBuild}},
    },
}


class AbstractQueryableBuildAPI(MethodView):
    """ An abstract class, housing some common functionality. """

    @cors_header()
    @validate_api_version()
    def get(self, api_version, id):
        id_flag = request.args.get("id")
        if id_flag:
            endpoint = request.endpoint.split("s_list")[0]
            raise ValidationError(
                'The "id" query option is invalid. Did you mean to go to "{0}"?'.format(
                    url_for(endpoint, api_version=api_version, id=id_flag)
                )
            )
        verbose_flag = request.args.get("verbose", "false").lower()
        short_flag = request.args.get("short", "false").lower()
        json_func_kwargs = {}
        json_func_name = "json"

        if id is None:
            # Lists all tracked builds
            p_query = self.query_filter(request)
            json_data = {"meta": pagination_metadata(p_query, api_version, request.args)}

            if verbose_flag == "true" or verbose_flag == "1":
                json_func_name = "extended_json"
                json_func_kwargs["show_state_url"] = True
                json_func_kwargs["api_version"] = api_version
            elif short_flag == "true" or short_flag == "1":
                if p_query.items and hasattr(p_query.items[0], "short_json"):
                    json_func_name = "short_json"
            if json_func_name == "json" or json_func_name == "extended_json":
                # Only ModuleBuild.json and ModuleBuild.extended_json has argument db_session
                json_func_kwargs["db_session"] = db.session
            json_data["items"] = [
                getattr(item, json_func_name)(**json_func_kwargs) for item in p_query.items
            ]

            return jsonify(json_data), 200
        else:
            # Lists details for the specified build
            instance = self.model.query.filter_by(id=id).first()
            if instance:
                if verbose_flag == "true" or verbose_flag == "1":
                    json_func_name = "extended_json"
                    json_func_kwargs["show_state_url"] = True
                    json_func_kwargs["api_version"] = api_version
                elif short_flag == "true" or short_flag == "1":
                    if getattr(instance, "short_json", None):
                        json_func_name = "short_json"
                if json_func_name == "json" or json_func_name == "extended_json":
                    # Only ModuleBuild.json and ModuleBuild.extended_json has argument db_session
                    json_func_kwargs["db_session"] = db.session
                return jsonify(getattr(instance, json_func_name)(**json_func_kwargs)), 200
            else:
                raise NotFound("No such %s found." % self.kind)


class ComponentBuildAPI(AbstractQueryableBuildAPI):
    kind = "component"
    query_filter = staticmethod(filter_component_builds)
    model = models.ComponentBuild


class ModuleBuildAPI(AbstractQueryableBuildAPI):
    kind = "module"
    query_filter = staticmethod(filter_module_builds)
    model = models.ModuleBuild

    @staticmethod
    def check_groups(username, groups, allowed_groups=conf.allowed_groups):
        # If the user is part of the whitelist, then the group membership check is skipped
        if username in conf.allowed_users:
            return
        if allowed_groups and not (allowed_groups & groups):
            raise Forbidden("%s is not in any of %r, only %r" % (username, allowed_groups, groups))

    # Additional POST and DELETE handlers for modules follow.
    @validate_api_version()
    def post(self, api_version):
        data = _dict_from_request(request)
        if "modulemd" in data or (hasattr(request, "files") and "yaml" in request.files):
            handler = YAMLFileHandler(request, data)
        else:
            handler = SCMHandler(request, data)

        if conf.no_auth is True and handler.username == "anonymous" and "owner" in handler.data:
            handler.username = handler.data["owner"]

        self.check_groups(handler.username, handler.groups)

        handler.validate()
        modules = handler.post()
        if api_version == 1:
            # Only show the first module build for backwards-compatibility
            rv = modules[0].extended_json(db.session, True, api_version)
        else:
            rv = [module.extended_json(db.session, True, api_version) for module in modules]
        return jsonify(rv), 201

    @validate_api_version()
    def patch(self, api_version, id):
        username, groups = module_build_service.web.auth.get_user(request)

        try:
            r = json.loads(request.get_data().decode("utf-8"))
        except Exception:
            log.exception("Invalid JSON submitted")
            raise ValidationError("Invalid JSON submitted")

        if "owner" in r:
            if conf.no_auth is not True:
                raise ValidationError(
                    "The request contains 'owner' parameter, however NO_AUTH is not allowed"
                )
            elif username == "anonymous":
                username = r["owner"]

        self.check_groups(username, groups)

        module = models.ModuleBuild.query.filter_by(id=id).first()
        if not module:
            raise NotFound("No such module found.")

        if module.owner != username and not (conf.admin_groups & groups):
            raise Forbidden("You are not owner of this build and therefore cannot modify it.")

        if not r.get("state"):
            log.error("Invalid JSON submitted")
            raise ValidationError("Invalid JSON submitted")

        state = r["state"]
        valid_input_states = ("failed", str(models.BUILD_STATES["failed"]))
        if state not in valid_input_states:
            raise ValidationError(
                "An invalid state was submitted. Valid states values are: {}"
                .format(", ".join(valid_input_states))
            )

        valid_states_to_cancel = ("build", "init", "wait")
        module_state_name = models.INVERSE_BUILD_STATES[module.state]
        if module_state_name not in valid_states_to_cancel:
            log.error(
                "The user %s attempted to cancel a build in the %s state",
                username, module_state_name,
            )
            raise ValidationError(
                "To cancel a module build, it must be in one of the following states: {}"
                .format(", ".join(valid_states_to_cancel))
            )

        module.transition(
            db.session, conf, models.BUILD_STATES["failed"], "Canceled by %s." % username)
        db.session.add(module)
        db.session.commit()

        return jsonify(module.extended_json(db.session, True, api_version)), 200


class AboutAPI(MethodView):
    @cors_header()
    @validate_api_version()
    def get(self, api_version):
        json = {"version": version, "api_version": max_api_version}
        config_items = ["auth_method"]
        for item in config_items:
            config_item = getattr(conf, item)
            # All config items have a default, so if doesn't exist it is a programming error
            if not config_item:
                raise ProgrammingError('An invalid config item of "{0}" was specified'.format(item))
            json[item] = config_item
        return jsonify(json), 200


class RebuildStrategies(MethodView):
    @cors_header()
    @validate_api_version()
    def get(self, api_version):
        items = []
        # Sort the items list by name
        for strategy in sorted(models.ModuleBuild.rebuild_strategies.keys()):
            default = False
            if strategy == conf.rebuild_strategy:
                default = True
                allowed = True
            elif (
                conf.rebuild_strategy_allow_override and strategy in conf.rebuild_strategies_allowed
            ):
                allowed = True
            else:
                allowed = False
            items.append({
                "name": strategy,
                "description": models.ModuleBuild.rebuild_strategies[strategy],
                "allowed": allowed,
                "default": default,
            })

        return jsonify({"items": items}), 200


class ImportModuleAPI(MethodView):
    @validate_api_version()
    def post(self, api_version):
        # disable this API endpoint if no groups are defined
        if not conf.allowed_groups_to_import_module:
            log.error(
                "Import module API is disabled. Set 'ALLOWED_GROUPS_TO_IMPORT_MODULE'"
                " configuration value first."
            )
            raise Forbidden("Import module API is disabled.")

        # auth checks
        username, groups = module_build_service.web.auth.get_user(request)
        ModuleBuildAPI.check_groups(
            username, groups, allowed_groups=conf.allowed_groups_to_import_module)

        # process request using SCM handler
        handler = SCMHandler(request)
        handler.validate(skip_branch=True, skip_optional_params=True)

        mmd, _ = fetch_mmd(handler.data["scmurl"], mandatory_checks=False)
        build, messages = import_mmd(db.session, mmd)
        json_data = {
            "module": build.json(db.session, show_tasks=False),
            "messages": messages
        }

        # return 201 Created if we reach this point
        return jsonify(json_data), 201


class LogMessageAPI(MethodView):

    @validate_api_version()
    def get(self, api_version, id, model):

        if not model:
            raise ValidationError("Model is not set for this log messages endpoint")

        query = model.query.filter_by(id=id).first().log_messages.order_by(
            models.LogMessage.time_created.desc())

        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 10, type=int)
        p_query = query.paginate(page, per_page, False)

        request_args = {"id": id}
        json_data = {"meta": pagination_metadata(p_query, api_version, request_args)}
        json_data["messages"] = [
            getattr(message, "json")() for message in p_query.items
        ]

        return jsonify(json_data), 200


class BaseHandler(object):
    valid_params = {
        "branch",
        "buildrequire_overrides",
        "modulemd",
        "module_name",
        "module_stream",
        "owner",
        "rebuild_strategy",
        "reuse_components_from",
        "require_overrides",
        "scmurl",
        "scratch",
        "srpms",
    }

    def __init__(self, request, data=None):
        self.username, self.groups = module_build_service.web.auth.get_user(request)
        self.data = data or _dict_from_request(request)

        # canonicalize and validate scratch option
        if "scratch" in self.data and str_to_bool(str(self.data["scratch"])):
            self.data["scratch"] = True
            if conf.modules_allow_scratch is not True:
                raise Forbidden("Scratch builds are not enabled")
        else:
            self.data["scratch"] = False

        # canonicalize and validate srpms list
        if "srpms" in self.data and self.data["srpms"]:
            if not self.data["scratch"]:
                raise Forbidden("srpms may only be specified for scratch builds")
            if not isinstance(self.data["srpms"], list):
                raise ValidationError("srpms must be specified as a list")
        else:
            self.data["srpms"] = []

    def _validate_dep_overrides_format(self, key):
        """
        Validate any dependency overrides provided to the API.

        :param str key: the override key to validate
        :raises ValidationError: when the overrides are an invalid format
        """
        if not self.data.get(key):
            return
        invalid_override_msg = (
            'The "{}" parameter must be an object with the keys as module '
            "names and the values as arrays of streams".format(key)
        )
        if not isinstance(self.data[key], dict):
            raise ValidationError(invalid_override_msg)
        for streams in self.data[key].values():
            if not isinstance(streams, list):
                raise ValidationError(invalid_override_msg)
            for stream in streams:
                if not isinstance(stream, string_types):
                    raise ValidationError(invalid_override_msg)

    def validate_optional_params(self):
        forbidden_params = [k for k in self.data if k not in self.valid_params]
        if forbidden_params:
            raise ValidationError(
                "The request contains unspecified parameters: {}".format(
                    ", ".join(forbidden_params))
            )

        if not conf.no_auth and "owner" in self.data:
            raise ValidationError(
                "The request contains 'owner' parameter, however NO_AUTH is not allowed")

        if not conf.rebuild_strategy_allow_override and "rebuild_strategy" in self.data:
            raise ValidationError(
                'The request contains the "rebuild_strategy" parameter but '
                "overriding the default isn't allowed"
            )

        if "rebuild_strategy" in self.data:
            if self.data["rebuild_strategy"] not in conf.rebuild_strategies_allowed:
                raise ValidationError(
                    'The rebuild method of "{0}" is not allowed. Choose from: {1}.'.format(
                        self.data["rebuild_strategy"], ", ".join(conf.rebuild_strategies_allowed))
                )

        self._validate_dep_overrides_format("buildrequire_overrides")
        self._validate_dep_overrides_format("require_overrides")

        if "reuse_components_from" in self.data:
            if "rebuild_strategy" in self.data and self.data["rebuild_strategy"] == "all":
                raise ValidationError(
                    'You cannot specify the parameter "reuse_components_from" when the '
                    '"rebuild_strategy" parameter is set to "all"'
                )

            invalid_identifier_msg = (
                'The parameter "reuse_components_from" contains an invalid module identifier')

            if isinstance(self.data["reuse_components_from"], int):
                reuse_module = models.ModuleBuild.get_by_id(
                    db.session, self.data["reuse_components_from"])
            elif isinstance(self.data["reuse_components_from"], string_types):
                try:
                    n, s, v, c = self.data["reuse_components_from"].split(":")
                except ValueError:
                    raise ValidationError(invalid_identifier_msg)
                reuse_module = models.ModuleBuild.get_build_from_nsvc(db.session, n, s, v, c)
            else:
                raise ValidationError(invalid_identifier_msg)

            if not reuse_module:
                raise ValidationError(
                    'The module in the parameter "reuse_components_from" could not be found')

            if reuse_module.state != models.BUILD_STATES["ready"]:
                raise ValidationError(
                    'The module in the parameter "reuse_components_from" must be in the ready state'
                )

            # Normalize the value so that it simplifies any code that uses this value
            self.data["reuse_components_from"] = reuse_module.id


class SCMHandler(BaseHandler):
    def validate(self, skip_branch=False, skip_optional_params=False):
        if "scmurl" not in self.data:
            log.error("Missing scmurl")
            raise ValidationError("Missing scmurl")

        url = self.data["scmurl"]
        allowed_prefix = any(url.startswith(prefix) for prefix in conf.scmurls)
        if not conf.allow_custom_scmurls and not allowed_prefix:
            log.error("The submitted scmurl %r is not allowed" % url)
            raise Forbidden("The submitted scmurl %s is not allowed" % url)

        if not get_scm_url_re().match(url):
            log.error("The submitted scmurl %r is not valid" % url)
            raise ValidationError("The submitted scmurl %s is not valid" % url)

        if not skip_branch and "branch" not in self.data:
            log.error("Missing branch")
            raise ValidationError("Missing branch")

        if "module_name" in self.data:
            log.error("Module name override is only allowed when a YAML file is submitted")
            raise ValidationError(
                "Module name override is only allowed when a YAML file is submitted"
            )
        if "module_stream" in self.data:
            log.error("Stream name override is only allowed when a YAML file is submitted")
            raise ValidationError(
                "Stream name override is only allowed when a YAML file is submitted"
            )

        if not skip_optional_params:
            self.validate_optional_params()

    def post(self):
        return submit_module_build_from_scm(
            db.session, self.username, self.data, allow_local_url=False)


class YAMLFileHandler(BaseHandler):
    def __init__(self, request, data=None):
        super(YAMLFileHandler, self).__init__(request, data)
        if not self.data["scratch"] and not conf.yaml_submit_allowed:
            raise Forbidden("YAML submission is not enabled")

    def validate(self):
        if (
            "modulemd" not in self.data
            and (not hasattr(request, "files") or "yaml" not in request.files)
        ):
            log.error("Invalid file submitted")
            raise ValidationError("Invalid file submitted")
        self.validate_optional_params()

    def post(self):
        if "modulemd" in self.data:
            handle = BytesIO(self.data["modulemd"].encode("utf-8"))
        else:
            handle = request.files["yaml"]
        if self.data.get("module_name"):
            handle.filename = self.data["module_name"]
        stream_name = self.data.get("module_stream", None)
        return submit_module_build_from_yaml(
            db.session, self.username, handle, self.data, stream=stream_name)


def _dict_from_request(request):
    if "multipart/form-data" in request.headers.get("Content-Type", ""):
        data = request.form.to_dict()
    else:
        try:
            data = json.loads(request.get_data().decode("utf-8"))
        except Exception:
            log.exception("Invalid JSON submitted")
            raise ValidationError("Invalid JSON submitted")
    return data


monitor_api = Blueprint(
    "monitor", __name__, url_prefix="/module-build-service/<int:api_version>/monitor")


@cors_header()
@validate_api_version()
@monitor_api.route("/metrics")
def metrics(api_version):
    return Response(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)


def register_api():
    """ Registers the MBS API. """
    module_view = ModuleBuildAPI.as_view("module_builds")
    component_view = ComponentBuildAPI.as_view("component_builds")
    about_view = AboutAPI.as_view("about")
    rebuild_strategies_view = RebuildStrategies.as_view("rebuild_strategies")
    import_module = ImportModuleAPI.as_view("import_module")
    log_message = LogMessageAPI.as_view("log_messages")
    for key, val in api_routes.items():
        if key.startswith("component_build"):
            app.add_url_rule(val["url"], endpoint=key, view_func=component_view, **val["options"])
        elif key.startswith("module_build"):
            app.add_url_rule(val["url"], endpoint=key, view_func=module_view, **val["options"])
        elif key.startswith("about"):
            app.add_url_rule(val["url"], endpoint=key, view_func=about_view, **val["options"])
        elif key == "rebuild_strategies_list":
            app.add_url_rule(
                val["url"], endpoint=key, view_func=rebuild_strategies_view, **val["options"]
            )
        elif key == "import_module":
            app.add_url_rule(val["url"], endpoint=key, view_func=import_module, **val["options"])
        elif key.startswith("log_message"):
            app.add_url_rule(val["url"], endpoint=key, view_func=log_message, **val["options"])
        else:
            raise NotImplementedError("Unhandled api key.")

    app.register_blueprint(monitor_api)


register_api()


def json_error(status, error, message):
    response = jsonify({"status": status, "error": error, "message": message})
    response.status_code = status
    return response


@app.errorhandler(ValidationError)
def validationerror_error(e):
    """Flask error handler for ValidationError exceptions"""
    return json_error(400, "Bad Request", str(e))


@app.errorhandler(Unauthorized)
def unauthorized_error(e):
    """Flask error handler for NotAuthorized exceptions"""
    return json_error(401, "Unauthorized", str(e))


@app.errorhandler(Forbidden)
def forbidden_error(e):
    """Flask error handler for Forbidden exceptions"""
    return json_error(403, "Forbidden", str(e))


@app.errorhandler(RuntimeError)
def runtimeerror_error(e):
    """Flask error handler for RuntimeError exceptions"""
    log.exception("RuntimeError exception raised")
    return json_error(500, "Internal Server Error", str(e))


@app.errorhandler(UnprocessableEntity)
def unprocessableentity_error(e):
    """Flask error handler for UnprocessableEntity exceptions"""
    return json_error(422, "Unprocessable Entity", str(e))


@app.errorhandler(Conflict)
def conflict_error(e):
    """Flask error handler for Conflict exceptions"""
    return json_error(409, "Conflict", str(e))


@app.errorhandler(NotFound)
def notfound_error(e):
    """Flask error handler for Conflict exceptions"""
    return json_error(404, "Not Found", str(e))


# Ensure the event handler is called on db.session
sqlalchemy.event.listen(
    db.session, "after_commit", send_message_after_module_build_state_change)
