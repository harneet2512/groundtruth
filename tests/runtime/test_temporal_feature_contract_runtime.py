"""Wave-5 RED contract for executable temporal feature contracts.

Feature contracts are runtime policy, not documentation.  The same typed
predicates and deterministic context must drive readiness, relevance,
commitment timing, expiry, revision invalidation, and fallback assurance.
"""
from __future__ import annotations

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)


def _decision(
    *,
    decision_id: str = "refresh-session",
    context=rr.DecisionContext.PATCH_CONSTRUCTION,
    neighborhood=("decision:refresh-session", "symbol:refreshSession"),
):
    return rr.ActiveDecision(
        decision_id=decision_id,
        context=context,
        primary_claim="Preserve refreshSession caller behavior.",
        required_roles=(
            rr.EvidenceRole.BEHAVIORAL_CONTRACT,
            rr.EvidenceRole.AFFECTED_CALLER,
        ),
        causal_neighborhood=neighborhood,
        token_budget=180,
        current_revision=REVISION,
    )


def _evidence(**overrides):
    values = {
        "evidence_id": "GT-E144",
        "feature_id": "caller_contract",
        "decision_context": rr.DecisionContext.PATCH_CONSTRUCTION,
        "roles": (
            rr.EvidenceRole.BEHAVIORAL_CONTRACT,
            rr.EvidenceRole.AFFECTED_CALLER,
        ),
        "subject": "src/auth/session.ts::refreshSession",
        "claim": "Two production callers consume the returned Session.",
        "actionable_consequence": "Preserve caller-visible return semantics.",
        "provenance": ("src/auth/middleware.ts::refreshRequest",),
        "grade": rr.EvidenceGrade.VERIFIED,
        "authority": rr.Authority.RESULT_DERIVED,
        "revision": REVISION,
        "causal_neighborhood": (
            "decision:refresh-session",
            "symbol:refreshSession",
        ),
        "lifecycle": rr.EvidenceLifecycle.PENDING,
        "fresh": True,
        "already_visible": False,
        "superseded": False,
        "mandatory_reason": None,
        "token_cost": 40,
        "failure_prevention": 5,
        "causal_value": 4,
        "contradiction_resolution": 0,
        "anchoring_risk": 0,
        "revision_dependencies": ("nodes", "edges", "props_rev"),
        "transition_history": (),
        "observed_substrates": ("graph", "lsp"),
    }
    values.update(overrides)
    return rr.EvidenceRecord(**values)


def _context(
    contract,
    *,
    active_decision=None,
    satisfied_predicates=None,
    commitment_window=None,
    current_revision=REVISION,
):
    if satisfied_predicates is None:
        satisfied_predicates = frozenset(contract.ready_predicates)
    if commitment_window is None:
        commitment_window = rr.CommitmentWindowState.NOT_OPEN
    return rr.TemporalRuntimeContext(
        active_decision=active_decision,
        satisfied_predicates=frozenset(satisfied_predicates),
        commitment_window=commitment_window,
        current_revision=current_revision,
        available_substrates=("graph", "lsp", "ast_references"),
    )


def test_all_17_contracts_use_typed_executable_predicates() -> None:
    assert len(rr.FEATURE_CONTRACTS) == 17
    for contract in rr.FEATURE_CONTRACTS.values():
        assert contract.ready_predicates
        assert contract.relevance_predicates
        assert contract.commitment_predicates
        assert contract.expiry_predicates
        assert all(
            isinstance(predicate, rr.TemporalPredicate)
            for predicates in (
                contract.ready_predicates,
                contract.relevance_predicates,
                contract.commitment_predicates,
                contract.expiry_predicates,
            )
            for predicate in predicates
        )


def test_pending_remains_pending_until_every_ready_predicate_is_true() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    missing_one = frozenset(contract.ready_predicates[:-1])

    evaluation = rr.evaluate_feature_contract(
        contract,
        _evidence(),
        _context(contract, satisfied_predicates=missing_one),
    )

    assert evaluation.ready is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.PENDING
    assert evaluation.unsatisfied_predicates == (contract.ready_predicates[-1],)


def test_pending_becomes_ready_only_after_all_ready_predicates() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None

    evaluation = rr.evaluate_feature_contract(
        contract,
        _evidence(),
        _context(contract),
    )

    assert evaluation.ready is True
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.READY
    assert evaluation.reason is rr.EvidenceTransitionReason.READINESS_RULES_SATISFIED


@pytest.mark.parametrize(
    "active_decision",
    (
        _decision(
            decision_id="other-patch",
            neighborhood=("decision:other-patch", "symbol:refreshSession"),
        ),
        _decision(
            context=rr.DecisionContext.COMPLETION,
            neighborhood=("decision:refresh-session", "symbol:refreshSession"),
        ),
        _decision(
            neighborhood=("symbol:unrelated",),
        ),
    ),
)
def test_relevance_requires_exact_active_decision_and_graph_connectivity(
    active_decision,
) -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None

    evaluation = rr.evaluate_feature_contract(
        contract,
        _evidence(lifecycle=rr.EvidenceLifecycle.READY),
        _context(contract, active_decision=active_decision),
    )

    assert evaluation.relevant is False
    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD


def test_release_occurs_only_while_commitment_window_is_open() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    evidence = _evidence(lifecycle=rr.EvidenceLifecycle.READY)

    before = rr.evaluate_feature_contract(
        contract,
        evidence,
        _context(
            contract,
            active_decision=_decision(),
            commitment_window=rr.CommitmentWindowState.NOT_OPEN,
        ),
    )
    open_window = rr.evaluate_feature_contract(
        contract,
        evidence,
        _context(
            contract,
            active_decision=_decision(),
            commitment_window=rr.CommitmentWindowState.OPEN,
        ),
    )

    assert before.relevant is True
    assert before.release_allowed is False
    assert before.next_lifecycle is rr.EvidenceLifecycle.READY
    assert open_window.release_allowed is True
    assert open_window.next_lifecycle is rr.EvidenceLifecycle.RELEASED
    assert open_window.reason is rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN


def test_closed_commitment_window_expires_evidence_deterministically() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    context = _context(
        contract,
        active_decision=_decision(),
        commitment_window=rr.CommitmentWindowState.CLOSED,
    )
    evidence = _evidence(lifecycle=rr.EvidenceLifecycle.READY)

    first = rr.evaluate_feature_contract(contract, evidence, context)
    second = rr.evaluate_feature_contract(contract, evidence, context)

    assert first == second
    assert first.expired is True
    assert first.invalidated is False
    assert first.next_lifecycle is rr.EvidenceLifecycle.EXPIRED
    assert first.reason is rr.EvidenceTransitionReason.DECISION_WINDOW_EXPIRED


def test_revision_change_invalidates_before_any_release_or_expiry() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    changed = rr.RevisionVector(
        repository_content="repo-1",
        graph="graph-2",
        lsp="lsp-1",
        runtime_evidence="runtime-1",
    )

    evaluation = rr.evaluate_feature_contract(
        contract,
        _evidence(lifecycle=rr.EvidenceLifecycle.READY),
        _context(
            contract,
            active_decision=_decision(),
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=changed,
        ),
    )

    assert evaluation.invalidated is True
    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.INVALIDATED
    assert evaluation.reason is (
        rr.EvidenceTransitionReason.REVISION_DEPENDENCY_CHANGED
    )


def test_evidence_must_match_exact_registered_contract_roles() -> None:
    with pytest.raises(ValueError, match="exact|feature contract|role"):
        _evidence(roles=(rr.EvidenceRole.BEHAVIORAL_CONTRACT,))


@pytest.mark.parametrize(
    "dependencies",
    (
        ("nodes", "edges"),
        ("nodes", "edges", "props_rev", "lsp"),
    ),
)
def test_evidence_must_match_exact_registered_revision_dependencies(
    dependencies,
) -> None:
    with pytest.raises(
        ValueError,
        match="revision.dependencies|feature contract|exact",
    ):
        _evidence(revision_dependencies=dependencies)


def test_every_feature_has_an_explicit_typed_fallback_assurance_policy() -> None:
    for feature_id, contract in rr.FEATURE_CONTRACTS.items():
        policy = contract.fallback_policy
        assert isinstance(policy, rr.FeatureFallbackPolicy)
        assert policy.feature_id == feature_id
        assert policy.preferred_substrates
        assert policy.fallback_substrates
        assert isinstance(policy.minimum_grade, rr.EvidenceGrade)
        assert isinstance(policy.minimum_authority, rr.Authority)
        assert not set(policy.preferred_substrates).intersection(
            policy.fallback_substrates
        )


def test_caller_contract_fallback_retains_a_verified_reference_floor() -> None:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    policy = contract.fallback_policy

    assert policy.preferred_substrates == ("graph", "lsp")
    assert policy.fallback_substrates == (
        "ast_references",
        "exact_lexical_references",
    )
    assert policy.minimum_grade is rr.EvidenceGrade.VERIFIED
    assert policy.minimum_authority >= rr.Authority.RESULT_DERIVED


def test_fallback_policy_is_not_derived_from_freshness_dependencies() -> None:
    assert not hasattr(rr, "_fallbacks_for_dependencies")
    definition = rr.feature_contract_for("def_partition")
    signature = rr.feature_contract_for("signature_delta")
    assert definition is not None and signature is not None
    assert definition.revision_dependencies == signature.revision_dependencies
    assert definition.fallback_policy.feature_id == "def_partition"
    assert signature.fallback_policy.feature_id == "signature_delta"
