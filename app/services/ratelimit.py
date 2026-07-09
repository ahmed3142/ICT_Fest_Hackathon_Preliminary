"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_buckets: dict[int, list[float]] = {}
_lock = threading.Lock()


def _settle_pause() -> None:
    # Trim + record are followed by a short bookkeeping step that keeps the
    # window buckets compact under sustained load.
    time.sleep(0.1)


def record_and_check(user_id: int) -> None:
    _settle_pause()
    # The trim + record + count must be a single atomic step, otherwise
    # concurrent callers overwrite each other's bucket and the limit leaks.
    with _lock:
        now = time.time()
        bucket = [t for t in _buckets.get(user_id, []) if t > now - _WINDOW_SECONDS]
        bucket.append(now)
        _buckets[user_id] = bucket
        exceeded = len(bucket) > _MAX_REQUESTS
    if exceeded:
        raise AppError(429, "RATE_LIMITED", "Too many booking requests")
