"""Image upload helper shared by every API protocol handler."""
import hashlib
import json
import threading
import time
from typing import Optional

from ..budget import RequestControlError, check_budget
from ..config import CONFIG
from ..image_prep import prepare_image
from ..multimodal import detect_image_mime, fetch_image_bytes, upload_image
from ..upstream.cookies import _active_auth_user, _active_cookie_path, load_cookie


class UploadedFileRef(str):
    """String-compatible upstream file reference carrying its MIME type."""

    def __new__(cls, value, mime_type):
        """Create a reference that serializes as a normal string.

        Args:
            value: Upstream file reference path.
            mime_type: Detected media type.

        Returns:
            A string-compatible reference instance.
        """
        obj = str.__new__(cls, value)
        obj.mime_type = mime_type
        return obj


def _image_byte_cap() -> int:
    """Resolve the configured global image byte cap.

    Args:
        None.

    Returns:
        Positive byte limit from CONFIG["max_image_bytes"] (20 MiB when
        unset or invalid).
    """
    try:
        value = int(CONFIG.get("max_image_bytes") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 20 * 1024 * 1024


def _normalize_images(images: list, *, byte_budget: Optional[int] = None) -> list:
    """Normalize raw image entries for either vision chain.

    Fetches http(s) URL entries, sniffs the real MIME type from magic
    bytes and runs the preprocessing pipeline (EXIF rotation, long-edge
    cap, transcode, budget re-encode) so both chains receive validated
    (bytes, mime) tuples that already fit their transport limits.

    Args:
        images: list of (bytes-or-url, mime) tuples from the protocol
            layer (see tools.messages_to_prompt).
        byte_budget: optional byte cap the prepared bytes must fit
            (defaults to the global image cap).

    Returns:
        List of (image_bytes, mime_type) tuples.

    Raises:
        RuntimeError: when an image cannot be downloaded, decodes to
            nothing, or exceeds the configured size cap.
    """
    cap = byte_budget or _image_byte_cap()
    prepared = []
    for item in images:
        check_budget("image processing")
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = None  # URL-declared types are unreliable; sniff instead
        if not data:
            raise RuntimeError("image fetch failed")
        max_bytes = _image_byte_cap()
        if len(data) > max_bytes:
            raise RuntimeError(f"image exceeds {max_bytes} bytes")
        mime = detect_image_mime(data, mime or "image/png")
        data, mime = prepare_image(data, mime, min(cap, max_bytes))
        if not data:
            raise RuntimeError("image decode failed")
        prepared.append((data, mime))
    return prepared


# ---------------------------------------------------------------------------
# Upload reference cache: identical bytes skip the Scotty round trip.
# ---------------------------------------------------------------------------
# Multi-turn conversations resend the same images on every request; the
# upstream references live for ~1 day (ttl_1d) but the borrowed session
# context makes aggressive reuse risky, so entries expire quickly.

_REF_CACHE_TTL = 900.0        # seconds a (ref, mime) pair is reused
_REF_CACHE_MAX = 64           # hard entry cap; dict order doubles as LRU
_ref_cache = {}
_ref_cache_lock = threading.Lock()


def _cache_key(data: bytes, mime: str = '') -> str:
    """Hash account, session and image identity; never retain credentials in keys.

    Args:
        data: Prepared image bytes.
        mime: Media type used by the upload.
    Returns:
        Opaque digest scoped to the selected account and current session.
    """
    from ..vision_bridge import _bridge_token_state
    check_budget('image cache lookup')
    cookie, sapisid = load_cookie()
    bridge = _bridge_token_state.get('tokens') or {}
    identity = [_active_cookie_path(), _active_auth_user(), cookie, sapisid,
                CONFIG.get('vision_bridge_url'), mime,
                [bridge.get(k) for k in ('at', 'push_id', 'f_sid')]]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode())
    digest.update(data)
    return digest.hexdigest()

def _cache_get(key: str):
    """Look up a cached upload reference.

    Args:
        key: hash from _cache_key.

    Returns:
        (UploadedFileRef, mime) tuple, or None when absent/expired.
    """
    with _ref_cache_lock:
        hit = _ref_cache.get(key)
        if not hit:
            return None
        ref, mime, ts = hit
        if time.monotonic() - ts > _REF_CACHE_TTL:
            _ref_cache.pop(key, None)
            return None
        del _ref_cache[key]  # re-insert: plain dict order doubles as LRU
        _ref_cache[key] = hit
        return UploadedFileRef(ref, mime), mime


def _cache_put(key: str, ref: str, mime: str) -> None:
    """Store an upload reference under key (bounded, FIFO eviction).

    Args:
        key: hash from _cache_key.
        ref: upstream file reference path.
        mime: MIME type of the uploaded image.

    Returns:
        None.
    """
    with _ref_cache_lock:
        _ref_cache[key] = (ref, mime, time.monotonic())
        while len(_ref_cache) > _REF_CACHE_MAX:
            _ref_cache.pop(next(iter(_ref_cache)))


# Upload filename extensions per MIME type. Google's upload pipeline
# treats the extension as the file type signal (g4f issue #3064: an
# extension-less or mismatched name shows as "unknown" and the model may
# refuse the image), so the reported name must carry the right suffix.
_MIME_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tiff",
    "image/heic": "heic", "image/heif": "heic", "image/avif": "avif",
}


def _upload_one(data: bytes, mime: str):
    """Upload one image with the reference cache in front.

    Args:
        data: prepared image bytes.
        mime: MIME type for the multipart part; also picks the filename
            extension reported to Google (see _MIME_EXT).

    Returns:
        UploadedFileRef (string-compatible, carries mime_type).

    Raises:
        RuntimeError: on upload failure.
    """
    check_budget('image upload')
    key = _cache_key(data, mime or 'image/png')
    cached = _cache_get(key)
    if cached:
        return cached[0]
    ext = _MIME_EXT.get(mime or "", "png")
    ref = upload_image(data, f"image.{ext}", mime or "image/png")
    check_budget("image upload")
    file_ref = UploadedFileRef(ref, mime or "image/png")
    # Token acquisition or cookie renewal may change identity during upload;
    # a conservative cache miss is safer than publishing under another session.
    if key == _cache_key(data, mime or 'image/png'):
        _cache_put(key, ref, mime or 'image/png')
    return file_ref


def _upload_images(images: list) -> list:
    """Upload normalized images and return list of file references.

    Args:
        images: list of (bytes-or-url, mime) tuples from the protocol
            layer.

    Returns:
        List of UploadedFileRef, or None when no images were supplied.
        Raises RuntimeError when any image cannot be fetched, prepared
        or uploaded.
    """
    if not images:
        return None
    prepared = _normalize_images(images)
    file_refs = []
    for data, mime in prepared:
        try:
            file_refs.append(_upload_one(data, mime))
        except RequestControlError:
            raise
        except Exception as e:
            check_budget("image upload")
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None
