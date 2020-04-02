# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Generic messaging functions."""

from __future__ import absolute_import

import pkg_resources
import six.moves.queue

from module_build_service.common import conf, log
from module_build_service.scheduler.parser import FedmsgMessageParser

# A queue containing message body that should be sent after
# ModuleBuild.transition call every time.
# The ModuleBuild.transition is responsible for generating the message body
# and put it into this queue.
# After changes to a module build made by the transition method are committed,
# the messages will be sent.
module_build_state_change_out_queue = six.moves.queue.Queue()


def publish(topic, msg, conf, service):
    """
    Publish a single message to a given backend, and return
    :param topic: the topic of the message (e.g. module.state.change)
    :param msg: the message contents of the message (typically JSON)
    :param conf: a Config object from the class in config.py
    :param service: the system that is publishing the message (e.g. mbs)
    :return:
    """
    try:
        handler = _messaging_backends[conf.messaging]["publish"]
    except KeyError:
        raise KeyError(
            "No messaging backend found for %r in %r" % (conf.messaging, _messaging_backends.keys())
        )

    from module_build_service.common.monitor import (
        messaging_tx_to_send_counter,
        messaging_tx_sent_ok_counter,
        messaging_tx_failed_counter,
    )

    messaging_tx_to_send_counter.inc()
    try:
        rv = handler(topic, msg, conf, service)
        messaging_tx_sent_ok_counter.inc()
        return rv
    except Exception:
        messaging_tx_failed_counter.inc()
        raise


def _fedmsg_publish(topic, msg, conf, service):
    # fedmsg doesn't really need access to conf, however other backends do
    import fedmsg

    return fedmsg.publish(topic, msg=msg, modname=service)


# A counter used for in-memory messages.
_in_memory_msg_id = 0
_initial_messages = []


def _in_memory_publish(topic, msg, conf, service):
    """ Puts the message into the in memory work queue. """
    # Increment the message ID.
    global _in_memory_msg_id
    _in_memory_msg_id += 1

    # Create fake fedmsg from the message so we can reuse
    # the BaseMessage.from_fedmsg code to get the particular BaseMessage
    # class instance.
    wrapped_msg = FedmsgMessageParser(known_fedmsg_services).parse({
        "msg_id": str(_in_memory_msg_id),
        "topic": service + "." + topic,
        "msg": msg
    })

    # Put the message to queue.
    from module_build_service.scheduler.consumer import work_queue_put

    try:
        work_queue_put(wrapped_msg)
    except ValueError as e:
        log.warning("No MBSConsumer found.  Shutting down?  %r" % e)
    except AttributeError:
        # In the event that `moksha.hub._hub` hasn't yet been initialized, we
        # need to store messages on the side until it becomes available.
        # As a last-ditch effort, try to hang initial messages in the config.
        log.warning("Hub not initialized.  Queueing on the side.")
        _initial_messages.append(wrapped_msg)


known_fedmsg_services = ["buildsys", "mbs", "greenwave"]


_fedmsg_backend = {
    "publish": _fedmsg_publish,
    "parser": FedmsgMessageParser(known_fedmsg_services),
    "services": known_fedmsg_services,
    "topic_suffix": ".",
}
_in_memory_backend = {
    "publish": _in_memory_publish,
    "parser": FedmsgMessageParser(known_fedmsg_services),  # re-used.  :)
    "services": [],
    "topic_suffix": ".",
}


_messaging_backends = {}
for entrypoint in pkg_resources.iter_entry_points("mbs.messaging_backends"):
    _messaging_backends[entrypoint.name] = ep = entrypoint.load()
    required = ["publish", "services", "parser", "topic_suffix"]
    if any([key not in ep for key in required]):
        raise ValueError("messaging backend %r is malformed: %r" % (entrypoint.name, ep))

if not _messaging_backends:
    raise ValueError("No messaging plugins are installed or available.")

# After loading registered messaging backends, the default messaging backend
# can be determined by configured messaging backend.
default_messaging_backend = _messaging_backends[conf.messaging]


# Helper functions to send message on specific event occurring


def notify_on_module_state_change(message_body):
    publish("module.state.change", message_body, conf, "mbs")
