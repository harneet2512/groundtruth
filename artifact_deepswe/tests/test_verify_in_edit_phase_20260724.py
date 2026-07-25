"""AUDIT 2026-07-24 — verification evidence is inadmissible exactly when it is actionable.

MEASURED on the run-27792475148 runtime ledgers: verify.* candidates were **103
suppressed_wrong_phase vs 42 delivered** (71% dropped), including verify.horizon.urgent
56-suppressed / 12-delivered. `verify.horizon.executed` (covering_red) appears ZERO times.

ROOT CAUSE is circular, not incidental. `trajectory_state.derive_phase`:

    if state.nonedit_streak >= 3 or state.test_count:
        return Phase.VERIFY
    return Phase.EDIT

VERIFY is entered only AFTER the agent has already run a test (or stalled 3 turns). An agent that
edits and keeps editing stays in EDIT — and Phase.EDIT contains NO verify.horizon.* kind. So GT's
"this edit is unverified" / executed covering-RED evidence is inadmissible precisely while it is
actionable, and becomes admissible only once the agent has done the very thing it was meant to
prompt. This is the structural reason covering_red never lands: it is produced post-edit, in EDIT.

It also explains the httpx-streaming whiff: the agent edited, submitted, and GT's verification
evidence was phase-inadmissible for the entire trajectory.

GT_VERIFY_IN_EDIT opens EDIT to verify.horizon.* ONLY. It does not widen the dose (the <=1-dose
arbiter still admits one candidate per observation) and each verify producer keeps its own
trigger/latch. ORIENT/VIEW stay closed — there is no edit to verify there.
"""
from __future__ import annotations
import pytest

from groundtruth.runtime.context_policy import Phase, phase_allows

_EXECUTED = "verify.horizon.executed"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GT_VERIFY_IN_EDIT", raising=False)


def test_flag_off_is_byte_identical():
    """Default OFF: EDIT must still refuse verify evidence (today's behaviour)."""
    assert phase_allows(_EXECUTED, Phase.EDIT) is False
    assert phase_allows(_EXECUTED, Phase.VERIFY) is True
    assert phase_allows(_EXECUTED, Phase.SUBMIT) is True


def test_flag_on_admits_verification_at_the_post_edit_boundary(monkeypatch):
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    assert phase_allows(_EXECUTED, Phase.EDIT) is True, \
        "covering_red still cannot reach the agent at the moment it edits"


def test_flag_changes_EDIT_and_nothing_else(monkeypatch):
    """Scope guard: exactly one phase may change, or this is a delivery-policy rewrite."""
    off = {p: phase_allows(_EXECUTED, p) for p in Phase}
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    on = {p: phase_allows(_EXECUTED, p) for p in Phase}
    changed = [p for p in off if off[p] != on[p]]
    assert changed == [Phase.EDIT], f"expected only EDIT to change, got {changed}"


def test_orient_and_view_stay_closed(monkeypatch):
    """There is no edit to verify before one exists — correct-or-quiet."""
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    assert phase_allows(_EXECUTED, Phase.ORIENT) is False
    assert phase_allows(_EXECUTED, Phase.VIEW) is False


def test_non_verify_kinds_are_untouched(monkeypatch):
    """The flag must not become a general phase-gate bypass."""
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    assert phase_allows("edit.syntax", Phase.EDIT) is False
    assert phase_allows("brief", Phase.EDIT) is False
    assert phase_allows("orientation", Phase.EDIT) is False


def test_every_verify_horizon_kind_benefits(monkeypatch):
    """urgent/advisory/gate/pivot were the measured 103 suppressions, not just executed."""
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    for k in ("verify.horizon.urgent", "verify.horizon.advisory",
              "verify.horizon.gate", "verify.horizon.pivot", _EXECUTED):
        assert phase_allows(k, Phase.EDIT) is True, k


# ---------------------------------------------------------------------------
# SAME DEFECT CLASS, SECOND FEATURE: consensus.scope (def_partition, one of the 17).
# Admissible ONLY in VERIFY, but it FIRES on a search/view that resolves a symbol's
# definition and partition — which happens in VIEW/EDIT. Because VERIFY needs
# `test_count or nonedit_streak>=3`, def_partition answers the agent's SEARCH only
# after the agent has already run a TEST. Measured on the run-27792475148 ledgers:
# 34 suppressed_wrong_phase vs 7 delivered (83% lost).
# ---------------------------------------------------------------------------

_SCOPE = "consensus.scope"


def test_scope_flag_off_is_byte_identical():
    assert phase_allows(_SCOPE, Phase.VIEW) is False
    assert phase_allows(_SCOPE, Phase.EDIT) is False
    assert phase_allows(_SCOPE, Phase.VERIFY) is True


def test_scope_flag_on_admits_it_where_it_actually_fires(monkeypatch):
    monkeypatch.setenv("GT_SCOPE_AT_SEARCH", "1")
    assert phase_allows(_SCOPE, Phase.VIEW) is True
    assert phase_allows(_SCOPE, Phase.EDIT) is True


def test_scope_flag_changes_only_VIEW_and_EDIT(monkeypatch):
    off = {p: phase_allows(_SCOPE, p) for p in Phase}
    monkeypatch.setenv("GT_SCOPE_AT_SEARCH", "1")
    on = {p: phase_allows(_SCOPE, p) for p in Phase}
    changed = sorted(p.name for p in off if off[p] != on[p])
    assert changed == ["EDIT", "VIEW"], changed
    assert phase_allows(_SCOPE, Phase.ORIENT) is False, \
        "ORIENT has no resolved search yet — must stay closed"


def test_scope_flag_does_not_open_other_kinds(monkeypatch):
    monkeypatch.setenv("GT_SCOPE_AT_SEARCH", "1")
    assert phase_allows("brief", Phase.VIEW) is False
    assert phase_allows("l3.cochange", Phase.VIEW) is False
    assert phase_allows("verify.horizon.executed", Phase.EDIT) is False, \
        "the two levers must be INDEPENDENT — this one is not a verify opener"


def test_the_two_levers_are_independent(monkeypatch):
    """VERIFY_IN_EDIT must not open consensus.scope, and vice versa."""
    monkeypatch.setenv("GT_VERIFY_IN_EDIT", "1")
    assert phase_allows(_SCOPE, Phase.EDIT) is False
