# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
""" This is a sub-module for backend/scheduler functionality. """

from celery import Celery

from module_build_service.common.config import conf

celery_app = Celery("module-build-service")
# Convert config names specific for Celery like this:
# celery_broker_url -> broker_url
celery_configs = {
    name[7:]: getattr(conf, name)
    for name in dir(conf) if name.startswith("celery_")
}
# Only allow a single process so that tasks are always serial per worker
celery_configs["worker_concurrency"] = 1
celery_app.conf.update(**celery_configs)
