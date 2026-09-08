"""Deterministic account isolation tests; filesystem and upstream I/O are mocked."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api import keepalive, multimodal
from gemini_web2api.config import CONFIG
from gemini_web2api.upstream import cookies, history, protocol


class _MemoryFile:
    """Minimal in-memory text file with observable flush and fsync ordering."""

    def __init__(self, fs, path, mode):
        """Bind a handle to a mocked file.

        Args:
            fs: Owning memory filesystem.
            path: File key.
            mode: Open mode.
        Returns:
            None.
        """
        self.fs, self.path, self.mode = fs, path, mode

    def __enter__(self):
        """Enter an open file scope.

        Args:
            None.
        Returns:
            This handle.
        """
        return self

    def __exit__(self, *exc):
        """Close without suppressing exceptions.

        Args:
            exc: Context manager exception information.
        Returns:
            False.
        """
        return False

    def read(self):
        """Read the current complete memory contents.

        Args:
            None.
        Returns:
            Text stored at the path.
        """
        self.fs.reads.append(self.path)
        return self.fs.files[self.path]

    def write(self, text):
        """Write after an optional event-controlled concurrency gate.

        Args:
            text: Serialized document to write.
        Returns:
            Number of characters written.
        """
        gate = self.fs.gate
        if gate and threading.current_thread().name == gate[0]:
            gate[1].set()
            if not gate[2].wait(3):
                raise AssertionError("writer was never released")
        self.fs.files[self.path] = text
        self.fs.mtimes[self.path] += 1
        self.fs.operations.append(("write", self.path))
        return len(text)

    def flush(self):
        """Record a user-space buffer flush.

        Args:
            None.
        Returns:
            None.
        """
        self.fs.operations.append(("flush", self.path))

    def fileno(self):
        """Supply a fake descriptor to the mocked fsync.

        Args:
            None.
        Returns:
            The path used as a fake descriptor.
        """
        return self.path


class _MemoryFS:
    """A pure mock filesystem; no test touches a real cookie file."""

    def __init__(self, documents):
        """Initialize independent account documents.

        Args:
            documents: Path to JSON document mapping.
        Returns:
            None.
        """
        self.files = {p: json.dumps(data) for p, data in documents.items()}
        self.mtimes = dict.fromkeys(documents, 1.0)
        self.operations, self.reads = [], []
        self.gate = None
        self.os = SimpleNamespace(
            path=SimpleNamespace(exists=self.exists, getmtime=self.getmtime),
            chmod=mock.Mock(side_effect=self.chmod),
            fsync=mock.Mock(side_effect=self.fsync),
        )

    def exists(self, path):
        """Check whether a fake file exists.

        Args:
            path: File key.
        Returns:
            Boolean existence flag.
        """
        return path in self.files

    def getmtime(self, path):
        """Read deterministic modification time.

        Args:
            path: File key.
        Returns:
            Monotonic fake mtime.
        """
        return self.mtimes[path]

    def open(self, path, mode="r", encoding=None):
        """Open a memory handle, modeling truncate-before-write accurately.

        Args:
            path: File key.
            mode: Read or write mode.
            encoding: Accepted for builtins.open compatibility.
        Returns:
            Memory handle.
        """
        if "w" in mode:
            self.files[path] = ""
            self.mtimes[path] = self.mtimes.get(path, 0) + 1
        elif path not in self.files:
            raise FileNotFoundError(path)
        return _MemoryFile(self, path, mode)

    def chmod(self, path, mode):
        """Record permission hardening.

        Args:
            path: File key.
            mode: Requested permission bits.
        Returns:
            None.
        """
        self.operations.append(("chmod", path, mode))

    def fsync(self, fd):
        """Record a durable flush without any real disk I/O.

        Args:
            fd: Fake descriptor returned by the memory handle.
        Returns:
            None.
        """
        self.operations.append(("fsync", fd))

    def document(self, path):
        """Decode a complete JSON document for assertions.

        Args:
            path: File key.
        Returns:
            Decoded document.
        """
        return json.loads(self.files[path])

    def replace(self, path, data):
        """Simulate an external cookie export and deterministic mtime change.

        Args:
            path: File key.
            data: Replacement JSON document or raw cookie text.
        Returns:
            None.
        """
        self.files[path] = data if isinstance(data, str) else json.dumps(data)
        self.mtimes[path] += 1


class _ObservedLock:
    """Expose lock attempts so concurrency tests need no scheduling sleeps."""

    def __init__(self, lock, events):
        """Wrap the production reentrant lock.

        Args:
            lock: Shared production lock.
            events: Thread name to attempted-acquire event mapping.
        Returns:
            None.
        """
        self.lock, self.events = lock, events

    def __enter__(self):
        """Signal the attempt before acquiring the lock.

        Args:
            None.
        Returns:
            This wrapper after acquisition.
        """
        event = self.events.get(threading.current_thread().name)
        if event is not None:
            event.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        """Release the wrapped lock.

        Args:
            exc: Context manager exception information.
        Returns:
            False.
        """
        self.lock.release()
        return False


def _response(**updates):
    """Build a mock response carrying separate Set-Cookie headers.

    Args:
        updates: Cookie names and replacement values.
    Returns:
        Mock response with HTTP 200 and a headers.get_list interface.
    """
    return SimpleNamespace(
        status_code=200,
        headers=SimpleNamespace(get_list=mock.Mock(return_value=[
            f"{key}={value}; Secure; Path=/" for key, value in updates.items()
        ])),
    )


class _AccountCase(unittest.TestCase):
    """Isolate every module singleton and all upstream/file I/O per test."""

    def setUp(self):
        """Install a pure mock two-account environment.

        Args:
            None.
        Returns:
            None.
        """
        self.a, self.b = "/mock/account-a.json", "/mock/account-b.json"
        self.fs = _MemoryFS({
            self.a: {"cookie": "SID=A; SAPISID=SA; SIDCC=old-a", "sapisid": "SA",
                     "auth_user": 0, "xsrf_token": "AOvx-export-a", "metadata": {"keep": True},
                     "accounts_cookie": "SID=AC-A; SIDCC=old-a; SAPISID=AC-SA"},
            self.b: {"cookie": "SID=B; SAPISID=SB; SIDCC=old-b", "sapisid": "SB",
                     "auth_user": 2, "accounts_cookie": "SID=AC-B; SIDCC=old-b"},
        })
        self.patch(mock.patch.dict(CONFIG, {
            "cookie_file": None, "cookie_files": [self.a, self.b],
            "xsrf_token": "AOvx-global-a", "auth_user": 9,
            "keepalive_sec": 540, "auto_delete_history": True,
            "temporary_chats": False, "log_requests": False,
        }))
        self.patch(mock.patch.object(cookies, "_active_cookie", threading.local()))
        for mapping in (cookies._cookie_caches, cookies._account_state,
                        keepalive._last_cookie_persist, keepalive._xsrf_refreshed_at):
            self.patch(mock.patch.dict(mapping, {}, clear=True))
        self.patch(mock.patch.dict(cookies._round_robin, {"i": 0}, clear=True))
        self.patch(mock.patch.dict(keepalive._keepalive_on, {"started": False}, clear=True))
        for module in (cookies, keepalive):
            self.patch(mock.patch.object(module, "os", self.fs.os))
            self.patch(mock.patch.object(module, "open", self.fs.open, create=True))
            self.patch(mock.patch.object(module, "log"))
        self.clock = SimpleNamespace(time=mock.Mock(return_value=1000.0), sleep=mock.Mock())
        self.patch(mock.patch.object(keepalive, "time", self.clock))
        self.patch(mock.patch.object(protocol, "time", self.clock))
        self.patch(mock.patch.object(history, "log"))
        for module, name in ((keepalive, "get_browser_session"),
                             (multimodal, "get_browser_session"),
                             (history, "_get_httpx_client"), (history, "_urllib_post"),
                             (keepalive, "generate")):
            self.patch(mock.patch.object(module, name, side_effect=AssertionError("unexpected network")))
        self.patch(mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")))

    def patch(self, patcher):
        """Start a patch with guaranteed cleanup, including on failed assertions.

        Args:
            patcher: unittest.mock patch object.
        Returns:
            The patched value or mock.
        """
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def assert_cache_matches(self, path):
        """Assert that a committed cache describes the exact persisted file.

        Args:
            path: Account file key.
        Returns:
            None.
        """
        data, cache = self.fs.document(path), cookies._cookie_caches[path]
        self.assertEqual(cache["str"], data["cookie"])
        self.assertEqual(cache["sapisid"], data.get("sapisid"))
        self.assertEqual(cache["auth_user"], data.get("auth_user"))
        self.assertEqual(cache["mtime"], self.fs.mtimes[path])


class AccountIdentityTests(_AccountCase):
    """Keep URLs, headers, page tokens and history bound to one account."""

    def test_cold_urls_and_headers_load_file_auth_user(self):
        """Resolve file auth_user even when no earlier code loaded cookies.

        Args:
            None.
        Returns:
            None.
        """
        for builder in (protocol._get_url, protocol._delete_url, protocol._build_headers):
            with self.subTest(builder=builder.__name__):
                cookies._cookie_caches.clear()
                cookies.set_active_cookie(self.b)
                result = builder()
                if isinstance(result, str):
                    self.assertIn("/u/2/_/", result)
                else:
                    self.assertEqual(result["X-Goog-AuthUser"], "2")
                    self.assertEqual(result["Referer"], "https://gemini.google.com/u/2/app")
                    self.assertIn("SID=B", result["Cookie"])
                    self.assertTrue(result["Authorization"].startswith("SAPISIDHASH 1000_"))

    def test_round_robin_deduplicates_and_clears_overrides(self):
        """Do not carry a previous request's auth-user override into the pool.

        Args:
            None.
        Returns:
            None.
        """
        CONFIG.update(cookie_files=[self.a, self.b, self.a], cookie_file=self.a)
        for path, auth in ((self.a, 0), (self.b, 2), (self.a, 0)):
            cookies.set_active_auth_user(7)
            cookies.pick_next_cookie()
            self.assertEqual(cookies._active_cookie_path(), path)
            self.assertEqual(cookies._active_auth_user(), auth)

    def test_nested_context_restores_original_auth_user(self):
        """Restore loaded metadata and explicit overrides after nested failure.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.load_cookie()
        cookies.set_active_auth_user(7)
        before = dict(cookies._active_cookie.__dict__)
        with self.assertRaisesRegex(RuntimeError, "nested"):
            with cookies.use_cookie(self.b):
                self.assertEqual(cookies._active_auth_user(), 2)
                with cookies.use_cookie(self.a):
                    self.assertEqual(cookies._active_auth_user(), 0)
                self.assertEqual(cookies._active_auth_user(), 2)
                raise RuntimeError("nested")
        self.assertEqual(cookies._active_cookie.__dict__, before)
        self.assertEqual(cookies._active_auth_user(), 7)
        cookies.set_active_auth_user(None)
        self.assertEqual(cookies._active_auth_user(), 0)
        cookies.restore_active_cookie(None)
        self.assertEqual(cookies._active_cookie.__dict__, {})

    def test_xsrf_export_and_refresh_are_isolated_without_global_pool_fallback(self):
        """Never put account A's global token into a tokenless pool B payload.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        self.assertEqual(parse_qs(protocol._build_payload("a", 1, 4))["at"], ["AOvx-export-a"])
        cookies.set_active_xsrf_token("AOvx-live-a")
        cookies.set_active_cookie(self.b)
        self.assertNotIn("at", parse_qs(protocol._build_payload("b", 1, 4)))
        cookies.set_active_xsrf_token("AOvx-live-b")
        self.assertEqual(parse_qs(protocol._build_payload("b", 1, 4))["at"], ["AOvx-live-b"])
        with cookies.use_cookie(self.a):
            self.assertEqual(cookies.get_active_xsrf_token(), "AOvx-live-a")
        self.assertEqual(CONFIG["xsrf_token"], "AOvx-global-a")

    def test_single_account_plain_cookie_and_global_token_remain_compatible(self):
        """Retain raw cookie files, global auth_user and legacy XSRF support.

        Args:
            None.
        Returns:
            None.
        """
        CONFIG.update(cookie_files=[], cookie_file=self.a, auth_user=4)
        self.fs.replace(self.a, "SID=A;SAPISID=SA")
        headers = protocol._build_headers()
        self.assertEqual(headers["Cookie"], "SID=A;SAPISID=SA")
        self.assertEqual(headers["X-Goog-AuthUser"], "4")
        self.assertEqual(cookies.get_active_xsrf_token(), "AOvx-global-a")
        CONFIG["cookie_file"] = None
        self.assertEqual(cookies.get_active_xsrf_token(), "AOvx-global-a")

    def test_external_export_invalidates_old_live_token(self):
        """Replace same-path session metadata without retaining stale XSRF.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.b)
        cookies.load_cookie()
        cookies.set_active_xsrf_token("AOvx-old-session")
        updated = self.fs.document(self.b)
        updated.update(cookie="SID=B-new; SAPISID=SB-new", sapisid="SB-new",
                       auth_user=3, xsrf_token="AOvx-new-session")
        self.fs.replace(self.b, updated)
        self.assertEqual(cookies.get_active_xsrf_token(), "AOvx-new-session")
        self.assertEqual(cookies._active_auth_user(), 3)

    def test_page_fetch_resolves_cold_account_index(self):
        """Cover page-token consumers that construct a URL before load_cookie.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.b)
        session = mock.Mock()
        session.get.return_value.text = '{"SNlM0e":"AOvx-page-b","qKIAYe":"bucket-b"}'
        with mock.patch.object(multimodal, "get_browser_session", return_value=session):
            self.assertEqual(multimodal._get_page_tokens()["at"], "AOvx-page-b")
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://gemini.google.com/u/2/app")
        self.assertEqual(kwargs["headers"]["X-Goog-AuthUser"], "2")
        self.assertIn("SID=B", kwargs["headers"]["Cookie"])

    def test_xsrf_refresh_throttle_is_per_account(self):
        """Refresh both accounts in one clock tick without mutating CONFIG.

        Args:
            None.
        Returns:
            None.
        """
        with mock.patch.object(multimodal, "_cached_page_tokens", side_effect=[
                {"at": "AOvx-live-a"}, {"at": "AOvx-live-b"}]) as fetch:
            for path in (self.a, self.a, self.b, self.b):
                with cookies.use_cookie(path):
                    keepalive._maybe_refresh_xsrf()
            self.assertEqual(fetch.call_count, 2)
        for path, token in ((self.a, "AOvx-live-a"), (self.b, "AOvx-live-b")):
            with cookies.use_cookie(path):
                self.assertEqual(cookies.get_active_xsrf_token(), token)
        self.assertEqual(CONFIG["xsrf_token"], "AOvx-global-a")

    def test_history_worker_captures_original_account_and_override(self):
        """Do not use the calling thread's later account or a reloaded index.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.set_active_auth_user(7)
        factory = mock.Mock()
        with mock.patch.object(history, "threading", SimpleNamespace(Thread=factory)):
            history.schedule_history_delete("c_origin")
        factory.return_value.start.assert_called_once_with()
        pending = factory.call_args.kwargs
        self.assertEqual(pending["args"], ("c_origin", self.a, 7))
        cookies.set_active_cookie(self.b)
        cookies.load_cookie()
        before = dict(cookies._active_cookie.__dict__)
        client = mock.Mock()
        client.post.return_value.text = "[]"
        with mock.patch.object(history, "HAS_HTTPX", True),                 mock.patch.object(history, "_get_httpx_client", return_value=client):
            self.assertTrue(pending["target"](*pending["args"]))
        args, kwargs = client.post.call_args
        self.assertIn("/u/7/_/", args[0])
        self.assertIn("SID=A", kwargs["headers"]["Cookie"])
        self.assertEqual(kwargs["headers"]["X-Goog-AuthUser"], "7")
        self.assertEqual(parse_qs(kwargs["content"])["at"], ["AOvx-export-a"])
        self.assertEqual(cookies._active_cookie.__dict__, before)

    def test_history_transport_failure_restores_context(self):
        """Preserve the caller's explicit auth_user after best-effort failure.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.set_active_auth_user(7)
        before = dict(cookies._active_cookie.__dict__)
        with mock.patch.object(history, "HAS_HTTPX", False),                 mock.patch.object(history, "_urllib_post", side_effect=OSError("mock offline")):
            self.assertFalse(history.delete_conversation("c_b", self.b, 2))
        self.assertEqual(cookies._active_cookie.__dict__, before)


class KeepaliveStartupTests(_AccountCase):
    """Do not poison startup state while disabled or when thread.start fails."""

    def test_disabled_then_enabled_starts_exactly_once(self):
        """Validate before setting started, including invalid/non-finite input.

        Args:
            None.
        Returns:
            None.
        """
        factory = mock.Mock()
        with mock.patch.object(keepalive, "threading", SimpleNamespace(Thread=factory)):
            for interval in (0, -1, None, "invalid", float("nan"), float("inf")):
                CONFIG["keepalive_sec"] = interval
                keepalive.start_keepalive()
                self.assertFalse(keepalive._keepalive_on["started"])
            CONFIG.update(keepalive_sec=540, cookie_files=[], cookie_file=None)
            keepalive.start_keepalive()
            self.assertFalse(keepalive._keepalive_on["started"])
            factory.assert_not_called()
            CONFIG["cookie_files"] = [self.a, self.b]
            keepalive.start_keepalive()
            keepalive.start_keepalive()
        factory.assert_called_once()
        factory.return_value.start.assert_called_once_with()
        self.assertTrue(keepalive._keepalive_on["started"])

    def test_concurrent_start_calls_create_only_one_worker(self):
        """Synchronize startup callers without permitting a real daemon launch.

        Args:
            None.
        Returns:
            None.
        """
        factory = mock.Mock()
        barrier = threading.Barrier(3)
        failures = []

        def start_worker():
            """Wait for both callers and invoke the public startup function.

            Args:
                None.
            Returns:
                None.
            """
            try:
                barrier.wait(3)
                keepalive.start_keepalive()
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(keepalive, "threading", SimpleNamespace(Thread=factory)):
            workers = [threading.Thread(target=start_worker, daemon=True) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait(3)
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        factory.assert_called_once()
        factory.return_value.start.assert_called_once_with()
        self.assertTrue(keepalive._keepalive_on["started"])

    def test_thread_start_failure_is_retryable(self):
        """Only record started after the real start operation succeeds.

        Args:
            None.
        Returns:
            None.
        """
        factory = mock.Mock()
        factory.return_value.start.side_effect = RuntimeError("mock start failure")
        with mock.patch.object(keepalive, "threading", SimpleNamespace(Thread=factory)):
            with self.assertRaisesRegex(RuntimeError, "mock start failure"):
                keepalive.start_keepalive()
            self.assertFalse(keepalive._keepalive_on["started"])
            factory.return_value.start.side_effect = None
            keepalive.start_keepalive()
        self.assertTrue(keepalive._keepalive_on["started"])
        self.assertEqual(factory.call_count, 2)

    def test_loop_visits_all_accounts_even_if_one_fails(self):
        """Run exactly one event-free tick without sleeping or spawning a daemon.

        Args:
            None.
        Returns:
            None.
        """
        factory = mock.Mock()
        self.clock.sleep.side_effect = [None, KeyboardInterrupt]
        with mock.patch.object(keepalive, "threading", SimpleNamespace(Thread=factory)),                 mock.patch.object(keepalive, "_rotate_psidts", side_effect=[RuntimeError("a"), True]) as rotate:
            keepalive.start_keepalive()
            with self.assertRaises(KeyboardInterrupt):
                factory.call_args.kwargs["target"]()
            self.assertEqual(rotate.call_args_list, [mock.call(self.a), mock.call(self.b)])

    def test_rotation_uses_target_account_and_restores_caller(self):
        """Rotate B using its accounts-domain jar without altering A's context.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.set_active_auth_user(7)
        before = dict(cookies._active_cookie.__dict__)
        original_a = self.fs.files[self.a]
        session = mock.Mock()
        session.post.return_value = _response(SIDCC="renewed-b")
        with mock.patch.object(keepalive, "get_browser_session", return_value=session),                 mock.patch.object(keepalive, "_maybe_refresh_xsrf"):
            self.assertTrue(keepalive._rotate_psidts(self.b))
        self.assertEqual(session.post.call_args.kwargs["headers"]["Cookie"],
                         "SID=AC-B; SIDCC=old-b")
        self.assertIn("SIDCC=renewed-b", self.fs.document(self.b)["cookie"])
        self.assertIn("SIDCC=renewed-b", self.fs.document(self.b)["accounts_cookie"])
        self.assertEqual(self.fs.files[self.a], original_a)
        self.assertEqual(cookies._active_cookie.__dict__, before)
        self.assert_cache_matches(self.b)

    def test_rotation_fallback_keeps_target_account(self):
        """Keep the same account when RotateCookies falls back to generation.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.set_active_auth_user(7)
        session = mock.Mock()
        session.post.return_value = SimpleNamespace(status_code=401)

        def heartbeat(*args):
            """Observe the fallback account without calling Google.

            Args:
                args: Generate arguments.
            Returns:
                Mock completion text.
            """
            self.assertEqual(cookies._active_cookie_path(), self.b)
            self.assertEqual(cookies._active_auth_user(), 2)
            return "ok"

        with mock.patch.object(keepalive, "get_browser_session", return_value=session),                 mock.patch.object(keepalive, "generate", side_effect=heartbeat):
            self.assertTrue(keepalive._rotate_psidts(self.b))
        self.assertEqual(cookies._active_cookie_path(), self.a)
        self.assertEqual(cookies._active_auth_user(), 7)


class CookiePersistenceTests(_AccountCase):
    """Prove both writers share one durability/cache consistency boundary."""

    def test_per_account_throttling_and_fsync_cache_publication(self):
        """Persist each account once, retaining metadata and exact cache data.

        Args:
            None.
        Returns:
            None.
        """
        for path, sid, index in ((self.a, "SA-new", 0), (self.b, "SB-new", 2)):
            with cookies.use_cookie(path):
                cookies.load_cookie()
            keepalive._persist_cookie_file(path, f"SAPISID={sid}", sid, index)
            keepalive._persist_cookie_file(path, "SHOULD=be-throttled", None, None)
            self.assert_cache_matches(path)
            self.assertEqual(self.fs.document(path)["sapisid"], sid)
            self.assertIn("accounts_cookie", self.fs.document(path))
        self.assertEqual(self.fs.document(self.a)["metadata"], {"keep": True})
        self.assertEqual(self.fs.os.fsync.call_count, 2)
        for path in (self.a, self.b):
            operations = [entry[0] for entry in self.fs.operations if entry[1] == path]
            self.assertEqual(operations, ["write", "flush", "chmod", "fsync"])
        self.fs.os.chmod.assert_any_call(self.a, 0o600)

    def test_accounts_sync_flushes_throttled_main_cookie(self):
        """An accounts-only write must not discard newer unpersisted main cookies.

        Args:
            None.
        Returns:
            None.
        """
        cookies.set_active_cookie(self.a)
        cookies.load_cookie()
        keepalive._last_cookie_persist[self.a] = self.clock.time()
        response = _response(SIDCC="renewed-a", SAPISID="SA-new")
        keepalive._merge_response_cookies(response)
        self.fs.os.fsync.assert_not_called()
        self.assertIn("SIDCC=old-a", self.fs.document(self.a)["cookie"])
        keepalive._sync_accounts_cookie(response)
        self.assert_cache_matches(self.a)
        self.assertIn("SIDCC=renewed-a", self.fs.document(self.a)["cookie"])
        self.assertIn("SIDCC=renewed-a", self.fs.document(self.a)["accounts_cookie"])
        self.assertEqual(self.fs.document(self.a)["sapisid"], "SA-new")
        reads = len(self.fs.reads)
        self.assertIn("SIDCC=renewed-a", cookies.load_cookie()[0])
        self.assertEqual(len(self.fs.reads), reads)
        self.fs.os.fsync.assert_called_once_with(self.a)

    def test_fsync_failure_does_not_publish_cache_or_throttle(self):
        """A failed durable flush must not claim successful publication.

        Args:
            None.
        Returns:
            None.
        """
        for path, sync in ((self.a, False), (self.b, True)):
            with cookies.use_cookie(path):
                cookies.load_cookie()
                before = dict(cookies._cookie_caches[path])
                self.fs.os.fsync.side_effect = OSError("mock fsync failure")
                if sync:
                    keepalive._sync_accounts_cookie(_response(SIDCC="changed"))
                else:
                    keepalive._persist_cookie_file(path, "SID=changed", None, None)
                self.assertNotIn(path, keepalive._last_cookie_persist)
                self.assertEqual(cookies._cookie_caches[path], before)
        keepalive.log.assert_called()

    def _race_writers(self, first_sync):
        """Force a second writer and reader into the first writer's truncate window.

        Args:
            first_sync: Whether accounts sync runs before main persistence.
        Returns:
            None after asserting linearizable reads and lossless final state.
        """
        cookies.set_active_cookie(self.a)
        old_cookie, _ = cookies.load_cookie()
        new_cookie = "SID=A-new; SAPISID=SA-new; SIDCC=main-new"
        entered, release = threading.Event(), threading.Event()
        attempts = {name: threading.Event() for name in ("second", "reader")}
        self.assertIs(keepalive._cookie_write_lock, cookies._cookie_lock)
        observed = _ObservedLock(cookies._cookie_lock, attempts)
        self.patch(mock.patch.object(cookies, "_cookie_lock", observed))
        self.patch(mock.patch.object(keepalive, "_cookie_write_lock", observed))
        self.fs.gate = ("first", entered, release)
        errors, read_results = [], []

        def worker(kind):
            """Perform one bound-account operation and capture worker failures.

            Args:
                kind: sync, persist, or read operation.
            Returns:
                None.
            """
            try:
                with cookies.use_cookie(self.a):
                    if kind == "sync":
                        keepalive._sync_accounts_cookie(_response(SIDCC="account-new"))
                    elif kind == "persist":
                        keepalive._persist_cookie_file(self.a, new_cookie, "SA-new", 0, min_interval=0)
                    else:
                        read_results.append(cookies.load_cookie())
            except BaseException as exc:
                errors.append(exc)

        kinds = ("sync", "persist") if first_sync else ("persist", "sync")
        workers = [threading.Thread(target=worker, args=(kind,), name=name, daemon=True)
                   for name, kind in (("first", kinds[0]), ("second", kinds[1]), ("reader", "read"))]
        workers[0].start()
        try:
            self.assertTrue(entered.wait(3), "first writer never reached truncate window")
            workers[1].start()
            workers[2].start()
            for event in attempts.values():
                self.assertTrue(event.wait(3), "concurrent operation did not use shared lock")
            self.assertEqual(self.fs.files[self.a], "")
            self.assertEqual(read_results, [])
        finally:
            release.set()
            for worker_thread in workers:
                if worker_thread.ident is not None:
                    worker_thread.join(3)
                    self.assertFalse(worker_thread.is_alive(), "worker deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(read_results), 1)
        self.assertIn(read_results[0][0], (old_cookie, new_cookie))
        self.assertEqual(self.fs.document(self.a)["cookie"], new_cookie)
        self.assertIn("SIDCC=account-new", self.fs.document(self.a)["accounts_cookie"])
        self.assertEqual(self.fs.document(self.a)["metadata"], {"keep": True})
        self.assert_cache_matches(self.a)
        self.assertEqual(self.fs.os.fsync.call_count, 2)

    def test_persist_then_concurrent_accounts_sync_and_reader(self):
        """Serialize account sync behind an in-flight main-cookie persistence.

        Args:
            None.
        Returns:
            None.
        """
        self._race_writers(first_sync=False)

    def test_accounts_sync_then_concurrent_persist_and_reader(self):
        """Serialize main persistence behind an in-flight accounts-cookie sync.

        Args:
            None.
        Returns:
            None.
        """
        self._race_writers(first_sync=True)


if __name__ == "__main__":
    unittest.main()
