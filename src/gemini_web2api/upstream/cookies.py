"""Cookie pool: multi-account loading, round-robin pick, mtime cache."""
import json
import os
import threading
from contextlib import contextmanager

from ..config import CONFIG
from ..logs import log


# Cookie pool: multiple Google accounts (cookie files) rotated per request.
_cookie_caches = {}                 # path -> {"str", "sapisid", "auth_user", "mtime"}
_active_cookie = threading.local()  # per-request selected cookie slot
_round_robin = {"i": 0}
_round_robin_lock = threading.Lock()
# File reads, cache publication and both renewal writers share this lock.
_cookie_lock = threading.RLock()

# Account-scoped session state.  Keeping XSRF data here is essential when a
# cookie pool is configured: Google binds upload references and StreamGenerate
# tokens to one account session.
_account_state = {}  # path -> {"xsrf_token": str}
_account_state_lock = threading.RLock()


def set_active_cookie(path: str):
    """Bind a cookie file and capture the entire previous account context.

    Args:
        path: Cookie JSON/text file to use for subsequent upstream calls.

    Returns:
        Opaque snapshot to pass to :func:`restore_active_cookie`.
    """
    previous = dict(_active_cookie.__dict__)
    _active_cookie.__dict__.clear()
    _active_cookie.path = path
    return previous


def restore_active_cookie(previous):
    """Restore a thread-local cookie binding after a scoped operation.

    Args:
        previous: Snapshot returned by :func:`set_active_cookie`. A legacy
            path or ``None`` is also accepted to bind/reset the context.

    Returns:
        None.
    """
    _active_cookie.__dict__.clear()
    if isinstance(previous, dict):
        _active_cookie.__dict__.update(previous)
    elif previous is not None:
        _active_cookie.path = previous


@contextmanager
def use_cookie(path: str):
    """Temporarily bind one account to the current thread.

    Args:
        path: Cookie file for the scoped operation.

    Returns:
        Context manager that restores the full previous account on exit.

    Yields:
        None while the account is active.
    """
    previous = set_active_cookie(path)
    try:
        yield
    finally:
        restore_active_cookie(previous)


def get_active_xsrf_token():
    """Return the XSRF token belonging to the active cookie account.

    Args:
        None.

    Returns:
        Live or exported account token. The legacy config token is used
        only without an account or for the sole configured account.
    """
    load_cookie()
    path = _active_cookie_path()
    with _cookie_lock:
        exported = (_cookie_caches.get(path) or {}).get("xsrf_token")
        with _account_state_lock:
            token = (_account_state.get(path) or {}).get("xsrf_token")
    if token or exported:
        return token or exported
    paths = _cookie_paths()
    if path is None or paths == [path]:
        return CONFIG.get("xsrf_token")
    return None


def set_active_xsrf_token(token: str):
    """Store an XSRF token for the active cookie account.

    Args:
        token: Fresh SNlM0e token; empty values are ignored.

    Returns:
        None.
    """
    if not token:
        return
    path = _active_cookie_path()
    with _account_state_lock:
        state = _account_state.setdefault(path, {})
        state["xsrf_token"] = token


def _cookie_paths() -> list:
    """List configured cookie paths without duplicates.

    Args:
        None.

    Returns:
        Pool entries followed by the legacy cookie file when configured.
    """
    paths = []
    for p in list(CONFIG.get("cookie_files") or []) + [CONFIG.get("cookie_file")]:
        if p and p not in paths:
            paths.append(p)
    return paths


def pick_next_cookie():
    """Pick the next cookie slot (round-robin) for the current request thread.

    Called once per incoming HTTP request; every upstream call made on this
    thread (image uploads + generation, including retries) then uses the same
    Google account. A new request clears all previous auth-user overrides.

    Args:
        None.

    Returns:
        None.
    """
    _active_cookie.__dict__.clear()
    paths = _cookie_paths()
    if len(paths) <= 1:
        _active_cookie.path = paths[0] if paths else None
        return
    with _round_robin_lock:
        idx = _round_robin["i"] % len(paths)
        _round_robin["i"] += 1
    _active_cookie.path = paths[idx]


def _active_cookie_path():
    """Return the active cookie path, including the first pooled account.

    Args:
        None.

    Returns:
        The thread-local path, configured legacy path, or first pool entry.
    """
    active = getattr(_active_cookie, "path", None)
    if active:
        return active
    configured = CONFIG.get("cookie_file")
    if configured:
        return configured
    paths = _cookie_paths()
    return paths[0] if paths else None


def _active_auth_user():
    """Resolve the auth-user index before any URL or header is constructed.

    Args:
        None.

    Returns:
        Explicit thread override, file-specific index, or global fallback.
    """
    load_cookie()
    au = getattr(_active_cookie, "auth_user_override", None)
    if au is None:
        au = getattr(_active_cookie, "auth_user", None)
    return CONFIG.get("auth_user") if au is None else au


def set_active_auth_user(auth_user):
    """Override the auth-user index on the current thread.

    Args:
        auth_user: Google account index, or None to clear the override.

    Returns:
        None.
    """
    _active_cookie.auth_user_override = auth_user
    _active_cookie.auth_user = auth_user


def load_cookie() -> tuple:
    """Load one account atomically with respect to renewal writers.

    Args:
        None.

    Returns:
        Cookie header and optional SAPISID. A failed read retains the last
        good cache; explicit thread auth-user overrides are never replaced.
    """
    cookie_file = _active_cookie_path()
    with _cookie_lock:
        cache = _cookie_caches.get(cookie_file) or {}
        try:
            if not cookie_file or not os.path.exists(cookie_file):
                _active_cookie.auth_user = None
                return "", None
            mtime = os.path.getmtime(cookie_file)
            if not cache or mtime != cache.get("mtime"):
                with open(cookie_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                data = json.loads(content) if content.startswith("{") else {"cookie": content}
                cookie_str = data.get("cookie", "")
                pairs = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p)
                updated = {
                    "str": cookie_str,
                    "sapisid": data.get("sapisid") or pairs.get("SAPISID") or None,
                    "auth_user": data.get("auth_user"),
                    "xsrf_token": data.get("xsrf_token"),
                    "mtime": mtime,
                }
                # External exports can replace an account session at the same
                # path. Do not retain a live token from the previous session.
                if cache and any(cache.get(k) != updated.get(k) for k in
                                 ("str", "sapisid", "auth_user", "xsrf_token")):
                    with _account_state_lock:
                        _account_state.pop(cookie_file, None)
                _cookie_caches[cookie_file] = cache = updated
        except Exception as e:
            log(f"Cookie load error: {e}")
        override = getattr(_active_cookie, "auth_user_override", None)
        _active_cookie.auth_user = cache.get("auth_user") if override is None else override
        return cache.get("str", ""), cache.get("sapisid")
