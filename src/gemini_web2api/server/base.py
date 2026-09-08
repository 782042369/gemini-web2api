"""HTTP server core: routing, auth, SSE plumbing, threaded server.

BaseAPIHandler owns transport-level concerns (request-body reading incl.
chunked encoding, JSON/SSE responses, CORS, API-key auth, routing). Protocol
implementations live in the mixin modules and are combined into
GeminiHandler at the bottom of this file.
"""
import json
import re
import time
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .. import __version__
from ..config import CONFIG
from ..budget import RequestBudget, RequestControlError, budget_scope, check_budget, remaining_timeout
from ..logs import get_request_id, log, set_request_id
from ..models import MODELS
from ..upstream import pick_next_cookie
from ..validation import (RequestValidationError, validate_chat_request,
                          validate_responses_request, validate_google_request)
from .google import GoogleGenerateMixin
from .openai_chat import OpenAIChatMixin
from .openai_responses import OpenAIResponsesMixin

from .request_body import RequestBodyError, read_request_body
from .writer import BudgetWriter


class BaseAPIHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive so clients (browser extensions, proxies) reuse TCP
    # connections instead of paying a handshake per request. SSE responses
    # opt out via "Connection: close" (no Content-Length can be known).
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 120  # close idle keep-alive connections

    def setup(self):
        """Wrap the actual downstream socket writer. Args: None. Returns: None."""
        super().setup()
        self._terminal_delivery = None
        self.wfile = BudgetWriter(self.wfile, self.connection, lambda: self._terminal_delivery)

    @contextmanager
    def _response_delivery(self, terminal=False):
        """Permit short best-effort error delivery. Args: terminal flag. Yields: None."""
        previous = self._terminal_delivery
        self._terminal_delivery = min(previous, time.monotonic() + 1) if terminal and previous is not None else (time.monotonic() + 1 if terminal else None)
        try:
            yield
        finally:
            self._terminal_delivery = previous

    def log_message(self, fmt, *args):
        # POST access lines are emitted at request end by do_POST (with
        # duration, status and request id); suppress the start-of-request
        # default line to avoid duplicates.
        if self.command == "POST":
            return
        # Health probes (GET / and favicon) poll every few minutes from
        # monitors; logging each one just dilutes the business signal.
        if self.command == "GET" and self.path in ("/", "/healthz", "/favicon.ico"):
            return
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def _begin_request(self):
        """Bind a correlation id to this worker thread and note the start.

        Honors an inbound x-request-id header (proxies/gateways may set
        one); otherwise generates a 12-hex id. The id is echoed on every
        response and appended to all log lines from this thread.

        Args:
            None.

        Returns:
            The request start timestamp (time.time()).
        """
        rid = (self.headers.get("x-request-id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", rid):
            rid = uuid.uuid4().hex[:12]
        set_request_id(rid)
        self._resp_status = None
        return time.time()

    def _access_line(self, t_start) -> None:
        """Emit the end-of-request access line with duration and status.

        Args:
            t_start: request start timestamp from _begin_request().

        Returns:
            None. Status falls back to 500 when no response was sent.
        """
        client_ip = self.client_address[0] if self.client_address else "-"
        status = getattr(self, "_resp_status", None) or 500
        path = self.path.split("?", 1)[0]
        log(f'{client_ip} "{self.command} {path} HTTP/1.1" {status} '
            f"{time.time() - t_start:.2f}s")

    def send_json(self, data, status=200):
        """Serialize and send one JSON response with common API headers.

        Args:
            data: JSON-serializable response value.
            status: HTTP status code.

        Returns:
            None.
        """
        with self._response_delivery(status >= 400):
            if status < 400:
                check_budget("JSON response")
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._resp_status = status
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            if get_request_id():
                self.send_header("x-request-id", get_request_id())
            self.end_headers()
            self.wfile.write(body)

    def send_error_json(self, message, status=400, error_type="invalid_request_error",
                        param=None, code=None):
        """Send an OpenAI-compatible structured error object.

        Args:
            message: Human-readable error message.
            status: HTTP status code.
            error_type: Stable error category.
            param: Optional request field associated with the error.
            code: Optional machine-readable error code.

        Returns:
            None.
        """
        error = {"message": str(message), "type": error_type}
        if param is not None:
            error["param"] = param
        if code is not None:
            error["code"] = code
        self.send_json({"error": error}, status)

    def _start_sse(self):
        """Open an unbuffered SSE response. Args: None. Returns: None."""
        check_budget("stream headers")
        self._resp_status = 200
        self.send_response(200)
        # Keep the legacy media type exact for OpenAI/Gemini clients that
        # compare it literally; UTF-8 is the protocol default for SSE bytes.
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Vary", "Origin")
        if get_request_id():
            self.send_header("x-request-id", get_request_id())
        # Stream end is only signalled by connection close; never reuse.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def _write_sse(self, payload, event=None):
        """Write and flush one SSE payload after headers were sent.

        Args:
            payload: JSON-serializable payload.
            event: Optional SSE event name for Responses-style streams.

        Returns:
            None.
        """
        encoded = json.dumps(payload, ensure_ascii=False)
        prefix = f"event: {event}\n" if event else ""
        self.wfile.write(f"{prefix}data: {encoded}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_stream_error(self, protocol, message="stream failed", error=None):
        """Terminate an already-open stream with a stable client error.

        Args:
            protocol: chat or google; Responses owns its sequenced terminal event.
            message: Safe public error message; no upstream exception text.
            error: Optional typed timeout/cancellation failure.

        Returns:
            None; broken client connections are allowed to propagate.
        """
        status = error.status if isinstance(error, RequestControlError) else 502
        code = error.code if isinstance(error, RequestControlError) else "stream_error"
        message = str(error) if isinstance(error, RequestControlError) else message
        payload = {"message": message, "type": "server_error", "code": code}
        with self._response_delivery(True):
            if protocol == "chat":
                self._write_sse({"error": payload})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                state = "DEADLINE_EXCEEDED" if status == 504 else "UNAVAILABLE"
                self._write_sse({"error": {"message": message, "code": status, "status": state}})

    def _parse_body(self, body: bytes) -> dict:
        """Parse a JSON object request body, rejecting scalar/array JSON.

        Args:
            body: Raw request bytes.

        Returns:
            A JSON object, or ``None`` for malformed/non-object input.
        """
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _validate_request(self, req, protocol):
        """Validate consumed request fields before side effects.

        Args:
            req: Parsed JSON object.
            protocol: chat, responses, or google.

        Returns:
            True on success; otherwise sends a field-specific 400 and returns False.
        """
        validators = {"chat": validate_chat_request, "responses": validate_responses_request,
                      "google": validate_google_request}
        try:
            validators[protocol](req)
        except RequestValidationError as exc:
            self.send_error_json(str(exc), 400, param=exc.param, code="invalid_request")
            return False
        return True

    def _read_request_body(self) -> bytes:
        """Read one bounded entity. Args: None. Returns: body bytes or raises a framing error."""
        return read_request_body(self.rfile, self.connection, self.headers,
                                 int(CONFIG["max_request_body_bytes"]),
                                 remaining_timeout(float(CONFIG["request_body_timeout_sec"]), "request body"))

    def _send_upstream_error(self, error, code="upstream_error"):
        """Preserve terminal budget errors instead of flattening them into 502.

        Args:
            error: Operation failure.
            code: Existing code for non-budget upstream failures.

        Returns:
            None; writes a structured error with the proper HTTP status.
        """
        if isinstance(error, RequestControlError):
            self.send_error_json(str(error), error.status, "server_error", code=error.code)
        else:
            self.send_error_json(f"upstream error: {error}", 502, "upstream_error", code=code)

    def _authorized(self):
        """Validate Bearer, API-key headers, or the native query key.

        Returns:
            True when authentication is disabled or a configured key matches.
        """
        keys = {str(key) for key in (CONFIG.get("api_keys") or []) if key}
        if not keys:
            return True
        auth = self.headers.get("Authorization", "").strip()
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip() in keys:
            return True
        for header in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(header, "").strip() in keys:
                return True
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
        return any(value in keys for value in query.get("key", []))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        # Let browsers cache the preflight instead of re-sending OPTIONS per request.
        self.send_header("Access-Control-Max-Age", "7200")
        self.end_headers()

    def do_HEAD(self):
        # HEAD-based monitors must see 2xx, not 501 Not Implemented.
        path = self.path.split("?", 1)[0]
        self.send_response(200 if path == "/" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            # Strip query strings before routing: monitors often append
            # cache-busters (/?t=123) which broke exact-match paths.
            path = self.path.split("?", 1)[0]
            is_api_path = path.startswith("/v1/") or path.startswith("/v1beta/")
            if is_api_path and not self._authorized():
                self.send_error_json("invalid api key", 401, "authentication_error", code="invalid_api_key")
                return
            if path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path == "/v1beta/models":
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys())})
            else:
                self.send_error_json("not found", 404, "invalid_request_error", code="not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        t_start = self._begin_request()
        request_budget = RequestBudget()
        try:
            with budget_scope(request_budget):
                # One Google account (cookie slot) per incoming request, round-robin.
                pick_next_cookie()
                path = urlsplit(self.path).path
                is_api_path = path.startswith("/v1/") or path.startswith("/v1beta/")
                if is_api_path and not self._authorized():
                    # Request body is unread; keep-alive would desync the connection.
                    self.close_connection = True
                    self.send_error_json("invalid api key", 401, "authentication_error", code="invalid_api_key")
                    return
                body = self._read_request_body()
                if path == "/v1/chat/completions":
                    self._handle_chat(body)
                elif path == "/v1/responses":
                    self._handle_responses(body)
                elif re.fullmatch(r"/v1(?:beta)?/models/[^/:?]+:streamGenerateContent", path):
                    self._handle_google_generate(body, stream=True)
                elif re.fullmatch(r"/v1(?:beta)?/models/[^/:?]+:generateContent", path):
                    self._handle_google_generate(body, stream=False)
                else:
                    self.send_error_json("not found", 404, "invalid_request_error", code="not_found")
        except RequestControlError as e:
            self.close_connection = True
            if self._resp_status is None:
                self._send_upstream_error(e)
        except RequestBodyError as e:
            self.close_connection = True
            try:
                request_budget.check("request body")
            except RequestControlError as control:
                self._send_upstream_error(control)
            else:
                self.send_error_json(str(e), e.status, "invalid_request_error", code=e.code)
        except (BrokenPipeError, ConnectionResetError):
            request_budget.cancel()
        except Exception as e:
            log(f"POST error: {type(e).__name__}: {e}")
            self.close_connection = True
            if self._resp_status is None:
                try:
                    self.send_error_json("internal server error", 500, "server_error", code="internal_error")
                except (BrokenPipeError, ConnectionResetError):
                    pass
        finally:
            request_budget.cancel()
            self._access_line(t_start)
            set_request_id(None)


class GeminiHandler(OpenAIChatMixin, OpenAIResponsesMixin, GoogleGenerateMixin,
                    BaseAPIHandler):
    """Full API handler: core routing plus every protocol mixin."""


class ThreadedServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server with daemon worker threads."""

    daemon_threads = True
    allow_reuse_address = True
