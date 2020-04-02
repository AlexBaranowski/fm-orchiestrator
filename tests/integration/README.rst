==============================================
Integration tests for the Module Build Service
==============================================

This directory stores the integration tests for MBS.

Configuration
=============

The tests should be configured by a ``test.env.yaml`` file placed in the
top-level directory of this repository. This can be changed to a different
path by setting ``MBS_TEST_CONFIG``.

Usually each test will trigger a new module build, and potentially wait until
it completes before doing the checks. In order to avoid waiting for this
during test development, an existing module build can be reused by specifying
a ``build_id`` for the test case.

See `tests/integration/example.test.env.yaml`_ for a complete list of
configuration options and examples.

Running the tests
=================

Tests can be triggered from the top-level directory of this repository with::

    tox -e integration

Note, that the ``integration`` environment is not part of the default ``tox``
envlist.

``REQUESTS_CA_BUNDLE`` is passed in ``tox.ini`` for the ``integration``
environment in order to enable running the tests against MBS instances which
have self-signed certificates. Example usage::

    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt tox -e integration

``MBS_TEST_WORKERS`` can be used to run the tests in parallel. For example to
have 4 tests running in parallel one could call::

    MBS_TEST_WORKERS=4 tox -e integration

Test case and test environment design
=====================================

Currently each test case is implemented in a separate file.

Test cases interact with the test environment, test configuration, and service
under test (SUT) through fixtures. These are defined in `conftest.py`_, and
`pytest takes care`_ to create them.

These fixtures usually instantiate a class from `utils.py`_. These classes are
intended to wrap the services or data the test cases need to interact with.
This wrapping creates a layer of abstraction which makes the test cases more
readable, and should also make updates easier, in case those services or data
change in the future.

Test cases should check for preconditions (if any) in the test data and test
environment. This helps to better understand test failures and debug failing
test cases.

.. _tests/integration/example.test.env.yaml: example.test.env.yaml
.. _conftest.py: conftest.py
.. _pytest takes care: https://docs.pytest.org/en/latest/fixture.html#conftest-py-sharing-fixture-functions
.. _utils.py: utils.py
