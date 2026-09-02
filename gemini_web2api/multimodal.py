"""Multimodal: one-shot multipart upload for Gemini image input."""
import json
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
    """Fetch WIZ_global_data tokens from Gemini page (Push-ID, X-Client-Pctx)."""
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
            ("at", r'"thykhd":"([^"]+)"'),
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
    now = time.time()
    if now - _page_tokens_cache["ts"] > 600:
        _page_tokens_cache["tokens"] = _get_page_tokens()
        _page_tokens_cache["ts"] = now
    return _page_tokens_cache["tokens"]


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
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


def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png") -> str:
    """Upload image via one-shot multipart POST to Google content-push.

    Matches the current Gemini web client (same approach as
    HanaokaYuzu/Gemini-API): a single multipart/form-data POST carries
    Push-ID + X-Tenant-Id headers plus the file part, and the plain-text
    response body is the /contrib_service/... file reference. The old
    two-step Scotty resumable flow still uploads, but StreamGenerate now
    rejects those references with BardErrorInfo 1100/1003.

    Parameters:
        image_bytes: raw image bytes to upload.
        filename: filename reported to Google for the multipart file part.
        mime_type: MIME type of the image (e.g. image/jpeg).

    Returns:
        File reference path (e.g. /contrib_service/ttl_1d/...).

    Raises:
        RuntimeError: on HTTP failure or when the response is not a valid
            file reference.
    """
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id", "feeds/mcudyrk2a4khkz")

    cookie_str, sapisid = load_cookie()
    headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "User-Agent": CHROME_UA,
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    url = "https://content-push.googleapis.com/upload"
    sess = get_browser_session()
    if sess is not None:
        # Preferred path: real Chrome TLS fingerprint via curl_cffi.
        # curl_cffi does not support the "files" kwarg; it wants a CurlMime part.
        from curl_cffi import CurlMime
        mime_part = CurlMime()
        try:
            mime_part.addpart(name="file", content_type=mime_type,
                              filename=filename, data=image_bytes)
            resp = sess.post(url, multipart=mime_part, headers=headers, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Image upload failed: HTTP {resp.status_code} {resp.text[:120]}")
            file_ref = resp.text.strip()
        finally:
            mime_part.close()
    else:
        # Fallback: hand-rolled multipart body over urllib.
        boundary = "----geminiweb2api" + uuid.uuid4().hex
        body = (
            b"--" + boundary.encode() + b"\r\n"
            + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
            + f"Content-Type: {mime_type}\r\n\r\n".encode()
            + image_bytes + b'\r\n'
            + b"--" + boundary.encode() + b"--\r\n"
        )
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        ctx = _get_ssl_ctx()
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx),
            )
            resp = opener.open(req, timeout=60)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=60)
        file_ref = resp.read().decode().strip()

    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError(f"Invalid file reference: {file_ref[:100]}")

    log(f"Image uploaded (multipart): {filename} -> {file_ref[:50]}...")
    return file_ref


def fetch_image_bytes(url: str) -> bytes:
    """Fetch image from URL."""
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
