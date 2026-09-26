"""/v1/chat/completions endpoint (OpenAI chat completions protocol)."""
import json
import time
import uuid

from ..budget import RequestControlError, check_budget
from ..config import CONFIG
from ..logs import log
from ..models import resolve_model
from ..multimodal import note_direct_vision_outcome, vision_direct_available, vision_direct_ready
from ..tools import messages_to_prompt, parse_tool_calls
from ..upstream import generate, generate_stream
from ..upstream.parser import extract_response_text
from ..vision_bridge import (
    VisionBridgeError,
    fetch_page_tokens,
    vision_bridge_enabled,
    vision_generate,
)
from .images import _normalize_images, _upload_images


class OpenAIChatMixin:
    """Handler methods for the /v1/chat/completions endpoint."""


    def _vision_mode(self) -> str:
        """Resolve the effective vision routing mode.

        Args:
            None.

        Returns:
            One of "auto", "bridge" or "direct". Without a configured
            bridge endpoint every mode degrades to "direct" (the bridge
            legs are unreachable anyway).
        """
        mode = CONFIG.get("vision_mode") or "auto"
        if mode not in ("auto", "bridge", "direct"):
            mode = "auto"
        if mode != "direct" and not vision_bridge_enabled():
            return "direct"
        return mode

    def _chat_via_vision_bridge(self, prompt, images, model_name, cid, stream,
                                 model_id=None, think_mode=None):
        """Serve one image request through the CDP browser bridge.

        Args:
            prompt: user prompt text.
            images: list of (image_bytes_or_url, mime) tuples from the
                protocol layer; URL entries are downloaded here (the
                in-page chain cannot fetch arbitrary hosts).
            model_name: requested model name for the response envelope.
            cid: chatcmpl id.
            stream: whether the client asked for SSE.
            model_id: MODE_CATEGORY id forwarded into the page payload.
            think_mode: thinking level forwarded into the page payload.

        Returns:
            (True, None) when a response was written; (False, error)
            otherwise - the caller decides between a direct-chain rescue
            attempt and surfacing the error to the client.
        """
        # Pre-flight: a wedged or logged-out tab would otherwise hang the
        # full chain timeout. Reading the page tokens is cheap and
        # wedge-tolerant; no at means the browser needs a Google re-login.
        try:
            if not fetch_page_tokens().get("at"):
                return False, VisionBridgeError(
                    "vision bridge tab is not logged in (no SNlM0e in page) - "
                    "re-login the Google account on the CDP browser desktop")
        except RequestControlError:
            raise
        except Exception:
            check_budget('vision preflight')
        try:
            # Global byte cap: the hybrid bridge chain uploads server-side
            # (no 4 MiB CDP limit); vision_generate re-fits to the bridge
            # budget itself when it falls back to the in-page upload.
            prepared = _normalize_images(images)
            sg_raw = vision_generate(prompt, prepared,
                                     model_id=model_id, think_mode=think_mode)
            text = extract_response_text(sg_raw)
        except RequestControlError:
            raise
        except Exception as e:
            check_budget('vision generation')
            return False, e
        if not text:
            return False, VisionBridgeError("vision bridge produced empty text")
        msg = {"role": "assistant", "content": text}
        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                  "finish_reason": "stop"}]}
            try:
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4,
                          "total_tokens": (len(prompt) + len(text)) // 4},
            })
        return True, None

    def _send_bridge_failure(self, error, code="vision_bridge_failed"):
        """Surface one bridge error to the client.

        Args:
            error: exception or message describing the failure.
            code: machine-readable error code for the response envelope.

        Returns:
            None; writes one error response.
        """
        code = ("vision_bridge_not_logged_in"
                if isinstance(error, VisionBridgeError) and "not logged in" in str(error)
                else code)
        self._send_upstream_error(error, code=code)

    def _serve_direct_vision(self, prompt, images, model_name, cid, stream,
                             model_id, think_mode, extra_fields, tools,
                             tool_choice, rescue_error=None):
        """Serve one vision request through the direct (server-side) chain.

        Used as the rescue leg when the CDP bridge fails while borrowed
        page tokens are still live: uploads proceed server-side and the
        generation runs with streaming disabled (a single SSE chunk when
        the client asked to stream), mirroring the bridge envelope.

        Args:
            prompt: user prompt text.
            images: normalized (bytes, mime) image tuples.
            model_name: requested model name for the response envelope.
            cid: chatcmpl id.
            stream: whether the client asked for SSE.
            model_id: MODE_CATEGORY id for generate().
            think_mode: thinking level for generate().
            extra_fields: optional inner-payload overrides.
            tools: tool definitions from the request (unused here - the
                rescue path serves plain text).
            tool_choice: tool choice from the request (see tools).
            rescue_error: the bridge error that led here, for logging.

        Returns:
            None; writes one complete (or single-chunk SSE) response.
        """
        try:
            file_refs = _upload_images(images)
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
            note_direct_vision_outcome(True)
        except Exception as e:
            note_direct_vision_outcome(False)
            log(f"direct-chain rescue failed too ({type(e).__name__}: {e})")
            self._send_upstream_error(e, code="vision_all_chains_failed")
            return
        if not text:
            self._send_upstream_error("direct chain produced empty text",
                                      code="vision_all_chains_failed")
            return
        msg = {"role": "assistant", "content": text}
        if stream:
            self._start_sse()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name,
                     "choices": [{"index": 0, "delta": msg, "finish_reason": "stop"}]}
            try:
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4,
                          "total_tokens": (len(prompt) + len(text)) // 4},
            })

    def _handle_chat(self, body: bytes):
        """Handle Chat input. Args: body contains JSON bytes. Returns: None; writes one response."""
        req = self._parse_body(body)
        if req is None:
            self.send_error_json("request body must be a JSON object", 400, param="body")
            return
        if not self._validate_request(req, "chat"):
            return
        requested_model = req.get("model", CONFIG["default_model"])
        if not isinstance(requested_model, str) or not requested_model.strip():
            self.send_error_json("model must be a non-empty string", 400, param="model")
            return
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages or any(not isinstance(item, dict) for item in messages):
            self.send_error_json("messages must be a non-empty array of objects", 400, param="messages")
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(requested_model)
        if err:
            self.send_error_json(err, 400, param="model")
            return

        tools = req.get("tools")
        if tools is not None and not isinstance(tools, list):
            self.send_error_json("tools must be an array", 400, param="tools")
            return
        tool_choice = req.get("tool_choice", "auto")
        try:
            prompt, images = messages_to_prompt(messages, tools, tool_choice)
        except (TypeError, ValueError, KeyError) as e:
            self.send_error_json(f"invalid messages: {e}", 400, param="messages")
            return
        if not prompt.strip():
            self.send_error_json("empty prompt", 400, param="messages")
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        # Vision routing (CONFIG["vision_mode"]): "bridge" runs the chain
        # inside the logged-in CDP tab (with a direct-chain rescue when the
        # tab fails but borrowed tokens are still live); "auto" prefers the
        # direct chain whenever a live token set (at + push_id, borrowed
        # from the CDP page when needed) is available and circuit-breaker
        # healthy, using the bridge otherwise and as an upload/generate
        # fallback; "direct" never uses the bridge.
        mode = self._vision_mode()
        bridge_kwargs = dict(model_id=model_id, think_mode=think_mode)

        def _bridge_or_error():
            """Run the bridge chain, or surface its failure. Args: none.

            Returns:
                None; writes the full response (success or error).
            """
            ok, error = self._chat_via_vision_bridge(
                prompt, images, model_name, cid, stream, **bridge_kwargs)
            if ok:
                return
            if vision_direct_ready():
                log(f"vision bridge failed ({error}); attempting direct-chain rescue")
                self._serve_direct_vision(prompt, images, model_name, cid, stream,
                                          model_id, think_mode, extra_fields, tools,
                                          tool_choice, rescue_error=error)
                return
            self._send_bridge_failure(error)

        if images and mode == "bridge":
            _bridge_or_error()
            return
        if images and mode == "auto" and not vision_direct_available():
            _bridge_or_error()
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            note_direct_vision_outcome(False)
            if images and mode == "auto":
                log(f"direct vision upload failed ({e}); falling back to CDP bridge")
                ok, error = self._chat_via_vision_bridge(
                    prompt, images, model_name, cid, stream, **bridge_kwargs)
                if not ok:
                    self._send_bridge_failure(error)
                return
            self._send_upstream_error(e, code="image_upload_failed")
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
                if images:
                    note_direct_vision_outcome(True)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                if images:
                    note_direct_vision_outcome(False)
                log(f"Stream error: {type(e).__name__}: {e}")
                try:
                    if self._resp_status is None:
                        self._send_upstream_error(e)
                    else:
                        self._write_stream_error("chat", error=e)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
            if images:
                note_direct_vision_outcome(True)
        except Exception as e:
            if images:
                note_direct_vision_outcome(False)
            # Non-stream requests can still be rescued through the CDP
            # bridge when the direct generation rejects the attachment.
            if images and mode == "auto":
                log(f"direct vision generate failed ({type(e).__name__}: {e}); "
                    "falling back to CDP bridge")
                ok, error = self._chat_via_vision_bridge(
                    prompt, images, model_name, cid, stream, **bridge_kwargs)
                if not ok:
                    self._send_bridge_failure(error)
                return
            self._send_upstream_error(e, code="upstream_error")
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            allowed_names = set()
            for tool in tools:
                if isinstance(tool, dict):
                    fn = tool.get("function", tool)
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                        allowed_names.add(fn["name"])
            if isinstance(tool_choice, dict):
                allowed_names &= {tool_choice.get("function", tool_choice)["name"]}
            text, tool_calls = parse_tool_calls(text, allowed_names)
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
