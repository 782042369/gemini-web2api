"""HTTP server: OpenAI-compatible API endpoints."""
import json
import time
import uuid
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from .config import CONFIG
from .models import MODELS, resolve_model
from .gemini import generate, generate_stream, log, pick_next_cookie
from .tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt, parse_google_function_calls
from .multimodal import detect_image_mime, fetch_image_bytes, upload_image
from . import __version__


def _usage(prompt: str, text: str) -> dict:
    p = len(prompt) // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class _MicroBatcher:
    """Transparent micro-batching for short single-segment requests.

    Companion plugins (e.g. Peiduwa) fire bursts of independent one-segment
    translation requests whose source cannot be changed. This batcher
    collects requests arriving within a short window (microbatch_sec) and
    runs them as ONE numbered upstream call, then hands each waiting HTTP
    handler its own [n] slice. Any slice the model drops falls back to a
    direct per-segment upstream call, so worst-case behavior equals the
    unbatched path. Segments are bucketed by upstream params so a batch
    never mixes models or options.
    """

    def __init__(self, window: float, max_segments: int, single_wait: float = 0.12):
        """Create a batcher.

        Args:
            window: seconds to wait after the first arrival before dispatch.
            max_segments: dispatch early once this many segments queued.
            single_wait: loner fast-path threshold - a window holding only
                ONE segment this long dispatches it immediately (direct
                path), so isolated requests never pay the full window.
        """
        self.window = window
        self.max_segments = max_segments
        self.single_wait = single_wait
        self._cv = threading.Condition()
        self._pending = []
        self._worker = None
        self._worker_lock = threading.Lock()

    def _ensure_worker(self):
        """Start the single dispatcher thread on first use (idempotent).

        Args:
            None.

        Returns:
            None.
        """
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._dispatch_loop,
                                                daemon=True, name="microbatch")
                self._worker.start()

    def submit(self, item: dict) -> str:
        """Queue one segment and block until its result is ready.

        Args:
            item: {"key": hashable upstream-param bucket, "prompt": str,
                   "params": (model_id, think_mode, extra_fields) tuple,
                   "runner": callable(prompts:list) -> aligned results list}.

        Returns:
            Result text for this segment. Raises the batch error on failure.
        """
        self._ensure_worker()
        ev = threading.Event()
        holder = {"event": ev, "result": None, "error": None}
        entry = dict(item)
        entry["holder"] = holder
        with self._cv:
            self._pending.append(entry)
            self._cv.notify_all()
        ev.wait(timeout=CONFIG["request_timeout_sec"] + self.window + 15)
        if holder["error"] is not None:
            raise holder["error"]
        if holder["result"] is None:
            raise RuntimeError("microbatch: result missing after wait")
        return holder["result"]

    def _dispatch_loop(self):
        """Dispatcher: gather a window of segments, then run them batched.

        Args:
            None.

        Returns:
            None (loops forever; daemon thread).
        """
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                batch_start = time.time()
                deadline = batch_start + self.window
                while self._pending and len(self._pending) < self.max_segments:
                    now = time.time()
                    if now >= deadline:
                        break
                    # Loner fast-path: a lone segment with no company after
                    # single_wait dispatches immediately via the direct path,
                    # so low-traffic requests skip the full window cost.
                    if len(self._pending) == 1 and now - batch_start >= self.single_wait:
                        break
                    self._cv.wait(max(0.01, deadline - now))
                batch, self._pending = self._pending, []
            self._run_batch(batch)

    def _run_batch(self, batch: list) -> None:
        """Execute one gathered batch, grouped by upstream-param bucket.

        Args:
            batch: list of pending entries from the dispatch window.

        Returns:
            None (fills each entry's holder and signals its event).
        """
        buckets = {}
        for entry in batch:
            buckets.setdefault(entry["key"], []).append(entry)
        for entries in buckets.values():
            prompts = [e["prompt"] for e in entries]
            try:
                results = entries[0]["runner"](prompts)
                if len(results) != len(prompts):
                    raise RuntimeError("microbatch: runner length mismatch")
            except Exception as e:
                for entry in entries:
                    entry["holder"]["error"] = e
                    entry["holder"]["event"].set()
                continue
            for entry, res in zip(entries, results):
                entry["holder"]["result"] = res
                entry["holder"]["event"].set()


_MICROBATCHER = _MicroBatcher(
    window=float(CONFIG.get("microbatch_sec") or 0),
    max_segments=int(CONFIG.get("microbatch_max") or 0),
    single_wait=float(CONFIG.get("microbatch_single_sec") or 0.12),
)


def _microbatch_eligible(req) -> tuple:
    """Return (instruction, prompt) when a request may join the micro-batcher.

    Eligible: non-streaming generateContent without tools, exactly one
    user content with one pure-text part, prompt short enough for numbered
    batching. A systemInstruction (typical of companion translation
    plugins like Peiduwa) is welcomed: its text becomes the batch-level
    instruction header. Everything else keeps its original path.

    Args:
        req: parsed generateContent request body (dict).

    Returns:
        (instruction, prompt) when eligible, else None. Ineligibility is
        logged (aggregated reason) so real-plugin hit rates are diagnosable.
    """
    reason = None
    if req.get("tools"):
        reason = "tools"
    sys_inst = req.get("systemInstruction") or {}
    sys_text = " ".join(
        p.get("text", "") for p in (sys_inst.get("parts") or []) if isinstance(p, dict)
    ).strip()
    contents = req.get("contents")
    if reason is None:
        if not isinstance(contents, list) or len(contents) != 1:
            reason = f"contents={len(contents) if isinstance(contents, list) else 'x'}"
    if reason is None:
        content = contents[0] or {}
        if content.get("role") not in (None, "user"):
            reason = f"role={content.get('role')}"
    if reason is None:
        parts = content.get("parts")
        if not isinstance(parts, list) or len(parts) != 1:
            reason = f"parts={len(parts) if isinstance(parts, list) else 'x'}"
    if reason is None:
        p = parts[0]
        if not isinstance(p, dict) or set(p.keys()) - {"text"}:
            reason = "non-text part"
    text = ""
    if reason is None:
        text = (p.get("text") or "").strip()
        limit = int(CONFIG.get("microbatch_max_prompt") or 0)
        if not text:
            reason = "empty"
        elif limit and len(text) > limit:
            reason = f"prompt_len={len(text)}>"
    if reason is not None:
        log(f"Microbatch skip: {reason}")
        return None
    return sys_text, text


def _microbatch_runner(model_id, think_mode, extra_fields, instruction=""):
    """Build a batch runner bound to one upstream parameter set + instruction.

    Args:
        model_id/think_mode/extra_fields: upstream generate() parameters.
        instruction: batch-level instruction (from systemInstruction); when
            non-empty it heads the packed prompt and the per-segment direct
            fallback, matching how the plugin's own requests read.

    Returns:
        callable(prompts) -> aligned result strings. Batched when >1 prompt:
        numbered packing, block-wise [n] parsing, per-segment fallback for
        any index the model dropped (fallback equals the unbatched path).
    """
    prefix = f"{instruction}\n" if instruction else ""

    def _direct(prompt):
        """Run one segment through the standard upstream path."""
        return generate(f"{prefix}{prompt}", model_id, think_mode, None, extra_fields)

    def _run(prompts):
        """Execute prompts - one upstream call when batched, else direct."""
        log(f"Microbatch dispatch: {len(prompts)} segment(s)")
        if len(prompts) == 1:
            return [_direct(prompts[0])]
        body = "\n".join(f"[{i}] {s}" for i, s in enumerate(prompts))
        packed = (
            f"{prefix}"
            f"以下 {len(prompts)} 个编号任务互相独立。逐个执行，输出要求:\n"
            "- 每个任务的结果以 [编号] 行开始，到下一个编号行为止\n"
            "- 除各任务结果外不输出任何解释或额外内容\n\n"
            f"{body}"
        )
        parsed = {}
        try:
            out = generate(packed, model_id, think_mode, None, extra_fields)
            cur, buf = None, []
            for line in (out or "").splitlines():
                m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
                if m and 0 <= int(m.group(1)) < len(prompts):
                    if cur is not None:
                        parsed[cur] = "\n".join(buf).strip()
                    cur, buf = int(m.group(1)), [m.group(2)]
                elif cur is not None:
                    buf.append(line)
            if cur is not None:
                parsed[cur] = "\n".join(buf).strip()
        except Exception as e:
            log(f"Microbatch upstream error: {e}")
        results = []
        for i, prompt in enumerate(prompts):
            if i in parsed and parsed[i]:
                results.append(parsed[i])
            else:
                results.append(_direct(prompt))
        return results

    return _run


def _extract_batch_segments(req) -> tuple:
    """Return (instruction, segments) when a generateContent request opts into batching.

    A request opts in by shape alone (no proprietary fields): exactly one
    user content whose parts are >=2 non-empty pure-text parts, plus a
    non-empty systemInstruction used as the per-segment instruction.
    Anything else (tools, images, multi-turn, single part) returns None so
    existing callers keep their current semantics untouched.

    Args:
        req: parsed generateContent request body (dict).

    Returns:
        (instruction, [segment, ...]) when batchable, else None.
    """
    if req.get("tools"):
        return None
    sys_inst = req.get("systemInstruction") or {}
    sys_text = " ".join(
        p.get("text", "") for p in (sys_inst.get("parts") or []) if isinstance(p, dict)
    ).strip()
    if not sys_text:
        return None
    contents = req.get("contents")
    if not isinstance(contents, list) or len(contents) != 1:
        return None
    content = contents[0] or {}
    if content.get("role") not in (None, "user"):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or len(parts) < 2:
        return None
    segs = []
    for p in parts:
        if not isinstance(p, dict) or set(p.keys()) - {"text"}:
            return None
        t = (p.get("text") or "").strip()
        if not t:
            return None
        segs.append(t)
    return sys_text, segs


def _upload_images(images: list) -> list:
    """Upload images and return list of file references. Returns None if no images."""
    if not images:
        return None
    file_refs = []
    for item in images:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = mime or "image/png"
        if not data:
            raise RuntimeError("image fetch failed")
        mime = detect_image_mime(data, mime or "image/png")
        try:
            ref = upload_image(data, "image.png", mime or "image/png")
            file_refs.append(ref)
        except Exception as e:
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None


class GeminiHandler(BaseHTTPRequestHandler):
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

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages", []), tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if stream and (not tools or tool_choice == "none"):
            try:
                self._start_sse()
                first_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(f"data: {json.dumps(first_chunk)}\n\n".encode())
                self.wfile.flush()
                for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                          "total_tokens": (len(prompt)+len(text or ""))//4},
            })

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("type") in ("input_text", "input_image", "image"):
                        messages.append({"role": "user", "content": [item]})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text":
                                        text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call":
                                        tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        messages.append({"role": role, "content": item.get("content", "")})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            file_refs = _upload_images(images)
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self._start_sse()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {
                    "type": event_type,
                    "sequence_number": sequence_number,
                    **fields,
                }
                self.wfile.write(
                    f"event: {event_type}\ndata: {json.dumps(event)}\n\n".encode()
                )

            usage = {
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(text or "") // 4,
                "total_tokens": (len(prompt) + len(text or "")) // 4,
            }
            base_response = {
                "id": rid,
                "object": "response",
                "created_at": int(time.time()),
                "model": model_name,
            }
            emit(
                "response.created",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            emit(
                "response.in_progress",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            for output_index, item in enumerate(output):
                if item["type"] == "function_call":
                    pending_item = {
                        "type": "function_call",
                        "id": item["id"],
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    emit(
                        "response.function_call_arguments.delta",
                        item_id=item["id"],
                        output_index=output_index,
                        delta=item["arguments"],
                    )
                    emit(
                        "response.function_call_arguments.done",
                        item_id=item["id"],
                        output_index=output_index,
                        arguments=item["arguments"],
                    )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
                elif item["type"] == "message":
                    pending_item = {
                        "type": "message",
                        "id": item["id"],
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    for content_index, content_part in enumerate(item["content"]):
                        event_fields = {
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": content_index,
                        }
                        emit(
                            "response.content_part.added",
                            **event_fields,
                            part={
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        )
                        emit(
                            "response.output_text.delta",
                            **event_fields,
                            delta=content_part["text"],
                        )
                        emit(
                            "response.output_text.done",
                            **event_fields,
                            text=content_part["text"],
                        )
                        emit(
                            "response.content_part.done",
                            **event_fields,
                            part=content_part,
                        )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
            emit(
                "response.completed",
                response={
                    **base_response,
                    "status": "completed",
                    "output": output,
                    "usage": usage,
                },
            )
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text or "")//4, "total_tokens": (len(prompt)+len(text or ""))//4}})

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _send_batch_translation(self, instruction, segments, model_name,
                                 model_id, think_mode, extra_fields):
        """Translate many segments via numbered-prompt batching and reply.

        Groups the client's parts into numbered upstream calls (25 segments
        per call), parses [n]-prefixed lines back out, and falls back to a
        per-segment upstream call for any index the model dropped. The
        response mirrors the request: one text part per input segment, in
        the original order.

        Args:
            instruction: systemInstruction text applied to every segment.
            segments: list of source-text segments (from request parts).
            model_name: resolved model name for logging and the response.
            model_id/think_mode/extra_fields: upstream generate() params.

        Returns:
            None (writes the JSON response to the client).
        """
        t0 = time.time()
        log(f"Google API batch: segments={len(segments)} model={model_name}")
        results = [None] * len(segments)
        batch_size = 25
        for start in range(0, len(segments), batch_size):
            chunk = [re.sub(r"\s*\n\s*", " ", s).strip() for s in segments[start:start + batch_size]]
            body = "\n".join(f"[{i}] {s}" for i, s in enumerate(chunk))
            prompt = (
                f"{instruction}\n"
                "将下列编号段落逐段处理，输出严格遵守:\n"
                "- 每段一行，行首为 [编号]，编号与顺序和输入完全一致\n"
                "- 不输出任何解释、注释或额外段落\n\n"
                f"{body}"
            )
            parsed = {}
            try:
                out = generate(prompt, model_id, think_mode, None, extra_fields)
                for m in re.finditer(r"^\[(\d+)\]\s*(.*)$", out or "", re.MULTILINE):
                    idx = int(m.group(1))
                    if 0 <= idx < len(chunk) and m.group(2).strip():
                        parsed[idx] = m.group(2).strip()
            except Exception as e:
                log(f"Batch upstream error: {e}")
            for i, seg in enumerate(chunk):
                if i in parsed:
                    results[start + i] = parsed[i]
                else:
                    try:
                        results[start + i] = generate(
                            f"{instruction}\n只输出结果，不要解释:\n\n{seg}",
                            model_id, think_mode, None, extra_fields)
                    except Exception as e:
                        log(f"Segment fallback error: {e}")
                        results[start + i] = seg
        joined = "".join(results)
        usage = {
            "promptTokenCount": sum(len(s) for s in segments) // 4,
            "candidatesTokenCount": len(joined) // 4,
            "totalTokenCount": (sum(len(s) for s in segments) + len(joined)) // 4,
        }
        self.send_json({
            "candidates": [{
                "content": {"parts": [{"text": t} for t in results], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }],
            "usageMetadata": usage,
            "modelVersion": model_name,
        })
        log(f"Google API batch done: {len(segments)} segments in {time.time() - t0:.1f}s")

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        # Accept both /v1beta/models/... (Gemini CLI) and /v1/models/...
        # (Google SDK style); previously /v1 paths silently used default model.
        m = re.match(r'/v1(?:beta)?/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        # Batched multi-segment path: non-streaming, tool-free requests that
        # carry >=2 pure-text parts + a systemInstruction are translated via
        # numbered-prompt batching (one upstream call per 25 segments).
        if not stream and not has_tools:
            batch = _extract_batch_segments(req)
            if batch:
                self._send_batch_translation(batch[0], batch[1], model_name,
                                             model_id, think_mode, extra_fields)
                return
            # Transparent micro-batching for single-segment bursts from
            # companion plugins that cannot be modified (e.g. Peiduwa).
            mb = _microbatch_eligible(req) if _MICROBATCHER.window > 0 else None
            if mb is not None:
                mb_instruction, mb_prompt = mb
                key = (model_id, think_mode,
                       tuple(sorted(extra_fields.items())) if extra_fields else None,
                       mb_instruction)
                try:
                    text = _MICROBATCHER.submit({
                        "key": key, "prompt": mb_prompt,
                        "runner": _microbatch_runner(model_id, think_mode,
                                                      extra_fields, mb_instruction),
                    })
                except Exception as e:
                    self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
                    return
                self.send_json({
                    "candidates": [{
                        "content": {"parts": [{"text": text or ""}], "role": "model"},
                        "finishReason": "STOP", "index": 0,
                    }],
                    "usageMetadata": {
                        "promptTokenCount": len(mb_prompt) // 4,
                        "candidatesTokenCount": len(text or "") // 4,
                        "totalTokenCount": (len(mb_prompt) + len(text or "")) // 4,
                    },
                    "modelVersion": model_name,
                })
                return
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        if stream and not has_tools:
            try:
                self._start_sse()
                full_text = ""
                for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                    if not delta:
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Google stream error: {e}")
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                for fc in function_calls:
                    response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or "I apologize, but I was unable to generate a response. Please try again."})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._start_sse()
            self.wfile.write(f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
