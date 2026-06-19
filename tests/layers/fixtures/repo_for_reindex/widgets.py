"""Tiny widget toolkit. Provides primitive elements used by layout."""
from __future__ import annotations


def make_button(label: str) -> dict:
    """Construct a button widget descriptor."""
    return {"kind": "button", "label": label}


def make_panel(title: str) -> dict:
    """Construct a panel widget descriptor."""
    return {"kind": "panel", "title": title}


def make_label(text: str) -> dict:
    """Construct a label widget descriptor."""
    return {"kind": "label", "text": text}
