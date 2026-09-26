"""Regression coverage for credential redaction and lossless independent translations."""
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from gemini_web2api.batching import _microbatch_runner, _MicroBatcher
from gemini_web2api.budget import RequestBudget, RequestDeadlineExceeded, current_budget
from gemini_web2api.config import CONFIG
from gemini_web2api.server.base import BaseAPIHandler
from gemini_web2api.server.google import GoogleGenerateMixin
from gemini_web2api.translation import parse_numbered_translations, split_translation_batches


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'OPTIONS'])
def test_query_keys_never_reach_access_log(method):
    """Redact all query values. Args: HTTP method. Returns: None."""
    path = '/v1beta/models?key=synthetic-secret&trace=private'
    handler = SimpleNamespace(command=method, path=path, client_address=('127.0.0.1', 1))
    with mock.patch('gemini_web2api.server.base.log') as logged:
        BaseAPIHandler.log_message(handler, '"%s" %s %s', f'{method} {path} HTTP/1.1', '200', '-')
    line = logged.call_args.args[0]
    assert 'synthetic-secret' not in line and 'private' not in line
    assert '/v1beta/models' in line and '200' in line


def test_malformed_request_log_does_not_format_raw_request():
    """Parser failures cannot reveal raw credentials. Args: None. Returns: None."""
    handler = SimpleNamespace(command=None, client_address=('127.0.0.1', 1))
    with mock.patch('gemini_web2api.server.base.log') as logged:
        BaseAPIHandler.log_message(handler, 'Bad request syntax (%r)', '/?key=synthetic-secret')
    assert 'synthetic-secret' not in logged.call_args.args[0]


def test_ambiguous_marker_invalidates_truncated_predecessor():
    """Do not trust a silently truncated block. Args: None. Returns: None."""
    text = '[0] body\n[1] reference\n[1] second'
    assert parse_numbered_translations(text, 2) == {}


@pytest.mark.parametrize('source', ['body\n[1] reference', '[0] item', 'body\n  [20] note', 'body\r[1] reference', 'body\u2028[2] note'])
def test_numbered_source_is_translated_intact(source):
    """Isolate numbered source in both translation paths. Args: source. Returns: None."""
    assert split_translation_batches(['a', source, 'b', 'c'], 25, 12000) == [['a'], [source], ['b', 'c']]
    handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())
    with mock.patch('gemini_web2api.server.google.generate', side_effect=[source, 'second']) as generated:
        GoogleGenerateMixin._send_batch_translation(handler, 'Translate', [source, 'b'], 'm', 1, 4, None)
    assert generated.call_args_list[0].args[0].endswith(source)
    parts = handler.send_json.call_args.args[0]['candidates'][0]['content']['parts']
    assert parts[0]['text'] == source
    with mock.patch('gemini_web2api.batching.generate', side_effect=[source, 'second']):
        assert _microbatch_runner(1, 4, None)([source, 'b']) == [source, 'second']


def entries_for(budgets=None):
    """Create two independent callers. Args: optional budgets. Returns: entry list."""
    runner = _microbatch_runner(1, 4, None)
    return [dict(key='same', prompt=p, runner=runner, budget=b,
                 holder=dict(event=threading.Event(), result=None, error=None))
            for p, b in zip(['a', 'b'], budgets or [None, None])]


@pytest.mark.parametrize('failure', [RuntimeError('rejected'), '', None])
def test_missing_member_failure_preserves_success(failure):
    """A fallback cannot revoke another result. Args: failure. Returns: None."""
    entries = entries_for()
    def generate(prompt, *args):
        """Verify success arrives before fallback. Args: prompt, args. Returns: text."""
        if prompt != 'b':
            return '[0] translated-a'
        assert entries[0]['holder']['event'].is_set()
        if isinstance(failure, Exception):
            raise failure
        return failure
    with mock.patch.dict(CONFIG, {'log_requests': False}), mock.patch('gemini_web2api.batching.generate', side_effect=generate):
        _MicroBatcher(0.01, 6)._run_batch(entries)
    assert entries[0]['holder']['result'] == 'translated-a'
    assert entries[0]['holder']['error'] is None
    assert isinstance(entries[1]['holder']['error'], RuntimeError)


def test_expired_missing_member_never_starts_fallback():
    """Use original member deadlines after packed work. Args: None. Returns: None."""
    clock = [0.0]
    with mock.patch('gemini_web2api.budget.time.monotonic', side_effect=lambda: clock[0]):
        entries = entries_for([RequestBudget(1), RequestBudget(5)])
        def generate(*args):
            """Expire the missing member. Args: ignored. Returns: successful second result."""
            clock[0] = 2
            return '[1] translated-b'
        with mock.patch('gemini_web2api.batching.generate', side_effect=generate) as generated:
            _MicroBatcher(0.01, 6)._run_batch(entries)
        assert generated.call_count == 1
    assert isinstance(entries[0]['holder']['error'], RequestDeadlineExceeded)
    assert entries[1]['holder']['result'] == 'translated-b'


def test_fallback_uses_own_budget_and_cannot_revoke_delivered_peer():
    """A shorter fallback keeps its deadline. Args: None. Returns: None."""
    clock = [0.0]
    with mock.patch('gemini_web2api.budget.time.monotonic', side_effect=lambda: clock[0]):
        budgets = [RequestBudget(2), RequestBudget(10)]
        entries = entries_for(budgets)
        def generate(prompt, *args):
            """Check active budget then expire it. Args: prompt, args. Returns: text."""
            if prompt == 'a':
                assert current_budget() is budgets[0]
                assert entries[1]['holder']['event'].is_set()
                clock[0] = 3
                return 'late-a'
            return '[1] translated-b'
        with mock.patch('gemini_web2api.batching.generate', side_effect=generate):
            _MicroBatcher(0.01, 6)._run_batch(entries)
    assert isinstance(entries[0]['holder']['error'], RequestDeadlineExceeded)
    assert entries[1]['holder']['result'] == 'translated-b'

def test_real_http_query_auth_does_not_log_key():
    """Exercise stdlib access logging on loopback. Args: None. Returns: None."""
    import http.client

    from gemini_web2api.server import GeminiHandler, ThreadedServer
    server = ThreadedServer(('127.0.0.1', 0), GeminiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with mock.patch.dict(CONFIG, {'api_keys': ['synthetic-secret']}):
            with mock.patch('gemini_web2api.server.base.log') as logged:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=2)
                try:
                    conn.request('GET', '/v1beta/models?key=synthetic-secret')
                    response = conn.getresponse()
                    assert response.status == 200
                    response.read()
                finally:
                    conn.close()
                assert logged.called
                assert all('synthetic-secret' not in call.args[0] for call in logged.call_args_list)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
