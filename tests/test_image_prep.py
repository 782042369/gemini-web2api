"""Offline tests for the vision image preprocessing pipeline."""
import io

from gemini_web2api import image_prep
from gemini_web2api.config import CONFIG


def _jpeg(width=4032, height=3024, quality=95, noise=True):
    """Build a real JPEG with Pillow. Args: width, height, quality, noise. Returns: bytes."""
    import random

    from PIL import Image
    img = Image.new("RGB", (width, height), (200, 30, 30))
    if noise:  # solid fills compress to nothing; noise forces real bytes
        rnd = random.Random(7)
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                     for _ in range(width * height)])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _png(width=64, height=32, rgba=False):
    """Build a real PNG with Pillow. Args: width, height, rgba. Returns: bytes."""
    from PIL import Image
    mode = "RGBA" if rgba else "RGB"
    img = Image.new(mode, (width, height), (10, 120, 240, 255) if rgba else (10, 120, 240))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_passthrough_when_already_small_and_web_safe():
    """A small JPEG under every cap passes through byte for byte."""
    data = _jpeg(width=320, height=200, quality=80)
    out, mime = image_prep.prepare_image(data, "image/jpeg", 4 * 1024 * 1024)
    assert out == data
    assert mime == "image/jpeg"


def test_oversized_image_resized_and_recompressed_under_budget():
    """A noisy 4032px JPEG exceeding the byte budget comes back small enough."""
    data = _jpeg(width=4032, height=3024, quality=95)
    assert len(data) > 512 * 1024
    out, mime = image_prep.prepare_image(data, "image/jpeg", 256 * 1024)
    assert len(out) <= 256 * 1024
    assert mime == "image/jpeg"
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert max(img.width, img.height) <= image_prep.DEFAULT_MAX_EDGE


def test_bridge_budget_four_megabyte_phone_photo_fits():
    """The classic 4 MiB bridge rejection case now compresses to fit."""
    data = _jpeg(width=4032, height=3024, quality=97)
    out, _mime = image_prep.prepare_image(data, "image/jpeg", 4 * 1024 * 1024)
    assert len(out) <= 4 * 1024 * 1024


def test_long_edge_capped_by_config():
    """vision_max_edge_px bounds the output long edge."""
    CONFIG["vision_max_edge_px"] = 640
    try:
        data = _jpeg(width=3200, height=2400, quality=90)
        out, _ = image_prep.prepare_image(data, "image/jpeg", 20 * 1024 * 1024)
        from PIL import Image
        img = Image.open(io.BytesIO(out))
        assert max(img.width, img.height) <= 640
    finally:
        CONFIG["vision_max_edge_px"] = None


def test_unsupported_container_transcoded():
    """A BMP (not web-safe) is transcoded to a web-safe encoding."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 60), (1, 2, 3)).save(buf, "BMP")
    out, mime = image_prep.prepare_image(buf.getvalue(), "image/bmp", 1 << 20)
    assert mime in ("image/jpeg", "image/png", "image/webp")
    assert out[:2] != b"BM"


def test_gif_passthrough_in_budget():
    """A GIF in budget keeps its bytes (re-encoding would drop frames)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("P", (32, 32)).save(buf, "GIF")
    out, mime = image_prep.prepare_image(buf.getvalue(), "image/gif", 1 << 20)
    assert out == buf.getvalue()
    assert mime == "image/gif"


def test_without_pillow_returns_passthrough(monkeypatch):
    """No Pillow available: the pipeline degrades to passthrough."""
    import sys
    data = _jpeg(width=4032, height=3024, quality=95)  # build BEFORE patching
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    out, _mime = image_prep.prepare_image(data, "image/jpeg", 1024)
    assert out == data


def test_undecodable_bytes_returned_unchanged():
    """Garbage bytes surface unchanged for upstream magic-byte sniffing."""
    data = b"not-an-image-at-all"
    out, mime = image_prep.prepare_image(data, "image/png", 4096)
    assert out == data
    assert mime == "image/png"


def test_empty_and_invalid_inputs_short_circuit():
    """Empty or non-bytes input returns immediately."""
    assert image_prep.prepare_image(b"", "image/png", 100)[0] == b""
    assert image_prep.prepare_image(None, "image/png", 100)[0] is None
    assert image_prep.prepare_image(b"x", "image/png", 0)[0] == b"x"
