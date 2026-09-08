"""Test-only loopback network guard: an accidental live upstream call is a failure."""
import ipaddress
import socket
from contextlib import ExitStack, contextmanager
from unittest import mock


def _allow_host(host):
    """Allow only loopback DNS/connect targets. Args: host name/IP. Returns: None or raises."""
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host is None or host == "localhost":
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise AssertionError("test attempted non-loopback networking")


@contextmanager
def offline_network():
    """Block real DNS and socket connections outside loopback.

    Args:
        None. Individual tests may still install fake DNS/sockets inside this guard.

    Yields:
        None while external network operations are disabled.
    """
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock, address):
        """Check then connect. Args: sock, address. Returns: original connect result."""
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _allow_host(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        """Check then connect_ex. Args: sock, address. Returns: original result."""
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _allow_host(address[0])
        return connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        """Check then resolve. Args: host, resolver args. Returns: resolver records."""
        _allow_host(host)
        return getaddrinfo(host, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(mock.patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        stack.enter_context(mock.patch.object(socket, "getaddrinfo", guarded_getaddrinfo))
        # Native libcurl can bypass Python sockets. Forbid it explicitly; tests
        # needing a curl response must inject a fake session, as upstream tests do.
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            pass
        else:
            stack.enter_context(mock.patch.object(curl_requests.Session, "request",
                                side_effect=AssertionError("test attempted native libcurl networking")))
        yield
