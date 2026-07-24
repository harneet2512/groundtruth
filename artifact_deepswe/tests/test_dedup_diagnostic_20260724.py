"""AUDIT 2026-07-24 — dedup DIAGNOSTIC (host-only telemetry, zero model bytes).

24 of 53 caller_contracts died as ss_semantic_dup in run 30121930273 and the ledger recorded only
the verdict, so their CORRECTNESS was unjudgeable. This pins that the decision is UNCHANGED while
the inputs (own size / prior size / exact-vs-strict-subset) become readable.
"""
from __future__ import annotations
import gt_mini_patch as g

BROAD = "a.py b.py c.py alpha( beta( gamma("
NARROW = "a.py alpha("


def _arm(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    monkeypatch.delenv("GT_SS_FLARE", raising=False)
    g._ss_known_entsets.clear()


def test_decision_unchanged_strict_subset_still_suppressed(monkeypatch):
    """BEHAVIOR PIN: the diagnostic must not alter suppression (the pinned cross-class contract)."""
    _arm(monkeypatch)
    g._ss_remember_known("l3b.evidence", BROAD, "/r")
    assert g._ss_dedup2_suppresses("l3.contract", NARROW, "/r") is True
    assert g._ss_dedup2_suppresses("l3.contract", "novel.py delta(", "/r") is False


def test_diagnostic_reason_carries_the_inputs(monkeypatch):
    """The recorded reason must distinguish an EXACT repeat from a STRICT SUBSET, with sizes."""
    recs = []
    _arm(monkeypatch)
    monkeypatch.setattr(g, "_control_participation_record",
                        lambda *a, **k: recs.append(k.get("reason", "")))
    g._ss_remember_known("l3b.evidence", BROAD, "/r")
    g._ss_content_decision("l3.contract", NARROW, "/r")
    dedup = [r for r in recs if str(r).startswith("semantic_duplicate")]
    assert dedup, f"no dedup diagnostic recorded; got {recs}"
    assert "strict_subset" in dedup[0] and "ents=" in dedup[0] and "prior=" in dedup[0], \
        f"diagnostic lacks the judgeable inputs: {dedup[0]}"
    # an EXACT repeat is labelled differently (that one IS unambiguously correct to suppress)
    recs.clear(); g._ss_known_entsets.clear()
    g._ss_remember_known("l3b.evidence", NARROW, "/r")
    g._ss_content_decision("l3.contract", NARROW, "/r")
    dedup = [r for r in recs if str(r).startswith("semantic_duplicate")]
    assert dedup and "exact" in dedup[0], f"exact repeat not labelled: {dedup}"
