"""AUDIT 2026-07-24 — CHECKER PROVENANCE.

Run 30121930273 recorded `trigger_false:clean_exit` for 46 clean edits, which could NOT distinguish
"the undefined-name leg ran and found nothing" from "it never executed" (missing pyflakes). That
ambiguity made GT_EDIT_CHECK_NAMES unprovable from the live ledger — an observability gap one level
below the terminal states themselves.
"""
from __future__ import annotations
import gt_mini_patch as g
from groundtruth.runtime import edit_check as ec


def test_clean_file_records_that_the_name_leg_RAN(tmp_path, monkeypatch):
    (tmp_path / "clean.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    monkeypatch.setenv("GT_EDIT_CHECK_NAMES", "1")
    r = ec.check_edit_syntax("clean.py", str(tmp_path), executor=None)
    assert r["verdict"] == "ok", "verdict must be unchanged by a provenance annotation"
    assert any("pyflakes" in str(c) for c in (r.get("checker") or [])), \
        "REGRESSION: a clean file no longer records that the name leg executed"


def test_flag_off_does_not_claim_the_name_leg_ran(tmp_path, monkeypatch):
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.delenv("GT_EDIT_CHECK_NAMES", raising=False)
    r = ec.check_edit_syntax("clean.py", str(tmp_path), executor=None)
    assert not any("pyflakes" in str(c) for c in (r.get("checker") or [])), \
        "flag OFF must not claim the name leg ran"


def test_terminal_state_carries_the_checker_list(tmp_path, monkeypatch):
    """The ledger reason must name the legs that ran, so 0-delivered is explainable."""
    rows = []
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setenv("GT_EDIT_CHECK_NAMES", "1")
    monkeypatch.setattr(g, "_runtime_ledger_record",
                        lambda **kw: rows.append((kw.get("kind"), kw.get("reason"))))
    (tmp_path / "clean.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    g._edit_syntax_candidate("clean.py")
    reasons = [r for k, r in rows if k == "edit.syntax"]
    assert any("checkers=" in str(r) for r in reasons), \
        f"terminal state lacks checker provenance: {reasons}"
    assert any("pyflakes" in str(r) for r in reasons), \
        f"terminal state does not show the name leg ran: {reasons}"
