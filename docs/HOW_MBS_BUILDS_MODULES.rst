How does MBS build modules?
===========================

This document describes how modules are built internally in MBS. The goal of this document is
to explain code-flow of module builds. It assumes everything goes as expected and does not
mention any error handling or corner cases.


User submits module build request
---------------------------------

There is the MBS frontend, which provides a REST API (See ``views.py``). A user sends a POST request
with JSON describing the module to build. There is mainly the URL to the git repository (called
``scmurl`` |---| which points to the git repository containing the modulemd file defining the module)
and branch name (called ``branch``).

This JSON data is handled by ``views.SCMHandler``, which validates the JSON and calls
``utils.submit.submit_module_build_from_scm(...)`` method. This goes down to
``submit_module_build(...)``.

Alternatively, if submitting a YAML modulemd file is allowed (MBS setting
``YAML_SUBMIT_ALLOWED`` is ``True``), the user can include it in the JSON data
(called ``modulemd`` and ``module_name``) or send a ``multipart/form-data``
POST request directly including the contents of a YAML modulemd file
(called ``yaml``). In this case, the JSON data and YAML file are handled by
``views.YAMLFileHandler``, which validates the data and calls the
``utils.submit.submit_module_build_from_yaml(...)`` method which also goes down
to ``submit_module_build(...)``.

If module scratch builds are allowed (MBS setting ``MODULES_ALLOW_SCRATCH`` is
``True``), the user can request a scratch module build (called ``scratch``).
With a scratch build request, the user can include a YAML modulemd file
(see above) and also upload one or more source RPMs to Koji
via calls to Koji's ``session.uploadWrapper(..)``, and supply the list of
upload links to MBS (called ``srpms``). Such custom SRPMs will be used to
override the git repository source for corresponding components.


Module Stream Expansion (MSE)
-----------------------------

The first thing done in ``submit_module_build(...)`` is Module Stream Expansion (MSE).

The submitted modulemd file might have buildrequires and requires pairs defined in an ambiguous way.
For example the module can buildrequire ``platform:[f28, f29]`` modules, which means it should
be built against the ``f28`` and ``f29`` streams of ``platform`` module.

The process of resolving these ambiguous buildrequires and requires is called Module Stream
Expansion.

Input for this process is the submitted modulemd file with ambiguous buildrequires/requires.
Output of this process is the list of multiple modulemd files with all the ambiguous
buildrequires/requires resolved.

This all happens in ``utils.mse.generate_expanded_mmds(...)`` method.

At first, this method finds out all the possible buildrequires/requires for the input module.
This is done using ``DBResolver`` which simply finds out the modules in the MBS database.
In our case, it would list all the ``platform:f28`` and ``platform:f29`` modules.

It then uses the ``MMDResolver`` class to find all the possible combinations of buildrequires/requires
against which the input module can be built.

In our case, it would generate two expanded modulemd files (one for each platform stream) which
would be identical to the input modulemd file with only the following exceptions:

- The buildrequires/requires pairs from the input modulemd files will be replaced by the particular
  combination returned by ``MMDResolver``
- The ``xmd`` section of generated modulemd files will contain ``buildrequires`` list which lists all
  the modules required to build this expanded modulemd file. Requirements are traversed to produce
  a transitive list that includes all ``requires`` of each ``buildrequires`` entry.  This is used later
  by MBS.
- The context is computed and filled for each expanded modulemd file. It is based on the
  expanded buildrequires and requires pairs. See ``models.ModuleBuild.contexts_from_mmd(...)``.

Such expanded modulemd files are then added to database as the next step in
``submit_module_build(...)`` and are handled as separate module builds later in MBS.

The ``submit_module_build(...)`` then moves the module builds to "init" state and sends a message on
the configured message bus.


Backend handles module moved to the "init" state
------------------------------------------------

When module build is moved to the "init" state, the backend handles that in the
``scheduler.handlers.modules.init(...)`` method.

This method calls ``utils.submit.record_component_builds`` which reads the modulemd file
stored in the database by the frontend and records all the components (future RPM packages) in the
database.

The components are divided into the **batches** based on their buildorder in the modulemd file.

Once the components which are supposed to be built as part of this module build are recorded,
the module moves to the "wait" state and another message is sent on the message bus.


Backend handles module moved to the "wait" state
------------------------------------------------

When the module build is moved to the "wait" state, the backend handles that in the
``scheduler.handlers.modules.wait(...)`` method.

At first, this method uses KojiModuleBuilder to generate the Koji tag in which the components will be
built. The Koji tag reflects the buildrequires of the module by inheriting their Koji tags. In our
case, the Koji tag would inherit just the ``platform:f28`` or ``platform:f29`` Koji tag, because that's
the only buildrequired module we have.
The list of modules buildrequired by the currently building module is determined by the ``buildrequires`` list in
the ``xmd`` section of the expanded modulemd file.

Once the Koji tag is ready, it tries to build a synthetic ``module-build-macros`` package. This
package contains special build macros which for example, define the dist-tag for built RPMs, ensure
that filtered packages are not installed into the buildroot and so on.

The module-build-macros package is always built in the first batch.


Module-build-macros package is built
------------------------------------

Once the ``module-build-macros`` package is built, Koji sends a message on the message bus, which is
handled in the ``scheduler.handlers.components.complete(...)`` method.

This method changes the state of that component build in the MBS database to "complete".

It then checks if there are any other unbuilt components in the current batch. Because the
``module-build-macros`` package is the only component in batch 1, it can continue tagging it
into the Koji tag representing the module, so the ``module-build-macros`` can be later
installed during the build of the next batch of components and can influence them.

Note that the ``module-build-macros`` package is the only package in the course of a module build that
*only* gets tagged into the build tag.  All other builds are tagged both into the build tag (to
influence subsequent component builds) and into the destination tag (to be delivered as a component
in the module).


Module-build-macros package is tagged into the Koji tag
-------------------------------------------------------

Once the module-build-macros package is tagged by Koji, the ``scheduler.handlers.tags.tagged(...)``
method is called.

This simply waits until all the components in a currently built batch are tagged in a Koji tag.

Because module-build-macros is the only component in batch 1, it can continue by regenerating
the Koji repository based on a tag, so the newly built packages (just module-build-macros
in our case), can be installed from that repository when building the next components in a module.


Koji repository is regenerated
------------------------------

Once the Koji repository containing packages from the currently built batch is regenerated,
the ``scheduler.handlers.repos.done(...)`` method is called.

This verifies that all the packages from the current batch (just module-build-macros for now)
really appear in the generated repository and if so, it starts building the next batch by calling
``module_build_service.scheduler.batches.start_next_batch_build(...)``.


Building the next batch
-----------------------

The ``start_next_batch_build(...)`` increases the ``ModuleBuild.batch`` counter to note that it
is going to build the next batch with the next component builds.

It then generates the list of unbuilt components in the batch and tries to reuse some from
previous module builds. This can happen for example when the component is built from the
same source as previously, no component builds in previous batches changed and the
buildrequires/requires of the current module build are still the same as previously.

For components which cannot be reused, it submits them to Koji.


Build all components in all batches in a module
-----------------------------------------------

The process for every component build is the same as for module-build-macros.

MBS builds it in Koji. Once all the components in the current batch are built, MBS tags them into
the Koji tag. Once they are tagged, it regenerates the Koji tag repository and then starts
building next batch.

Rinse and repeat!  This process is repeated until all the batches are complete.


Last component is built
-----------------------

Once the last component is built and the repository is regenerated, the
``scheduler.handlers.repos.done(...)`` method moves the module build to the "done" state.


Importing the module build to Koji
----------------------------------

The "done" state message is handled in the ``scheduler.handlers.modules.done(...)`` method.

This method imports the module build into Koji using the ``KojiContentGenerator`` class.
The module build in Koji points to the Koji tag with the module's components and also contains the
final modulemd files generated for earch architecture the module is built for.

.. |---| unicode:: U+2014  .. em dash, trimming surrounding whitespace
   :trim:
