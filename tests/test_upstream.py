"""Unit tests for upstream pure logic: parser, retry policy, batching, models.

No network access: every upstream call is mocked or replaced with crafted
wrb.fr payloads. Companion to test_api.py (server/HTTP layer).
"""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from gemini_web2api.batching import (_MicroBatcher, _extract_batch_segments,
                                     _microbatch_eligible, _microbatch_runner)
from gemini_web2api.config import CONFIG
from gemini_web2api.models import resolve_model
from gemini_web2api.upstream import cookies as cookies_mod
from gemini_web2api.upstream.generate import _retry_delay
from gemini_web2api.upstream.parser import clean_text, extract_response_text
from gemini_web2api.upstream.protocol import _build_headers, make_sapisidhash


def _wrb_line(texts, cid="c_test"):
    """Build one wrb.fr response line carrying the given candidate texts.

    Args:
        texts: list of candidate strings for inner[4].
        cid: conversation id placed at inner[1][0].

    Returns:
        JSON-encoded line string long enough to pass parser length guards.
    """
    inner = [None, [cid, "r_id"], None, None, [[None, list(texts)]]]
    return json.dumps([["wrb.fr", None, json.dumps(inner), None, None]])


class ResponseParsingTests(unittest.TestCase):
    def test_extracts_longest_candidate_text(self):
        short = "short answer " * 5
        long = "a much longer candidate answer " * 10
        raw = "\n" + _wrb_line([short, long])
        self.assertEqual(extract_response_text(raw), clean_text(long))

    def test_bard_error_raises_with_code(self):
        with self.assertRaises(RuntimeError) as ctx:
            extract_response_text("garbage\nBardErrorInfo [1100]\nmore")
        self.assertIn("1100", str(ctx.exception))

    def test_empty_or_unparseable_input_returns_empty(self):
        self.assertEqual(extract_response_text(""), "")
        self.assertEqual(extract_response_text("no wrb data here"), "")

    def test_clean_text_stale_code_reference_block(self):
        fence = chr(96) * 3
        text = ("before\n" + fence + "python?code_reference&code_event_index=0\n"
                "STALE PAYLOAD\n" + fence + "\nafter")
        cleaned = clean_text(text)
        self.assertNotIn("STALE PAYLOAD", cleaned)
        self.assertNotIn("code_reference", cleaned)
        self.assertIn("after", cleaned)

    def test_clean_text_card_content_link(self):
        text = "see\nhttp://googleusercontent.com/card_content/3\nend"
        cleaned = clean_text(text)
        self.assertNotIn("card_content", cleaned)
        self.assertIn("end", cleaned)


class RetryPolicyTests(unittest.TestCase):
    def test_rate_limited_uses_long_ladder_capped_at_60(self):
        self.assertEqual(_retry_delay(0, rate_limited=True), 10)
        self.assertEqual(_retry_delay(1, rate_limited=True), 25)
        self.assertEqual(_retry_delay(9, rate_limited=True), 60)

    def test_transport_error_retries_immediately(self):
        self.assertEqual(_retry_delay(0, transport_error=True), 0.05)
        self.assertEqual(_retry_delay(3, transport_error=True), 0.05)

    def test_generic_error_exponential_backoff_with_jitter(self):
        base = CONFIG["retry_delay_sec"]
        for attempt in (0, 1, 2):
            delay = _retry_delay(attempt)
            self.assertGreaterEqual(delay, base * (2 ** attempt))
            self.assertLess(delay, base * (2 ** attempt) + 0.5)
        self.assertLessEqual(_retry_delay(30), 15)


class MicrobatchEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CONFIG)
        CONFIG["log_requests"] = False
        CONFIG["microbatch_max_prompt"] = 3000

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.saved)

    def _req(self, parts, sys_text=None):
        req = {"contents": [{"role": "user", "parts": parts}]}
        if sys_text is not None:
            req["systemInstruction"] = {"parts": [{"text": sys_text}]}
        return req

    def test_eligible_single_text_part_with_instruction(self):
        result = _microbatch_eligible(self._req([{"text": "translate me"}], "be terse"))
        self.assertEqual(result, ("be terse", "translate me"))

    def test_ineligible_tools(self):
        req = self._req([{"text": "hi"}], "sys")
        req["tools"] = [{"x": 1}]
        self.assertIsNone(_microbatch_eligible(req))

    def test_ineligible_multiple_parts_or_contents(self):
        self.assertIsNone(_microbatch_eligible(self._req([{"text": "a"}, {"text": "b"}])))
        two_contents = {"contents": [{"role": "user", "parts": [{"text": "a"}]},
                                      {"role": "user", "parts": [{"text": "b"}]}]}
        self.assertIsNone(_microbatch_eligible(two_contents))

    def test_ineligible_non_text_or_empty_part(self):
        self.assertIsNone(_microbatch_eligible(self._req([{"text": "a", "inlineData": {}}])))
        self.assertIsNone(_microbatch_eligible(self._req([{"text": "   "}])))

    def test_ineligible_prompt_too_long(self):
        CONFIG["microbatch_max_prompt"] = 5
        self.assertIsNone(_microbatch_eligible(self._req([{"text": "way too long"}])))

    def test_extract_batch_segments_groups_parts(self):
        result = _extract_batch_segments(self._req(
            [{"text": "one"}, {"text": "two"}, {"text": "three"}], "translate"))
        self.assertEqual(result, ("translate", ["one", "two", "three"]))

    def test_extract_batch_segments_requires_instruction_and_two_parts(self):
        self.assertIsNone(_extract_batch_segments(self._req([{"text": "one"}, {"text": "two"}])))
        self.assertIsNone(_extract_batch_segments(self._req([{"text": "solo"}], "sys")))


class MicroBatcherTests(unittest.TestCase):
    def _entry(self, key, prompt, result=None, error=None):
        return {"key": key, "prompt": prompt,
                "holder": {"event": threading.Event(), "result": result, "error": error}}

    def test_run_batch_dispatches_results_in_order(self):
        batcher = _MicroBatcher(window=0.01, max_segments=4)
        entries = [self._entry("k", "p%d" % i) for i in range(3)]
        entries[0]["runner"] = lambda prompts: ["r-" + p for p in prompts]
        entries[1]["runner"] = entries[2]["runner"] = None  # same bucket uses first
        batcher._run_batch(entries)
        for i, e in enumerate(entries):
            self.assertTrue(e["holder"]["event"].is_set())
            self.assertIsNone(e["holder"]["error"])
            self.assertEqual(e["holder"]["result"], "r-p%d" % i)

    def test_run_batch_propagates_runner_error_to_all(self):
        batcher = _MicroBatcher(window=0.01, max_segments=4)

        def boom(prompts):
            raise RuntimeError("upstream down")

        entries = [self._entry("k", "p%d" % i) for i in range(2)]
        entries[0]["runner"] = boom
        entries[1]["runner"] = None
        batcher._run_batch(entries)
        for e in entries:
            self.assertIsInstance(e["holder"]["error"], RuntimeError)
            self.assertIsNone(e["holder"]["result"])


class MicrobatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CONFIG)
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.saved)

    @mock.patch("gemini_web2api.batching.generate")
    def test_single_prompt_uses_direct_path(self, generate):
        generate.return_value = "solo result"
        runner = _microbatch_runner(1, 4, None, "instruction")
        self.assertEqual(runner(["only one"]), ["solo result"])
        generate.assert_called_once_with("instruction\nonly one", 1, 4, None, None)

    @mock.patch("gemini_web2api.batching.generate")
    def test_batched_prompts_parse_numbered_blocks(self, generate):
        generate.return_value = "[0] first\n[1] second"
        runner = _microbatch_runner(1, 4, None, "")
        self.assertEqual(runner(["aaa", "bbb"]), ["first", "second"])
        self.assertEqual(generate.call_count, 1)

    @mock.patch("gemini_web2api.batching.generate")
    def test_dropped_segment_falls_back_to_direct(self, generate):
        generate.side_effect = ["[0] kept", "fallback text"]
        runner = _microbatch_runner(1, 4, None, "")
        self.assertEqual(runner(["aaa", "bbb"]), ["kept", "fallback text"])
        self.assertEqual(generate.call_count, 2)


class ModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CONFIG)
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.saved)

    def test_known_model(self):
        name, mode, think, err, extra = resolve_model("gemini-3.8-flash")
        self.assertEqual((name, err, extra), ("gemini-3.8-flash", None, None))
        self.assertEqual(mode, 1)
        self.assertEqual(think, 4)

    def test_think_suffix_overrides(self):
        name, mode, think, err, extra = resolve_model("gemini-3.8-flash@think=0")
        self.assertEqual((name, err), ("gemini-3.8-flash", None))
        self.assertEqual(think, 0)

    def test_invalid_think_suffix_errors(self):
        _, _, _, err, _ = resolve_model("gemini-3.8-flash@think=abc")
        self.assertIn("Invalid think level", err)

    def test_unknown_model_falls_back_to_default(self):
        name, _, _, err, _ = resolve_model("not-a-model")
        self.assertIsNone(err)
        self.assertEqual(name, CONFIG.get("default_model", "gemini-3.8-flash"))

    def test_extra_fields_propagated(self):
        _, _, _, err, extra = resolve_model("gemini-3.1-pro-enhanced")
        self.assertIsNone(err)
        self.assertEqual(extra, {31: 2, 80: 3})


class HeaderBuildingTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CONFIG)
        CONFIG["log_requests"] = False
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.tmp.write(json.dumps({
            "cookie": "SID=x; HSID=y; SAPISID=testsapisid",
            "sapisid": "testsapisid",
        }))
        self.tmp.close()
        CONFIG["cookie_file"] = self.tmp.name
        cookies_mod._cookie_caches.clear()

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.saved)
        cookies_mod._cookie_caches.clear()
        os.unlink(self.tmp.name)

    def test_sapisidhash_format(self):
        token = make_sapisidhash("testsapisid")
        self.assertTrue(token.startswith("SAPISIDHASH "))
        ts, _, digest = token[len("SAPISIDHASH "):].partition("_")
        self.assertTrue(ts.isdigit())
        self.assertEqual(len(digest), 40)

    def test_build_headers_carries_cookie_and_auth(self):
        headers = _build_headers()
        self.assertIn("SAPISID=testsapisid", headers.get("Cookie", ""))
        self.assertTrue(headers.get("Authorization", "").startswith("SAPISIDHASH "))
        self.assertEqual(headers.get("Origin"), "https://gemini.google.com")


if __name__ == "__main__":
    unittest.main()
