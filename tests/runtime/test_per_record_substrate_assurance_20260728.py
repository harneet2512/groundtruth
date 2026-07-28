"""A temporal release may use only substrates observed by that evidence record."""

from __future__ import annotations

from dataclasses import replace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from tests.runtime.test_substrate_gate_tautology_20260726 import (
    _context as _syntax_context,
    _syntax_record,
)
from tests.runtime.test_wave6_lipi_red_20260725 import (
    _decision as _caller_decision,
    _evidence as _caller_evidence,
)


def _evaluate_syntax(
    record: rr.EvidenceRecord,
    *,
    available: tuple[str, ...],
) -> rr.TemporalContractEvaluation:
    contract = rr.feature_contract_for("syntax_result")
    assert contract is not None
    return rr.evaluate_feature_contract(
        contract,
        record,
        _syntax_context(record, available_substrates=available),
    )


def test_other_record_cannot_lend_parser_assurance_to_target_record() -> None:
    observed = replace(
        _syntax_record(),
        evidence_id="GT-E-parser-owner",
        observed_substrates=("parser_result",),
    )
    target = replace(
        _syntax_record(),
        evidence_id="GT-E-parser-target",
        observed_substrates=(),
    )
    attempt_wide = seam.CanonicalRuntimeAttachment._available_substrates(
        (observed, target)
    )
    assert attempt_wide == ("parser_result",)

    evaluation = _evaluate_syntax(target, available=attempt_wide)

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.PREREQUISITES_PENDING
    )


def test_target_observing_preferred_parser_result_releases() -> None:
    target = replace(
        _syntax_record(),
        observed_substrates=("parser_result",),
    )

    evaluation = _evaluate_syntax(
        target,
        available=("parser_result",),
    )

    assert evaluation.release_allowed is True
    assert evaluation.reason is rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN


def test_target_observing_assured_fallback_releases() -> None:
    target = replace(
        _syntax_record(),
        observed_substrates=("exact_exit_status",),
    )

    evaluation = _evaluate_syntax(
        target,
        available=("exact_exit_status",),
    )

    assert evaluation.release_allowed is True
    assert evaluation.reason is rr.EvidenceTransitionReason.DECISION_WINDOW_OPEN


def test_record_observation_cannot_override_missing_context_availability() -> None:
    target = replace(
        _syntax_record(),
        observed_substrates=("parser_result",),
    )

    evaluation = _evaluate_syntax(target, available=())

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.PREREQUISITES_PENDING
    )


def test_historical_empty_observed_substrates_stays_correct_or_quiet() -> None:
    historical = replace(
        _syntax_record(),
        observed_substrates=(),
    )

    evaluation = _evaluate_syntax(
        historical,
        available=("compiler_result", "parser_result"),
    )

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.PREREQUISITES_PENDING
    )


def _revision(
    repository: str,
    *,
    graph: str,
) -> rr.RevisionVector:
    return rr.RevisionVector(
        repository_content=repository,
        graph=graph,
        lsp="lsp-current",
        runtime_evidence="runtime-current",
    )


def test_old_graph_record_cannot_authorize_new_graphless_record() -> None:
    old_revision = _revision("repo-old", graph="graph-old")
    current_revision = _revision("repo-new", graph="graph-current")
    old = replace(
        _caller_evidence(evidence_id="GT-E-old-graph"),
        revision=old_revision,
        observed_substrates=("graph",),
    )
    target = replace(
        _caller_evidence(evidence_id="GT-E-current-no-graph"),
        revision=current_revision,
        observed_substrates=(),
    )
    attempt_wide = seam.CanonicalRuntimeAttachment._available_substrates(
        (old, target)
    )
    assert attempt_wide == ("graph",)

    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    evaluation = rr.evaluate_feature_contract(
        contract,
        target,
        rr.TemporalRuntimeContext(
            active_decision=replace(
                _caller_decision(),
                current_revision=current_revision,
            ),
            current_revision=current_revision,
            commitment_window=rr.CommitmentWindowState.OPEN,
            satisfied_predicates=frozenset(contract.ready_predicates),
            available_substrates=attempt_wide,
        ),
    )

    assert evaluation.release_allowed is False
    assert evaluation.next_lifecycle is rr.EvidenceLifecycle.HELD
    assert (
        evaluation.reason
        is rr.EvidenceTransitionReason.PREREQUISITES_PENDING
    )

