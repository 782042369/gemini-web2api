"""Session keepalive and cookie renewal, split from gemini.py.

Owns: Set-Cookie parsing/merging, throttled persistence, SNlM0e refresh,
and the background keepalive loop (RotateCookies with a StreamGenerate
heartbeat fallback - the generate endpoint is what actually rotates
PSIDTS). See gemini.py for the request pipeline that calls back into
_renew_cookies/_refresh_xsrf here.
"""
import json
import math
import os
import threading
import time
import urllib.request

from .config import CONFIG
from .budget import RequestControlError
from .logs import log
from .upstream import generate
from .upstream.cookies import (
    _active_cookie_path, _cookie_paths, _cookie_caches,
    _cookie_lock as _cookie_write_lock,
    get_active_xsrf_token, load_cookie, set_active_cookie,
    set_active_xsrf_token, restore_active_cookie,
)
from .upstream.transport import CHROME_UA, _get_ssl_ctx, get_browser_session


_last_cookie_persist = {}  # cookie path -> last disk persistence timestamp


def _parse_set_cookies(resp) -> dict:
    """Extract name->value pairs from a response's Set-Cookie headers.

    Args:
        resp: upstream response object (curl_cffi / httpx / urllib style).

    Returns:
        dict of cookie name -> value (pair text before the first ';').
    """
    pairs = {}
    headers = getattr(resp, "headers", None)
    if headers is None:
        return pairs
    raw_list = []
    try:
        raw_list = headers.get_list("set-cookie")          # httpx.Headers
    except AttributeError:
        try:
            raw_list = headers.getlist("set-cookie")        # curl_cffi / requests
        except AttributeError:
            single = headers.get("set-cookie") or headers.get("Set-Cookie")
            if single:
                raw_list = [single]
    for item in raw_list:
        head = item.split(";", 1)[0].strip()
        if "=" in head:
            name, _, value = head.partition("=")
            pairs[name.strip()] = value.strip()
    return pairs


def _write_cookie_data(cookie_file: str, data: dict) -> None:
    """Flush a prepared cookie document and publish its matching cache.

    Args:
        cookie_file: Destination file, including single-file bind mounts.
        data: Complete JSON document; caller holds _cookie_write_lock for
            the entire read/modify/write transaction.

    Returns:
        None. Errors propagate without advancing cache mtime or throttles.
    """
    serialized = json.dumps(data, ensure_ascii=False)
    with open(cookie_file, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.chmod(cookie_file, 0o600)
        os.fsync(f.fileno())
    mtime = os.path.getmtime(cookie_file)
    cookie_str = data.get("cookie", "")
    pairs = dict(p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p)
    _cookie_caches[cookie_file] = {
        "str": cookie_str,
        "sapisid": data.get("sapisid") or pairs.get("SAPISID") or None,
        "auth_user": data.get("auth_user"),
        "xsrf_token": data.get("xsrf_token"),
        "mtime": mtime,
    }


def _persist_cookie_file(cookie_file: str, cookie_str: str, sapisid, auth_user,
                         min_interval: float = 300.0) -> None:
    """Persist renewed cookies in place, serialized with all other readers/writers.

    Single-file bind mounts cannot be replaced by rename(2). Serialize
    in-process read/modify/write transactions and fsync before publishing
    cache metadata. Failures remain non-fatal and do not consume the throttle.

    Args:
        cookie_file: Cookie JSON/text file to update.
        cookie_str: Merged Cookie header string to persist.
        sapisid: Current SAPISID value, or None to preserve the stored value.
        auth_user: Per-account index, or None to preserve the stored value.
        min_interval: Minimum seconds between successful writes per account.

    Returns:
        None.
    """
    with _cookie_write_lock:
        now = time.time()
        last_persisted = _last_cookie_persist.get(cookie_file)
        if last_persisted is not None and now - last_persisted < min_interval:
            return
        try:
            data = {}
            if os.path.exists(cookie_file):
                with open(cookie_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content.startswith("{"):
                    data = json.loads(content)
            data["cookie"] = cookie_str
            if sapisid is not None:
                data["sapisid"] = sapisid
            if auth_user is not None:
                data["auth_user"] = auth_user
            _write_cookie_data(cookie_file, data)
            _last_cookie_persist[cookie_file] = now
            log(f"Cookie persisted to {cookie_file}")
        except Exception as e:
            log(f"Cookie persist error: {e}")


def _merge_response_cookies(resp) -> None:
    """Merge Set-Cookie renewals without losing concurrent account updates.

    Args:
        resp: Upstream response whose Set-Cookie headers to merge.

    Returns:
        None. In-memory renewals survive throttled or failed persistence.
    """
    try:
        updates = _parse_set_cookies(resp)
    except Exception:
        return
    if not updates:
        return
    cookie_file = _active_cookie_path()
    if not cookie_file:
        return
    with _cookie_write_lock:
        load_cookie()
        cache = _cookie_caches.get(cookie_file)
        if not cache or not cache.get("str"):
            return
        existing = dict(p.strip().split("=", 1) for p in cache["str"].split(";") if "=" in p)
        if all(existing.get(k) == v for k, v in updates.items()):
            return
        existing.update(updates)
        new_str = "; ".join(f"{k}={v}" for k, v in existing.items())
        sapisid = updates.get("SAPISID", cache.get("sapisid"))
        _cookie_caches[cookie_file] = dict(cache, str=new_str, sapisid=sapisid)
        log(f"Cookie renewed upstream ({len(updates)}): {', '.join(sorted(updates)[:5])}")
        _persist_cookie_file(cookie_file, new_str, sapisid, cache.get("auth_user"))


_xsrf_refreshed_at = {}  # cookie path -> last page-token refresh timestamp


def _maybe_refresh_xsrf():
    """Refresh the active account XSRF token from its live app page.

    The at= parameter binds a StreamGenerate request to the account's
    CURRENT session. A stale at (from an exported cookie snapshot)
    still passes plain-text generation, but uploaded-file references fail
    session binding with BardErrorInfo [1100] - the file "does not belong"
    to the session named by the old token. The live page always carries
    the current token (WIZ_global_data SNlM0e), so pulling from there
    (at most every 300s) keeps at= aligned with the account session.

    Args:
        None.

    Returns:
        None; updates only the active account token and logs the change.
    """
    path = _active_cookie_path() or "__anonymous__"
    now = time.time()
    if now - _xsrf_refreshed_at.get(path, 0.0) < 300:
        return
    _xsrf_refreshed_at[path] = now
    try:
        from .multimodal import _cached_page_tokens
        at = _cached_page_tokens().get("at")
        current = get_active_xsrf_token()
        if at and at.startswith("AOvx") and at != current:
            old = (current or "")[:10]
            set_active_xsrf_token(at)
            log(f"xsrf_token refreshed for {path}: {old}... -> {at[:10]}...")
    except RequestControlError:
        raise
    except Exception as e:
        log(f"xsrf refresh failed for {path}: {e}")


_keepalive_lock = threading.Lock()
_keepalive_on = {"started": False}


def _rotate_psidts(cookie_file=None) -> bool:
    """Rotate one configured account while preserving thread-local context.

    Args:
        cookie_file: Explicit account file; defaults to the active account.

    Returns:
        True when the account rotation completed successfully.
    """
    path = cookie_file or _active_cookie_path()
    if not path:
        return False
    previous = set_active_cookie(path)
    try:
        return _rotate_psidts_active()
    finally:
        restore_active_cookie(previous)


def _rotate_psidts_active() -> bool:
    """Actively rotate short-lived session cookies via Google's RotateCookies.

    Mirrors HanaokaYuzu/Gemini-API rotate_1psidts: one lightweight POST to
    accounts.google.com/RotateCookies makes Google issue fresh __Secure-*
    session cookies (PSIDTS etc.) via Set-Cookie. The response is fed
    through the existing renewal path (in-memory merge + throttled
    persist), so the on-disk cookie file stays fresh.

    Args:
        None.

    Returns:
        True when the rotation completed with a 200 response.
    """
    cookie_file = _active_cookie_path() or CONFIG.get("cookie_file")
    if not cookie_file:
        return False
    cookie_str, _sapisid = load_cookie()
    if not cookie_str:
        return False
    # RotateCookies lives on accounts.google.com and needs that domain's
    # cookies (SID/HSID/SSID/...). Prefer the dedicated accounts_cookie
    # export when present; a gemini-domain-only cookie jar gets 401.
    rotate_cookie = cookie_str
    try:
        with _cookie_write_lock:
            with open(cookie_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("{"):
                acct = json.loads(content).get("accounts_cookie") or ""
                if acct.strip():
                    rotate_cookie = acct.strip()
    except (OSError, ValueError):
        pass
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://accounts.google.com",
        "Referer": "https://accounts.google.com/",
        "User-Agent": CHROME_UA,
        "Cookie": rotate_cookie,
    }
    body = '[000,"-0000000000000000000"]'
    try:
        sess = get_browser_session()
        if sess is not None:
            resp = sess.post("https://accounts.google.com/RotateCookies",
                             headers=headers, data=body, timeout=30)
        else:
            req = urllib.request.Request("https://accounts.google.com/RotateCookies",
                                         data=body.encode(), headers=headers, method="POST")
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
        if getattr(resp, "status_code", 200) != 200:
            # accounts.google.com/RotateCookies needs accounts-domain cookies
            # (SID/HSID...) that a gemini-domain export lacks -> 401. Fall
            # back to a minimal StreamGenerate heartbeat: the generate
            # endpoint is the one that reliably issues fresh PSIDTS via
            # Set-Cookie (observed in production logs).
            log(f"Keepalive rotate: HTTP {getattr(resp, 'status_code', '?')}, falling back to heartbeat")
            return _generate_heartbeat()
        _merge_response_cookies(resp)
        _sync_accounts_cookie(resp)
        _maybe_refresh_xsrf()
        return True
    except Exception as e:
        log(f"Keepalive rotate failed: {e}")
        return _generate_heartbeat()


def _sync_accounts_cookie(resp) -> None:
    """Apply RotateCookies renewals to the stored accounts_cookie field.

    The accounts-domain export carries its own short-lived tokens; without
    this sync it would age out and RotateCookies would start returning 401
    again even though the main gemini cookie stays healthy.

    Args:
        resp: the RotateCookies response whose Set-Cookie headers to apply.

    Returns:
        None. Failures are logged and non-fatal.
    """
    try:
        updates = _parse_set_cookies(resp)
    except Exception:
        return
    if not updates:
        return
    cookie_file = _active_cookie_path() or CONFIG.get("cookie_file")
    if not cookie_file:
        return
    with _cookie_write_lock:
        try:
            load_cookie()
            with open(cookie_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content.startswith("{"):
                return
            data = json.loads(content)
            acct = data.get("accounts_cookie") or ""
            if not acct.strip():
                return
            pairs = dict(p.strip().split("=", 1) for p in acct.split(";") if "=" in p)
            changed = {k: v for k, v in updates.items() if k in pairs and pairs[k] != v}
            if not changed:
                return
            pairs.update(changed)
            data["accounts_cookie"] = "; ".join(f"{k}={v}" for k, v in pairs.items())
            cache = _cookie_caches.get(cookie_file)
            if cache and cache.get("mtime") == os.path.getmtime(cookie_file):
                # Include renewals that the main-cookie throttle left in RAM;
                # otherwise our new mtime would mark stale disk data as fresh.
                data["cookie"] = cache["str"]
                if cache.get("sapisid") is not None:
                    data["sapisid"] = cache["sapisid"]
                if cache.get("auth_user") is not None:
                    data["auth_user"] = cache["auth_user"]
            _write_cookie_data(cookie_file, data)
            _last_cookie_persist[cookie_file] = time.time()
            log(f"accounts_cookie renewed ({len(changed)}): {', '.join(sorted(changed)[:4])}")
        except Exception as e:
            log(f"accounts_cookie sync failed: {e}")


def _generate_heartbeat() -> bool:
    """Run one minimal StreamGenerate call as a session keepalive.

    StreamGenerate is the endpoint that actually rotates the short-lived
    session cookies (PSIDTS) via Set-Cookie; a tiny prompt keeps each
    tick at a few tokens. The call goes through the normal pipeline, so
    cookie renewal, the slow-walk breaker and retries all apply.

    Args:
        None.

    Returns:
        True when the heartbeat generate succeeded.
    """
    try:
        generate("hi", 1, 4)
        return True
    except Exception as e:
        log(f"Keepalive heartbeat failed: {e}")
        return False


def start_keepalive():
    """Start one keepalive worker only when configuration enables it.

    Args:
        None.

    Returns:
        None. Disabled/invalid configurations leave startup retryable, as
        does a failure to create or start the daemon thread.
    """
    def _loop():
        """Rotate every configured account once per keepalive interval.

        Args:
            None.

        Returns:
            Never normally returns; the daemon stops with the process.
        """
        while True:
            time.sleep(interval)
            for path in _cookie_paths():
                try:
                    ok = _rotate_psidts(path)
                    log(f"Keepalive tick: account={path} rotate={'ok' if ok else 'failed'}")
                except Exception as e:
                    log(f"Keepalive loop error for {path}: {e}")

    with _keepalive_lock:
        if _keepalive_on["started"]:
            return
        try:
            interval = float(CONFIG.get("keepalive_sec") or 0)
        except (TypeError, ValueError):
            return
        if not math.isfinite(interval) or interval <= 0 or not _cookie_paths():
            return
        threading.Thread(target=_loop, daemon=True, name="session-keepalive").start()
        _keepalive_on["started"] = True
    log(f"Keepalive started: every {interval:.0f}s")
