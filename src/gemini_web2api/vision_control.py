"""Cooperative request budgets for browser transport and parallel image uploads."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import wraps

from .budget import RequestBudget, budget_scope, check_budget, current_budget, remaining_timeout
from .upstream.cookies import _active_auth_user, _active_cookie_path, set_active_auth_user, use_cookie


def request_scoped(function):
    """Reuse the caller budget or create one. Args: function. Returns: wrapped callable."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        """Check before and after execution. Args: forwarded arguments. Returns: function result."""
        with budget_scope():
            result = function(*args, **kwargs)
            check_budget('vision completion')
            return result
    return wrapped


def bridge_sleep(seconds):
    """Sleep cooperatively without renewing deadlines. Args: seconds. Returns: None."""
    budget = current_budget()
    if budget is None:
        time.sleep(seconds)
    else:
        budget.sleep(seconds, 'vision page settle')


def receive_cdp(ws, request_id, deadline):
    """Poll a reply under an absolute deadline. Args: socket, id, deadline. Returns: dict."""
    from websocket import WebSocketTimeoutException
    while True:
        check_budget('vision CDP reply')
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('vision CDP reply timed out')
        ws.settimeout(min(0.2, remaining_timeout(left, 'vision CDP reply')))
        try:
            raw = ws.recv()
        except (WebSocketTimeoutException, TimeoutError):
            continue
        check_budget('vision CDP reply')
        if not raw:
            raise ConnectionError('vision CDP connection closed')
        msg = json.loads(raw)
        if msg.get('id') == request_id:
            return msg


def parallel_map(items, fn):
    """Map fn over items concurrently with account/budget context carried.

    Worker threads reuse the caller's cookie account (path + auth_user)
    and share the caller's request budget (deadline + cancellation), so
    per-item work behaves exactly as if it ran on the caller thread.

    Args:
        items: list of work items forwarded to fn.
        fn: callable(item) -> result; runs under the carried context.

    Returns:
        List of fn results in input order.

    Raises:
        The first failure in input order propagates unchanged;
        RequestControlError propagates immediately.
    """
    with budget_scope() as parent:
        path, auth_user = _active_cookie_path(), _active_auth_user()
        child = RequestBudget(deadline=parent.deadline, cancel_check=lambda: (
            parent.cancelled.is_set() or (parent.cancel_check is not None and parent.cancel_check())))
        def work(item):
            """Bind and restore worker-local context. Args: one item. Returns: fn result."""
            with budget_scope(child), use_cookie(path):
                set_active_auth_user(auth_user)
                result = fn(item)
                check_budget('vision parallel work')
                return result
        pool = ThreadPoolExecutor(max_workers=min(4, len(items)))
        futures = []
        try:
            futures = [pool.submit(work, item) for item in items]
            results = []
            for future in futures:
                while True:
                    try:
                        results.append(future.result(timeout=min(0.1, parent.remaining('vision parallel work'))))
                        break
                    except FutureTimeout:
                        if future.done():
                            raise
            parent.check('vision parallel work')
            return results
        finally:
            child.cancel()
            for future in futures:
                future.cancel()
            # Never let executor shutdown extend an expired caller deadline.
            pool.shutdown(wait=False)


def parallel_uploads(prepared, upload):
    """Carry account and budget into workers. Args: images, upload callable. Returns: refs."""
    return parallel_map(prepared, lambda item: (str(upload(*item)), item[1]))
