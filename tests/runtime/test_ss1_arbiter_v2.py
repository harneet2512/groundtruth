"""SS-1 GT_SS_ARBITER_V2 — global_arbiter.arbitrate v2 behaviors (pure-unit, Stage-1).

From the 29-task causal audit of run 29236533134:
  (a) empty-payload guard — a candidate that will render ZERO bytes must NEVER win the dose
      / produce a delivered ledger row (the hydra-3005 chars=0 / empty-seal delivered row);
  (b) repair_support RELAXATION — a preventive scope/companion fact whose pre-submit
      COMPLETENESS decision point is still LIVE (obligations_open) is NOT labeled
      repair_support, so the seam does not late-suppress it (fixes consensus.scope, the ONLY
      class with a proven consumption, suppressed 18x as repair_support_late);
  (c) retire-vs-DEFER — a localization fact is RETIRED only when genuinely REDUNDANT with an
      already-delivered def_ref_partition; a merely-late localization fact is NOT retired
      (it stays eligible and defers via the seam's repair_support-driven re-arm).

Every test passes ``ss_v2`` EXPLICITLY (the ranking core stays pure). RED-first: each guard
has >=2 biting mutations (removing the branch reddens a green assertion), plus the
flag-off / inert-default byte-identity pin.
"""
from __future__ import annotations

from groundtruth.runtime.global_arbiter import (
    PLANE_GATEWAY,
    PLANE_LANE_A,
    PLANE_STEER,
    REASON_OUTRANKED,
    REASON_REDUNDANT,
    REASON_SS_EMPTY_PAYLOAD,
    Candidate,
    arbitrate,
)


def _c(plane, kind, **kw):
    return Candidate(plane=plane, kind=kind, dedup_key=kw.pop("dedup_key", kind + ":k"), **kw)


# --------------------------------------------------------------------------- #
# (a) EMPTY-PAYLOAD GUARD — rendered_chars == 0 never wins / never delivers.
# --------------------------------------------------------------------------- #
def test_empty_payload_candidate_never_wins_under_v2():
    # covering_verdict is the HIGHEST ladder class (70) — but empty -> it must lose to the
    # non-empty recovery fact. MUTATION[drop the rendered_chars==0 branch] -> empty wins, RED.
    empty = _c(PLANE_GATEWAY, "covering_verdict", rendered_chars=0)
    ok = _c(PLANE_STEER, "l5.stuck", rendered_chars=12)
    res = arbitrate([empty, ok], ss_v2=True)
    assert res.winner is ok
    assert (empty, REASON_SS_EMPTY_PAYLOAD) in res.losers


def test_empty_payload_only_candidate_yields_silence_under_v2():
    empty = _c(PLANE_GATEWAY, "covering_verdict", rendered_chars=0)
    res = arbitrate([empty], ss_v2=True)
    assert res.winner is None  # MUTATION[drop guard] -> winner is empty, RED
    assert res.losers == [(empty, REASON_SS_EMPTY_PAYLOAD)]


def test_unknown_rendered_chars_is_never_empty_rejected():
    # -1 (the DEFAULT == unknown) is SKIPPED even under v2 -> the fact competes normally, so a
    # candidate built WITHOUT the signal is unaffected (byte-identical to pre-SS-1).
    c = _c(PLANE_GATEWAY, "covering_verdict")  # rendered_chars defaults -1
    assert arbitrate([c], ss_v2=True).winner is c
    c2 = _c(PLANE_GATEWAY, "covering_verdict", rendered_chars=7)  # non-empty -> allowed
    assert arbitrate([c2], ss_v2=True).winner is c2


def test_empty_payload_ignored_when_v2_off():
    # v2 OFF -> the pre-SS-1 engine ignores rendered_chars entirely; the empty fact wins.
    empty = _c(PLANE_GATEWAY, "covering_verdict", rendered_chars=0)
    assert arbitrate([empty], ss_v2=False).winner is empty


# --------------------------------------------------------------------------- #
# (b) REPAIR-SUPPORT RELAXATION — obligations_open + preventive + late -> NOT repair.
# --------------------------------------------------------------------------- #
def test_repair_support_relaxed_when_obligations_open():
    # consensus.scope (localization = a PREVENTIVE class) delivered LATE (cur 4 > boundary 1)
    # at a review turn while obligations remain OPEN -> its decision point is still live, so it
    # is NOT repair support and the seam must not late-suppress it.
    late = _c(PLANE_LANE_A, "consensus.scope",
              boundary_ordinal=1, current_ordinal=4, obligations_open=True)
    res = arbitrate([late], ss_v2=True)
    assert res.winner is late
    assert res.repair_support is False  # MUTATION[drop relaxation] -> True (late-suppressed), RED


def test_repair_support_holds_when_obligations_clean():
    # SAME late preventive fact, obligations CLOSED -> repair_support True (suppress only after
    # a clean submit-gate pass); the seam then defers-and-refires it. MUTATION[always relax] ->
    # False here, RED.
    late = _c(PLANE_LANE_A, "consensus.scope",
              boundary_ordinal=1, current_ordinal=4, obligations_open=False)
    assert arbitrate([late], ss_v2=True).repair_support is True


def test_relaxation_is_preventive_only():
    # a REACTIVE class (recovery) late with obligations_open -> still repair_support True (the
    # relaxation is preventive-only; reactive lateness is its nature). MUTATION[relax all
    # classes] -> False here, RED.
    late = _c(PLANE_STEER, "l5.stuck",
              boundary_ordinal=1, current_ordinal=4, obligations_open=True)
    assert arbitrate([late], ss_v2=True).repair_support is True


def test_relaxation_inert_when_v2_off():
    late = _c(PLANE_LANE_A, "consensus.scope",
              boundary_ordinal=1, current_ordinal=4, obligations_open=True)
    # pre-SS-1: late == repair support regardless of obligations_open.
    assert arbitrate([late], ss_v2=False).repair_support is True


# --------------------------------------------------------------------------- #
# (c) RETIRE-vs-DEFER — retire ONLY when redundant with a delivered def_partition.
# --------------------------------------------------------------------------- #
def test_redundant_localization_is_retired_under_v2():
    redundant = _c(PLANE_GATEWAY, "def_ref_partition", redundant_with_delivered=True)
    rec = _c(PLANE_STEER, "l5.no_test", dedup_key="rec")
    res = arbitrate([redundant, rec], ss_v2=True)
    assert res.winner is rec
    assert (redundant, REASON_REDUNDANT) in res.losers  # MUTATION[drop retire] -> loc competes, RED


def test_late_localization_not_redundant_is_NOT_retired_defers():
    # a LATE localization fact that is NOT redundant must NOT be retired: it stays eligible
    # (here the sole candidate -> it WINS) and its repair_support drives the seam's
    # defer-and-refire. MUTATION[retire all late localization] -> winner None, RED.
    late = _c(PLANE_GATEWAY, "def_ref_partition", boundary_ordinal=1, current_ordinal=3)
    res = arbitrate([late], ss_v2=True)
    assert res.winner is late
    assert res.repair_support is True  # late (no open obligations) -> seam defers


def test_redundant_flag_only_retires_localization_class():
    # redundant_with_delivered on a NON-localization class is IGNORED (retire is localization-
    # only — a caller_break is never "redundant with a def_partition").
    c = _c(PLANE_GATEWAY, "caller_break", redundant_with_delivered=True)
    assert arbitrate([c], ss_v2=True).winner is c


def test_redundant_inert_when_v2_off():
    c = _c(PLANE_GATEWAY, "def_ref_partition", redundant_with_delivered=True)
    assert arbitrate([c], ss_v2=False).winner is c


# --------------------------------------------------------------------------- #
# BYTE-IDENTITY — inert defaults + flag-off reproduce the pre-SS-1 engine exactly.
# --------------------------------------------------------------------------- #
def _mixed_pool():
    return [
        _c(PLANE_LANE_A, "l3.contract", seq=0),                                   # caller_contract 40
        _c(PLANE_GATEWAY, "def_ref_partition", seq=1,
           boundary_ordinal=1, current_ordinal=3),                               # localization 20 (late)
        _c(PLANE_STEER, "l5.stuck", seq=2),                                       # recovery 10
    ]


def test_arbitrate_v2_off_is_byte_identical():
    # With the NEW fields at their inert defaults, v2 ON and v2 OFF yield the SAME winner, the
    # SAME repair_support, and the SAME ordered loser reasons (every v2 branch is a no-op).
    off = arbitrate(_mixed_pool(), ss_v2=False)
    on = arbitrate(_mixed_pool(), ss_v2=True)
    assert off.winner.dedup_key == on.winner.dedup_key == "l3.contract:k"
    assert off.repair_support == on.repair_support is False
    assert [r for _c2, r in off.losers] == [r for _c2, r in on.losers] == [REASON_OUTRANKED, REASON_OUTRANKED]


def test_env_activation_via_none_default(monkeypatch):
    # ss_v2=None resolves GT_SS_ARBITER_V2 from env — the unedited-seam activation path.
    empty = _c(PLANE_GATEWAY, "covering_verdict", rendered_chars=0)
    ok = _c(PLANE_STEER, "l5.stuck", rendered_chars=5)
    monkeypatch.delenv("GT_SS_ARBITER_V2", raising=False)
    assert arbitrate([empty, ok]).winner is empty     # flag unset -> v2 off -> empty ignored -> highest wins
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    assert arbitrate([empty, ok]).winner is ok        # flag set -> empty rejected (winner flips)


def test_leak_zero_new_fields_are_scalars():
    # SS-1 added ONLY scalar signals (int/bool) — no free-text surface, so leak=0 by
    # construction is preserved (a delivered/returned value can carry no test identity).
    f = Candidate.__dataclass_fields__
    for new in ("rendered_chars", "obligations_open", "redundant_with_delivered"):
        assert new in f
    assert "payload" not in f and "provenance" not in f and "output" not in f
