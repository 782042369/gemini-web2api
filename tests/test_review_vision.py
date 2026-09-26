"""Offline regressions for browser budgets and account-scoped image references."""
import json
import threading
import time
from unittest import mock

import pytest

from gemini_web2api import vision_bridge as bridge
from gemini_web2api.budget import (
    RequestBudget,
    RequestCancelled,
    RequestDeadlineExceeded,
    budget_scope,
    current_budget,
)
from gemini_web2api.config import CONFIG
from gemini_web2api.server import images
from gemini_web2api.server.openai_chat import OpenAIChatMixin
from gemini_web2api.upstream import cookies
from gemini_web2api.vision_control import parallel_uploads


@pytest.fixture(autouse=True)
def isolated_vision(monkeypatch):
    """Use only fake sessions and images. Args: monkeypatch. Returns: None."""
    monkeypatch.setitem(CONFIG, 'cookie_file', None)
    monkeypatch.setitem(CONFIG, 'cookie_files', [])
    monkeypatch.setitem(CONFIG, 'vision_bridge_url', 'http://fake:22')
    monkeypatch.setitem(CONFIG, 'vision_bridge_server_upload', False)
    monkeypatch.setitem(CONFIG, 'log_requests', False)
    monkeypatch.setattr('gemini_web2api.image_prep.prepare_image', lambda data, mime, *a, **kw: (data, mime))


def test_cancelled_bridge_never_prepares_or_generates():
    """Cancelled callers cannot start another chain. Args: None. Returns: None."""
    budget = RequestBudget(1)
    with budget_scope(budget), mock.patch.object(bridge, '_run_page_chain') as chain:
        budget.cancel()
        with pytest.raises(RequestCancelled):
            bridge.vision_generate('identify', [(b'img', 'image/png')])
    chain.assert_not_called()


def test_lock_wait_obeys_request_deadline():
    """Queue time cannot exceed the caller budget. Args: None. Returns: None."""
    with bridge._bridge_lock, budget_scope(RequestBudget(0.03)):
        with pytest.raises(RequestDeadlineExceeded):
            bridge.vision_generate('identify', [(b'img', 'image/png')])


def test_cancelled_token_lookup_does_not_invalidate_shared_cache():
    """Cancellation is not a browser health failure. Args: None. Returns: None."""
    state = dict(tokens={'at': 'valid'}, ts=0.0, fail_ts=0.0)
    with mock.patch.dict(bridge._bridge_token_state, state, clear=True):
        with mock.patch.object(bridge, '_fetch_page_tokens_now', side_effect=RequestCancelled()):
            with pytest.raises(RequestCancelled):
                bridge.fetch_page_tokens(force=True)
        assert bridge._bridge_token_state == state


def test_preflight_cancellation_never_attempts_generation():
    """API preflight must propagate control failures. Args: None. Returns: None."""
    with mock.patch('gemini_web2api.server.openai_chat.fetch_page_tokens', side_effect=RequestCancelled()):
        with mock.patch('gemini_web2api.server.openai_chat.vision_generate') as generate:
            with pytest.raises(RequestCancelled):
                OpenAIChatMixin()._chat_via_vision_bridge('p', [], 'm', 'id', False)
        generate.assert_not_called()


def test_upload_cancellation_never_uses_in_page_fallback(monkeypatch):
    """Control failures bypass upload rescue. Args: monkeypatch. Returns: None."""
    monkeypatch.setitem(CONFIG, 'vision_bridge_server_upload', True)
    with mock.patch.object(images, '_upload_one', side_effect=RequestCancelled()):
        with mock.patch.object(bridge, '_run_page_chain') as chain:
            with pytest.raises(RequestCancelled):
                bridge.vision_generate('p', [(b'img', 'image/png')])
        chain.assert_not_called()


def test_parallel_upload_preserves_account_auth_and_deadline():
    """Workers inherit context and restore the caller. Args: None. Returns: None."""
    budget = RequestBudget(2)
    seen = []
    def upload(data, mime):
        """Capture worker-local identity. Args: bytes, mime. Returns: fake ref."""
        seen.append((cookies._active_cookie_path(), cookies._active_auth_user(), current_budget().deadline))
        return '/ref/' + data.decode()
    with cookies.use_cookie('/fake/B'), budget_scope(budget):
        cookies.set_active_auth_user(2)
        with mock.patch.object(images, '_upload_one', side_effect=upload):
            refs = bridge._server_upload_all([(b'a', 'image/png'), (b'b', 'image/jpeg')])
        assert cookies._active_cookie_path() == '/fake/B' and current_budget() is budget
    assert refs == [('/ref/a', 'image/png'), ('/ref/b', 'image/jpeg')]
    assert seen == [('/fake/B', 2, budget.deadline)] * 2


def test_parallel_upload_timeout_does_not_wait_for_executor_shutdown():
    """Slow worker cleanup cannot extend caller timeout. Args: None. Returns: None."""
    release, entered = threading.Event(), threading.Event()
    finished = [threading.Event(), threading.Event()]
    def upload(data, mime):
        """Block until test cleanup. Args: data, mime. Returns: ref."""
        entered.set()
        try:
            release.wait(2)
            return '/ref'
        finally:
            finished[int(data)].set()
    try:
        started = time.monotonic()
        with budget_scope(RequestBudget(0.08)), pytest.raises(RequestDeadlineExceeded):
            parallel_uploads([(b'0', 'image/png'), (b'1', 'image/png')], upload)
        assert entered.is_set() and time.monotonic() - started < 0.6
    finally:
        release.set()
        for event in finished:
            assert event.wait(2)


def test_cdp_evaluate_uses_remaining_budget_and_closes_socket():
    """Events do not reset the absolute deadline. Args: None. Returns: None."""
    clock = [0.0]
    ws = mock.Mock()
    def recv():
        """Advance fake time per event. Args: None. Returns: CDP event."""
        clock[0] += 0.04
        return json.dumps({'method': 'event'})
    ws.recv.side_effect = recv
    with mock.patch('gemini_web2api.budget.time.monotonic', side_effect=lambda: clock[0]):
        with mock.patch.object(bridge, '_tab_ws', return_value=ws) as connect, budget_scope(RequestBudget(0.1)):
            with pytest.raises(RequestDeadlineExceeded):
                bridge._tab_evaluate({}, '1', 180)
        assert connect.call_args.args[1] <= 0.1
    ws.close.assert_called_once_with(timeout=0)


def test_page_chain_installs_abort_timer_and_sends_targeted_cancel():
    """Abort only this request on cancellation. Args: None. Returns: None."""
    seen = []
    def evaluate(tab, js, timeout):
        """Capture execution and its cleanup. Args: tab, js, timeout. Returns: None."""
        seen.append((js, timeout))
        if len(seen) == 1:
            raise RequestCancelled('gone')
    with mock.patch.object(bridge, '_find_gemini_tab', return_value={'id': 'fake'}):
        with mock.patch.object(bridge, '_tab_evaluate', side_effect=evaluate), budget_scope(RequestBudget(2)):
            with pytest.raises(RequestCancelled):
                bridge._run_page_chain('(async function(){return await fetch("fake")})()')
    assert 'AbortController' in seen[0][0] and 'signal:controller.signal' in seen[0][0]
    assert 'setTimeout' in seen[0][0] and seen[0][1] <= 2
    assert '?.abort()' in seen[1][0] and seen[1][1] == 0.1


def test_upload_cache_is_scoped_to_account_auth_mime_and_session(monkeypatch):
    """Identical bytes may only reuse same-session refs. Args: monkeypatch. Returns: None."""
    cookie = ['session-1']
    monkeypatch.setattr(images, 'load_cookie', lambda: (cookie[0], 'sapisid'))
    monkeypatch.setattr(images, '_active_auth_user', lambda: cookies._active_cookie.auth_user_override)
    with mock.patch.object(images, 'upload_image', side_effect=['/a', '/b', '/auth', '/mime', '/session']) as upload:
        with cookies.use_cookie('/fake/A'):
            cookies.set_active_auth_user(0)
            assert images._upload_one(b'same', 'image/png') == '/a'
            assert images._upload_one(b'same', 'image/png') == '/a'
        with cookies.use_cookie('/fake/B'):
            cookies.set_active_auth_user(0)
            assert images._upload_one(b'same', 'image/png') == '/b'
            cookies.set_active_auth_user(1)
            assert images._upload_one(b'same', 'image/png') == '/auth'
            assert images._upload_one(b'same', 'image/jpeg') == '/mime'
            cookie[0] = 'session-2'
            assert images._upload_one(b'same', 'image/jpeg') == '/session'
    assert upload.call_count == 5
    assert all('session-' not in key for key in images._ref_cache)

def test_expired_chain_does_not_start_transient_retry():
    """Do not retry stale failures after caller expiry. Args: None. Returns: None."""
    clock = [0.0]
    def chain(js):
        """Return a late retryable failure. Args: js. Returns: failure dict."""
        clock[0] = 2
        return {'stage': 'exception', 'err': 'late'}
    with mock.patch('gemini_web2api.budget.time.monotonic', side_effect=lambda: clock[0]):
        with mock.patch.object(bridge, '_run_page_chain', side_effect=chain) as generated:
            with budget_scope(RequestBudget(1)), pytest.raises(RequestDeadlineExceeded):
                bridge.vision_generate('p', [(b'img', 'image/png')])
        assert generated.call_count == 1
    assert bridge._bridge_lock.acquire(blocking=False)
    bridge._bridge_lock.release()
