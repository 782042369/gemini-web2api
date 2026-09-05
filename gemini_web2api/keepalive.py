"""Session keepalive and cookie renewal, split from gemini.py.

Owns: Set-Cookie parsing/merging, throttled persistence, SNlM0e refresh,
and the background keepalive loop (RotateCookies with a StreamGenerate
heartbeat fallback - the generate endpoint is what actually rotates
PSIDTS). See gemini.py for the request pipeline that calls back into
_renew_cookies/_refresh_xsrf here.
"""
import json
import os
import threading
import time
import urllib.request

from .config import CONFIG
from .gemini import (
    log, load_cookie, generate, CHROME_UA, get_browser_session,
    _get_ssl_ctx, _cookie_caches, _active_cookie_path,
)


_cookie_write_lock = threading.Lock()
_last_cookie_persist = {"t": 0.0}


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


def _persist_cookie_file(cookie_file: str, cookie_str: str, sapisid, auth_user,
                         min_interval: float = 300.0) -> None:
    """Write the renewed cookie back to its JSON file, in place and throttled.

    The cookie file is a single-file bind mount, so rename(2)-based atomic
    replace fails (EBUSY); instead an in-place truncate+write guarded by a
    lock is used, with fsync. Any failure is logged and non-fatal.

    Args:
        cookie_file: path of the cookie JSON file (as seen in-container).
        cookie_str: merged Cookie header string to persist.
        sapisid: current SAPISID value (kept as-is when None).
        auth_user: per-account auth_user override (kept as-is when None).
        min_interval: minimum seconds between two disk writes (throttle).

    Returns:
        None.
    """
    with _cookie_write_lock:
        now = time.time()
        if now - _last_cookie_persist["t"] < min_interval:
            return
        try:
            data = {}
            if os.path.exists(cookie_file):
                with open(cookie_file, "r") as f:
                    content = f.read().strip()
                if content.startswith("{"):
                    data = json.loads(content)
            data["cookie"] = cookie_str
            if sapisid:
                data["sapisid"] = sapisid
            if auth_user is not None:
                data["auth_user"] = auth_user
            with open(cookie_file, "w") as f:
                f.write(json.dumps(data, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(cookie_file, 0o600)
            _last_cookie_persist["t"] = now
            cache = _cookie_caches.get(cookie_file)
            if cache is not None:
                try:
                    cache["mtime"] = os.path.getmtime(cookie_file)
                except OSError:
                    pass
            log(f"Cookie persisted to {cookie_file}")
        except Exception as e:
            log(f"Cookie persist error: {e}")


def _merge_response_cookies(resp) -> None:
    """Merge upstream Set-Cookie renewals into the active cookie cache.

    Google rotates short-lived session tokens (SIDCC / PSIDTS / OSID ...)
    via Set-Cookie during normal traffic. Merging them keeps the in-memory
    cookie fresh and (throttled) persisted, so container restarts no longer
    fall back to a stale export from the sync extension.

    Args:
        resp: upstream response object whose Set-Cookie headers to merge.

    Returns:
        None. Failures are swallowed (best-effort renewal).
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
    cache = _cookie_caches.get(cookie_file)
    if not cache or not cache.get("str"):
        return
    existing = dict(p.split("=", 1) for p in cache["str"].split("; ") if "=" in p)
    if all(existing.get(k) == v for k, v in updates.items()):
        return
    existing.update(updates)
    new_str = "; ".join(f"{k}={v}" for k, v in existing.items())
    sapisid = updates.get("SAPISID") or cache.get("sapisid")
    _cookie_caches[cookie_file] = {
        "str": new_str,
        "sapisid": sapisid,
        "auth_user": cache.get("auth_user"),
        "mtime": cache.get("mtime", 0),
    }
    log(f"Cookie renewed upstream ({len(updates)}): {', '.join(sorted(updates)[:5])}")
    _persist_cookie_file(cookie_file, new_str, sapisid, cache.get("auth_user"))


_xsrf_refreshed_at = 0.0


def _maybe_refresh_xsrf():
    """Refresh CONFIG xsrf_token from the live app page (throttled).

    The at= parameter binds a StreamGenerate request to the account's
    CURRENT session. A stale at (from an exported cookie snapshot)
    still passes plain-text generation, but uploaded-file references fail
    session binding with BardErrorInfo [1100] - the file "does not belong"
    to the session named by the old token. The live page always carries
    the current token (WIZ_global_data thykhd), so pulling from there
    (at most every 300s) keeps at= aligned with the account session.

    Returns:
        None; updates CONFIG["xsrf_token"] in place and logs the change.
    """
    global _xsrf_refreshed_at
    if time.time() - _xsrf_refreshed_at < 300:
        return
    _xsrf_refreshed_at = time.time()
    try:
        from .multimodal import _cached_page_tokens
        at = _cached_page_tokens().get("at")
        if at and at.startswith("AOvx") and at != CONFIG.get("xsrf_token"):
            old = (CONFIG.get("xsrf_token") or "")[:10]
            CONFIG["xsrf_token"] = at
            log(f"xsrf_token refreshed from page: {old}... -> {at[:10]}...")
    except Exception as e:
        log(f"xsrf refresh failed: {e}")


_keepalive_lock = threading.Lock()
_keepalive_on = {"started": False}


def _rotate_psidts() -> bool:
    """Actively rotate short-lived session cookies via Google's RotateCookies.

    Mirrors HanaokaYuzu/Gemini-API rotate_1psidts: one lightweight POST to
    accounts.google.com/RotateCookies makes Google issue fresh __Secure-*
    session cookies (PSIDTS etc.) via Set-Cookie. The response is fed
    through the existing renewal path (in-memory merge + throttled
    persist), so the on-disk cookie file stays fresh.

    Returns:
        True when the rotation completed with a 200 response.
    """
    cookie_file = _active_cookie_path() or CONFIG.get("cookie_file")
    if not cookie_file:
        return False
    cookie_str, _sapisid = load_cookie()
    if not cookie_str:
        return False
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://accounts.google.com",
        "Referer": "https://accounts.google.com/",
        "User-Agent": CHROME_UA,
        "Cookie": cookie_str,
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
        _maybe_refresh_xsrf()
        return True
    except Exception as e:
        log(f"Keepalive rotate failed: {e}")
        return _generate_heartbeat()


def _generate_heartbeat() -> bool:
    """Run one minimal StreamGenerate call as a session keepalive.

    StreamGenerate is the endpoint that actually rotates the short-lived
    session cookies (PSIDTS) via Set-Cookie; a tiny prompt keeps each
    tick at a few tokens. The call goes through the normal pipeline, so
    cookie renewal, the slow-walk breaker and retries all apply.

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
    """Start the background session-keepalive loop (idempotent, once).

    Every keepalive_sec (default 540s) the daemon thread actively rotates
    the short-lived session cookies (PSIDTS) and refreshes the SNlM0e
    xsrf token. This keeps the exported cookie file fresh even during
    idle periods, so restarts never fall back to a stale export and the
    browser-side re-export workflow becomes unnecessary.

    Returns:
        None; disabled silently when keepalive_sec <= 0 or no cookie file.
    """
    with _keepalive_lock:
        if _keepalive_on["started"]:
            return
        _keepalive_on["started"] = True
    interval = float(CONFIG.get("keepalive_sec") or 0)
    if interval <= 0 or not (CONFIG.get("cookie_file") or CONFIG.get("cookie_files")):
        return

    def _loop():
        while True:
            time.sleep(interval)
            try:
                ok = _rotate_psidts()
                log(f"Keepalive tick: rotate={'ok' if ok else 'failed'}")
            except Exception as e:
                log(f"Keepalive loop error: {e}")

    threading.Thread(target=_loop, daemon=True, name="session-keepalive").start()
    log(f"Keepalive started: every {interval:.0f}s")

