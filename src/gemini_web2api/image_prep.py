"""Vision image preprocessing: normalize, downscale and re-encode before upload.

Research-driven pipeline (see docs/VISION_RESEARCH.md): every image -
whether an inline data URL or a downloaded link - passes through one
preparation function before it reaches either vision chain. Goals:

1. Fit transport limits. The CDP bridge degrades with multi-megabyte
   evaluate payloads and hard-rejects anything above 4 MiB; oversized
   phone photos are resized/re-encoded instead of failing the request.
2. Feed the model what it reads best. Long edges are capped (default
   2048 px), EXIF rotation is applied and exotic containers
   (HEIC/AVIF/BMP/TIFF) are transcoded to JPEG/PNG the Gemini web
   pipeline reliably accepts.
3. Stay lossless when possible. Images already within limits, already
   in a web-safe format and already small enough pass through byte for
   byte - no gratuitous recompression.

Pillow is optional: without it the module degrades to passthrough (the
pre-Pillow behavior). A decompression-bomb guard rejects hostile files.
"""
import io
import threading
import warnings
from typing import Optional

from .config import CONFIG
from .logs import log

# Long-edge cap applied before upload. 2048 px keeps OCR fidelity for
# the translation/识图 focus while bounding upload size and CDP payload.
DEFAULT_MAX_EDGE = 2048
# Never shrink below this long edge while chasing a byte budget.
MIN_EFFECTIVE_EDGE = 640
# Re-encode quality ladder tried while fitting a byte budget.
_JPEG_QUALITY_LADDER = (87, 80, 72, 64)
# Formats the Gemini web pipeline accepts without transcoding.
_WEB_SAFE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

_prep_lock = threading.Lock()


def _max_edge() -> int:
    """Resolve the configured long-edge cap with a sane floor.

    Args:
        None.

    Returns:
        Positive pixel cap for the longest image edge.
    """
    try:
        value = int(CONFIG.get("vision_max_edge_px") or DEFAULT_MAX_EDGE)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_EDGE
    return max(256, value)


def _pillow():
    """Import Pillow lazily so deployments without it keep working.

    Also registers the optional pillow-heif opener when installed so
    HEIC/HEIF iPhone photos decode and transcode (research: HEIC is the
    most common real-world vision input from phones; pillow-heif is an
    optional dependency, absent = those files pass through untouched).

    Args:
        None.

    Returns:
        The PIL module, or None when Pillow is unavailable.
    """
    try:
        import PIL
        from PIL import Image  # noqa: F401  (import check)
        try:
            import pillow_heif
            pillow_heif.register_heif_opener(thumbnails=False)
        except Exception:
            pass  # optional extra; HEIC passthrough without it
        return PIL
    except Exception:
        return None


def prepare_image(data: bytes, mime: str, max_bytes: int,
                  *, max_edge: Optional[int] = None) -> tuple:
    """Prepare one image for upload to either vision chain.

    Passthrough (bytes unchanged) when the image already satisfies the
    format/size/edge constraints or when Pillow is unavailable; otherwise
    EXIF-orient, downscale and re-encode, iterating quality/edge until the
    encoded bytes fit max_bytes or the floor is reached.

    Args:
        data: raw image bytes (already downloaded/decoded).
        mime: detected or declared MIME type of data.
        max_bytes: byte budget the prepared image must fit (upload or
            bridge evaluate limit).
        max_edge: optional long-edge pixel cap override.

    Returns:
        (prepared_bytes, mime) tuple; never larger than the input unless
        transcoding a tiny exotic file was the only option, and always
        within max_bytes when Pillow succeeded.
    """
    if not isinstance(data, bytes) or not data:
        return data, mime
    edge_cap = max_edge or _max_edge()
    try:
        budget = int(max_bytes)
    except (TypeError, ValueError):
        return data, mime
    if budget <= 0:
        return data, mime

    pil = _pillow()
    if pil is None:
        return data, mime

    from PIL import Image, ImageOps

    try:
        with _prep_lock:  # Pillow decode is not guaranteed thread-safe
            # Decompression-bomb guard: the 1-2x MAX_IMAGE_PIXELS band is
            # only a warning by default; hostile files must fail hard
            # (Pillow security handbook).
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                img = Image.open(io.BytesIO(data))
                img.load()
        fmt = (img.format or "").upper()
        animated = getattr(img, "is_animated", False)
        # Animated GIFs keep their frames: re-encoding would silently
        # drop animation, and Scotty accepts them as-is when in budget.
        if animated and fmt == "GIF" and len(data) <= budget:
            return data, "image/gif"

        needs_transcode = fmt not in _WEB_SAFE_FORMATS
        longest = max(img.width, img.height)
        needs_resize = longest > edge_cap

        if not needs_transcode and not needs_resize and len(data) <= budget:
            return data, mime or _mime_for_format(fmt)

        img = ImageOps.exif_transpose(img)

        # Alpha handling: small PNG/WEBP with alpha stays PNG; anything
        # needing recompress composites onto white as JPEG.
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info)

        def _encode(image, quality=None, fmt_out="JPEG"):
            """Encode one image to bytes. Args: image, quality, fmt_out. Returns: bytes."""
            buf = io.BytesIO()
            save_kwargs = {"format": fmt_out}
            if quality is not None:
                save_kwargs["quality"] = quality
            image.save(buf, **save_kwargs)
            return buf.getvalue()

        # Fast path: right format, only mildly over budget -> try plain
        # recompress of the current pixels before any resizing.
        if not needs_transcode and not needs_resize:
            if fmt == "PNG" and has_alpha:
                for q in _JPEG_QUALITY_LADDER:
                    out = _encode(img.convert("RGBA"), q, "WEBP")
                    if len(out) <= budget:
                        return out, "image/webp"
            elif fmt in ("JPEG", "WEBP"):
                for q in _JPEG_QUALITY_LADDER:
                    out = _encode(img, q, "WEBP" if fmt == "WEBP" else "JPEG")
                    if len(out) <= budget:
                        return out, _mime_for_format(fmt)

        # General path: cap the long edge, then walk the quality ladder
        # (halving the edge when quality bottoms out) until it fits.
        current_cap = edge_cap
        while True:
            work = img.copy()
            work.thumbnail((current_cap, current_cap), Image.LANCZOS)
            out_fmt = "PNG" if (has_alpha and not needs_transcode
                                and work.width * work.height < 400_000) else "JPEG"
            if out_fmt == "PNG":
                out = _encode(work, None, "PNG")
                if len(out) <= budget:
                    return out, "image/png"
            encoded = None
            for q in _JPEG_QUALITY_LADDER:
                canvas = work
                if has_alpha:
                    canvas = Image.new("RGB", work.size, (255, 255, 255))
                    canvas.paste(work, mask=work.split()[-1] if work.mode in ("RGBA", "LA") else None)
                else:
                    canvas = work.convert("RGB")
                encoded = _encode(canvas, q, "JPEG")
                if len(encoded) <= budget:
                    return encoded, "image/jpeg"
            if current_cap <= MIN_EFFECTIVE_EDGE or encoded is None:
                # Floor reached: return the smallest encoding we produced.
                return encoded or data, (mime or "image/jpeg") if encoded else mime
            current_cap = max(MIN_EFFECTIVE_EDGE, current_cap // 2)
    except Exception as exc:  # unopenable data, bomb guard, encoder failure
        log(f"image prepare skipped ({type(exc).__name__}: {exc})")
        return data, mime


def _mime_for_format(fmt: str) -> str:
    """Map a Pillow format name to its MIME type.

    Args:
        fmt: Pillow format string (e.g. "JPEG").

    Returns:
        MIME type string; image/png for unknown formats.
    """
    return {"JPEG": "image/jpeg", "PNG": "image/png",
            "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt, "image/png")
