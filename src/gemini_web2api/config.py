"""Configuration management: defaults, JSON loading, validation.

CONFIG is a plain dict (seeded from DEFAULT_CONFIG) that every module reads
and that tests mutate directly; keep that contract intact.
"""
import json
import os
import sys

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
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
    "impersonate": "chrome",
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
}

# Known key types for validation: "int", "float", "str", "bool", "list".
_TYPED_KEYS = {
    "port": "int", "retry_attempts": "int", "request_timeout_sec": "int",
    "retry_delay_sec": "int", "max_concurrent_requests": "int",
    "keep_warm_interval_sec": "int", "keepalive_sec": "int",
    "slow_retry_sec": "int",
    "microbatch_sec": "float", "microbatch_single_sec": "float",
    "microbatch_max": "int", "microbatch_max_prompt": "int",
    "host": "str", "gemini_bl": "str", "default_model": "str",
    "impersonate": "str",
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
