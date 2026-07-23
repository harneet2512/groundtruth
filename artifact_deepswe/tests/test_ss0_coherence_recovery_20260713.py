"""SS-0 features 3 (GT_SS_COHERENCE_V2) + 4 (GT_SS_RECOVERY_V2).

Causal-audit context (run 29236533134):
  * detect.coherence's "rewritten N times" was factually wrong on ~16/20 firings — the
    churn counter conflated L6-reindex hook-fires (incl. cat READS + failed edits) and
    collided distinct files by basename; the "no passing test between edits" clause was
    asserted but never checked.
  * recovery misfired on non-stuck states (privacyidea m137 git-log archaeology; m175
    while tests were passing) and EXPIRED once (dynaconf: suppressed 11x then delivered
    one message before LimitsExceeded).

V2 rebuilds churn from the EDIT-EVENT ledger (successful writes to the EXACT path only,
no basename collision, no reindex fires) with the no-passing-test-between check actually
asserted; and rebuilds recovery eligibility as "SAME test-classified command observed
FAILING >=2x with NO intervening edit", releasing at the SECOND qualifying repeat.
Both flag-gated + byte-identical off.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import hypothesis_ledger as hl  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for k in ("GT_SS_COHERENCE_V2", "GT_SS_RECOVERY_V2", "GT_HYPOTHESIS",
              "GT_GLOBAL_ARBITER"):
        monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()
    yield
    g._gt_hypothesis_recovery = None


# --------------------------------------------------------------------------- #
# Feature 3 — COHERENCE V2 churn
# --------------------------------------------------------------------------- #
def test_coherence_v2_counts_successful_writes_exact_path():
    g._ss_edit_events.extend([("a/x.py", 1, True), ("a/x.py", 2, True), ("a/x.py", 3, True)])
    assert g._ss_coherence_churn("a/x.py") == 3
    # <=2 writes -> None (correct-or-quiet)
    g._ss_edit_events.append(("a/y.py", 4, True))
    assert g._ss_coherence_churn("a/y.py") is None


def test_coherence_v2_no_basename_collision():
    # distinct files sharing a basename must NEVER combine (the audited collision bug)
    g._ss_edit_events.extend([
        ("a/config.py", 1, True), ("b/config.py", 2, True),
        ("a/config.py", 3, True), ("a/config.py", 4, True)])
    assert g._ss_coherence_churn("a/config.py") == 3   # steps 1,3,4
    assert g._ss_coherence_churn("b/config.py") is None  # 1 write only


def test_coherence_v2_excludes_failed_edits():
    g._ss_edit_events.extend([
        ("c/z.py", 1, True), ("c/z.py", 2, False), ("c/z.py", 3, True)])
    assert g._ss_coherence_churn("c/z.py") is None  # only 2 successful writes


def test_coherence_v2_passing_test_between_suppresses():
    g._ss_edit_events.extend([("a/x.py", 1, True), ("a/x.py", 3, True), ("a/x.py", 5, True)])
    assert g._ss_coherence_churn("a/x.py") == 3
    # a PASSING test between the first (1) and last (5) write -> not thrash -> None
    g._ss_test_events.append((3, True))
    assert g._ss_coherence_churn("a/x.py") is None
    # a FAILING test between does NOT clear it (still overwriting blind)
    g._ss_test_events.clear()
    g._ss_test_events.append((3, False))
    assert g._ss_coherence_churn("a/x.py") == 3


def test_coherence_candidate_uses_v2_when_on(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    monkeypatch.setattr(g, "_last_test_outcome_failed", True)  # anchor the fire
    monkeypatch.setattr(g, "_oracle_focus", lambda: set())
    g._coherence_fired_files.clear()
    g._ss_edit_events.extend([("a/x.py", 1, True), ("a/x.py", 2, True), ("a/x.py", 3, True)])
    got = g._coherence_collapse_candidate("a/x.py")
    assert got is not None and "rewritten x.py 3 times" in got[1]
    # a passing test between -> V2 produces nothing (even though _edit_churn>=3 legacy would)
    g._coherence_fired_files.clear()
    g._ss_test_events.append((2, True))
    assert g._coherence_collapse_candidate("a/x.py") is None


def test_coherence_v2_off_uses_legacy_churn(monkeypatch):
    """RED anchor: flag OFF -> the legacy _edit_churn counter drives the fire (V2 ignored)."""
    monkeypatch.setattr(g, "_last_test_outcome_failed", True)
    monkeypatch.setattr(g, "_oracle_focus", lambda: set())
    g._coherence_fired_files.clear()
    g._edit_churn["a/x.py"] = 3          # legacy counter says 3
    g._ss_edit_events.clear()            # V2 ledger EMPTY -> V2 would return None
    got = g._coherence_collapse_candidate("a/x.py")
    assert got is not None              # legacy path fires (byte-identical pre-SS)


# --------------------------------------------------------------------------- #
# Feature 4 — RECOVERY V2 eligibility + stall-gate release
# --------------------------------------------------------------------------- #
def test_recovery_v2_eligible_flips_on_second_repeat():
    g._ss_test_fail_counts.clear()
    g._ss_test_fail_counts["pytest tests/x.py"] = 1
    assert g._ss_recovery_eligible() is False          # one fail -> not stuck
    g._ss_test_fail_counts["pytest tests/x.py"] = 2
    assert g._ss_recovery_eligible() is True            # SECOND qualifying repeat -> release


def test_recovery_v2_edit_resets_streak():
    g._ss_record_test("pytest x", "1 failed", True, False)   # count 1
    g._ss_record_test("pytest x", "1 failed", True, False)   # count 2 -> eligible
    assert g._ss_recovery_eligible() is True
    g._ss_record_edit("a/x.py", "sed -i s/a/b/ a/x.py", "")  # an intervening edit
    assert g._ss_recovery_eligible() is False           # streak cleared (privacyidea m137)


def test_recovery_v2_pass_and_differing_command_ineligible():
    g._ss_record_test("pytest a", "1 failed", True, False)   # a:1
    g._ss_record_test("pytest b", "1 failed", True, False)   # b:1 (different command)
    assert g._ss_recovery_eligible() is False           # neither command reached 2
    g._ss_record_test("pytest a", "3 passed", False, True)   # a passes -> clears a (m175)
    assert "pytest a" not in g._ss_test_fail_counts


def test_recovery_candidate_v2_gate(monkeypatch):
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    g._gt_hypothesis_recovery = (hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    g._ss_test_fail_counts.clear()
    g._ss_record_test("pytest x", "1 failed", True, False)
    assert g._recovery_candidate() is None              # not eligible -> suppressed
    g._ss_record_test("pytest x", "1 failed", True, False)
    rc = g._recovery_candidate()
    assert rc is not None and rc[1] == "recovery"        # released at repeat-2


def test_recovery_v2_admits_degenerate_loop_thrash(monkeypatch):
    """Thrash-rewrites clear the fail streak; hard degenerate-loop still releases."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    g._gt_hypothesis_recovery = (hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    g._ss_test_fail_counts.clear()
    g._ss_current_failure_event = None
    g._detect_loop_fired = False
    assert g._recovery_candidate() is None
    g._detect_loop_fired = True
    rc = g._recovery_candidate()
    assert rc is not None and rc[1] == "recovery"
    g._detect_loop_fired = False


def test_recovery_v2_off_falls_to_legacy(monkeypatch):
    """RED anchor: flag OFF -> V2 gate is inert; the legacy (arbiter-scoped) path governs."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")             # GT_SS_RECOVERY_V2 NOT set
    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    g._gt_hypothesis_recovery = (hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    g._ss_test_fail_counts.clear()                        # V2 says ineligible, but flag off
    # arbiter off -> the legacy inline path delivers the candidate (byte-identical pre-SS)
    rc = g._recovery_candidate()
    assert rc is not None and rc[1] == "recovery"
