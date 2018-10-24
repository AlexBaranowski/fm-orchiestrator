# Copyright (c) 2017  Red Hat, Inc.
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

import pytest

import module_build_service.utils
from module_build_service import glib
from module_build_service.errors import StreamAmbigous
from tests import (db, clean_database, make_module)


class TestUtilsModuleStreamExpansion:

    def setup_method(self, test_method):
        clean_database(False)

    def teardown_method(self, test_method):
        clean_database()

    def _get_mmds_required_by_module_recursively(self, module_build):
        """
        Convenience wrapper around get_mmds_required_by_module_recursively
        returning the list with nsvc strings of modules returned by this the wrapped
        method.
        """
        mmd = module_build.mmd()
        module_build_service.utils.expand_mse_streams(db.session, mmd)
        modules = module_build_service.utils.get_mmds_required_by_module_recursively(mmd)
        nsvcs = [":".join([m.get_name(), m.get_stream(), str(m.get_version()), m.get_context()])
                 for m in modules]
        return nsvcs

    def _generate_default_modules(self):
        """
        Generates gtk:1, gtk:2, foo:1 and foo:2 modules requiring the
        platform:f28 and platform:f29 modules.
        """
        platform_f28 = make_module("platform:f28:0:c10", {}, {})
        platform_f29 = make_module("platform:f29:0:c11", {}, {})
        make_module("gtk:1:0:c2", {"platform": ["f28"]}, {}, platform_f28)
        make_module("gtk:1:0:c3", {"platform": ["f29"]}, {}, platform_f29)
        make_module("gtk:2:0:c4", {"platform": ["f28"]}, {}, platform_f28)
        make_module("gtk:2:0:c5", {"platform": ["f29"]}, {}, platform_f29)
        make_module("foo:1:0:c2", {"platform": ["f28"]}, {}, platform_f28)
        make_module("foo:1:0:c3", {"platform": ["f29"]}, {}, platform_f29)
        make_module("foo:2:0:c4", {"platform": ["f28"]}, {}, platform_f28)
        make_module("foo:2:0:c5", {"platform": ["f29"]}, {}, platform_f29)
        make_module("app:1:0:c6", {"platform": ["f29"]}, {}, platform_f29)

    def test_generate_expanded_mmds_context(self):
        self._generate_default_modules()
        module_build = make_module(
            "app:1:0:c1", {"gtk": ["1", "2"]}, {"platform": ["f28"], "gtk": ["1", "2"]})
        mmds = module_build_service.utils.generate_expanded_mmds(
            db.session, module_build.mmd())
        contexts = set([mmd.get_context() for mmd in mmds])
        assert set(['e1e005fb', 'ce132a1e']) == contexts

    @pytest.mark.parametrize(
        'requires,build_requires,stream_ambigous,expected_xmd,expected_buildrequires', [
            ({"gtk": ["1", "2"]},
             {"platform": ["f28"], "gtk": ["1", "2"]}, True,
             set([
                 frozenset(['platform:f28:0:c10', 'gtk:2:0:c4']),
                 frozenset(['platform:f28:0:c10', 'gtk:1:0:c2'])
             ]),
             set([
                 frozenset(['gtk:1', 'platform:f28']),
                 frozenset(['gtk:2', 'platform:f28']),
             ])),

            ({"foo": ["1"]},
             {"platform": ["f28"], "foo": ["1"], "gtk": ["1", "2"]}, True,
             set([
                 frozenset(['foo:1:0:c2', 'gtk:1:0:c2', 'platform:f28:0:c10']),
                 frozenset(['foo:1:0:c2', 'gtk:2:0:c4', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['foo:1', 'gtk:1', 'platform:f28']),
                 frozenset(['foo:1', 'gtk:2', 'platform:f28'])
             ])),

            ({"gtk": ["1"], "foo": ["1"]},
             {"platform": ["f28"], "gtk": ["1"], "foo": ["1"]}, False,
             set([
                 frozenset(['foo:1:0:c2', 'gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['foo:1', 'gtk:1', 'platform:f28'])
             ])),

            ({"gtk": ["1"], "foo": ["1"]},
             {"gtk": ["1"], "foo": ["1"], "platform": ["f28"]}, False,
             set([
                 frozenset(['foo:1:0:c2', 'gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['foo:1', 'gtk:1', 'platform:f28'])
             ])),

            ({"gtk": ["-2"], "foo": ["-2"]},
             {"platform": ["f28"], "gtk": ["-2"], "foo": ["-2"]}, True,
             set([
                 frozenset(['foo:1:0:c2', 'gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['foo:1', 'gtk:1', 'platform:f28'])
             ])),

            ({"gtk": ["1"], "foo": ["1"]},
             {"platform": ["f28"], "gtk": ["-1", "1"], "foo": ["-2", "1"]}, False,
             set([
                 frozenset(['foo:1:0:c2', 'gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['foo:1', 'gtk:1', 'platform:f28'])
             ])),

            ({"gtk": ["1"], "foo": ["1"]},
             {"platform": ["f28"], "gtk": ["1"]}, False,
             set([
                 frozenset(['gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['gtk:1', 'platform:f28'])
             ])),

            ({"gtk": []}, {"platform": ["f28"], "gtk": ["1"]}, True,
             set([
                 frozenset(['gtk:1:0:c2', 'platform:f28:0:c10'])
             ]),
             set([
                 frozenset(['gtk:1', 'platform:f28'])
             ])),

            ({}, {"platform": ["f29"], "app": ["1"]}, False,
             set([
                 frozenset(['app:1:0:c6', 'platform:f29:0:c11'])
             ]),
             set([
                 frozenset(['app:1', 'platform:f29'])
             ])),
        ])
    def test_generate_expanded_mmds_buildrequires(
            self, requires, build_requires, stream_ambigous, expected_xmd,
            expected_buildrequires):
        self._generate_default_modules()
        module_build = make_module("app:1:0:c1", requires, build_requires)

        # Check that generate_expanded_mmds raises an exception if stream is ambigous
        # and also that it does not raise an exception otherwise.
        if stream_ambigous:
            with pytest.raises(StreamAmbigous):
                module_build_service.utils.generate_expanded_mmds(
                    db.session, module_build.mmd(), raise_if_stream_ambigous=True)
        else:
            module_build_service.utils.generate_expanded_mmds(
                db.session, module_build.mmd(), raise_if_stream_ambigous=True)

        # Check that if stream is ambigous and we define the stream, it does not raise
        # an exception.
        if stream_ambigous:
            default_streams = {}
            for ns in list(expected_buildrequires)[0]:
                name, stream = ns.split(":")
                default_streams[name] = stream
            module_build_service.utils.generate_expanded_mmds(
                db.session, module_build.mmd(), raise_if_stream_ambigous=True,
                default_streams=default_streams)

        mmds = module_build_service.utils.generate_expanded_mmds(
            db.session, module_build.mmd())

        buildrequires_per_mmd_xmd = set()
        buildrequires_per_mmd_buildrequires = set()
        for mmd in mmds:
            xmd = glib.from_variant_dict(mmd.get_xmd())
            br_nsvcs = []
            for name, detail in xmd['mbs']['buildrequires'].items():
                br_nsvcs.append(":".join([
                    name, detail["stream"], detail["version"], detail["context"]]))
            buildrequires_per_mmd_xmd.add(frozenset(br_nsvcs))

            assert len(mmd.get_dependencies()) == 1

            buildrequires = set()
            dep = mmd.get_dependencies()[0]
            for req_name, req_streams in dep.get_buildrequires().items():
                for req_stream in req_streams.get():
                    buildrequires.add(":".join([req_name, req_stream]))
            buildrequires_per_mmd_buildrequires.add(frozenset(buildrequires))

        assert buildrequires_per_mmd_xmd == expected_xmd
        assert buildrequires_per_mmd_buildrequires == expected_buildrequires

    @pytest.mark.parametrize('requires,build_requires,expected', [
        ({"gtk": ["1", "2"]}, {"platform": [], "gtk": ["1", "2"]},
         set([
             frozenset(['gtk:1']),
             frozenset(['gtk:2']),
         ])),

        ({"gtk": ["1", "2"]}, {"platform": [], "gtk": ["1"]},
         set([
             frozenset(['gtk:1', 'gtk:2']),
         ])),

        ({"gtk": ["1"], "foo": ["1"]},
         {"platform": [], "gtk": ["1"], "foo": ["1"]},
         set([
             frozenset(['foo:1', 'gtk:1']),
         ])),

        ({"gtk": ["-2"], "foo": ["-2"]},
         {"platform": [], "gtk": ["-2"], "foo": ["-2"]},
         set([
             frozenset(['foo:1', 'gtk:1']),
         ])),

        ({"gtk": ["-1", "1"], "foo": ["-2", "1"]},
         {"platform": [], "gtk": ["-1", "1"], "foo": ["-2", "1"]},
         set([
             frozenset(['foo:1', 'gtk:1']),
         ])),

        ({"gtk": [], "foo": []}, {"platform": [], "gtk": ["1"], "foo": ["1"]},
         set([
             frozenset([]),
         ])),

    ])
    def test_generate_expanded_mmds_requires(self, requires, build_requires, expected):
        self._generate_default_modules()
        module_build = make_module("app:1:0:c1", requires, build_requires)
        mmds = module_build_service.utils.generate_expanded_mmds(
            db.session, module_build.mmd())

        requires_per_mmd = set()
        for mmd in mmds:
            assert len(mmd.get_dependencies()) == 1
            mmd_requires = set()
            dep = mmd.get_dependencies()[0]
            for req_name, req_streams in dep.get_requires().items():
                for req_stream in req_streams.get():
                    mmd_requires.add(":".join([req_name, req_stream]))
            requires_per_mmd.add(frozenset(mmd_requires))

        assert requires_per_mmd == expected

    @pytest.mark.parametrize('requires,build_requires,expected', [
        ({}, {"platform": [], "gtk": ["1", "2"]},
         ['platform:f29:0:c11', 'gtk:2:0:c4', 'gtk:2:0:c5',
          'platform:f28:0:c10', 'gtk:1:0:c2', 'gtk:1:0:c3']),

        ({}, {"platform": [], "gtk": ["1"], "foo": ["1"]},
         ['platform:f28:0:c10', 'gtk:1:0:c2', 'gtk:1:0:c3',
          'foo:1:0:c2', 'foo:1:0:c3', 'platform:f29:0:c11']),

        ({}, {"gtk": ["1"], "foo": ["1"], "platform": ["f28"]},
         ['platform:f28:0:c10', 'gtk:1:0:c2',
          'foo:1:0:c2']),

        ([{}, {}], [{"platform": [], "gtk": ["1"], "foo": ["1"]},
                    {"platform": [], "gtk": ["2"], "foo": ["2"]}],
         ['foo:1:0:c2', 'foo:1:0:c3', 'foo:2:0:c4', 'foo:2:0:c5',
          'platform:f28:0:c10', 'platform:f29:0:c11', 'gtk:1:0:c2',
          'gtk:1:0:c3', 'gtk:2:0:c4', 'gtk:2:0:c5']),

        ({}, {"platform": [], "gtk": ["-2"], "foo": ["-2"]},
         ['foo:1:0:c2', 'foo:1:0:c3', 'platform:f29:0:c11',
          'platform:f28:0:c10', 'gtk:1:0:c2', 'gtk:1:0:c3']),

        ({}, {"platform": [], "gtk": ["-1", "1"], "foo": ["-2", "1"]},
         ['foo:1:0:c2', 'foo:1:0:c3', 'platform:f29:0:c11',
          'platform:f28:0:c10', 'gtk:1:0:c2', 'gtk:1:0:c3']),
    ])
    def test_get_required_modules_simple(self, requires, build_requires, expected):
        module_build = make_module("app:1:0:c1", requires, build_requires)
        self._generate_default_modules()
        nsvcs = self._get_mmds_required_by_module_recursively(module_build)
        assert set(nsvcs) == set(expected)

    def _generate_default_modules_recursion(self):
        """
        Generates the gtk:1 module requiring foo:1 module requiring bar:1
        and lorem:1 modules which require base:f29 module requiring
        platform:f29 module :).
        """
        base_module = make_module("platform:f29:0:c11", {}, {})
        make_module("gtk:1:0:c2", {"foo": ["unknown"]}, {}, base_module)
        make_module("gtk:1:1:c2", {"foo": ["1"]}, {}, base_module)
        make_module("foo:1:0:c2", {"bar": ["unknown"]}, {}, base_module)
        make_module("foo:1:1:c2", {"bar": ["1"], "lorem": ["1"]}, {}, base_module)
        make_module("bar:1:0:c2", {"base": ["unknown"]}, {}, base_module)
        make_module("bar:1:1:c2", {"base": ["f29"]}, {}, base_module)
        make_module("lorem:1:0:c2", {"base": ["unknown"]}, {}, base_module)
        make_module("lorem:1:1:c2", {"base": ["f29"]}, {}, base_module)
        make_module("base:f29:0:c3", {"platform": ["f29"]}, {}, base_module)

    @pytest.mark.parametrize('requires,build_requires,expected', [
        ({}, {"platform": [], "gtk": ["1"]},
         ['foo:1:1:c2', 'base:f29:0:c3', 'platform:f29:0:c11',
          'bar:1:1:c2', 'gtk:1:1:c2', 'lorem:1:1:c2']),

        ({}, {"platform": [], "foo": ["1"]},
         ['foo:1:1:c2', 'base:f29:0:c3', 'platform:f29:0:c11',
          'bar:1:1:c2', 'lorem:1:1:c2']),
    ])
    def test_get_required_modules_recursion(self, requires, build_requires, expected):
        module_build = make_module("app:1:0:c1", requires, build_requires)
        self._generate_default_modules_recursion()
        nsvcs = self._get_mmds_required_by_module_recursively(module_build)
        assert set(nsvcs) == set(expected)

    def _generate_default_modules_modules_multiple_stream_versions(self):
        """
        Generates the gtk:1 module requiring foo:1 module requiring bar:1
        and lorem:1 modules which require base:f29 module requiring
        platform:f29 module :).
        """
        f290000 = make_module("platform:f29.0.0:0:c11", {}, {})
        f290100 = make_module("platform:f29.1.0:0:c11", {}, {})
        f290200 = make_module("platform:f29.2.0:0:c11", {}, {})
        make_module("gtk:1:0:c2", {"platform": ["f29"]}, {}, f290000)
        make_module("gtk:1:1:c2", {"platform": ["f29"]}, {}, f290100)
        make_module("gtk:1:2:c2", {"platform": ["f29"]}, {}, f290100)
        make_module("gtk:1:3:c2", {"platform": ["f29"]}, {}, f290200)

    @pytest.mark.parametrize('requires,build_requires,expected', [
        ({}, {"platform": ["f29.1.0"], "gtk": ["1"]},
         ['platform:f29.0.0:0:c11', 'gtk:1:0:c2', 'gtk:1:2:c2', 'platform:f29.1.0:0:c11']),
    ])
    def test_get_required_modules_stream_versions(self, requires, build_requires, expected):
        module_build = make_module("app:1:0:c1", requires, build_requires)
        self._generate_default_modules_modules_multiple_stream_versions()
        nsvcs = self._get_mmds_required_by_module_recursively(module_build)
        assert set(nsvcs) == set(expected)
