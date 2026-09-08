"""/v1/responses endpoint (OpenAI Responses API, Codex CLI protocol)."""
import json
import time
import uuid

from ..config import CONFIG
from ..budget import RequestControlError
from ..models import resolve_model
from ..logs import log
from ..tools import messages_to_prompt, parse_tool_calls
from ..upstream import generate, generate_stream
from .images import _upload_images


class OpenAIResponsesMixin:
    """Handler methods for the /v1/responses endpoint."""

    def _stream_text_response(self, model_name, model_id, think_mode, extra_fields,
                              prompt, images):
        """Stream text responses without waiting for complete generation.

        Args:
            model_name: Public model identifier.
            model_id: Gemini mode identifier.
            think_mode: Gemini thinking level.
            extra_fields: Optional upstream payload overrides.
            prompt: Flattened user prompt.
            images: Parsed image inputs.

        Returns:
            None; writes a Responses API SSE stream.
        """
        try:
            file_refs = _upload_images(images)
        except Exception as exc:
            self._send_upstream_error(exc, code="image_upload_failed")
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        item_id = f"msg_{uuid.uuid4().hex[:12]}"
        sequence_number = 0
        text = ""
        base_response = {"id": rid, "object": "response", "created_at": int(time.time()),
                         "model": model_name}

        self._start_sse()

        def emit(event_type, **fields):
            """Write one sequenced Responses SSE event."""
            nonlocal sequence_number
            sequence_number += 1
            event = {"type": event_type, "sequence_number": sequence_number, **fields}
            self.wfile.write(
                f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
            )
            self.wfile.flush()

        emit("response.created", response={**base_response, "status": "in_progress",
                                             "output": [], "usage": None})
        emit("response.in_progress", response={**base_response, "status": "in_progress",
                                                "output": [], "usage": None})
        pending_item = {"type": "message", "id": item_id, "role": "assistant",
                        "status": "in_progress", "content": []}
        emit("response.output_item.added", output_index=0, item=pending_item)
        emit("response.content_part.added", item_id=item_id, output_index=0, content_index=0,
             part={"type": "output_text", "text": "", "annotations": []})
        try:
            for delta in generate_stream(prompt, model_id, think_mode, file_refs, extra_fields):
                if not delta:
                    continue
                text += delta
                emit("response.output_text.delta", item_id=item_id, output_index=0,
                     content_index=0, delta=delta)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log(f"Responses stream error: {type(exc).__name__}: {exc}")
            failure = {"message": str(exc) if isinstance(exc, RequestControlError) else "stream failed",
                       "type": "server_error",
                       "code": exc.code if isinstance(exc, RequestControlError) else "stream_error"}
            with self._response_delivery(True):
                emit("response.failed", response={**base_response, "status": "failed", "output": [], "error": failure})
            return

        content = {"type": "output_text", "text": text, "annotations": []}
        output_item = {"type": "message", "id": item_id, "role": "assistant",
                       "status": "completed", "content": [content]}
        emit("response.output_text.done", item_id=item_id, output_index=0, content_index=0, text=text)
        emit("response.content_part.done", item_id=item_id, output_index=0, content_index=0, part=content)
        emit("response.output_item.done", output_index=0, item=output_item)
        usage = {"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4,
                 "total_tokens": (len(prompt) + len(text)) // 4}
        emit("response.completed", response={**base_response, "status": "completed",
                                              "output": [output_item], "usage": usage})


    def _handle_responses(self, body: bytes):
        """Handle Responses input. Args: body contains JSON bytes. Returns: None; writes one response."""
        req = self._parse_body(body)
        if req is None:
            self.send_error_json("request body must be a JSON object", 400, param="body")
            return
        if not self._validate_request(req, "responses"):
            return
        requested_model = req.get("model", CONFIG["default_model"])
        if not isinstance(requested_model, str) or not requested_model.strip():
            self.send_error_json("model must be a non-empty string", 400, param="model")
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(requested_model)
        if err:
            self.send_error_json(err, 400, param="model")
            return

        input_items = req.get("input", [])
        if not isinstance(input_items, (str, list)):
            self.send_error_json("input must be a string or array", 400, param="input")
            return
        tools = req.get("tools")
        if tools is not None and not isinstance(tools, list):
            self.send_error_json("tools must be an array", 400, param="tools")
            return
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
                    if item.get("type") == "function_call":
                        messages.append({"role": "assistant", "content": None, "tool_calls": [{
                            "id": item["call_id"], "type": "function",
                            "function": {"name": item["name"], "arguments": item.get("arguments", "{}")},
                        }]})
                    elif item.get("type") == "function_call_output":
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
            if any(not isinstance(tool, dict) for tool in tools):
                self.send_error_json("tools must contain objects", 400, param="tools")
                return
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        try:
            prompt, images = messages_to_prompt(messages, tools, tool_choice)
        except (TypeError, ValueError, KeyError) as e:
            self.send_error_json(f"invalid input: {e}", 400, param="input")
            return
        if not prompt.strip():
            self.send_error_json("empty input", 400, param="input")
            return

        if req.get("stream") and (not tools or tool_choice == "none"):
            self._stream_text_response(model_name, model_id, think_mode, extra_fields, prompt, images)
            return

        try:
            file_refs = _upload_images(images)
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
