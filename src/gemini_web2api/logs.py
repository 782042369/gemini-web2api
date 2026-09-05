"""Central logging gate: stderr stream + optional rotating local file.

Every module logs through log() so the output format stays uniform across
the service and can be gated by the log_requests config flag. Log message
texts are load-bearing: production log greps match on them - the emitted
line shape "[HH:MM:SS] message" is byte-identical on both sinks.

Sinks:
    - stderr always (docker logs / journalctl consume this)
    - CONFIG["log_file"] when set: TimedRotatingFileHandler rolling at
      midnight, keeping CONFIG["log_retention_days"] days (default 7) of
      history on disk.
"""
import logging
import os
import sys
import threading
from logging.handlers import TimedRotatingFileHandler

from .config import CONFIG

_FORMAT = "[%(asctime)s] %(message)s"
_DATEFMT = "%H:%M:%S"

_logger = None
_file_handler = None
_file_handler_path = None
# RLock: _ensure_file_handler() calls _get_logger() while already holding
# the lock; a plain Lock() would self-deadlock on first file-sink setup.
_setup_lock = threading.RLock()


def _get_logger() -> logging.Logger:
    """Return the package logger with the stderr handler attached (lazy).

    Args:
        None.

    Returns:
        Configured Logger writing "[HH:MM:SS] msg" lines to stderr.
    """
    global _logger
    if _logger is not None:
        return _logger
    with _setup_lock:
        if _logger is not None:
            return _logger
        logger = logging.getLogger("gemini_web2api")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
            logger.addHandler(handler)
        _logger = logger
        return _logger


def _ensure_file_handler() -> None:
    """Attach the rotating file sink when CONFIG["log_file"] is configured.

    Lazy and idempotent: config is loaded after module import, so the
    handler is (re)checked on every log() call; path changes re-attach.
    Failures to open the file are swallowed with one stderr notice - the
    stderr sink must keep flowing regardless.

    Args:
        None.

    Returns:
        None.
    """
    global _file_handler, _file_handler_path
    path = CONFIG.get("log_file")
    if not path:
        return
    if _file_handler is not None and _file_handler_path == path:
        return
    with _setup_lock:
        if _file_handler is not None and _file_handler_path == path:
            return
        logger = _get_logger()
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            retention = int(CONFIG.get("log_retention_days") or 7)
            handler = TimedRotatingFileHandler(
                path, when="midnight", backupCount=max(1, retention),
                encoding="utf-8", utc=False,
            )
            handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
            if _file_handler is not None:
                logger.removeHandler(_file_handler)
                _file_handler.close()
            logger.addHandler(handler)
            _file_handler = handler
            _file_handler_path = path
        except Exception as exc:
            sys.stderr.write(f"[logs] file sink unavailable: {exc}\n")
            _file_handler = None
            _file_handler_path = None


def log(msg: str):
    """Write one log line to stderr and the rotating file (when enabled).

    Args:
        msg: message text (no trailing newline).

    Returns:
        None. No-op entirely when CONFIG["log_requests"] is falsy.
    """
    if not CONFIG.get("log_requests"):
        return
    _ensure_file_handler()
    _get_logger().info(msg)
