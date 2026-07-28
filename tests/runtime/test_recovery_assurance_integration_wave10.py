"""RED contracts for integrity-proven bounded recovery.

Recovery is allowed exactly once per core-fault signature.  It must be derived
from the append-only journal itself, not from caller-supplied booleans, and the
native agent path must remain enabled even when GT is quarantined.
"""

from __future__ import annotations

from dataclasses import replace

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent
from groundtruth.runtime.recovery_assurance import (
    handle_runtime_fault,
    verify_runtime_integrity,
)


REV = rr.RevisionVector(
    repository_content="repo-wave10",
    graph="graph-wave10",
    lsp="lsp-wave10",
    runtime_evidence="runtime-wave10",
)


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "recovery-wave10.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-recovery-wave10",
        journal=journal,
        initial_revision=REV,
    )
    return runtime, journal


def _append_search(runtime: rr.AttemptReasoningRuntime) -> None:
    action = rr.CanonicalAction(
        action_id="search-action",
        operation=rr.ActionOperation.SEARCH,
        tool_family="search",
        tool_name="mini-swe",
        structured_operation="search",
        subject="refreshSession",
        query="refreshSession",
        targets=("src",),
        raw_command="structured search",
    )
    proposed = miniswe.canonicalize_action_proposal(
        action,
        event_id="ev-search-proposed",
        attempt_id=runtime.attempt_id,
        sequence=1,
        model_turn_id="call-1",
        observation_id="obs-0",
        revision=REV,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="other",
            carrier_kind="other",
            command="audit carrier",
            output="src/auth/session.py:41:def refreshSession(token):",
            exit_status=0,
            semantic_events=(),
            semantics_authoritative=True,
        ),
        proposal=proposed,
        result=rr.CanonicalResult(
            status="success",
            exit_code=0,
            hit_count=1,
            files_hit=("src/auth/session.py",),
        ),
        event_id="ev-search-result",
        sequence=2,
        observation_id="obs-1",
        revision_after=REV,
        previous_event_hash=proposed.content_hash,
    )
    runtime.append_event(proposed)
    runtime.append_event(result)


def _evidence() -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("localization")
    assert contract is not None
    return rr.EvidenceRecord(
        evidence_id="GT-E-recovery-integrity",
        feature_id="localization",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim="refreshSession is the active production definition",
        actionable_consequence="inspect this definition before editing",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REV,
        causal_neighborhood=(
            "decision:source-target",
            "symbol:refreshSession",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=20,
        failure_prevention=4,
        causal_value=4,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.STRUCTURED,
        # REQUIRED for release: the temporal gate authorizes a record only on substrates THAT
        # RECORD observed, so a fixture declaring none is held at PREREQUISITES_PENDING, the
        # coalition never forms, and `plan.delivery_attempt_id` is EMPTY. Taken from the
        # contract's preferred substrates, matching what every production producer declares.
        observed_substrates=tuple(
            sorted(contract.fallback_policy.preferred_substrates)
        ),
    )


def _core_fault(signature: str = "state-mismatch@ev-search-result"):
    return rr.RuntimeFault(
        code=rr.FaultCode.STATE_HASH_MISMATCH,
        component="canonical_runtime",
        signature=signature,
        event_id="ev-search-result",
    )


def test_integrity_attestation_replays_every_projection_twice(tmp_path) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        _append_search(runtime)
        runtime.ingest_evidence(_evidence())

        attestation = verify_runtime_integrity(runtime)

        assert attestation.replay_count == 2
        assert attestation.snapshot_id == (
            f"{runtime.attempt_id}:{runtime.work_state.sequence}"
        )
        assert attestation.work_state_hash == runtime.work_state.state_hash
        assert attestation.reasoning_graph_hash == (
            runtime.reasoning_graph.graph_hash
        )
        assert len(attestation.evidence_state_hash) == 64
        assert len(attestation.compilation_delivery_hash) == 64
        assert len(attestation.aggregate_hash) == 64
    finally:
        journal.close()


def test_compilation_delivery_hash_tracks_the_persisted_delivery_projection(
    tmp_path,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        _append_search(runtime)
        evidence = _evidence()
        runtime.ingest_evidence(evidence)
        before = verify_runtime_integrity(runtime)
        contract = rr.feature_contract_for(evidence.feature_id)
        assert contract is not None
        decision = rr.ActiveDecision(
            decision_id="source-target",
            context=contract.decision_context,
            primary_claim="select the active refreshSession definition",
            required_roles=contract.roles,
            causal_neighborhood=evidence.causal_neighborhood,
            token_budget=180,
            current_revision=REV,
        )

        plan = runtime.prepare_next_inference(
            decisions=(decision,),
            satisfied_predicates=frozenset(rr.TemporalPredicate),
            commitment_window=rr.CommitmentWindowState.OPEN,
            available_substrates=(
                *contract.fallback_policy.preferred_substrates,
                *contract.fallback_policy.fallback_substrates,
            ),
            native_observation="$ rg refreshSession src\n",
            observation_id="obs-delivery",
            source_model_call_id="call-source",
            model_call_id="call-delivery",
        )
        assert plan.delivery_attempt_id

        after = verify_runtime_integrity(runtime)
        assert (
            after.compilation_delivery_hash
            != before.compilation_delivery_hash
        )
        assert after.replay_count == 2
    finally:
        journal.close()


def test_core_recovery_uses_journal_attestation_then_same_signature_quarantines(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        _append_search(runtime)
        fault = _core_fault()

        recovered = handle_runtime_fault(runtime, fault)

        assert recovered.health is rr.RuntimeHealthState.RECOVERED
        assert recovered.assurance is rr.AssuranceStatus.ASSURED
        assert recovered.recovery_attempted_signatures == (fault.signature,)
        assert recovered.native_path_enabled is True

        def must_not_read_events(*_args, **_kwargs):
            raise AssertionError("same signature must not attempt recovery twice")

        monkeypatch.setattr(journal, "events", must_not_read_events)
        quarantined = handle_runtime_fault(runtime, fault)
        assert quarantined.health is rr.RuntimeHealthState.QUARANTINED
        assert quarantined.assurance is rr.AssuranceStatus.UNASSURED
        assert quarantined.gt_emission_enabled is False
        assert quarantined.gt_interruption_enabled is False
        assert quarantined.gt_certification_enabled is False
        assert quarantined.native_path_enabled is True
    finally:
        journal.close()


def test_recovery_input_failure_quarantines_without_escaping(tmp_path, monkeypatch):
    runtime, journal = _runtime(tmp_path)
    try:
        fault = _core_fault("snapshot-load@attempt")

        def corrupt_recovery_input():
            raise rr.StateIntegrityError("snapshot is unreadable")

        monkeypatch.setattr(runtime, "recovery_input", corrupt_recovery_input)
        state = handle_runtime_fault(runtime, fault)

        assert state.health is rr.RuntimeHealthState.QUARANTINED
        assert state.recovery_attempted_signatures == (fault.signature,)
        assert state.native_path_enabled is True
        assert state.failed_event_id == fault.event_id
    finally:
        journal.close()


def test_second_independent_replay_mismatch_quarantines(tmp_path, monkeypatch):
    runtime, journal = _runtime(tmp_path)
    try:
        _append_search(runtime)
        evidence = _evidence()
        runtime.ingest_evidence(evidence)
        original = journal.evidence_records_for_attempt
        reads = 0

        def divergent_evidence(attempt_id):
            nonlocal reads
            reads += 1
            records = original(attempt_id)
            if reads == 2:
                return (
                    replace(
                        records[0],
                        claim="a divergent second replay projection",
                    ),
                )
            return records

        monkeypatch.setattr(
            journal,
            "evidence_records_for_attempt",
            divergent_evidence,
        )
        state = handle_runtime_fault(
            runtime,
            _core_fault("nondeterministic-evidence@attempt"),
        )

        assert reads == 2
        assert state.health is rr.RuntimeHealthState.QUARANTINED
        assert state.assurance is rr.AssuranceStatus.UNASSURED
        assert state.native_path_enabled is True
    finally:
        journal.close()


def test_component_fault_isolated_without_reading_corrupt_core(tmp_path, monkeypatch):
    runtime, journal = _runtime(tmp_path)
    try:
        def must_not_read_core():
            raise AssertionError("component isolation must not replay core state")

        monkeypatch.setattr(runtime, "recovery_input", must_not_read_core)
        state = handle_runtime_fault(
            runtime,
            rr.RuntimeFault(
                code=rr.FaultCode.EVIDENCE_PRODUCER_FAILED,
                component="caller_contract",
                signature="caller-contract@producer",
            ),
        )

        assert state.health is rr.RuntimeHealthState.DEGRADED
        assert state.isolated_components == ("caller_contract",)
        assert state.recovery_attempted_signatures == ()
        assert state.native_path_enabled is True
    finally:
        journal.close()
