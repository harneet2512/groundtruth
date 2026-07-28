"""RED contracts for canonical D2 acquired-target suppression.

The legacy arbiter projects native reads into ``EpisodeState.read_targets`` before
selecting localization evidence.  The canonical runtime already records the same
truth in ``WorkState.viewed_files`` but does not pass it to the pure coalition
selector.  These tests require that projection without conflating native
acquisition with provider/model visibility.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
        rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
    }
)


def _record(
    feature_id: str,
    subject: str,
    *,
    evidence_id: str | None = None,
    lifecycle: rr.EvidenceLifecycle = rr.EvidenceLifecycle.READY,
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for(feature_id)
    assert contract is not None
    observed = tuple(
        sorted(
            set(contract.fallback_policy.preferred_substrates)
            or set(contract.fallback_policy.fallback_substrates)
        )
    )
    return rr.EvidenceRecord(
        evidence_id=evidence_id or f"GT-E-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject=subject,
        claim=f"{feature_id} claim for {subject}",
        actionable_consequence=f"apply {feature_id} before changing {subject}",
        provenance=(f"{subject}:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"subject:{subject}",
        ),
        lifecycle=lifecycle,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        observed_substrates=observed,
    )


def _decision(record: rr.EvidenceRecord) -> rr.ActiveDecision:
    return rr.ActiveDecision(
        decision_id=f"decision:{record.evidence_id}",
        context=record.decision_context,
        primary_claim=f"decide using {record.feature_id}",
        required_roles=record.roles,
        causal_neighborhood=(f"subject:{record.subject}",),
        token_budget=180,
        current_revision=REVISION,
    )


def _suppression(
    oracle: rr.OracleDecision,
    evidence_id: str,
) -> rr.SuppressionRecord:
    return next(
        item for item in oracle.suppressed
        if item.evidence_id == evidence_id
    )


def _view_event(
    *,
    attempt_id: str,
    subject: str,
) -> rr.CanonicalEvent:
    return rr.CanonicalEvent(
        event_id=f"{attempt_id}:view",
        attempt_id=attempt_id,
        sequence=1,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=rr.Authority.STRUCTURED,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.SOURCE_VIEWED,
                subject=subject,
                authority=rr.Authority.STRUCTURED,
                provenance=("viewed_files",),
            ),
        ),
        revision_before=REVISION,
        revision_after=REVISION,
        previous_event_hash="",
        observation_id=f"{attempt_id}:observation:1",
        carrier="view",
    )


def _edit_event(
    *,
    attempt_id: str,
    subject: str,
) -> rr.CanonicalEvent:
    return rr.CanonicalEvent(
        event_id=f"{attempt_id}:edit",
        attempt_id=attempt_id,
        sequence=1,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=rr.Authority.REPOSITORY_DELTA,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.EDIT_EXECUTED,
                subject=subject,
                changed=True,
                authority=rr.Authority.REPOSITORY_DELTA,
                provenance=("changed_files",),
            ),
        ),
        revision_before=REVISION,
        revision_after=REVISION,
        previous_event_hash="",
        observation_id=f"{attempt_id}:observation:1",
        carrier="edit",
    )


def _runtime_after_view(
    tmp_path: Path,
    *,
    attempt_id: str,
    viewed_subject: str,
) -> rr.AttemptReasoningRuntime:
    journal = rr.RuntimeJournal(tmp_path / f"{attempt_id}.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id=attempt_id,
        journal=journal,
        initial_revision=REVISION,
    )
    runtime.append_event(
        _view_event(attempt_id=attempt_id, subject=viewed_subject)
    )
    assert runtime.work_state.viewed_files == (viewed_subject,)
    return runtime


def _runtime_after_edit(
    tmp_path: Path,
    *,
    attempt_id: str,
    edited_subject: str,
) -> rr.AttemptReasoningRuntime:
    journal = rr.RuntimeJournal(tmp_path / f"{attempt_id}.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id=attempt_id,
        journal=journal,
        initial_revision=REVISION,
    )
    runtime.append_event(
        _edit_event(attempt_id=attempt_id, subject=edited_subject)
    )
    assert runtime.work_state.edited_files == (edited_subject,)
    return runtime


def _prepare(
    runtime: rr.AttemptReasoningRuntime,
    record: rr.EvidenceRecord,
) -> rr.InferencePlan:
    runtime.ingest_evidence(
        replace(record, lifecycle=rr.EvidenceLifecycle.PENDING)
    )
    return runtime.prepare_next_inference(
        decisions=(_decision(record),),
        satisfied_predicates=SATISFIED,
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=(
            seam.CanonicalRuntimeAttachment._available_substrates((record,))
        ),
        native_observation="native file-view observation",
        observation_id="observation:next",
        source_model_call_id="model:source",
        model_call_id=f"model:{record.evidence_id}",
    )


def test_pure_selector_api_names_native_acquisition_separately() -> None:
    signature = inspect.signature(rr.select_evidence_coalition)

    assert "acquired_subjects" in signature.parameters
    assert signature.parameters["acquired_subjects"].default == ()
    assert rr.SuppressionReason.ALREADY_ACQUIRED.value == "ALREADY_ACQUIRED"


def test_pure_selector_suppresses_an_exact_acquired_target() -> None:
    record = _record("localization", "src/a.py")

    oracle = rr.select_evidence_coalition(
        _decision(record),
        (record,),
        acquired_subjects=("src/a.py",),
    )

    assert oracle.coalition == ()
    assert oracle.release_allowed is False
    assert (
        _suppression(oracle, record.evidence_id).reason
        is rr.SuppressionReason.ALREADY_ACQUIRED
    )


def test_runtime_projects_source_view_into_selector_and_stages_no_capsule(
    tmp_path: Path,
) -> None:
    viewed_runtime = _runtime_after_view(
        tmp_path,
        attempt_id="attempt-acquired",
        viewed_subject="src/a.py",
    )
    record = _record("localization", "src/a.py")
    try:
        viewed_plan = _prepare(viewed_runtime, record)
    finally:
        viewed_runtime.journal.close()

    edited_runtime = _runtime_after_edit(
        tmp_path,
        attempt_id="attempt-edited",
        edited_subject="src/a.py",
    )
    try:
        edited_plan = _prepare(edited_runtime, record)
    finally:
        edited_runtime.journal.close()

    for plan in (viewed_plan, edited_plan):
        assert plan.oracle_decision.coalition == ()
        assert plan.delivery_attempt_id == ""
        assert plan.compilation.state is not rr.CapsuleCompilationState.COMPILED
        assert (
            _suppression(plan.oracle_decision, record.evidence_id).reason
            is rr.SuppressionReason.ALREADY_ACQUIRED
        )


def test_unopened_target_remains_eligible_and_compiles(tmp_path: Path) -> None:
    runtime = _runtime_after_view(
        tmp_path,
        attempt_id="attempt-unopened",
        viewed_subject="src/a.py",
    )
    record = _record("localization", "src/b.py")
    try:
        plan = _prepare(runtime, record)
    finally:
        runtime.journal.close()

    assert tuple(
        item.evidence_id for item in plan.oracle_decision.coalition
    ) == (record.evidence_id,)
    assert plan.delivery_attempt_id
    assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED


@pytest.mark.parametrize("feature_id", ("caller_contract", "syntax_result"))
def test_same_file_non_target_evidence_is_not_over_suppressed(
    tmp_path: Path,
    feature_id: str,
) -> None:
    runtime = _runtime_after_view(
        tmp_path,
        attempt_id=f"attempt-nontarget-{feature_id}",
        viewed_subject="src/a.py",
    )
    record = _record(feature_id, "src/a.py")
    try:
        plan = _prepare(runtime, record)
    finally:
        runtime.journal.close()

    assert tuple(
        item.evidence_id for item in plan.oracle_decision.coalition
    ) == (record.evidence_id,)
    assert plan.delivery_attempt_id
    assert plan.compilation.state is rr.CapsuleCompilationState.COMPILED


@pytest.mark.parametrize(
    ("viewed_subject", "record_subject"),
    (
        (r"src\a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("src/a.py", r".\src\a.py"),
    ),
)
def test_acquired_target_matching_normalizes_repo_relative_separators(
    tmp_path: Path,
    viewed_subject: str,
    record_subject: str,
) -> None:
    runtime = _runtime_after_view(
        tmp_path,
        attempt_id="attempt-normalized",
        viewed_subject=viewed_subject,
    )
    record = _record("localization", record_subject)
    try:
        plan = _prepare(runtime, record)
    finally:
        runtime.journal.close()

    assert plan.delivery_attempt_id == ""
    assert (
        _suppression(plan.oracle_decision, record.evidence_id).reason
        is rr.SuppressionReason.ALREADY_ACQUIRED
    )
