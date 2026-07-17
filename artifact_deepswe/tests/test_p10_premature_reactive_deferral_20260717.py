"""P10 (2026-07-17) — PREMATURE-REACTIVE DEFERRAL at the edit boundary.

THE DEFECT (measured in this worktree, confirmed RED below): at a post_edit turn the global
arbiter ranks by ladder CLASS, so a PREMATURE reactive completeness/gate fact
(``obligation.unexercised`` -> class ``obligation_violation``, boundary ordinal 5 = verify/submit,
rank 50) — which is NOT yet actionable at the edit — takes the single dose ahead of the ON-TIME
preventive edit-bound SHAPE contract (``l3.contract`` -> class ``caller_contract``, boundary
ordinal 3 = the edit, rank 40). The SHAPE contract is starved at the exact keep-vs-remove fork
where it is outcome-determining (P10: l3.contract fired 5x, delivered 0 bytes).

THE FIX (arbitration/timing layer, gt_mini_patch): the symmetric complement of the SM-10
late-preventive demotion — defer the premature reactive winner (it re-arms + re-competes at its own
boundary) and promote the on-time preventive edit-bound fact as the SINGLE dose. Gated behind
``GT_SS_EDIT_PREVENTIVE`` (byte-identical off). STAYS <=1 dose.

RED-FIRST: ``test_flag_off_is_byte_identical_obligation_wins`` pins the pre-fix behavior (the RED);
the flag-ON tests pin the GREEN. Mutation notes are inline (each MUT-n comment names a code change
the assertion below it catches)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.global_arbiter import (  # noqa: E402
    REASON_OUTRANKED,
    class_of_kind,
)


def _reset():
    g._oracle_edited_rels = set()
    try:
        g._EPISODE.delivered_dedup.clear()
    except Exception:  # noqa: BLE001
        pass


def _cand(kind: str, seq: int, *, kkind: str = "post_edit", target: str = "pkg/importer.py"):
    """A candidate exactly as the seam builds it at the given boundary (Lane-A plane)."""
    return g._ga_make_candidate(
        g._GA_PLANE_LANE_A, kind, dedup_key="d" + str(seq),
        target=target, kkind=kkind, seq=seq)


def _plan_winner(pool):
    _reset()
    _res, winner = g._global_pool_plan(pool)
    return winner, _res


def _pool(*kinds):
    return [(_cand(k, i), (lambda: None)) for i, k in enumerate(kinds)]


# --------------------------------------------------------------------------- #
# environment discipline: the whole path runs only under the global arbiter.
# --------------------------------------------------------------------------- #
def setup_module(module):  # noqa: D401
    os.environ["GT_GLOBAL_ARBITER"] = "1"


def teardown_module(module):  # noqa: D401
    os.environ.pop("GT_SS_EDIT_PREVENTIVE", None)


# --------------------------------------------------------------------------- #
# ordinal / class sanity — the diagnosis the fix rests on.
# --------------------------------------------------------------------------- #
def test_diagnosis_ordinals_hold():
    c_contract = _cand("l3.contract", 0)
    c_oblig = _cand("obligation.unexercised", 1)
    assert class_of_kind("l3.contract") == "caller_contract"
    assert class_of_kind("obligation.unexercised") == "obligation_violation"
    # the SHAPE contract's boundary IS the edit (on-time at post_edit).
    assert (c_contract.current_ordinal, c_contract.boundary_ordinal) == (3, 3)
    # the obligation gate's boundary is verify/submit (premature at the edit).
    assert (c_oblig.current_ordinal, c_oblig.boundary_ordinal) == (3, 5)
    assert c_oblig.current_ordinal < c_oblig.boundary_ordinal  # premature


def test_is_premature_deferrable_helper():
    assert g._ga_is_premature_deferrable(_cand("obligation.unexercised", 0)) is True
    # MUT-1: adding "executed_world_fact" to _GA_PREMATURE_DEFERRABLE_CLASSES flips this.
    assert g._ga_is_premature_deferrable(_cand("covering_verdict", 0)) is False
    # a preventive fact is never "premature-deferrable" (handled by the late rule instead).
    assert g._ga_is_premature_deferrable(_cand("l3.contract", 0)) is False
    # MUT-2: relaxing the strict `<` to `<=` would call edit_violation (boundary==cur) premature.
    assert g._ga_is_premature_deferrable(_cand("edit.syntax", 0)) is False


# --------------------------------------------------------------------------- #
# RED baseline + byte-identity (flag off).
# --------------------------------------------------------------------------- #
def test_flag_off_is_byte_identical_obligation_wins():
    os.environ.pop("GT_SS_EDIT_PREVENTIVE", None)
    winner, res = _plan_winner(_pool("l3.contract", "obligation.unexercised"))
    # THE RED: the premature obligation takes the dose; the on-time SHAPE contract is starved.
    assert winner.kind == "obligation.unexercised"
    assert ("l3.contract", REASON_OUTRANKED) in [(c.kind, r) for c, r in res.losers]


# --------------------------------------------------------------------------- #
# GREEN — flag on.
# --------------------------------------------------------------------------- #
def test_flag_on_promotes_on_time_contract_at_edit():
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, res = _plan_winner(_pool("l3.contract", "obligation.unexercised"))
    # THE GREEN: the on-time preventive SHAPE contract wins the single dose.
    assert winner.kind == "l3.contract"
    # the premature obligation is DEFERRED (not destroyed) with the honest reason.
    reasons = {c.kind: r for c, r in res.losers}
    assert reasons.get("obligation.unexercised") == g._GA_REASON_PREMATURE_EARLY
    # MUT-3: dropping `losers.pop(_i)` leaves the promoted contract in the loser set.
    assert "l3.contract" not in reasons


def test_causal_chain_edit_bound_also_promoted():
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, _res = _plan_winner(_pool("l3.cochange", "obligation.unexercised"))
    # cochange -> causal_chain, boundary 3 (the edit) -> on-time preventive -> promoted.
    assert winner.kind == "l3.cochange"


# --------------------------------------------------------------------------- #
# SAFE-GUARDS — the fix must never over-reach (these are the biting mutations).
# --------------------------------------------------------------------------- #
def test_executed_world_fact_never_deferred():
    """MUT-1 biter: a covering RED that executed at post_edit is a REALIZED world-fact —
    actionable the instant it fires — and must keep the dose over a contract."""
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, _res = _plan_winner(_pool("covering_verdict", "l3.contract"))
    assert winner.kind == "covering_verdict"


def test_obligation_only_pool_never_silenced():
    """MUT-4 biter: removing the 'on-time preventive replacement EXISTS' guard would demote a
    premature obligation with nothing to promote -> silence a deliverable turn (regression)."""
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, _res = _plan_winner(_pool("obligation.unexercised"))
    assert winner is not None and winner.kind == "obligation.unexercised"


def test_late_localization_not_promoted():
    """A LATE preventive fact (localization boundary 1 < current 3 at the edit) is NOT on-time,
    so it must NOT be promoted; the premature obligation is then kept (correct-or-quiet)."""
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, _res = _plan_winner(_pool("post_search.localize", "obligation.unexercised"))
    assert winner.kind == "obligation.unexercised"


def test_contract_alone_unchanged():
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    winner, _res = _plan_winner(_pool("l3.contract"))
    assert winner.kind == "l3.contract"


# --------------------------------------------------------------------------- #
# end-to-end at the flush: <=1 dose, the WINNER's thunk fires, the LOSER's does not.
# --------------------------------------------------------------------------- #
def test_flush_delivers_contract_thunk_only(monkeypatch):
    os.environ["GT_SS_EDIT_PREVENTIVE"] = "1"
    _reset()
    monkeypatch.setattr(g, "_record_hook_suppress", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None, raising=False)
    fired = []
    c_contract = _cand("l3.contract", 0)
    c_oblig = _cand("obligation.unexercised", 1)
    pool = [
        (c_contract, lambda: fired.append("l3.contract")),
        (c_oblig, lambda: fired.append("obligation.unexercised")),
    ]
    out: dict = {}
    g._global_pool_flush(pool, kkind="post_edit", kf="pkg/importer.py", krel="pkg/importer.py")
    # exactly ONE dose, and it is the on-time SHAPE contract — never the premature obligation.
    assert fired == ["l3.contract"]
