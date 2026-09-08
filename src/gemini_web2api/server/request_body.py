"""Bounded HTTP/1.1 request body reader with strict framing and a total deadline."""
import math
import re
import time


class RequestBodyError(ValueError):
    """An invalid or incomplete HTTP entity that requires connection close."""

    status = 400
    code = "invalid_request"


class RequestBodyTooLarge(RequestBodyError):
    """The declared or decoded request body exceeds the configured cap."""

    status = 413
    code = "request_too_large"


class RequestBodyTimeout(RequestBodyError):
    """The body did not arrive within the total body-read budget."""

    status = 408
    code = "request_timeout"


class _Reader:
    """Read exactly one HTTP entity without consuming the next request."""

    def __init__(self, stream, connection, timeout):
        """Initialize a deadline. Args: stream, connection, timeout seconds. Returns: None."""
        self.stream = stream
        self.connection = connection
        self.deadline = time.monotonic() + timeout

    def read(self, size):
        """Read up to size bytes. Args: size is bounded by caller. Returns: bytes."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestBodyTimeout("request body timed out")
        self.connection.settimeout(remaining)
        try:
            # read1 performs at most one underlying read, so a trickling peer
            # cannot reset a full timeout repeatedly inside BufferedReader.read.
            result = self.stream.read1(size)
        except TimeoutError as exc:
            raise RequestBodyTimeout("request body timed out") from exc
        if time.monotonic() > self.deadline:
            raise RequestBodyTimeout("request body timed out")
        return result

    def exact(self, size):
        """Read a known length. Args: size in bytes. Returns: bytes or raises on EOF."""
        data = bytearray()
        while len(data) < size:
            chunk = self.read(min(size - len(data), 65536))
            if not chunk:
                raise RequestBodyError("truncated request body")
            data.extend(chunk)
        return data

    def line(self, limit):
        """Read a bounded CRLF line. Args: limit includes CRLF. Returns: bytes."""
        line = bytearray()
        while len(line) < limit:
            char = self.read(1)
            if not char:
                raise RequestBodyError("truncated chunked request body")
            line.extend(char)
            if char == b"\n":
                if not line.endswith(b"\r\n"):
                    raise RequestBodyError("chunk framing requires CRLF")
                return bytes(line)
        raise RequestBodyError("chunk framing line is too long")


def read_request_body(stream, connection, headers, max_bytes, timeout):
    """Read a bounded fixed-length or chunked entity with unambiguous framing.

    Args:
        stream: Buffered socket reader, exposing read1.
        connection: Socket whose timeout is temporarily set and restored.
        headers: HTTPMessage headers (including get_all).
        max_bytes: Positive decoded-entity limit.
        timeout: Positive total body-read timeout in seconds.

    Returns:
        Bytes belonging to exactly one request; raises RequestBodyError on
        malformed, oversized, incomplete, or timed-out input. Never reads EOF
        as a body delimiter and never consumes a pipelined next request.
    """
    if max_bytes <= 0 or not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("invalid HTTP body limit configuration")
    lengths = headers.get_all("Content-Length", [])
    encodings = headers.get_all("Transfer-Encoding", [])
    if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
        raise RequestBodyError("ambiguous request body framing")
    if encodings and encodings[0].strip().lower() != "chunked":
        raise RequestBodyError("unsupported Transfer-Encoding")
    length = 0
    if lengths:
        raw_length = lengths[0].strip()
        if not re.fullmatch(r"[0-9]+", raw_length) or len(raw_length) > 20:
            raise RequestBodyError("invalid Content-Length")
        length = int(raw_length)
        if length > max_bytes:
            raise RequestBodyTooLarge("request body exceeds configured limit")
    previous_timeout = connection.gettimeout()
    reader = _Reader(stream, connection, timeout)
    try:
        if not encodings:
            return bytes(reader.exact(length))
        body = bytearray()
        while True:
            line = reader.line(8192)
            size_text = line[:-2].split(b";", 1)[0]
            if not re.fullmatch(rb"[0-9a-fA-F]+", size_text) or len(size_text) > 16:
                raise RequestBodyError("invalid chunk size")
            size = int(size_text, 16)
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = reader.line(8192)
                    trailer_bytes += len(trailer)
                    if trailer_bytes > 16384:
                        raise RequestBodyError("chunk trailers are too large")
                    if trailer == b"\r\n":
                        return bytes(body)
                    name, separator, _ = trailer.partition(b":")
                    if not separator or not re.fullmatch(rb"[!#$%&'*+.^_\x60|~0-9A-Za-z-]+", name):
                        raise RequestBodyError("invalid chunk trailer")
                    if name.lower() in (b"content-length", b"transfer-encoding", b"host", b"authorization"):
                        raise RequestBodyError("forbidden chunk trailer")
            if size > max_bytes - len(body):
                raise RequestBodyTooLarge("request body exceeds configured limit")
            body.extend(reader.exact(size))
            if reader.exact(2) != b"\r\n":
                raise RequestBodyError("invalid chunk terminator")
    finally:
        connection.settimeout(previous_timeout)
