"""Application entry point. Glues layout, store, and events together."""
from __future__ import annotations

from layout import build_window
from store import make_default_store
from events import dispatch_click, dispatch_close, boot_dispatcher


def run_app() -> dict:
    """Build the UI, boot the dispatcher, and emit one click."""
    window = build_window()
    store = boot_dispatcher()
    dispatch_click(store, "main")
    return {"window": window, "store_ready": store.fetch("ready")}


def shutdown_app(store) -> None:
    """Run the close dispatcher path."""
    dispatch_close(store)


def make_store_for_tests():
    """Helper used only by external tests."""
    return make_default_store()
