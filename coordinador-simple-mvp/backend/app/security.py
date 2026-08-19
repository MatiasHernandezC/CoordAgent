from __future__ import annotations

from collections import OrderedDict, deque
from threading import Lock
from time import monotonic


class SlidingWindowLimiter:
    """Limitador en memoria, acotado, suficiente para una instancia de demo."""

    def __init__(self, max_buckets: int = 4096) -> None:
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    self._buckets.popitem(last=False)
                bucket = deque()
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


request_limiter = SlidingWindowLimiter()

