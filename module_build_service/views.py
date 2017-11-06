# -*- coding: utf-8 -*-
# Copyright (c) 2016  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Petr Šabata <contyk@redhat.com>
#            Matt Prahl <mprahl@redhat.com>

""" The module build orchestrator for Modularity, API.
This is the implementation of the orchestrator's public RESTful API.
"""

import json
import module_build_service.auth
from flask import request, jsonify
from flask.views import MethodView

from module_build_service import app, conf, log, models, db, version
from module_build_service.utils import (
    pagination_metadata, filter_module_builds, filter_component_builds,
    submit_module_build_from_scm, submit_module_build_from_yaml,
    get_scm_url_re)
from module_build_service.errors import (
    ValidationError, Forbidden, NotFound, ProgrammingError)

api_v1 = {
    'module_builds': {
        'url': '/module-build-service/1/module-builds/',
        'options': {
            'methods': ['POST'],
        }
    },
    'module_builds_list': {
        'url': '/module-build-service/1/module-builds/',
        'options': {
            'defaults': {'id': None},
            'methods': ['GET'],
        }
    },
    'module_build': {
        'url': '/module-build-service/1/module-builds/<int:id>',
        'options': {
            'methods': ['GET', 'PATCH'],
        }
    },
    'component_builds_list': {
        'url': '/module-build-service/1/component-builds/',
        'options': {
            'defaults': {'id': None},
            'methods': ['GET'],
        }
    },
    'component_build': {
        'url': '/module-build-service/1/component-builds/<int:id>',
        'options': {
            'methods': ['GET'],
        }
    },
    'about': {
        'url': '/module-build-service/1/about/',
        'options': {
            'methods': ['GET']
        }
    },
    'rebuild_strategies_list': {
        'url': '/module-build-service/1/rebuild-strategies/',
        'options': {
            'methods': ['GET']
        }
    }
}


class AbstractQueryableBuildAPI(MethodView):
    """ An abstract class, housing some common functionality. """

    def get(self, id):
        verbose_flag = request.args.get('verbose', 'false').lower()

        if id is None:
            # Lists all tracked builds
            p_query = self.query_filter(request)

            json_data = {
                'meta': pagination_metadata(p_query, request.args)
            }

            if verbose_flag == 'true' or verbose_flag == '1':
                json_data['items'] = [item.extended_json() for item in p_query.items]
            else:
                json_data['items'] = [item.json() for item in p_query.items]

            return jsonify(json_data), 200
        else:
            # Lists details for the specified build
            instance = self.model.query.filter_by(id=id).first()
            if instance:
                if verbose_flag == 'true' or verbose_flag == '1':
                    return jsonify(instance.extended_json()), 200
                else:
                    return jsonify(instance.json()), 200
            else:
                raise NotFound('No such %s found.' % self.kind)


class ComponentBuildAPI(AbstractQueryableBuildAPI):
    kind = 'component'
    query_filter = staticmethod(filter_component_builds)
    model = models.ComponentBuild


class ModuleBuildAPI(AbstractQueryableBuildAPI):
    kind = 'module'
    query_filter = staticmethod(filter_module_builds)
    model = models.ModuleBuild

    # Additional POST and DELETE handlers for modules follow.

    def post(self):
        if "multipart/form-data" in request.headers.get("Content-Type", ""):
            handler = YAMLFileHandler(request)
        else:
            handler = SCMHandler(request)

        if conf.no_auth is True and handler.username == "anonymous" and "owner" in handler.data:
            handler.username = handler.data["owner"]

        if conf.allowed_groups and not (conf.allowed_groups & handler.groups):
            raise Forbidden("%s is not in any of  %r, only %r" % (
                handler.username, conf.allowed_groups, handler.groups))

        handler.validate()
        module = handler.post()
        return jsonify(module.extended_json()), 201

    def patch(self, id):
        username, groups = module_build_service.auth.get_user(request)

        try:
            r = json.loads(request.get_data().decode("utf-8"))
        except Exception:
            log.error('Invalid JSON submitted')
            raise ValidationError('Invalid JSON submitted')

        if "owner" in r:
            if conf.no_auth is not True:
                raise ValidationError(("The request contains 'owner' parameter,"
                                       " however NO_AUTH is not allowed"))
            elif username == "anonymous":
                username = r["owner"]

        if conf.allowed_groups and not (conf.allowed_groups & groups):
            raise Forbidden("%s is not in any of  %r, only %r" % (
                username, conf.allowed_groups, groups))

        module = models.ModuleBuild.query.filter_by(id=id).first()
        if not module:
            raise NotFound('No such module found.')

        if module.owner != username and not (conf.admin_groups & groups):
            raise Forbidden('You are not owner of this build and '
                            'therefore cannot modify it.')

        if not r.get('state'):
            log.error('Invalid JSON submitted')
            raise ValidationError('Invalid JSON submitted')

        if module.state == models.BUILD_STATES['failed']:
            raise Forbidden('You can\'t cancel a failed module')

        if r['state'] == 'failed' \
                or r['state'] == str(models.BUILD_STATES['failed']):
            module.transition(conf, models.BUILD_STATES["failed"],
                              "Canceled by %s." % username)
        else:
            log.error('The provided state change of "{}" is not supported'
                      .format(r['state']))
            raise ValidationError('The provided state change is not supported')
        db.session.add(module)
        db.session.commit()

        return jsonify(module.extended_json()), 200


class AboutAPI(MethodView):
    def get(self):
        json = {'version': version}
        config_items = ['auth_method']
        for item in config_items:
            config_item = getattr(conf, item)
            # All config items have a default, so if doesn't exist it is a programming error
            if not config_item:
                raise ProgrammingError(
                    'An invalid config item of "{0}" was specified'.format(item))
            json[item] = config_item
        return jsonify(json), 200


class RebuildStrategies(MethodView):
    def get(self):
        items = []
        # Sort the items list by name
        for strategy in sorted(models.ModuleBuild.rebuild_strategies.keys()):
            default = False
            if strategy == conf.rebuild_strategy:
                default = True
                allowed = True
            elif conf.rebuild_strategy_allow_override and \
                    strategy in conf.rebuild_strategies_allowed:
                allowed = True
            else:
                allowed = False
            items.append({
                'name': strategy,
                'description': models.ModuleBuild.rebuild_strategies[strategy],
                'allowed': allowed,
                'default': default
            })

        return jsonify({'items': items}), 200


class BaseHandler(object):
    def __init__(self, request):
        self.username, self.groups = module_build_service.auth.get_user(request)
        self.data = None

    @property
    def optional_params(self):
        return {k: v for k, v in self.data.items() if k not in ["owner", "scmurl", "branch"]}

    def validate_optional_params(self):
        forbidden_params = [k for k in self.data
                            if k not in models.ModuleBuild.__table__.columns and
                            k not in ["branch", "rebuild_strategy"]]
        if forbidden_params:
            raise ValidationError('The request contains unspecified parameters: {}'
                                  .format(", ".join(forbidden_params)))

        forbidden_params = [k for k in self.data if k.startswith("copr_")]
        if conf.system != "copr" and forbidden_params:
            raise ValidationError(('The request contains parameters specific to Copr builder:'
                                   ' {} even though {} is used')
                                  .format(", ".join(forbidden_params), conf.system))

        if not conf.no_auth and "owner" in self.data:
            raise ValidationError(("The request contains 'owner' parameter,"
                                   " however NO_AUTH is not allowed"))

        if not conf.rebuild_strategy_allow_override and 'rebuild_strategy' in self.data:
            raise ValidationError('The request contains the "rebuild_strategy" parameter but '
                                  'overriding the default isn\'t allowed')

        if 'rebuild_strategy' in self.data:
            if self.data['rebuild_strategy'] not in conf.rebuild_strategies_allowed:
                raise ValidationError(
                    'The rebuild method of "{0}" is not allowed. Choose from: {1}.'
                    .format(self.data['rebuild_strategy'],
                            ', '.join(conf.rebuild_strategies_allowed)))


class SCMHandler(BaseHandler):
    def __init__(self, request):
        super(SCMHandler, self).__init__(request)
        try:
            self.data = json.loads(request.get_data().decode("utf-8"))
        except Exception:
            log.error('Invalid JSON submitted')
            raise ValidationError('Invalid JSON submitted')

    def validate(self):
        if "scmurl" not in self.data:
            log.error('Missing scmurl')
            raise ValidationError('Missing scmurl')

        url = self.data["scmurl"]
        allowed_prefix = any(url.startswith(prefix) for prefix in conf.scmurls)
        if not conf.allow_custom_scmurls and not allowed_prefix:
            log.error("The submitted scmurl %r is not allowed" % url)
            raise Forbidden("The submitted scmurl %s is not allowed" % url)

        if not get_scm_url_re().match(url):
            log.error("The submitted scmurl %r is not valid" % url)
            raise Forbidden("The submitted scmurl %s is not valid" % url)

        if "branch" not in self.data:
            log.error('Missing branch')
            raise ValidationError('Missing branch')

        self.validate_optional_params()

    def post(self):
        url = self.data["scmurl"]
        branch = self.data["branch"]

        # python-modulemd expects this to be bytes, not unicode.
        if isinstance(branch, unicode):
            branch = branch.encode('utf-8')

        return submit_module_build_from_scm(self.username, url, branch,
                                            allow_local_url=False,
                                            optional_params=self.optional_params)


class YAMLFileHandler(BaseHandler):
    def __init__(self, request):
        if not conf.yaml_submit_allowed:
            raise Forbidden("YAML submission is not enabled")
        super(YAMLFileHandler, self).__init__(request)
        self.data = request.form.to_dict()

    def validate(self):
        if "yaml" not in request.files:
            log.error('Invalid file submitted')
            raise ValidationError('Invalid file submitted')
        self.validate_optional_params()

    def post(self):
        handle = request.files["yaml"]
        return submit_module_build_from_yaml(self.username, handle,
                                             optional_params=self.optional_params)


def register_api_v1():
    """ Registers version 1 of MBS API. """
    module_view = ModuleBuildAPI.as_view('module_builds')
    component_view = ComponentBuildAPI.as_view('component_builds')
    about_view = AboutAPI.as_view('about')
    rebuild_strategies_view = RebuildStrategies.as_view('rebuild_strategies')
    for key, val in api_v1.items():
        if key.startswith('component_build'):
            app.add_url_rule(val['url'],
                             endpoint=key,
                             view_func=component_view,
                             **val['options'])
        elif key.startswith('module_build'):
            app.add_url_rule(val['url'],
                             endpoint=key,
                             view_func=module_view,
                             **val['options'])
        elif key.startswith('about'):
            app.add_url_rule(val['url'],
                             endpoint=key,
                             view_func=about_view,
                             **val['options'])
        elif key == 'rebuild_strategies_list':
            app.add_url_rule(val['url'],
                             endpoint=key,
                             view_func=rebuild_strategies_view,
                             **val['options'])
        else:
            raise NotImplementedError("Unhandled api key.")


register_api_v1()
