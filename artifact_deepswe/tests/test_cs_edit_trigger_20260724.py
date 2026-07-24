"""AUDIT 2026-07-24 — change_surface's OWN edit-path trigger (file CREATION).

MEASURED: newfile_precedent / GT_CHANGE_SURFACE emitted 0 rows across all 4 tasks of run
30121930273. Root cause is DISPATCH, not detection: change_surface was ONLY outcome-dispatched
(ZERO_ABSENT) and that route additionally requires failing the SAME search stem twice. An agent that
searches once, finds nothing, and creates the file never reaches it.

ADDITIVE by construction: obligations keeps task-start, patch_delta/caller_contract keep their edit
slots, the ZERO_ABSENT route is unchanged. Nothing is displaced.
"""
from __future__ import annotations
from groundtruth.runtime import gateway as gw


class _Ev:
    def __init__(self, mapping):
        self.edit_before_after = mapping


def test_creation_is_detected_structurally():
    """before empty + after non-empty == a creation. Language-agnostic, no verb list."""
    assert gw._event_creates_new_file(_Ev({"new.py": ("", "def f():\n    return 1\n")})) is True
    assert gw._event_creates_new_file(_Ev({"new.ts": (None, "export const x = 1\n")})) is True


def test_a_plain_modification_is_NOT_a_creation():
    """A modify-in-place must never masquerade as a creation (correct-or-quiet)."""
    assert gw._event_creates_new_file(_Ev({"old.py": ("def f(): pass\n", "def f(): return 1\n")})) is False
    assert gw._event_creates_new_file(_Ev({})) is False
    assert gw._event_creates_new_file(_Ev(None)) is False
    # a DELETION (content removed) is not a creation either
    assert gw._event_creates_new_file(_Ev({"gone.py": ("def f(): pass\n", "")})) is False


def test_trigger_flag_defaults_off(monkeypatch):
    """Byte-identical unless explicitly enabled."""
    monkeypatch.delenv("GT_CS_EDIT_TRIGGER", raising=False)
    assert gw._cs_edit_trigger_on() is False
    monkeypatch.setenv("GT_CS_EDIT_TRIGGER", "1")
    assert gw._cs_edit_trigger_on() is True


def test_zero_absent_route_still_requires_the_repeat(monkeypatch):
    """The historical search route is UNCHANGED — require_repeat defaults True."""
    import inspect
    sig = inspect.signature(gw._produce_change_surface)
    assert sig.parameters["require_repeat"].default is True, \
        "the ZERO_ABSENT caller must keep the repeat gate (byte-identical on that route)"
