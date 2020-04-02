# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import collections

import pytest
import solv

from module_build_service.web.mmd_resolver import MMDResolver
from tests import make_module


class TestMMDResolver:
    def setup_method(self, test_method):
        self.mmd_resolver = MMDResolver()

    def teardown_method(self, test_method):
        pass

    @pytest.mark.parametrize(
        "deps, expected",
        (
            ([], "None"),
            ([{"x": []}], "module(x)"),
            ([{"x": ["1"]}], "(module(x) with module(x:1))"),
            ([{"x": ["1", "2"]}], "(module(x) with (module(x:1) or module(x:2)))"),
            ([{"x": [], "y": []}], "(module(x) and module(y))"),
            ([{"x": []}, {"y": []}], "(module(x) or module(y))"),
        ),
    )
    def test_deps2reqs(self, deps, expected):
        # Sort by keys here to avoid unordered dicts
        deps = [collections.OrderedDict(sorted(dep.items())) for dep in deps]
        reqs = self.mmd_resolver._deps2reqs(deps)
        assert str(reqs) == expected

    @pytest.mark.parametrize(
        "dependencies, expected",
        (
            (
                [{"buildrequires": {"platform": []}}],
                [
                    [
                        ["platform:f28:0:c0:x86_64"],
                        ["platform:f29:0:c0:x86_64"],
                    ]
                ]
            ),
            (
                [{"buildrequires": {"platform": ["f28"]}}],
                [
                    [["platform:f28:0:c0:x86_64"]]
                ]
            ),
            (
                [{"buildrequires": {"gtk": [], "qt": []}}],
                [
                    [
                        ["gtk:3:0:c8:x86_64", "qt:4:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:4:0:c8:x86_64", "qt:4:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:3:0:c8:x86_64", "qt:5:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:4:0:c8:x86_64", "qt:5:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                    ]
                ],
            ),
            (
                [{"buildrequires": {"gtk": [], "qt": [], "platform": []}}],
                [
                    [
                        ["gtk:3:0:c8:x86_64", "qt:4:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:4:0:c8:x86_64", "qt:4:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:3:0:c8:x86_64", "qt:5:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:4:0:c8:x86_64", "qt:5:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["gtk:3:0:c9:x86_64", "qt:4:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                        ["gtk:4:0:c9:x86_64", "qt:4:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                        ["gtk:3:0:c9:x86_64", "qt:5:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                        ["gtk:4:0:c9:x86_64", "qt:5:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                    ]
                ],
            ),
            (
                [
                    {"buildrequires": {"qt": [], "platform": ["f28"]}},
                    {"buildrequires": {"gtk": [], "platform": ["f29"]}}
                ],
                [
                    [
                        ["qt:4:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["qt:5:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                    ],
                    [
                        ["gtk:3:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                        ["gtk:4:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                    ],
                ],
            ),
            (
                [{"buildrequires": {"mess": []}}],
                [[["mess:1:0:c0:x86_64", "gtk:3:0:c8:x86_64", "platform:f28:0:c0:x86_64"]]],
            ),
            (
                [{"buildrequires": {"mess": [], "platform": []}}],
                [
                    [
                        ["mess:1:0:c0:x86_64", "gtk:3:0:c8:x86_64", "platform:f28:0:c0:x86_64"],
                        ["mess:1:0:c0:x86_64", "gtk:4:0:c9:x86_64", "platform:f29:0:c0:x86_64"],
                    ]
                ],
            ),
        ),
    )
    def test_solve(self, dependencies, expected):
        modules = (
            ("platform:f28:0:c0", []),
            ("platform:f29:0:c0", []),
            ("gtk:3:0:c8", [{"requires": {"platform": ["f28"]}}]),
            ("gtk:3:0:c9", [{"requires": {"platform": ["f29"]}}]),
            ("gtk:4:0:c8", [{"requires": {"platform": ["f28"]}}]),
            ("gtk:4:0:c9", [{"requires": {"platform": ["f29"]}}]),
            ("qt:4:0:c8", [{"requires": {"platform": ["f28"]}}]),
            ("qt:4:0:c9", [{"requires": {"platform": ["f29"]}}]),
            ("qt:5:0:c8", [{"requires": {"platform": ["f28"]}}]),
            ("qt:5:0:c9", [{"requires": {"platform": ["f29"]}}]),
            (
                "mess:1:0:c0",
                [
                    {"requires": {"gtk": ["3"], "platform": ["f28"]}},
                    {"requires": {"gtk": ["4"], "platform": ["f29"]}},
                ],
            ),
        )
        for nsvc, deps in modules:
            self.mmd_resolver.add_modules(
                make_module(nsvc, dependencies=deps))

        app = make_module("app:1:0", dependencies=dependencies)
        expanded = self.mmd_resolver.solve(app)

        expected = set(
            frozenset(["app:1:0:%d:src" % c] + e) for c, exp in enumerate(expected) for e in exp
        )

        assert expanded == expected

    @pytest.mark.parametrize(
        "dependencies, expected",
        (
            # BR all platform streams -> build for all platform streams.
            (
                [{"buildrequires": {"platform": []}}],
                [
                    [
                        ["platform:el8.2.0.z:0:c0:x86_64"],
                        ["platform:el8.1.0:0:c0:x86_64"],
                        ["platform:el8.0.0:0:c0:x86_64"],
                        ["platform:el7.6.0:0:c0:x86_64"],
                    ]
                ],
            ),
            # BR "el8" platform stream -> build for all el8 platform streams.
            (
                [{"buildrequires": {"platform": ["el8"]}}],
                [
                    [
                        ["platform:el8.2.0.z:0:c0:x86_64"],
                        ["platform:el8.1.0:0:c0:x86_64"],
                        ["platform:el8.0.0:0:c0:x86_64"],
                    ]
                ],
            ),
            # BR "el8.1.0" platform stream -> build just for el8.1.0.
            (
                [{"buildrequires": {"platform": ["el8.1.0"]}}],
                [[["platform:el8.1.0:0:c0:x86_64"]]]
            ),
            # BR platform:el8.1.0 and gtk:3, which is not built against el8.1.0,
            # but it is built only against el8.0.0 -> cherry-pick gtk:3 from el8.0.0
            # and build once against platform:el8.1.0.
            (
                [{"buildrequires": {"platform": ["el8.1.0"], "gtk": ["3"]}}],
                [[["platform:el8.1.0:0:c0:x86_64", "gtk:3:0:c8:x86_64"]]],
            ),
            # BR platform:el8.2.0 and gtk:3, this time gtk:3 build against el8.2.0 exists
            # -> use both platform and gtk from el8.2.0 and build once.
            (
                [{"buildrequires": {"platform": ["el8.2.0.z"], "gtk": ["3"]}}],
                [[["platform:el8.2.0.z:0:c0:x86_64", "gtk:3:1:c8:x86_64"]]],
            ),
            # BR platform:el8.2.0 and mess:1 which is built against platform:el8.1.0 and
            # requires gtk:3 which is built against platform:el8.2.0 and platform:el8.0.0
            # -> Use platform:el8.2.0 and
            # -> cherry-pick mess:1 from el8.1.0 and
            # -> use gtk:3:1 from el8.2.0.
            (
                [{"buildrequires": {"platform": ["el8.2.0.z"], "mess": ["1"]}}],
                [[["platform:el8.2.0.z:0:c0:x86_64", "mess:1:0:c0:x86_64", "gtk:3:1:c8:x86_64"]]],
            ),
            # BR platform:el8.1.0 and mess:1 which is built against platform:el8.1.0 and
            # requires gtk:3 which is built against platform:el8.2.0 and platform:el8.0.0
            # -> Use platform:el8.1.0 and
            # -> Used mess:1 from el8.1.0 and
            # -> cherry-pick gtk:3:0 from el8.0.0.
            (
                [{"buildrequires": {"platform": ["el8.1.0"], "mess": ["1"]}}],
                [[["platform:el8.1.0:0:c0:x86_64", "mess:1:0:c0:x86_64", "gtk:3:0:c8:x86_64"]]],
            ),
            # BR platform:el8.0.0 and mess:1 which is built against platform:el8.1.0 and
            # requires gtk:3 which is built against platform:el8.2.0 and platform:el8.0.0
            # -> No valid combination, because mess:1 is only available in el8.1.0 and later.
            ([{"buildrequires": {"platform": ["el8.0.0"], "mess": ["1"]}}], []),
            # This is undefined... it might build just once against latest platform or
            # against all the platforms... we don't know
            # ({"platform": ["el8"], "gtk": ["3"]}, {}, [
            #     [["platform:el8.2.0:0:c0:x86_64", "gtk:3:1:c8:x86_64"]],
            # ]),
        ),
    )
    def test_solve_virtual_streams(self, dependencies, expected):
        modules = (
            # (nsvc, dependencies, xmd_mbs_buildrequires, virtual_streams)
            ("platform:el8.0.0:0:c0", [], {}, ["el8"]),
            ("platform:el8.1.0:0:c0", [], {}, ["el8"]),
            ("platform:el8.2.0.z:0:c0", [], {}, ["el8"]),
            ("platform:el7.6.0:0:c0", [], {}, ["el7"]),
            (
                "gtk:3:0:c8",
                [{"requires": {"platform": ["el8"]}}],
                {"platform": {"stream": "el8.0.0"}},
                None
            ),
            (
                "gtk:3:1:c8",
                [{"requires": {"platform": ["el8"]}}],
                {"platform": {"stream": "el8.2.0.z"}},
                None
            ),
            (
                "mess:1:0:c0",
                [{"requires": {"gtk": ["3"], "platform": ["el8"]}}],
                {"platform": {"stream": "el8.1.0"}},
                None
            ),
        )
        for nsvc, deps, xmd_mbs_brs, virtual_streams in modules:
            xmd = {"mbs": {"buildrequires": xmd_mbs_brs}}
            if virtual_streams:
                xmd["mbs"]["virtual_streams"] = virtual_streams
            self.mmd_resolver.add_modules(
                make_module(nsvc, dependencies=deps, xmd=xmd))

        app = make_module("app:1:0", dependencies=dependencies)
        if not expected:
            with pytest.raises(RuntimeError):
                self.mmd_resolver.solve(app)
            return
        else:
            expanded = self.mmd_resolver.solve(app)

        expected = set(
            frozenset(["app:1:0:%d:src" % c] + e) for c, exp in enumerate(expected) for e in exp)

        assert expanded == expected

    @pytest.mark.parametrize(
        "dependencies, modules, err_msg_regex",
        (
            # app --br--> gtk:1 --req--> bar:1* ---req---> platform:f29
            #    \--br--> foo:1 --req--> bar:2* ---req--/
            (
                [{"buildrequires": {"gtk": ["1"], "foo": ["1"]}}],
                (
                    ("platform:f29:0:c0", []),
                    ("gtk:1:1:c01", [{"requires": {"bar": ["1"]}}]),
                    ("bar:1:0:c02", [{"requires": {"platform": ["f29"]}}]),
                    ("foo:1:1:c03", [{"requires": {"bar": ["2"]}}]),
                    ("bar:2:0:c04", [{"requires": {"platform": ["f29"]}}]),
                ),
                "bar:1:0:c02 and bar:2:0:c04",
            ),
            # app --br--> gtk:1 --req--> bar:1* ----------req----------> platform:f29
            #    \--br--> foo:1 --req--> baz:1 --req--> bar:2* --req--/
            (
                [{"buildrequires": {"gtk": ["1"], "foo": ["1"]}}],
                (
                    ("platform:f29:0:c0", []),
                    ("gtk:1:1:c01", [{"requires": {"bar": ["1"]}}]),
                    ("bar:1:0:c02", [{"requires": {"platform": ["f29"]}}]),
                    ("foo:1:1:c03", [{"requires": {"baz": ["1"]}}]),
                    ("baz:1:1:c04", [{"requires": {"bar": ["2"]}}]),
                    ("bar:2:0:c05", [{"requires": {"platform": ["f29"]}}]),
                ),
                "bar:1:0:c02 and bar:2:0:c05",
            ),
            # Test multiple conflicts pairs are detected.
            # app --br--> gtk:1 --req--> bar:1* ---------req-----------\
            #    \--br--> foo:1 --req--> baz:1 --req--> bar:2* ---req---> platform:f29
            #    \--br--> pkga:1 --req--> perl:5' -------req-----------/
            #    \--br--> pkgb:1 --req--> perl:6' -------req-----------/
            (
                [{
                    "buildrequires": {
                        "gtk": ["1"],
                        "foo": ["1"],
                        "pkga": ["1"],
                        "pkgb": ["1"],
                    }
                }],
                (
                    ("platform:f29:0:c0", {}),
                    ("gtk:1:1:c01", [{"requires": {"bar": ["1"]}}]),
                    ("bar:1:0:c02", [{"requires": {"platform": ["f29"]}}]),
                    ("foo:1:1:c03", [{"requires": {"baz": ["1"]}}]),
                    ("baz:1:1:c04", [{"requires": {"bar": ["2"]}}]),
                    ("bar:2:0:c05", [{"requires": {"platform": ["f29"]}}]),
                    ("pkga:1:0:c06", [{"requires": {"perl": ["5"]}}]),
                    ("perl:5:0:c07", [{"requires": {"platform": ["f29"]}}]),
                    ("pkgb:1:0:c08", [{"requires": {"perl": ["6"]}}]),
                    ("perl:6:0:c09", [{"requires": {"platform": ["f29"]}}]),
                ),
                # MMD Resolver should still catch a conflict
                "bar:1:0:c02 and bar:2:0:c05",
            ),
        ),
    )
    def test_solve_stream_conflicts(self, dependencies, modules, err_msg_regex):
        for nsvc, deps in modules:
            self.mmd_resolver.add_modules(
                make_module(nsvc, dependencies=deps))

        app = make_module("app:1:0", dependencies=dependencies)

        with pytest.raises(RuntimeError, match=err_msg_regex):
            self.mmd_resolver.solve(app)

    def test_solve_new_platform(self,):
        """
        Tests that MMDResolver works in case we add new platform and there is
        modular dependency of input module which is built only against old
        platforms and in the same time the input module wants to be built
        with all available platforms.
        """
        modules = (
            ("platform:f28:0:c0", []),
            ("platform:f29:0:c0", []),
            ("platform:f30:0:c0", []),
            ("gtk:3:0:c8", [{"requires": {"platform": ["f28"]}}]),
            ("gtk:3:0:c9", [{"requires": {"platform": ["f29"]}}]),
        )
        for nsvc, deps in modules:
            self.mmd_resolver.add_modules(
                make_module(nsvc, dependencies=deps))

        app = make_module("app:1:0", dependencies=[
            {"buildrequires": {"platform": [], "gtk": ["3"]}}
        ])
        expanded = self.mmd_resolver.solve(app)

        # Build only against f28 and f29, because "gtk:3" is not built against f30.
        expected = {
            frozenset(["gtk:3:0:c8:x86_64", "app:1:0:0:src", "platform:f28:0:c0:x86_64"]),
            frozenset(["gtk:3:0:c9:x86_64", "app:1:0:0:src", "platform:f29:0:c0:x86_64"]),
        }

        assert expanded == expected

    def test_solve_requires_any_platform(self,):
        """
        Tests that MMDResolver finds out the buildrequired module `foo` even if
        it was built on newer platform stream, but can run on any platform stream.
        """
        modules = (
            ("platform:f28:0:c0", [], {}),
            ("platform:f29:0:c0", [], {}),
            ("platform:f30:0:c0", [], {}),
            (
                "foo:1:0:c8",
                [{"requires": {"platform": []}}],
                {"platform": {"stream": "f29"}}
            ),
        )
        for nsvc, deps, xmd_mbs_brs in modules:
            mmd = make_module(nsvc, dependencies=deps, xmd={
                "mbs": {"buildrequires": xmd_mbs_brs}
            })
            self.mmd_resolver.add_modules(mmd)

        app = make_module("app:1:0", [
            {"buildrequires": {"platform": ["f28"], "foo": ["1"]}}
        ])
        expanded = self.mmd_resolver.solve(app)

        expected = {
            frozenset(["foo:1:0:c8:x86_64", "app:1:0:0:src", "platform:f28:0:c0:x86_64"]),
        }

        assert expanded == expected

    @pytest.mark.parametrize(
        "nsvc, dependencies, expected",
        (
            ("platform:f28:0:c0", [], True),
            ("platform:latest:5:c8", [], False),
            ("gtk:3:0:c8", [{"requires": {"platform": ["f28"]}}], False)
        ),
    )
    def test_base_module_stream_version(self, nsvc, dependencies, expected):
        """
        Tests that add_base_module_provides returns True for base modules with stream versions
        """
        mmd = make_module(nsvc, dependencies=dependencies)
        solvable = self.mmd_resolver.available_repo.add_solvable()
        solvable.name = nsvc
        solvable.evr = str(mmd.get_version())
        solvable.arch = "x86_64"
        assert self.mmd_resolver._add_base_module_provides(solvable, mmd) is expected

    @pytest.mark.parametrize(
        "nsvc, expected",
        (
            ("platform:f28:3:c0", {"module(platform)", "module(platform:f28) = 28.0"}),
            ("platform:latest:5:c8", {"module(platform)", "module(platform:latest) = 5"}),
        ),
    )
    def test_base_module_provides(self, nsvc, expected):
        self.mmd_resolver.add_modules(make_module(nsvc))
        ns = nsvc.rsplit(":", 2)[0]
        provides = self.mmd_resolver.solvables[ns][0].lookup_deparray(solv.SOLVABLE_PROVIDES)
        assert {str(provide) for provide in provides} == expected
