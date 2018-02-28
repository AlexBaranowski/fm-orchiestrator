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
# Written by Ralph Bean <rbean@redhat.com>

import os

import gi
gi.require_version('Modulemd', '1.0')  # noqa
from gi.repository import Modulemd

from tests.test_models import init_data
from tests import init_data as init_data_contexts
from module_build_service import conf
from module_build_service.models import ComponentBuild, ModuleBuild, make_session


class TestModels:
    def setup_method(self, test_method):
        init_data()

    def test_app_sqlalchemy_events(self):
        with make_session(conf) as session:
            component_build = ComponentBuild()
            component_build.package = 'before_models_committed'
            component_build.scmurl = \
                ('git://pkgs.domain.local/rpms/before_models_committed?'
                 '#9999999999999999999999999999999999999999')
            component_build.format = 'rpms'
            component_build.task_id = 999999999
            component_build.state = 1
            component_build.nvr = ('before_models_committed-0.0.0-0'
                                   '.module_before_models_committed_0_0')
            component_build.batch = 1
            component_build.module_id = 1

            session.add(component_build)
            session.commit()

        with make_session(conf) as session:
            c = session.query(ComponentBuild).filter(ComponentBuild.id == 1).one()
            assert c.component_builds_trace[0].id == 1
            assert c.component_builds_trace[0].component_id == 1
            assert c.component_builds_trace[0].state == 1
            assert c.component_builds_trace[0].state_reason is None
            assert c.component_builds_trace[0].task_id == 999999999

    def test_context_functions(self):
        """ Test that the build_context, runtime_context, and context hashes are correctly
        determined"""
        build = ModuleBuild.query.filter_by(id=1).one()
        yaml_path = os.path.join(
            os.path.dirname(__file__), '..', 'staged_data', 'testmodule_dependencies.yaml')
        mmd = Modulemd.Module().new_from_file(yaml_path)
        mmd.upgrade()
        build.modulemd = mmd.dumps()
        build.build_context, build.runtime_context = ModuleBuild.contexts_from_mmd(build.modulemd)
        assert build.build_context == 'f6e2aeec7576196241b9afa0b6b22acf2b6873d7'
        assert build.runtime_context == '1739827b08388842fc90ccc0b6070c59b7d856fc'
        assert build.context == 'e7a3d35e'

class TestModelsGetStreamsContexts:
    def setup_method(self, test_method):
        init_data_contexts(contexts=True)

    def test_get_last_build_in_all_streams(self):
        with make_session(conf) as session:
            builds = ModuleBuild.get_last_build_in_all_streams(
                session, "nginx")
            builds = ["%s:%s:%s" % (build.name, build.stream, str(build.version))
                      for build in builds]
            assert builds == ["nginx:%d:%d" % (i, i + 2) for i in range(10)]

    def test_get_last_build_in_stream(self):
        with make_session(conf) as session:
            build = ModuleBuild.get_last_build_in_stream(
                session, "nginx", "1")
            build = "%s:%s:%s" % (build.name, build.stream, str(build.version))
            assert build == 'nginx:1:3'

    def test_get_builds_in_version(self):
        with make_session(conf) as session:
            builds = ModuleBuild.get_builds_in_version(
                session, "nginx", "1", "3")
            builds = ["%s:%s:%s:%s" % (build.name, build.stream, str(build.version),
                                       build.context) for build in builds]
            assert builds == ['nginx:1:3:d5a6c0fa', 'nginx:1:3:795e97c1']
