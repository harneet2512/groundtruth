"""SM-5 F (#50) + finding 2b — GENERALIZED Lane-A production-latch rollback.

THE BUG (F, #50): under GT_GLOBAL_ARBITER several Lane-A producers burn a FIRE-ONCE latch
at PRODUCTION (while `lane_a` is built), BEFORE the ranked competition. A Lane-A candidate
that LOSES ships 0 bytes (its thunk never runs) yet its production latch stays burned ->
the producer returns "" for that key FOREVER -> the fact is permanently destroyed. The
pre-fix flush re-armed only PLANE_STEER losers.

THE FIX (Fable-sanctioned, generalized — per-candidate opaque rollback, NOT plane-specific
re-arm): each producer attaches a zero-arg ROLLBACK closure that un-burns exactly what it
burned; `_global_pool_flush` invokes it for every LOSER via one generic lookup keyed by
kind. The WINNER is never a loser, so its latch stays committed (no double-deliver).

Finding 2b: `_global_pool_add_gateway` hardcoded kkind="post_edit" (=3), mislabeling every
localization-class gateway fact delivered on a search/test turn as repair_support=True. Fix
threads the REAL `ev.kind` -> the correct current_ordinal.

RED-first: each test FAILS on the pre-fix tree (proven by reverting the corresponding hunk /
running the documented mutation) and PASSES after. Hermetic: `_query_scope` monkeypatched
(no real graph), no ONNX, no repo checkout.

Documented BITING MUTATIONS (each reddens a specific test):
  M1  drop the flush's generic Lane-A rollback invocation
      -> test_lane_a_loser_latch_is_rearmed_not_destroyed REDDENS (latch stays burned).
  M2  ALSO invoke the winner's rollback (re-arm the winner)
      -> test_lane_a_winner_latch_stays_committed_no_double_deliver REDDENS (re-fires).
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import global_arbiter as ga  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def arbiter(monkeypatch):
    """GT_GLOBAL_ARBITER on, GT-on, a 1-neighbour scope, clean oracle state."""
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/neighbor.py"])
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


def _produce_scope(rel: str) -> str:
    """Seed the scope membership + PRODUCE the real consensus.scope_map fact (burns its
    ("consensus_scope", n) latch and attaches its rollback closure)."""
    g._consensus_scope.add(g._norm_rel(rel))
    return g._consensus_scope_block(rel)


# --------------------------------------------------------------------------- #
# F (#50) — a Lane-A LOSER is deferred, not destroyed
# --------------------------------------------------------------------------- #
def test_lane_a_loser_latch_is_rearmed_not_destroyed(arbiter):
    """A real consensus.scope_map that LOSES to a higher-rank caller_break re-arms its
    PRODUCTION latch, so the SAME fact re-produces next turn instead of "" forever.

    RED pre-fix: the flush re-armed only PLANE_STEER losers -> the Lane-A latch stayed
    burned -> `_consensus_scope_block` returned "" for that file for the rest of the run."""
    rel = "src/work.py"
    t1 = _produce_scope(rel)
    assert t1 and "<gt-scope" in t1
    key = ("consensus_scope", g._norm_rel(rel))
    assert key in g._seen, "producer must burn the latch at production"
    assert "consensus.scope_map" in g._lane_a_rearm_pending, "producer must attach a rollback"

    delivered = []
    loser = ga.Candidate(plane=ga.PLANE_LANE_A, kind="consensus.scope_map",
                         dedup_key="csk", seq=0)
    winner = ga.Candidate(plane=ga.PLANE_GATEWAY, kind="caller_break",
                          dedup_key="cbk", seq=1)  # caller_contract(40) > localization(20)
    pool = [(loser, lambda: delivered.append("scope")),
            (winner, lambda: delivered.append("caller_break"))]
    g._global_pool_flush(pool, kkind="post_edit", kf=rel, krel=rel)

    assert delivered == ["caller_break"], "exactly ONE dose, the higher class"
    assert key not in g._seen, "a Lane-A LOSER must re-arm (un-burn) its production latch"
    t2 = _produce_scope(rel)
    assert t2 == t1 and "<gt-scope" in t2, "the fact must RE-PRODUCE (deferred, not destroyed)"


def test_lane_a_winner_latch_stays_committed_no_double_deliver(arbiter):
    """A consensus.scope_map that WINS keeps its production latch committed -> it does NOT
    re-fire next turn (no double-deliver).

    Catches MUTATION M2 (re-arm the winner too): the winner's latch would be un-burned and
    the fact would re-produce + re-deliver next turn."""
    rel = "src/work.py"
    t1 = _produce_scope(rel)
    assert t1 and "<gt-scope" in t1
    key = ("consensus_scope", g._norm_rel(rel))

    delivered = []
    winner = ga.Candidate(plane=ga.PLANE_LANE_A, kind="consensus.scope_map",
                          dedup_key="csk", seq=0)   # sole candidate -> WINS
    pool = [(winner, lambda: delivered.append("scope"))]
    g._global_pool_flush(pool, kkind="post_edit", kf=rel, krel=rel)

    assert delivered == ["scope"], "the winner delivered"
    assert key in g._seen, "a delivered WINNER must keep its production latch burned"
    assert g._consensus_scope_block(rel) == "", \
        "a delivered winner must not re-produce next turn (no double-deliver)"


def test_concern_consensus_rollback_unburns_footprint_and_files(arbiter):
    """The concern.consensus (DCC) rollback un-burns the footprint-set latch AND drops the
    files enqueued this turn — the graph-computed keys are NOT reconstructable in the flush,
    proving the value of capturing the rollback at the production site."""
    fp = frozenset({"pkg/a.py", "pkg/b.py"})
    g._dcc_latch.add(fp)
    g._dcc_enqueued_files.update({"pkg/c.py", "pkg/d.py"})
    # the EXACT closure _dcc_block attaches after its two latch updates.
    g._arm_lane_a_rearm(
        "concern.consensus",
        lambda f=fp, e=frozenset({"pkg/c.py", "pkg/d.py"}): g._dcc_latch_rollback(f, e))

    loser = ga.Candidate(plane=ga.PLANE_LANE_A, kind="concern.consensus",
                         dedup_key="dk", seq=0)
    winner = ga.Candidate(plane=ga.PLANE_GATEWAY, kind="caller_break",
                          dedup_key="wk", seq=1)
    g._global_pool_flush([(loser, lambda: None), (winner, lambda: None)],
                         kkind="post_edit", kf="", krel="")

    assert fp not in g._dcc_latch, "footprint latch must be un-burned on loss"
    assert "pkg/c.py" not in g._dcc_enqueued_files
    assert "pkg/d.py" not in g._dcc_enqueued_files


# --------------------------------------------------------------------------- #
# byte-identical OFF — the attach is a no-op when the arbiter is off
# --------------------------------------------------------------------------- #
def test_arm_rearm_is_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_lane_a_rearm_pending", {})
    g._arm_lane_a_rearm("l3b.evidence", lambda: None)
    assert g._lane_a_rearm_pending == {}, \
        "flag off -> no rollback registered (producers latch-at-production, byte-identical)"


def test_flush_ignores_kinds_without_a_rollback(arbiter):
    """A losing Lane-A kind that latches at DELIVERY (l3.contract) attaches NO rollback ->
    the generic flush lookup is a no-op (never touches a latch it does not own)."""
    delivered = []
    loser = ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="a", seq=0)
    winner = ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict",
                          dedup_key="b", seq=1)
    # no rollback was attached for l3.contract -> pending is empty for it.
    assert "l3.contract" not in g._lane_a_rearm_pending
    g._global_pool_flush([(loser, lambda: delivered.append("c")),
                          (winner, lambda: delivered.append("cv"))],
                         kkind="post_edit", kf="x.py", krel="x.py")
    assert delivered == ["cv"]  # no crash, single dose


# --------------------------------------------------------------------------- #
# finding 2b — the gateway candidate's correct-time ordinal reflects the REAL event
# --------------------------------------------------------------------------- #
class _FakeWinner:
    evidence_type = "def_ref_partition"   # localization class, boundary=1
    dedup_key = "dk"
    target = "a/x.py"
    tier = "INFO"
    confidence = 0.5


def test_gateway_candidate_ordinal_reflects_event_kind():
    """A localization gateway fact delivered on a SEARCH turn is AT its decision boundary
    (repair_support False, preventive); the SAME fact on an EDIT turn is AFTER the search
    boundary (repair_support True). RED pre-fix: the hardcoded kkind='post_edit' pinned
    current_ordinal=3 on EVERY turn -> always repair_support True (and no ev_kind param)."""
    pool = []
    g._global_pool_add_gateway(pool, _FakeWinner(), False, lambda: None, ev_kind="search")
    cand = pool[0][0]
    assert cand.current_ordinal == 1, "search=1 (was hardcoded post_edit=3)"
    assert ga.arbitrate([cand]).repair_support is False, "AT boundary -> not repair support"

    pool2 = []
    g._global_pool_add_gateway(pool2, _FakeWinner(), False, lambda: None, ev_kind="edit")
    assert pool2[0][0].current_ordinal == 3, "edit=3"
    assert ga.arbitrate([pool2[0][0]]).repair_support is True, "AFTER boundary -> repair support"
