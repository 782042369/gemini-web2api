"""Multimodal: browser-aligned two-step Scotty resumable upload."""
import os
import urllib.request
import urllib.parse
import time
import re
import threading

from .config import CONFIG
from .budget import RequestControlError, budget_lock, check_budget, remaining_timeout
from .image_fetch import fetch_image_bytes as fetch_image_bytes
from .logs import log
from .upstream.cookies import _active_auth_user, _active_cookie_path, load_cookie
from .upstream.protocol import make_sapisidhash
from .upstream.transport import (CHROME_UA, _get_ssl_ctx, get_browser_session,
                                 curl_total_timeout, read_urllib_response)


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the Gemini app page.

    Returns:
        dict with "push_id" (qKIAYe), "pctx" (Ylro7b) and "at" (SNlM0e)
        when present; {} on failure. The push_id binds uploads to the
        signed-in account's storage bucket - without it an upload would
        land in the anonymous bucket whose references StreamGenerate
        rejects with BardErrorInfo 1100.
    """
    check_budget("image session refresh")
    auth_user = _active_auth_user()
    account_prefix = f"/u/{auth_user}" if auth_user not in (None, "") else ""
    headers = {
        "User-Agent": CHROME_UA,
        "Referer": f"https://gemini.google.com{account_prefix}/app",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(auth_user)
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    try:
        sess = get_browser_session()
        if sess is not None:
            timeout = remaining_timeout(30, "image session refresh")
            with curl_total_timeout(sess, timeout):
                resp = sess.get(f"https://gemini.google.com{account_prefix}/app", headers=headers, timeout=timeout)
                try:
                    check_budget("image session refresh")
                    html = resp.text
                finally:
                    resp.close()
        else:
            req = urllib.request.Request(f"https://gemini.google.com{account_prefix}/app", headers=headers)
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
                )
                resp = opener.open(req, timeout=remaining_timeout(30, "image session refresh"))
            else:
                resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=remaining_timeout(30, "image session refresh"))
            with resp:
                html = read_urllib_response(resp, "image session refresh").decode()
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            ("at", r'"SNlM0e":"([^"]+)"'),
            # f.sid binds StreamGenerate/ProcessFile to the live page session;
            # bl is the freshest frontend build label (overrides config gemini_bl).
            ("f_sid", r'"FdrFJe":"([^"]+)"'),
            ("bl", r'"cfb2h":"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
        return tokens
    except RequestControlError:
        raise
    except Exception as e:
        check_budget("image session refresh")
        log(f"Page token fetch failed: {e}")
        return {}


_page_tokens_cache = {}  # (cookie path, auth_user) -> tokens, freshness, lock
_page_tokens_lock = threading.RLock()


def _cached_page_tokens() -> dict:
    """Fetch tokens once per account without blocking unrelated image accounts.

    Args:
        None; account context is bound to the current request thread.

    Returns:
        Tokens for the selected account. Complete upload tokens are cached for
        600 seconds, failed/incomplete fetches for only 30 seconds. A cookie-file
        change invalidates the entry immediately.
    """
    path = _active_cookie_path() or "__anonymous__"
    key = (path, _active_auth_user())
    with _page_tokens_lock:
        cache = _page_tokens_cache.setdefault(key, {"tokens": {}, "ts": None,
                                                   "mtime": None, "lock": threading.Lock()})
    with budget_lock(cache["lock"], "image session cache"):
        now = time.monotonic()
        try:
            cookie_mtime = os.path.getmtime(path) if path != "__anonymous__" else 0.0
        except OSError:
            cookie_mtime = 0.0
        # SNlM0e has been removed from the page upstream; push_id alone marks
        # a usable token set (f_sid/bl ride along when present).
        ttl = 600 if cache["tokens"].get("push_id") else 30
        if cache["ts"] is not None and now - cache["ts"] < ttl and cache["mtime"] == cookie_mtime:
            return dict(cache["tokens"])
        tokens = _get_page_tokens()
        cache.update(tokens=tokens, ts=time.monotonic(), mtime=cookie_mtime)
        return dict(tokens)


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
    timeout = remaining_timeout(90, "image upload")
    sess = get_browser_session()
    if sess is not None:
        with curl_total_timeout(sess, timeout):
            resp = sess.post(url, headers=headers, data=data, timeout=timeout)
            try:
                check_budget("image upload")
                return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text
            finally:
                resp.close()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    proxy = CONFIG.get("proxy")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
        )
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=timeout)
    with resp:
        body = read_urllib_response(resp, "image upload").decode("utf-8", "replace")
        heads = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, heads, body


def _upload_multipart_once(image_bytes: bytes, filename: str, mime_type: str, push_id: str):
    """One-shot multipart upload to content-push.googleapis.com.

    Browser-session (curl_cffi) only: shares the impersonated Chrome TLS
    fingerprint with the generate path, matching the reference client
    HanaokaYuzu/Gemini-API. Returns the file reference on success, or None
    when this path is unavailable so the caller falls back to the resumable
    two-step flow.

    Parameters:
        image_bytes: raw image bytes to upload.
        filename: filename reported to Google.
        mime_type: MIME type of the multipart part.
        push_id: account page push id (qKIAYe token).

    Returns:
        File reference string (e.g. /contrib_service/ttl_1d/...) or None.
    """
    sess = get_browser_session()
    if sess is None:
        return None
    try:
        from curl_cffi import CurlMime
    except ImportError:
        return None
    timeout = remaining_timeout(90, "image upload")
    headers = {
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "X-Tenant-Id": "bard-storage",
        "Push-ID": push_id,
        "User-Agent": CHROME_UA,
    }
    mime = CurlMime()
    try:
        mime.addpart(name="file", content_type=mime_type,
                     filename=_sanitize_upload_name(filename), data=image_bytes)
        with curl_total_timeout(sess, timeout):
            resp = sess.post("https://content-push.googleapis.com/upload",
                             headers=headers, multipart=mime, timeout=timeout)
            body = resp.text
        if resp.status_code == 200 and body.strip().startswith("/"):
            log(f"Image uploaded via content-push multipart: {body.strip()[:60]}")
            return body.strip()
        log(f"content-push multipart not usable: HTTP {resp.status_code} {body[:80]}")
        return None
    except Exception as exc:  # transport failure -> resumable fallback
        check_budget("image upload")
        log(f"content-push multipart failed ({exc}); falling back to resumable")
        return None
    finally:
        mime.close()


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

    # Preferred path (2026-09): one-shot multipart to content-push in the
    # form HanaokaYuzu/Gemini-API uses; resumable two-step stays as fallback.
    ref = _upload_multipart_once(image_bytes, filename, mime_type, push_id)
    if ref:
        return ref

    cookie_str, sapisid = load_cookie()
    check_budget("image session refresh")
    auth_user = _active_auth_user()
    account_prefix = f"/u/{auth_user}" if auth_user not in (None, "") else ""
    base = {
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Tenant-Id": "bard-storage",
        "Push-ID": push_id,
        "Accept": "*/*",
        "User-Agent": CHROME_UA,
    }
    if account_prefix:
        base["X-Goog-AuthUser"] = str(auth_user)
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
