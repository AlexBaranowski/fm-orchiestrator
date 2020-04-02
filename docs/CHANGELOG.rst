Change Log
==========

v3.0.0
------
* Rearchitect MBS to support multiple backends using Celery
* Send module build state changes only after they are available via the REST API
* Set ``mock.yum.module_hotfixes = 1`` on created Koji build tags by default
* Use Z stream base module streams per the schedule in product pages as well as the
  existing behavior of past the GA date

v2.32.0
-------
* Fix the provides of base modules when they have a stream version
* Use gssapi if correct python-requests-kerberos is available
* Actually fail if dnf can't read the repo
* Make dnf timeout configurable

v2.31.0
-------
* Use jinja templates to provide 'full-jobs'
* Only allow cancelling module builds in the init, wait, and build states
* Remove the koji.ClientSession backport
* Gracefully handle builds without a koji tag
* Allow koji tags to be created with a configurable permission

v2.30.4
-------
* allow component reuse in some cases when a component is added

v2.30.3
-------
* Fix a local build bug caused by the refactoring of how database sessions are handled

v2.30.2
-------
* Fixed bugs that caused local builds to fail on Fedora 31

v2.30.1
-------
* Fixed a bug that caused local builds to fail depending on the version of DNF being used

v2.30.0
-------
* Remove Kerberos authentication directly performed by MBS (``mod_auth_gssapi`` is now required)
* Find the correct default modules based on the buildrequired base module (e.g. platform)
* Fix the name field in the modulemd text for ``-devel`` content generator builds
* The KojiResolver feature is now ready for testing
* Fixed a bug that caused local builds to fail

v2.29.1
-------
* Fix a fork in the database migration scripts

v2.29.0
-------
* Add buildonly support (#1307)
* Make the DNF minrate setting configurable when loading repos
* Load the DNF repos in parallel to improve speed
* Record the build_context without base modules
* Convert arch to Koji's canon arch for default modules
* Initial KojiResolver code

v2.28.2
-------
* Fix a bug that caused the code that handles the conflicts in base module modular Koji tags to not run
* Modifications to the stream when querying Product Pages are now on the GA date and not after

v2.28.1
-------
* Fix the ``list_of_args and list_of_kwargs must be list or None`` error when building a module

v2.28.0
-------
* Do not ignore building components to reset state when submit an existing module build
* Handle the conflicts between base module modular Koji tags everytime
* Fix DetachedInstanceError on local builds
* Fix an issue where components were reused from a module built with an incompatible platform

v2.27.0
-------
* Add "scratch_build_only_branches" configuration options to define the branches
  from which only scratch builds can be submitted.
* Do not check Greenwave gating status for scratch builds.
* Fix invalid date in module-build-macros when building modules locally when non-US
  locale is used.
* Fix --add-local-build with MBS Resolver.
* Fail the module build in case it buildrequires module build which does not have
  all the MSE builds in failed or ready state.
* Fix the issue in component reuse code when it could reuse components from module
  build built against older version of base module.

v2.26.0
-------
* Allow the poller to clean up module builds without arches.
* Prevent overlapping RPMs from buildrequired base modules from being available when using
  default modules.
* Add an REST endpoint to display log messages.
* Allow reusing components from builds built against compatible base module streams.
* Serialize component build state trace correctly if state is unset.
* Return empty result if short=true is specified on empty list of builds.


v2.25.0
-------
* Add support for "default modules" (Ursa-Prime)
* Allow modules built against an EoL platform to be used as a build dependency
* Allow the poller to clean up module builds without arches
* Default the cache directory to "/tmp/mbs" instead of under the home directory
* Do not start a Koji newRepo task if one is in progress
* Support base modules with virtual streams and no stream version
* Allow specifying a specific module build to reuse components from
* Fix a bug that prevented local module builds from completing after the libmodulemd v2 migration


v2.24.0
-------
* Allow configuring the number of parallel submissions to Koji
* Add state_reason on a Greenwave failure
* Module builds now remember what module they reused for building
* Fix type error preventing inclusion of custom SRPMs when running on F31/Rawhide
* Fix decoding issue with ``kobo.rpmlib.get_header_field()``


v2.23.0
-------
* Accept floats when filtering by stream_version_lte on the API.
* When no architecture is set in Koji tag, fallback to ``conf.arches``.


v2.22.0
-------
* Allow configuring ``dynamic_buildrequires``
* Allow buildrequiring modules built against all platform streams
* Take the list of arches for ``-build`` Koji tag from buildrequired modules
* Make ``sync_koji_build_tags`` poller working only with the builds that are in build state
  for some time
* Use single session object in greenwave handler and call ``commit()`` in the end
* Fix an issue with unset RPM whitelist
* Invalid ``scmurl`` on import yields status 400


v2.21.0
-------
* MBS now requires libmodulemd v2. The old libmodulemd v1 is not supported.
* Provide a better error message when the submitted modulemd version is unsupported.
* Fix a compatibility with PostgreSQL database when handling modules with virtual streams.
* Add ``scratch`` field to short JSON output.
* Add the Greenwave support to gate modules from "done" to "ready" state.


v2.20.0
-------
* Fix a bug where the ``master`` branch had to exist on module component git repos even when they
  were not used
* Fix an issue where local builds get their stream overwritten by the base module stream
* Fix -debuginfo/-debugsource package handling for the Koji Content Generator build
* Expose the stream version of a module in the REST API
* Fix buildrequiring a virtual stream of a base module
* Add support for the ``stream_version_lte`` parameter in the ``modules`` REST API endpoint
* Order the module version as if they were integers instead of strings in the REST API
* Accept multiple order by parameters of the same direction in the REST API
* Allow configuring "release streams" for base module streams and how they affect the stream
  version
* Convert the stream version to be a float

v2.19.1
-------
* Expose metrics about the number of completed builds and their status
* Improvements to offline local builds such as being able to provide the platform ID
* Add backwards-compatibility for cloning local repos with the artifact name and not the full path

v2.19.0
-------
* Fix an issue in MMDResolver when a transitive dependency cannot be satisfied
* Add support for offline local builds that use local repositories for dependency resolution
* Add initial support for gating of modules using Greenwave
* Fix a bug that caused successful local builds to not regenerate the repo with module metadata
* Allow resubmitting the same NSV for scratch module builds
* Allow importing modules without a Koji tag
* Add extra user input validation
* Allow whitelisted buildrequires with xmd.mbs.disttag_marking set to influnece the disttag
* Allow buildrequiring virtual streams to always get the latest

v2.18.2
-------
* Find compatible base modules based on the virtual streams and stream versions, not just the stream versions
* Support base modules with x.y.z versioning and no virtual streams

v2.18.1
-------
* Fix an issue where certain module builds would fail with the "Invalid modulemd" error

v2.18.0
-------
* Do not allow building modules with the same name as a base module (e.g. platform).
* Categorize log messages to make debug logs easier to read.
* Use the "scrmod" prefix also for build targets for scratch builds.
* Add the ability to override the base module marking used in the RPM disttags.
* Accept modulemd for scratch module builds as a parameter in the submitted JSON. 
* Do not default the module name to "unnamed" on a direct modulemd submission.
* Add the ability to override a buildrequired module stream based on a module's branch.

v2.17.0
-------
* Fix the Kerberos auth for Import modulemd API.
* Fix the way how KojiContentGenerator computes the size for modulemd files with unicode characters.
* Always allow submitting YAML for scratch builds.

v2.16.0
-------
* Add support for building scratch-builds of modules.
* Fix traceback when creating CG build in Koji introduced in 2.15.0.

v2.15.0
-------
* Create Koji CG module build in the end of "build" phase. Previously, it was created in the end of "done" phase.
* Fix the race-condition between MBS and its poller resulting in module build fail in case initial git clone took too long.
* Add simple mbs-cli tool to execute administration tasks using the MBS REST API.
* Return an exception to the user if no dependency combination is determined.
* Send more user-friendly message back to user in case the modulemd is invalid.
* Set proper state_reason when module build fails in early phase because of Koji relate issue.

v2.14.0
-------
* Add the ``allowed_users`` configuration for service accounts to bypass the group membership check
* Fix the handling of modulemd files with unicode characters
* Fix issues that occur if a module build is cancelled in the ``init`` state and resumed
* Add basic Prometheus monitoring
* Fix a bug in the ``init`` state handler when commit hashes are provided instead of a branch name
* Add Python 3 support except for Kerberos authentication with Koji

v2.13.1
-------
* Allow resubmiting the same module build when it results in new MSE build(s)
* Allow setting the context in an imported MMD file

v2.13.0
-------
* Add the retire command to mbs-manager
* Stop fedmsg-hub process when DNS resolution starts failing so that systemd can restart the service

v2.12.2
-------
* Properly set the ``distgits`` config value to match the new dist-git URLs

v2.12.1
-------
* Don't discard buildrequires if filtered_rpms already is found. This applies to local builds and resumed builds.
* Use https as the default protocol when interacting with Fedora dist-git

v2.12.0
-------
* Handle lost Koji messages informing MBS about a component being tagged
* Stop defining the DistTag RPM label and just use ModularityLabel
* Don't try to reupload a Koji content generator build if it already exists
* Fix an issue that would cause the hash provided to a Koji content generator build to be incorrect

v2.11.1
-------
* Fix a bug in the poller that caused it to not properly nudge module builds stuck in the ``init`` state

v2.11.0
-------
* Fix the creation of Content Generator builds without any components
* Add a poller handler to nudge module builds stuck in the ``init`` state
* List the failed component names in the state reason of a failed module build
* Fail the module build when Koji fails to return RPM headers (occurs during certain Koji outages)
* Use a separate Kerberos context per thread so both threads can use the thread keyring to store the Kerberos cache
* Return a non-zero return code when a local build fails

v2.10.0
-------
* Fix a bug where the SRPM NVR instead of the SRPM NEVRA was recorded in the modulemd files used in the Content Generator builds
* Use a separate Kerberos cache per thread to avoid Kerberos cache corruption
* Remove the ability to authenticate with Koji using only a Kerberos cache
* Remove the configuration option ``KRB_CCACHE``

v2.9.2
------
* Fix handling of SRPMs in Content Generator builds when SRPM name and main package name are different
* Use anonymous Koji sessions when authentication isn't necessary to perform an action

v2.9.1
------
* Look for stream collisions with buildrequired base modules on the backend instead of the API

v2.9.0
------
* Show the expanded buildrequires in the API output
* Make "-devel" modules optional through a configuration option
* Set the "modularitylabel" RPM header on component builds
* Workaround stream collisions that occur from modules included in a base module by Ursa-Major
* Remove dangling "debug" RPMs from the modulemd that ends up in the Koji Content Generator build
* Make "-devel" module builds require its "non-devel" counterpart
* Remove infrastructure information in the modulemd that ends up in the Koji Content Generator build
* Fail the module build immediately when a component build submission to Koji fails
* Return a friendly error when a stream collision occurs
* Fix database migrations when upgrading an old instance of MBS

v2.8.1
------
* Fix one of the database migration scripts

v2.8.0
------
* The config option ``KOJI_ARCHES`` was renamed to ``ARCHES``
* Import -devel Koji CG builds with RPMs which are filtered out of the traditional CG builds
* Add the ability to override buildrequires and requires when submitting a module build
* Use modules built against all compatible base module streams during buildrequire module resolution
* Record the stream versions (e.g. ``f29.0.0`` => ``290000``) of base modules (e.g. platform)
* Fix wrong inclusion of non-multilib packages in final modulemd of Koji CG builds
* Default arches are now applied to module components but they don't take any effect in the Koji builder yet

v2.7.0
------
* Fix filtering noarch RPMs when generating the Koji CG build information
* Prefix the module version based on the first base module (e.g. platform) it buildrequires
* Prefix the component disttag with the first base module stream the module buildrequires
* Add consistency to the way dependencies were chosen when doing a local build
* Don't run the final ``createrepo`` if the module build failed when doing a local build to help debug build errors
* The config option ``base_module_names`` is now a list instead of a set, so that there is an order of preference for some operations
* Set the default ``base_module_names`` config option to be ``['platform']``

v2.6.2
------
* Bugfix:  Set modulemd 'arch' field in arch-specific modulemd files imported to CG build.

v2.6.1
------
* RFE: Attach architecture specific modulemd files to content generator build in Koji.
  These modulemd files respect multilib, filters, whitelists and RPM headers. They also
  include list of licences.
* Bugfix: Fix bug breaking local builds in createrepo phase.

v2.6.0
------
* Bugfix: Fix to local builds of components in local git repos prefixed with file:///.
* Bugfix: Allow module components to use a git ref outside of the master branch.
  https://pagure.io/fm-orchestrator/pull-request/1008
* Bugfix: Fix to recording of buildrequires in the modulemd xmd block.
* RFE: Add a new API to allow importing modules to the MBS DB.  This facilitates
  management of so-called pseudo-modules.
* RFE: Module builds stuck in a state for more than a week will now be cleaned up
  by the poller.
* RFE: If configured, MBS can now refuse to build modules if their stream is EOL.

v2.5.1
------
* List of filtered RPMs is now generated on backend, so frontend does not query Koji.
* Fix issues when some exception raised in frontend were not forwarded to MBS client.

v2.5.0
------
* Cleaned up some debug log spam.
* Modulemd files can now override stream and name from scm if server is configured to allow it.
* Modules will now be built for architectures derived from a per-basemodule config map.
* Some fixes to filter generation.

v2.4.2
------
* Fix a bug where the fedmsg messaging plugin wouldn't send the NVR on a KojiTagChange message

v2.4.1
------
* Fix bugs when building modules with SCL components
* Expose the component build's NVR and batch in the REST API

v2.4.0
------
* MBS local builds now use the production MBS API for dependency resolution instead of PDC
* Remove COPR support (this hasn't been working for several months now)
* Make ``repo_include_all setting`` configurable in xmd

v2.3.2
------
* Typofix related to the v2.3.1 release.

v2.3.1
------
* Support the modulemd buildopts.rpms.whitelist option
* Allow searching for modules by a binary RPM using the MBS API
* Some fixes around local builds

v2.3.0
------

* Get buildrequired modules for Koji tag inheritance using NSVC instead of NSV
* Support querying for modules/components with multiple state filters
* Support querying for builds by an NSVC string
* Fix an MSE issue when a module buildrequires on the same name and stream as itself

v2.2.4
------

* Use /etc instead of %_sysconfdir in module-build-macros to fix builds with flatpak-rpm-macros

v2.2.3
------

* Fix an issue that occurred when the legacy modulemd module wasn't installed

v2.2.2
------

* Fix some local build issues

v2.2.1
------

* Fix exception in the poller when processing old module builds
* Revert the context values in the database to what they were previous to the algorithm change

v2.2.0
------

* Make the published messages smaller
* Show siblings and component_builds in the standard modules API

v2.1.1
------

* Some py3 compat fixes.
* Fallback to the old Koji tag format for the target when the tag name is too long

v2.1.0
------

* Change ModuleBuild.context to a database column which allows filtering
* Generate informative Koji tag names when "name:stream:version" is not too long
* Reuse components only from modules with the same build_context (same buildrequires names and streams)
* Generate 'context' from hash based on buildrequires/requires stream instead of commit hashes
* Allow defining list of packages which are blocked in the "-build" tag until they are built in a module
* Keep the 'module_name:[]' in requires unexpanded in the recorded modulemd

v2.0.2
------

* Return a friendly error when a module build's dependencies can't be met
* Remove unused dependencies

v2.0.1
------

* Fix Koji Content Generator imports
* Fix a module stream expansion issue that occurs when a module requires a module that isn't also a
  buildrequire

v2.0.0
------

* Add module stream expansion support
* Remove deprecated mbs-build tool (fedpkg/rhpkg should be used instead)
* Add the ``mbs-manager import_module`` command
* Add a database resolver for resolving dependencies for increased performance
* Support modulemd v2
* Fix error that occurs when a batch hasn't started but a repo regen message is received
* Improve Python 3 compatibility
* Improve unit testing performance

v1.7.0
------

* Use external repos tied to the Koji tags on local builds
* Make the MBS resolver interchangeable
* Make component reuse faster
* Fix a bug that caused module builds with no buildrequires to fail
* Make the poller not resume paused module builds if there was recent activity on the build
* A module's "time_modified" attribute is now updated more often to reflect changes in the build
* Fix getting the module name when a YAML file is submitted directly instead of using SCM
* Remove the Koji proxyuser functionality
* Set the owner on the overall module build in Koji
* Fix a bug that could cause a module build to fail with multiple buildrequires

v1.6.3
------

* Fix a bug that caused a module build to fail when it was cancelled during the module-build-macros phase and then resumed
* Reset the "state_reason" field on all components after a module build is resumed

v1.6.2
------

* Cancel new repo tasks on module build failures in Koji

v1.6.1
------

* Fix an error that occurs when a module build is resumed and module-build-macros was cancelled

v1.6.0
------

* Use available Koji repos during local builds instead of building them locally
* Add an incrementing prefix to module components' releases
* Add a "context" field on component and module releases in Koji for uniqueness for when Module Stream Expansion is implemented
* Remove urlgrabber as a dependency
* Set an explicit log level on our per-build file handler
* Set the timeout on git operations to 60 seconds to help alleviate client tooling timeouts
* Improve the efficiency of the stale module builds poller
* Fix situations where module-build-macros builds in Koji but fails in MBS and the build is resumed
