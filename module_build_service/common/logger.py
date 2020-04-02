# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
Logging functions.

At the beginning of the MBS flow, init_logging(conf) must be called.

After that, logging from any module is possible using Python's "logging"
module as showed at
<https://docs.python.org/3/howto/logging.html#logging-basic-tutorial>.

Examples:

import logging

logging.debug("Phasers are set to stun.")
logging.info("%s tried to build something", username)
logging.warning("%s failed to build", task_id)

"""

from __future__ import absolute_import
import os
import logging
import inspect

levels = {
    "debug": logging.DEBUG,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
}

level_flags = {
    "debug": levels["debug"],
    "verbose": levels["info"],
    "quiet": levels["error"],
}


log_format = "%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s"


class ModuleBuildFileHandler(logging.FileHandler):
    """
    FileHandler subclass which handles only messages generated during
    particular module build with `build_id` set in its constructor.
    """

    def __init__(self, build_id, filename, mode="a", encoding=None, delay=0):
        logging.FileHandler.__init__(self, filename, mode, encoding, delay)
        self.build_id = build_id

    def emit(self, record):
        # Imported here because of importing cycle between __init__.py,
        # scheduler and models.
        from module_build_service.scheduler.consumer import MBSConsumer

        # Check the currently handled module build and emit the message only
        # if it's associated with current module.
        build_id = MBSConsumer.current_module_build_id
        if not build_id or build_id != self.build_id:
            return

        logging.FileHandler.emit(self, record)


class ModuleBuildLogs(object):
    """
    Manages ModuleBuildFileHandler logging handlers.
    """

    def __init__(self, build_logs_dir, build_logs_name_format, level=logging.INFO):
        """
        Creates new ModuleBuildLogs instance. Module build logs are stored
        to `build_logs_dir` directory.
        """
        self.handlers = {}
        self.build_logs_dir = build_logs_dir
        self.build_logs_name_format = build_logs_name_format
        self.level = level

    def path(self, db_session, build):
        """
        Returns the full path to build log of module with id `build_id`.
        """
        path = os.path.join(self.build_logs_dir, self.name(db_session, build))
        return path

    def name(self, db_session, build):
        """
        Returns the filename for a module build
        """
        name = self.build_logs_name_format.format(**build.json(db_session))
        return name

    def start(self, db_session, build):
        """
        Starts logging build log for module with `build_id` id.
        """
        if not self.build_logs_dir:
            return

        if build.id in self.handlers:
            return

        # Create and add ModuleBuildFileHandler.
        handler = ModuleBuildFileHandler(build.id, self.path(db_session, build))
        handler.setLevel(self.level)
        handler.setFormatter(logging.Formatter(log_format, None))
        log = logging.getLogger()
        log.setLevel(self.level)
        log.addHandler(handler)

        self.handlers[build.id] = handler

    def stop(self, build):
        """
        Stops logging build log for module with `build_id` id. It does *not*
        remove the build log from fs.
        """
        if build.id not in self.handlers:
            return

        handler = self.handlers[build.id]
        handler.flush()
        handler.close()

        # Remove the log handler.
        log = logging.getLogger()
        log.removeHandler(handler)
        del self.handlers[build.id]


class MBSLogger:
    def __init__(self):
        self._logger = logging.getLogger("MBS")
        self._level = logging.NOTSET
        self._current_path = os.path.dirname(os.path.realpath(__file__))

    @property
    def logger(self):
        return self._logger

    @property
    def level(self):
        return self._level

    @level.setter
    def level(self, level):
        self._level = level
        self.logger.setLevel(self._level)

    def setLevel(self, level):
        self.level = level

    def debug(self, *args, **kwargs):
        return self._log_call("debug", args, kwargs)

    def info(self, *args, **kwargs):
        return self._log_call("info", args, kwargs)

    def warning(self, *args, **kwargs):
        return self._log_call("warning", args, kwargs)

    def error(self, *args, **kwargs):
        return self._log_call("error", args, kwargs)

    def critical(self, *args, **kwargs):
        return self._log_call("critical", args, kwargs)

    def exception(self, *args, **kwargs):
        return self._log_call("exception", args, kwargs)

    def log(self, *args, **kwargs):
        return self._log_call("log", args, kwargs)

    def _log_call(self, level_name, args, kwargs):
        caller_filename = inspect.stack()[2][1]
        caller_filename = os.path.normpath(caller_filename)
        if not caller_filename.startswith(self._current_path):
            log_name = "MBS"
        else:
            log_name = "MBS" + caller_filename[len(self._current_path):-3].replace("/", ".")
        return getattr(logging.getLogger(log_name), level_name)(*args, **kwargs)


def str_to_log_level(level):
    """
    Returns internal representation of logging level defined
    by the string `level`.

    Available levels are: debug, info, warning, error
    """
    if level not in levels:
        return logging.NOTSET

    return levels[level]


def supported_log_backends():
    return ("console", "file")


def init_logging(conf):
    """
    Initializes logging according to configuration file.
    """
    log_backend = conf.log_backend

    if not log_backend or len(log_backend) == 0 or log_backend == "console":
        logging.basicConfig(level=conf.log_level, format=log_format)
        log = MBSLogger()
        log.level = conf.log_level
    else:
        logging.basicConfig(filename=conf.log_file, level=conf.log_level, format=log_format)
        log = MBSLogger()
