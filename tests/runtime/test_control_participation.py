from __future__ import annotations

import pytest

from groundtruth.runtime.control_participation import (
    CONTROL_DECISION_CONTRACTS,
    CONTROL_PARTICIPATION_SCHEMA,
    ControlParticipation,
    build_control_participation,
    control_contract,
    participation_to_dict,
)
from groundtruth.runtime.feature_lineage import CAP_ELIGIBILITY_IDS, CAP_MEDIATOR_IDS


def test_contract_is_total_for_all_decision_controls() -> None:
    assert set(CONTROL_DECISION_CONTRACTS) == set(CAP_ELIGIBILITY_IDS | CAP_MEDIATOR_IDS)
    assert len(CONTROL_DECISION_CONTRACTS) == 39
    assert control_contract("GT_SS_NOVELTY").measurement_status == "SUPPORTED"
    assert all(
        contract.measurement_status == "SUPPORTED"
        for contract in CONTROL_DECISION_CONTRACTS.values()
    )
    assert all(
        CONTROL_DECISION_CONTRACTS[feature_id].role == "mediator"
        and CONTROL_DECISION_CONTRACTS[feature_id].fact_class_required
        for feature_id in CAP_MEDIATOR_IDS
    )
    assert CONTROL_DECISION_CONTRACTS[
        "GT_SS_ACK_METRICS"
    ].temporal_relation == "RECEIPT_FOLLOWS_DELIVERY"
    assert all(
        contract.temporal_relation == "CONTROL_PRECEDES_DELIVERY"
        for feature_id, contract in CONTROL_DECISION_CONTRACTS.items()
        if feature_id != "GT_SS_ACK_METRICS"
    )


def test_participation_is_typed_and_bound_to_exact_candidate_bytes() -> None:
    record = build_control_participation(
        feature_id="GT_SS_NOVELTY",
        decision_site="mini_seam.ss_content_decision.novelty",
        decision="NO_EFFECT",
        iteration=7,
        candidate_bytes="rendered fact\n",
        fact_class=None,
        reason="novel_entity",
    )
    payload = participation_to_dict(record)
    assert payload == {
        "schema": CONTROL_PARTICIPATION_SCHEMA,
        "control_ref": {
            "category": "CAP", "feature_id": "GT_SS_NOVELTY",
            "role": "eligibility",
        },
        "decision_site": "mini_seam.ss_content_decision.novelty",
        "decision": "NO_EFFECT",
        "iteration": 7,
        "candidate_chars": 14,
        "candidate_sha256_16": "63352a96c04b446d",
        "fact_class": None,
        "candidate_id": "",
        "temporal_relation": "CONTROL_PRECEDES_DELIVERY",
        "related_delivery_iteration": None,
        "reason": "novel_entity",
    }
    assert "features" not in payload
    assert "feature_ids" not in payload


def test_unknown_site_cannot_forge_participation() -> None:
    with pytest.raises(ValueError, match="unsupported decision site"):
        build_control_participation(
            feature_id="GT_SS_NOVELTY",
            decision_site="enabled.flag",
            decision="APPLIED",
            iteration=1,
        )
    with pytest.raises(ValueError, match="unsupported decision site"):
        build_control_participation(
            feature_id="GT_BRIEF_MINIMAL",
            decision_site="pretask.brief.minimal",
            decision="APPLIED",
            iteration=1,
        )


def test_direct_construction_rejects_byte_identity_half_pairs() -> None:
    with pytest.raises(ValueError, match="candidate byte identity"):
        ControlParticipation(
            schema=CONTROL_PARTICIPATION_SCHEMA,
            feature_id="GT_SS_NOVELTY",
            role="eligibility",
            decision_site="mini_seam.ss_content_decision.novelty",
            decision="NO_EFFECT",
            iteration=0,
            candidate_chars=4,
            candidate_sha256_16="",
            fact_class=None,
            reason="",
        )


@pytest.mark.parametrize("field,value", [("iteration", True), ("iteration", -1),
                                           ("candidate_chars", False),
                                           ("candidate_chars", -1)])
def test_direct_construction_rejects_bool_and_negative_integer_fields(
    field: str, value: object,
) -> None:
    kwargs = dict(
        schema=CONTROL_PARTICIPATION_SCHEMA,
        feature_id="GT_SS_NOVELTY",
        role="eligibility",
        decision_site="mini_seam.ss_content_decision.novelty",
        decision="NO_EFFECT",
        iteration=0,
        candidate_chars=0,
        candidate_sha256_16="",
        fact_class=None,
        reason="",
    )
    kwargs[field] = value
    with pytest.raises(ValueError, match="non-negative integer"):
        ControlParticipation(**kwargs)


def test_mediator_requires_registered_concrete_fact_identity() -> None:
    record = build_control_participation(
        feature_id="GT_XSESSION_RANKUP",
        decision_site="gateway.xsession_rankup.boost",
        decision="APPLIED",
        iteration=2,
        candidate_bytes="post-control bytes",
        fact_class="caller_contract",
        candidate_id="candidate-1",
    )
    assert participation_to_dict(record)["fact_class"] == "caller_contract"
    for bad in (None, "", "*", "not_registered"):
        with pytest.raises(ValueError, match="fact_class"):
            build_control_participation(
                feature_id="GT_XSESSION_RANKUP",
                decision_site="gateway.xsession_rankup.boost",
                decision="APPLIED",
                iteration=2,
                candidate_bytes="post-control bytes",
                fact_class=bad,
                candidate_id="candidate-1",
            )

    with pytest.raises(ValueError, match="candidate_id"):
        build_control_participation(
            feature_id="GT_XSESSION_RANKUP",
            decision_site="gateway.xsession_rankup.boost",
            decision="APPLIED",
            iteration=2,
            candidate_bytes="post-control bytes",
            fact_class="caller_contract",
        )


def test_candidate_chars_counts_unicode_characters_not_utf8_octets() -> None:
    record = build_control_participation(
        feature_id="GT_SS_NOVELTY",
        decision_site="mini_seam.ss_content_decision.novelty",
        decision="NO_EFFECT",
        iteration=0,
        candidate_bytes="é🙂",
    )
    assert record.candidate_chars == 2


def test_direct_construction_rejects_malformed_or_missing_reason() -> None:
    base = dict(
        schema=CONTROL_PARTICIPATION_SCHEMA,
        feature_id="GT_SS_NOVELTY", role="eligibility",
        decision_site="mini_seam.ss_content_decision.novelty",
        decision="NO_EFFECT", iteration=0, candidate_chars=0,
        candidate_sha256_16="", fact_class=None, candidate_id="",
    )
    with pytest.raises(ValueError, match="reason must be a string"):
        ControlParticipation(**base, reason={})
    with pytest.raises(ValueError, match="requires a non-empty reason"):
        ControlParticipation(**{**base, "decision": "SUPPRESSED"}, reason="")


def test_direct_construction_cannot_bypass_declared_decision_site() -> None:
    with pytest.raises(ValueError, match="unsupported decision site"):
        ControlParticipation(
            schema=CONTROL_PARTICIPATION_SCHEMA,
            feature_id="GT_BRIEF_MINIMAL",
            role="eligibility",
            decision_site="enabled.flag_exists",
            decision="APPLIED",
            iteration=0,
            candidate_chars=0,
            candidate_sha256_16="",
            fact_class=None,
            reason="",
        )


def test_error_is_explicit_and_not_a_success_decision() -> None:
    record = build_control_participation(
        feature_id="GT_SS_EXEC_TRUTH",
        decision_site="mini_seam.covering_selection.runner_eligibility",
        decision="ERROR",
        iteration=3,
        reason="runner_filter_error:OSError",
    )
    assert participation_to_dict(record)["decision"] == "ERROR"
