"""Offline HTTP and tool contract regressions; upstream side effects are forbidden."""
import copy
import http.client
import json
import socket
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.tools import parse_google_function_calls, parse_tool_calls


class ContractTests(unittest.TestCase):
    """Exercise real local HTTP framing with mocked upstream services."""

    @classmethod
    def setUpClass(cls):
        """Start a loopback test server. Args: None. Returns: None."""
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        """Close test sockets and worker. Args: None. Returns: None."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        """Forbid unmocked upstream work. Args: None. Returns: None."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False}))
        self.stack.enter_context(mock.patch("gemini_web2api.server.google._MICROBATCHER.window", 0))
        for module in ("openai_chat", "openai_responses", "google"):
            for method in ("generate", "generate_stream", "_upload_images"):
                self.stack.enter_context(mock.patch(f"gemini_web2api.server.{module}.{method}",
                                                     side_effect=AssertionError("unexpected upstream work")))

    def post(self, path, payload):
        """Send JSON. Args: path, payload. Returns: HTTP status, headers, text."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def raw_post(self, headers, body=b""):
        """Send exact wire framing. Args: raw headers, body. Returns: HTTPResponse and bytes."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            wire = ("POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n" + headers + "\r\n").encode()
            sock.sendall(wire + body)
            sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            data = response.read()
            response.close()
            return response, data

    def test_nested_chat_values_return_field_specific_400(self):
        """Validate before prompt/upload work. Args: None. Returns: None."""
        base = {"messages": [{"role": "user", "content": "hello"}]}
        mutations = [
            ({"tools": [1]}, "tools[0]"),
            ({"tools": [{"type": "function", "function": None}]}, "tools[0].function"),
            ({"tools": [{"type": "function", "function": {}}]}, "tools[0].function.name"),
            ({"tools": [{"type": "function", "name": "f", "parameters": []}]}, "tools[0].parameters"),
            ({"messages": [{"content": [1]}]}, "messages[0].content[0]"),
            ({"messages": [{"content": [{"type": "text", "text": None}]}]}, "messages[0].content[0].text"),
            ({"messages": [{"tool_calls": [None]}]}, "messages[0].tool_calls[0]"),
            ({"messages": [{"content": {"text": "oops"}}]}, "messages[0].content"),
            ({"messages": [{"content": [{"type": "image_url", "image_url": {"url": 2}}]}]},
             "messages[0].content[0].image_url.url"),
            ({"stream": "false"}, "stream"),
            ({"stream_options": {"include_usage": 1}}, "stream_options.include_usage"),
            ({"tool_choice": {"type": "function", "function": []}}, "tool_choice"),
            ({"tool_choice": "required"}, "tool_choice"),
        ]
        for change, param in mutations:
            with self.subTest(change=change):
                status, _, raw = self.post("/v1/chat/completions", {**base, **change})
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["param"], param)

    def test_nested_responses_values_return_400(self):
        """Reject invalid Responses shapes. Args: None. Returns: None."""
        bad = [
            {"tools": [{"type": "function"}]}, {"input": [False]},
            {"instructions": []}, {"input": [{"role": "assistant", "content": [42]}]},
            {"input": [{"type": "function_call", "call_id": "c", "name": None}]},
            {"input": [{"type": "function_call_output", "call_id": "c", "output": 123}]},
        ]
        for change in bad:
            with self.subTest(change=change):
                status, _, raw = self.post("/v1/responses", {"input": "hello", **change})
                self.assertEqual(status, 400, raw)
                self.assertIn("param", json.loads(raw)["error"])

    def test_nested_google_values_return_400_before_batching(self):
        """Reject malformed Google input early. Args: None. Returns: None."""
        base = {"contents": [{"parts": [{"text": "hello"}]}]}
        bad = [
            {"contents": [3]}, {"contents": [{"parts": [None]}]},
            {"contents": [{"parts": [{"text": []}]}]},
            {"contents": [{"parts": [{"inlineData": []}]}]},
            {"contents": [{"parts": [{"functionCall": {}}]}]},
            {"systemInstruction": "system"}, {"systemInstruction": {"parts": [5]}},
            {"tools": [False]}, {"tools": [{"functionDeclarations": [7]}]},
            {"toolConfig": {"functionCallingConfig": False}},
            {"toolConfig": {"functionCallingConfig": {"mode": []}}},
        ]
        for change in bad:
            with self.subTest(change=change):
                status, _, raw = self.post("/v1beta/models/gemini-3.8-flash:generateContent", {**base, **change})
                self.assertEqual(status, 400, raw)
                self.assertIn("message", json.loads(raw)["error"])

    def test_google_optional_null_config_is_safe(self):
        """Optional null is accepted consistently. Args: None. Returns: None."""
        for config in (None, {"functionCallingConfig": None}):
            with self.subTest(config=config), mock.patch("gemini_web2api.server.google.generate", return_value="ok"), \
                    mock.patch("gemini_web2api.server.google._upload_images", return_value=None):
                status, _, raw = self.post("/v1/models/gemini-3.8-flash:generateContent", {
                    "contents": [{"parts": [{"text": "hi"}]}], "toolConfig": config})
                self.assertEqual(status, 200, raw)

    def test_function_call_history_and_flat_choice_survive_normalization(self):
        """Keep Responses tool history. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.server.openai_responses.generate", return_value="done") as generate, \
                mock.patch("gemini_web2api.server.openai_responses._upload_images", return_value=None):
            status, _, raw = self.post("/v1/responses", {"input": [
                {"type": "function_call", "call_id": "c", "name": "weather", "arguments": '{"city":"Beijing"}'},
                {"type": "function_call_output", "call_id": "c", "output": "sunny"}],
                "tools": [{"type": "function", "name": "weather"}],
                "tool_choice": {"type": "function", "name": "weather"}})
            self.assertEqual(status, 200, raw)
            prompt = generate.call_args.args[0]
            self.assertIn('MUST call the tool "weather"', prompt)
            self.assertIn('"city":"Beijing"', prompt)
            self.assertIn("sunny", prompt)

    def test_failed_streams_send_errors_not_success_or_second_http(self):
        """Fail after a partial delta for all protocols. Args: None. Returns: None."""
        def broken(*args):
            """Yield partial text then fail. Args: ignored. Yields: text."""
            yield "partial"
            raise RuntimeError("upstream-secret-should-not-leak")

        cases = [
            ("openai_chat", "/v1/chat/completions", {"messages": [{"content": "hi"}], "stream": True}),
            ("openai_responses", "/v1/responses", {"input": "hi", "stream": True}),
            ("google", "/v1beta/models/gemini-3.8-flash:streamGenerateContent", {"contents": [{"parts": [{"text": "hi"}]}]}),
        ]
        for module, path, payload in cases:
            with self.subTest(module=module), \
                    mock.patch(f"gemini_web2api.server.{module}.generate_stream", side_effect=broken), \
                    mock.patch(f"gemini_web2api.server.{module}._upload_images", return_value=None):
                status, headers, raw = self.post(path, payload)
                self.assertEqual(status, 200)
                self.assertEqual(headers["X-Accel-Buffering"], "no")
                self.assertIn("partial", raw)
                self.assertIn("stream failed", raw)
                self.assertNotIn("upstream-secret", raw)
                self.assertNotIn("HTTP/1.1", raw)
                self.assertNotIn('"finish_reason": "stop"', raw)
                self.assertNotIn('"finishReason": "STOP"', raw)
                self.assertNotIn('"status": "completed"', raw)
                if module == "openai_chat":
                    self.assertTrue(raw.endswith("data: [DONE]\n\n"))
                if module == "openai_responses":
                    events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
                    self.assertEqual(events[-1]["type"], "response.failed")
                    self.assertEqual([e["sequence_number"] for e in events], list(range(1, len(events) + 1)))

    def test_responses_delta_arrives_before_generation_finishes(self):
        """Use an Event gate rather than a timing guess. Args: None. Returns: None."""
        release = threading.Event()
        entered = threading.Event()

        def streaming(*args):
            """Gate final delta. Args: ignored. Yields: two text deltas."""
            yield "first"
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test gate was not released")
            yield "last"

        with mock.patch("gemini_web2api.server.openai_responses.generate_stream", side_effect=streaming), \
                mock.patch("gemini_web2api.server.openai_responses._upload_images", return_value=None):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                conn.request("POST", "/v1/responses", json.dumps({"input": "hi", "stream": True}))
                response = conn.getresponse()
                while True:
                    line = response.readline()
                    self.assertTrue(line, "stream ended before first delta")
                    if b'"delta": "first"' in line:
                        break
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                release.set()
                self.assertIn(b"response.completed", response.read())
            finally:
                release.set()
                conn.close()

    def test_chunked_request_is_decoded_and_does_not_consume_next_request(self):
        """Send explicit chunks followed by a pipelined GET. Args: None. Returns: None."""
        payload = json.dumps({"messages": [{"content": "chunked"}]}).encode()
        with mock.patch("gemini_web2api.server.openai_chat.generate", return_value="ok"), \
                mock.patch("gemini_web2api.server.openai_chat._upload_images", return_value=None), \
                socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            wire = b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
            for chunk in (payload[:9], payload[9:]):
                wire += f"{len(chunk):x};part=yes\r\n".encode() + chunk + b"\r\n"
            wire += b"0\r\nX-Test: trailer\r\n\r\nGET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            sock.sendall(wire)
            # Read all bytes once: HTTPResponse buffering can otherwise hide a
            # pipelined response from a second HTTPResponse on the same socket.
            data = bytearray()
            while True:
                block = sock.recv(4096)
                if not block:
                    break
                data.extend(block)
            self.assertEqual(data.count(b"HTTP/1.1 200 OK"), 2, bytes(data))
            self.assertIn(b'"content": "ok"', data)

    def test_invalid_http_framing_closes_connection(self):
        """Reject ambiguity and incomplete framing. Args: None. Returns: None."""
        cases = [
            ("Content-Length: -1\r\n", b""),
            ("Content-Length: +2\r\n", b"{}"),
            ("Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
            ("Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", b"{}"),
            ("Transfer-Encoding: gzip, chunked\r\n", b"0\r\n\r\n"),
            ("Transfer-Encoding: chunked\r\n", b"+2\r\n{}\r\n0\r\n\r\n"),
            ("Transfer-Encoding: chunked\r\n", b"2\r\n{}xx0\r\n\r\n"),
            ("Transfer-Encoding: chunked\r\n", b"0\r\n"),
            ("Transfer-Encoding: chunked\r\n", b"0\r\nContent-Length: 3\r\n\r\n"),
            ("Content-Length: 30\r\n", b"{}"),
        ]
        for headers, body in cases:
            with self.subTest(headers=headers, body=body):
                response, data = self.raw_post(headers, body)
                self.assertEqual(response.status, 400, data)
                self.assertEqual(json.loads(data)["error"]["code"], "invalid_request")

    def test_chunked_size_cap_is_cumulative(self):
        """Enforce decoded byte cap before reading oversized chunk. Args: None. Returns: None."""
        CONFIG["max_request_body_bytes"] = 4
        response, data = self.raw_post("Transfer-Encoding: chunked\r\n", b"3\r\nabc\r\n2\r\nxx\r\n0\r\n\r\n")
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(data)["error"]["code"], "request_too_large")


class ToolOutputContractTests(unittest.TestCase):
    """No model-produced malformed output becomes an executable tool call."""

    def fence(self, marker, data):
        """Build a test fence. Args: marker, data value. Returns: text."""
        fence = chr(96) * 3
        return fence + marker + "\n" + json.dumps(data) + "\n" + fence

    def test_malformed_tool_shapes_are_preserved(self):
        """Keep rejected output as text. Args: None. Returns: None."""
        invalid = [None, [], True, 1, "oops", {}, {"name": []}, {"name": ""},
                   {"name": "f", "arguments": "broken-json"}, {"name": "f", "args": [1]},
                   {"name": "f", "args": {"x": float("nan")}}]
        for marker, parser in (("tool_call", parse_tool_calls), ("function_call", parse_google_function_calls)):
            for data in invalid:
                with self.subTest(marker=marker, data=data):
                    text = self.fence(marker, data)
                    self.assertEqual(parser(text, {"f"}), (text, []))

    def test_empty_allowlist_forbids_every_function(self):
        """Empty is not unrestricted. Args: None. Returns: None."""
        for marker, parser in (("tool_call", parse_tool_calls), ("function_call", parse_google_function_calls)):
            text = self.fence(marker, {"name": "f", "arguments": {}})
            self.assertEqual(parser(text, set()), (text, []))
            self.assertEqual(parser(text, {"other"}), (text, []))
            self.assertEqual(len(parser(text, {"f"})[1]), 1)

    def test_json_string_arguments_are_decoded_once(self):
        """Never double encode JSON arguments. Args: None. Returns: None."""
        text = self.fence("tool_call", {"name": "f", "arguments": '{"nested":{"x":1}}'})
        clean, calls = parse_tool_calls(text, {"f"})
        self.assertEqual(clean, "")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"nested": {"x": 1}})

    def test_google_bare_nested_json_is_not_truncated(self):
        """Decode nested braces correctly. Args: None. Returns: None."""
        data = {"name": "f", "args": {"nested": {"x": "}"}}}
        original = copy.deepcopy(data)
        clean, calls = parse_google_function_calls("function_call\n" + json.dumps(data), {"f"})
        self.assertEqual(clean, "")
        self.assertEqual(calls, [original])
