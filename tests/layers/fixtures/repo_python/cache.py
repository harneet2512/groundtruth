"""Tiny in-memory cache used by the server."""
from __future__ import annotations

import time
from typing import Any


class TTLCache:
    """A bounded TTL cache."""

    def __init__(self, max_size: int = 128, ttl_seconds: float = 60.0) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def set(self, key: str, value: Any) -> None:
        if len(self._store) >= self.max_size:
            self._evict_oldest()
        self._store[key] = (time.time(), value)

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self.ttl_seconds:
            self._store.pop(key, None)
            return None
        return value

    def _evict_oldest(self) -> None:
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k][0])
        self._store.pop(oldest_key, None)
