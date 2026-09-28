"""Offline regressions for research round 5 vision bridge upgrades.

Covers the at-less rescue chain (empty at + bare references), the
fallback push id, the payload alignment additions (inner[80] thinking
tier, x-goog-ext-525005358-jspb header) and the soft activity
heartbeat wiring in the tab keepalive.
"""
from unittest import mock

import pytest

from gemini_web2api import keepalive, vision_bridge as bridge
from gemini_web2api.budget import RequestBudget, budget_scope
from gemini_web2api.config import CONFIG


@pytest.fixture(autouse=True)
def isolated_vision(monkeypatch):
    """Use only fake sessions and images. Args: monkeypatch. Returns: None."""
    monkeypatch.setitem(CONFIG, 'cookie_file', None)
    monkeypatch.setitem(CONFIG, 'cookie_files', [])
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', 'http://fake:22')
    monkeypatch.setitem(CONFIG, 'vision_bridge_server_upload', False)
    monkeypatch.setitem(CONFIG, 'log_requests', False)
    monkeypatch.setattr('gemini_web2api.image_prep.prepare_image',
                        lambda data, mime, *a, **kw: (data, mime))


def _chain_js(**kwargs):
    """Build one page-chain script with a single tiny image.

    Args:
        **kwargs: forwarded to bridge._page_chain_js.

    Returns:
        The generated JavaScript source string.
    """
    return bridge._page_chain_js('hi', [(b'img', 'image/png')], **kwargs)


def test_chain_carries_fallback_push_id_and_atless_path():
    """Missing SNlM0e must degrade, not hard-fail. Args: None. Returns: None."""
    js = _chain_js()
    # Fallback push id from the reference clients (g4f, still accepted).
    assert "'feeds/mcudyrk2a4khkz'" in js
    # at-less downgrade: empty-string at plus the [[ref], name] form.
    assert "var atless = !at" in js
    assert "if (atless) at = ''" in js
    assert "atless ? [[ref], name]" in js
    # The old hard gate on at/push_id is gone; bl remains mandatory.
    assert "no at/push_id" not in js
    assert "no build label" in js


def test_chain_payload_alignment_additions():
    """New reference-client payload fields are present. Args: None. Returns: None."""
    js = _chain_js()
    assert "inner[80]" in js
    assert "> 0 ? 2 : 1" in js  # thinking tier 2 (extended) / 1 (standard)
    assert "x-goog-ext-525005358-jspb" in js
    assert "inner[59] = reqUuid" in js
    # Success reports the atless flag for observability.
    assert "{sg: sgText, atless: atless}" in js


def test_bare_ref_flag_still_uses_har_attachment_form():
    """Explicit bare_ref keeps the observed [ref, 1, null, mime] shape.

    Args: None. Returns: None.
    """
    js = _chain_js(allow_bare_ref=True)
    assert "[[ref, 1, null, img.mime], name]" in js


def test_atless_success_returns_generation():
    """An at-less success flows through like a normal chain. Args: None."""
    result = {'sg': 'x', 'atless': True}
    with mock.patch.object(bridge, '_server_upload_all', return_value=None), \
            mock.patch.object(bridge, '_run_page_chain', return_value=result) as chain:
        with budget_scope(RequestBudget(5)):
            out = bridge.vision_generate('p', [(b'img', 'image/png')])
    assert out == 'x'
    assert chain.call_count == 1


def test_activity_ping_js_shape():
    """Heartbeat uses batchexecute ESY5D with the activity flag.

    Args: None. Returns: None.
    """
    js = bridge._ACTIVITY_JS
    assert 'ESY5D' in js
    assert 'bard_activity_enabled' in js
    assert 'batchexecute' in js
    assert 'no-at' in js  # never runs without a live token


def test_activity_ping_failure_is_soft():
    """Transport failures return ok=False instead of raising. Args: None."""
    with mock.patch.object(bridge, '_find_gemini_tab',
                           side_effect=bridge.VisionBridgeError('down')):
        assert bridge.page_activity_ping() == {"ok": False}


def test_keepalive_runs_activity_ping_after_fresh_probe(monkeypatch):
    """Fresh at triggers the heartbeat once per cycle. Args: monkeypatch."""
    monkeypatch.setitem(CONFIG, 'vision_tab_keepalive_sec', 30)
    keepalive._vision_tab_last['ts'] = 0.0
    with mock.patch.object(bridge, 'fetch_page_tokens',
                           return_value={'at': 'AOvxX'}) as tokens, \
            mock.patch.object(bridge, 'page_activity_ping',
                              return_value={'ok': True}) as ping:
        keepalive._maybe_keep_vision_tab()
    tokens.assert_called_once_with(force=True)
    ping.assert_called_once()
    keepalive._vision_tab_last['ts'] = 0.0
    with mock.patch.object(bridge, 'fetch_page_tokens',
                           return_value={'at': 'AOvxX'}), \
            mock.patch.object(bridge, 'page_activity_ping',
                              side_effect=Exception('boom')):
        keepalive._maybe_keep_vision_tab()  # heartbeat failure is non-fatal
