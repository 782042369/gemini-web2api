"""Upstream generation pipeline: retries, coalescing, streaming, keep-warm.

Owns the request loop around one upstream generation: in-flight coalescing,
retry/backoff policy, the wall-clock slow-walk breaker, streaming delta
extraction and the opt-in keep-warm background loop.
"""
import codecs
import random
import re
import threading
import time
import uuid

from ..config import CONFIG
from ..logs import log
from .concurrency import _UpstreamSlot
from .cookies import _active_cookie_path, pick_next_cookie
from .history import schedule_history_delete
from .parser import (_extract_conversation_id, _extract_texts_from_line,
                     clean_text, extract_conversation_id,
                     extract_response_text)
from .protocol import _build_headers, _build_payload, _get_url
from .transport import (HAS_HTTPX, _get_httpx_client, _urllib_post,
                        get_browser_session)

try:
    import httpx
except ImportError:
    httpx = None


# In-flight coalescing: identical concurrent generate() calls share one upstream request.
_inflight = {}
_inflight_lock = threading.Lock()


def _renew_cookies(resp) -> None:
    """Forward a response's Set-Cookie renewal to the keepalive module.

    Lazy import avoids an import cycle (keepalive imports this module).

    Args:
        resp: upstream response object.

    Returns:
        None.
    """
    from ..keepalive import _merge_response_cookies
    _merge_response_cookies(resp)


def _refresh_xsrf():
    """Forward the throttled SNlM0e refresh to the keepalive module.

    Args:
        None.

    Returns:
        None.
    """
    from ..keepalive import _maybe_refresh_xsrf
    _maybe_refresh_xsrf()


def _retry_delay(attempt: int, transport_error: bool = False,
                 rate_limited: bool = False) -> float:
    """Exponential backoff with jitter, capped at 15s (60s when rate-limited).

    Connection-level failures (stale pooled connection, TLS reset, DNS blip)
    are retried immediately: the upstream hasn't rejected the request, so
    sleeping 2s+ would only add latency to what is a fresh-reconnect case.

    Upstream 429/503 means account- or IP-level throttling: hammering it
    again within 0.1s only deepens the penalty, so those retry on a long
    ladder (10s, 25s) to let the limit cool down.

    Args:
        attempt: zero-based retry index.
        transport_error: True for connection-level errors (immediate retry).
        rate_limited: True when the error carried HTTP 429/503.

    Returns:
        Seconds to sleep before the next attempt.
    """
    if rate_limited:
        return min(10 + 15 * attempt, 60)
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


def _stream_upstream_chunks(sess, client, url: str, body: bytes, headers: dict):
    """Open one streaming upstream POST and yield decoded text chunks.

    All transport-specific plumbing ends here. The curl_cffi path is
    preferred (real Chrome TLS/h2 fingerprint); its bytes chunks are fed
    through an incremental UTF-8 decoder so multi-byte characters split
    across chunks survive, which a naive per-chunk decode would corrupt.
    The pooled httpx path surfaces iter_text() chunks directly. Both raise
    for HTTP status errors and renew cookies from Set-Cookie.

    Streaming the response even for non-streaming generate() is
    intentional: iterating chunks lets callers enforce a WALL-CLOCK
    deadline, which a plain read timeout cannot do when Google slow-walks
    with a trickle of bytes.

    Args:
        sess: curl_cffi session (preferred), or None to use httpx.
        client: pooled httpx client; required when sess is None.
        url: StreamGenerate endpoint URL.
        body: urlencoded request body (bytes).
        headers: prepared request headers.

    Yields:
        Decoded, non-empty text chunks (str).

    Raises:
        Whatever the transport raises; propagated to the caller unchanged.
    """
    if sess is not None:
        r = sess.post(url, data=body, headers=headers, stream=True,
                      timeout=(10, CONFIG["request_timeout_sec"]))
        try:
            r.raise_for_status()
            _renew_cookies(r)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            for chunk in r.iter_content():
                if chunk:
                    text = decoder.decode(chunk)
                    if text:
                        yield text
            tail = decoder.decode(b"", True)
            if tail:
                yield tail
        finally:
            r.close()
    else:
        with client.stream("POST", url, content=body, headers=headers,
                           timeout=_per_attempt_timeout()) as resp:
            resp.raise_for_status()
            _renew_cookies(resp)
            for chunk in resp.iter_text():
                if chunk:
                    yield chunk


def _generate_upstream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Single-owner upstream call with retries. Uses the pooled httpx client."""
    _refresh_xsrf()
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
                if sess is not None or client is not None:
                    parts = []
                    for chunk in _stream_upstream_chunks(sess, client, url, body, headers):
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
                rate_limited = any(s in str(e) for s in ("503", "429"))
                delay = _retry_delay(attempt, transport_error=_is_transport_error(e),
                                     rate_limited=rate_limited)
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
                buf = ""
                first_delta_t = None
                for chunk in _stream_upstream_chunks(sess, client, url, body, headers):
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
                                if first_delta_t is None:
                                    first_delta_t = time.time()
                                yield delta
            finally:
                slot.__exit__(None, None, None)
            if CONFIG.get("auto_delete_history"):
                schedule_history_delete(stream_cid)
            # Latency breakdown: ttfb (request sent -> first emitted delta)
            # vs total (request sent -> stream end) separates "Google is
            # slow to start" from "generation itself is long" in production
            # logs. Format is a stable grep key; keep it byte-stable.
            ttfb = f"{first_delta_t - t0:.2f}s" if first_delta_t is not None else "n/a"
            log(f"Upstream stream: ttfb={ttfb} total={time.time() - t0:.2f}s chars={len(emitted_raw_text)} attempt={attempt + 1}")
            return
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                rate_limited = any(s in str(e) for s in ("503", "429"))
                delay = _retry_delay(attempt, transport_error=_is_transport_error(e),
                                     rate_limited=rate_limited)
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
    from ..models import resolve_model
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
