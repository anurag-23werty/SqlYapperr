"""Sliding-window rate limiters.

* :class:`RateLimiter`: in-process (tests, single worker).
* :class:`SharedRateLimiter`: stored in the app database, so every worker process
  on the host shares the same counters (the production default). Keys are hashed
  so emails and IPs are not stored in clear text. For multi-host deployments,
  swap in a Redis-backed implementation with the same ``allow``/``reset`` API.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage.app_db import AppDB

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, max_keys: int = 100_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > self.max_keys:  # bound memory under key-spraying
                self._hits.clear()
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class SharedRateLimiter:
    def __init__(self, db: AppDB, limit: int, window_s: float = 60.0, namespace: str = "rl") -> None:
        self.db = db
        self.limit = limit
        self.window_s = window_s
        self.namespace = namespace

    def _key(self, key: str) -> str:
        return hashlib.sha256(f"{self.namespace}:{key}".encode()).hexdigest()

    def allow(self, key: str) -> bool:
        try:
            return self.db.rate_hit(self._key(key), self.limit, self.window_s)
        except Exception:  # pragma: no cover - a broken limiter must not take the site down
            log.exception("rate limiter unavailable; allowing request")
            return True

    def reset(self, key: str) -> None:
        self.db.rate_reset(self._key(key))
