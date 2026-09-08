"""Deterministic offline regressions for request budgets, queues and retry execution."""
import importlib
import io
import json
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from gemini_web2api.budget import (RequestBudget, RequestCancelled, RequestDeadlineExceeded,
                                  QueueFull, QueueTimeout, budget_scope, current_budget)
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.upstream import concurrency
from gemini_web2api.upstream.retry import UpstreamRejection
from gemini_web2api.upstream.transport import HAS_CURL_CFFI, curl_total_timeout

pipeline = importlib.import_module("gemini_web2api.upstream.generate")


def wrb(text):
    """Create a protocol snapshot. Args: text. Returns: one encoded line."""
    inner = [None, ["c_test", "r_test"], None, None, [[None, [text]]]]
    return json.dumps([["wrb.fr", None, json.dumps(inner), None, None]]) + "\n"


class BudgetTests(unittest.TestCase):
    """A nested call can never silently renew the overall request allowance."""

    def test_nested_scope_reuses_and_restores_budget(self):
        """Do not reset deadlines in helper calls. Args: None. Returns: None."""
        self.assertIsNone(current_budget())
        outer = RequestBudget(seconds=10)
        with budget_scope(outer):
            with budget_scope() as nested:
                self.assertIs(nested, outer)
                self.assertEqual(nested.deadline, outer.deadline)
            self.assertIs(current_budget(), outer)
        self.assertIsNone(current_budget())

    def test_wait_raises_instead_of_returning_missing_result(self):
        """A timed-out event is an error. Args: None. Returns: None."""
        budget = RequestBudget(seconds=0.02)
        with self.assertRaises(RequestDeadlineExceeded):
            budget.wait(threading.Event(), "test result")

    def test_retry_backoff_is_rejected_before_spending_insufficient_budget(self):
        """Do not sleep beyond the original deadline. Args: None. Returns: None."""
        budget = RequestBudget(seconds=2)
        with mock.patch.object(budget.cancelled, "wait") as wait:
            with self.assertRaises(RequestDeadlineExceeded):
                budget.sleep(20)
            wait.assert_not_called()

    def test_cancel_is_terminal_even_if_result_event_is_set(self):
        """Cancellation wins a completion race. Args: None. Returns: None."""
        budget = RequestBudget(seconds=10)
        event = threading.Event()
        event.set()
        budget.cancel()
        with self.assertRaises(RequestCancelled):
            budget.wait(event)

    def test_native_curl_timeout_is_restored_after_failure(self):
        """Streaming options must not leak to future calls. Args: None. Returns: None."""
        if not HAS_CURL_CFFI:
            self.skipTest("curl_cffi unavailable")
        from curl_cffi import CurlOpt
        session = SimpleNamespace(curl_options={CurlOpt.TIMEOUT_MS: 5000})
        with self.assertRaisesRegex(RuntimeError, "test"):
            with curl_total_timeout(session, 0.25):
                self.assertEqual(session.curl_options[CurlOpt.TIMEOUT_MS], 250)
                raise RuntimeError("test")
        self.assertEqual(session.curl_options[CurlOpt.TIMEOUT_MS], 5000)
        session.curl_options.clear()
        with curl_total_timeout(session, 0.1):
            self.assertEqual(session.curl_options[CurlOpt.TIMEOUT_MS], 100)
        self.assertEqual(session.curl_options, {})


class QueueBudgetTests(unittest.TestCase):
    """Queue limits and cancellations must leave semaphore accounting correct."""

    def setUp(self):
        """Isolate account/queue configuration. Args: None. Returns: None."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False,
                                  "max_concurrent_requests": 1, "queue_timeout_sec": 0.03}))
        self.stack.enter_context(mock.patch.object(concurrency, "_active_cookie_path", return_value="account-test"))
        concurrency._upstream_semaphores.clear()
        concurrency._upstream_waiters.clear()
        self.semaphore = concurrency._get_semaphore()

    def test_queue_timeout_does_not_leak_waiter_or_slot(self):
        """Only the original owner holds capacity after timeout. Args: None. Returns: None."""
        self.semaphore.acquire()
        try:
            with budget_scope(RequestBudget(seconds=1)), self.assertRaises(QueueTimeout):
                with concurrency._UpstreamSlot():
                    self.fail("must not acquire")
            self.assertEqual(concurrency._upstream_waiters, {})
            self.assertFalse(self.semaphore.acquire(blocking=False))
        finally:
            self.semaphore.release()

    def test_overall_deadline_wins_over_longer_queue_wait(self):
        """Queueing consumes the request budget. Args: None. Returns: None."""
        CONFIG["queue_timeout_sec"] = 2
        self.semaphore.acquire()
        try:
            with budget_scope(RequestBudget(seconds=0.02)), self.assertRaises(RequestDeadlineExceeded):
                with concurrency._UpstreamSlot():
                    self.fail("must not acquire")
            self.assertEqual(concurrency._upstream_waiters, {})
        finally:
            self.semaphore.release()

    def test_queue_full_is_immediate_and_keeps_existing_waiters(self):
        """Do not admit beyond the queue cap. Args: None. Returns: None."""
        CONFIG["max_queued_requests"] = 1
        concurrency._upstream_waiters["account-test"] = 1
        self.semaphore.acquire()
        try:
            with budget_scope(RequestBudget(seconds=1)), self.assertRaises(QueueFull):
                with concurrency._UpstreamSlot():
                    self.fail("queue must be full")
            self.assertEqual(concurrency._upstream_waiters["account-test"], 1)
        finally:
            self.semaphore.release()
            concurrency._upstream_waiters.clear()

    def test_cancelled_waiter_is_removed_without_releasing_another_owner(self):
        """Cancel during a real bounded acquire. Args: None. Returns: None."""
        CONFIG["queue_timeout_sec"] = 2
        budget = RequestBudget(seconds=3)
        waiting = threading.Event()
        done = threading.Event()
        outcome = []
        real = self.semaphore
        real.acquire()

        def acquire(*args, **kwargs):
            """Signal the blocking acquire. Args: semaphore arguments. Returns: acquired flag."""
            if "timeout" in kwargs:
                waiting.set()
            return real.acquire(*args, **kwargs)

        def worker():
            """Wait as a cancellable request. Args: None. Returns: None."""
            try:
                with budget_scope(budget), concurrency._UpstreamSlot():
                    outcome.append("unexpected slot")
            except Exception as exc:
                outcome.append(exc)
            finally:
                done.set()

        proxy = SimpleNamespace(acquire=acquire, release=real.release)
        with mock.patch.object(concurrency, "_get_semaphore", return_value=proxy):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(waiting.wait(1))
                budget.cancel()
                self.assertTrue(done.wait(2))
                self.assertIsInstance(outcome[0], RequestCancelled)
                self.assertEqual(concurrency._upstream_waiters, {})
                self.assertFalse(real.acquire(blocking=False))
            finally:
                budget.cancel()
                real.release()
                thread.join(3)

    def test_exception_in_active_request_releases_slot(self):
        """All successful acquisitions release once. Args: None. Returns: None."""
        with self.assertRaisesRegex(RuntimeError, "operation"):
            with concurrency._UpstreamSlot():
                raise RuntimeError("operation")
        self.assertTrue(self.semaphore.acquire(blocking=False))
        self.semaphore.release()


class CoalescingBudgetTests(unittest.TestCase):
    """Sharing an upstream call must not share the waiters' cancellation state."""

    def setUp(self):
        """Isolate single-flight state. Args: None. Returns: None."""
        pipeline._inflight.clear()
        patch = mock.patch.object(pipeline, "_inflight_key", return_value=("same-request",))
        patch.start()
        self.addCleanup(patch.stop)

    def test_short_waiter_times_out_without_cancelling_owner(self):
        """The live owner remains usable by other callers. Args: None. Returns: None."""
        started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        outcome = []

        def upstream(*args):
            """Block the one owner. Args: ignored. Returns: translated text."""
            started.set()
            if not release.wait(3):
                raise RuntimeError("test gate timed out")
            return "translated"

        def owner():
            """Call with a longer budget. Args: None. Returns: None."""
            try:
                with budget_scope(RequestBudget(seconds=3)):
                    outcome.append(pipeline.generate("source", 1, 4))
            except Exception as exc:
                outcome.append(exc)
            finally:
                done.set()

        with mock.patch.object(pipeline, "_generate_upstream", side_effect=upstream) as run:
            thread = threading.Thread(target=owner)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                with budget_scope(RequestBudget(seconds=0.02)), self.assertRaises(RequestDeadlineExceeded):
                    pipeline.generate("source", 1, 4)
                self.assertEqual(len(pipeline._inflight), 1)
                release.set()
                self.assertTrue(done.wait(2))
                self.assertEqual(outcome, ["translated"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(pipeline._inflight, {})
            finally:
                release.set()
                thread.join(3)

    def test_long_waiter_can_take_over_after_short_owner_stops(self):
        """Another caller's control failure must not poison ours. Args: None. Returns: None."""
        for stop in ("cancel", "deadline"):
            with self.subTest(stop=stop):
                entered = threading.Event()
                waiting = threading.Event()
                release = threading.Event()
                first_budget = RequestBudget(seconds=3)
                follower_budget = RequestBudget(seconds=3)
                original_deadline = follower_budget.deadline
                outcomes = {}
                calls = []

                def upstream(*args):
                    """Stop first owner, serve its follower. Args: ignored. Returns: text."""
                    calls.append(True)
                    if len(calls) == 1:
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError("test gate not released")
                        if stop == "cancel":
                            first_budget.cancel()
                        else:
                            first_budget.deadline = 0
                        first_budget.check("test owner")
                    return "follower translation"

                original_wait = follower_budget.wait

                def follower_wait(*args, **kwargs):
                    """Signal arrival before waiting. Args: event arguments. Returns: bool."""
                    waiting.set()
                    return original_wait(*args, **kwargs)

                def call(name, budget):
                    """Capture each caller's own outcome. Args: name, budget. Returns: None."""
                    try:
                        with budget_scope(budget):
                            outcomes[name] = pipeline.generate("source", 1, 4)
                    except Exception as exc:
                        outcomes[name] = exc

                with mock.patch.object(pipeline, "_generate_upstream", side_effect=upstream), \
                        mock.patch.object(follower_budget, "wait", side_effect=follower_wait):
                    owner = threading.Thread(target=call, args=("owner", first_budget))
                    follower = threading.Thread(target=call, args=("follower", follower_budget))
                    owner.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        follower.start()
                        self.assertTrue(waiting.wait(1))
                        release.set()
                        owner.join(2)
                        follower.join(2)
                        expected = RequestCancelled if stop == "cancel" else RequestDeadlineExceeded
                        self.assertIsInstance(outcomes["owner"], expected)
                        self.assertEqual(outcomes["follower"], "follower translation")
                        self.assertEqual(follower_budget.deadline, original_deadline)
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(pipeline._inflight, {})
                    finally:
                        release.set()
                        first_budget.cancel()
                        follower_budget.cancel()
                        owner.join(3)
                        if follower.ident is not None:
                            follower.join(3)

    def test_shared_error_is_preserved_not_returned_as_none(self):
        """Wake followers with the actual terminal failure. Args: None. Returns: None."""
        event = threading.Event()
        event.set()
        pipeline._inflight[("same-request",)] = {"event": event, "result": None, "error": UpstreamRejection(1100)}
        try:
            with self.assertRaises(UpstreamRejection):
                pipeline.generate("source", 1, 4)
        finally:
            pipeline._inflight.clear()

    def test_owner_interruption_signals_followers_and_restores_context(self):
        """Even BaseException must settle the shared entry. Args: None. Returns: None."""
        captured = []

        def interrupted(*args):
            """Capture then interrupt. Args: ignored. Returns: never."""
            captured.append(pipeline._inflight[("same-request",)])
            raise KeyboardInterrupt()

        with mock.patch.object(pipeline, "_generate_upstream", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                pipeline.generate("source", 1, 4)
        self.assertEqual(pipeline._inflight, {})
        self.assertTrue(captured[0]["event"].is_set())
        self.assertIsInstance(captured[0]["error"], RuntimeError)
        self.assertIsNone(current_budget())


class GenerationBudgetTests(unittest.TestCase):
    """Assert actual attempt counts rather than only classifying exceptions."""

    def setUp(self):
        """Prevent all session or transport networking. Args: None. Returns: None."""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(CONFIG, {**DEFAULT_CONFIG, "log_requests": False}))
        self.stack.enter_context(mock.patch.object(pipeline, "_refresh_xsrf"))
        self.stack.enter_context(mock.patch.object(pipeline, "get_browser_session", return_value=object()))
        self.stack.enter_context(mock.patch.object(pipeline, "_active_auth_user", return_value=None))
        pipeline._inflight.clear()
        self.answer = "translation result with adequate protocol padding " * 5

    def test_bard_1100_is_attempted_once(self):
        """Do not replay an unchanged bad attachment. Args: None. Returns: None."""
        with mock.patch.object(pipeline, "_stream_upstream_chunks", return_value=iter(["BardErrorInfo [1100]"])) as send:
            with self.assertRaises(UpstreamRejection):
                pipeline.generate("source", 1, 4)
            self.assertEqual(send.call_count, 1)

    def test_programming_error_is_attempted_once(self):
        """Unknown local failures do not cause network fan-out. Args: None. Returns: None."""
        with mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=TypeError("bug")) as send:
            with self.assertRaises(TypeError):
                pipeline.generate("source", 1, 4)
            self.assertEqual(send.call_count, 1)

    def test_transient_connection_failure_can_retry_before_output(self):
        """Keep useful retries under the budget. Args: None. Returns: None."""
        with mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=[ConnectionError("reset"), iter([wrb(self.answer)])]) as send:
            self.assertEqual(pipeline.generate("source", 1, 4), self.answer.strip())
            self.assertEqual(send.call_count, 2)

    def test_retry_after_larger_than_total_budget_does_not_sleep_or_retry(self):
        """Honor server retry hints without extending the request. Args: None. Returns: None."""
        failure = RuntimeError("server throttled")
        failure.response = SimpleNamespace(status_code=503, headers={"Retry-After": "120"})
        with budget_scope(RequestBudget(seconds=5)), \
                mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=failure) as send:
            with self.assertRaises(RequestDeadlineExceeded):
                pipeline.generate("source", 1, 4)
            self.assertEqual(send.call_count, 1)

    def test_stream_failure_after_first_delta_never_replays(self):
        """No duplicate text or fresh generation after delivery. Args: None. Returns: None."""
        closed = []

        def chunks(*args):
            """Emit then break. Args: ignored. Yields: one protocol line."""
            try:
                yield wrb(self.answer)
                raise ConnectionError("reset after content")
            finally:
                closed.append(True)

        with mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=chunks) as send:
            stream = pipeline.generate_stream("source", 1, 4)
            self.assertEqual(next(stream), self.answer)
            with self.assertRaises(ConnectionError):
                next(stream)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(closed, [True])

    def test_closing_stream_releases_response_and_capacity(self):
        """Explicit cancellation unwinds generator resources. Args: None. Returns: None."""
        CONFIG["max_concurrent_requests"] = 1
        closed = []

        def chunks(*args):
            """A closable upstream iterator. Args: ignored. Yields: protocol lines."""
            try:
                yield wrb(self.answer)
                yield wrb(self.answer + "tail")
            finally:
                closed.append(True)

        with mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=chunks):
            stream = pipeline.generate_stream("source", 1, 4)
            next(stream)
            stream.close()
            self.assertEqual(closed, [True])
            with concurrency._UpstreamSlot():
                pass
        self.assertIsNone(current_budget())

    def test_interleaved_library_streams_do_not_share_implicit_budget(self):
        """Yield must restore thread context between independent callers. Args: None. Returns: None."""
        budgets = []

        def chunks(*args):
            """Capture the active budget per stream. Args: ignored. Yields: snapshots."""
            budgets.append(current_budget())
            yield wrb(self.answer)
            yield wrb(self.answer + "tail")

        with mock.patch.object(pipeline, "_stream_upstream_chunks", side_effect=chunks):
            first = pipeline.generate_stream("one", 1, 4)
            second = pipeline.generate_stream("two", 1, 4)
            try:
                self.assertIsNone(current_budget())
                next(first)
                self.assertIsNone(current_budget())
                next(second)
                self.assertIsNone(current_budget())
                self.assertEqual(len(budgets), 2)
                self.assertIsNot(budgets[0], budgets[1])
                budgets[0].deadline = 0
                with self.assertRaises(RequestDeadlineExceeded):
                    next(first)
                self.assertEqual(next(second), "tail")
                self.assertIsNone(current_budget())
            finally:
                first.close()
                second.close()
            self.assertIsNone(current_budget())

    def test_attempt_timeout_remains_finite_when_slow_breaker_disabled(self):
        """Do not accidentally pass timeout=None to httpx. Args: None. Returns: None."""
        CONFIG["slow_retry_sec"] = 0
        with budget_scope(RequestBudget(seconds=5)):
            timeout = pipeline._per_attempt_timeout()
            self.assertIsNotNone(timeout)
            self.assertLessEqual(getattr(timeout, "read", timeout), 5)

    def test_upload_time_reduces_both_scotty_step_timeouts(self):
        """Uploads use remaining time, not 90s per step. Args: None. Returns: None."""
        from gemini_web2api import multimodal
        clock = [100.0]
        responses = [SimpleNamespace(status_code=200, headers={"x-goog-upload-url": "https://push.clients6.google.com/f"},
                                     text="", close=mock.Mock()),
                     SimpleNamespace(status_code=200, headers={}, text="/contrib_service/file", close=mock.Mock())]
        timeouts = []

        def post(*args, **kwargs):
            """Consume simulated transport time. Args: request arguments. Returns: response."""
            timeouts.append(kwargs["timeout"])
            clock[0] += 2
            return responses[len(timeouts) - 1]

        session = SimpleNamespace(post=post, curl_options={})
        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(multimodal, "get_browser_session", return_value=session), \
                mock.patch.object(multimodal, "_cached_page_tokens", return_value={"push_id": "p"}), \
                mock.patch.object(multimodal, "load_cookie", return_value=("", None)), \
                mock.patch.object(multimodal, "_active_auth_user", return_value=None), \
                budget_scope(RequestBudget(seconds=5)):
            self.assertEqual(multimodal.upload_image(b"image"), "/contrib_service/file")
        self.assertEqual(timeouts, [5, 3])
        self.assertTrue(all(r.close.called for r in responses))

    def test_download_budget_errors_are_not_swallowed_as_empty_bytes(self):
        """Keep total deadline failure distinct from bad images. Args: None. Returns: None."""
        from gemini_web2api import image_fetch
        with budget_scope(RequestBudget(seconds=5)), \
                mock.patch.object(image_fetch, "_open_image_url", side_effect=RequestDeadlineExceeded("expired")):
            with self.assertRaises(RequestDeadlineExceeded):
                image_fetch.fetch_image_bytes("https://example.com/image.jpg")


class FallbackReadBudgetTests(unittest.TestCase):
    """Fallback read loops check deadlines between raw reads."""

    def test_trickle_read_cannot_reset_budget(self):
        """Reject continuing data after the original deadline. Args: None. Returns: None."""
        from gemini_web2api.upstream.transport import read_urllib_response
        clock = [0.0]
        stream = io.BytesIO(b"data")

        def read1(size):
            """Advance the clock for one read. Args: size. Returns: one byte."""
            clock[0] += 0.4
            return stream.read(1)

        response = SimpleNamespace(read1=read1)
        with mock.patch("gemini_web2api.budget.time.monotonic", side_effect=lambda: clock[0]), \
                budget_scope(RequestBudget(seconds=1)):
            with self.assertRaises(RequestDeadlineExceeded):
                read_urllib_response(response, "fallback")
