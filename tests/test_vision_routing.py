"""Offline tests for vision_mode routing, bridge token merge and readiness."""
import threading

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
