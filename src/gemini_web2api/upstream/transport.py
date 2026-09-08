"""HTTP transports for upstream calls.

Transport ladder (best first): curl_cffi session impersonating a real Chrome
TLS/h2 fingerprint, pooled httpx client, bare urllib fallback. Selection is
transparent to callers; HAS_HTTPX / HAS_CURL_CFFI report availability.
"""
import ssl
import threading
import urllib.request
from contextlib import contextmanager

from ..budget import check_budget, remaining_timeout

from ..config import CONFIG

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


_ssl_ctx = None
_httpx_client = None


# Full Chrome UA matching the curl_cffi impersonation profile. The previous
# truncated "Mozilla/5.0 (... ) AppleWebKit/537.36" string is itself a bot tell.
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


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


@contextmanager
def curl_total_timeout(session, seconds):
    """Set a real libcurl wall-clock bound, including streaming header waits.

    Args:
        session: Thread-local curl_cffi session; fake/absent transports are left alone.
        seconds: Positive maximum duration for this single transport operation.

    Yields:
        None. Restores existing session options so later requests are unaffected.
    """
    options = getattr(session, "curl_options", None)
    if not HAS_CURL_CFFI or not isinstance(options, dict):
        yield
        return
    from curl_cffi import CurlOpt
    sentinel = object()
    previous = options.get(CurlOpt.TIMEOUT_MS, sentinel)
    milliseconds = max(1, int(seconds * 1000))
    if isinstance(previous, (int, float)) and previous > 0:
        milliseconds = min(milliseconds, previous)
    options[CurlOpt.TIMEOUT_MS] = milliseconds
    try:
        yield
    finally:
        if previous is sentinel:
            options.pop(CurlOpt.TIMEOUT_MS, None)
        else:
            options[CurlOpt.TIMEOUT_MS] = previous


def read_urllib_response(response, stage):
    """Read a fallback response cooperatively under the current request budget.

    Args:
        response: urllib HTTPResponse with read1 support.
        stage: Safe timeout stage name.

    Returns:
        Complete response bytes; checks the budget between raw reads. DNS and
        urllib header parsing still rely on their underlying socket timeout.
    """
    chunks = []
    while True:
        check_budget(stage)
        chunk = response.read1(65536)
        check_budget(stage)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _urllib_post(url: str, body: bytes, headers: dict, timeout=None) -> str:
    """Fallback POST via urllib (no connection pooling), used when httpx is absent."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    proxy = CONFIG.get("proxy")
    ctx = _get_ssl_ctx()
    to = remaining_timeout(timeout if isinstance(timeout, (int, float)) else CONFIG["request_timeout_sec"],
                           "upstream request")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        resp = opener.open(req, timeout=to)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=to)
    with resp:
        return read_urllib_response(resp, "upstream response").decode("utf-8", errors="replace")
