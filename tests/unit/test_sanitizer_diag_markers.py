"""C6B: [GT_RANK_DIAG] / [GT_BRIEF_DIAG] must be in the SINGLE central
_HIDDEN_PREFIXES so no agent-facing path can re-leak them. Previously they were
stripped only by a local filter in the wrapper brief path."""
from __future__ import annotations

from groundtruth.runtime.sanitizer import has_leak, is_hidden_line, sanitize


def test_central_filter_catches_diag_markers():
    assert is_hidden_line("[GT_RANK_DIAG] #1 score=0.7 app/core.py")
    assert is_hidden_line("[GT_BRIEF_DIAG] ranked 5 candidates")
    assert has_leak("brief text\n[GT_RANK_DIAG] noise\nmore")


def test_sanitize_strips_diag_but_keeps_real_content():
    raw = (
        "<gt-task-brief>\n1. app/core.py\n[GT_RANK_DIAG] #1 score=0.7\n"
        "   Contract: raises ValueError\n[GT_BRIEF_DIAG] x\n</gt-task-brief>"
    )
    out = sanitize(raw)
    assert "[GT_RANK_DIAG]" not in out
    assert "[GT_BRIEF_DIAG]" not in out
    assert "Contract: raises ValueError" in out
    assert "<gt-task-brief>" in out


def test_real_markers_not_treated_as_hidden():
    # Agent-facing markers must survive (only diagnostics are hidden).
    assert not is_hidden_line("[CONTRACT] def run( -> dict")
    assert not is_hidden_line("[GT KEY CONTRACTS]")
    assert not is_hidden_line("[GT_VERIFY] Tests covering your changed files")
