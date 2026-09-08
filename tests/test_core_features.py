"""Core-product regressions: complete translations and faithful image attachment handling."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api.batching import _MicroBatcher, _microbatch_runner
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server.google import GoogleGenerateMixin
from gemini_web2api.server.images import _upload_images
from gemini_web2api.translation import parse_numbered_translations
from gemini_web2api.upstream import cookies
from gemini_web2api.upstream.protocol import _build_payload
from gemini_web2api.validation import RequestValidationError, validate_chat_request, validate_google_request


class TranslationTests(unittest.TestCase):
    """Prevent missing/misordered paragraphs from being reported as success."""

    def setUp(self):
        """Isolate test configuration. Args: None. Returns: None."""
        patch = mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False})
        patch.start()
        self.addCleanup(patch.stop)

    def test_multiline_blocks_are_preserved_in_input_order(self):
        """Support reordered multiline output. Args: None. Returns: None."""
        parsed = parse_numbered_translations("[1] second\ncontinued\n[0] first\n\nparagraph", 2)
        self.assertEqual([parsed[0], parsed[1]], ["first\n\nparagraph", "second\ncontinued"])

    def test_duplicate_empty_and_out_of_range_indexes_are_not_trusted(self):
        """Require individual retry for ambiguity. Args: None. Returns: None."""
        self.assertEqual(parse_numbered_translations("[0] first\n[0] different\n[1] stable", 2), {1: "stable"})
        self.assertEqual(parse_numbered_translations("[0]\n[1] valid", 2), {1: "valid"})
        self.assertEqual(parse_numbered_translations("[1] shifted\n[2] shifted", 2), {})

    def batch(self, segments, outputs):
        """Run one API batch without networking. Args: segments, canned outputs. Returns: handler, mock."""
        handler = SimpleNamespace(send_json=mock.Mock(), send_error_json=mock.Mock())
        with mock.patch("gemini_web2api.server.google.generate", side_effect=outputs) as generate:
            GoogleGenerateMixin._send_batch_translation(handler, "Translate faithfully", segments,
                                                         "gemini-3.8-flash", 1, 4, None)
        return handler, generate

    def test_batch_does_not_flatten_source_or_drop_translated_lines(self):
        """Keep paragraph formatting. Args: None. Returns: None."""
        handler, generate = self.batch(["a\nb", "c"], ["[0] 甲\n乙\n[1] 丙"])
        self.assertIn("[0] a\nb", generate.call_args.args[0])
        parts = handler.send_json.call_args.args[0]["candidates"][0]["content"]["parts"]
        self.assertEqual(parts, [{"text": "甲\n乙"}, {"text": "丙"}])
        handler.send_error_json.assert_not_called()

    def test_only_missing_segment_falls_back(self):
        """Preserve verified batch results. Args: None. Returns: None."""
        handler, generate = self.batch(["a", "b", "c"], ["[2] 丙\n[0] 甲", "乙"])
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(generate.call_args.args[0].endswith("b"))
        parts = handler.send_json.call_args.args[0]["candidates"][0]["content"]["parts"]
        self.assertEqual([part["text"] for part in parts], ["甲", "乙", "丙"])

    def test_failed_or_empty_fallback_never_returns_source_as_translation(self):
        """Failure must not look like success. Args: None. Returns: None."""
        for failure in (RuntimeError("offline"), "", None):
            with self.subTest(failure=failure):
                handler, _ = self.batch(["original-a", "original-b"], ["[0] translated", failure])
                handler.send_json.assert_not_called()
                self.assertEqual(handler.send_error_json.call_args.args[1], 502)
                self.assertEqual(handler.send_error_json.call_args.kwargs["code"], "translation_failed")

    def test_more_than_25_segments_keep_global_order(self):
        """Verify the 25-segment API boundary. Args: None. Returns: None."""
        segments = [f"source-{index}" for index in range(28)]
        output_a = "\n".join(f"[{index}] result-{index}" for index in range(25))
        output_b = "\n".join(f"[{index}] result-{index + 25}" for index in range(3))
        handler, generate = self.batch(segments, [output_a, output_b])
        self.assertEqual(generate.call_count, 2)
        parts = handler.send_json.call_args.args[0]["candidates"][0]["content"]["parts"]
        self.assertEqual([p["text"] for p in parts], [f"result-{index}" for index in range(28)])

    def test_microbatch_uses_the_same_duplicate_guard(self):
        """Both translation paths retry ambiguity. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.batching.generate", side_effect=["[0] bad\n[0] worse\n[1] good", "fixed"]):
            self.assertEqual(_microbatch_runner(1, 4, None)(["a", "b"]), ["fixed", "good"])

    def test_microbatch_rejects_empty_direct_results(self):
        """Do not produce an empty successful segment. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.batching.generate", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "empty"):
                _microbatch_runner(1, 4, None)(["a"])

    def test_account_context_and_max_segments_are_respected(self):
        """Each group runs as its selected account. Args: None. Returns: None."""
        seen = []

        def runner(prompts):
            """Capture context. Args: prompts. Returns: translated list."""
            seen.append((cookies._active_cookie_path(), cookies._active_auth_user(), len(prompts)))
            return ["translated:" + prompt for prompt in prompts]

        batcher = _MicroBatcher(0.01, 1)
        entries = [{"key": "same-instruction", "prompt": str(i), "cookie_path": account,
                    "auth_user": index, "runner": runner,
                    "holder": {"event": threading.Event(), "result": None, "error": None}}
                   for i, (account, index) in enumerate((("/fake/a", 1), ("/fake/a", 1), ("/fake/b", 2)))]
        before = dict(cookies._active_cookie.__dict__)
        batcher._run_batch(entries)
        self.assertEqual(seen, [("/fake/a", 1, 1), ("/fake/a", 1, 1), ("/fake/b", 2, 1)])
        self.assertEqual(cookies._active_cookie.__dict__, before)
        self.assertTrue(all(e["holder"]["event"].is_set() for e in entries))

    def test_lone_request_waits_single_window_not_full_window(self):
        """Use a deterministic clock to test early wakeup. Args: None. Returns: None."""
        clock = [10.0]
        batcher = _MicroBatcher(1.5, 6, single_wait=0.2)
        batcher._pending = [{"prompt": "one"}]
        condition = mock.MagicMock()

        def wait(seconds):
            """Advance the clock. Args: seconds. Returns: None."""
            clock[0] += seconds

        condition.wait.side_effect = wait
        batcher._cv = condition
        with mock.patch("gemini_web2api.batching.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(batcher, "_run_batch", side_effect=RuntimeError("stop-test")):
            with self.assertRaisesRegex(RuntimeError, "stop-test"):
                batcher._dispatch_loop()
        self.assertAlmostEqual(clock[0] - 10, 0.2)

    def test_cancelled_waiter_does_not_trigger_later_generation(self):
        """Drop timed-out work still queued. Args: None. Returns: None."""
        runner = mock.Mock()
        batcher = _MicroBatcher(0.01, 2)
        batcher._run_batch([{"key": "x", "prompt": "obsolete", "runner": runner,
                             "holder": {"cancelled": True}}])
        runner.assert_not_called()


class VisionInputTests(unittest.TestCase):
    """Make image inputs faithful rather than silently ignored."""

    def test_bad_inline_image_is_rejected_before_text_only_generation(self):
        """Reject malformed inline data at API validation. Args: None. Returns: None."""
        with self.assertRaises(RequestValidationError):
            validate_chat_request({"messages": [{"content": [{"type": "image_url", "image_url": "data:image/png;base64,%%%"}]}]})
        with self.assertRaises(RequestValidationError):
            validate_google_request({"contents": [{"parts": [{"inlineData": {"data": "%%%"}}]}]})

    def test_detected_jpeg_mime_reaches_upstream_attachment(self):
        """Keep actual image MIME across upload and payload. Args: None. Returns: None."""
        with mock.patch("gemini_web2api.server.images.upload_image", return_value="/contrib/jpeg"):
            refs = _upload_images([(b"\xff\xd8\xffjpeg-data", "image/png")])
        with mock.patch("gemini_web2api.upstream.protocol.get_active_xsrf_token", return_value=None):
            payload = _build_payload("describe", 1, 4, refs)
        inner = json.loads(json.loads(parse_qs(payload)["f.req"][0])[1])
        self.assertEqual(inner[0][3][0][0][3], "image/jpeg")

    def test_oversized_image_is_rejected_before_upload(self):
        """Avoid upstream upload cost for over-limit inputs. Args: None. Returns: None."""
        with mock.patch.dict(CONFIG, {"max_image_bytes": 2}), \
                mock.patch("gemini_web2api.server.images.upload_image") as upload:
            with self.assertRaises(RuntimeError):
                _upload_images([(b"larger", "image/png")])
            upload.assert_not_called()

    def test_image_session_cache_is_account_scoped_and_retries_failures(self):
        """Retry failed tokens promptly without cross-account reuse. Args: None. Returns: None."""
        from gemini_web2api import multimodal
        clock = [0.0]
        with mock.patch.dict(multimodal._page_tokens_cache, {}, clear=True), \
                mock.patch.object(multimodal, "_active_cookie_path", return_value="/fake/a") as active, \
                mock.patch.object(multimodal, "_active_auth_user", return_value=0), \
                mock.patch.object(multimodal.os.path, "getmtime", return_value=1), \
                mock.patch.object(multimodal.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(multimodal, "_get_page_tokens", side_effect=[{}, {"push_id": "A", "at": "AOvx-A"},
                                                                            {"push_id": "B", "at": "AOvx-B"}]) as fetch:
            self.assertEqual(multimodal._cached_page_tokens(), {})
            clock[0] = 10
            self.assertEqual(multimodal._cached_page_tokens(), {})
            self.assertEqual(fetch.call_count, 1)
            clock[0] = 31
            self.assertEqual(multimodal._cached_page_tokens()["push_id"], "A")
            active.return_value = "/fake/b"
            self.assertEqual(multimodal._cached_page_tokens()["push_id"], "B")
            active.return_value = "/fake/a"
            self.assertEqual(multimodal._cached_page_tokens()["push_id"], "A")
            self.assertEqual(fetch.call_count, 3)

    def test_scotty_start_and_finalize_use_the_same_account(self):
        """Lock the two-step upload contract. Args: None. Returns: None."""
        from gemini_web2api import multimodal
        with mock.patch.object(multimodal, "_cached_page_tokens", return_value={"push_id": "p", "pctx": "ctx"}), \
                mock.patch.object(multimodal, "load_cookie", return_value=("SID=test", None)), \
                mock.patch.object(multimodal, "_active_auth_user", return_value=2), \
                mock.patch.object(multimodal, "_upload_post", side_effect=[
                    (200, {"x-goog-upload-url": "https://push.clients6.google.com/finalize"}, ""),
                    (200, {}, "/contrib_service/image")]) as post:
            self.assertEqual(multimodal.upload_image(b"jpeg", "image.jpg", "image/jpeg"), "/contrib_service/image")
            start, finalize = post.call_args_list
            self.assertEqual(start.args[1]["X-Goog-Upload-Command"], "start")
            self.assertEqual(finalize.args[1]["X-Goog-Upload-Command"], "upload, finalize")
            self.assertEqual(finalize.args[2], b"jpeg")
            for call in (start, finalize):
                self.assertEqual(call.args[1]["X-Goog-AuthUser"], "2")
                self.assertEqual(call.args[1]["Push-ID"], "p")
                self.assertIn("/u/2/app", call.args[1]["Referer"])

    def test_missing_push_token_does_not_upload_unbound_images(self):
        """Never upload to an anonymous bucket as fallback. Args: None. Returns: None."""
        from gemini_web2api import multimodal
        with mock.patch.object(multimodal, "_cached_page_tokens", return_value={}), \
                mock.patch.object(multimodal, "_upload_post") as post:
            with self.assertRaisesRegex(RuntimeError, "push_id"):
                multimodal.upload_image(b"image")
            post.assert_not_called()

