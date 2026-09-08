"""Per-account upstream capacity with bounded, cancellable queue waits."""
import threading
import time

from ..config import CONFIG
from ..budget import RequestBudget, QueueFull, QueueTimeout, current_budget, positive_seconds
from ..logs import log
from .cookies import _active_cookie_path


_upstream_semaphores = {}
_upstream_sema_lock = threading.Lock()
_upstream_waiters = {}


def _get_semaphore():
    """Return the active account semaphore. Args: None. Returns: semaphore or None when unlimited."""
    limit = int(CONFIG.get("max_concurrent_requests") or 0)
    if limit <= 0:
        return None
    account = _active_cookie_path() or "__anonymous__"
    with _upstream_sema_lock:
        entry = _upstream_semaphores.get(account)
        if entry is None or entry["limit"] != limit:
            entry = {"limit": limit, "semaphore": threading.BoundedSemaphore(limit)}
            _upstream_semaphores[account] = entry
        return entry["semaphore"]


def max_queued_requests():
    """Read a finite queue size. Args: None. Returns: positive maximum queued count."""
    value = CONFIG.get("max_queued_requests", 64)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 64


class _UpstreamSlot:
    """Acquire capacity without starting work after a deadline or cancellation."""

    def __enter__(self):
        """Wait for a slot under both queue and request limits. Args: None. Returns: self."""
        self._sema = _get_semaphore()
        self._acquired = False
        budget = current_budget() or RequestBudget()
        budget.check("upstream queue")
        if self._sema is None:
            return self
        started = time.monotonic()
        account = _active_cookie_path() or "__anonymous__"
        if not self._sema.acquire(blocking=False):
            with _upstream_sema_lock:
                if _upstream_waiters.get(account, 0) >= max_queued_requests():
                    raise QueueFull("upstream queue is full")
                _upstream_waiters[account] = _upstream_waiters.get(account, 0) + 1
            until = started + positive_seconds("queue_timeout_sec", 30)
            try:
                while True:
                    remaining = budget.remaining("upstream queue")
                    local_left = until - time.monotonic()
                    if local_left <= 0:
                        raise QueueTimeout("upstream queue wait timed out")
                    if self._sema.acquire(timeout=min(remaining, local_left, 0.1)):
                        break
            finally:
                with _upstream_sema_lock:
                    count = _upstream_waiters[account] - 1
                    if count:
                        _upstream_waiters[account] = count
                    else:
                        _upstream_waiters.pop(account, None)
        self._acquired = True
        try:
            budget.check("upstream queue")
        except BaseException:
            self._sema.release()
            self._acquired = False
            raise
        waited = time.monotonic() - started
        if waited > 0.5:
            log("Upstream busy: queued %.1fs (account=%s max_concurrent_requests=%s)"
                % (waited, account, CONFIG.get("max_concurrent_requests")))
        return self

    def __exit__(self, *exc):
        """Release exactly one acquired slot. Args: exception tuple. Returns: False."""
        if self._sema is not None and self._acquired:
            self._sema.release()
            self._acquired = False
        return False
