Running Tests
=============

Inside Container
----------------

Since MBS requires Python dependencies that aren't available using PyPi (e.g.
libsolv bindings), there are container images (based on CentOS and Fedora) that
can be used to run the code analysis and unit tests.

* ``docker/Dockerfile-tests`` is based on ``centos:7``, inside which tests run
  with Python 2.

* ``docker/Dockerfile-tests-py3`` is based on ``fedora:29``, inside which tests
  run with Python 3.

Both of these images are available from Quay.io under `factory2 organization`_
and named ``mbs-test-centos`` and ``mbs-test-fedora`` individually. Refer to
section "Updating test images in Quay" to learn how to manage these images.

.. _factory2: https://quay.io/organization/factory2

To run the tests, just simply run: ``run-unittests.sh``

By default, this script runs tests inside container ``mbs-test-centos``
with Python 2 and SQLite database.

There are options to change the tests enviornment:

* ``--py3``: run tests with Python 3.
* ``--with-pgsql``: run tests with PostgreSQL database.
* ``--no-tty``: don't use tty for containers
* ``--sudo``: run Docker via sudo
* ``--podman``: use Podman instead of Docker
* ``--no-pull``: don't update Docker images

For example, ``run-unittests.sh --py3 --with-pgsql``.

You can specify the subset of tests to run inside the container as well. Tests
specified from the command-line are passed to ``py.test`` directly. Please note that,
the path of each test must be a relative path. For example::

    run-unittests.sh \
        tests/test_utils/ \
        tests/test_mmd_resolver.py \
        tests/test_builder/test_koji.py::TestKojiBuilder::test_tag_to_repo

Inside Vagrant machine
----------------------

You can run tests with either SQLite or PostgreSQL as well. The former is default.

To start to run tests with SQLite, you could simply run ``py.test`` or ``tox``.

To start to run tests with PostgreSQL, set environment variable
``DATABASE_URI`` before running ``py.test`` or ``tox``::

    export DATABASE_URI=postgresql+psycopg2://postgres:@127.0.0.1/mbstest

Style Guide
===========

Automatically Checked
---------------------

The codebase conforms to the style enforced by ``flake8`` with the following exceptions:

- The maximum line length allowed is 100 characters instead of 80 characters
- The use of lambda functions are allowed (ignoring E731)
- Line breaks should occur before a binary operator (ignoring W503)

These rules are enforced by running ``tox -e flake8`` on pull-requests.

Requires Manual Review
----------------------

In addition to the ``flake8`` rules, **double quotes** are used over single quotes. If the string
contains double quotes in it, then single quotes may be used to avoid escaping.

Also, the format of the docstrings should be in the
`Sphinx style <http://www.sphinx-doc.org/en/master/usage/restructuredtext/domains.html>`_ such as:

::

    Calculate the sum of two numbers.

    :param int a: the first number to add
    :param int b: the second number to add
    :return: the sum of a and b
    :rtype: int
    :raises TypeError: if a or b are not integers


Additionally, the imports should be ordered by standard library, third-party, then local.
``from __future__ import absolute_import`` should always be included so that imports are consistent
in Python 2 and Python 3. For example:

::

    from __future__ import absolute_import
    import math
    import os

    import flask
    import requests

    import module_build_service.web
    from module_build_service.common.errors import ValidationError


Lastly, hanging indentation should be avoided when possible. For example:

::

    # Preferred
    def get_module_build_dependencies(
        self, name=None, stream=None, version=None, context=None, mmd=None, strict=False
    ):
        pass

    # Should be avoided
    def get_module_build_dependencies(self, name=None, stream=None, version=None,
                                      context=None, mmd=None, strict=False):
        pass

Development
===========

In most cases, you don't need to deploy your development instance. Please,
refer to the `Running Tests`_ section first.

The easiest way to setup a development environment is by using ``vagrant``. You can see instructions
about it below.

Vagrant
-------

If you are using VirtualBox, you will need to install the Vagrant plugin
``vagrant-vbguest``. This plugin automatically installs guest additions to
Vagrant guests that do not have them installed. The official Fedora Vagrant
box unfortunately does not contain the guest additions, and they are needed
for folder syncing::

    $ vagrant plugin install vagrant-vbguest

If you are using libvirt, then folder syncing will be done using SSHFS. To
install this on Fedora, use:

    $ dnf install vagrant-sshfs

If you are using libvirt but not using Fedora, you can install the plugin
directly in Vagrant using:

    $ vagrant plugin install vagrant-sshfs

To launch Vagrant, run (depending on your OS, you may need to run it with sudo)::

    $ vagrant up

This will start module_build_service's frontend (API) and scheduler. To
access the frontend, visit the following URL::

    https://127.0.0.1:5000/module-build-service/1/module-builds/

At any point you may enter the guest VM with::

    $ vagrant ssh

The outputs of running services can be tailed as follows::

    $ tail -f /tmp/*.out &

To start the frontend manually, run the following inside the guest::

    $ mbs-frontend

To start the scheduler manually, run the following at
``/opt/module_build_service`` inside the guest::

    $ fedmsg-hub

Alternatively, you can restart the Vagrant guest, which inherently
starts/restarts the frontend and the scheduler with::

    $ vagrant reload

Logging
-------

If you're running module_build_service from scm, then the DevConfiguration
from ``module_build_service/common/config.py`` which contains ``LOG_LEVEL=debug`` should get
applied. See more about it in ``module_build_service/common/config.py``,
``app.config.from_object()``.

Environment
-----------

The environment variable ``MODULE_BUILD_SERVICE_DEVELOPER_ENV``, which if
set to "1", indicates to the Module Build Service that the development
configuration should be used. Vagrant already runs with this environment variable set.
This overrides all configuration settings and forces usage of DevConfiguration section
in ``conf/config.py`` from MBS's develop instance.

Prior to starting MBS, you can force development mode::

    $ export MODULE_BUILD_SERVICE_DEVELOPER_ENV=1

Database Model Changes
----------------------

When making changes to any of the database models, a corresponding migration
script must be created. To generate one, run the following::

    # Stash any changes you may have
    $ git stash
    # Switch to the master branch
    $ git checkout master
    # Generate a database file with the current schema
    $ MODULE_BUILD_SERVICE_DEVELOPER_ENV=1 mbs-manager upgradedb
    # Switch back to your branch, if applicable
    $ git checkout <my-branch>
    # Restore your changes if they were previously stashed
    $ git stash pop
    # Finally, generate the migration script
    $ MODULE_BUILD_SERVICE_DEVELOPER_ENV=1 mbs-manager db migrate

These steps will generate a new file under ``module_build_service/migrations/versions/``.
Rename the file to a meaningful value. For example::

    $ mv a3afae7b01f8_.py a3afae7b01f8_add_spam_build.py

Please, inspect the file for correctness and adjust it according to the style guide.
This file should be part of the commit that is modifying the database model.

PEP 8
=====

Following PEP 8 is highly recommended and all patches and future code
changes shall be PEP 8 compliant to keep at least constant or decreasing
number of PEP 8 violations.

Historical Names of Module Build Service
========================================

- Rida
- The Orchestrator

Updating test images in Quay
============================

The docker images inside which to run tests could be built locally or via Quay
web UI.

For building locally, use ``podman build`` or ``docker build``. For example
with ``podman``::

    $ podman build -t quay.io/factory2/mbs-test-centos -f docker/Dockerfile-tests .

or::

    $ podman build -t quay.io/factory2/mbs-test-fedora -f docker/Dockerfile-tests-py3 .

To update the images used for testing via Quay web UI:

* https://quay.io/repository/factory2/mbs-test-centos
* https://quay.io/repository/factory2/mbs-test-fedora

Members of `the factory2 Quay organization <https://quay.io/organization/factory2>`_ 
can start a new build from the *Builds* page of the above repositories. 
The `:latest` tags need to be applied to the new images on the *Tags* page 
after the builds complete.

We plan to automate the process above in the future.
