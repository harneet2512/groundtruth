from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import ss_live_diagnosis as diagnosis  # noqa: E402


def _metric(value: object) -> dict[str, object]:
    return {"value": value, "status": "MEASURED"}


def _opportunity(*, delivery_eligible: bool) -> dict[str, object]:
    return {
        "status": "BOUND",
        "reason": "producer_matched_typed_lineage",
        # The collector's historical field means "candidate existed".  The
        # per-boundary delivery_eligible bit is the terminal eligibility fact.
        "eligible_opportunity": True,
        "opportunity_count": 1,
        "delivery_eligible_count": int(delivery_eligible),
        "selected_count": int(delivery_eligible),
        "decision_boundary_evidence": [{
            "delivery_eligible": delivery_eligible,
            "selected": delivery_eligible,
            "parent_policy_joined": True,
        }],
    }


def test_bound_opportunity_is_cardinal_for_delivery_terminal_states() -> None:
    lifecycle = {
        # Deliberately contradict opportunity evidence.  Terminal diagnosis
        # must not borrow this broad heuristic.
        "eligible": _metric(True),
        "produced": _metric(False),
    }

    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, {"gates": {}}, [], [],
        _opportunity(delivery_eligible=False), artifacts_attested=True,
    ) == "NOT_ELIGIBLE"

    lifecycle["eligible"] = _metric(False)
    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, {"gates": {}}, [], [],
        _opportunity(delivery_eligible=True), artifacts_attested=True,
    ) == "DARK_ELIGIBLE_NO_PRODUCER"


def test_dark_requires_bound_opportunity_and_complete_attestation() -> None:
    lifecycle = {"eligible": _metric(True), "produced": _metric(False)}
    readiness = {"gates": {}}

    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness, [], [],
        _opportunity(delivery_eligible=True), artifacts_attested=False,
    ) == "UNMEASURED:artifact_attestation_incomplete"
    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness, [], [],
        None, artifacts_attested=True,
    ) == "UNMEASURED:no_bound_opportunity"
    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness, [], [],
        {"status": "BOUND", "decision_boundary_evidence": "forged"},
        artifacts_attested=True,
    ) == "UNMEASURED:malformed_bound_opportunity"
    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT", lifecycle, readiness, [], [],
        {"status": "UNMEASURED", "reason": "producer_binding"},
        artifacts_attested=True,
    ) == "UNMEASURED:producer_binding"


def test_selected_candidate_must_be_delivery_eligible() -> None:
    impossible = _opportunity(delivery_eligible=False)
    evidence = impossible["decision_boundary_evidence"]
    assert isinstance(evidence, list)
    evidence[0]["selected"] = True
    impossible["selected_count"] = 1

    assert diagnosis.classify_bound_delivery_feature(
        "caller_contract", "FACT",
        {"eligible": _metric(True), "produced": _metric(False)},
        {"gates": {}}, [], [], impossible, artifacts_attested=True,
    ) == "UNMEASURED:malformed_bound_opportunity"
