"""Batching: transparent micro-batcher + numbered multi-segment translation.

Micro-batching collects burst single-segment generateContent requests into
one numbered upstream call; batch translation splits one multi-part request
into numbered segments. Both fall back to direct calls per dropped segment.
"""
import re
import threading
import time

from .config import CONFIG
from .logs import log
from .upstream import generate


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
