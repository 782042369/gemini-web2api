"""Multimodal: browser-aligned two-step Scotty resumable upload."""
import base64
import urllib.request
import urllib.parse
import time
import ssl
import re
import uuid
from urllib.parse import urlparse

from .config import CONFIG
from .gemini import load_cookie, make_sapisidhash, _get_ssl_ctx, log, get_browser_session, CHROME_UA


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the Gemini app page.

    Returns:
        dict with "push_id" (qKIAYe), "pctx" (Ylro7b) and "at" (thykhd)
        when present; {} on failure. The push_id binds uploads to the
        signed-in account's storage bucket - without it an upload would
        land in the anonymous bucket whose references StreamGenerate
        rejects with BardErrorInfo 1100.
    """
    headers = {
        "User-Agent": CHROME_UA,
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    try:
        sess = get_browser_session()
        if sess is not None:
            resp = sess.get("https://gemini.google.com/app", headers=headers, timeout=30)
            html = resp.text
        else:
            req = urllib.request.Request("https://gemini.google.com/app", headers=headers)
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
                )
                resp = opener.open(req, timeout=30)
            else:
                resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
            html = resp.read().decode()
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            ("at", r'"SNlM0e":"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
        return tokens
    except Exception as e:
        log(f"Page token fetch failed: {e}")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0}


def _cached_page_tokens() -> dict:
    """Return page tokens refreshed at most every 600s.

    Returns:
        Cached token dict (see _get_page_tokens).
    """
    now = time.time()
    if now - _page_tokens_cache["ts"] > 600:
        _page_tokens_cache["tokens"] = _get_page_tokens()
        _page_tokens_cache["ts"] = now
    return _page_tokens_cache["tokens"]


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature.

    Parameters:
        image_bytes: raw image bytes.
        fallback: MIME returned when no signature matches.

    Returns:
        The sniffed MIME type string.
    """
    if not isinstance(image_bytes, bytes):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


def fetch_image_bytes(url: str) -> bytes:
    """Fetch image bytes from an http(s) URL.

    Parameters:
        url: image URL to download.

    Returns:
        Raw image bytes, or b"" on unsupported scheme / failure.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log(f"Image fetch skipped for unsupported URL scheme: {parsed.scheme or 'none'}")
        return b""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            resp = opener.open(req, timeout=30)
        else:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
        return resp.read()
    except Exception as e:
        log(f"Image fetch failed: {e}")
        return b""


def _sanitize_upload_name(name: str) -> str:
    """Strip characters that would break the start-request body.

    Parameters:
        name: requested filename.

    Returns:
        A safe single-line filename ("upload.bin" when empty).
    """
    name = (name or "").strip().replace("\r", "").replace("\n", "")
    return name or "upload.bin"


def _upload_post(url: str, headers: dict, data: bytes):
    """POST bytes through the shared browser session (same exit as generate).

    Parameters:
        url: absolute upload URL.
        headers: request headers.
        data: raw request body bytes.

    Returns:
        (status_code, response_headers_dict, response_body_text).

    Raises:
        RuntimeError: on transport failure.
    """
    sess = get_browser_session()
    if sess is not None:
        resp = sess.post(url, headers=headers, data=data, timeout=90)
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    proxy = CONFIG.get("proxy")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
        )
        resp = opener.open(req, timeout=90)
    else:
        resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=90)
    body = resp.read().decode("utf-8", "replace")
    heads = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status, heads, body


def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png") -> str:
    """Upload an image via the browser-aligned two-step Scotty resumable flow.

    Step 1 ("start") posts the filename + length to push.clients6.google.com
    with the account's page push_id and receives a one-time upload URL;
    step 2 ("upload, finalize") posts the raw bytes and the plain-text
    response body is the /contrib_service/... file reference. Upload and
    generation share the same session/exit so Google sees one account.

    The earlier one-shot multipart POST to content-push.googleapis.com also
    uploads successfully, but the references it yields are now rejected by
    StreamGenerate with BardErrorInfo 1100 - the two-step flow is what the
    current web client actually uses.

    Parameters:
        image_bytes: raw image bytes to upload.
        filename: filename reported to Google in the start step.
        mime_type: informational MIME (the resumable flow sends no mime).

    Returns:
        File reference path (e.g. /contrib_service/ttl_1d/...).

    Raises:
        RuntimeError: when page tokens are unavailable, either HTTP step
            fails, or the response is not a valid file reference.
    """
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id")
    if not push_id:
        raise RuntimeError(
            "upload aborted: page push_id unavailable - references uploaded "
            "without it are rejected by StreamGenerate (BardErrorInfo 1100)")
    pctx = tokens.get("pctx")

    cookie_str, sapisid = load_cookie()
    base = {
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "X-Tenant-Id": "bard-storage",
        "Push-ID": push_id,
        "Accept": "*/*",
        "User-Agent": CHROME_UA,
    }
    if pctx:
        base["X-Client-Pctx"] = pctx
    if cookie_str:
        base["Cookie"] = cookie_str
    if sapisid:
        base["Authorization"] = make_sapisidhash(sapisid)

    # Step 1: start - exchange filename/length for a one-time upload URL.
    start_headers = dict(base)
    start_headers.update({
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
    })
    status, heads, body = _upload_post(
        "https://push.clients6.google.com/upload/",
        start_headers,
        ("File name: " + _sanitize_upload_name(filename)).encode(),
    )
    if status != 200:
        raise RuntimeError(f"upload start failed: HTTP {status} {body[:160]}")
    put_url = heads.get("x-goog-upload-url")
    if not put_url:
        raise RuntimeError("upload start: no x-goog-upload-url in response")

    # Step 2: upload + finalize - raw bytes in, file reference out.
    up_headers = dict(base)
    up_headers.update({
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
    })
    status, _, body = _upload_post(put_url, up_headers, image_bytes)
    if status != 200:
        raise RuntimeError(f"upload finalize failed: HTTP {status} {body[:160]}")
    ref = body.strip()
    if not ref.startswith("/"):
        raise RuntimeError(f"upload returned a non-reference body: {ref[:160]}")
    return ref
