"""Application state store with simple put/get."""
from __future__ import annotations


class Store:
    """A naive in-memory store keyed by string."""

    def __init__(self) -> None:
        self._data: dict = {}

    def put(self, key: str, value) -> None:
        self._data[key] = value

    def fetch(self, key: str):
        return self._data.get(key)

    def remove(self, key: str) -> bool:
        if key in self._data:
            del self._data[key]
            return True
        return False


def make_default_store() -> Store:
    """Construct a Store and seed it with one row."""
    s = Store()
    s.put("ready", True)
    return s
