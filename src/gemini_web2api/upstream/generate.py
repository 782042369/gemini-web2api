"""Bounded upstream generation, retry classification and single-flight sharing."""
import codecs
import json
import re
import threading
import time
import uuid

from ..config import CONFIG
from ..budget import (RequestBudget, RequestControlError, RequestCancelled, RequestDeadlineExceeded,
                      budget_scope, check_budget, current_budget,
                      positive_seconds, remaining_timeout)
from ..logs import log
from .concurrency import _UpstreamSlot
from .cookies import _active_auth_user, _active_cookie_path, pick_next_cookie
from .history import schedule_history_delete
from .parser import (_extract_conversation_id, _extract_texts_from_line, clean_text,
                     extract_conversation_id, extract_response_text)
from .retry import EmptyUpstreamResponse, UpstreamRejection, retry_decision
from .retry import _retry_delay as _retry_delay  # backward-compatible import path
from .protocol import _build_headers, _build_payload, _get_url
from .transport import (HAS_HTTPX, _get_httpx_client, _urllib_post,
                        curl_total_timeout, get_browser_session)

try:
    import httpx
except ImportError:
    httpx = None

_inflight = {}
_inflight_lock = threading.Lock()


class _AttemptDeadlineExceeded(TimeoutError):
    """One attempt's slow-walk limit expired; the overall budget is unchanged."""


def _renew_cookies(resp):
    """Renew active cookies. Args: response. Returns: None."""
    from ..keepalive import _merge_response_cookies
    _merge_response_cookies(resp)


def _refresh_xsrf():
    """Refresh active XSRF under the same request budget. Args: None. Returns: None."""
    from ..keepalive import _maybe_refresh_xsrf
    check_budget("session refresh")
    _maybe_refresh_xsrf()
    check_budget("session refresh")


def _is_transport_error(error):
    """Check classifier compatibility. Args: exception. Returns: transport category flag."""
    return retry_decision(error, 0).category == "transport"


def _inflight_key(prompt, model_id, think_mode, file_refs, extra_fields):
    """Build an account/options-safe sharing key. Args: generation arguments. Returns: tuple."""
    return (prompt, model_id, think_mode, _active_cookie_path(), _active_auth_user(),
            tuple((str(ref), getattr(ref, "mime_type", None)) for ref in (file_refs or [])),
            json.dumps(extra_fields, sort_keys=True, separators=(",", ":")) if extra_fields else None)


def generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    """Generate once for identical concurrent calls without coupling waiter deadlines.

    Args:
        prompt: Flattened prompt.
        model_id: Gemini mode.
        think_mode: Thinking level.
        file_refs: Uploaded references for this account.
        extra_fields: Additional protocol slots.

    Returns:
        Non-empty text, or raises; a timed-out follower never returns None as success.
    """
    with budget_scope() as budget:
        key = _inflight_key(prompt, model_id, think_mode, file_refs, extra_fields)
        while True:
            budget.check("coalesced generation")
            with _inflight_lock:
                entry = _inflight.get(key)
                owner = entry is None
                if owner:
                    entry = {"event": threading.Event(), "result": None, "error": None}
                    _inflight[key] = entry
            if owner:
                break
            budget.wait(entry["event"], "coalesced generation")
            if isinstance(entry["error"], (RequestCancelled, RequestDeadlineExceeded)):
                # Another request's shorter deadline/cancellation is not ours.
                # Rejoin ownership only within this waiter's original budget.
                budget.check("coalesced owner ended")
                continue
            if entry["error"] is not None:
                raise entry["error"]
            if not isinstance(entry["result"], str) or not entry["result"].strip():
                raise EmptyUpstreamResponse("coalesced generation returned empty output")
            return entry["result"]
        try:
            result = _generate_upstream(prompt, model_id, think_mode, file_refs, extra_fields)
            budget.check("generation completion")
            entry["result"] = result
            return result
        except BaseException as exc:
            entry["error"] = exc if isinstance(exc, Exception) else RuntimeError("shared generation interrupted")
            raise
        finally:
            with _inflight_lock:
                entry["event"].set()
                _inflight.pop(key, None)


def _attempt_deadline():
    """Cap one attempt by its slow-walk and overall limits. Args: None. Returns: seconds."""
    cap = positive_seconds("request_timeout_sec", 180)
    try:
        slow = float(CONFIG.get("slow_retry_sec") or 0)
        if slow > 0:
            cap = min(cap, slow)
    except (TypeError, ValueError):
        pass
    return remaining_timeout(cap, "upstream generation")


def _per_attempt_timeout():
    """Return finite httpx phase timeouts. Args: None. Returns: httpx timeout or seconds."""
    seconds = _attempt_deadline()
    return httpx.Timeout(seconds, connect=min(10, seconds)) if HAS_HTTPX else seconds


def _stream_upstream_chunks(sess, client, url, body, headers):
    """Read one response under native/phase timeouts and a monotonic attempt bound.

    Args:
        sess: Preferred curl_cffi session or None.
        client: httpx fallback or None.
        url: Gemini endpoint.
        body: Encoded request bytes.
        headers: Prepared account headers.

    Yields:
        Decoded text. curl uses TIMEOUT_MS even for streaming headers/trickle
        reads; fallback phase timeouts are reinforced by between-chunk checks.
    """
    seconds = _attempt_deadline()
    until = time.monotonic() + seconds

    def check():
        """Check both bounds. Args: None. Returns: None or raises."""
        check_budget("upstream response")
        if time.monotonic() >= until:
            raise _AttemptDeadlineExceeded(f"slow-walk breaker: attempt exceeded {seconds:.0f}s")

    if sess is not None:
        with curl_total_timeout(sess, seconds):
            response = sess.post(url, data=body, headers=headers, stream=True,
                                 timeout=(min(10, seconds), seconds))
        try:
            check()
            response.raise_for_status()
            _renew_cookies(response)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            for chunk in response.iter_content():
                check()
                if chunk:
                    decoded = decoder.decode(chunk)
                    if decoded:
                        yield decoded
            check()
            tail = decoder.decode(b"", True)
            if tail:
                yield tail
        finally:
            response.close()
    elif client is not None:
        with client.stream("POST", url, content=body, headers=headers, timeout=_per_attempt_timeout()) as response:
            check()
            response.raise_for_status()
            _renew_cookies(response)
            for chunk in response.iter_text():
                check()
                if chunk:
                    yield chunk
            check()
    else:
        raw = _urllib_post(url, body, headers, timeout=seconds)
        check()
        yield raw


def _attempts():
    """Read a bounded retry count. Args: None. Returns: positive number of attempts."""
    value = CONFIG.get("retry_attempts", 3)
    return max(1, min(value, 10)) if isinstance(value, int) and not isinstance(value, bool) else 3


def _retry(error, attempt, attempts, emitted=False):
    """Apply classified backoff only if useful and affordable.

    Args:
        error: Failed attempt exception.
        attempt: Zero-based attempt number.
        attempts: Maximum total attempts.
        emitted: Whether any output was already sent downstream.

    Returns:
        True for another attempt, False for terminal failures. Budget expiry
        raises instead of extending the deadline to accommodate Retry-After.
    """
    if isinstance(error, RequestControlError):
        raise error
    check_budget("retry decision")
    decision = retry_decision(error, attempt, emitted=emitted)
    if not decision.retryable or attempt + 1 >= attempts:
        log(f"Upstream retry stopped: category={decision.category} attempt={attempt + 1} emitted={emitted}")
        return False
    log(f"Retry {attempt + 1}/{attempts} in {decision.delay:.2f}s: category={decision.category}")
    current_budget().sleep(decision.delay)
    return True


def _generate_upstream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    """Generate under a single queue/upload/retry budget. Args: generation fields. Returns: non-empty text."""
    with budget_scope():
        request_uuid = str(uuid.uuid4()).upper()
        attempts = _attempts()
        for attempt in range(attempts):
            try:
                with _UpstreamSlot():
                    _refresh_xsrf()
                    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, request_uuid).encode()
                    url, headers = _get_url(), _build_headers(request_uuid)
                    sess = get_browser_session()
                    client = _get_httpx_client() if sess is None and HAS_HTTPX else None
                    started = time.monotonic()
                    raw = "".join(_stream_upstream_chunks(sess, client, url, body, headers))
                text = extract_response_text(raw)
                if not text.strip():
                    raise EmptyUpstreamResponse("upstream returned empty output")
                check_budget("generation completion")
                log(f"Upstream generate: {time.monotonic() - started:.2f}s chars={len(text)} attempt={attempt + 1}")
                if CONFIG.get("auto_delete_history"):
                    schedule_history_delete(extract_conversation_id(raw))
                return text
            except Exception as exc:
                if not _retry(exc, attempt, attempts):
                    raise


def generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None):
    """Stream using one budget without leaking thread context across yields.

    Args:
        prompt: Flattened prompt.
        model_id: Gemini mode.
        think_mode: Thinking level.
        file_refs: Uploaded references.
        extra_fields: Protocol overrides.

    Yields:
        Text deltas. Interleaved library streams retain independent deadlines;
        HTTP calls retain their explicit request budget. Close always releases
        the response and capacity, even when the budget has expired.
    """
    budget = current_budget() or RequestBudget()
    stream = _generate_stream_owned(prompt, model_id, think_mode, file_refs, extra_fields)
    try:
        while True:
            try:
                with budget_scope(budget):
                    delta = next(stream)
            except StopIteration:
                return
            yield delta
    finally:
        with budget_scope(budget, check=False):
            stream.close()


def _generate_stream_owned(prompt, model_id, think_mode, file_refs, extra_fields):
    """Run a stream under the wrapper's active budget. Args: generation fields. Yields: deltas."""
    request_uuid = str(uuid.uuid4()).upper()
    attempts = _attempts()
    for attempt in range(attempts):
        emitted = False
        raw_text = ""
        conversation_id = None
        first_delta = None
        try:
            with _UpstreamSlot():
                _refresh_xsrf()
                body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, request_uuid).encode()
                url, headers = _get_url(), _build_headers(request_uuid)
                sess = get_browser_session()
                client = _get_httpx_client() if sess is None and HAS_HTTPX else None
                started = time.monotonic()
                buf = ""

                def deltas(line):
                    """Parse a snapshot. Args: one protocol line. Yields: new text only."""
                    nonlocal raw_text, conversation_id
                    conversation_id = _extract_conversation_id(line) or conversation_id
                    texts = _extract_texts_from_line(line)
                    if not texts:
                        return
                    value = max(texts, key=len)
                    if value == raw_text or raw_text.startswith(value):
                        return
                    if not value.startswith(raw_text):
                        raise RuntimeError("Gemini stream content changed")
                    delta = clean_text(value[len(raw_text):], strip=False)
                    raw_text = value
                    if delta:
                        yield delta

                chunks = _stream_upstream_chunks(sess, client, url, body, headers)
                try:
                    for chunk in chunks:
                        check_budget("stream generation")
                        buf += chunk
                        rejected = re.search(r'BardErrorInfo(?:\s*"?\s*,)?\s*\[\s*(\d+)\s*\]', buf)
                        if rejected:
                            raise UpstreamRejection(int(rejected.group(1)))
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            for delta in deltas(line):
                                if first_delta is None:
                                    first_delta = time.monotonic()
                                emitted = True
                                yield delta
                                check_budget("stream delivery")
                    for delta in deltas(buf):
                        if first_delta is None:
                            first_delta = time.monotonic()
                        emitted = True
                        yield delta
                        check_budget("stream delivery")
                finally:
                    chunks.close()
            if not emitted:
                raise EmptyUpstreamResponse("upstream stream returned empty output")
            if CONFIG.get("auto_delete_history"):
                schedule_history_delete(conversation_id)
            ttfb = f"{first_delta - started:.2f}s" if first_delta is not None else "n/a"
            log(f"Upstream stream: ttfb={ttfb} total={time.monotonic() - started:.2f}s chars={len(raw_text)} attempt={attempt + 1}")
            return
        except Exception as exc:
            if not _retry(exc, attempt, attempts, emitted=emitted):
                raise


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
