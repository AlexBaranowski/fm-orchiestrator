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
# Written by Ralph Bean <rbean@redhat.com>

""" Handlers for module change events on the message bus. """

import rida.builder
import rida.database
import rida.pdc
import logging

log = logging.getLogger(__name__)


def wait(config, session, msg):
    """ Called whenever a module enters the 'wait' state.

    We transition to this state shortly after a modulebuild is first requested.

    All we do here is request preparation of the buildroot.
    The kicking off of individual component builds is handled elsewhere,
    in rida.schedulers.handlers.repos.
    """
    build = rida.database.ModuleBuild.from_fedmsg(session, msg)
    module_info = build.json()
    log.info("Found module_info=%s from message" % module_info)

    pdc_session = rida.pdc.get_pdc_client_session(config)
    tag = rida.pdc.get_module_tag(pdc_session, module_info, strict=True)
    log.debug("Found tag=%s for module %r" % (tag, build))

    # Hang on to this information for later.  We need to know which build is
    # associated with which koji tag, so that when their repos are regenerated
    # in koji we can figure out which for which module build that event is
    # relevant.
    build.tag = tag

    dependencies = rida.pdc.get_module_dependencies(pdc_session, module_info)
    builder = rida.builder.KojiModuleBuilder(build.name, config)
    builder.buildroot_add_dependency(dependencies)
    build.buildroot_task_id = builder.buildroot_prep()
    # TODO: build srpm with dist_tag macros
    # TODO submit build from srpm to koji
    # TODO: buildroot.add_artifact(build_with_dist_tags)
    # TODO: buildroot.ready(artifact=$artifact)
    build.transition(config, state="build")  # Wait for the buildroot to be ready.
    session.commit()
