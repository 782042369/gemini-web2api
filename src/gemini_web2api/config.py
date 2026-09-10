"""Configuration management: defaults, JSON loading, validation.

CONFIG is a plain dict (seeded from DEFAULT_CONFIG) that every module reads
and that tests mutate directly; keep that contract intact.
"""
import json
import math
import os
import sys

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "request_deadline_sec": 180,
    "queue_timeout_sec": 30,
    "max_queued_requests": 64,
    "translation_batch_max_chars": 12000,
    "translation_batch_max_segments": 25,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.8-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    "cookie_files": [],
    "max_concurrent_requests": 0,
    "auto_delete_history": False,
    "keep_warm_interval_sec": 0,
    "slow_retry_sec": 60,
    # chrome145 pins the profile HanaokaYuzu/Gemini-API uses to avoid
    # Google's Device Bound Session Credentials experiment on newer profiles.
    "impersonate": "chrome145",
    # CDP vision bridge: base URL of a Chrome DevTools endpoint whose
    # browser holds a genuinely logged-in Gemini tab (see docs/INCIDENT-
    # 20260908-vision.md). Image-bearing requests are executed inside that
    # tab. None/empty disables the bridge and keeps the direct chain.
    "vision_bridge_url": None,
    # Transparent micro-batching of short single-segment generateContent
    # requests (companion plugins firing burst translations). 0 disables.
    # Active session keepalive: rotate PSIDTS + refresh SNlM0e every N
    # seconds even with zero traffic (mirrors HanaokaYuzu auto_refresh,
    # default 540s). 0 disables.
    "keepalive_sec": 540,
    "microbatch_sec": 1.5,
    "microbatch_single_sec": 0.45,
    "microbatch_max": 6,
    "microbatch_max_prompt": 3000,
    # Local log file (rotated daily at midnight, keeping
    # log_retention_days days). None disables the file sink; stderr is
    # always written.
    "log_file": None,
    "log_retention_days": 7,
    # Hard limits protect the threaded HTTP server from accidental or hostile
    # memory exhaustion. Images have a separate cap because they are fetched
    # and uploaded before generation.
    "max_request_body_bytes": 16 * 1024 * 1024,
    "max_image_bytes": 20 * 1024 * 1024,
    "request_body_timeout_sec": 30,
    "allow_private_image_urls": False,
}

# Known key types for validation: "int", "float", "str", "bool", "list".
_TYPED_KEYS = {
    "port": "int", "retry_attempts": "int", "request_timeout_sec": "int",
    "request_deadline_sec": "float", "queue_timeout_sec": "float",
    "max_queued_requests": "int", "translation_batch_max_chars": "int",
    "translation_batch_max_segments": "int",
    "retry_delay_sec": "int", "max_concurrent_requests": "int",
    "keep_warm_interval_sec": "int", "keepalive_sec": "int",
    "slow_retry_sec": "int",
    "microbatch_sec": "float", "microbatch_single_sec": "float",
    "microbatch_max": "int", "microbatch_max_prompt": "int",
    "host": "str", "gemini_bl": "str", "default_model": "str",
    "impersonate": "str",
    "log_file": "str", "log_retention_days": "int",
    "max_request_body_bytes": "int", "max_image_bytes": "int",
    "request_body_timeout_sec": "int", "allow_private_image_urls": "bool",
    "log_requests": "bool", "temporary_chats": "bool",
    "auto_delete_history": "bool",
    "api_keys": "list", "cookie_files": "list",
}

CONFIG = dict(DEFAULT_CONFIG)


def _warn(msg: str) -> None:
    """Print a config warning to stderr (always, independent of log gate).

    Args:
        msg: warning text.

    Returns:
        None.
    """
    sys.stderr.write(f"[config] {msg}\n")


def validate_config(cfg: dict = None) -> list:
    """Validate a config dict against known keys and expected types.

    Args:
        cfg: dict to validate (defaults to the live CONFIG).

    Returns:
        List of human-readable problem strings; empty when valid.
    """
    cfg = CONFIG if cfg is None else cfg
    problems = []
    for key, expected in _TYPED_KEYS.items():
        if key not in cfg or cfg[key] is None:
            continue
        value = cfg[key]
        ok = {
            "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "str": lambda v: isinstance(v, str),
            "bool": lambda v: isinstance(v, bool),
            "list": lambda v: isinstance(v, list),
        }[expected](value)
        if not ok:
            problems.append(f"{key}: expected {expected}, got {type(value).__name__}")
    for key in ("request_deadline_sec", "queue_timeout_sec", "max_queued_requests",
                "translation_batch_max_chars", "translation_batch_max_segments"):
        value = cfg.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                valid = math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                problems.append(f"{key}: must be positive and finite; safe default will be used")
    for key in cfg:
        if key not in DEFAULT_CONFIG:
            problems.append(f"unknown key (typo?): {key}")
    return problems


def load_config(path: str = None):
    """Load config from a JSON file into the live CONFIG dict.

    Args:
        path: config file path; missing files are silently skipped so a
            default configuration still boots.

    Returns:
        The updated CONFIG dict.
    """
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    for problem in validate_config():
        _warn(problem)
    return CONFIG


def find_config():
    """Search for a config file in standard locations.

    Args:
        None.

    Returns:
        First existing path among ./config.json and
        ~/.config/gemini-web2api/config.json, else None.
    """
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
