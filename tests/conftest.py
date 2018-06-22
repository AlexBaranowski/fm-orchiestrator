# Copyright (c) 2018  Red Hat, Inc.
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
# Written by Matt Prahl <mprahl@redhat.com>
import os

import pytest

from module_build_service import Modulemd


BASE_DIR = os.path.dirname(__file__)
STAGED_DATA_DIR = os.path.join(BASE_DIR, 'staged_data')

_mmd = Modulemd.Module().new_from_file(
    os.path.join(STAGED_DATA_DIR, 'platform.yaml'))
_mmd.upgrade()
PLATFORM_MODULEMD = _mmd.dumps()

_mmd2 = Modulemd.Module().new_from_file(
    os.path.join(STAGED_DATA_DIR, 'formatted_testmodule.yaml'))
_mmd2.upgrade()
TESTMODULE_MODULEMD = _mmd2.dumps()

_mmd3 = Modulemd.Module().new_from_file(
    os.path.join(STAGED_DATA_DIR, 'formatted_testmodule.yaml'))
_mmd3.upgrade()
_mmd3.set_context("c2c572ed")
TESTMODULE_MODULEMD_SECOND_CONTEXT = _mmd3.dumps()


@pytest.fixture()
def testmodule_mmd_9c690d0e():
    return TESTMODULE_MODULEMD


@pytest.fixture()
def testmodule_mmd_c2c572ed():
    return TESTMODULE_MODULEMD_SECOND_CONTEXT


@pytest.fixture()
def formatted_testmodule_mmd():
    return _mmd2


@pytest.fixture()
def platform_mmd():
    return PLATFORM_MODULEMD
