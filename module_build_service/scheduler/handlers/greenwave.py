# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import

from module_build_service.common import conf, log
from module_build_service.common.koji import get_session
from module_build_service.common.models import ModuleBuild, BUILD_STATES
from module_build_service.scheduler.db_session import db_session
from module_build_service.scheduler import celery_app, events


def get_corresponding_module_build(nvr):
    """Find corresponding module build from database and return

    :param str nvr: module build NVR. This is the subject_identifier included
        inside ``greenwave.decision.update`` message.
    :return: the corresponding module build object. For whatever the reason,
        if the original module build id cannot be found from the Koji build of
        ``nvr``, None will be returned.
    :rtype: :class:`ModuleBuild` or None
    """
    koji_session = get_session(conf, login=False)
    build_info = koji_session.getBuild(nvr)
    if build_info is None:
        return None

    try:
        module_build_id = build_info["extra"]["typeinfo"]["module"]["module_build_service_id"]
    except KeyError:
        # If any of the keys is not present, the NVR is not the one for
        # handling Greenwave event.
        return None

    return ModuleBuild.get_by_id(db_session, module_build_id)


@celery_app.task
@events.mbs_event_handler
def decision_update(msg_id, decision_context, subject_identifier, policies_satisfied):
    """Move module build to ready or failed according to Greenwave result

    :param str msg_id: the original id of the message being handled which is
        received from the message bus.
    :param str decision_context: the context of the greewave decision. Refer to
        the messaging document for detailed information.
    :param str subject_identifier: usually a build NVR. Refer to
        https://docs.pagure.org/greenwave/messaging.html for detailed information.
    :param bool policies_satisfied: whether the build satisfies Greenwave rules.
        Refer to the messaging document for detailed information.
    """
    if not conf.greenwave_decision_context:
        log.debug(
            "Skip Greenwave message %s as MBS does not have GREENWAVE_DECISION_CONTEXT "
            "configured",
            msg_id,
        )
        return

    if decision_context != conf.greenwave_decision_context:
        log.debug(
            "Skip Greenwave message %s as MBS only handles messages with the "
            'decision context "%s"',
            msg_id,
            conf.greenwave_decision_context,
        )
        return

    module_build_nvr = subject_identifier

    if not policies_satisfied:
        log.debug(
            "Skip to handle module build %s because it has not satisfied Greenwave policies.",
            module_build_nvr,
        )
        return

    build = get_corresponding_module_build(module_build_nvr)

    if build is None:
        log.debug(
            "No corresponding module build of subject_identifier %s is found.", module_build_nvr)
        return

    if build.state == BUILD_STATES["done"]:
        build.transition(
            db_session,
            conf,
            BUILD_STATES["ready"],
            state_reason="Module build {} has satisfied Greenwave policies.".format(
                module_build_nvr
            ),
        )
    else:
        log.warning(
            "Module build %s is not in done state but Greenwave tells "
            "it passes tests in decision context %s",
            module_build_nvr, decision_context,
        )

    db_session.commit()
