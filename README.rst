The Module Build Service (MBS) for Modularity
=============================================

The MBS coordinates module builds and is responsible for a number of
tasks:

- Providing an interface for module client-side tooling via which module build
  submission and build state queries are possible.
- Verifying the input data (modulemd, RPM SPEC files and others) is available
  and correct.
- Preparing the build environment in the supported build systems, such as koji.
- Scheduling and building of the module components and tracking the build
  state.
- Emitting bus messages about all state changes so that other infrastructure
  services can pick up the work.


Change Log
==========

For a detailed change log, see |docs/CHANGELOG.rst|_.

.. |docs/CHANGELOG.rst| replace:: ``docs/CHANGELOG.rst``
.. _docs/CHANGELOG.rst: docs/CHANGELOG.rst

Contributing
============

For detailed information on how to contribute, see |docs/CONTRIBUTING.rst|_.

.. |docs/CONTRIBUTING.rst| replace:: ``docs/CONTRIBUTING.rst``
.. _docs/CONTRIBUTING.rst: docs/CONTRIBUTING.rst

Rebuild Strategies
==================

For detailed information on rebuild strategies, see |docs/REBUILD_STRATEGIES.rst|_.

.. |docs/REBUILD_STRATEGIES.rst| replace:: ``docs/REBUILD_STRATEGIES.rst``
.. _docs/REBUILD_STRATEGIES.rst: docs/REBUILD_STRATEGIES.rst

Supported build systems
=======================

Koji
----

Koji is the software that builds RPM packages from source tarballs and
SPEC files. It uses its own Mock to create chroot environments to
perform builds.

MBS comes with its own ``koji.conf`` config file which allows configuring
for your custom Koji instance(s).

Mock
----

Mock is a simple program that will build source RPMs inside a chroot. It
doesn't do anything terribly fancy other than populate a chroot with the
contents specified by a configuration file, then build any input SRPM(s)
in that chroot.

MBS is able to perform local module builds by directing local Mock.

MBS supports threaded Mock builds which utilizes performance and
significantly speeds up local module builds.

_`Client tooling`
=================

``mbs-manager``
---------------

This command controls the MBS instance itself.

There are subcommands for running the MBS server, performing database
migrations, generating certificates and submitting local module
builds. For more info, there's an existing help available.

Client-side API
===============

The MBS implements a RESTful interface for module build submission and state
querying. Not all REST methods are supported. See below for details. For client
tooling which utilizes the API, please refer to `Client tooling`_ section.

Module build submission
-----------------------

Module submission is done via posting the modulemd SCM URL.

::

    POST /module-build-service/1/module-builds/

::

    {
        "scmurl": "https://src.fedoraproject.org/modules/foo?#f1d2d2f924e986ac86fdf7b36c94bcdf32beec15",
        "branch": "master"
    }

The response, in case of a successful submission, would include the task ID.

::

    HTTP 201 Created

::

    {
        "id": 42,
        "state": "wait",
        ...
    }

Options:

- ``buildrequire_overrides`` - the buildrequires to override the modulemd with. The overrides must
  be to existing buildrequires on the modulemd. The expected format is
  ``{'platform': ['f28', 'f29']}``.
- ``modulemd`` - a string for submitting a YAML modulemd file as a parameter in the JSON data as
  an alternative to sending it in a ``multipart/form-data`` request. Only allowed if
  ``scratch`` is ``True`` or if the MBS setting ``YAML_SUBMIT_ALLOWED`` is ``True``.
- ``module_name`` - a string to use as the module name if a scratch build is requested and the
  YAML modulemd is submitted using the ``modulemd`` parameter.
  - ``module_stream`` - a string to use as the stream name if a scratch build is requested and the
  YAML modulemd is submitted using the ``modulemd`` parameter.
- ``rebuild_strategy`` - a string of the desired rebuild strategy (affects what components get
  rebuilt). For the available options, please look at the "Rebuild Strategies" section below.
- ``require_overrides`` - the requires to override the modulemd with. The overrides must be to
  existing requires on the modulemd. The expected format is ``{'platform': ['f28', 'f29']}``.
- ``reuse_components_from`` - the ID or NSVC of the module build to reuse components from. If it's
  not set, MBS will try to find a compatible module build to reuse components from.
- ``scratch`` - a boolean indicating if a scratch module build should be performed.
  Only allowed to be ``True`` if the MBS setting ``MODULES_ALLOW_SCRATCH`` is ``True``.
- ``srpms`` - an optional list of Koji upload URLs of SRPMs to include in a module scratch build.
  Only allowed if ``scratch`` is ``True``.
- ``yaml`` - a string of the input file when submitting a YAML modulemd file directly in a
  ``multipart/form-data`` request. Only allowed if ``scratch`` is ``True`` or if the MBS
  setting ``YAML_SUBMIT_ALLOWED`` is ``True``. The basename of the file will be used as
  the module name.


Module build state query
------------------------

Once created, the client can query the current build state by requesting the
build task's URL.

::

    GET /module-build-service/1/module-builds/1042

The response, if the task exists, would include various pieces of information
about the referenced build task.

::

    HTTP 200 OK

::

    {
      "id": 1042,
      "koji_tag": "module-f8c7dcdcc884bf1d",
      "name": "cloud-init",
      "owner": "karsten",
      "scmurl": "https://src.fedoraproject.org/modules/cloud-init?#d5fc9ab58f359b618e67ebdd0c7b143962242546",
      "state": 5,
      "state_name": "ready",
      "state_reason": null,
      "stream": "master",
      "tasks": {
        "rpms": {
          "cloud-init": {
            "nvr": "cloud-init-0.7.9-9.module_f8c7dcdc",
            "state": 1,
            "state_reason": "",
            "task_id": 22264880
          },
          "module-build-macros": {
            "nvr": "module-build-macros-0.1-1.module_f8c7dcdc",
            "state": 1,
            "state_reason": "",
            "task_id": 22264426
          },
          "pyserial": {
            "nvr": "pyserial-3.1.1-5.module_f8c7dcdc",
            "state": 1,
            "state_reason": "",
            "task_id": 22264727
          },
          ...
        }
      },
      "time_completed": "2017-10-05T11:58:44Z",
      "time_modified": "2017-10-05T11:58:58Z",
      "time_submitted": "2017-10-05T11:37:39Z",
      "version": "20171005113458"
    }

The response includes:

- ``id`` - the ID of the module build.
- ``koji_tag`` - the Koji tag the component builds are tagged in.
- ``name`` - the name of the module.
- ``owner`` - the username of the owner or person who submitted the module build.
- ``scmurl`` - the source control URL used to build the module.
- ``state`` - the numerical state of the module build.
- ``state_name`` - the named state of the module build. See the section called
  `Module Build States`_ for more information.
- ``state_reason`` - the reason why the module build is in this state. This is useful
  when the build fails.
- ``stream`` - the module's stream.
- ``tasks`` - a dictionary of information about the individual component builds.
- ``time_completed`` - Zulu ISO 8601 timestamp of when the module build completed.
- ``time_modified`` - Zulu ISO 8601 timestamp of when the module build was last modified.
- ``time_submitted`` - Zulu ISO 8601 timestamp of when the module build was submitted.
- ``version`` - the module build's version.


Listing all module builds
-------------------------

The list of all tracked builds and their states can be obtained by
querying the "module-builds" resource.
There are a number of configurable GET parameters to change how the
module builds are displayed. These parameters are:

- ``verbose`` - Shows the builds with additional detail such as the modulemd
  and state trace (i.e. ``verbose=True``). This value defaults to ``False``.
- ``short`` - Shows the builds with a minimum amount of information
  (i.e. ``short=True``). This value defaults to ``False``.
- ``page`` - Specifies which page should be displayed (e.g. ``page=3``). This
  value defaults to 1.
- ``per_page`` - Specifies how many items per page should be displayed
  (e.g. ``per_page=20``). This value defaults to 10.
- ``order_by`` - a database column to order the API by in ascending order. Multiple can be provided.
- ``order_desc_by`` - a database column to order the API by in descending order. Multiple can be
  provided. This defaults to ``id``.

An example of querying the "module-builds" resource with the "per_page" and the "page"
parameters::

    GET /module-build-service/1/module-builds/?per_page=2&page=1

::

    HTTP 200 OK

::

    {
      "items": [
        {
          "id": 124,
          "koji_tag": "module-de66baf89b40367c",
          "name": "testmodule",
          "owner": "mprahl",
          "scmurl": "https://src.fedoraproject.org/modules/testmodule.git?#86d9cfe53d20118d863ae051641fc3784d91d981",
          "state": 5,
          "state_name": "ready",
          "state_reason": null,
          "stream": "master",
          "tasks": {
            "rpms": {
              "ed": {
                "nvr": "ed-1.14.1-4.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22267993
              },
              "mksh": {
                "nvr": "mksh-56b-1.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22268059
              }
            }
          },
          "time_completed": "2017-10-05T18:45:56Z",
          "time_modified": "2017-10-05T18:46:10Z",
          "time_submitted": "2017-10-05T18:34:39Z",
          "version": "20171005183359"
        },
        {
          "id": 123,
          "koji_tag": "module-4620ad476f3d2b5c",
          "name": "testmodule",
          "owner": "mprahl",
          "scmurl": "https://src.fedoraproject.org/modules/testmodule.git?#373bb6eccccbfebbcb222a2723e643e7095c7973",
          "state": 5,
          "state_name": "ready",
          "state_reason": null,
          "stream": "master",
          "tasks": {
            "rpms": {
              "ed": {
                "nvr": "ed-1.14.1-4.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22267993
              },
              "mksh": {
                "nvr": "mksh-56b-1.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22268059
              }
            }
          },
          "time_completed": "2017-10-05T18:45:50Z",
          "time_modified": "2017-10-05T18:46:01Z",
          "time_submitted": "2017-10-05T18:24:09Z",
          "version": "20171005182359"
        }
      ],
      "meta": {
        "first": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?per_page=2&page=1",
        "last": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?per_page=2&page=340",
        "next": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?per_page=2&page=2",
        "page": 1,
        "pages": 60,
        "per_page": 2,
        "prev": null,
        "total": 120
      }
    }


An example of querying the "module-builds" resource with the "verbose", "per_page", and the "page"
parameters::

    GET /module-build-service/1/module-builds/?per_page=2&page=1?verbose=true

::

    HTTP 200 OK

::

    {
      "items": [
        {
          "base_module_buildrequires": [
            {
              "context": "00000000",
              "name": "platform",
              "stream": "f29",
              "stream_version": 290000,
              "version": "5"
            }
          ],
          "component_builds": [
            57047,
            57048
          ],
          "id": 124,
          "koji_tag": "module-de66baf89b40367c",
          "modulemd": "...."
          "name": "testmodule",
          "owner": "mprahl",
          "reused_module_id": 121,
          "scmurl": "https://src.fedoraproject.org/modules/testmodule.git?#86d9cfe53d20118d863ae051641fc3784d91d981",
          "state": 5,
          "state_name": "ready",
          "state_reason": null,
          "state_trace": [
            {
              "reason": null,
              "state": 1,
              "state_name": "wait",
              "time": "2017-10-05T18:34:50Z"
            },
            ...
          ],
          "state_url": "/module-build-service/1/module-builds/1053",
          "stream": "master",
          "tasks": {
            "rpms": {
              "ed": {
                "nvr": "ed-1.14.1-4.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22267993
              },
              "mksh": {
                "nvr": "mksh-56b-1.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22268059
              }
            }
          },
          "time_completed": "2017-10-05T18:45:56Z",
          "time_modified": "2017-10-05T18:46:10Z",
          "time_submitted": "2017-10-05T18:34:39Z",
          "version": "20171005183359"
        },
        {
          "component_builds": [
            57045,
            57046
          ],
          "id": 123,
          "koji_tag": "module-4620ad476f3d2b5c",
          "modulemd": "...."
          "name": "testmodule",
          "owner": "mprahl",
          "scmurl": "https://src.fedoraproject.org/modules/testmodule.git?#373bb6eccccbfebbcb222a2723e643e7095c7973",
          "state": 5,
          "state_name": "ready",
          "state_reason": null,
          "state_trace": [
            {
              "reason": null,
              "state": 1,
              "state_name": "wait",
              "time": "2017-10-05T18:24:19Z"
            },
            ...
          ],
          "state_url": "/module-build-service/1/module-builds/1052",
          "stream": "master",
          "tasks": {
            "rpms": {
              "ed": {
                "nvr": "ed-1.14.1-4.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22267993
              },
              "mksh": {
                "nvr": "mksh-56b-1.module_d2a2f5c8",
                "state": 1,
                "state_reason": "Reused component from previous module build",
                "task_id": 22268059
              }
            }
          },
          "time_completed": "2017-10-05T18:45:50Z",
          "time_modified": "2017-10-05T18:46:01Z",
          "time_submitted": "2017-10-05T18:24:09Z",
          "version": "20171005182359"
        }
      ],
      "meta": {
        "first": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?verbose=true&per_page=2&page=1",
        "last": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?verbose=true&per_page=2&page=340",
        "next": "http://mbs.fedoraproject.org/module-build-service/1/module-builds/?verbose=true&per_page=2&page=2",
        "page": 1,
        "pages": 120,
        "per_page": 2,
        "prev": null,
        "total": 60
      }
    }

Filtering module builds
-----------------------

The module builds can be filtered by a variety of GET parameters. Some of these
parameters include:

- ``base_module_br`` - the name:stream:version:context of a base module the module buildrequires
- ``base_module_br_context`` - the context of a base module the module buildrequires
- ``base_module_br_name`` - the name of a base module the module buildrequires
- ``base_module_br_stream`` - the stream of a base module the module buildrequires
- ``base_module_br_stream_version`` - the stream version of a base module the module buildrequires
- ``base_module_br_stream_version_lte`` - less than or equal to the stream version of a base module
  the module buildrequires
- ``base_module_br_stream_version_gte`` - greater than or equal to the stream version of a base
  module the module buildrequires
- ``base_module_br_version`` - the version of a base module the module buildrequires
- ``batch``
- ``cg_build_koji_tag``
- ``completed_after`` - Zulu ISO 8601 format e.g. ``completed_after=2016-08-23T09:40:07Z``
- ``completed_before`` - Zulu ISO 8601 format e.g. ``completed_before=2016-08-22T09:40:07Z``
- ``koji_tag``
- ``modified_after`` - Zulu ISO 8601 format e.g. ``modified_after=2016-08-22T09:40:07Z``
- ``modified_before`` - Zulu ISO 8601 format e.g. ``modified_before=2016-08-23T09:40:07Z``
- ``name``
- ``new_repo_task_id``
- ``owner``
- ``rebuild_strategy``
- ``reuse_components_from`` - the compatible module that was used for component reuse
- ``scmurl``
- ``state`` - Can be the state name or the state ID e.g. ``state=done``. This
  parameter can be given multiple times, in which case or-ing will be used.
- ``state_reason``
- ``stream``
- ``stream_version_lte`` - less than or equal to the stream version. This is limited to
  the major version. This value only applies to base modules.
- ``submitted_after`` - Zulu ISO 8601 format e.g. ``submitted_after=2016-08-22T09:40:07Z``
- ``submitted_before`` - Zulu ISO 8601 format e.g. ``submitted_before=2016-08-23T09:40:07Z``
- ``version``

An example of querying the "module-builds" resource with the "state",
and the "submitted_before" parameters::

    GET /module-build-service/1/module-builds/?state=done&submitted_before=2016-08-23T08:10:07Z

::

    HTTP 200 OK

::

    {
      "items": [
        {
          "id": 3,
          "state": 3,
          ...
        },
        {
          "id": 2,
          "state": 3,
          ...
        },
        {
          "id": 1,
          "state": 3,
          ...
        }
      ],
      "meta": {
        "first": "https://127.0.0.1:5000/module-build-service/1/module-builds/?per_page=10&page=1",
        "last": "https://127.0.0.1:5000/module-build-service/1/module-builds/?per_page=10&page=1",
        "page": 1,
        "pages": 1,
        "per_page": 3,
        "total": 3
      }

Component build state query
---------------------------

Getting particular component build is very similar to a module build query.

::

    GET /module-build-service/1/component-builds/1

The response, if the build exists, would include various pieces of information
about the referenced component build.

::

    HTTP 200 OK

::

    {
      "format": "rpms",
      "id": 854,
      "module_build": 42,
      "nvr": "pth-1-1",
      "package": "pth",
      "state": 1,
      "state_name": "COMPLETE",
      "state_reason": "",
      "task_id": 18367215
    }


The response includes:

- ``id`` - the ID of the component build.
- ``format`` - typically "rpms".
- ``nvr`` - the NVR of the component build.
- ``package`` - the package name.
- ``state`` - the numerical state of the component build.
- ``state_name`` - the named component build state and can be "COMPLETE",
  "FAILED", or "CANCELED".
- ``state_reason`` - the reason why the component build is in this state. This is useful
  when the build fails.
- ``task_id`` - the related task ID in the backend buildsystem.


Listing component builds
------------------------

An example of querying the "component-builds" resource without any additional
parameters::

    GET /module-build-service/1/component-builds/

::

    HTTP 200 OK

::

    {
      "items": [
        {
          "format": "rpms",
          "id": 854,
          "module_build": 42,
          "package": "pth",
          "state": 1,
          "state_name": "COMPLETE",
          "state_reason": "",
          "state_trace": [
            {
              "reason": "Submitted pth to Koji",
              "state": 0,
              "state_name": "init",
              "time": "2017-03-14T00:07:43Z"
            },
            {
              "reason": "",
              "state": 1,
              "state_name": "wait",
              "time": "2017-03-14T00:13:30Z"
            },
            {
              "reason": "",
              "state": 1,
              "state_name": "wait",
              "time": "2017-03-14T14:41:21Z"
            }
          ],
          "task_id": 18367215
        },
        ...
      ],
      "meta": {
        "first": "http://mbs.fedoraproject.org/module-build-service/1/component-builds/?per_page=10&page=1",
        "last": "http://mbs.fedoraproject.org/module-build-service/1/component-builds/?per_page=10&page=5604",
        "next": "http://mbs.fedoraproject.org/module-build-service/1/component-builds/?per_page=10&page=2",
        "page": 1,
        "pages": 5604,
        "per_page": 10,
        "prev": null,
        "total": 56033
      }
    }



Filtering component builds
--------------------------

The component builds can be filtered by a variety of GET parameters. Some of these
parameters include:

- ``batch``
- ``build_time_only`` - boolean e.g. "true" or "false"
- ``format``
- ``module_id`` or ``module_build``
- ``nvr``
- ``package``
- ``ref``
- ``scmurl``
- ``state`` - Can be the state name or the state ID. Koji states are used
  for resolving to IDs. This parameter can be given multiple times, in which
  case or-ing will be used.
- ``state_reason``
- ``tagged`` - boolean e.g. "true" or "false"
- ``tagged_in_final`` - boolean e.g. "true" or "false"
- ``task_id``


Import module
-------------

Importing of modules is done via posting the SCM URL of a repository
which contains the generated modulemd YAML file. Name, stream, version,
context and other important information must be present in the metadata.

::

    POST /module-build-service/1/import-module/

::

    {
      "scmurl": "https://src.fedoraproject.org/modules/foo.git?#21f92fb05572d81d78fd9a27d313942d45055840"
    }


If the module build is imported successfully, JSON containing the most
important information is returned from MBS. The JSON also contains log
messages collected during the import.

::

    HTTP 201 Created

::

    {
      "module": {
        "component_builds": [],
        "context": "00000000",
        "id": 3,
        "koji_tag": "",
        "name": "mariadb",
        "owner": "mbs_import",
        "rebuild_strategy": "all",
        "scmurl": null,
        "siblings": [],
        "state": 5,
        "state_name": "ready",
        "state_reason": null,
        "stream": "10.2",
        "time_completed": "2018-07-24T12:58:14Z",
        "time_modified": "2018-07-24T12:58:14Z",
        "time_submitted": "2018-07-24T12:58:14Z",
        "version": "20180724000000"
      },
      "messages": [
        "Updating existing module build mariadb:10.2:20180724000000:00000000.",
        "Module mariadb:10.2:20180724000000:00000000 imported"
      ]
    }


If the module import fails, an error message is returned.

::

    HTTP 422 Unprocessable Entity

::

    {
      "error": "Unprocessable Entity",
      "message": "Incomplete NSVC: None:None:0:00000000"
    }


Listing about
-------------

This API shows information about the MBS server::

    GET /module-build-service/1/about/

::

    HTTP 200 OK

::

    {
      "auth_method": "oidc",
      "version": "1.3.26"
    }


HTTP Response Codes
-------------------

Possible response codes are for various requests include:

- HTTP 200 OK - The task exists and the query was successful.
- HTTP 201 Created - The module build task was successfully created.
- HTTP 400 Bad Request - The client's input isn't a valid request.
- HTTP 401 Unauthorized - No 'authorization' header found.
- HTTP 403 Forbidden - The SCM URL is not pointing to a whitelisted SCM server.
- HTTP 404 Not Found - The requested URL has no handler associated with it or
  the requested resource doesn't exist.
- HTTP 409 Conflict - The submitted module's NVR already exists.
- HTTP 422 Unprocessable Entity - The submitted modulemd file is not valid or
  the module components cannot be retrieved
- HTTP 500 Internal Server Error - An unknown error occured.
- HTTP 501 Not Implemented - The requested URL is valid but the handler isn't
  implemented yet.
- HTTP 503 Service Unavailable - The service is down, possibly for maintanance.

_`Module Build States`
----------------------

You can see the list of possible states with::

    from module_build_service.common.models import BUILD_STATES
    print(BUILD_STATES)

Here's a description of what each of them means:

init
~~~~

This is (obviously) the first state a module build enters.

When a user first submits a module build, it enters this state. We parse the
modulemd file, learn the NVR, and create a record for the module build.

Then, we validate that the components are available, and that we can fetch
them. If this is all good, then we set the build to the 'wait' state. If
anything goes wrong, we jump immediately to the 'failed' state.

wait
~~~~

Here, the scheduler picks up tasks in wait and switches to build immediately.
Eventually, we'll add throttling logic here so we don't submit too many
builds for the build system to handle.

build
~~~~~

The scheduler works on builds in this state. We prepare the buildroot, submit
builds for all the components, and wait for the results to come back.

done
~~~~

Once all components have succeeded, we set the top-level module build to 'done'.

failed
~~~~~~

If any of the component builds fail, then we set the top-level module
build to 'failed' also.

ready
~~~~~

This is a state to be set when a module is ready to be part of a
larger compose. perhaps it is set by an external service that knows
about the Grand Plan.

Bus messages
============

Supported messaging backends:

- fedmsg - Federated Messaging with ZeroMQ
- in_memory - Local/internal messaging only
- amq - Apache ActiveMQ

Message Topic
-------------

The suffix for message topics concerning changes in module state is
``module.state.change``. Currently, it is expected that these messages are sent
from koji or module_build_service_daemon, i.e. the topic is prefixed with
``*.buildsys.`` or ``*.module_build_service.``, respectively.

Message Body
------------

The message body is a dictionary with these fields:

``state``
~~~~~~~~~

This is the current state of the module, corresponding with the states
described above in `Module Build States`_.

``name``, ``version``, ``release``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Name, version and release of the module.

``scmurl``
~~~~~~~~~~

Specifies the exact repository state from which a module is built.

E.g. ``"scmurl": "https://src.stg.fedoraproject.org/modules/testmodule.git?#020ea37251df5019fde9e7899d2f7d7a987dfbf5"``

``topdir``
~~~~~~~~~~

The toplevel directory containing the trees for each architecture of a module.
This field is only present when a module finished building, i.e. with the
states 'done' or 'ready'.

Configuration
=============

MBS configures itself according to the environment where it runs + according to
the following rules (all of them are evaluated from top to bottom):

- DevConfiguration is the initial configuration chosen.
- If configuration file is found within its final installation location,
  ProdConfiguration is assumed.
- If Flask app running within mod_wsgi is detected,
  ProdConfiguration is assumed.
- If environment variables determining configuration file/section are found,
  they are used for configuration. Following environment variables are
  recognized:

    - ``MBS_CONFIG_FILE``: Overrides default configuration file location,
      typically ``/etc/module-build-service/config.py``.
    - ``MBS_CONFIG_SECTION``: Overrides configuration section.

  It is possible to set these values in httpd using ``SetEnv``,
  anywhere in ``/etc/profile.d/`` etc.

- If test-runtime environment is detected,
  TestConfiguration is used, otherwise...
- if ``MODULE_BUILD_SERVICE_DEVELOPER_ENV`` is set to some reasonable
  value, DevConfiguration is forced and ``config.py`` is used directly from the
  MBS's develop instance. For more information see |docs/CONTRIBUTING.rst|_.

.. |docs/CONTRIBUTING.rst| replace:: ``docs/CONTRIBUTING.rst``
.. _docs/CONTRIBUTING.rst: docs/CONTRIBUTING.rst


Setting Up Kerberos + LDAP Authentication
=========================================

MBS defaults to using OIDC as its authentication mechanism. It additionally
supports Kerberos (through mod_auth_gssapi) + LDAP, where Kerberos proves the user's identity
and LDAP is used to determine the user's group membership. To configure this, the following
must be set in ``/etc/module-build-service/config.py``:

- ``AUTH_METHOD`` must be set to ``'kerberos'``.
- ``LDAP_URI`` is the URI to connect to LDAP (e.g. ``'ldaps://ldap.domain.local:636'``
  or ``'ldap://ldap.domain.local'``).
- ``LDAP_GROUPS_DN`` is the distinguished name of the container or organizational unit where groups
  are located (e.g. ``'ou=groups,dc=domain,dc=local'``). MBS does not search the tree below the
  distinguished name specified here for security reasons because it ensures common names are
  unique.
- ``ALLOWED_GROUPS`` and ``ADMIN_GROUPS`` both need to declare the common name of the LDAP groups,
  not the distinguished name.

Development
===========

For help on setting up a development environment, see |docs/CONTRIBUTING.rst|_.

License
=======

MBS is licensed under MIT license. See |LICENSE|_ file for details.

.. |LICENSE| replace:: ``LICENSE``
.. _LICENSE: LICENSE

Parts of MBS are licensed under 3-clause BSD license from:
https://github.com/projectatomic/atomic-reactor/blob/master/LICENSE


Virtual Modules
===============

For a detailed description of virtual modules, see |docs/VIRTUAL_MODULES.rst|_.

.. |docs/VIRTUAL_MODULES.rst| replace:: ``docs/VIRTUAL_MODULES.rst``
.. _docs/VIRTUAL_MODULES.rst: docs/VIRTUAL_MODULES.rst
