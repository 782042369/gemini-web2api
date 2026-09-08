"""Real loopback HTTP regressions for queue/deadline error mapping and scope cleanup."""
import http.client
import json
import threading
import unittest
from unittest import mock

from gemini_web2api.budget import (RequestDeadlineExceeded, QueueTimeout,
                                  QueueFull, current_budget)
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer


class HTTPBudgetTests(unittest.TestCase):
    """Check transport-visible results without any Google calls."""

    @classmethod
    def setUpClass(cls):
        """Start a test server. Args: None. Returns: None."""
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        """Release loopback resources. Args: None. Returns: None."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(3)

    def setUp(self):
        """Isolate configuration. Args: None. Returns: None."""
        patch = mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False})
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, path, payload):
        """Call the local test handler. Args: path, payload. Returns: status, type, text."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read().decode()
        finally:
            conn.close()

    def test_nonstream_control_errors_keep_status_and_machine_code(self):
        """Do not flatten deadline/queue failures to 502. Args: None. Returns: None."""
        for error in (RequestDeadlineExceeded("expired"), QueueTimeout("wait expired"), QueueFull("full")):
            with self.subTest(error=error), \
                    mock.patch("gemini_web2api.server.openai_chat._upload_images", return_value=None), \
                    mock.patch("gemini_web2api.server.openai_chat.generate", side_effect=error):
                status, content_type, text = self.post("/v1/chat/completions", {"messages": [{"content": "hello"}]})
                self.assertEqual(status, error.status)
                self.assertTrue(content_type.startswith("application/json"))
                self.assertEqual(json.loads(text)["error"]["code"], error.code)

    def test_expiry_during_upload_stops_generation(self):
        """No generation after an upload consumed the deadline. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.server.openai_chat._upload_images", side_effect=RequestDeadlineExceeded("upload")), \
                mock.patch("gemini_web2api.server.openai_chat.generate") as generate:
            status, _, text = self.post("/v1/chat/completions", {"messages": [{"content": "hi"}]})
            self.assertEqual(status, 504)
            self.assertEqual(json.loads(text)["error"]["code"], "request_timeout")
            generate.assert_not_called()

    def test_expiry_before_sse_headers_uses_valid_http_error(self):
        """Never send raw SSE bytes before HTTP headers. Args: None. Returns: None."""
        clock = [0.0]
        CONFIG["request_deadline_sec"] = 5

        def upload(images):
            """Consume simulated time. Args: parsed images. Returns: no references."""
            clock[0] += 6
            return None

        cases = [
            ("openai_chat", "/v1/chat/completions", {"messages": [{"content": "hi"}], "stream": True}),
            ("google", "/v1/models/gemini-3.8-flash:streamGenerateContent", {"contents": [{"parts": [{"text": "hi"}]}]}),
        ]
        for module, path, payload in cases:
            clock[0] = 0
            with self.subTest(module=module), \
                    mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                    mock.patch(f"gemini_web2api.server.{module}._upload_images", side_effect=upload), \
                    mock.patch(f"gemini_web2api.server.{module}.generate_stream") as generate:
                status, content_type, text = self.post(path, payload)
                self.assertEqual(status, 504, text)
                self.assertTrue(content_type.startswith("application/json"))
                self.assertEqual(json.loads(text)["error"]["code"], "request_timeout")
                generate.assert_not_called()

    def test_midstream_deadline_has_explicit_error_not_success(self):
        """Keep a stream's committed HTTP status but terminate with error. Args: None. Returns: None."""
        def stream(*args):
            """Emit one piece before a timeout. Args: ignored. Yields: text."""
            yield "first"
            raise RequestDeadlineExceeded("request deadline exceeded during generation")

        with mock.patch("gemini_web2api.server.openai_chat._upload_images", return_value=None), \
                mock.patch("gemini_web2api.server.openai_chat.generate_stream", side_effect=stream):
            status, content_type, text = self.post("/v1/chat/completions", {"messages": [{"content": "hi"}], "stream": True})
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/event-stream")
            self.assertIn('"code": "request_timeout"', text)
            self.assertNotIn('"finish_reason": "stop"', text)
            self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_upload_and_generate_share_deadline_then_context_is_cancelled(self):
        """No deadline reset between HTTP stages. Args: None. Returns: None."""
        seen = []

        def upload(images):
            """Capture the HTTP budget. Args: images. Returns: None."""
            seen.append(current_budget())
            return None

        def generate(*args):
            """Capture the same HTTP budget. Args: ignored. Returns: translation."""
            seen.append(current_budget())
            return "translated"

        with mock.patch("gemini_web2api.server.openai_chat._upload_images", side_effect=upload), \
                mock.patch("gemini_web2api.server.openai_chat.generate", side_effect=generate):
            status, _, _ = self.post("/v1/chat/completions", {"messages": [{"content": "hi"}]})
            self.assertEqual(status, 200)
            self.assertEqual(len(seen), 2)
            self.assertIs(seen[0], seen[1])
            self.assertTrue(seen[0].cancelled.wait(1))
