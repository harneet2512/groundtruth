"""B-TERM (2026-07-16) — red-first tests for PRODUCT DECISION 1 (P5): the control terminal's
``mediation_correct`` gate is a REAL, producer-derived, re-verifiable check (it can FAIL), and the
causal fair-probe is ENRICHMENT that never gates completeness.

Every test asserts the NEW behavior; each fails against the pre-B-TERM grader (which hardwired
``mediation_correct=None`` and gated completeness on a hardwired-None causal probe). Two biting
mutations flip a single load-bearing input and assert the verdict flips with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# builders — exact shapes produced by _control_participation_evidence
# --------------------------------------------------------------------------- #
def _rec(feature_id, role, decision, *, seal="a" * 16, chars=12, row_index=0, fact_class="caller_contract"):
    return {
        "row_index": row_index, "feature_id": feature_id, "role": role,
        "decision": decision, "candidate_sha256_16": seal, "candidate_chars": chars,
        "candidate_id": f"{fact_class}:dedup", "fact_class": fact_class,
        "temporal_relation": "CONTROL_PRECEDES_DELIVERY",
    }


def _join(rec):
    return {**rec, "delivery_row_index": 99}


def _delivered(seal="a" * 16, chars=12):
    return {"outcome": "delivered", "content_sha256_16": seal, "chars_delivered": chars}


def _correctness(rows, records, joins):
    return metrics._control_declared_effect_correctness(rows, records, joins)


# --------------------------------------------------------------------------- #
# mediation_correct — the real check, per decision + role
# --------------------------------------------------------------------------- #
def test_mediator_applied_is_correct_iff_it_exact_joined_a_delivery() -> None:
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "APPLIED")
    # joined → True
    assert _correctness([_delivered()], {"GT_CONTRACT_NATIVE": [rec]},
                        {"GT_CONTRACT_NATIVE": [_join(rec)]})["GT_CONTRACT_NATIVE"] is True
    # BITING MUTATION 1: drop the join (declared APPLIED, nothing delivered/joined) → False, exposed.
    assert _correctness([_delivered()], {"GT_CONTRACT_NATIVE": [rec]},
                        {"GT_CONTRACT_NATIVE": []})["GT_CONTRACT_NATIVE"] is False


def test_mediator_suppressed_is_correct_iff_bytes_were_not_delivered() -> None:
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "SUPPRESSED")
    # the suppressed candidate bytes WERE delivered → the suppression is a lie → False.
    assert _correctness([_delivered()], {"GT_CONTRACT_NATIVE": [rec]},
                        {"GT_CONTRACT_NATIVE": []})["GT_CONTRACT_NATIVE"] is False
    # BITING MUTATION 2: no delivered row carries the suppressed bytes → honest suppression → True.
    assert _correctness([_delivered(seal="b" * 16)], {"GT_CONTRACT_NATIVE": [rec]},
                        {"GT_CONTRACT_NATIVE": []})["GT_CONTRACT_NATIVE"] is True


def test_eligibility_polarity_is_not_machine_derivable_so_it_is_never_graded() -> None:
    # Correct-or-quiet: a gate's APPLIED may mean "blocked" and its NO_EFFECT means "passed through"
    # (so the candidate SHOULD be delivered) — polarity is control-specific and not derivable from
    # the ledger. Grading it by delivery-presence is UNSOUND, so eligibility is always None, never a
    # guessed True/False (this is the fix for the spurious-False class on novelty/dedup/late-drop).
    for decision in ("APPLIED", "NO_EFFECT", "SUPPRESSED"):
        rec = _rec("GT_SS_NOVELTY", "eligibility", decision)
        assert _correctness([_delivered()], {"GT_SS_NOVELTY": [rec]}, {})["GT_SS_NOVELTY"] is None


def test_mediator_no_effect_that_did_not_join_is_unverifiable_none() -> None:
    # a NO_EFFECT ("delivered-but-ineffective") claim with no matching delivery is unverifiable, not
    # a lie → contributes None (never a spurious False).
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "NO_EFFECT")
    assert _correctness([_delivered(seal="b" * 16)], {"GT_CONTRACT_NATIVE": [rec]},
                        {"GT_CONTRACT_NATIVE": []})["GT_CONTRACT_NATIVE"] is None


def test_zero_candidate_no_effect_is_unverifiable_none_not_a_false() -> None:
    # a NO_EFFECT no-op with no candidate identity is honest, not a lie → contributes None.
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "NO_EFFECT", seal="", chars=0)
    assert _correctness([_delivered()], {"GT_CONTRACT_NATIVE": [rec]}, {})["GT_CONTRACT_NATIVE"] is None


def test_member_aggregation_any_false_makes_the_member_false() -> None:
    good = _rec("GT_CONTRACT_NATIVE", "mediator", "APPLIED", row_index=0)
    bad = _rec("GT_CONTRACT_NATIVE", "mediator", "APPLIED", row_index=1)
    out = _correctness([_delivered()], {"GT_CONTRACT_NATIVE": [good, bad]},
                       {"GT_CONTRACT_NATIVE": [_join(good)]})  # only ``good`` joined
    assert out["GT_CONTRACT_NATIVE"] is False


def test_no_records_is_unmeasured_none_fail_closed() -> None:
    assert _correctness([_delivered()], {}, {}) == {}


# --------------------------------------------------------------------------- #
# terminal restructure — completeness = receipt + mediated_fact_ids + mediation_correct;
# causal probe is enrichment ONLY.
# --------------------------------------------------------------------------- #
def _lifecycles():
    return {fc: metrics.new_lifecycle("fixture") for fc in ("recovery", "caller_contract")}


def test_three_gate_completeness_reachable_and_causal_probe_never_gates() -> None:
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "APPLIED")
    evidence = {
        "records": {"GT_CONTRACT_NATIVE": [rec]},
        "joins": {"GT_CONTRACT_NATIVE": [_join(rec)]},
        "correctness": {"GT_CONTRACT_NATIVE": True},
    }
    # a FALSE causal probe on the mediated class must NOT block completeness (it is enrichment).
    readiness = metrics._infra_control_readiness(
        "GT_CONTRACT_NATIVE", ("caller_contract",), _lifecycles(),
        ledger_artifact="ledger", control_evidence=evidence,
        fair_probe_by_fc={"caller_contract": False},
    )
    assert tuple(readiness["gates"]) == (
        "runtime_member_control_receipt", "mediated_fact_ids", "mediation_correct",
    )
    assert readiness["gates"]["mediation_correct"] is True
    assert readiness["infra_control_complete"] is True          # REACHABLE (was structurally not)
    assert readiness["mediation_causal_fair_probe"] is False    # reported, not gating
    assert "mediation_causal_fair_probe" not in readiness["gates"]


def test_false_mediation_correct_blocks_completeness_and_is_exposed() -> None:
    rec = _rec("GT_CONTRACT_NATIVE", "mediator", "APPLIED")
    evidence = {
        "records": {"GT_CONTRACT_NATIVE": [rec]},
        "joins": {"GT_CONTRACT_NATIVE": []},         # declared APPLIED, never joined
        "correctness": {"GT_CONTRACT_NATIVE": False},
    }
    readiness = metrics._infra_control_readiness(
        "GT_CONTRACT_NATIVE", ("caller_contract",), _lifecycles(),
        ledger_artifact="ledger", control_evidence=evidence,
    )
    assert readiness["gates"]["mediation_correct"] is False
    assert readiness["infra_control_complete"] is False
    assert "mediation_correct" in readiness["blockers"]


def test_kernel_mediator_has_no_single_mediated_class_causal_probe_is_none() -> None:
    # GT_GATEWAY mediates the empty tuple (all classes) → no single mediated FACT → None (honest).
    readiness = metrics._infra_control_readiness(
        "GT_GATEWAY", (), _lifecycles(),
        ledger_artifact="ledger",
        control_evidence={"records": {}, "joins": {}, "correctness": {}},
        fair_probe_by_fc={"caller_contract": True},
    )
    assert readiness["mediation_causal_fair_probe"] is None
