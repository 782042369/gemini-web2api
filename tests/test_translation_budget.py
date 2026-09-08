"""Character-budget segmentation and shared-batch deadline integration tests."""
import random
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from gemini_web2api.batching import _MicroBatcher, _microbatch_runner
from gemini_web2api.budget import RequestBudget, RequestDeadlineExceeded, budget_scope
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server.google import GoogleGenerateMixin
from gemini_web2api.translation import split_translation_batches
from gemini_web2api.upstream.retry import UpstreamRejection


class TranslationSplitTests(unittest.TestCase):
    """Never reorder, truncate, or conflate characters with bytes/tokens."""

    def test_exact_character_boundary_and_overhead(self):
        """Count the numbering and reserved instruction. Args: None. Returns: None."""
        self.assertEqual(split_translation_batches(["abc", "def"], 25, 16), [["abc", "def"]])
        self.assertEqual(split_translation_batches(["abc", "def"], 25, 15), [["abc"], ["def"]])
        self.assertEqual(split_translation_batches(["abc", "def"], 25, 17, 1), [["abc", "def"]])
        self.assertEqual(split_translation_batches(["abc", "def"], 25, 16, 1), [["abc"], ["def"]])

    def test_count_limit_applies_even_to_empty_segments(self):
        """Keep empty/duplicate strings without exceeding count. Args: None. Returns: None."""
        self.assertEqual(split_translation_batches(["", "", ""], 2, 100), [["", ""], [""]])
        self.assertEqual(split_translation_batches([], 2, 100), [])

    def test_unicode_and_multiline_are_preserved(self):
        """Budget Python characters while preserving formatting. Args: None. Returns: None."""
        self.assertEqual(split_translation_batches(["中文", "🚲"], 25, 13), [["中文", "🚲"]])
        values = [" leading\n\ntrailing ", "same", "same"]
        groups = split_translation_batches(iter(values), 25, 20)
        self.assertEqual([part for group in groups for part in group], values)

    def test_oversized_middle_segment_is_intact_and_alone(self):
        """Do not truncate a long source or pack another beside it. Args: None. Returns: None."""
        long = "段落\n" * 100
        self.assertEqual(split_translation_batches(["a", long, "b", "c"], 25, 20), [["a"], [long], ["b", "c"]])

    def test_oversized_instruction_forces_singletons(self):
        """Preserve oversized instruction use rather than mutating sources. Args: None. Returns: None."""
        self.assertEqual(split_translation_batches(["a", "b"], 25, 10, 1000), [["a"], ["b"]])

    def test_invalid_limits_and_sources_are_rejected(self):
        """Reject malformed helper arguments. Args: None. Returns: None."""
        for args in ((["a"], 0, 100), (["a"], 2, 0), (["a"], True, 100), (["a"], 2, 1.5),
                     (["a"], 2, 100, -1), ([1], 2, 100), ("abc", 2, 100)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                split_translation_batches(*args)

    def test_randomized_order_count_and_character_invariants(self):
        """Check shape invariants across reproducible source sizes. Args: None. Returns: None."""
        rng = random.Random(4174)
        for _ in range(100):
            values = ["文\n" * rng.randrange(0, 20) for _ in range(rng.randrange(0, 40))]
            count, cap, overhead = rng.randrange(1, 10), rng.randrange(1, 150), rng.randrange(0, 100)
            batches = split_translation_batches(values, count, cap, overhead)
            self.assertEqual([s for batch in batches for s in batch], values)
            for batch in batches:
                self.assertLessEqual(len(batch), count)
                cost = overhead + sum(len(s) + len(str(i)) + 4 for i, s in enumerate(batch))
                if len(batch) > 1:
                    self.assertLessEqual(cost, cap)


class TranslationBudgetIntegrationTests(unittest.TestCase):
    """Apply the splitter and deadlines in both externally used batch paths."""

    def setUp(self):
        """Isolate small test limits. Args: None. Returns: None."""
        patch = mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False})
        patch.start()
        self.addCleanup(patch.stop)

    def test_api_character_budget_sends_long_source_directly(self):
        """Oversized singleton takes direct path without truncation. Args: None. Returns: None."""
        instruction = "Translate"
        CONFIG["translation_batch_max_chars"] = len(instruction) + 512 + 13
        handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())
        source = ["a", "b", "long paragraph " * 20, "c"]
        with mock.patch("gemini_web2api.server.google.generate", side_effect=["[0] 甲\n[1] 乙", "长译文", "丙"]) as generate:
            GoogleGenerateMixin._send_batch_translation(handler, instruction, source, "m", 1, 4, None)
        self.assertEqual(generate.call_count, 3)
        self.assertTrue(generate.call_args_list[1].args[0].endswith(source[2]))
        self.assertNotIn("[0]", generate.call_args_list[1].args[0])
        result = handler.send_json.call_args.args[0]["candidates"][0]["content"]["parts"]
        self.assertEqual([p["text"] for p in result], ["甲", "乙", "长译文", "丙"])

    def test_terminal_batch_failure_does_not_fan_out_per_segment(self):
        """A deterministic refusal must not multiply calls. Args: None. Returns: None."""
        handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())
        with mock.patch("gemini_web2api.server.google.generate", side_effect=UpstreamRejection(1100)) as send:
            GoogleGenerateMixin._send_batch_translation(handler, "Translate", ["a", "b"], "m", 1, 4, None)
        self.assertEqual(send.call_count, 1)
        handler.send_json.assert_not_called()
        self.assertEqual(handler.send_error_json.call_args.args[1], 502)

    def test_microbatch_terminal_failure_also_does_not_fan_out(self):
        """Microbatch fallback honors retry classification. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.batching.generate", side_effect=UpstreamRejection(1100)) as send:
            with self.assertRaises(UpstreamRejection):
                _microbatch_runner(1, 4, None)(["a", "b"])
            self.assertEqual(send.call_count, 1)

    def test_throttled_batch_does_not_bypass_retry_after_via_fallback(self):
        """An exhausted 429/503 must not become fresh per-segment attempts. Args: None. Returns: None."""
        CONFIG["retry_attempts"] = 1
        for status in (429, 503):
            failure = RuntimeError("server cooldown")
            failure.response = SimpleNamespace(status_code=status, headers={"Retry-After": "120"})
            with self.subTest(status=status), budget_scope(RequestBudget(seconds=30)):
                handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())
                with mock.patch("gemini_web2api.server.google.generate", side_effect=failure) as send:
                    GoogleGenerateMixin._send_batch_translation(handler, "Translate", ["a", "b"], "m", 1, 4, None)
                    self.assertEqual(send.call_count, 1)
                    handler.send_json.assert_not_called()
                with mock.patch("gemini_web2api.batching.generate", side_effect=failure) as send:
                    with self.assertRaises(RuntimeError):
                        _microbatch_runner(1, 4, None)(["a", "b"])
                    self.assertEqual(send.call_count, 1)

    def test_budget_exhausted_by_one_batch_prevents_next_batch(self):
        """All API batches consume one total budget. Args: None. Returns: None."""
        CONFIG["translation_batch_max_segments"] = 2
        clock = [0.0]
        handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())

        def generate(*args):
            """Exhaust the simulated budget. Args: ignored. Returns: first batch text."""
            clock[0] += 6
            return "[0] a\n[1] b"

        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("gemini_web2api.server.google.generate", side_effect=generate) as send, \
                budget_scope(RequestBudget(seconds=5)):
            with self.assertRaises(RequestDeadlineExceeded):
                GoogleGenerateMixin._send_batch_translation(handler, "Translate", ["a", "b", "c"], "m", 1, 4, None)
            self.assertEqual(send.call_count, 1)
        handler.send_json.assert_not_called()

    def test_microbatch_character_groups_keep_entry_order(self):
        """Metadata follows its original text when split. Args: None. Returns: None."""
        CONFIG["translation_batch_max_chars"] = 12
        seen = []

        def runner(prompts):
            """Record groups. Args: prompts. Returns: aligned translations."""
            seen.append(list(prompts))
            return ["translated-" + value for value in prompts]

        batcher = _MicroBatcher(0.01, 6)
        entries = [{"key": "one", "prompt": text, "runner": runner,
                    "holder": {"event": threading.Event(), "result": None, "error": None}}
                   for text in ["a", "b", "long paragraph", "c"]]
        batcher._run_batch(entries)
        self.assertEqual(seen, [["a", "b"], ["long paragraph"], ["c"]])
        self.assertEqual([e["holder"]["result"] for e in entries], ["translated-a", "translated-b", "translated-long paragraph", "translated-c"])

    def test_shared_batch_short_expiry_does_not_poison_long_waiter(self):
        """Use the latest active deadline, but never deliver stale results. Args: None. Returns: None."""
        clock = [0.0]
        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]):
            short, long = RequestBudget(seconds=1), RequestBudget(seconds=5)

            def runner(prompts):
                """Expire only the short waiter. Args: prompts. Returns: translations."""
                clock[0] = 2
                return ["translated" for _ in prompts]

            entries = [{"key": "same", "prompt": "p", "runner": runner, "budget": budget,
                        "holder": {"event": threading.Event(), "started": threading.Event(), "result": None, "error": None}}
                       for budget in (short, long)]
            _MicroBatcher(0.01, 6)._run_batch(entries)
        self.assertIsInstance(entries[0]["holder"]["error"], RequestDeadlineExceeded)
        self.assertIsNone(entries[0]["holder"]["result"])
        self.assertEqual(entries[1]["holder"]["result"], "translated")
        self.assertIsNone(entries[1]["holder"]["error"])

    def test_expired_batch_member_is_skipped_before_generation(self):
        """No later generation for an expired queue entry. Args: None. Returns: None."""
        budget = RequestBudget(deadline=0)
        runner = mock.Mock()
        entry = {"key": "k", "prompt": "old", "budget": budget, "runner": runner,
                 "holder": {"event": threading.Event(), "started": threading.Event()}}
        _MicroBatcher(0.01, 6)._run_batch([entry])
        runner.assert_not_called()
        self.assertIsInstance(entry["holder"]["error"], RequestDeadlineExceeded)
        self.assertTrue(entry["holder"]["started"].is_set())

    def test_submit_timeout_removes_pending_entry(self):
        """Do not leave queued work after the caller expires. Args: None. Returns: None."""
        batcher = _MicroBatcher(1.5, 6)
        with mock.patch.object(batcher, "_ensure_worker"), budget_scope(RequestBudget(seconds=0.02)):
            with self.assertRaises(RequestDeadlineExceeded):
                batcher.submit({"key": "k", "prompt": "p", "runner": mock.Mock()})
        self.assertEqual(batcher._pending, [])
