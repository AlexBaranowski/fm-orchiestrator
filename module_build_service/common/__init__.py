# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT

from module_build_service.common.config import conf
from module_build_service.common import logger

__all__ = ('conf', 'log', 'build_logs')

logger.init_logging(conf)
log = logger.MBSLogger()
build_logs = logger.ModuleBuildLogs(
    conf.build_logs_dir, conf.build_logs_name_format, conf.log_level)
