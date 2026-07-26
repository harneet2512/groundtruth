"""RED contract for the exact 17-feature deterministic runtime registry.

The feature universe is not copied from a prose plan.  It is the union of the
ten current model-facing FACT registrations and the seven exact CAP byte
owners.  ``cochange_prior`` remains registered internal support and is
therefore intentionally outside this 17-feature live-delivery tranche.
"""

from __future__ import annotations

import json

import pytest

from groundtruth.runtime import fact_registry
from groundtruth.runtime import feature_lineage
from groundtruth.runtime import reasoning_runtime as rr


EXPECTED_FACT_IDS = {
    "caller_contract",
    "covering_red",
    "def_partition",
    "localization",
    "newfile_precedent",
    "obligations",
    "recovery",
    "signature_delta",
    "submit_refusal",
    "syntax_result",
}
EXPECTED_CAP_IDS = {
    "GT_CHANGE_SURFACE",
    "GT_PATCH_DELTA",
    "GT_LOC_RESLOT",
    "GT_SS_SUBMIT_RED",
    "GT_EDIT_CHECK",
    "GT_HYPOTHESIS",
    "GT_CERT_DELIVERY",
}
EXPECTED_FEATURE_IDS = EXPECTED_FACT_IDS | EXPECTED_CAP_IDS


def test_exact_17_are_derived_from_current_fact_and_lineage_authorities() -> None:
    current_delivery_facts = {
        feature_id
        for feature_id, registration in fact_registry.REGISTRY.items()
        if registration.fact_role == fact_registry.FACT_ROLE_DELIVERY
    }
    assert current_delivery_facts == EXPECTED_FACT_IDS
    assert set(feature_lineage.CAP_BYTE_OWNER_IDS) == EXPECTED_CAP_IDS
    assert current_delivery_facts | set(feature_lineage.CAP_BYTE_OWNER_IDS) == (
        EXPECTED_FEATURE_IDS
    )
    assert len(EXPECTED_FEATURE_IDS) == 17


def test_feature_contract_registry_covers_exactly_the_17_live_features() -> None:
    assert set(rr.FEATURE_CONTRACTS) == EXPECTED_FEATURE_IDS
    assert len(rr.FEATURE_CONTRACTS) == 17
    assert rr.feature_contract_for("cochange_prior") is None
    assert rr.feature_contract_for("not-a-feature") is None


def test_every_feature_contract_is_decision_complete_and_revision_bound() -> None:
    for feature_id in sorted(EXPECTED_FEATURE_IDS):
        contract = rr.feature_contract_for(feature_id)
        assert contract is not None
        assert contract.feature_id == feature_id
        assert contract.failure_definition.strip()
        assert isinstance(contract.decision_context, rr.DecisionContext)
        assert contract.roles
        assert len(contract.roles) == len(set(contract.roles))
        assert all(isinstance(role, rr.EvidenceRole) for role in contract.roles)
        assert contract.ready_rules
        assert contract.relevance_rules
        assert contract.commitment_rules
        assert contract.expiry_rules
        assert contract.revision_dependencies
        assert contract.fallback_substrates
        assert all(value.strip() for value in contract.ready_rules)
        assert all(value.strip() for value in contract.relevance_rules)
        assert all(value.strip() for value in contract.commitment_rules)
        assert all(value.strip() for value in contract.expiry_rules)
        assert all(value.strip() for value in contract.revision_dependencies)
        assert all(value.strip() for value in contract.fallback_substrates)


def test_fact_contract_revision_dependencies_match_the_existing_registry() -> None:
    for feature_id in sorted(EXPECTED_FACT_IDS):
        contract = rr.feature_contract_for(feature_id)
        registration = fact_registry.REGISTRY[feature_id]
        assert contract.revision_dependencies == registration.freshness_deps


@pytest.mark.parametrize(
    ("owner", "fact"),
    (
        ("GT_CHANGE_SURFACE", "newfile_precedent"),
        ("GT_PATCH_DELTA", "signature_delta"),
        ("GT_LOC_RESLOT", "localization"),
        ("GT_SS_SUBMIT_RED", "submit_refusal"),
        ("GT_EDIT_CHECK", "syntax_result"),
        ("GT_HYPOTHESIS", "recovery"),
        ("GT_CERT_DELIVERY", "submit_refusal"),
    ),
)
def test_byte_owner_contracts_serve_the_same_decision_as_their_fact(
    owner: str,
    fact: str,
) -> None:
    owner_contract = rr.feature_contract_for(owner)
    fact_contract = rr.feature_contract_for(fact)
    assert owner_contract.decision_context is fact_contract.decision_context
    assert set(owner_contract.roles) <= set(fact_contract.roles) | {
        rr.EvidenceRole.TERMINAL_ASSURANCE,
    }
    assert owner_contract.revision_dependencies == fact_contract.revision_dependencies


def test_contract_registry_is_immutable_and_serializes_deterministically() -> None:
    with pytest.raises(TypeError):
        rr.FEATURE_CONTRACTS["invented"] = rr.FEATURE_CONTRACTS["localization"]

    first = rr.feature_contract_registry_json()
    second = rr.feature_contract_registry_json()
    assert first == second
    payload = json.loads(first)
    assert list(payload) == sorted(EXPECTED_FEATURE_IDS)
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == first

