"""Wire test: the structural-edit-risk verification trigger inside the oracle's
verification-horizon producer. Flag OFF -> byte-identical to today (no trigger).
Flag ON + high blast-radius edited-untested symbol -> a budget-free structural
advisory that NAMES the risk. Correct-or-quiet when the risk is quiet.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.edit_risk import EditRisk, SymbolRisk  # noqa: E402


def _base(monkeypatch):
    """Neutral oracle state so the producer runs without crashing."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_GT_STEP_LIMIT", None)  # product (interactive) surface
    monkeypatch.setattr(g, "_horizon_advisory_fired", False)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/a.py"})
    monkeypatch.setattr(g, "_oracle_edited_tokens", {"hub"})
    monkeypatch.setattr(g, "_oracle_edited_tokens_by_file", {})
    monkeypatch.setattr(g, "_oracle_tested_tokens", set())
    monkeypatch.setattr(g, "_oracle_nonedit_streak", 0)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: {"hub"})
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda toks: [])
    monkeypatch.setattr(g, "_db_path", lambda: "/nonexistent.db")


_HIGH = EditRisk(0.8, (SymbolRisk("hub", 9, 0.8),), 3.0)
_QUIET = EditRisk(0.0, (), 0.0)


def test_note_quiet_when_flag_off(monkeypatch):
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", False)
    monkeypatch.setattr(g, "_structural_edit_risk", lambda db, syms: _HIGH)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: {"hub"})
    monkeypatch.setattr(g, "_db_path", lambda: "/x.db")
    assert g._structural_risk_note() == ("", False)


def test_note_fires_on_high_risk_when_flag_on(monkeypatch):
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", True)
    monkeypatch.setattr(g, "_RISK_TRIGGER", 0.5)
    monkeypatch.setattr(g, "_structural_edit_risk", lambda db, syms: _HIGH)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: {"hub"})
    monkeypatch.setattr(g, "_db_path", lambda: "/x.db")
    note, trig = g._structural_risk_note()
    assert trig is True
    assert "hub" in note and "9 verified caller" in note


def test_note_quiet_on_low_risk_when_flag_on(monkeypatch):
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", True)
    monkeypatch.setattr(g, "_RISK_TRIGGER", 0.5)
    monkeypatch.setattr(g, "_structural_edit_risk", lambda db, syms: _QUIET)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: set())
    monkeypatch.setattr(g, "_db_path", lambda: "/x.db")
    assert g._structural_risk_note() == ("", False)


def test_producer_fires_structural_advisory_on_high_risk(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", True)
    monkeypatch.setattr(g, "_RISK_TRIGGER", 0.5)
    monkeypatch.setattr(g, "_structural_edit_risk", lambda db, syms: _HIGH)
    res = g._verification_horizon_candidate()
    assert res is not None
    sev, kind, block, edit_bound = res
    assert kind == "verify.horizon.advisory"
    assert edit_bound is True
    assert "hub" in block and "9 verified caller" in block


def test_producer_no_structural_trigger_when_flag_off(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_STRUCTURAL_RISK_ON", False)  # OFF
    monkeypatch.setattr(g, "_structural_edit_risk", lambda db, syms: _HIGH)
    # flag off + product coverage gate not met (nonedit_streak=0) -> nothing fires,
    # proving the high structural risk did NOT trigger anything on its own.
    assert g._verification_horizon_candidate() is None
