"""Pure-offline image fetch regressions: fake DNS/sockets, no credentials/Google.

Run with: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_image_fetch -v
"""

import io
import ipaddress
import os
import queue
import socket
import ssl
import threading
import unittest
from email.message import Message
from unittest import mock

from gemini_web2api import image_fetch

_PUBLIC_V4 = "93.184.216.34"
_PUBLIC_V6 = "2606:4700:4700::1111"


def _record(ip, port=80):
    """Make a numeric resolver result without contacting a resolver.

    Args:
        ip: Numeric IPv4 or IPv6 string.
        port: Resolver-returned port (the implementation must use the URL port).

    Returns:
        One getaddrinfo result tuple.
    """
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address


def _wire(body=b"image", headers=(), status=200):
    """Serialize an offline HTTP response, preserving duplicate/malformed headers.

    Args:
        body: Bytes after the header terminator (possibly chunk framing).
        headers: Sequence of name/value pairs.
        status: Response status code.

    Returns:
        Complete response bytes for a fake socket.
    """
    lines = [f"HTTP/1.1 {status} Test"]
    lines.extend(f"{key}: {value}" for key, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class _WireFile(io.RawIOBase):
    """Socket-file reference with controlled byte reads and explicit ownership."""

    def __init__(self, owner):
        """Keep the fake socket alive independently of connection.close.

        Args:
            owner: Fake socket owning the incoming bytes.

        Returns:
            None.
        """
        super().__init__()
        self.owner = owner

    def readinto(self, buffer):
        """Supply at most the requested raw bytes, optionally advancing a clock.

        Args:
            buffer: Writable raw read buffer.

        Returns:
            Number of bytes copied, or an injected transport exception.
        """
        if self.owner.read_error:
            raise self.owner.read_error
        if self.owner.before_read:
            self.owner.before_read(self.owner)
        count = min(len(buffer), self.owner.max_read, len(self.owner.wire) - self.owner.offset)
        buffer[:count] = self.owner.wire[self.owner.offset:self.owner.offset + count]
        self.owner.offset += count
        return count


class _FakeSocket:
    """No socket syscalls: record numeric connect, request, timeouts and fd refs."""

    def __init__(self, wire=None):
        """Initialize a socket with a deterministic in-memory response.

        Args:
            wire: Complete HTTP response, or None for a small default image.

        Returns:
            None.
        """
        self.wire = _wire() if wire is None else wire
        self.offset = 0
        self.max_read = 8192
        self.before_read = None
        self.read_error = None
        self.connect_error = None
        self.send_error = None
        self.makefile_error = None
        self.connected = []
        self.timeouts = []
        self.sent = bytearray()
        self.files = []
        self.close_calls = 0

    def settimeout(self, timeout):
        """Record a timeout, including after connection.close while a file owns fd.

        Args:
            timeout: Finite remaining budget set by the downloader.

        Returns:
            None.
        """
        assert timeout > 0
        self.timeouts.append(timeout)

    def connect(self, sockaddr):
        """Reject nonnumeric destinations and record the exact validated IP.

        Args:
            sockaddr: IPv4/IPv6 numeric socket address tuple.

        Returns:
            None, or raises an injected connect error.
        """
        ipaddress.ip_address(sockaddr[0])
        self.connected.append(sockaddr)
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        """Capture an HTTP request without sending any packets.

        Args:
            data: Request bytes.

        Returns:
            None, or raises an injected send error.
        """
        if self.send_error:
            raise self.send_error
        self.sent.extend(data)

    def makefile(self, mode, buffering=0):
        """Create an independently closeable socket-file reference.

        Args:
            mode: Expected binary read mode.
            buffering: Expected zero for unbuffered raw I/O.

        Returns:
            A fake raw socket file.
        """
        assert mode == "rb" and buffering == 0
        if self.makefile_error:
            raise self.makefile_error
        raw = _WireFile(self)
        self.files.append(raw)
        return raw

    def close(self):
        """Record closure of the socket reference, leaving existing file refs valid.

        Args:
            None.

        Returns:
            None.
        """
        self.close_calls += 1


class _Clock:
    """Controllable monotonic clock for no-sleep slowloris tests."""

    def __init__(self, body_only=False):
        """Initialize the synthetic monotonic time.

        Args:
            body_only: Advance only after the HTTP header terminator.

        Returns:
            None.
        """
        self.now = 100.0
        self.body_only = body_only

    def monotonic(self):
        """Return simulated time without sleeping.

        Args:
            None.

        Returns:
            Current synthetic monotonic seconds.
        """
        return self.now

    def drip(self, sock):
        """Advance one second per selected raw socket read.

        Args:
            sock: Fake socket currently being read.

        Returns:
            None.
        """
        if not self.body_only or sock.offset >= sock.wire.index(b"\r\n\r\n") + 4:
            self.now += 1


class ImageFetchTests(unittest.TestCase):
    """Use real HTTP parsing over exclusively mocked transports."""

    def setUp(self):
        """Isolate configuration and fail any unplanned network entry point.

        Args:
            None.

        Returns:
            None.
        """
        self.config = {"proxy": None, "allow_private_image_urls": False, "max_image_bytes": 32}
        self._patch("gemini_web2api.image_fetch.CONFIG", new=self.config)
        self.log = self._patch("gemini_web2api.image_fetch.log")
        self.dns = self._patch("socket.getaddrinfo", return_value=[_record(_PUBLIC_V4)])
        self.factory = self._patch("socket.socket", side_effect=AssertionError("unexpected socket"))
        self.unpinned = self._patch("socket.create_connection", side_effect=AssertionError("unbound connect"))
        self.urlopen = self._patch("urllib.request.urlopen", side_effect=AssertionError("urllib bypass"))

    def _patch(self, target, **kwargs):
        """Start a patch with guaranteed per-test restoration.

        Args:
            target: Dotted patch target.
            **kwargs: unittest.mock.patch options.

        Returns:
            Started mock or replacement.
        """
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _serve(self, wire=None):
        """Make the next direct socket use an in-memory response.

        Args:
            wire: Complete response bytes, or None for the default response.

        Returns:
            Fake socket returned by socket.socket.
        """
        sock = _FakeSocket(wire)
        self.factory.side_effect = None
        self.factory.return_value = sock
        return sock

    def _assert_closed(self, sock):
        """Verify both connection and response file references were released.

        Args:
            sock: Fake socket used by a completed/failed download.

        Returns:
            None; asserts there are no remaining file references.
        """
        self.assertGreater(sock.close_calls, 0)
        self.assertTrue(all(raw.closed for raw in sock.files))

    def _mock_response(self, headers=(), chunks=(b"image", b"")):
        """Replace just HTTPResponse to inspect logical read caps and cleanup.

        Args:
            headers: Header name/value sequence.
            chunks: Read results and/or exceptions.

        Returns:
            Mock response returned by HTTPConnection.getresponse.
        """
        message = Message()
        for key, value in headers:
            message[key] = value
        response = mock.Mock(status=200, headers=message)
        response.read.side_effect = chunks
        self._patch("http.client.HTTPConnection.getresponse", return_value=response)
        return response

    def test_dns_rebinding_connects_only_first_validated_result(self):
        """Pin a public result even when a second lookup would return loopback.

        Args:
            None.

        Returns:
            None.
        """
        self.dns.side_effect = [[_record(_PUBLIC_V4)], [_record("127.0.0.1")]]
        sock = self._serve()
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a.png?sig=a%2Fb#ignored"), b"image")
        self.dns.assert_called_once_with("images.example", 80, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                         socket.IPPROTO_TCP)
        self.assertEqual(sock.connected, [(_PUBLIC_V4, 80)])
        self.assertIn(b"GET /a.png?sig=a%2Fb HTTP/1.1\r\n", sock.sent)
        self.assertIn(b"Host: images.example\r\n", sock.sent)
        self.assertNotIn(b"ignored", sock.sent)
        self.assertNotIn(b"Cookie:", sock.sent)
        self.assertNotIn(b"Authorization:", sock.sent)
        self.assertIs(socket.getaddrinfo, self.dns)
        self.unpinned.assert_not_called()
        self.urlopen.assert_not_called()
        self._assert_closed(sock)

    def test_https_preserves_sni_and_verified_original_domain(self):
        """Use original IDNA authority for TLS and Host, never the chosen IP.

        Args:
            None.

        Returns:
            None.
        """
        sock = self._serve()
        context = ssl.create_default_context()
        wrap = mock.Mock(return_value=sock)
        context.wrap_socket = wrap
        factory = self._patch("ssl.create_default_context", return_value=context)
        self.assertEqual(image_fetch.fetch_image_bytes("https://täst.example:8443/图.png"), b"image")
        factory.assert_called_once_with()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        wrap.assert_called_once_with(sock, server_hostname="xn--tst-qla.example")
        self.assertEqual(sock.connected, [(_PUBLIC_V4, 8443)])
        self.assertIn(b"Host: xn--tst-qla.example:8443\r\n", sock.sent)
        self.assertIn(b"GET /%E5%9B%BE.png ", sock.sent)
        self.assertEqual(self.dns.call_count, 1)
        self._assert_closed(sock)

    def test_ipv6_dns_and_literal_urls(self):
        """Support numeric IPv6 connects and bracketed HTTP authorities.

        Args:
            None.

        Returns:
            None.
        """
        for url, expected_host, lookups in (("http://images.example/a", "images.example", 1),
                                           (f"http://[{_PUBLIC_V6}]:8080/a", f"[{_PUBLIC_V6}]:8080", 0)):
            with self.subTest(url=url):
                self.dns.reset_mock()
                self.dns.return_value = [_record(_PUBLIC_V6)]
                sock = self._serve()
                self.assertEqual(image_fetch.fetch_image_bytes(url), b"image")
                port = 8080 if lookups == 0 else 80
                self.assertEqual(sock.connected, [(_PUBLIC_V6, port, 0, 0)])
                self.factory.assert_called_with(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                self.assertIn(f"Host: {expected_host}\r\n".encode(), sock.sent)
                self.assertEqual(self.dns.call_count, lookups)
                self._assert_closed(sock)

    def test_dual_stack_fallback_never_resolves_again(self):
        """Retry only the already-validated IP list and close failed sockets.

        Args:
            None.

        Returns:
            None.
        """
        self.dns.return_value = [_record(_PUBLIC_V6), _record(_PUBLIC_V4)]
        failed, success = _FakeSocket(), _FakeSocket()
        failed.connect_error = OSError("offline unreachable")
        self.factory.side_effect = [failed, success]
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"image")
        self.assertEqual(failed.connected, [(_PUBLIC_V6, 80, 0, 0)])
        self.assertEqual(success.connected, [(_PUBLIC_V4, 80)])
        self.assertEqual(self.dns.call_count, 1)
        self._assert_closed(failed)
        self._assert_closed(success)

    def test_entire_dns_answer_is_validated_before_any_connect(self):
        """Reject mixed public/private or metadata DNS answers before dialing.

        Args:
            None.

        Returns:
            None.
        """
        for private in ("127.0.0.1", "10.0.0.1", "::1", "100.64.0.1", "224.0.0.1", "168.63.129.16"):
            with self.subTest(private=private):
                self.dns.return_value = [_record(_PUBLIC_V4), _record(private)]
                self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.factory.assert_not_called()

    def test_non_global_multicast_metadata_and_transition_literals_are_denied(self):
        """Cover special IPv4/IPv6 ranges, embedded addresses and metadata IPs.

        Args:
            None.

        Returns:
            None.
        """
        denied = ("0.0.0.0", "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.1.2",
                  "169.254.169.254", "100.100.100.200", "100.64.0.1", "198.18.0.1", "192.0.2.1",
                  "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255", "168.63.129.16",
                  "192.80.8.124", "192.0.0.192", "::", "::1", "fc00::1", "fe80::1", "ff02::1",
                  "2001:db8::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "fd00:ec2::254",
                  "64:ff9b::a9fe:a9fe", "64:ff9b:1::a9fe:a9fe", "2002:7f00:1::1", "100::1")
        for ip in denied:
            with self.subTest(ip=ip):
                host = f"[{ip}]" if ":" in ip else ip
                self.assertEqual(image_fetch.fetch_image_bytes(f"http://{host}/a"), b"")
        self.dns.assert_not_called()
        self.factory.assert_not_called()

    def test_localhost_and_metadata_names_are_rejected_before_dns(self):
        """Do not consult DNS for known local/metadata authorities.

        Args:
            None.

        Returns:
            None.
        """
        for host in ("localhost", "LOCALHOST.", "a.localhost", "localhost.localdomain",
                     "metadata.google.internal", "metadata.google.internal.", "instance-data.ec2.internal"):
            self.assertEqual(image_fetch.fetch_image_bytes(f"http://{host}/a"), b"")
        self.dns.assert_not_called()
        self.factory.assert_not_called()

    def test_legacy_numeric_spellings_are_classified_after_one_resolution(self):
        """Prevent octal, short, hex and integer IPv4 URL bypasses.

        Args:
            None.

        Returns:
            None.
        """
        self.dns.return_value = [_record("127.0.0.1")]
        for host in ("127.1", "0177.0.0.1", "2130706433", "0x7f000001", "127.0.0.1."):
            with self.subTest(host=host):
                self.dns.reset_mock()
                self.assertEqual(image_fetch.fetch_image_bytes(f"http://{host}/a"), b"")
                self.assertEqual(self.dns.call_count, 1)
        self.factory.assert_not_called()

    def test_explicit_private_opt_in_supports_ipv4_ipv6_and_dns(self):
        """Retain intentional internal unicast access without weakening pinning.

        Args:
            None.

        Returns:
            None.
        """
        self.config["allow_private_image_urls"] = True
        self.dns.return_value = [_record("127.0.0.1")]
        for host, ip in (("localhost", "127.0.0.1"), ("10.0.0.8", "10.0.0.8"), ("[::1]", "::1")):
            with self.subTest(host=host):
                sock = self._serve()
                self.assertEqual(image_fetch.fetch_image_bytes(f"http://{host}/a"), b"image")
                self.assertEqual(sock.connected[0][0], ip)
                self._assert_closed(sock)

    def test_truthy_nonboolean_values_do_not_enable_private_urls(self):
        """Require a real boolean true, not string truthiness or numeric one.

        Args:
            None.

        Returns:
            None.
        """
        for value in (False, None, "false", "true", 1):
            self.config["allow_private_image_urls"] = value
            self.assertEqual(image_fetch.fetch_image_bytes("http://127.0.0.1/a"), b"")
        self.factory.assert_not_called()

    def test_metadata_and_multicast_stay_denied_with_private_opt_in(self):
        """Keep hard-denied non-image endpoints blocked even for internal access.

        Args:
            None.

        Returns:
            None.
        """
        self.config["allow_private_image_urls"] = True
        for host in ("169.254.169.254", "168.63.129.16", "224.0.0.1", "[ff02::1]", "[::]",
                     "metadata.google.internal", "[::ffff:169.254.169.254]", "[64:ff9b::a9fe:a9fe]"):
            self.assertEqual(image_fetch.fetch_image_bytes(f"http://{host}/a"), b"")
        self.factory.assert_not_called()

    def test_invalid_urls_do_not_resolve_or_connect(self):
        """Reject non-HTTP schemes, userinfo, bad ports, scopes and control text.

        Args:
            None.

        Returns:
            None.
        """
        urls = ("file:///tmp/image", "gopher://images.example/x", "ftp://images.example/x", "data:image/png,abc",
                "http:///x", "http://[broken/x", "http://images.example:0/x", "http://images.example:65536/x",
                "http://images.example:bad/x", "http://user:password@images.example/x", "http://@images.example/x",
                "http://images.example/\r\nHost: other", "http://[fe80::1%25eth0]/x", "http://%31%32%37.0.0.1/x",
                "http://images.example\\evil/x", None, "")
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(image_fetch.fetch_image_bytes(url), b"")
        self.dns.assert_not_called()
        self.factory.assert_not_called()

    def test_configured_proxies_fail_closed_before_dns_even_with_opt_in(self):
        """Never ask a proxy to resolve a previously validated image hostname.

        Args:
            None.

        Returns:
            None.
        """
        for proxy in ("http://proxy.invalid:3128", "https://proxy.invalid", "socks5h://proxy.invalid:1080"):
            for private in (False, True):
                self.config.update(proxy=proxy, allow_private_image_urls=private)
                self.assertEqual(image_fetch.fetch_image_bytes("https://images.example/a"), b"")
        self.dns.assert_not_called()
        self.factory.assert_not_called()

    def test_environment_proxies_cannot_change_pinned_direct_transport(self):
        """Explicitly ignore environment proxy discovery and NO_PROXY behavior.

        Args:
            None.

        Returns:
            None.
        """
        sock = self._serve()
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid", "HTTPS_PROXY": "http://proxy.invalid",
                                         "ALL_PROXY": "socks5h://proxy.invalid", "NO_PROXY": ""}):
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"image")
        self.assertEqual(sock.connected, [(_PUBLIC_V4, 80)])
        self.urlopen.assert_not_called()
        self._assert_closed(sock)

    def test_redirects_never_follow_location_and_close_response(self):
        """Reject every redirect class, including public and private locations.

        Args:
            None.

        Returns:
            None.
        """
        for status in (300, 301, 302, 303, 304, 305, 307, 308):
            with self.subTest(status=status):
                self.dns.reset_mock()
                sock = self._serve(_wire(headers=(("Location", "http://127.0.0.1/private"),), status=status))
                self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
                self.assertEqual(self.dns.call_count, 1)
                self.assertEqual(len(sock.connected), 1)
                self._assert_closed(sock)

    def test_success_closes_response_for_keepalive_and_connection_close(self):
        """Release both socket and file refs regardless of server persistence.

        Args:
            None.

        Returns:
            None.
        """
        for persistence in ("keep-alive", "close"):
            sock = self._serve(_wire(headers=(("Content-Length", "5"), ("Connection", persistence))))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"image")
            self.assertEqual(len(sock.files), 1)
            self._assert_closed(sock)

    def test_http_errors_close_response(self):
        """Close non-success responses without returning error pages as images.

        Args:
            None.

        Returns:
            None.
        """
        for status in (400, 403, 404, 500, 503):
            sock = self._serve(_wire(status=status))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)

    def test_oversized_or_malformed_content_length_never_reads_body(self):
        """Reject negative, enormous, nonnumeric, folded or duplicate lengths.

        Args:
            None.

        Returns:
            None.
        """
        bad_values = ("-1", "+5", "1.0", "1e6", "", " ", "5, 5", "9" * 10000, "33", "0x10", "١", "5\r\n 5")
        for value in bad_values:
            with self.subTest(value=value[:30]):
                sock = self._serve()
                response = self._mock_response((("Content-Length", value),))
                self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
                response.read.assert_not_called()
                response.close.assert_called_once_with()
                self._assert_closed(sock)
        response = self._mock_response((("Content-Length", "5"), ("Content-Length", "5")))
        self._serve()
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        response.read.assert_not_called()
        response.close.assert_called_once_with()

    def test_missing_and_zero_content_lengths_are_valid(self):
        """Allow absent length and valid nonnegative HTTP framing.

        Args:
            None.

        Returns:
            None.
        """
        for body, headers in ((b"image", ()), (b"", (("Content-Length", "0"),)),
                              (b"image", (("Content-Length", " 0005\t"),))):
            sock = self._serve(_wire(body, headers))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), body)
            self._assert_closed(sock)

    def test_body_cap_reads_only_remaining_budget_plus_one(self):
        """Use bounded reads and reject overflow even with no declared length.

        Args:
            None.

        Returns:
            None.
        """
        self.config["max_image_bytes"] = 4
        sock = self._serve()
        response = self._mock_response(chunks=(b"ab", b"cde"))
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.assertEqual(response.read.call_args_list, [mock.call(5), mock.call(3)])
        response.close.assert_called_once_with()
        self._assert_closed(sock)

    def test_body_cap_handles_chunked_and_close_delimited_wire_responses(self):
        """Enforce the same body limit for both unknown-length framing forms.

        Args:
            None.

        Returns:
            None.
        """
        self.config["max_image_bytes"] = 4
        responses = (_wire(b"abcde"), _wire(b"5\r\nabcde\r\n0\r\n\r\n", (("Transfer-Encoding", "chunked"),)))
        for wire in responses:
            sock = self._serve(wire)
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)
        sock = self._serve(_wire(b"4\r\nabcd\r\n0\r\n\r\n", (("Transfer-Encoding", "chunked"),)))
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"abcd")
        self._assert_closed(sock)

    def test_negative_chunk_length_cannot_trigger_unbounded_read(self):
        """Reject the stdlib negative-chunk read(-1) pitfall before body reads.

        Args:
            None.

        Returns:
            None.
        """
        sock = self._serve(_wire(b"-1\r\n" + b"x" * 100, (("Transfer-Encoding", "chunked"),)))
        sock.max_read = 1
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.assertEqual(sock.offset, sock.wire.index(b"-1\r\n") + 4)
        self._assert_closed(sock)

    def test_conflicting_transfer_framing_is_rejected(self):
        """Reject Transfer-Encoding plus length and unsupported transfer codings.

        Args:
            None.

        Returns:
            None.
        """
        for headers in ((("Transfer-Encoding", "chunked"), ("Content-Length", "5")),
                        (("Transfer-Encoding", "gzip"),), (("Transfer-Encoding", "chunked, gzip"),)):
            sock = self._serve(_wire(headers=headers))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)

    def test_truncated_bodies_do_not_return_partial_images(self):
        """Reject early EOF for fixed-size and chunked image bodies.

        Args:
            None.

        Returns:
            None.
        """
        for wire in (_wire(b"abc", (("Content-Length", "5"),)),
                     _wire(b"5\r\nabc", (("Transfer-Encoding", "chunked"),))):
            sock = self._serve(wire)
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)

    def test_failed_reads_and_response_close_errors_release_connection(self):
        """Close responses and connections on read/cleanup exceptions.

        Args:
            None.

        Returns:
            None.
        """
        for error in (OSError("offline read error"), TimeoutError("offline read timeout")):
            sock = self._serve()
            response = self._mock_response(chunks=(b"a", error))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            response.close.assert_called_once_with()
            self._assert_closed(sock)
        sock = self._serve()
        response = self._mock_response()
        response.close.side_effect = OSError("offline close error")
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self._assert_closed(sock)

    def test_transport_and_header_errors_release_socket_files(self):
        """Handle connect, write, makefile, parser and raw read failures safely.

        Args:
            None.

        Returns:
            None.
        """
        for attribute in ("connect_error", "send_error", "makefile_error", "read_error"):
            sock = self._serve()
            setattr(sock, attribute, OSError("offline injected error"))
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)
        sock = self._serve(b"not HTTP\r\n\r\n")
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self._assert_closed(sock)

    def test_tls_verification_failure_closes_without_plaintext_retry(self):
        """Never disable certificates or downgrade to HTTP after TLS failure.

        Args:
            None.

        Returns:
            None.
        """
        sock = self._serve()
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("offline hostname mismatch")
        self._patch("ssl.create_default_context", return_value=context)
        self.assertEqual(image_fetch.fetch_image_bytes("https://images.example/a"), b"")
        self.assertEqual(sock.sent, b"")
        self.assertEqual(self.factory.call_count, 1)
        self._assert_closed(sock)

    def test_invalid_configuration_never_disables_the_byte_cap(self):
        """Use finite defaults and a hard upper bound for operator byte limits.

        Args:
            None.

        Returns:
            None.
        """
        self._patch("gemini_web2api.image_fetch._DEFAULT_MAX_BYTES", new=4)
        self._patch("gemini_web2api.image_fetch._HARD_MAX_BYTES", new=8)
        for value in (None, 0, -1, "broken", float("inf"), float("nan")):
            self.config["max_image_bytes"] = value
            sock = self._serve()
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
            self._assert_closed(sock)
        self.config["max_image_bytes"] = 10 ** 100
        self.assertEqual(image_fetch._byte_limit(), 8)

    def test_absolute_deadline_stops_slow_headers_and_body(self):
        """Simulate byte-dripping without sleeping or using any real sockets.

        Args:
            None.

        Returns:
            None.
        """
        for body_only in (False, True):
            for chunked in (False, True):
                with self.subTest(body_only=body_only, chunked=chunked):
                    clock = _Clock(body_only)
                    wire = (_wire(b"5\r\nimage\r\n0\r\n\r\n", (("Transfer-Encoding", "chunked"),))
                            if chunked else _wire())
                    sock = self._serve(wire)
                    sock.max_read, sock.before_read = 1, clock.drip
                    with mock.patch("time.monotonic", clock.monotonic), mock.patch.object(image_fetch, "_FETCH_TIMEOUT", 3):
                        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
                    self.assertLessEqual(clock.now, 103)
                    self.assertTrue(all(0 < timeout <= 3 for timeout in sock.timeouts))
                    self._assert_closed(sock)

    def test_dns_timeout_late_result_never_connects(self):
        """Simulate a timed-out libc lookup and then deliver its late result.

        Args:
            None.

        Returns:
            None.
        """
        slots = threading.BoundedSemaphore(1)
        result = mock.Mock()
        result.get.side_effect = queue.Empty
        self._patch("gemini_web2api.image_fetch._DNS_SLOTS", new=slots)
        self._patch("queue.Queue", return_value=result)
        thread_factory = self._patch("threading.Thread")
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.assertFalse(slots.acquire(blocking=False))
        worker = thread_factory.call_args.kwargs
        self.assertTrue(worker["daemon"])
        worker["target"](*worker["args"])
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        self.dns.assert_called_once()
        self.factory.assert_not_called()

    def test_dns_capacity_and_resolution_errors_fail_closed(self):
        """Bound resolver workers and never connect after failure or empty answers.

        Args:
            None.

        Returns:
            None.
        """
        slots = mock.Mock()
        slots.acquire.return_value = False
        with mock.patch.object(image_fetch, "_DNS_SLOTS", slots):
            self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.dns.assert_not_called()
        self.dns.side_effect = socket.gaierror("offline DNS failure")
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.dns.side_effect = None
        self.dns.return_value = []
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.factory.assert_not_called()

    def test_https_over_ipv6_keeps_domain_sni(self):
        """Keep TLS identity independent from IPv6 transport address selection.

        Args:
            None.

        Returns:
            None.
        """
        self.dns.return_value = [_record(_PUBLIC_V6)]
        sock = self._serve()
        context = mock.Mock()
        context.wrap_socket.return_value = sock
        self._patch("ssl.create_default_context", return_value=context)
        self.assertEqual(image_fetch.fetch_image_bytes("https://images.example/a"), b"image")
        context.wrap_socket.assert_called_once_with(sock, server_hostname="images.example")
        self.assertEqual(sock.connected, [(_PUBLIC_V6, 443, 0, 0)])
        self.assertEqual(self.dns.call_count, 1)
        self._assert_closed(sock)

    def test_dns_worker_creation_failure_returns_capacity(self):
        """Do not leak resolver slots when a daemon thread cannot be created.

        Args:
            None.

        Returns:
            None.
        """
        slots = threading.BoundedSemaphore(1)
        self._patch("gemini_web2api.image_fetch._DNS_SLOTS", new=slots)
        self._patch("threading.Thread", side_effect=RuntimeError("offline thread limit"))
        self.assertEqual(image_fetch.fetch_image_bytes("http://images.example/a"), b"")
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        self.dns.assert_not_called()
        self.factory.assert_not_called()

    def test_multimodal_public_api_is_compatible_without_loading_credentials(self):
        """Keep the established multimodal.fetch_image_bytes import and signature.

        Args:
            None.

        Returns:
            None.
        """
        from gemini_web2api import multimodal
        self.assertIs(multimodal.fetch_image_bytes, image_fetch.fetch_image_bytes)
        sock = self._serve()
        with mock.patch.object(multimodal, "load_cookie", side_effect=AssertionError("must not read credentials")) as cookie:
            self.assertEqual(multimodal.fetch_image_bytes("http://images.example/a"), b"image")
        cookie.assert_not_called()
        self._assert_closed(sock)


if __name__ == "__main__":
    unittest.main()
