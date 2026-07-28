"""Producer-observed substrate proof for the canonical temporal gate."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


def _revision() -> rr.RevisionVector:
    return rr.RevisionVector(
        repository_content="repo-substrate",
        graph="graph-substrate",
        lsp="lsp-substrate",
        runtime_evidence="runtime-substrate",
    )


def _syntax_record() -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-substrate",
        feature_id="syntax_result",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="src/session.py",
        claim="The edited file has a parser-confirmed syntax error.",
        actionable_consequence="Repair the syntax before continuing.",
        provenance=("src/session.py:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=_revision(),
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            "subject:src/session.py",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=rr.MandatoryReason.BLOCKER,
        token_cost=12,
        failure_prevention=10,
        causal_value=10,
        contradiction_resolution=5,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
    )


def _context(
    record: rr.EvidenceRecord,
    *,
    available_substrates: tuple[str, ...],
) -> rr.TemporalRuntimeContext:
    contract = rr.feature_contract_for(record.feature_id)
    assert contract is not None
    active = rr.ActiveDecision(
        decision_id="decision-substrate",
        context=contract.decision_context,
        primary_claim="Preserve patch validity.",
        required_roles=contract.roles,
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            "subject:src/session.py",
        ),
        token_budget=128,
        current_revision=record.revision,
    )
    return rr.TemporalRuntimeContext(
        active_decision=active,
        current_revision=record.revision,
        commitment_window=rr.CommitmentWindowState.OPEN,
        satisfied_predicates=frozenset(contract.ready_predicates),
        available_substrates=available_substrates,
    )


def test_seam_unions_only_substrates_observed_by_records() -> None:
    records = (
        SimpleNamespace(
            feature_id="syntax_result",
            observed_substrates=("parser_result",),
        ),
        SimpleNamespace(
            feature_id="caller_contract",
            observed_substrates=("graph", "lsp"),
        ),
    )

    assert seam.CanonicalRuntimeAttachment._available_substrates(records) == (
        "graph",
        "lsp",
        "parser_result",
    )


def test_feature_identity_cannot_self_attest_a_substrate() -> None:
    record = SimpleNamespace(
        feature_id="syntax_result",
        observed_substrates=(),
    )

    assert seam.CanonicalRuntimeAttachment._available_substrates((record,)) == ()


def test_missing_observed_substrate_reaches_prerequisites_pending() -> None:
    record = _syntax_record()
    contract = rr.feature_contract_for(record.feature_id)
    assert contract is not None

    evaluation = rr.evaluate_feature_contract(
        contract,
        record,
        _context(record, available_substrates=()),
    )

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.PREREQUISITES_PENDING
    )


def test_observed_parser_result_opens_the_same_gate() -> None:
    record = replace(
        _syntax_record(),
        observed_substrates=("parser_result",),
    )
    contract = rr.feature_contract_for(record.feature_id)
    assert contract is not None

    evaluation = rr.evaluate_feature_contract(
        contract,
        record,
        _context(record, available_substrates=("parser_result",)),
    )

    assert evaluation.release_allowed is True
    assert evaluation.reason is rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN
