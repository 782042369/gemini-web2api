"""Cookie pool: multi-account loading, round-robin pick, mtime cache."""
import json
import os
import threading

from ..config import CONFIG
from ..logs import log


# Cookie pool: multiple Google accounts (cookie files) rotated per request.
_cookie_caches = {}                 # path -> {"str", "sapisid", "auth_user", "mtime"}
_active_cookie = threading.local()  # per-request selected cookie slot
_round_robin = {"i": 0}
_round_robin_lock = threading.Lock()


def _cookie_paths() -> list:
    """All configured cookie file paths (cookie_files pool + legacy cookie_file)."""
    paths = []
    for p in list(CONFIG.get("cookie_files") or []) + [CONFIG.get("cookie_file")]:
        if p and p not in paths:
            paths.append(p)
    return paths


def pick_next_cookie():
    """Pick the next cookie slot (round-robin) for the current request thread.

    Called once per incoming HTTP request; every upstream call made on this
    thread (image uploads + generation, including retries) then uses the same
    Google account. With a single cookie configured this is a no-op.
    """
    _active_cookie.auth_user = None
    paths = _cookie_paths()
    if len(paths) <= 1:
        _active_cookie.path = paths[0] if paths else None
        return
    with _round_robin_lock:
        idx = _round_robin["i"] % len(paths)
        _round_robin["i"] += 1
    _active_cookie.path = paths[idx]


def _active_cookie_path():
    return getattr(_active_cookie, "path", None) or CONFIG.get("cookie_file")


def _active_auth_user():
    au = getattr(_active_cookie, "auth_user", None)
    if au is None:
        au = CONFIG.get("auth_user")
    return au


def load_cookie() -> tuple:
    """Load the request's cookie from file with mtime-based caching.

    Supports a pool of cookie files (see pick_next_cookie). The JSON cookie
    format may carry a per-account "auth_user" override.
    """
    cookie_file = _active_cookie_path()
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    cache = _cookie_caches.get(cookie_file)
    try:
        mtime = os.path.getmtime(cookie_file)
        if cache and mtime == cache["mtime"] and cache["str"]:
            _active_cookie.auth_user = cache.get("auth_user")
            return cache["str"], cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        auth_user = None
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
            auth_user = data.get("auth_user")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_caches[cookie_file] = {
            "str": cookie_str, "sapisid": sapisid or None,
            "auth_user": auth_user, "mtime": mtime,
        }
        _active_cookie.auth_user = auth_user
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        prev = _cookie_caches.get(cookie_file) or {}
        _active_cookie.auth_user = prev.get("auth_user")
        return prev.get("str", ""), prev.get("sapisid")
