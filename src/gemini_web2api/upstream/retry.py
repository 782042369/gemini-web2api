"""Pure retry classification; request budgets and sleeping belong to the caller.

Only explicitly transient failures may replay a request, and never after text
has escaped to the client. HTTP metadata, not error-message substrings, decides
status policy. A valid Retry-After is a minimum wait, not a capped suggestion.
"""
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.error import HTTPError as UrllibHTTPError

from ..budget import RequestControlError
from ..config import CONFIG

try:
    import httpx
except ImportError:
    httpx = None

try:
    from curl_cffi.requests import exceptions as curl_exceptions
except ImportError:
    curl_exceptions = None


_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_LEGACY_REJECTION = re.compile(r"Gemini upstream rejected request: BardErrorInfo \[([0-9]+)\]")
_TRANSPORT_ERRORS = (ConnectionError, TimeoutError, OSError)
_HTTP_ERRORS = (UrllibHTTPError,)
_LOCAL_ERRORS = (
    ValueError, TypeError, RuntimeError, FileNotFoundError, PermissionError,
    IsADirectoryError, NotADirectoryError,
)
if httpx is not None:
    _TRANSPORT_ERRORS += (httpx.TransportError,)
    _HTTP_ERRORS += (httpx.HTTPStatusError,)
    _LOCAL_ERRORS += (httpx.UnsupportedProtocol, httpx.LocalProtocolError)
if curl_exceptions is not None:
    _TRANSPORT_ERRORS += (curl_exceptions.RequestException,)
    _HTTP_ERRORS += (curl_exceptions.HTTPError,)
    # These are request construction/state failures despite inheriting OSError.
    _LOCAL_ERRORS += tuple(
        getattr(curl_exceptions, name)
        for name in (
            "CookieConflict", "ImpersonateError", "InterfaceError",
            "InvalidJSONError", "SessionClosed", "TooManyRedirects",
            "URLRequired", "UnrewindableBodyError",
        )
        if isinstance(getattr(curl_exceptions, name, None), type)
    )


class UpstreamRejection(RuntimeError):
    """A structured BardErrorInfo refusal that must not replay the same request."""

    def __init__(self, code: int):
        """Retain the parsed rejection code and legacy public error message.

        Args:
            code: Numeric BardErrorInfo code returned by Gemini.

        Returns:
            None.
        """
        self.code = code
        super().__init__(f"Gemini upstream rejected request: BardErrorInfo [{code}]")


class EmptyUpstreamResponse(RuntimeError):
    """An upstream attempt finished without producing any usable output."""


@dataclass(frozen=True)
class RetryDecision:
    """A retry verdict, diagnostic category, minimum delay and optional HTTP code."""

    retryable: bool
    category: str
    delay: float
    status_code: Optional[int] = None


def _http_status(error: BaseException) -> Optional[int]:
    """Read actual HTTP status metadata without confusing libcurl error codes.

    Args:
        error: Exception with an optional response or urllib HTTPError code.

    Returns:
        A valid numeric HTTP status, or None when no HTTP response is known.
    """
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(error, UrllibHTTPError):
        status = error.code
    # curl responses use zero before receiving HTTP headers. It is not HTTP 0.
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return int(status)
    return None


def _retry_after(error: BaseException) -> Optional[float]:
    """Parse Retry-After safely as seconds or a UTC HTTP date.

    Args:
        error: HTTP exception whose response/urllib headers may contain a hint.

    Returns:
        Nonnegative finite seconds, with no upper cap, or None for invalid data.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None and isinstance(error, UrllibHTTPError):
        headers = error.headers
    try:
        value = next(
            (value for key, value in headers.items()
             if isinstance(key, str) and key.lower() == "retry-after"),
            None,
        )
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if not isinstance(value, str):
            return None
        value = value.strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0.0, date.timestamp() - time.time())
        return seconds if math.isfinite(seconds) else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _retry_delay(attempt: int, transport_error: bool = False,
                 rate_limited: bool = False) -> float:
    """Keep the original transport, throttling and jittered exponential ladders.

    Args:
        attempt: Zero-based retry index.
        transport_error: Whether a connection-level failure allows a fast retry.
        rate_limited: Whether HTTP 429 or 503 requires the longer cooldown.

    Returns:
        Local backoff seconds, capped at 15 normally or 60 for throttling.
    """
    attempt = max(0, attempt)
    if rate_limited:
        return float(min(10 + 15 * attempt, 60))
    if transport_error:
        return 0.05
    base = CONFIG.get("retry_delay_sec", 2)
    try:
        exponential = math.ldexp(base, attempt)
    except OverflowError:
        exponential = 15.0
    return min(exponential + random.uniform(0, 0.5), 15.0)


def retry_decision(error: BaseException, attempt: int, emitted: bool = False) -> RetryDecision:
    """Classify one failure without issuing requests, sleeping or changing state.

    Budget/deadline/cancellation exceptions should be caught by the caller first;
    they are also terminal here as a defense against nested retry amplification.
    Categories are already_emitted, request_control, upstream_rejection, http_transient,
    http_permanent, empty_response, transport and unknown. Nonretryable results
    always carry zero delay. The caller must stop if its remaining budget cannot
    accommodate the delay, including a server Retry-After larger than 60 seconds.

    Args:
        error: Failure from one upstream attempt.
        attempt: Zero-based retry index used by the existing backoff ladders.
        emitted: Whether any text has already been delivered to the client.

    Returns:
        An immutable retry verdict with category, delay and optional HTTP status.
    """
    if emitted:
        return RetryDecision(False, "already_emitted", 0.0, _http_status(error))
    if isinstance(error, RequestControlError):
        return RetryDecision(False, "request_control", 0.0)
    if isinstance(error, UpstreamRejection) or (
        isinstance(error, RuntimeError) and _LEGACY_REJECTION.fullmatch(str(error))
    ):
        return RetryDecision(False, "upstream_rejection", 0.0)

    status = _http_status(error)
    # A successful response header does not make a later body timeout an HTTP
    # rejection: curl can attach a 200 response to a genuine transport failure.
    if status is not None and status >= 400:
        if status not in _TRANSIENT_HTTP_STATUSES:
            return RetryDecision(False, "http_permanent", 0.0, status)
        delay = _retry_delay(attempt, rate_limited=status in (429, 503))
        server_delay = _retry_after(error)
        if server_delay is not None:
            delay = max(delay, server_delay)
        return RetryDecision(True, "http_transient", delay, status)
    # Explicit HTTP status errors (including redirects or missing metadata) are
    # not connection errors, despite urllib/curl HTTPError inheriting OSError.
    if isinstance(error, _HTTP_ERRORS):
        return RetryDecision(False, "http_permanent", 0.0, status)
    if isinstance(error, EmptyUpstreamResponse):
        return RetryDecision(True, "empty_response", _retry_delay(attempt), status)
    if isinstance(error, _LOCAL_ERRORS):
        return RetryDecision(False, "unknown", 0.0, status)
    if isinstance(error, _TRANSPORT_ERRORS):
        return RetryDecision(True, "transport", _retry_delay(attempt, transport_error=True), status)
    return RetryDecision(False, "unknown", 0.0, status)
