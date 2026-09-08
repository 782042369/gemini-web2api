"""Google native /v1beta generateContent endpoints (Gemini CLI protocol)."""
import json
import re
import time

from ..batching import (_MICROBATCHER, _extract_batch_segments,
                        _microbatch_eligible, _microbatch_runner)
from ..config import CONFIG
from ..logs import log
from ..models import resolve_model
from ..translation import parse_numbered_translations, require_translation, split_translation_batches
from ..budget import RequestControlError, check_budget
from ..upstream.retry import retry_decision
from ..tools import google_contents_to_prompt, parse_google_function_calls
from ..upstream import generate, generate_stream
from .images import _upload_images


class GoogleGenerateMixin:
    """Handler methods for the Google generateContent endpoints."""


    def _send_batch_translation(self, instruction, segments, model_name,
                                 model_id, think_mode, extra_fields):
        """Translate many segments via numbered-prompt batching and reply.

        Groups parts by both segment count and a character budget, keeps
        oversized single segments intact, and parses [n]-prefixed lines back out. It falls back to a
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
        batch_size = CONFIG.get("translation_batch_max_segments", 25)
        max_chars = CONFIG.get("translation_batch_max_chars", 12000)
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            batch_size = 25
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            max_chars = 12000
        batches = split_translation_batches(segments, batch_size, max_chars, len(instruction) + 512)
        start = 0
        for chunk in batches:
            check_budget("translation batch")
            body = "\n".join(f"[{i}] {s}" for i, s in enumerate(chunk))
            prompt = (
                f"{instruction}\n"
                "将下列编号段落逐段处理，输出严格遵守:\n"
                "- 每段结果以 [编号] 开始，保留段内换行，编号与输入完全一致\n"
                "- 不输出任何解释、注释或额外段落\n\n"
                f"{body}"
            )
            parsed = {}
            try:
                if len(chunk) == 1:
                    results[start] = require_translation(generate(
                        f"{instruction}\n只输出结果，不要解释:\n\n{chunk[0]}",
                        model_id, think_mode, None, extra_fields))
                    start += 1
                    continue
                out = generate(prompt, model_id, think_mode, None, extra_fields)
                parsed = parse_numbered_translations(out, len(chunk))
            except RequestControlError:
                raise
            except Exception as e:
                decision = retry_decision(e, 0)
                log(f"Batch upstream error: category={decision.category}")
                # Only a successful response with missing/ambiguous numbering
                # can fall back. Infrastructure failures must not fan out.
                self.send_error_json("translation batch failed", 502, "upstream_error", code="translation_failed")
                return
            for i, seg in enumerate(chunk):
                check_budget("translation fallback")
                if i in parsed:
                    results[start + i] = parsed[i]
                else:
                    try:
                        results[start + i] = require_translation(generate(
                            f"{instruction}\n只输出结果，不要解释:\n\n{seg}",
                            model_id, think_mode, None, extra_fields))
                    except RequestControlError:
                        raise
                    except Exception as e:
                        log(f"Segment fallback error at index={start + i}: {type(e).__name__}")
                        self.send_error_json("translation failed for a source segment", 502,
                                             "upstream_error", code="translation_failed")
                        return
            start += len(chunk)
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
        """Handle Google input. Args: body JSON bytes, stream flag. Returns: None; writes one response."""
        req = self._parse_body(body)
        if req is None:
            self.send_error_json("request body must be a JSON object", 400, param="body")
            return
        if not self._validate_request(req, "google"):
            return
        if not isinstance(req.get("contents"), list) or not req.get("contents"):
            self.send_error_json("contents must be a non-empty array", 400, param="contents")
            return
        # Accept both /v1beta/models/... (Gemini CLI) and /v1/models/...
        # (Google SDK style); previously /v1 paths silently used default model.
        m = re.match(r'/v1(?:beta)?/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_error_json(err, 400, param="model")
            return

        tool_config = req.get("toolConfig", {})
        if tool_config is None:
            tool_config = {}
        if not isinstance(tool_config, dict):
            self.send_error_json("toolConfig must be an object", 400, param="toolConfig")
            return
        tools = req.get("tools")
        if tools is not None and not isinstance(tools, list):
            self.send_error_json("tools must be an array", 400, param="tools")
            return
        fc_mode = (tool_config.get("functionCallingConfig") or {}).get("mode", "AUTO")
        has_tools = bool(tools) and fc_mode != "NONE"
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
                    self._send_upstream_error(e, code="upstream_error")
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
            self.send_error_json("empty content", 400, param="contents")
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self._send_upstream_error(e, code="image_upload_failed")
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
                log(f"Google stream error: {type(e).__name__}: {e}")
                try:
                    if self._resp_status is None:
                        self._send_upstream_error(e)
                    else:
                        self._write_stream_error("google", error=e)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            self._send_upstream_error(e, code="upstream_error")
            return

        if not text:
            log("Warning: empty response from Gemini")

        response_parts = []
        if has_tools and text:
            allowed_names = {fn["name"] for group in tools for fn in group.get("functionDeclarations", [])}
            restricted = (tool_config.get("functionCallingConfig") or {}).get("allowedFunctionNames")
            if restricted:
                allowed_names &= set(restricted)
            clean_text, function_calls = parse_google_function_calls(text, allowed_names)
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
