"""Offline regressions for the 2026-09 research pass 4 vision features.

Covers: OpenAI image detail hints (resolution tiers), concurrent
multi-image normalization/uploads, and the standalone soft-first vision
tab keepalive thread.
"""
import threading
from unittest import mock

import pytest

from gemini_web2api import keepalive
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server import images
from gemini_web2api.tools import _image_from_part


@pytest.fixture(autouse=True)
def isolated_vision(monkeypatch):
    """Keep tests offline and deterministic. Args: monkeypatch. Returns: None."""
    monkeypatch.setitem(CONFIG, 'cookie_file', None)
    monkeypatch.setitem(CONFIG, 'cookie_files', [])
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', None)
    monkeypatch.setitem(CONFIG, 'log_requests', False)


def test_detail_hint_extracted_from_image_url():
    """image_url.detail rides along as a third tuple element. Args: None. Returns: None."""
    part = {"type": "image_url",
            "image_url": {"url": "https://x/i.png", "detail": "low"}}
    assert _image_from_part(part) == ("https://x/i.png", "image/png", "low")
    part["image_url"]["detail"] = "high"
    assert _image_from_part(part)[2] == "high"


def test_detail_hint_from_responses_input_image():
    """Responses input_image parts also carry the detail hint. Args: None. Returns: None."""
    part = {"type": "input_image", "url": "https://x/i.png", "detail": "high"}
    assert _image_from_part(part) == ("https://x/i.png", "image/png", "high")


def test_invalid_or_absent_detail_keeps_two_tuple():
    """Unknown detail values never widen the tuple. Args: None. Returns: None."""
    part = {"type": "image_url",
            "image_url": {"url": "https://x/i.png", "detail": "auto"}}
    assert _image_from_part(part) == ("https://x/i.png", "image/png")
    part = {"type": "image_url", "image_url": "https://x/i.png"}
    assert _image_from_part(part) == ("https://x/i.png", "image/png")


def test_normalize_applies_detail_edge_caps(monkeypatch):
    """detail=low/high map to 1024/2048px edge caps. Args: monkeypatch. Returns: None."""
    seen = []

    def fake_prepare(data, mime, budget, max_edge=None):
        """Capture the per-image edge cap. Args: data, mime, budget, max_edge. Returns: tuple."""
        seen.append(max_edge)
        return data, mime

    monkeypatch.setattr(images, 'prepare_image', fake_prepare)
    low = (b'img-low', 'image/png', 'low')
    high = (b'img-high', 'image/png', 'high')
    plain = (b'img-plain', 'image/png')
    prepared = images._normalize_images([low, high, plain])
    assert [p[0] for p in prepared] == [low[0], high[0], plain[0]]
    assert seen == [1024, 2048, None]


def test_normalize_parallel_preserves_order_and_propagates_failure(monkeypatch):
    """Concurrent normalization keeps input order and surfaces errors. Args: monkeypatch. Returns: None."""
    monkeypatch.setattr(images, 'prepare_image',
                        lambda data, mime, budget, max_edge=None: (data, mime))
    items = [(bytes([48 + i]) + b'-img', 'image/png') for i in range(6)]
    prepared = images._normalize_images(items)
    assert [p[0] for p in prepared] == [i[0] for i in items]

    def failing_prepare(data, mime, budget, max_edge=None):
        """Fail only on the third image. Args: data, mime, budget, max_edge. Returns: tuple."""
        if data.startswith(b'2-'):
            raise RuntimeError("boom")
        return data, mime

    monkeypatch.setattr(images, 'prepare_image', failing_prepare)
    with pytest.raises(RuntimeError):
        images._normalize_images(items)


def test_parallel_uploads_keep_ref_type(monkeypatch):
    """Direct-chain parallel uploads return UploadedFileRef objects. Args: monkeypatch. Returns: None."""
    refs = []

    def fake_upload(data, mime):
        """Record and return a real UploadedFileRef. Args: data, mime. Returns: UploadedFileRef."""
        ref = images.UploadedFileRef(f"/contrib_service/ttl_1d/{data[:1].decode()}", mime)
        refs.append(ref)
        return ref

    monkeypatch.setattr(images, '_upload_one', fake_upload)
    monkeypatch.setattr(images, '_normalize_images',
                        lambda imgs: [(d, m) for d, m in imgs])
    out = images._upload_images([(b'1png', 'image/png'), (b'2png', 'image/png')])
    assert [str(r) for r in out] == ["/contrib_service/ttl_1d/1", "/contrib_service/ttl_1d/2"]
    assert all(isinstance(r, images.UploadedFileRef) for r in out)
    assert out[0].mime_type == "image/png"


def test_single_upload_still_non_threaded(monkeypatch):
    """One image stays on the simple direct path. Args: monkeypatch. Returns: None."""
    calls = []

    def fake_upload(data, mime):
        """Record the single call. Args: data, mime. Returns: UploadedFileRef."""
        calls.append(data)
        return images.UploadedFileRef("/contrib_service/ttl_1d/x", mime)

    monkeypatch.setattr(images, '_upload_one', fake_upload)
    monkeypatch.setattr(images, '_normalize_images', lambda imgs: [(b'a', 'image/png')])
    out = images._upload_images([(b'a', 'image/png')])
    assert calls == [b'a']
    assert str(out[0]) == "/contrib_service/ttl_1d/x"


def test_vision_tab_keepalive_default_on():
    """The soft-first ring defaults to 1800s (research pass 4). Args: None. Returns: None."""
    assert DEFAULT_CONFIG["vision_tab_keepalive_sec"] == 1800


def test_vision_tab_keepalive_thread_requires_bridge(monkeypatch):
    """No thread spawns without a configured bridge. Args: monkeypatch. Returns: None."""
    keepalive._vision_tab_thread_on["started"] = False
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', None)
    keepalive.start_vision_tab_keepalive()
    assert keepalive._vision_tab_thread_on["started"] is False


def test_vision_tab_keepalive_thread_starts_once(monkeypatch):
    """The dedicated thread starts once and only once. Args: monkeypatch. Returns: None."""
    keepalive._vision_tab_thread_on["started"] = False
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', 'http://fake:22')
    started = []
    real_thread = threading.Thread

    class RecordingThread(real_thread):
        """Thread subclass that records starts. Args: as threading.Thread."""
        def __init__(self, *args, **kwargs):
            """Capture the target for later probing. Args: args, kwargs. Returns: None."""
            super().__init__(*args, **kwargs)
            started.append(kwargs.get("target") or (args[0] if args else None))

    with mock.patch("threading.Thread", RecordingThread):
        keepalive.start_vision_tab_keepalive()
        assert keepalive._vision_tab_thread_on["started"] is True
        keepalive.start_vision_tab_keepalive()  # second call is a no-op
    assert len(started) == 1
    keepalive._vision_tab_thread_on["started"] = False


def test_vision_tab_keepalive_disabled_by_zero(monkeypatch):
    """vision_tab_keepalive_sec=0 explicitly disables the thread. Args: monkeypatch. Returns: None."""
    keepalive._vision_tab_thread_on["started"] = False
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', 'http://fake:22')
    monkeypatch.setitem(CONFIG, 'vision_tab_keepalive_sec', 0)
    keepalive.start_vision_tab_keepalive()
    assert keepalive._vision_tab_thread_on["started"] is False
