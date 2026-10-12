"""A small in-process cache so page refreshes do not each start a BigQuery job. One instance, one worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TTLCache:
    def __init__(self, ttl_s: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict]] = {}

    def get(self, key: str, load: Callable[[], dict]) -> tuple[dict, bool]:
        """Return (value, stale). A value younger than the TTL is returned without calling load. If load fails and an
        older value exists, that value is returned marked stale, and no new attempt is made for another TTL."""
        with self._lock:  # held across load on purpose: concurrent requests must not start duplicate jobs
            now = self._clock()
            entry = self._entries.get(key)
            if entry and now < entry[0]:
                return entry[1], bool(entry[1].get("_stale"))
            try:
                value = load()
            except Exception:
                if not entry:
                    raise
                value = {**entry[1], "_stale": True}
            self._entries[key] = (now + self.ttl_s, value)
            return value, bool(value.get("_stale"))
