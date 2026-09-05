"""Upstream concurrency cap: FIFO queueing via a bounded semaphore."""
import threading
import time

from ..config import CONFIG
from ..logs import log


# Upstream concurrency cap (max_concurrent_requests config; 0 = unlimited).
_upstream_semaphore = None
_upstream_sema_lock = threading.Lock()


def _get_semaphore():
    global _upstream_semaphore
    limit = CONFIG.get("max_concurrent_requests") or 0
    if limit <= 0:
        return None
    with _upstream_sema_lock:
        if _upstream_semaphore is None:
            _upstream_semaphore = threading.BoundedSemaphore(limit)
        return _upstream_semaphore


class _UpstreamSlot:
    """Context manager acquiring a global upstream concurrency slot (if capped).

    Gemini Web only serves ~3-4 concurrent streams per account well; beyond
    that it slow-walks or rejects requests. Capping locally queues requests
    FIFO instead, which keeps tail latency predictable.
    """

    def __enter__(self):
        sema = _get_semaphore()
        self._sema = sema
        if sema is None:
            return self
        self._t0 = time.time()
        sema.acquire()
        waited = time.time() - self._t0
        if waited > 0.5:
            log(f"Upstream busy: queued {waited:.1f}s (max_concurrent_requests={CONFIG.get('max_concurrent_requests')})")
        return self

    def __exit__(self, *exc):
        if self._sema is not None:
            self._sema.release()
        return False
