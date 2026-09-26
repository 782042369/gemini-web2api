"""Offline tests for vision_mode routing, bridge token merge and readiness."""
import threading
import time

import pytest

from gemini_web2api import multimodal, vision_bridge
from gemini_web2api.config import CONFIG
from gemini_web2api.server.openai_chat import OpenAIChatMixin


def _reset_bridge_state():
    """Clear module-level bridge token caches. Args: None. Returns: None."""
    vision_bridge._bridge_token_state.update(tokens=None, ts=0.0, fail_ts=0.0)


def test_vision_mode_resolution():
    """_vision_mode degrades to direct without a bridge endpoint."""
    handler = OpenAIChatMixin()
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    for mode, expected in [("auto", "auto"), ("bridge", "bridge"),
                           ("direct", "direct"), ("bogus", "auto"),
                           (None, "auto")]:
        CONFIG["vision_mode"] = mode
        assert handler._vision_mode() == expected, mode
    CONFIG["vision_bridge_url"] = None
    for mode in ("auto", "bridge", None):
        CONFIG["vision_mode"] = mode
        assert handler._vision_mode() == "direct", mode


def test_merge_bridge_tokens_fills_missing_at(monkeypatch):
    """Bridge tokens fill at and gaps; server-scraped values win."""
    _reset_bridge_state()
    monkeypatch.setattr(vision_bridge, "fetch_page_tokens", lambda force=False: {
        "at": "AOvxBRIDGE", "push_id": "P_BRIDGE", "bl": "BL_BRIDGE"})
    monkeypatch.setattr(multimodal, "fetch_page_tokens",
                         vision_bridge.fetch_page_tokens, raising=False)
    tokens = multimodal._merge_bridge_tokens({"push_id": "P_SERVER", "bl": "BL_SERVER"})
    assert tokens["at"] == "AOvxBRIDGE"
    assert tokens["push_id"] == "P_SERVER"   # server value wins
    assert tokens["bl"] == "BL_SERVER"


def test_merge_bridge_tokens_skips_when_at_present(monkeypatch):
    """A server-side at means no bridge consultation at all."""
    _reset_bridge_state()
    calls = []
    monkeypatch.setattr(vision_bridge, "fetch_page_tokens",
                         lambda force=False: calls.append(1) or {"at": "AOvxX"})
    tokens = multimodal._merge_bridge_tokens({"at": "AOvxOWN", "push_id": "P"})
    assert tokens["at"] == "AOvxOWN"
    assert calls == []


def test_merge_bridge_tokens_swallows_bridge_errors(monkeypatch):
    """An unreachable bridge must not break the token merge."""
    _reset_bridge_state()
    def boom(force=False):
        raise RuntimeError("bridge down")
    monkeypatch.setattr(vision_bridge, "fetch_page_tokens", boom)
    tokens = multimodal._merge_bridge_tokens({"push_id": "P"})
    assert "at" not in tokens


def test_fetch_page_tokens_caches_success(monkeypatch):
    """A successful read is cached; force bypasses the cache."""
    _reset_bridge_state()
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    reads = []

    def fake_now():
        return {"at": "AOvx1", "push_id": "P1"}

    monkeypatch.setattr(vision_bridge, "_fetch_page_tokens_now",
                         lambda: reads.append(1) or fake_now())
    first = vision_bridge.fetch_page_tokens()
    second = vision_bridge.fetch_page_tokens()
    assert first["at"] == "AOvx1" and second["at"] == "AOvx1"
    assert len(reads) == 1
    forced = vision_bridge.fetch_page_tokens(force=True)
    assert forced["at"] == "AOvx1"
    assert len(reads) == 2


def test_fetch_page_tokens_failure_cooldown(monkeypatch):
    """Failures (logged out / unreachable) cool down before retrying."""
    _reset_bridge_state()
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    reads = []
    monkeypatch.setattr(vision_bridge, "_fetch_page_tokens_now",
                         lambda: reads.append(1) or {})
    assert vision_bridge.fetch_page_tokens() == {}
    assert vision_bridge.fetch_page_tokens() == {}
    assert len(reads) == 1


def test_fetch_page_tokens_disabled_bridge(monkeypatch):
    """Without a configured bridge the call is a cheap no-op."""
    _reset_bridge_state()
    CONFIG["vision_bridge_url"] = None
    monkeypatch.setattr(vision_bridge, "_fetch_page_tokens_now",
                         lambda: pytest.fail("must not touch CDP"))
    assert vision_bridge.fetch_page_tokens(force=True) == {}


def test_vision_direct_ready(monkeypatch):
    """Readiness needs both at and push_id in the token cache."""
    monkeypatch.setattr(multimodal, "_cached_page_tokens",
                         lambda: {"push_id": "P"})
    assert multimodal.vision_direct_ready() is False
    monkeypatch.setattr(multimodal, "_cached_page_tokens",
                         lambda: {"push_id": "P", "at": "AOvx1"})
    assert multimodal.vision_direct_ready() is True


def test_token_cache_ttl_requires_at():
    """Complete (at+push_id) sets live 600s; push_id-only sets 120s."""
    cache = {"tokens": {}, "ts": None, "mtime": None, "lock": threading.Lock()}
    multimodal._page_tokens_cache.clear()
    key = ("/fake/cookie.txt", None)
    multimodal._page_tokens_cache[key] = cache
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(multimodal.os.path, "getmtime", lambda p: 1.0)
        monkey.setattr(multimodal, "_get_page_tokens",
                        lambda: {"push_id": "P", "at": "AOvx1"})
        multimodal._active_cookie_path = lambda: "/fake/cookie.txt"
        multimodal._active_auth_user = lambda: None
        multimodal._cached_page_tokens()
        cache.update(ts=multimodal.time.monotonic())
        # second call immediately: hit (no refetch) - both ttls
        assert multimodal._cached_page_tokens()["at"] == "AOvx1"
        # expire the short window: push_id-only token set must refetch
        cache.update(tokens={"push_id": "P"},
                     ts=multimodal.time.monotonic() - 121)
        refetched = []
        monkey.setattr(multimodal, "_get_page_tokens",
                        lambda: refetched.append(1) or {"push_id": "P"})
        multimodal._cached_page_tokens()
        assert refetched == [1]
        # complete set within 600s must NOT refetch
        cache.update(tokens={"push_id": "P", "at": "AOvx2"},
                     ts=multimodal.time.monotonic() - 500)
        monkey.setattr(multimodal, "_get_page_tokens",
                        lambda: pytest.fail("must be a cache hit"))
        assert multimodal._cached_page_tokens()["at"] == "AOvx2"
    finally:
        monkey.undo()
        multimodal._page_tokens_cache.clear()
# ---------------------------------------------------------------------------
# Circuit breaker, chain classification and payload passthrough (2026-09).
# ---------------------------------------------------------------------------


def test_direct_breaker_opens_and_recovers():
    """Three consecutive direct failures cool the chain down for 60s."""
    multimodal._direct_breaker.update(fails=0, cool_until=0.0)
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(multimodal, "_cached_page_tokens",
                        lambda: {"push_id": "P", "at": "AOvx1"})
        assert multimodal.vision_direct_available() is True
        for _ in range(3):
            multimodal.note_direct_vision_outcome(False)
        assert multimodal.vision_direct_available() is False
        assert multimodal.vision_direct_ready() is True  # tokens still fine
        multimodal.note_direct_vision_outcome(True)  # (unreachable while open)
    finally:
        monkey.undo()
        multimodal._direct_breaker.update(fails=0, cool_until=0.0)
    # cooldown expiry restores availability
    multimodal._direct_breaker.update(fails=0, cool_until=0.0)
    monkey2 = pytest.MonkeyPatch()
    try:
        monkey2.setattr(multimodal, "_cached_page_tokens",
                        lambda: {"push_id": "P", "at": "AOvx1"})
        multimodal._direct_breaker.update(fails=3, cool_until=multimodal.time.monotonic() - 1)
        assert multimodal.vision_direct_available() is True
    finally:
        monkey2.undo()
        multimodal._direct_breaker.update(fails=0, cool_until=0.0)


def test_breaker_requires_ready_tokens():
    """An open internet circuit cannot make an unavailable chain available."""
    multimodal._direct_breaker.update(fails=0, cool_until=0.0)
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(multimodal, "_cached_page_tokens", lambda: {"push_id": "P"})
        assert multimodal.vision_direct_available() is False
    finally:
        monkey.undo()


def test_classify_chain_failure_matrix():
    """Stage/code pairs map to the three retry verdicts."""
    cases = [
        ({"stage": "process_file", "code": 7, "err": "x"}, "stale_session"),
        ({"stage": "process_file", "code": 8, "err": "x"}, "stale_session"),
        ({"stage": "process_file", "code": None, "err": "session binding"}, "stale_session"),
        ({"stage": "upload_start", "err": "upload start failed 500"}, "transient"),
        ({"stage": "upload", "err": "upload failed"}, "transient"),
        ({"stage": "exception", "err": "chain exception: x"}, "transient"),
        ({"stage": "session", "err": "no at"}, "fatal"),
        ({"stage": "generate", "err": "upstream rejected: 1100"}, "fatal"),
        ({"err": "weird"}, "fatal"),
        ({"stage": "process_file", "code": 42, "err": "other"}, "fatal"),
    ]
    for data, expected in cases:
        assert vision_bridge._classify_chain_failure(data) == expected, data


def test_page_chain_js_carries_model_and_extensions():
    """The page payload forwards model/think and correct file extensions."""
    js = vision_bridge._page_chain_js(
        "hi", [(b"a", "image/jpeg"), (b"b", "image/webp")], 2, 0)
    assert '"model_id": 2' in js
    assert '"think_mode": 0' in js
    assert "'jpg'" in js and "'webp'" in js
    bare = vision_bridge._page_chain_js("hi", [(b"a", "image/png")], 1, None, True)
    assert '"bare_ref": true' in bare
    assert 'if (PAYLOAD.bare_ref)' in bare


def test_vision_generate_serializes_chains(monkeypatch):
    """Concurrent generates run one at a time under the bridge lock."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    events = []
    fake_js = "js"

    def fake_chain(js):
        """Assert the lock is held while a chain runs. Args: js. Returns: dict."""
        assert vision_bridge._bridge_lock.acquire(blocking=False) is False
        events.append("enter")
        time.sleep(0.05)
        events.append("exit")
        return {"sg": "payload"}

    monkeypatch.setattr(vision_bridge, "_page_chain_js", lambda *a, **k: fake_js)
    monkeypatch.setattr(vision_bridge, "_run_page_chain", fake_chain)
    monkeypatch.setattr(vision_bridge, "_server_upload_all", lambda p: None)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))

    threads = [threading.Thread(
        target=lambda: vision_bridge.vision_generate("p", [(b"imgdata", "image/png")]))
        for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert events[0] == "enter" and events[1] == "exit", events


def test_vision_generate_stale_session_reloads_and_retries(monkeypatch):
    """A ProcessFile code-7 failure triggers one reload + retry."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    calls = {"chain": 0, "reload": 0}

    def fake_chain(js):
        """Fail the registered chain once, then succeed. Args: js. Returns: dict."""
        calls["chain"] += 1
        if calls["chain"] == 1:
            return {"stage": "process_file", "code": 7, "err": "stale"}
        return {"sg": "recovered"}

    monkeypatch.setattr(vision_bridge, "_page_chain_js", lambda *a, **k: "js")
    monkeypatch.setattr(vision_bridge, "_run_page_chain", fake_chain)
    monkeypatch.setattr(vision_bridge, "_reload_gemini_tab",
                        lambda: calls.update(reload=1) or {"at": "AOvxNEW"})
    monkeypatch.setattr(vision_bridge, "_server_upload_all", lambda p: None)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    out = vision_bridge.vision_generate("p", [(b"img", "image/png")])
    assert out == "recovered"
    assert calls == {"chain": 2, "reload": 1}


def test_vision_generate_bare_ref_fallback(monkeypatch):
    """Persistent ProcessFile failure falls back to bare references."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    payloads = []

    def fake_chain(js):
        """Record which chain form ran. Args: js. Returns: dict."""
        payloads.append(js)
        if len(payloads) == 1:
            return {"stage": "process_file", "code": 42, "err": "no uuid"}
        return {"sg": "bare ok"}

    monkeypatch.setattr(vision_bridge, "_run_page_chain", fake_chain)
    monkeypatch.setattr(vision_bridge, "_reload_gemini_tab", lambda: {})
    monkeypatch.setattr(vision_bridge, "_server_upload_all", lambda p: None)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    out = vision_bridge.vision_generate("p", [(b"img", "image/jpeg")])
    assert out == "bare ok"
    assert len(payloads) == 2  # registered, then bare-ref form


def test_vision_generate_busy_lock_times_out(monkeypatch):
    """A held bridge lock surfaces a busy error instead of queueing forever."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    monkeypatch.setattr(vision_bridge, "_BRIDGE_CHAIN_WAIT", 0.05)
    monkeypatch.setattr(vision_bridge, "_server_upload_all", lambda p: None)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    acquired = vision_bridge._bridge_lock.acquire()
    assert acquired
    try:
        with pytest.raises(vision_bridge.VisionBridgeError, match="busy"):
            vision_bridge.vision_generate("p", [(b"img", "image/png")])
    finally:
        vision_bridge._bridge_lock.release()


def test_keepalive_vision_ring_gated_and_fires(monkeypatch):
    """The keepalive ring skips reloads while at stays fresh (soft-first)."""
    from gemini_web2api import keepalive
    keepalive._vision_tab_last["ts"] = 0.0
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    try:
        CONFIG["vision_tab_keepalive_sec"] = 0
        keepalive._maybe_keep_vision_tab()  # disabled: no-op
        CONFIG["vision_tab_keepalive_sec"] = 600
        reloaded = []
        probed = []
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr("gemini_web2api.vision_bridge.vision_bridge_enabled",
                                lambda: True)
            monkeypatch.setattr("gemini_web2api.vision_bridge._reload_gemini_tab",
                                lambda: reloaded.append(1) or {"at": "AOvx1"})
            monkeypatch.setattr("gemini_web2api.vision_bridge.fetch_page_tokens",
                                lambda force=False: probed.append(force)
                                or ({"at": "AOvx1"} if len(probed) == 1 else {}))
            # Soft path: a live at means no reload at all.
            keepalive._maybe_keep_vision_tab()
            assert probed == [True] and reloaded == []
            # Hard path: the next tick finds no at and reloads once.
            keepalive._vision_tab_last["ts"] = 0.0
            keepalive._maybe_keep_vision_tab()
            assert reloaded == [1]
            keepalive._vision_tab_last["ts"] = 0.0
            keepalive._maybe_keep_vision_tab()  # within interval skip via reset
        finally:
            monkeypatch.undo()
    finally:
        CONFIG["vision_tab_keepalive_sec"] = 0
        keepalive._vision_tab_last["ts"] = 0.0


def test_page_chain_js_ref_payload_skips_upload():
    """Hybrid payload entries carry refs and no base64 bytes."""
    js = vision_bridge._page_chain_js(
        "hi", [("/contrib_service/ttl_1d/abc", "image/jpeg")], 1, None)
    assert '"ref": "/contrib_service/ttl_1d/abc"' in js
    assert '"b64"' not in js
    assert "push.clients6.google.com/upload" in js  # upload code kept for fallback


def test_vision_generate_hybrid_uses_server_upload(monkeypatch):
    """Server-side refs flow into the page chain when uploads succeed."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    CONFIG["vision_bridge_server_upload"] = True
    captured = []

    def fake_chain_js(prompt, images, model_id, think_mode, allow_bare_ref=False):
        """Record the chain images. Args: as _page_chain_js. Returns: str."""
        captured.append(images)
        return "js"

    monkeypatch.setattr(vision_bridge, "_page_chain_js", fake_chain_js)
    monkeypatch.setattr(vision_bridge, "_run_page_chain", lambda js: {"sg": "ok"})
    monkeypatch.setattr(vision_bridge, "_server_upload_all",
                        lambda p: [("/contrib_service/ttl_1d/x", m)
                                   for _, m in p])
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    out = vision_bridge.vision_generate("p", [(b"img", "image/jpeg")])
    assert out == "ok"
    assert captured == [[("/contrib_service/ttl_1d/x", "image/jpeg")]]


def test_vision_generate_hybrid_falls_back_to_in_page(monkeypatch):
    """A failed server upload falls back to the in-page base64 chain."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    CONFIG["vision_bridge_server_upload"] = True
    captured = []

    def fake_chain_js(prompt, images, model_id, think_mode, allow_bare_ref=False):
        """Record the chain images. Args: as _page_chain_js. Returns: str."""
        captured.append(images)
        return "js"

    monkeypatch.setattr(vision_bridge, "_page_chain_js", fake_chain_js)
    monkeypatch.setattr(vision_bridge, "_run_page_chain", lambda js: {"sg": "ok"})
    monkeypatch.setattr(vision_bridge, "_server_upload_all", lambda p: None)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    out = vision_bridge.vision_generate("p", [(b"img", "image/jpeg")])
    assert out == "ok"
    assert captured == [[(b"img", "image/jpeg")]]


def test_vision_generate_hybrid_disabled_by_config(monkeypatch):
    """vision_bridge_server_upload=false restores the pure in-page chain."""
    CONFIG["vision_bridge_url"] = "http://bridge:22"
    CONFIG["vision_bridge_server_upload"] = False
    captured = []

    def fake_chain_js(prompt, images, model_id, think_mode, allow_bare_ref=False):
        """Record the chain images. Args: as _page_chain_js. Returns: str."""
        captured.append(images)
        return "js"

    def no_upload(prepared):
        """Must never run while disabled. Args: prepared. Returns: refs."""
        raise AssertionError("server upload ran while disabled")

    monkeypatch.setattr(vision_bridge, "_page_chain_js", fake_chain_js)
    monkeypatch.setattr(vision_bridge, "_run_page_chain", lambda js: {"sg": "ok"})
    monkeypatch.setattr(vision_bridge, "_server_upload_all", no_upload)
    monkeypatch.setattr("gemini_web2api.image_prep.prepare_image",
                        lambda data, mime, budget, **k: (data, mime))
    out = vision_bridge.vision_generate("p", [(b"img", "image/png")])
    assert out == "ok"
    assert captured == [[(b"img", "image/png")]]
    CONFIG["vision_bridge_server_upload"] = True


def test_upload_one_reports_correct_extension(monkeypatch):
    """Server-side uploads name files with the MIME-matching extension."""
    from gemini_web2api.server import images as server_images
    names = []

    def fake_upload(data, filename, mime):
        """Record the reported filename. Args: per upload_image. Returns: ref."""
        names.append(filename)
        return "/contrib_service/ttl_1d/ref-" + str(len(names))

    monkeypatch.setattr(server_images, "upload_image", fake_upload)
    server_images._ref_cache.clear()
    ref = server_images._upload_one(b"jpegbytes", "image/jpeg")
    assert str(ref) == "/contrib_service/ttl_1d/ref-1"
    assert names == ["image.jpg"]

