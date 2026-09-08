"""Deadline enforcement while a downstream reader stalls, without external traffic."""
import http.client
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from gemini_web2api.budget import RequestBudget, RequestDeadlineExceeded, budget_scope
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.server.writer import BudgetWriter
from gemini_web2api.upstream import concurrency


class WriterBudgetTests(unittest.TestCase):
    """Each real write receives the remaining budget and restores prior state."""

    def test_actual_write_uses_remaining_socket_timeout(self):
        """Do not retain the 120-second body-reader timeout. Args: None. Returns: None."""
        clock = [100.0]
        connection = mock.Mock()
        connection.gettimeout.return_value = 120
        underlying = mock.Mock()
        underlying.write.return_value = 1
        writer = BudgetWriter(underlying, connection, lambda: None)
        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                budget_scope(RequestBudget(seconds=1)):
            clock[0] = 100.75
            writer.write(b"x")
        self.assertEqual(connection.settimeout.call_args_list, [mock.call(0.25), mock.call(120)])

    def test_terminal_error_grace_is_not_renewed_per_write(self):
        """The complete error has one shared grace deadline. Args: None. Returns: None."""
        clock = [100.0]
        connection = mock.Mock()
        connection.gettimeout.return_value = 120
        underlying = mock.Mock()
        writer = BudgetWriter(underlying, connection, lambda: 101.0)
        with mock.patch("gemini_web2api.server.writer.time.monotonic", side_effect=lambda: clock[0]):
            writer.write(b"headers")
            clock[0] = 100.9
            writer.write(b"body")
            clock[0] = 101.1
            with self.assertRaises(socket.timeout):
                writer.write(b"too late")
        durations = [call.args[0] for call in connection.settimeout.call_args_list if call.args[0] != 120]
        self.assertEqual(durations[0], 1)
        self.assertAlmostEqual(durations[1], 0.1)
        self.assertEqual(underlying.write.call_count, 2)

    def test_send_timeout_is_mapped_to_total_deadline(self):
        """Expired socket writes raise the typed control error. Args: None. Returns: None."""
        clock = [0.0]
        connection = mock.Mock()
        connection.gettimeout.return_value = 120

        def write(data):
            """Simulate a blocked socket exhausting time. Args: data. Returns: never."""
            clock[0] = 2
            raise socket.timeout("blocked")

        writer = BudgetWriter(SimpleNamespace(write=write), connection, lambda: None)
        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                budget_scope(RequestBudget(seconds=1)):
            with self.assertRaises(RequestDeadlineExceeded):
                writer.write(b"x")
        self.assertEqual(connection.settimeout.call_args.args[0], 120)


class SmallBufferHandler(GeminiHandler):
    """Force backpressure using small loopback send buffers."""

    def setup(self):
        """Limit the kernel send buffer. Args: None. Returns: None."""
        super().setup()
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)


class SlowClientBudgetTests(unittest.TestCase):
    """Real socket behavior, fake upstream generation and short deadlines."""

    def setUp(self):
        """Start an isolated loopback server. Args: None. Returns: None."""
        patch = mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False,
                                        "request_deadline_sec": 0.25, "max_concurrent_requests": 1})
        patch.start()
        self.addCleanup(patch.stop)
        self.server = ThreadedServer(("127.0.0.1", 0), SmallBufferHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        """Close the loopback test server. Args: None. Returns: None."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_client_that_does_not_read_cannot_hold_slot_for_120_seconds(self):
        """Large delta plus backpressure still releases capacity promptly. Args: None. Returns: None."""
        released = threading.Event()
        entered = threading.Event()
        slot = threading.BoundedSemaphore(1)

        def stream(*args):
            """Hold one slot while yielding to the socket writer. Args: ignored. Yields: text."""
            try:
                with concurrency._UpstreamSlot():
                    entered.set()
                    yield "x" * (4 * 1024 * 1024)
            finally:
                released.set()

        with mock.patch.object(concurrency, "_get_semaphore", return_value=slot), \
                mock.patch("gemini_web2api.server.openai_chat._upload_images", return_value=None), \
                mock.patch("gemini_web2api.server.openai_chat.generate_stream", side_effect=stream), \
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client.settimeout(3)
            client.connect(self.server.server_address)
            body = b'{"messages":[{"content":"hi"}],"stream":true}'
            headers = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n").encode()
            started = time.monotonic()
            client.sendall(headers + body)
            self.assertTrue(entered.wait(1))
            # Deliberately do not read the response. Include <=1s terminal-error
            # grace and scheduling slack; this used to wait the old 120s timeout.
            self.assertTrue(released.wait(2.5), "upstream capacity leaked behind blocked write")
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(slot.acquire(blocking=False))
            slot.release()

    def test_body_local_timeout_and_overall_deadline_are_distinct(self):
        """Map body cap to 408 and total cap to 504. Args: None. Returns: None."""
        for total, body_limit, expected in ((0.04, 1, 504), (1, 0.04, 408)):
            CONFIG["request_deadline_sec"] = total
            CONFIG["request_body_timeout_sec"] = body_limit
            with self.subTest(expected=expected), socket.create_connection(self.server.server_address, timeout=2) as client:
                client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{")
                response = http.client.HTTPResponse(client)
                response.begin()
                data = response.read()
                response.close()
                self.assertEqual(response.status, expected, data)
