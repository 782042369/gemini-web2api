"""/v1/chat/completions endpoint (OpenAI chat completions protocol)."""
import json
import time
import uuid

from ..config import CONFIG
from ..logs import log
from ..models import resolve_model
from ..tools import messages_to_prompt, parse_tool_calls
from ..upstream import generate, generate_stream
from ..upstream.parser import extract_response_text
from ..vision_bridge import vision_bridge_enabled, vision_generate
from .images import _upload_images


class OpenAIChatMixin:
    """Handler methods for the /v1/chat/completions endpoint."""


    def _chat_via_vision_bridge(self, prompt, images, model_name, cid, stream):
        """Serve one image request through the CDP browser bridge.

        Args:
            prompt: user prompt text.
            images: list of (image_bytes, mime) tuples.
            model_name: requested model name for the response envelope.
            cid: chatcmpl id.
            stream: whether the client asked for SSE.

        Returns:
            None; writes one complete (or single-chunk SSE) response.
        """
        try:
            sg_raw = vision_generate(prompt, images)
            text = extract_response_text(sg_raw)
        except Exception as e:
            self._send_upstream_error(e, code="vision_bridge_failed")
            return
        if not text:
            self._send_upstream_error("vision bridge produced empty text", code="vision_bridge_failed")
            return
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

        # Vision bridge: image-bearing requests are executed inside the
        # logged-in Gemini tab of the CDP browser (the only environment the
        # upstream still hands a valid XSRF token to). Falls through to the
        # direct chain when the bridge is not configured.
        if images and vision_bridge_enabled():
            self._chat_via_vision_bridge(prompt, images, model_name, cid, stream)
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
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
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
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
        except Exception as e:
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
