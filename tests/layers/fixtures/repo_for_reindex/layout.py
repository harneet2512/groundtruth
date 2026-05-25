"""Layout composer. Builds a window from primitive widgets."""
from __future__ import annotations

from widgets import make_button, make_panel, make_label


def build_toolbar() -> list:
    """Compose a toolbar from button widgets."""
    return [make_button("save"), make_button("open"), make_button("quit")]


def build_sidebar() -> dict:
    """Compose a sidebar panel with a title label."""
    panel = make_panel("Sidebar")
    panel["children"] = [make_label("hello")]
    return panel


def build_window() -> dict:
    """Compose the top-level window."""
    return {
        "toolbar": build_toolbar(),
        "sidebar": build_sidebar(),
    }
