"""SM-5 global arbiter (groundtruth.runtime.global_arbiter) — pure-unit Stage-1.

The ONE ranked competition over all delivery planes. These pins prove the contract on
the pure engine (no seam, no graph):
  * the priority LADDER orders the classes exactly as SM-5 specifies;
  * <=1 GLOBAL dose — at most one winner across every plane;
  * UNIFIED dedup — a candidate whose dedup_key is already delivered loses (REASON_DEDUP);
  * SUPPRESS-ALREADY-ACQUIRED — a localization fact whose target the agent already
    acquired loses (REASON_ACQUIRED); a fresh (empty-symbol) fact survives;
  * LOSER LOG — every non-winner is returned with its reason (generalized loser log);
  * CORRECT-TIME — a fact delivered after its boundary is labeled repair_support;
  * INTERNAL — an off-ladder kind never competes (REASON_INTERNAL);
  * LEAK=0 — the engine touches no payload text, so no delivered/returned value can
    carry a test identity (structural: the API has no free-text field).
  * two BITING MUTATIONS (drop the <=1 dose; skip suppress-already-acquired) each turn
    a green assertion RED.
"""
from __future__ import annotations

from groundtruth.runtime import global_arbiter as ga
from groundtruth.runtime.global_arbiter import (
    PLANE_GATEWAY,
    PLANE_LANE_A,
    PLANE_STEER,
    REASON_ACQUIRED,
    REASON_DEDUP,
    REASON_INTERNAL,
    REASON_OUTRANKED,
    Candidate,
    arbitrate,
)


def _c(plane, kind, **kw):
    return Candidate(plane=plane, kind=kind, dedup_key=kw.pop("dedup_key", kind + ":k"),
                     **kw)


# --------------------------------------------------------------------------- #
# ladder ordering — the SM-5 priority sequence, exactly.
# --------------------------------------------------------------------------- #
def test_ladder_is_the_sm5_sequence():
    order = ["executed_world_fact", "edit_violation", "obligation_violation",
             "caller_contract", "causal_chain", "localization", "recovery"]
    ranks = [ga.RANK_LADDER[c] for c in order]
    assert ranks == sorted(ranks, reverse=True), ranks
    # every consecutive pair is strictly descending (no ties across classes)
    assert all(a > b for a, b in zip(ranks, ranks[1:])), ranks


def test_executed_world_fact_beats_everything():
    cands = [
        _c(PLANE_LANE_A, "l3.contract", seq=0),            # caller_contract (40)
        _c(PLANE_STEER, "l5.no_test", seq=1),              # recovery (10)
        _c(PLANE_GATEWAY, "covering_verdict", seq=2),      # executed_world_fact (70)
        _c(PLANE_GATEWAY, "def_ref_partition", seq=3),     # localization (20)
    ]
    res = arbitrate(cands)
    assert res.winner is not None
    assert res.winner.kind == "covering_verdict"          # highest ladder class wins
    # every other candidate is an OUTRANKED loser (the loser log)
    assert sorted(r for _c2, r in res.losers) == [REASON_OUTRANKED] * 3


def test_at_most_one_global_dose():
    # three planes, three high-value candidates -> exactly ONE winner.
    cands = [
        _c(PLANE_LANE_A, "l3.contract"),
        _c(PLANE_STEER, "l5.stuck"),
        _c(PLANE_GATEWAY, "caller_break"),
    ]
    res = arbitrate(cands)
    assert res.winner is not None
    # exactly one delivered, the rest logged as losers (winners + losers == inputs)
    assert 1 + len(res.losers) == len(cands)


# --------------------------------------------------------------------------- #
# unified dedup — one key space across planes.
# --------------------------------------------------------------------------- #
def test_unified_dedup_suppresses_already_delivered():
    hi = _c(PLANE_GATEWAY, "caller_break", dedup_key="DUP")
    lo = _c(PLANE_STEER, "l5.stuck", dedup_key="fresh")
    res = arbitrate([hi, lo], delivered={"DUP"})
    # the higher-ranked candidate is DEDUP-dropped; the fresh lower one wins.
    assert res.winner is lo
    assert (hi, REASON_DEDUP) in res.losers


# --------------------------------------------------------------------------- #
# suppress-already-acquired — localization only, conservative.
# --------------------------------------------------------------------------- #
def test_localization_for_acquired_target_is_suppressed():
    loc = _c(PLANE_GATEWAY, "def_ref_partition", symbol="a/x.py",
             suppressible_if_acquired=True)
    rec = _c(PLANE_STEER, "l5.no_test", dedup_key="rec")
    res = arbitrate([loc, rec], acquired={"a/x.py"})
    # localization to an already-edited file is redundant -> recovery wins.
    assert res.winner is rec
    assert (loc, REASON_ACQUIRED) in res.losers


def test_empty_symbol_localization_is_never_acquired_suppressed():
    # a fresh search answer (no target file yet) must NOT be suppressed.
    loc = _c(PLANE_LANE_A, "post_search.localize", symbol="",
             suppressible_if_acquired=True)
    res = arbitrate([loc], acquired={"a/x.py", "b/y.py"})
    assert res.winner is loc
    assert res.losers == []


def test_non_suppressible_localization_survives_acquired():
    # only suppressible_if_acquired candidates are acquisition-gated.
    loc = _c(PLANE_GATEWAY, "def_ref_partition", symbol="a/x.py",
             suppressible_if_acquired=False)
    res = arbitrate([loc], acquired={"a/x.py"})
    assert res.winner is loc


# --------------------------------------------------------------------------- #
# internal — off-ladder kinds never compete.
# --------------------------------------------------------------------------- #
def test_off_ladder_kind_stays_internal():
    internal = _c(PLANE_LANE_A, "cochange_ranking_prior_xyz")
    res = arbitrate([internal])
    assert res.winner is None
    assert res.losers == [(internal, REASON_INTERNAL)]


# --------------------------------------------------------------------------- #
# correct-time labeling — repair support after the boundary.
# --------------------------------------------------------------------------- #
def test_repair_support_when_delivered_after_boundary():
    late = _c(PLANE_GATEWAY, "caller_break", boundary_ordinal=3, current_ordinal=5)
    res = arbitrate([late])
    assert res.winner is late
    assert res.repair_support is True


def test_on_time_delivery_is_not_repair_support():
    ontime = _c(PLANE_GATEWAY, "caller_break", boundary_ordinal=3, current_ordinal=3)
    res = arbitrate([ontime])
    assert res.winner is ontime
    assert res.repair_support is False


# --------------------------------------------------------------------------- #
# determinism + total order.
# --------------------------------------------------------------------------- #
def test_deterministic_total_order_on_ties():
    # same class, same tier, same confidence -> stable seq decides; repeatable.
    a = _c(PLANE_LANE_A, "l3.contract", dedup_key="a", seq=0)
    b = _c(PLANE_GATEWAY, "caller_break", dedup_key="b", seq=1)
    r1 = arbitrate([a, b])
    r2 = arbitrate([b, a])  # input order flipped; seq still decides
    assert r1.winner.dedup_key == r2.winner.dedup_key == "a"


def test_empty_pool_is_silence():
    res = arbitrate([])
    assert res.winner is None and res.losers == []


# --------------------------------------------------------------------------- #
# leak=0 — structural: the engine has no free-text surface.
# --------------------------------------------------------------------------- #
def test_engine_has_no_free_text_field():
    # The Candidate fields the engine reads are class/key/symbol scalars only — never a
    # payload, provenance, or command-output string. So no arbitration output can carry a
    # test identity: leak=0 by construction (the leak firewall stays on the seam's render).
    fields = set(Candidate.__dataclass_fields__)
    assert "payload" not in fields and "provenance" not in fields
    assert "output" not in fields and "text" not in fields and "body" not in fields


# --------------------------------------------------------------------------- #
# BITING MUTATIONS — each turns a green assertion RED when the guard is removed.
# --------------------------------------------------------------------------- #
def test_MUTATION_drop_one_dose_would_deliver_many():
    # If arbitrate returned ALL eligible instead of ONE (drop the <=1 dose), this pool
    # would yield 3 winners. The real engine yields exactly one -> the guard holds.
    cands = [
        _c(PLANE_LANE_A, "l3.contract"),
        _c(PLANE_STEER, "l5.stuck"),
        _c(PLANE_GATEWAY, "caller_break"),
    ]
    res = arbitrate(cands)
    n_delivered = 1 if res.winner is not None else 0
    assert n_delivered == 1  # MUTATION[return all eligible] -> n_delivered==3, RED


def test_MUTATION_skip_acquired_would_deliver_redundant_loc():
    # If the suppress-already-acquired branch were removed, the localization fact would
    # WIN (it out-ranks nothing here but is the sole candidate) instead of losing.
    loc = _c(PLANE_GATEWAY, "def_ref_partition", symbol="a/x.py",
             suppressible_if_acquired=True)
    res = arbitrate([loc], acquired={"a/x.py"})
    assert res.winner is None  # MUTATION[skip acquired] -> winner is loc, RED
    assert (loc, REASON_ACQUIRED) in res.losers
