"""Address-pinned, credential-free image downloads (no upstream Google calls).

A single resolver result is validated in full, then TCP connects to those numeric
sockaddrs directly. HTTP never resolves the host again. HTTPS uses the original
IDNA hostname for SNI and certificate verification, not the chosen IP. Redirects
and ambiguous HTTP framing are rejected; no cookies or authorization are sent.

Proxy policy: CONFIG.proxy makes image downloads fail closed, even with private
URLs enabled. Sending a hostname to an HTTP/SOCKS proxy would lose address pinning.
Environment proxy variables are deliberately ignored; they never select a proxy.
The explicit boolean allow_private_image_urls=True allows ordinary private and
loopback unicast destinations, but not multicast, unspecified addresses, known
metadata endpoints or transition/translation IPv6 ranges. Scoped IPv6 URLs are
unsupported. Network-specific NAT64 prefixes and privileged network routing are
outside application-level IP validation; use an egress firewall as defense in depth.

Bounds: max_image_bytes defaults to 20 MiB for missing/invalid/nonpositive values
and is capped at 100 MiB. There is a 30-second overall deadline, including DNS
queueing, TCP/TLS, response headers and body. Each raw socket read uses the remaining
budget, so slow headers/chunk framing cannot reset the deadline. libc DNS cannot be
cancelled portably: at most eight daemon resolvers may outlive timed-out callers;
their late results never initiate a connection. HTTP parser buffering is bounded
separately from the image-body cap. Content encoding is not decompressed.
"""

import http.client
import io
import ipaddress
import queue
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from typing import NamedTuple
from urllib.parse import quote, urlsplit

from .config import CONFIG
from .budget import RequestControlError, check_budget, remaining_timeout
from .logs import log

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_HARD_MAX_BYTES = 100 * 1024 * 1024
_FETCH_TIMEOUT = 30.0
_DNS_SLOTS = threading.BoundedSemaphore(8)
_METADATA_IPS = frozenset(ipaddress.ip_address(value) for value in (
    "169.254.169.254", "169.254.170.2", "169.254.170.23", "100.100.100.200",
    "192.0.0.192", "168.63.129.16", "192.80.8.124", "fd00:ec2::254",
))
_METADATA_HOSTS = frozenset((
    "metadata", "metadata.google", "metadata.google.internal",
    "instance-data", "instance-data.ec2.internal",
))
_TRANSITION_V6 = tuple(ipaddress.ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
))
# Conservative across Python versions whose is_global special-use tables differ.
_SPECIAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "192.0.0.0/24", "192.88.99.0/24", "2001::/23",
))


class _Target(NamedTuple):
    """Immutable HTTP authority and request target, separate from TCP endpoints."""

    host: str
    port: int
    secure: bool
    path: str
    authority: str


def _remaining(deadline):
    """Return a strictly positive remaining overall time budget.

    Args:
        deadline: Absolute time.monotonic deadline.

    Returns:
        Remaining seconds; raises TimeoutError when the deadline has expired.
    """
    check_budget("image download")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("image download deadline exceeded")
    return remaining


def _parse_target(url):
    """Parse a credential-free HTTP(S) target without doing DNS.

    Args:
        url: Absolute URL provided by the caller.

    Returns:
        An immutable _Target; raises ValueError for unsafe/unsupported syntax.
    """
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("invalid image URL characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("image URL must use HTTP(S) with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("image URL credentials are not supported")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if any(char in host for char in "%\\/#?@"):
        raise ValueError("scoped or ambiguous image host is not supported")
    if host.rstrip(".") in _METADATA_HOSTS:
        raise ValueError("metadata image host is forbidden")
    secure = parsed.scheme == "https"
    port = parsed.port if parsed.port is not None else (443 if secure else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid image URL port")
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if secure else 80):
        authority += f":{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    return _Target(host, port, secure, path, authority)


def _address_allowed(address, allow_private):
    """Classify numeric destinations, including IPv4 embedded in IPv6.

    Args:
        address: Parsed IPv4Address or IPv6Address.
        allow_private: Explicit operator opt-in for ordinary internal unicast.

    Returns:
        True only when the address satisfies the image egress policy.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _address_allowed(address.ipv4_mapped, allow_private)
        if any(address in network for network in _TRANSITION_V6):
            return False
    if address.is_multicast or address.is_unspecified or address in _METADATA_IPS:
        return False
    if allow_private:
        return True
    return (address.is_global and not address.is_reserved and not address.is_loopback
            and not address.is_link_local
            and not any(address in network for network in _SPECIAL_NETWORKS))


def _dns_worker(host, port, result, slots):
    """Resolve once in a bounded daemon worker, never connecting to the result.

    Args:
        host: Original normalized hostname.
        port: Numeric destination port.
        result: One-item result queue owned by this request.
        slots: Semaphore whose slot this worker owns.

    Returns:
        None; posts either resolver records or the resolver exception.
    """
    try:
        records = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        result.put_nowait((True, records))
    except Exception as exc:
        result.put_nowait((False, exc))
    finally:
        slots.release()


def _lookup_once(host, port, deadline):
    """Bound resolver queueing/waiting by the same download deadline.

    Args:
        host: Original normalized hostname to resolve exactly once.
        port: Destination port.
        deadline: Absolute overall monotonic deadline.

    Returns:
        getaddrinfo records, or raises on resolution failure/timeout.
    """
    slots = _DNS_SLOTS
    if not slots.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("image DNS capacity deadline exceeded")
    try:
        result = queue.Queue(maxsize=1)
        worker = threading.Thread(target=_dns_worker, args=(host, port, result, slots), daemon=True)
        worker.start()
    except BaseException:
        slots.release()
        raise
    try:
        succeeded, value = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("image DNS lookup deadline exceeded") from None
    _remaining(deadline)
    if not succeeded:
        raise value
    return value


def _resolve_addresses(target, allow_private, deadline):
    """Validate all answers before returning immutable numeric TCP endpoints.

    Args:
        target: Parsed URL authority.
        allow_private: Explicit internal-unicast opt-in.
        deadline: Absolute overall monotonic deadline.

    Returns:
        Tuple of (address-family, numeric-sockaddr) pairs; no hostname survives.
    """
    host = target.host.rstrip(".")
    if not allow_private and (host in ("localhost", "localhost.localdomain") or host.endswith(".localhost")):
        raise ValueError("private image host is forbidden")
    try:
        literal = ipaddress.ip_address(target.host)
    except ValueError:
        records = _lookup_once(target.host, target.port, deadline)
    else:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        records = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(literal), target.port))]
    endpoints = []
    for family, socktype, protocol, _canonical, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM:
            raise ValueError("unsupported image address family")
        if protocol not in (0, socket.IPPROTO_TCP) or "%" in sockaddr[0]:
            raise ValueError("unsupported image address protocol or scope")
        address = ipaddress.ip_address(sockaddr[0])
        if (family == socket.AF_INET6) != (address.version == 6):
            raise ValueError("inconsistent image address family")
        if not _address_allowed(address, allow_private):
            raise ValueError("private, non-global or metadata image address is forbidden")
        # Rebuild from the parsed IP, not unchecked sockaddr text or DNS port data.
        numeric = (str(address), target.port, 0, 0) if address.version == 6 else (str(address), target.port)
        endpoint = (family, numeric)
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        raise ValueError("image host resolved to no usable addresses")
    _remaining(deadline)
    return tuple(endpoints)


def _connect_pinned(target, endpoints, deadline):
    """Dial only validated numeric IPs and preserve the TLS server identity.

    Args:
        target: Original HTTP authority, including the TLS hostname.
        endpoints: Fully validated numeric address-family/sockaddr pairs.
        deadline: Absolute overall monotonic deadline.

    Returns:
        Connected socket (TLS-wrapped for HTTPS), owned by the caller.
    """
    context = ssl.create_default_context() if target.secure else None
    if context is not None:
        context.set_alpn_protocols(["http/1.1"])
    last_error = None
    for family, sockaddr in endpoints:
        _remaining(deadline)
        sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(sockaddr)  # Numeric IP only: never socket.create_connection(host).
            if context is not None:
                sock.settimeout(_remaining(deadline))
                sock = context.wrap_socket(sock, server_hostname=target.host)
            _remaining(deadline)
            return sock
        except BaseException as exc:
            sock.close()
            if not isinstance(exc, OSError) or isinstance(exc, ssl.SSLError):
                raise
            last_error = exc
    raise last_error or OSError("image host has no reachable address")


class _DeadlineReader(io.RawIOBase):
    """Unbuffered socket-file adapter enforcing the deadline on every raw read."""

    def __init__(self, raw, sock, deadline):
        """Own a socket file while retaining its socket for timeouts.

        Args:
            raw: Unbuffered socket.makefile result (retains the socket fd).
            sock: Socket shared with that file.
            deadline: Absolute overall monotonic deadline.

        Returns:
            None.
        """
        super().__init__()
        self._raw = raw
        self._sock = sock
        self._deadline = deadline

    def readable(self):
        """Advertise raw read support to BufferedReader.

        Args:
            None.

        Returns:
            True.
        """
        return True

    def readinto(self, buffer):
        """Perform one raw read without resetting the overall deadline.

        Args:
            buffer: Writable buffer supplied by BufferedReader.

        Returns:
            Number of bytes read, or raises on timeout/transport failure.
        """
        self._sock.settimeout(_remaining(self._deadline))
        count = self._raw.readinto(buffer)
        _remaining(self._deadline)
        return count

    def close(self):
        """Close the underlying socket-file reference, including error paths.

        Args:
            None.

        Returns:
            None.
        """
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Minimal http.client socket interface around an already-pinned socket."""

    def __init__(self, sock, deadline):
        """Wrap the connected socket without any ability to resolve a host.

        Args:
            sock: Already-connected plain/TLS socket.
            deadline: Absolute overall monotonic deadline.

        Returns:
            None.
        """
        self._sock = sock
        self._deadline = deadline

    def sendall(self, data):
        """Send a request with a deadline-limited socket write.

        Args:
            data: HTTP request bytes supplied by http.client.

        Returns:
            None.
        """
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(data)
        _remaining(self._deadline)

    def makefile(self, mode):
        """Create a bounded-buffer reader with real socket reference ownership.

        Args:
            mode: Binary read mode requested by HTTPResponse.

        Returns:
            Buffered deadline-aware reader; response.close releases its fd ref.
        """
        raw = self._sock.makefile(mode, buffering=0)
        try:
            return io.BufferedReader(_DeadlineReader(raw, self._sock, self._deadline))
        except BaseException:
            raw.close()
            raise

    def close(self):
        """Release the connection's socket reference (response may still own one).

        Args:
            None.

        Returns:
            None.
        """
        self._sock.close()


class _ImageHTTPResponse(http.client.HTTPResponse):
    """HTTP parser with negative chunk lengths rejected before any body read."""

    def _read_next_chunk_size(self):
        """Prevent negative sizes from becoming an unbounded file.read(-1).

        Args:
            None.

        Returns:
            Nonnegative chunk length; raises ValueError for negative framing.
        """
        size = super()._read_next_chunk_size()
        if size < 0:
            raise ValueError("negative image chunk size")
        return size


@contextmanager
def _open_image_url(url, deadline, allow_private):
    """Open one pinned HTTP request; close both response and socket on every exit.

    Args:
        url: Candidate HTTP(S) image URL.
        deadline: Absolute overall monotonic deadline.
        allow_private: Explicit internal-unicast opt-in.

    Returns:
        Context manager yielding HTTPResponse; redirects are errors, never followed.
    """
    if CONFIG.get("proxy"):
        raise ValueError("image downloads disabled while CONFIG.proxy is configured (DNS pinning)")
    target = _parse_target(url)
    endpoints = _resolve_addresses(target, allow_private, deadline)
    connection = http.client.HTTPConnection(target.host, target.port, timeout=_remaining(deadline))
    # Even if http.client attempts a reconnect, it must fail rather than resolve.
    connection.auto_open = 0
    connection.response_class = _ImageHTTPResponse
    response = None
    try:
        connection.sock = _DeadlineSocket(_connect_pinned(target, endpoints, deadline), deadline)
        connection.request("GET", target.path, headers={
            "Host": target.authority,
            "User-Agent": "Mozilla/5.0",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ValueError(f"image HTTP status {response.status}; redirects are disabled")
        yield response
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def _byte_limit():
    """Convert configuration to an always-finite image body cap.

    Args:
        None.

    Returns:
        Positive byte limit, using a safe default and a 100 MiB hard ceiling.
    """
    try:
        value = int(CONFIG.get("max_image_bytes") or _DEFAULT_MAX_BYTES)
    except (TypeError, ValueError, OverflowError):
        value = _DEFAULT_MAX_BYTES
    return min(value, _HARD_MAX_BYTES) if value > 0 else _DEFAULT_MAX_BYTES


def _content_length(response, max_bytes):
    """Validate HTTP length/framing instead of trusting urllib's permissive parse.

    Args:
        response: HTTPResponse whose complete headers are available.
        max_bytes: Maximum acceptable image body length.

    Returns:
        Declared nonnegative length, or None when no Content-Length is present.
    """
    lengths = response.headers.get_all("Content-Length", [])
    encodings = response.headers.get_all("Transfer-Encoding", [])
    if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != "chunked" or lengths):
        raise ValueError("ambiguous or unsupported image transfer framing")
    if not lengths:
        return None
    if len(lengths) != 1:
        raise ValueError("duplicate image Content-Length")
    value = lengths[0].strip(" \t")
    if not value or len(value) > 20 or any(char not in "0123456789" for char in value):
        raise ValueError("invalid image Content-Length")
    length = int(value)
    if length > max_bytes:
        raise ValueError("oversized image Content-Length")
    return length


def fetch_image_bytes(url: str) -> bytes:
    """Fetch a bounded image using validated-address pinning and strict TLS.

    Args:
        url: Absolute HTTP(S) image URL, without embedded credentials.

    Returns:
        Raw body bytes on success; b"" on policy rejection or download failure.
    """
    try:
        deadline = time.monotonic() + remaining_timeout(_FETCH_TIMEOUT, "image download")
        max_bytes = _byte_limit()
        allow_private = CONFIG.get("allow_private_image_urls") is True
        with _open_image_url(url, deadline, allow_private) as response:
            declared = _content_length(response, max_bytes)
            body = bytearray()
            while True:
                _remaining(deadline)
                chunk = response.read(min(65536, max_bytes - len(body) + 1))
                _remaining(deadline)
                if not chunk:
                    break
                if len(body) + len(chunk) > max_bytes:
                    raise ValueError("oversized image body")
                body.extend(chunk)
            if declared is not None and len(body) != declared:
                raise ValueError("truncated image body")
            return bytes(body)
    except RequestControlError:
        raise
    except Exception as exc:
        check_budget("image download")
        # Never include the URL/query (potential credentials) or response body.
        log(f"Image fetch failed: {type(exc).__name__}")
        return b""
