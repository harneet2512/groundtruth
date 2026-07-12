"""SM-3 (2026-07-11) — HypothesisLedger activation -> recovery selection.

``classify_all`` runs live per turn over the shared ``_EPISODE`` + a ``LedgerEvent`` and
NAMES the recovery situation. It emits NO narration of its own: only the winning
DISPOSITION -> a SHORT leak-free imperative reaches the model, and only when a Lane-B
PIVOT fires (rendered via ``render_recovery_native``).

PINNED HERE (behaviour):
  1. The disposition->imperative map is EXACTLY the five recovery classes (and NOT
     ``no_discriminating`` progress / ``not_a_loop``), keyed on the engine's own constants.
  2. Flag OFF -> classify is a no-op: no selection, and the shared failure memory is NOT
     touched (byte-identical path).
  3. Flag ON -> an env-failure observation classifies ``repair_not_source``; a repeated
     failure fingerprint (no intervening edit) classifies ``request_new_hypothesis`` on
     the SECOND turn (the failure-memory feed makes the repeat classifier live).
  4. PIVOT CONSULT: when a pivot fires with a selected recovery, the steer's block is the
     classification's imperative (leak-free), NOT the generic pivot text; off -> generic.

Mutations bitten: drop the pivot consult (M1) -> #4 fails; classify regardless of the flag
/ feed memory when off (M2) -> #2 fails; map drift (M3) -> #1 fails.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import hypothesis_ledger as hl  # noqa: E402
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_test_identity, render_recovery_native)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    g._EPISODE.reset_attempt()
    g._gt_hypothesis_recovery = None
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    yield
    g._EPISODE.reset_attempt()
    g._gt_hypothesis_recovery = None


# --------------------------------------------------------------------------- #
# 1. The imperative map is exactly the five recovery classes (engine constants).
# --------------------------------------------------------------------------- #
def test_imperative_map_is_exactly_the_recovery_dispositions():
    g._HYPOTHESIS_IMPERATIVE_CACHE = None  # force a rebuild from the engine constants
    m = g._hypothesis_imperative_map()
    assert set(m) == {
        hl.D_REPAIR_NOT_SOURCE,
        hl.D_HYPOTHESIS_FALSIFIED,
        hl.D_REQUEST_NEW_HYPOTHESIS,
        hl.D_ALTERNATE_SURFACE_CANDIDATE,
        hl.D_REFRESH_BEFORE_ADVICE,
    }
    # progress dispositions carry NO recovery imperative.
    assert hl.D_NOT_A_LOOP not in m
    # every imperative is a non-empty single-line string with no test identity.
    for imp in m.values():
        assert imp and "\n" not in imp
        assert not contains_test_identity(imp)


# --------------------------------------------------------------------------- #
# 2. Flag OFF -> no selection, failure memory untouched (byte-identical).
# --------------------------------------------------------------------------- #
def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)
    g._gt_hypothesis_classify_turn("run", "bash: gcc: command not found")
    assert g._gt_hypothesis_recovery is None
    assert g._EPISODE.failure_fingerprints == set(), "flag OFF must not feed failure memory"


# --------------------------------------------------------------------------- #
# 3a. Flag ON -> env failure classifies repair_not_source.
# --------------------------------------------------------------------------- #
def test_env_failure_selects_repair_not_source(monkeypatch):
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    g._gt_hypothesis_classify_turn("make", "bash: gcc: command not found")
    assert g._gt_hypothesis_recovery is not None
    disp, imp = g._gt_hypothesis_recovery
    assert disp == hl.D_REPAIR_NOT_SOURCE
    assert imp == g._hypothesis_imperative_map()[hl.D_REPAIR_NOT_SOURCE]


# --------------------------------------------------------------------------- #
# 3b. A repeated failure fingerprint with no edit -> request_new_hypothesis (turn 2).
# --------------------------------------------------------------------------- #
def test_repeated_failure_no_edit_selects_new_hypothesis(monkeypatch):
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    obs = "AssertionError: expected 3 got 4\nFAILED some check"
    # turn 1: fresh failure -> memory seeded, no repeat classification yet.
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    g._gt_hypothesis_classify_turn("pytest", obs)
    assert g._EPISODE.failure_fingerprints, "a failure observation must seed the memory"
    # turn 2: SAME failure, NO edit recorded between -> same_failure_unchanged_patch.
    monkeypatch.setattr(g, "_action_count", 6, raising=False)
    g._gt_hypothesis_classify_turn("pytest", obs)
    assert g._gt_hypothesis_recovery is not None
    assert g._gt_hypothesis_recovery[0] == hl.D_REQUEST_NEW_HYPOTHESIS


# --------------------------------------------------------------------------- #
# 4. PIVOT CONSULT: a fired pivot frames its steer from the classification.
# --------------------------------------------------------------------------- #
@pytest.fixture
def pivot_ready(monkeypatch):
    """Force ``_verification_horizon_candidate`` to the pivot-band render path."""
    monkeypatch.setattr(g, "_GT_STEP_LIMIT", 100, raising=False)
    monkeypatch.setattr(g, "_horizon_advisory_fired", True, raising=False)
    monkeypatch.setattr(g, "_horizon_pivot_fired", False, raising=False)
    monkeypatch.setattr(g, "_estimate_v", lambda: 0.5)
    monkeypatch.setattr(g, "verify_horizon_band", lambda **kw: "pivot")
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda *a, **k: [])
    monkeypatch.setattr(g, "_render_verify_emission", lambda *a, **k: "GENERIC PIVOT")
    monkeypatch.setattr(g, "_structural_risk_note", lambda: ("", False))


def test_pivot_uses_recovery_imperative_when_selected(monkeypatch, pivot_ready):
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    g._gt_hypothesis_recovery = (hl.D_REPAIR_NOT_SOURCE,
                                 g._hypothesis_imperative_map()[hl.D_REPAIR_NOT_SOURCE])
    out = g._verification_horizon_candidate()
    assert out is not None
    _sev, kind, block, _bound = out
    assert kind == "verify.horizon.pivot"
    # the steer's pivot is the classification imperative, NOT the generic text.
    assert block == render_recovery_native(*g._gt_hypothesis_recovery)
    assert block != "GENERIC PIVOT"
    assert not contains_test_identity(block)


def test_pivot_generic_when_no_selection_or_flag_off(monkeypatch, pivot_ready):
    # flag ON but NO classification -> generic pivot (byte-identical to today).
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    g._gt_hypothesis_recovery = None
    out = g._verification_horizon_candidate()
    assert out is not None and out[2] == "GENERIC PIVOT"

    # a selection present but the FLAG OFF -> still generic (byte-identical off path).
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)
    monkeypatch.setattr(g, "_horizon_pivot_fired", False, raising=False)
    g._gt_hypothesis_recovery = (hl.D_REPAIR_NOT_SOURCE, "x")
    out = g._verification_horizon_candidate()
    assert out is not None and out[2] == "GENERIC PIVOT"
