"""HTTP server core: routing, auth, SSE plumbing, threaded server.

BaseAPIHandler owns transport-level concerns (request-body reading incl.
chunked encoding, JSON/SSE responses, CORS, API-key auth, routing). Protocol
implementations live in the mixin modules and are combined into
GeminiHandler at the bottom of this file.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .. import __version__
from ..config import CONFIG
from ..logs import log
from ..models import MODELS
from ..upstream import pick_next_cookie
from .google import GoogleGenerateMixin
from .openai_chat import OpenAIChatMixin
from .openai_responses import OpenAIResponsesMixin


class BaseAPIHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive so clients (browser extensions, proxies) reuse TCP
    # connections instead of paying a handshake per request. SSE responses
    # opt out via "Connection: close" (no Content-Length can be known).
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 120  # close idle keep-alive connections

    def log_message(self, fmt, *args):
        # Health probes (GET / and favicon) poll every few minutes from
        # monitors; logging each one just dilutes the business signal.
        if self.command == "GET" and self.path in ("/", "/healthz", "/favicon.ico"):
            return
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Stream end is only signalled by connection close; never reuse.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def _parse_body(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_text = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

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
            if path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            # One Google account (cookie slot) per incoming request, round-robin.
            pick_next_cookie()
            if self.path.startswith("/v1") and not self._authorized():
                # Request body is unread; keep-alive would desync the connection.
                self.close_connection = True
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            body = self._read_request_body()
            if self.path == "/v1/chat/completions":
                self._handle_chat(body)
            elif self.path == "/v1/responses":
                self._handle_responses(body)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass


class GeminiHandler(OpenAIChatMixin, OpenAIResponsesMixin, GoogleGenerateMixin,
                    BaseAPIHandler):
    """Full API handler: core routing plus every protocol mixin."""


class ThreadedServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server with daemon worker threads."""

    daemon_threads = True
    allow_reuse_address = True
