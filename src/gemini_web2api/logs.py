"""Central stderr logging gate.

Every module logs through log() so the output format stays uniform across
the service and can be gated by the log_requests config flag. Log message
texts are load-bearing: production log greps match on them.
"""
import sys
import time

from .config import CONFIG


def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()
