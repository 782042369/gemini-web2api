"""Gemini StreamGenerate protocol implementation with streaming clients."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib
import random
import threading

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from curl_cffi import requests as _curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from .config import CONFIG

_ssl_ctx = None
_httpx_client = None

# Full Chrome UA matching the curl_cffi impersonation profile. The previous
# truncated "Mozilla/5.0 (... ) AppleWebKit/537.36" string is itself a bot tell.
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# Upstream concurrency cap (max_concurrent_requests config; 0 = unlimited).
_upstream_semaphore = None
_upstream_sema_lock = threading.Lock()

# Cookie pool: multiple Google accounts (cookie files) rotated per request.
_cookie_caches = {}                 # path -> {"str", "sapisid", "auth_user", "mtime"}
_active_cookie = threading.local()  # per-request selected cookie slot
_round_robin = {"i": 0}
_round_robin_lock = threading.Lock()

# In-flight coalescing: identical concurrent generate() calls share one upstream request.
_inflight = {}
_inflight_lock = threading.Lock()


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
        transport = httpx.HTTPTransport(proxy=proxy, limits=limits) if proxy else None
        _httpx_client = httpx.Client(
            transport=transport, timeout=CONFIG["request_timeout_sec"],
            verify=True, limits=limits,
        )
    return _httpx_client


# Thread-local browser-fingerprint sessions (curl_cffi). Python's default TLS
# handshake + HTTP/1.1 is trivially distinguishable from a real browser and is
# a likely trigger for Google's slow-walk treatment. curl_cffi impersonates a
# genuine Chrome TLS/h2 fingerprint; sessions are per-thread because
# curl_cffi Session is not guaranteed thread-safe.
_browser_session_local = threading.local()


def get_browser_session():
    """Thread-local curl_cffi session impersonating Chrome, or None if absent."""
    if not HAS_CURL_CFFI:
        return None
    s = getattr(_browser_session_local, "session", None)
    if s is None:
        proxy = CONFIG.get("proxy")
        s = _curl_requests.Session(
            impersonate=CONFIG.get("impersonate") or "chrome",
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        _browser_session_local.session = s
    return s


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


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = _active_auth_user()
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(uuid_val: str = None) -> dict:
    """Build request headers for StreamGenerate.

    Parameters:
        uuid_val: request uuid shared with inner[59] of the payload. When
            set, it is also sent as the x-goog-ext-525005358-jspb header
            (["<uuid>",1]) like the current web client, which binds the
            request to uploaded file references.

    Returns:
        Header dict for the StreamGenerate POST.
    """
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": CHROME_UA,
    }
    if uuid_val:
        headers["x-goog-ext-525005358-jspb"] = f'["{uuid_val}",1]'
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(_active_auth_user())
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, uuid_val: str = None) -> str:
    """Build the urlencoded f.req payload for StreamGenerate.

    Parameters:
        prompt: user prompt text.
        model_id: MODE_CATEGORY id (1=FAST, 2=THINKING, 3=PRO, 4=AUTO...).
        think_mode: thinking level (0=dynamic, 4=default).
        file_refs: list of uploaded file references to attach.
        extra_fields: optional {index: value} overrides for inner payload slots.
        uuid_val: request uuid for inner[59]; generated (uppercase) when omitted.

    Returns:
        urlencoded request body string.
    """
    inner = [None] * 102
    if file_refs:
        refs = [[[ref], "image.png"] for ref in file_refs]  # [[url], filename] per HanaokaYuzu format
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = uuid_val or str(uuid.uuid4()).upper()
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo(?:\s*"?\s*,)?\s*\[\s*(\d+)\s*\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def _extract_conversation_id(line: str):
    """Parse a single wrb.fr line and return the conversation id (inner[1][0])."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return None
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str:
            return None
        inner = json.loads(inner_str)
        if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], list) and inner[1]:
            cid = inner[1][0]
            if isinstance(cid, str) and cid:
                return cid
    except (json.JSONDecodeError, IndexError, TypeError):
        return None
    return None


def extract_conversation_id(raw: str) -> str:
    """Extract conversation id from a full (non-streaming) response."""
    for line in raw.split("\n"):
        cid = _extract_conversation_id(line)
        if cid:
            return cid
    return None


def _delete_url() -> str:
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "batchexecute"
        f"?rpcids=hNktQb&bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def delete_conversation(cid: str) -> bool:
    """EXPERIMENTAL: best-effort delete of a conversation from account history.

    Upstream rejects the hNktQb batchexecute call on current builds (XSRF
    error 138/139), so this only logs failures. Recommended alternative:
    temporary_chats=true (conversations are never saved at all).
    """
    if not cid:
        return False
    inner_arg = '["' + cid + '",1]'
    payload = json.dumps([[["hNktQb", inner_arg, None, "generic"]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": payload, "at": CONFIG.get("xsrf_token") or ""})
    headers = _build_headers()
    client = _get_httpx_client() if HAS_HTTPX else None
    try:
        if client is not None:
            resp = client.post(_delete_url(), content=body, headers=headers, timeout=15)
            text = resp.text
            resp.raise_for_status()
        else:
            text = _urllib_post(_delete_url(), body.encode(), headers)
        ok = "BardErrorInfo" not in text
        log(f"History delete cid={cid[:14]}...: {'ok' if ok else 'rejected'}")
        return ok
    except Exception as e:
        log(f"History delete failed cid={cid[:14]}...: {e}")
        return False


def schedule_history_delete(cid: str):
    """Fire and forget history deletion (auto_delete_history config)."""
    if not CONFIG.get("auto_delete_history") or not cid:
        return
    if CONFIG.get("temporary_chats"):
        # Temporary chats are never saved server-side; the delete RPC would
        # just 400 (noise + a wasted upstream request on every generation).
        return
    threading.Thread(target=delete_conversation, args=(cid,), daemon=True).start()


def _get_semaphore():
    global _upstream_semaphore
    limit = CONFIG.get("max_concurrent_requests") or 0
    if limit <= 0:
        return None
    with _upstream_sema_lock:
        if _upstream_semaphore is None:
            _upstream_semaphore = threading.BoundedSemaphore(limit)
        return _upstream_semaphore


class _UpstreamSlot:
    """Context manager acquiring a global upstream concurrency slot (if capped).

    Gemini Web only serves ~3-4 concurrent streams per account well; beyond
    that it slow-walks or rejects requests. Capping locally queues requests
    FIFO instead, which keeps tail latency predictable.
    """

    def __enter__(self):
        sema = _get_semaphore()
        self._sema = sema
        if sema is None:
            return self
        self._t0 = time.time()
        sema.acquire()
        waited = time.time() - self._t0
        if waited > 0.5:
            log(f"Upstream busy: queued {waited:.1f}s (max_concurrent_requests={CONFIG.get('max_concurrent_requests')})")
        return self

    def __exit__(self, *exc):
        if self._sema is not None:
            self._sema.release()
        return False


def _retry_delay(attempt: int, transport_error: bool = False) -> float:
    """Exponential backoff with jitter, capped at 15s.

    Connection-level failures (stale pooled connection, TLS reset, DNS blip)
    are retried immediately: the upstream hasn't rejected the request, so
    sleeping 2s+ would only add latency to what is a fresh-reconnect case.
    """
    if transport_error:
        return 0.05
    base = CONFIG.get("retry_delay_sec", 2)
    return min(base * (2 ** attempt) + random.uniform(0, 0.5), 15)


def _is_transport_error(e: BaseException) -> bool:
    """True for connection-level errors worth an immediate retry."""
    if isinstance(e, _AttemptDeadlineExceeded):
        return True  # slow-walk breaker: retry at once, backoff would waste time
    if HAS_HTTPX and isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, (ConnectionError, OSError)) and not isinstance(e, RuntimeError)


def _urllib_post(url: str, body: bytes, headers: dict, timeout=None) -> str:
    """Fallback POST via urllib (no connection pooling), used when httpx is absent."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    proxy = CONFIG.get("proxy")
    ctx = _get_ssl_ctx()
    to = timeout if isinstance(timeout, (int, float)) else CONFIG["request_timeout_sec"]
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        resp = opener.open(req, timeout=to)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=to)
    return resp.read().decode("utf-8", errors="replace")


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Non-streaming generation with retry.

    Concurrent identical requests (same prompt/model/cookie) are coalesced
    into a single upstream call - common when several browser tabs translate
    the same text at once.
    """
    key = (
        prompt, model_id, think_mode, _active_cookie_path(),
        tuple(file_refs or []),
        tuple(sorted(extra_fields.items())) if extra_fields else None,
    )
    with _inflight_lock:
        entry = _inflight.get(key)
        is_owner = entry is None
        if is_owner:
            entry = {"event": threading.Event(), "result": None, "error": None}
            _inflight[key] = entry
    if not is_owner:
        entry["event"].wait(timeout=CONFIG["request_timeout_sec"] * 2 + 10)
        if entry["error"] is not None:
            raise entry["error"]
        return entry["result"]
    try:
        result = _generate_upstream(prompt, model_id, think_mode, file_refs, extra_fields)
        entry["result"] = result
        return result
    except Exception as e:
        entry["error"] = e
        raise
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        entry["event"].set()


def _per_attempt_timeout():
    """Per-read timeout for one upstream attempt (slow-walk detection aid).

    Note: httpx "read" timeout only fires BETWEEN chunks. Google slow-walks
    often trickle bytes, keeping every inter-chunk gap short — so this alone
    never bounds total time (observed 516s with read=60s). The real bound is
    the wall-clock deadline enforced while iterating the streamed response.
    """
    deadline = CONFIG.get("slow_retry_sec") or 0
    total = CONFIG["request_timeout_sec"]
    try:
        deadline = float(deadline)
    except (TypeError, ValueError):
        return None
    if deadline <= 0 or deadline >= total:
        return None
    if HAS_HTTPX:
        return httpx.Timeout(total, read=deadline)
    return deadline


def _attempt_deadline():
    """Absolute wall-clock deadline for one upstream attempt, or None."""
    deadline = CONFIG.get("slow_retry_sec") or 0
    total = CONFIG["request_timeout_sec"]
    try:
        deadline = float(deadline)
    except (TypeError, ValueError):
        return None
    if deadline <= 0 or deadline >= total:
        return None
    return deadline


class _AttemptDeadlineExceeded(Exception):
    """Wall-clock slow-walk breaker tripped for one upstream attempt."""


def _generate_upstream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Single-owner upstream call with retries. Uses the pooled httpx client."""
    uuid_val = str(uuid.uuid4()).upper()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, uuid_val).encode()
    url = _get_url()
    headers = _build_headers(uuid_val)
    sess = get_browser_session()
    client = _get_httpx_client() if HAS_HTTPX else None
    attempt_timeout = _per_attempt_timeout()

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            with _UpstreamSlot():
                t0 = time.time()
                deadline_sec = _attempt_deadline()
                if sess is not None:
                    # Preferred path: real Chrome TLS/h2 fingerprint.
                    parts = []
                    r = sess.post(url, data=body, headers=headers, stream=True,
                                  timeout=(10, CONFIG["request_timeout_sec"]))
                    try:
                        r.raise_for_status()
                        for chunk in r.iter_content():
                            parts.append(chunk)
                            if deadline_sec and time.time() - t0 > deadline_sec:
                                raise _AttemptDeadlineExceeded(
                                    f"slow-walk breaker: attempt exceeded {deadline_sec:.0f}s"
                                )
                    finally:
                        r.close()
                    raw = b"".join(parts).decode("utf-8", "replace")
                elif client is not None:
                    # Stream the response even for non-streaming generate():
                    # iterating chunks lets us enforce a WALL-CLOCK deadline,
                    # which a plain read timeout cannot do when Google
                    # slow-walks with a trickle of bytes.
                    parts = []
                    with client.stream("POST", url, content=body, headers=headers,
                                       timeout=attempt_timeout) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_text():
                            parts.append(chunk)
                            if deadline_sec and time.time() - t0 > deadline_sec:
                                raise _AttemptDeadlineExceeded(
                                    f"slow-walk breaker: attempt exceeded {deadline_sec:.0f}s"
                                )
                    raw = "".join(parts)
                else:
                    raw = _urllib_post(url, body, headers, timeout=attempt_timeout)
                    if deadline_sec and time.time() - t0 > deadline_sec:
                        raise _AttemptDeadlineExceeded(
                            f"slow-walk breaker: attempt exceeded {deadline_sec:.0f}s"
                        )
            text = extract_response_text(raw)
            log(f"Upstream generate: {time.time() - t0:.2f}s chars={len(text)} attempt={attempt + 1}")
            if CONFIG.get("auto_delete_history"):
                schedule_history_delete(extract_conversation_id(raw))
            return text
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                delay = _retry_delay(attempt, transport_error=_is_transport_error(e))
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']} in {delay:.1f}s: {e}")
                time.sleep(delay)
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation with retry on connection failure."""
    sess = get_browser_session()
    if sess is None and not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    uuid_val = str(uuid.uuid4()).upper()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, uuid_val)
    url = _get_url()
    headers = _build_headers(uuid_val)
    client = _get_httpx_client() if HAS_HTTPX else None

    last_err = None
    emitted_raw_text = ""
    stream_cid = None
    for attempt in range(CONFIG["retry_attempts"]):
        stream_cid = None
        try:
            slot = _UpstreamSlot().__enter__()
            t0 = time.time()
            deadline_sec = _attempt_deadline()
            try:
                if sess is not None:
                    # Preferred path: real Chrome TLS/h2 fingerprint.
                    r = sess.post(url, data=body, headers=headers, stream=True,
                                  timeout=(10, CONFIG["request_timeout_sec"]))
                    try:
                        r.raise_for_status()
                        buf = b""
                        for chunk in r.iter_content():
                            buf += chunk
                            if deadline_sec and time.time() - t0 > deadline_sec:
                                raise _AttemptDeadlineExceeded(
                                    f"slow-walk breaker: stream exceeded {deadline_sec:.0f}s"
                                )
                            if b"BardErrorInfo" in buf:
                                bard_err = re.search(r'BardErrorInfo(?:\s*"?\s*,)?\s*\[\s*(\d+)\s*\]',
                                                     buf.decode("utf-8", "replace"))
                                if bard_err:
                                    raise RuntimeError(
                                        f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                                    )
                            while b"\n" in buf:
                                line_b, buf = buf.split(b"\n", 1)
                                line = line_b.decode("utf-8", "replace")
                                stream_cid = _extract_conversation_id(line) or stream_cid
                                for t in _extract_texts_from_line(line):
                                    if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                        continue
                                    if not t.startswith(emitted_raw_text):
                                        raise RuntimeError("Gemini stream content changed during retry")
                                    delta = clean_text(t[len(emitted_raw_text):], strip=False)
                                    emitted_raw_text = t
                                    if delta:
                                        yield delta
                    finally:
                        r.close()
                else:
                    with client.stream("POST", url, content=body, headers=headers,
                                       timeout=_per_attempt_timeout()) as resp:
                        resp.raise_for_status()
                        buf = ""
                        for chunk in resp.iter_text():
                            buf += chunk
                            if deadline_sec and time.time() - t0 > deadline_sec:
                                raise _AttemptDeadlineExceeded(
                                    f"slow-walk breaker: stream exceeded {deadline_sec:.0f}s"
                                )
                            if "BardErrorInfo" in buf:
                                bard_err = re.search(r'BardErrorInfo(?:\s*"?\s*,)?\s*\[\s*(\d+)\s*\]', buf)
                                if bard_err:
                                    raise RuntimeError(
                                        f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                                    )
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                stream_cid = _extract_conversation_id(line) or stream_cid
                                for t in _extract_texts_from_line(line):
                                    if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                        continue
                                    if not t.startswith(emitted_raw_text):
                                        raise RuntimeError("Gemini stream content changed during retry")
                                    delta = clean_text(t[len(emitted_raw_text):], strip=False)
                                    emitted_raw_text = t
                                    if delta:
                                        yield delta
            finally:
                slot.__exit__(None, None, None)
            if CONFIG.get("auto_delete_history"):
                schedule_history_delete(stream_cid)
            return
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                delay = _retry_delay(attempt, transport_error=_is_transport_error(e))
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']} in {delay:.1f}s: {e}")
                time.sleep(delay)
    raise last_err


def _keep_warm_loop(interval: float):
    """Background loop keeping the Google session warm (opt-in).

    Gemini Web occasionally slow-walks the first generation after the
    account has been idle for a minute+ (observed 5-15s vs ~2s normal).
    A tiny periodic generation keeps the session hot. Costs one trivial
    upstream request per interval per cookie slot, so it is opt-in via
    keep_warm_interval_sec in config.json.
    """
    from .models import resolve_model
    while True:
        time.sleep(interval)
        try:
            pick_next_cookie()  # rotate across the cookie pool, like real traffic
            _, model_id, think_mode, _, _ = resolve_model(CONFIG.get("default_model"))
            generate("hi", model_id, think_mode, None, None)
        except Exception as e:
            log(f"keep-warm: {e}")


def start_keep_warm():
    """Start the keep-warm thread if keep_warm_interval_sec > 0."""
    interval = CONFIG.get("keep_warm_interval_sec") or 0
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        return
    threading.Thread(target=_keep_warm_loop, args=(interval,), daemon=True, name="keep-warm").start()
    log(f"keep-warm enabled: every {interval:.0f}s")
