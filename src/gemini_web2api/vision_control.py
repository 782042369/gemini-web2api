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


def parallel_uploads(prepared, upload):
    """Carry account and budget into workers. Args: images, upload callable. Returns: refs."""
    with budget_scope() as parent:
        path, auth_user = _active_cookie_path(), _active_auth_user()
        child = RequestBudget(deadline=parent.deadline, cancel_check=lambda: (
            parent.cancelled.is_set() or (parent.cancel_check is not None and parent.cancel_check())))
        def work(item):
            """Bind and restore worker-local context. Args: image tuple. Returns: ref tuple."""
            with budget_scope(child), use_cookie(path):
                set_active_auth_user(auth_user)
                ref = str(upload(*item))
                check_budget('vision upload completion')
                return ref, item[1]
        pool = ThreadPoolExecutor(max_workers=min(4, len(prepared)))
        futures = []
        try:
            futures = [pool.submit(work, item) for item in prepared]
            refs = []
            for future in futures:
                while True:
                    try:
                        refs.append(future.result(timeout=min(0.1, parent.remaining('vision uploads'))))
                        break
                    except FutureTimeout:
                        if future.done():
                            raise
            parent.check('vision uploads')
            return refs
        finally:
            child.cancel()
            for future in futures:
                future.cancel()
            # Never let executor shutdown extend an expired caller deadline.
            pool.shutdown(wait=False)
