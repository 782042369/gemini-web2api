"""Offline tests for the XSRF self-heal path (upstream PR#100 pattern)."""
import importlib
import threading
import unittest
from unittest import mock

from gemini_web2api import keepalive, multimodal, vision_bridge

# upstream/__init__ re-exports the generate() function, shadowing the
# submodule attribute - import_module returns the real module object.
gen = importlib.import_module("gemini_web2api.upstream.generate")


class _FakeResponse:
    """Minimal response stand-in for transport errors."""

    def __init__(self, status_code, text="", readable=True):
        self.status_code = status_code
        self._text = text
        self._readable = readable

    @property
    def text(self):
        if not self._readable:
            raise RuntimeError("streamed body not buffered")
        return self._text


class _FakeHttpError(Exception):
    """curl_cffi/httpx-shaped error carrying a response."""

    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _FakeUrllibError(Exception):
    """urllib.error.HTTPError shape: code, no readable body."""

    code = 400


class XsrfRejectionTests(unittest.TestCase):
    """Detection rules for xsrf_rejection."""

    def test_400_with_xsrf_body_is_detected(self):
        err = _FakeHttpError(_FakeResponse(400, '[["er",null,400,null,[{"48448350":["xsrf"]}]]]'))
        self.assertTrue(keepalive.xsrf_rejection(err))

    def test_400_unreadable_body_is_detected(self):
        err = _FakeHttpError(_FakeResponse(400, readable=False))
        self.assertTrue(keepalive.xsrf_rejection(err))

    def test_400_readable_non_xsrf_body_is_not_detected(self):
        err = _FakeHttpError(_FakeResponse(400, "invalid payload shape"))
        self.assertFalse(keepalive.xsrf_rejection(err))

    def test_500_is_not_detected(self):
        err = _FakeHttpError(_FakeResponse(500, "server error"))
        self.assertFalse(keepalive.xsrf_rejection(err))

    def test_urllib_http_error_shape_is_detected(self):
        self.assertTrue(keepalive.xsrf_rejection(_FakeUrllibError()))


class InvalidateTests(unittest.TestCase):
    """invalidate_session_tokens clears every token cache layer."""

    def test_invalidate_clears_caches(self):
        multimodal._page_tokens_cache["k"] = {"tokens": {"at": "x"},
                                              "ts": 1.0, "mtime": 0.0,
                                              "lock": threading.Lock()}
        vision_bridge._bridge_token_state.update(tokens={"at": "x"}, ts=1.0)
        keepalive._xsrf_refreshed_at["p"] = 1.0
        keepalive.invalidate_session_tokens()
        self.assertEqual(multimodal._page_tokens_cache, {})
        self.assertIsNone(vision_bridge._bridge_token_state["tokens"])
        self.assertEqual(keepalive._xsrf_refreshed_at, {})


class HealRetryTests(unittest.TestCase):
    """_generate_upstream retries once after an XSRF rejection."""

    def setUp(self):
        self.calls = []
        patcher = mock.patch.object(keepalive, "invalidate_session_tokens")
        self.inval = patcher.start()
        self.addCleanup(patcher.stop)

        def fake_stream(sess, client, url, body, headers):
            self.calls.append(body)
            if len(self.calls) == 1:
                raise _FakeHttpError(_FakeResponse(400, '{"er":["xsrf"]}'))
            yield 'chunk'

        patches = [
            mock.patch.object(gen, "_stream_upstream_chunks", fake_stream),
            mock.patch.object(gen, "_refresh_xsrf"),
            mock.patch.object(gen, "_UpstreamSlot"),
            mock.patch.object(gen, "extract_response_text", return_value="healed"),
            mock.patch.object(gen, "_build_payload", return_value="f.req=x"),
            mock.patch.object(gen, "_get_url", return_value="https://x/"),
            mock.patch.object(gen, "_build_headers", return_value={}),
            mock.patch.object(gen, "get_browser_session", return_value=None),
            mock.patch.object(gen, "_get_httpx_client", return_value=None),
            mock.patch.object(gen, "extract_conversation_id", return_value=None),
            mock.patch.object(gen, "schedule_history_delete"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_heal_retry_succeeds_on_second_attempt(self):
        text = gen._generate_upstream("p", 1, 4)
        self.assertEqual(text, "healed")
        self.assertEqual(len(self.calls), 2)
        self.inval.assert_called_once()

    def test_heal_only_once_per_request(self):
        def always_xsrf(sess, client, url, body, headers):
            self.calls.append(body)
            raise _FakeHttpError(_FakeResponse(400, '{"er":["xsrf"]}'))

        gen._stream_upstream_chunks = always_xsrf
        self.addCleanup(delattr, gen, "_stream_upstream_chunks")
        with self.assertRaises(_FakeHttpError):
            gen._generate_upstream("p", 1, 4)
        self.assertGreaterEqual(len(self.calls), 2)
        self.inval.assert_called_once()
