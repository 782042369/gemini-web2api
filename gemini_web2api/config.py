"""Configuration management."""
import json
import os

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
    "microbatch_sec": 1.5,
    "microbatch_single_sec": 0.45,
    "microbatch_max": 6,
    "microbatch_max_prompt": 3000,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
