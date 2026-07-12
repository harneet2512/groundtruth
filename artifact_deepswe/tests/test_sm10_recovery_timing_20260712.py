"""SM-10 (2026-07-12) — recovery-timing DECOUPLE.

The typed HypothesisLedger recovery nudge has the RIGHT form (short imperative, native
via ``render_recovery_native``) but historically the WRONG trigger: it rode the
Verification-Horizon PIVOT band (``_render_verify_emission`` ~:7598) as a PASSENGER. An
agent that is stuck EARLY (repeated failing command, no edits, no budget) never reaches
the pivot zone, so the disposition was computed each turn then DISCARDED.

The fix gives recovery its OWN ladder-floor candidate (``_recovery_candidate``), wired
into the per-turn steer pool, so the nudge is delivered at the failure decision point
independent of the pivot. It stays flag-gated (GT_HYPOTHESIS) and byte-identical off.

RED-first (the e2e test): on the pre-fix tree — equivalently, with the
``cands.append(_recovery_candidate())`` wiring removed (the MUTATION that re-couples
recovery to the pivot) — the recovery imperative is NOT delivered on the early-stuck turn
that reaches no pivot. Post-fix it IS. The assertion is a TRUE value assertion
(imperative substring in the delivered observation), never a compile/attribute error.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import hypothesis_ledger as hl  # noqa: E402
from groundtruth.runtime.context_policy import (  # noqa: E402
    EVENT_BOUND_PAYLOADS, PHASE_POLICY, Event, PayloadKind, Phase, phase_allows)
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_test_identity, render_recovery_native)


def _mk_graph(path: str) -> None:
    """Minimal production-schema graph.db so the Lane-B producers degrade quietly."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL,
            source_file TEXT, source_line INTEGER
        );
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','frob','src/frob.py','python',NULL,0)")
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _reset():
    g._gt_hypothesis_recovery = None
    yield
    g._gt_hypothesis_recovery = None


# --------------------------------------------------------------------------- #
# CONTEXT POLICY: recovery is a real PayloadKind, event-bound + phase-allowed.
# --------------------------------------------------------------------------- #
def test_recovery_is_a_payloadkind_bound_to_failure_events():
    assert PayloadKind.RECOVERY.value == "recovery"
    for ev in (Event.TEST_RESULT, Event.POST_EDIT, Event.POST_VIEW):
        assert "recovery" in EVENT_BOUND_PAYLOADS[ev]
    for ph in (Phase.VIEW, Phase.EDIT, Phase.VERIFY):
        assert phase_allows("recovery", ph) is True
    # correct-or-quiet: not an ORIENT/SUBMIT concern (event-binding covers early test turns)
    assert "recovery" not in PHASE_POLICY[Phase.ORIENT]
    assert "recovery" not in PHASE_POLICY[Phase.SUBMIT]


# --------------------------------------------------------------------------- #
# PRODUCER: _recovery_candidate is flag-gated, ladder-floor, native, leak-free.
# --------------------------------------------------------------------------- #
def test_recovery_candidate_producer(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]

    # flag OFF -> None even with a stale selection present (byte-identical off).
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)
    g._gt_hypothesis_recovery = (hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    assert g._recovery_candidate() is None

    # flag ON + a selection -> a ladder-floor, edit-bound, native 'recovery' candidate.
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    rc = g._recovery_candidate()
    assert rc is not None
    sev, kind, text, edit_bound = rc
    assert kind == "recovery" and edit_bound is True
    assert text == render_recovery_native(hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    assert not contains_test_identity(text)
    # ladder rank 10 — defers to l5.stuck / l5.failure (severity 2) and everything above.
    assert sev < g._SEV_STUCK

    # flag ON but NO selection -> None (correct-or-quiet).
    g._gt_hypothesis_recovery = None
    assert g._recovery_candidate() is None


# --------------------------------------------------------------------------- #
# E2E (the RED-first proof): an EARLY-stuck agent that reaches NO pivot still gets the
# recovery nudge delivered at the failure decision point.
# --------------------------------------------------------------------------- #
def _setup_oracle(monkeypatch, tmp_path):
    anchors = tmp_path / "anchors.json"
    anchors.write_text('{"symbols": ["frob"]}', encoding="utf-8")
    db = tmp_path / "graph.db"
    _mk_graph(str(db))
    monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.delenv("GT_BASELINE", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_GT_STEP_LIMIT", None, raising=False)  # no budget -> no pivot band
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_oracle_focus_cache", None, raising=False)
    g._reset_oracle_state()
    g._gt_hypothesis_recovery = None
    # Silence the OTHER steer producers so recovery is isolated; the REAL classify, phase
    # filter, gate and delivery all run unchanged (only recovery's competitors are quiet).
    monkeypatch.setattr(g, "_l5_nudge", lambda *a, **k: "")
    monkeypatch.setattr(g, "_l5_failure_nudge", lambda *a, **k: "")
    monkeypatch.setattr(g, "_l5_unresolved_build_guard", lambda *a, **k: "")
    monkeypatch.setattr(g, "_l5_no_test_evidence_nudge", lambda *a, **k: "")
    monkeypatch.setattr(g, "_degenerate_loop_candidate", lambda *a, **k: None)
    # NO pivot reached: the early, edit-less, budget-less agent produces no horizon band.
    monkeypatch.setattr(g, "_verification_horizon_candidate", lambda: None)


def test_early_stuck_recovery_delivered_without_pivot(monkeypatch, tmp_path):
    _setup_oracle(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    obs = "AssertionError: expected 3 got 4\nFAILED some check"

    # turn 1: a FRESH failure — memory seeded, no repeat classification yet.
    out1 = {"output": obs}
    g._augment_output({"command": "pytest tests/test_a.py"}, out1)

    # turn 2: the SAME failure fingerprint, NO intervening edit -> request_new_hypothesis.
    out2 = {"output": obs}
    g._augment_output({"command": "pytest tests/test_b.py"}, out2)

    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    # POST-FIX: the recovery imperative is delivered at the failure decision point even
    # though NO pivot fired. (RED pre-fix / under the re-couple mutation: absent.)
    assert imp in (out2.get("output") or ""), (
        "recovery nudge must be delivered at the failure decision point without a pivot "
        "(SM-10 decouple); pre-fix the disposition is computed then discarded"
    )
    # A fresh (non-repeat) failure carries no recovery nudge.
    assert imp not in (out1.get("output") or "")
    # leak law: GT surfaced no test identity.
    assert not contains_test_identity(out2["output"])


def test_flag_off_is_byte_identical(monkeypatch, tmp_path):
    _setup_oracle(monkeypatch, tmp_path)
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)
    obs = "AssertionError: expected 3 got 4\nFAILED some check"

    out1 = {"output": obs}
    g._augment_output({"command": "pytest tests/test_a.py"}, out1)
    out2 = {"output": obs}
    g._augment_output({"command": "pytest tests/test_b.py"}, out2)

    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    assert imp not in (out2.get("output") or "")
    # with the flag off and every other producer quiet, GT appends ZERO bytes.
    assert out2["output"] == obs
    assert out1["output"] == obs
