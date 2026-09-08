"""One monotonic time budget across queueing, uploads, generation and retries.

Budgets are explicitly carried into shared batch workers. Waiter cancellation
never cancels another request merely sharing the same upstream generation.
Transport adapters also cap their own timeouts to the remaining budget.
"""
import math
import threading
import time
from contextlib import contextmanager

from .config import CONFIG


class RequestControlError(RuntimeError):
    """A terminal control-flow failure; never retry or wrap as an upstream error."""

    status = 504
    code = "request_timeout"


class RequestDeadlineExceeded(RequestControlError):
    """The request's overall monotonic deadline was reached."""


class RequestCancelled(RequestControlError):
    """The request no longer has an interested caller."""

    status = 499
    code = "request_cancelled"


class QueueTimeout(RequestControlError):
    """No upstream slot became available within the queue wait limit."""

    status = 503
    code = "queue_timeout"


class QueueFull(RequestControlError):
    """The configured maximum number of queued requests was reached."""

    status = 503
    code = "queue_full"


def positive_seconds(key, default):
    """Read a positive finite setting. Args: key, fallback default. Returns: seconds."""
    value = CONFIG.get(key, default)
    if isinstance(value, bool):
        return float(default)
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) and value > 0 else float(default)


class RequestBudget:
    """Absolute request deadline plus explicit cooperative cancellation."""

    def __init__(self, seconds=None, deadline=None, cancel_check=None):
        """Create a budget. Args: seconds or absolute deadline, cancel callback. Returns: None."""
        seconds = positive_seconds("request_deadline_sec", 180) if seconds is None else float(seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("request budget must be positive and finite")
        self.deadline = time.monotonic() + seconds if deadline is None else float(deadline)
        if not math.isfinite(self.deadline):
            raise ValueError("request deadline must be finite")
        self.cancelled = threading.Event()
        self.cancel_check = cancel_check

    def is_active(self):
        """Inspect without raising. Args: None. Returns: whether this caller still has time."""
        return not self.cancelled.is_set() and time.monotonic() < self.deadline

    def cancel(self):
        """Mark cancellation without affecting other budgets. Args: None. Returns: None."""
        self.cancelled.set()

    def remaining(self, stage="request"):
        """Check cancellation and deadline. Args: safe stage label. Returns: remaining seconds."""
        if self.cancelled.is_set() or (self.cancel_check is not None and self.cancel_check()):
            raise RequestCancelled(f"request cancelled during {stage}")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestDeadlineExceeded(f"request deadline exceeded during {stage}")
        return remaining

    def check(self, stage="request"):
        """Raise on deadline/cancellation. Args: safe stage label. Returns: None."""
        self.remaining(stage)

    def wait(self, event, stage="waiting", limit=None):
        """Wait without resetting the request budget. Args: event, stage, local limit. Returns: bool."""
        until = self.deadline if limit is None else min(self.deadline, time.monotonic() + limit)
        while True:
            remaining = self.remaining(stage)
            if event.is_set():
                return True
            local_left = until - time.monotonic()
            if local_left <= 0:
                return False
            if event.wait(min(remaining, local_left, 0.1)):
                self.check(stage)
                return True

    def sleep(self, seconds, stage="retry backoff"):
        """Wait a retry delay within the same budget. Args: seconds, stage. Returns: None."""
        if seconds < 0 or not math.isfinite(seconds):
            raise ValueError("retry delay must be nonnegative and finite")
        if seconds >= self.remaining(stage):
            raise RequestDeadlineExceeded("insufficient request budget for retry backoff")
        until = time.monotonic() + seconds
        while True:
            remaining = self.remaining(stage)
            left = until - time.monotonic()
            if left <= 0:
                return
            self.cancelled.wait(min(remaining, left, 0.1))


_context = threading.local()


def current_budget():
    """Read the current thread budget. Args: None. Returns: budget or None."""
    return getattr(_context, "budget", None)


@contextmanager
def budget_scope(budget=None, check=True):
    """Bind/reuse a budget and restore the prior context.

    Args:
        budget: Explicit budget for HTTP or batch workers; None reuses an
            existing budget or creates one for direct library calls.
        check: False only for cleanup, which must run even after expiry.

    Yields:
        The selected RequestBudget. The same budget is reused by nested calls.
    """
    previous = current_budget()
    selected = budget if budget is not None else (previous or RequestBudget())
    _context.budget = selected
    try:
        if check:
            selected.check()
        yield selected
    finally:
        _context.budget = previous


def check_budget(stage):
    """Check the active budget if bound. Args: stage label. Returns: None."""
    budget = current_budget()
    if budget is not None:
        budget.check(stage)


def remaining_timeout(cap, stage):
    """Cap a transport timeout by remaining request time. Args: cap, stage. Returns: seconds."""
    budget = current_budget()
    return min(float(cap), budget.remaining(stage)) if budget is not None else float(cap)


@contextmanager
def budget_lock(lock, stage):
    """Acquire a potentially contended lock within the current budget.

    Args:
        lock: A lock exposing acquire(timeout) and release().
        stage: Safe operation name used in timeout errors.

    Yields:
        None while the lock is held; always releases on exception/cancellation.
    """
    budget = current_budget()
    if budget is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=min(0.1, budget.remaining(stage))):
            pass
    try:
        check_budget(stage)
        yield
    finally:
        lock.release()
