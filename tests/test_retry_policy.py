"""Offline retry-policy tests using in-memory HTTP errors, never real requests."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError as UrllibHTTPError, URLError

import httpx
import pytest
from curl_cffi.requests import Response as CurlResponse
from curl_cffi.requests import exceptions as curl_exceptions

from gemini_web2api.budget import (QueueFull, QueueTimeout, RequestCancelled,
                                  RequestControlError, RequestDeadlineExceeded)
from gemini_web2api.upstream import retry
from gemini_web2api.upstream.retry import (EmptyUpstreamResponse, RetryDecision,
                                         UpstreamRejection, retry_decision)


_HTTP_TRANSPORTS = ("httpx", "curl", "urllib")
_TRANSIENT = (408, 425, 429, 500, 502, 503, 504)
_NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def deterministic_backoff(monkeypatch):
    """Stabilize config and randomness. Args: monkeypatch fixture. Returns: None."""
    monkeypatch.setitem(retry.CONFIG, "retry_delay_sec", 2)
    monkeypatch.setattr(retry.random, "uniform", Mock(return_value=0.25))


def _http_error(transport, status, headers=None):
    """Build real HTTP exception types without a client or connection.

    Args:
        transport: Name of the HTTP library whose exception should be returned.
        status: Numeric response status, unrelated to the misleading message.
        headers: Optional response headers with string values.

    Returns:
        An httpx, curl_cffi or urllib HTTP status exception.
    """
    message = "HTTP failure; unrelated request IDs contain 429 and 503"
    if transport == "httpx":
        request = httpx.Request("POST", "https://offline.invalid/generate")
        response = httpx.Response(status, headers=headers, request=request)
        return httpx.HTTPStatusError(message, request=request, response=response)
    if transport == "curl":
        response = CurlResponse()
        response.status_code = status
        response.headers.update(headers or {})
        return curl_exceptions.HTTPError(message, response=response)
    if transport == "urllib":
        message_headers = Message()
        for key, value in (headers or {}).items():
            message_headers[key] = value
        return UrllibHTTPError("https://offline.invalid/generate", status, message, message_headers, None)
    raise AssertionError("unknown test transport")


@pytest.mark.parametrize("code", [0, 429, 503, 1050, 1052, 1100, 999999, -1])
def test_typed_rejections_never_retry(code):
    """Reject every typed code. Args: code under test. Returns: None."""
    error = UpstreamRejection(code)
    assert isinstance(error, RuntimeError)
    assert error.code == code
    assert str(error) == f"Gemini upstream rejected request: BardErrorInfo [{code}]"
    assert retry_decision(error, 0) == RetryDecision(False, "upstream_rejection", 0.0)


@pytest.mark.parametrize("code", [0, 429, 503, 1050, 1052, 1100, 999999])
def test_exact_legacy_rejections_never_retry(code):
    """Retain exact legacy compatibility. Args: code under test. Returns: None."""
    error = RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{code}]")
    assert retry_decision(error, 0) == RetryDecision(False, "upstream_rejection", 0.0)


@pytest.mark.parametrize("error", [
    RuntimeError("prefix Gemini upstream rejected request: BardErrorInfo [1050]"),
    RuntimeError("Gemini upstream rejected request: BardErrorInfo [1050] suffix"),
    RuntimeError("Gemini upstream rejected request: BardErrorInfo [1050]\n"),
    RuntimeError("Gemini upstream rejected request: BardErrorInfo [ 1050 ]"),
    RuntimeError("BardErrorInfo [1050]"),
    ValueError("Gemini upstream rejected request: BardErrorInfo [1050]"),
])
def test_legacy_regex_is_a_full_match_for_runtime_errors_only(error):
    """Avoid loose legacy recognition. Args: noncanonical error. Returns: None."""
    assert retry_decision(error, 0) == RetryDecision(False, "unknown", 0.0)


@pytest.mark.parametrize("error", [UpstreamRejection(503), RuntimeError(str(UpstreamRejection(429)))])
def test_rejection_takes_precedence_over_http_metadata(error):
    """Do not replay a rejection. Args: rejection with HTTP metadata. Returns: None."""
    error.response = SimpleNamespace(status_code=503, headers={"Retry-After": "120"})
    assert retry_decision(error, 0) == RetryDecision(False, "upstream_rejection", 0.0)


@pytest.mark.parametrize("transport", _HTTP_TRANSPORTS)
@pytest.mark.parametrize("status", _TRANSIENT)
def test_transient_http_statuses_retry_before_any_output(transport, status):
    """Use real HTTP metadata. Args: transport and status under test. Returns: None."""
    expected = 10.0 if status in (429, 503) else 2.25
    assert retry_decision(_http_error(transport, status), 0) == RetryDecision(
        True, "http_transient", expected, status,
    )


@pytest.mark.parametrize("transport", _HTTP_TRANSPORTS)
@pytest.mark.parametrize("status", [status for status in range(400, 500) if status not in _TRANSIENT]
                         + [200, 301, 307, 501, 505, 507, 599])
def test_other_http_statuses_do_not_retry_even_with_misleading_message(transport, status):
    """Cover all permanent 4xx. Args: transport and HTTP status. Returns: None."""
    error = _http_error(transport, status, {"Retry-After": "120"})
    assert retry_decision(error, 0) == RetryDecision(False, "http_permanent", 0.0, status)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("attempt, expected", [(0, 10), (1, 25), (2, 40), (3, 55), (4, 60), (99, 60)])
def test_throttling_ladder_preserves_original_policy(status, attempt, expected):
    """Retain long backoff. Args: HTTP status, index and expected delay. Returns: None."""
    assert retry_decision(_http_error("httpx", status), attempt).delay == expected


@pytest.mark.parametrize("attempt, expected", [(0, 3.25), (1, 6.25), (2, 12.25), (3, 15), (10000, 15)])
def test_normal_backoff_reads_config_and_caps_with_jitter(monkeypatch, attempt, expected):
    """Retain bounded exponential backoff. Args: fixture, index and delay. Returns: None."""
    monkeypatch.setitem(retry.CONFIG, "retry_delay_sec", 3)
    assert retry_decision(EmptyUpstreamResponse("no output"), attempt) == RetryDecision(
        True, "empty_response", expected,
    )
    retry.random.uniform.assert_called_once_with(0, 0.5)


@pytest.mark.parametrize("error", [
    ConnectionError("connection lost; request 429"),
    ConnectionResetError("reset"), BrokenPipeError("closed"),
    TimeoutError("read timed out after 503 ms"), OSError("network unreachable"),
    URLError("DNS failure"),
    httpx.TransportError("transport failed"), httpx.ConnectError("connect failed"),
    httpx.ConnectTimeout("connect timeout"), httpx.ReadTimeout("read timeout"),
    httpx.ReadError("read failed"), httpx.WriteError("write failed"),
    httpx.RemoteProtocolError("server disconnected"),
])
def test_actual_network_errors_use_fast_retry_not_message_digits(error):
    """Recognize transport classes. Args: in-memory network error. Returns: None."""
    assert retry_decision(error, 5) == RetryDecision(True, "transport", 0.05)


@pytest.mark.parametrize("name", [
    "RequestException", "ConnectionError", "DNSError", "ConnectTimeout",
    "ReadTimeout", "Timeout", "ProxyError", "SSLError", "ChunkedEncodingError",
])
def test_installed_curl_exception_types_are_transport_errors(name):
    """Exercise the installed curl classes. Args: actual class name. Returns: None."""
    error = getattr(curl_exceptions, name)("request 503 failed", code=429)
    assert isinstance(error, curl_exceptions.RequestException)
    assert retry_decision(error, 0) == RetryDecision(True, "transport", 0.05)


def test_curl_zero_status_is_not_an_http_response():
    """Ignore libcurl zero status. Args: None. Returns: None."""
    response = CurlResponse()
    response.status_code = 0  # libcurl CURLINFO_RESPONSE_CODE before receiving headers
    error = curl_exceptions.RequestException("connect failed", code=503, response=response)
    assert retry_decision(error, 0) == RetryDecision(True, "transport", 0.05)


@pytest.mark.parametrize("name", ["Timeout", "ReadTimeout", "ConnectionError", "RequestException"])
@pytest.mark.parametrize("status", [200, 204, 206])
def test_curl_body_transport_failures_after_success_headers_remain_retryable(name, status):
    """Distinguish body failures from HTTP errors. Args: curl class and 2xx status. Returns: None."""
    response = CurlResponse()
    response.status_code = status
    error = getattr(curl_exceptions, name)("body read interrupted", response=response)
    assert retry_decision(error, 0) == RetryDecision(True, "transport", 0.05, status)
    assert retry_decision(error, 0, emitted=True) == RetryDecision(False, "already_emitted", 0.0, status)


@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_successful_http_metadata_does_not_make_programming_errors_retryable(error_type):
    """Do not retry local failures with a response. Args: programming exception class. Returns: None."""
    error = error_type("failed to process HTTP 200 body")
    error.response = SimpleNamespace(status_code=200, headers={"Retry-After": "120"})
    assert retry_decision(error, 0) == RetryDecision(False, "unknown", 0.0, 200)


def test_curl_http_error_without_metadata_does_not_fall_through_to_oserror():
    """Fail closed on an unknown HTTP status. Args: None. Returns: None."""
    error = curl_exceptions.HTTPError("429: missing response", code=429)
    assert isinstance(error, OSError)
    assert retry_decision(error, 0) == RetryDecision(False, "http_permanent", 0.0)


@pytest.mark.parametrize("name", [
    "InvalidURL", "InvalidProxyURL", "MissingSchema", "InvalidSchema",
    "InvalidHeader", "InvalidJSONError", "CookieConflict", "SessionClosed",
    "StreamConsumedError", "TooManyRedirects", "URLRequired",
    "UnrewindableBodyError", "ImpersonateError", "InterfaceError",
])
def test_curl_local_request_errors_are_not_retried_as_oserror(name):
    """Exclude deterministic curl misuse. Args: installed class name. Returns: None."""
    error = getattr(curl_exceptions, name)("local request configuration invalid")
    assert isinstance(error, OSError)
    assert retry_decision(error, 0) == RetryDecision(False, "unknown", 0.0)


@pytest.mark.parametrize("error", [
    RuntimeError("upstream 429"), RuntimeError("503 Service Unavailable"),
    RuntimeError("empty upstream response"), ValueError("bad status 503"),
    TypeError("429 incompatible value"), AttributeError("missing field"),
    KeyError("503"), ZeroDivisionError("503"), AssertionError("bad invariant"),
    Exception("unrecognized failure"), FileNotFoundError("missing file"),
    PermissionError("denied"), httpx.UnsupportedProtocol("bad scheme"),
    httpx.LocalProtocolError("invalid local headers"),
])
def test_programming_and_unknown_errors_do_not_retry(error):
    """No substring fallback is permitted. Args: non-network error. Returns: None."""
    assert retry_decision(error, 0) == RetryDecision(False, "unknown", 0.0)


@pytest.mark.parametrize("status", [None, 0, -1, True, "429", 503.0, 999])
def test_invalid_status_metadata_does_not_become_an_http_retry(status):
    """Require real numeric HTTP status. Args: malformed status value. Returns: None."""
    error = RuntimeError("503 and 429 in diagnostic only")
    error.response = SimpleNamespace(status_code=status, headers={})
    error.code = 429
    assert retry_decision(error, 0) == RetryDecision(False, "unknown", 0.0)


@pytest.mark.parametrize("transport", _HTTP_TRANSPORTS)
@pytest.mark.parametrize("status", [429, 503, 500])
@pytest.mark.parametrize("value", ["0", "2", "3.5", "75", " 120 ", "86400"])
def test_retry_after_seconds_are_a_minimum_without_a_sixty_second_cap(transport, status, value):
    """Honor server delays. Args: transport, HTTP status and header. Returns: None."""
    error = _http_error(transport, status, {"rEtRy-AfTeR": value})
    fallback = 10.0 if status in (429, 503) else 2.25
    assert retry_decision(error, 0).delay == max(fallback, float(value))


@pytest.mark.parametrize("transport", _HTTP_TRANSPORTS)
@pytest.mark.parametrize("offset", [-120, 0, 120, 86400])
def test_retry_after_httpdate_is_relative_to_utc_and_not_capped(monkeypatch, transport, offset):
    """Honor HTTP dates safely. Args: fixture, transport and UTC offset. Returns: None."""
    monkeypatch.setattr(retry.time, "time", Mock(return_value=_NOW.timestamp()))
    value = format_datetime(_NOW + timedelta(seconds=offset), usegmt=True)
    error = _http_error(transport, 503, {"Retry-After": value})
    assert retry_decision(error, 0).delay == max(10.0, float(offset))


@pytest.mark.parametrize("value", [
    "Wed Jan  1 00:02:00 2025",
    "Wednesday, 01-Jan-25 00:02:00 GMT",
    "Tue, 31 Dec 2024 19:02:00 -0500",
])
def test_retry_after_legacy_httpdate_and_explicit_timezone(monkeypatch, value):
    """Accept supported HTTP date variants. Args: fixture and date. Returns: None."""
    monkeypatch.setattr(retry.time, "time", Mock(return_value=_NOW.timestamp()))
    error = _http_error("httpx", 429, {"Retry-After": value})
    assert retry_decision(error, 0).delay == 120.0


@pytest.mark.parametrize("value", [
    None, 12, object(), b"\xff", "", " ", "no date", "-1", "NaN", "Infinity",
    "1e3", "1, 2", "9" * 5000, "Wed, 99 Jan 2025 00:00:00 GMT",
    "Wed, 01 Jan 999999 00:00:00 GMT",
])
def test_malformed_retry_after_never_raises_or_alters_fallback(value):
    """Treat malformed hints as absent. Args: hostile header value. Returns: None."""
    error = RuntimeError("HTTP exception with response metadata")
    error.response = SimpleNamespace(status_code=503, headers={"Retry-After": value})
    assert retry_decision(error, 1) == RetryDecision(True, "http_transient", 25.0, 503)


@pytest.mark.parametrize("headers", [None, object(), [], {"Other-Header": "120"}])
def test_missing_or_invalid_header_container_falls_back(headers):
    """Tolerate absent header mappings. Args: malformed header container. Returns: None."""
    error = RuntimeError("HTTP exception with response metadata")
    error.response = SimpleNamespace(status_code=429, headers=headers)
    assert retry_decision(error, 0) == RetryDecision(True, "http_transient", 10.0, 429)


def test_header_case_and_ascii_bytes_on_plain_mapping():
    """Read case-insensitive header names. Args: None. Returns: None."""
    error = RuntimeError("HTTP exception with response metadata")
    error.response = SimpleNamespace(status_code=429, headers={"rEtRy-AfTeR": b"120"})
    assert retry_decision(error, 0).delay == 120.0


@pytest.mark.parametrize("transport", _HTTP_TRANSPORTS)
@pytest.mark.parametrize("status", _TRANSIENT)
def test_http_failures_never_replay_after_any_text(monkeypatch, transport, status):
    """Output wins over every transient status. Args: fixture, transport, status. Returns: None."""
    monkeypatch.setattr(retry.random, "uniform", Mock(side_effect=AssertionError("must not compute delay")))
    error = _http_error(transport, status, {"Retry-After": "120"})
    assert retry_decision(error, 0, emitted=True) == RetryDecision(False, "already_emitted", 0.0, status)


@pytest.mark.parametrize("error", [
    UpstreamRejection(1050), RuntimeError(str(UpstreamRejection(1052))),
    EmptyUpstreamResponse(), OSError("network down"), TimeoutError("timeout"),
    httpx.ReadError("read failed"), curl_exceptions.RequestException("connection lost"),
    RuntimeError("unknown"),
])
def test_every_error_stops_after_emission(error):
    """Never replay partially emitted output. Args: exception under test. Returns: None."""
    assert retry_decision(error, 0, emitted=True) == RetryDecision(False, "already_emitted", 0.0)


@pytest.mark.parametrize("error_type", [
    RequestControlError, RequestDeadlineExceeded, RequestCancelled, QueueTimeout, QueueFull,
])
def test_request_control_errors_are_terminal_even_with_http_metadata(error_type):
    """Defend nested retry callers. Args: actual control exception class. Returns: None."""
    error = error_type("503 request control signal")
    error.response = SimpleNamespace(status_code=503, headers={"Retry-After": "120"})
    assert retry_decision(error, 0) == RetryDecision(False, "request_control", 0.0)
    assert not retry_decision(error, 0, emitted=True).retryable


def test_retry_decision_is_frozen_and_status_is_optional():
    """Keep the public immutable result shape. Args: None. Returns: None."""
    decision = RetryDecision(True, "transport", 0.05)
    assert decision.status_code is None
    with pytest.raises(FrozenInstanceError):
        decision.retryable = False
