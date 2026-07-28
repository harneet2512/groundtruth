"""RED-first tests for receipt_predicates — the B-cluster Gate-4 acknowledgment evaluators.

Each registry ``receipt_predicate`` name has ONE deterministic evaluator answering: did the
agent acknowledge the delivered fact in the class's registry-specific way? True / False / None
(None = UNMEASURED, fail-closed). The class rollup is the Gate-4 value.

BITING MUTATIONS (each applied to receipt_predicates and observed to turn a passing assertion
RED, then reverted):
  M1 — OPEN receipt drops the ``not _self_acquired`` gate (``return _committed(ec)``): a
       localization the agent GREP-found itself before delivery would wrongly read True.
       ``test_open_receipt_false_when_self_acquired`` goes RED.
  M2 — COVERING receipt returns ``_committed(ec)`` WITHOUT requiring a complete attestation:
       a covering RED with no attestation would wrongly read True/False instead of None.
       ``test_covering_receipt_unmeasured_without_attestation`` goes RED.
  M3 — ``roll_up`` returns True on any True (``any`` instead of the all-True rule): a class
       with one True + one False would wrongly PASS. ``test_rollup_*`` goes RED.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import receipt_predicates as rp  # noqa: E402


def _ec(
    *,
    evidence_type: str,
    delivery_index=10,
    decision_open_index=10,
    decision_commit_index=12,
    native_acquisition_index=None,
    delivery_seal="a" * 16,
    fact_class=None,
):
    ch = SimpleNamespace(
        delivery_index=delivery_index,
        decision_open_index=decision_open_index,
        decision_commit_index=decision_commit_index,
        native_acquisition_index=native_acquisition_index,
        acknowledgment_index=None,
        action_index=None,
    )
    return SimpleNamespace(
        chronology=ch,
        evidence_type=evidence_type,
        fact_class=fact_class,
        delivery_seal=delivery_seal,
    )


def _covering_attestation(seal="a" * 16, verdict="PASS", evidence_type="covering_verdict"):
    return SimpleNamespace(
        evidence_type=evidence_type,
        delivery_seal=seal,
        truth_predicates=(SimpleNamespace(verdict=verdict),),
    )


# --------------------------------------------------------------------------- #
# every registry predicate has an evaluator (import-time self-check enforces it)
# --------------------------------------------------------------------------- #
def test_all_registry_predicates_have_an_evaluator() -> None:
    from groundtruth.runtime.fact_registry import REGISTRY

    declared = {r.receipt_predicate for r in REGISTRY.values() if r.receipt_predicate}
    assert declared <= rp.ALL_RECEIPT_PREDICATES
    assert declared  # non-empty


# --------------------------------------------------------------------------- #
# UNMEASURED precondition
# --------------------------------------------------------------------------- #
def test_unmeasured_when_delivery_or_decision_unlocated() -> None:
    # no delivery index -> the receipt is unobservable -> None (fail-closed), never a guess.
    ec = _ec(evidence_type="signature_mismatch", delivery_index=None)
    assert rp.evaluate("updated_callers_for_delta", ec) is None
    ec2 = _ec(evidence_type="signature_mismatch", decision_open_index=None)
    assert rp.evaluate("updated_callers_for_delta", ec2) is None


# --------------------------------------------------------------------------- #
# COMMIT receipts (mutation / plan / pivot classes)
# --------------------------------------------------------------------------- #
def test_commit_receipt_true_when_committed_false_when_not() -> None:
    committed = _ec(evidence_type="signature_mismatch", decision_commit_index=12)
    assert rp.evaluate("updated_callers_for_delta", committed) is True
    not_committed = _ec(evidence_type="signature_mismatch", decision_commit_index=None)
    assert rp.evaluate("updated_callers_for_delta", not_committed) is False


# --------------------------------------------------------------------------- #
# OPEN receipts (localization / def_partition) — opened WITHOUT self-searching first
# --------------------------------------------------------------------------- #
def test_open_receipt_true_when_opened_without_self_search() -> None:
    ec = _ec(
        evidence_type="localization",
        decision_commit_index=12,
        native_acquisition_index=None,
    )
    assert rp.evaluate("opened_ranked_file_without_search", ec) is True


def test_open_receipt_false_when_self_acquired() -> None:
    # M1: the agent grep-found the file (native_acquisition_index=8) BEFORE GT delivered it ->
    # GT added no value -> the OPEN receipt is False even though the file was opened.
    ec = _ec(
        evidence_type="localization",
        decision_commit_index=12,
        native_acquisition_index=8,
    )
    assert rp.evaluate("opened_ranked_file_without_search", ec) is False


def test_open_receipt_false_when_never_opened() -> None:
    ec = _ec(
        evidence_type="def_ref_partition",
        decision_commit_index=None,
        native_acquisition_index=None,
    )
    assert rp.evaluate("inspected_partitioned_def", ec) is False


# --------------------------------------------------------------------------- #
# COVERING receipt — requires a complete attestation bound to the seal
# --------------------------------------------------------------------------- #
def test_covering_receipt_true_with_complete_attestation_and_commit() -> None:
    ec = _ec(
        evidence_type="covering_verdict",
        decision_commit_index=12,
        delivery_seal="a" * 16,
    )
    atts = [_covering_attestation(seal="a" * 16, verdict="PASS")]
    assert rp.evaluate("targeted_covering_failure", ec, attestations=atts) is True


def test_covering_receipt_unmeasured_without_attestation() -> None:
    # M2: no attestation -> the executed RED cannot be tied to a targeted repair -> None
    # (UNMEASURED, fail-closed), NEVER True/False from the commit alone.
    ec = _ec(evidence_type="covering_verdict", decision_commit_index=12)
    assert rp.evaluate("targeted_covering_failure", ec, attestations=None) is None
    # an attestation for the WRONG seal is not a proof either.
    atts = [_covering_attestation(seal="b" * 16, verdict="PASS")]
    assert rp.evaluate("targeted_covering_failure", ec, attestations=atts) is None
    # an INCOMPLETE (non-PASS) attestation is not a proof.
    atts2 = [_covering_attestation(seal="a" * 16, verdict="UNMEASURED")]
    assert rp.evaluate("targeted_covering_failure", ec, attestations=atts2) is None


# --------------------------------------------------------------------------- #
# rollup (Gate-4 class value)
# --------------------------------------------------------------------------- #
def test_rollup_all_true_passes() -> None:
    assert rp.roll_up([True, True]) is True


def test_rollup_any_false_is_false() -> None:
    # M3: any False row makes the class FALSE, even mixed with True/None.
    assert rp.roll_up([True, False, None]) is False


def test_rollup_true_mixed_with_none_is_unmeasured() -> None:
    # a class does not PASS unless EVERY delivered row is True; a None row -> UNMEASURED.
    assert rp.roll_up([True, None]) is None
    assert rp.roll_up([None, None]) is None
    assert rp.roll_up([]) is None


def test_acknowledgment_by_fact_class_rolls_up_per_class() -> None:
    loc_true = _ec(evidence_type="localization", fact_class="localization",
                   decision_commit_index=12, native_acquisition_index=None)
    loc_false = _ec(evidence_type="localization", fact_class="localization",
                    decision_commit_index=12, native_acquisition_index=8)
    sig_true = _ec(evidence_type="signature_mismatch", fact_class="signature_delta",
                   decision_commit_index=12)
    by_fc = rp.acknowledgment_by_fact_class([loc_true, loc_false, sig_true])
    assert by_fc["localization"] is False  # one row self-acquired
    assert by_fc["signature_delta"] is True


def test_unknown_predicate_is_unmeasured_never_silent_true() -> None:
    ec = _ec(evidence_type="signature_mismatch")
    assert rp.evaluate("not_a_real_predicate", ec) is None


# --------------------------------------------------------------------------- #
# GROUP ASSIGNMENT — the drift that the presence check above cannot see.
#
# ``test_all_registry_predicates_have_an_evaluator`` proves every declared predicate HAS an
# evaluator. It does not prove it has the RIGHT one. The three groups are not interchangeable:
# only _OPEN_RECEIPTS applies ``not _self_acquired``. So if a new identity-shaped class lands in
# _COMMIT_RECEIPTS -- or ``localization`` is ever repointed at a COMMIT predicate -- the
# non-reacquisition test silently disappears for it and Gate 4 starts crediting rows where the
# agent grep-found the file ITSELF. That is the same over-credit failure ss_live_diagnosis
# describes when it refuses the generic consumption ladder.
#
# WHAT THE TESTS ABOVE ALREADY CATCH, measured by mutation rather than assumed:
#   M4 -- move "opened_ranked_file_without_search" from _OPEN_ to _COMMIT_RECEIPTS: caught,
#         because ``test_open_receipt_false_when_self_acquired`` asserts the OPEN semantic for
#         that exact name.
#   M5 -- repoint the ``localization`` REGISTRATION at "plan_reflects_obligations" (a name
#         already in _COMMIT_RECEIPTS): NOT caught. Every test above names its predicate as a
#         string literal, so they keep grading a predicate no class uses any more, all green,
#         while the live localization row quietly loses its non-reacquisition test.
# M5 is the gap these tests close, and it is the likelier drift: adding a class or retargeting
# one is ordinary registry work, whereas editing the module's group sets is conspicuous.
#
# The group is a prose judgement (``target_decision`` is free text -- "which file to open" vs
# "how to modify a fn" -- not an enum), so it cannot be derived. It is pinned instead: a
# regrouping must be a deliberate edit to this map.
# --------------------------------------------------------------------------- #
_RECEIPT_GROUP_BY_FACT_CLASS: dict[str, str] = {
    "caller_contract": "COMMIT",
    "cochange_prior": "COMMIT",
    "covering_red": "COVERING",
    "def_partition": "OPEN",
    "localization": "OPEN",
    "newfile_precedent": "COMMIT",
    "obligations": "COMMIT",
    "recovery": "COMMIT",
    "signature_delta": "COMMIT",
    "submit_refusal": "COMMIT",
    "syntax_result": "COMMIT",
}


def _group_of(predicate: str) -> str:
    if predicate in rp._OPEN_RECEIPTS:
        return "OPEN"
    if predicate in rp._COVERING_RECEIPTS:
        return "COVERING"
    if predicate in rp._COMMIT_RECEIPTS:
        return "COMMIT"
    return "UNGROUPED"


def test_receipt_group_per_fact_class_is_pinned() -> None:
    from groundtruth.runtime.fact_registry import REGISTRY

    observed = {
        r.fact_class: _group_of(r.receipt_predicate)
        for r in REGISTRY.values()
        if r.receipt_predicate
    }
    assert observed == _RECEIPT_GROUP_BY_FACT_CLASS, (
        "a fact class changed receipt GROUP (or a new class appeared). The groups are not "
        "interchangeable -- only OPEN applies the non-reacquisition test. Decide deliberately, "
        "then update this map."
    )


def test_open_classes_still_reject_self_acquisition_resolved_through_the_registry() -> None:
    """Resolved by FACT CLASS, not by a hardcoded predicate name.

    This is the assertion M5 trips: it asks the REGISTRY which predicate the class uses, so
    repointing ``localization`` at a COMMIT predicate name turns it RED while every
    name-literal test above stays green.
    """
    open_classes = [
        fc for fc, g in _RECEIPT_GROUP_BY_FACT_CLASS.items() if g == "OPEN"
    ]
    assert open_classes, "no OPEN classes -- the probe below would be vacuous"
    for fact_class in open_classes:
        self_acquired = _ec(
            evidence_type=fact_class,
            decision_commit_index=12,
            native_acquisition_index=8,
        )
        assert rp.acknowledgment_for_row(self_acquired) is False, fact_class
        # CALIBRATION. Without a demonstrated True the False above is unreadable -- it could
        # equally mean the class no longer resolves to any predicate at all.
        delivered_first = _ec(
            evidence_type=fact_class,
            decision_commit_index=12,
            native_acquisition_index=None,
        )
        assert rp.acknowledgment_for_row(delivered_first) is True, fact_class


def test_commit_classes_deliberately_ignore_self_acquisition() -> None:
    """The asymmetry, pinned so it stays a DECISION rather than an oversight.

    A COMMIT receipt is earned by what the agent DID with the fact (preserved the contract,
    halted on the refusal, pivoted). Whether the agent had also seen the entity on its own does
    not un-earn that. Contrast OPEN, where being told a file the agent already found is exactly
    the case where GT added nothing. This is a design assertion, not a proof the design is right
    -- but it makes "add the gate everywhere" a visible change rather than a silent one.
    """
    commit_classes = [
        fc for fc, g in _RECEIPT_GROUP_BY_FACT_CLASS.items() if g == "COMMIT"
    ]
    for fact_class in commit_classes:
        for native in (None, 8):
            ec = _ec(
                evidence_type=fact_class,
                decision_commit_index=12,
                native_acquisition_index=native,
            )
            assert rp.acknowledgment_for_row(ec) is True, (fact_class, native)
