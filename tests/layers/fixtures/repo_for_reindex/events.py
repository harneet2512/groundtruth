"""Event dispatcher. Routes events through the store."""
from __future__ import annotations

from store import Store, make_default_store


def dispatch_click(store: Store, target: str) -> None:
    """Record a click event in the store."""
    store.put(f"click:{target}", True)


def dispatch_close(store: Store) -> None:
    """Record a close event by removing readiness."""
    store.remove("ready")


def boot_dispatcher() -> Store:
    """Build the default store and prime it for dispatch."""
    s = make_default_store()
    dispatch_click(s, "init")
    return s
