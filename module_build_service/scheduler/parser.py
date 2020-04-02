# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import re

from module_build_service.common.errors import IgnoreMessage
from module_build_service.scheduler import events


class MessageParser(object):
    """Base class for parsing messages received from a specific message bus

    :param topic_categories: list of known services, that MBS can handle the
        messages sent from them. For example, a value could be
        ``["buildsys", "mbs", "greenwave"]``.
    :type topic_categories: list[str]
    """

    def __init__(self, topic_categories):
        self.topic_categories = topic_categories

    def parse(self, msg):
        raise NotImplementedError()


class FedmsgMessageParser(MessageParser):

    def parse(self, msg):
        """
        Parse a received message and convert it to a consistent format

        :param dict msg: the message contents from the message bus.
        :return: a mapping representing the corresponding event.
            If the topic isn't recognized, None is returned.
        :rtype: dict or None
        :raises IgnoreMessage: if the message should be ignored
        """

        if "body" in msg:
            msg = msg["body"]
        topic = msg["topic"]
        categories_re = "|".join(map(re.escape, self.topic_categories))
        regex_pattern = re.compile(
            r"(?P<category>" + categories_re + r")"
            r"(?:(?:\.)(?P<object>build|repo|module|decision))?"
            r"(?:(?:\.)(?P<subobject>state|build))?"
            r"(?:\.)(?P<event>change|done|end|tag|update)$"
        )
        regex_results = re.search(regex_pattern, topic)

        if regex_results:
            category = regex_results.group("category")
            object = regex_results.group("object")
            subobject = regex_results.group("subobject")
            event = regex_results.group("event")

            msg_id = msg.get("msg_id")
            msg_inner_msg = msg.get("msg")

            # If there isn't a msg dict in msg then this message can be skipped
            if not msg_inner_msg:
                raise IgnoreMessage(
                    "Ignoring message without any content with the " 'topic "{0}"'.format(topic))

            # Ignore all messages from the secondary koji instances.
            if category == "buildsys":
                instance = msg_inner_msg.get("instance", "primary")
                if instance != "primary":
                    raise IgnoreMessage("Ignoring message from %r koji hub." % instance)

                if object == "build" and subobject == "state" and event == "change":
                    task_id = msg_inner_msg.get("task_id")
                    if task_id is None:
                        raise IgnoreMessage(
                            "Ignoring message {}, with has a null task_id.".format(msg_id))
                    return {
                        "msg_id": msg_id,
                        "event": events.KOJI_BUILD_CHANGE,
                        "task_id": task_id,
                        "build_new_state": msg_inner_msg.get("new"),
                        "build_name": msg_inner_msg.get("name"),
                        "build_version": msg_inner_msg.get("version"),
                        "build_release": msg_inner_msg.get("release"),
                        "module_build_id": None,
                        "state_reason": None,
                    }

                if object == "repo" and subobject is None and event == "done":
                    return {
                        "msg_id": msg_id,
                        "event": events.KOJI_REPO_CHANGE,
                        "tag_name": msg_inner_msg.get("tag")
                    }

                if event == "tag":
                    name = msg_inner_msg.get("name")
                    version = msg_inner_msg.get("version")
                    release = msg_inner_msg.get("release")
                    nvr = None
                    if name and version and release:
                        nvr = "-".join((name, version, release))
                    return {
                        "msg_id": msg_id,
                        "event": events.KOJI_TAG_CHANGE,
                        "tag_name": msg_inner_msg.get("tag"),
                        "build_nvr": nvr,
                    }

            if (category == "mbs"
                    and object == "module" and subobject == "state" and event == "change"):
                return {
                    "msg_id": msg_id,
                    "event": events.MBS_MODULE_STATE_CHANGE,
                    "module_build_id": msg_inner_msg.get("id"),
                    "module_build_state": msg_inner_msg.get("state"),
                }

            if (category == "greenwave"
                    and object == "decision" and subobject is None and event == "update"):
                return {
                    "msg_id": msg_id,
                    "event": events.GREENWAVE_DECISION_UPDATE,
                    "decision_context": msg_inner_msg.get("decision_context"),
                    "policies_satisfied": msg_inner_msg.get("policies_satisfied"),
                    "subject_identifier": msg_inner_msg.get("subject_identifier"),
                }
