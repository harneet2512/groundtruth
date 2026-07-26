"""RED contract for centralized GT failure assurance and core quarantine.

Ordinary component faults are isolated.  Only canonical-state integrity faults
may trigger attempt-wide recovery/quarantine, and a fault signature gets one
integrity-checked replay attempt from the latest verified snapshot plus its
committed event tail.
"""
from __future__ import annotations

from groundtruth.runtime.reasoning_runtime import (
    AssuranceStatus,
    DeliveryAttempt,
    DeliveryState,
    FailurePolicyState,
    FaultCode,
    RecoveryInput,
    RecoveryProof,
    RuntimeFault,
    RuntimeHealthState,
    apply_failure_policy,
    is_delivered,
)


def _core_fault(signature: str = "gap@ev-204") -> RuntimeFault:
    return RuntimeFault(
        code=FaultCode.CAUSAL_EVENT_GAP,
        component="canonical_event_fabric",
        signature=signature,
        event_id="ev-204",
    )


def _recovery_input() -> RecoveryInput:
    return RecoveryInput(
        snapshot_id="snap-188",
        snapshot_state_hash="a" * 64,
        committed_event_ids=("ev-189", "ev-190", "ev-204"),
        committed_tail_hash="b" * 64,
    )


def _valid_proof(request: RecoveryInput) -> RecoveryProof:
    return RecoveryProof(
        snapshot_id=request.snapshot_id,
        snapshot_state_hash=request.snapshot_state_hash,
        committed_event_ids=request.committed_event_ids,
        committed_tail_hash=request.committed_tail_hash,
        snapshot_hash_valid=True,
        event_sequence_complete=True,
        deterministic_replay=True,
        state_hash_matches=True,
        reasoning_graph_hash_matches=True,
        evidence_graph_hash_matches=True,
        repository_revision_consistent=True,
        invariants_pass=True,
        recovered_state_hash="c" * 64,
    )


def test_producer_fault_is_component_isolated_without_core_recovery() -> None:
    calls: list[RecoveryInput] = []
    initial = FailurePolicyState.initial(attempt_id="attempt-1")

    disposition = apply_failure_policy(
        initial,
        RuntimeFault(
            code=FaultCode.EVIDENCE_PRODUCER_FAILED,
            component="caller_contract",
            signature="caller-contract@42",
            event_id="ev-42",
        ),
        recovery_input=_recovery_input(),
        recover=lambda request: calls.append(request) or _valid_proof(request),
    )

    assert calls == []
    assert disposition.health is RuntimeHealthState.DEGRADED
    assert disposition.assurance is AssuranceStatus.DEGRADED
    assert disposition.isolated_components == ("caller_contract",)
    assert disposition.recovery_attempted_signatures == ()
    assert disposition.gt_emission_enabled
    assert disposition.gt_interruption_enabled
    assert not disposition.gt_certification_enabled
    assert disposition.native_path_enabled


def test_core_fault_recovers_once_from_exact_verified_snapshot_and_tail() -> None:
    seen: list[RecoveryInput] = []
    recovery_input = _recovery_input()

    disposition = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        _core_fault(),
        recovery_input=recovery_input,
        recover=lambda request: seen.append(request) or _valid_proof(request),
    )

    assert seen == [recovery_input]
    assert disposition.health is RuntimeHealthState.RECOVERED
    assert disposition.assurance is AssuranceStatus.ASSURED
    assert disposition.recovery_attempted_signatures == ("gap@ev-204",)
    assert disposition.last_verified_snapshot_id == "snap-188"
    assert disposition.gt_emission_enabled
    assert disposition.gt_interruption_enabled
    assert disposition.gt_certification_enabled


def test_failed_integrity_proof_quarantines_attempt_and_keeps_native_path() -> None:
    invalid_proof = RecoveryProof(
        snapshot_id="snap-188",
        snapshot_state_hash="a" * 64,
        committed_event_ids=("ev-189", "ev-190", "ev-204"),
        committed_tail_hash="b" * 64,
        snapshot_hash_valid=True,
        event_sequence_complete=True,
        deterministic_replay=False,
        state_hash_matches=False,
        reasoning_graph_hash_matches=True,
        evidence_graph_hash_matches=True,
        repository_revision_consistent=True,
        invariants_pass=False,
        recovered_state_hash="",
    )

    disposition = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        _core_fault(),
        recovery_input=_recovery_input(),
        recover=lambda _request: invalid_proof,
    )

    assert disposition.health is RuntimeHealthState.QUARANTINED
    assert disposition.assurance is AssuranceStatus.UNASSURED
    assert not disposition.gt_emission_enabled
    assert not disposition.gt_interruption_enabled
    assert not disposition.gt_certification_enabled
    assert disposition.native_path_enabled


def test_recovery_proof_must_bind_exact_snapshot_and_committed_tail() -> None:
    request = _recovery_input()
    unrelated = RecoveryProof(
        snapshot_id=request.snapshot_id,
        snapshot_state_hash="f" * 64,
        committed_event_ids=("ev-other",),
        committed_tail_hash="e" * 64,
        snapshot_hash_valid=True,
        event_sequence_complete=True,
        deterministic_replay=True,
        state_hash_matches=True,
        reasoning_graph_hash_matches=True,
        evidence_graph_hash_matches=True,
        repository_revision_consistent=True,
        invariants_pass=True,
        recovered_state_hash="c" * 64,
    )

    disposition = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        _core_fault(),
        recovery_input=request,
        recover=lambda _request: unrelated,
    )

    assert disposition.health is RuntimeHealthState.QUARANTINED
    assert disposition.assurance is AssuranceStatus.UNASSURED
    assert disposition.quarantine_reason is FaultCode.CAUSAL_EVENT_GAP
    assert disposition.failed_event_id == "ev-204"
    assert disposition.recovery_attempted_signatures == ("gap@ev-204",)


def test_repeated_core_fault_signature_quarantines_without_second_replay() -> None:
    calls: list[RecoveryInput] = []

    recovered = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        _core_fault(),
        recovery_input=_recovery_input(),
        recover=lambda request: calls.append(request) or _valid_proof(request),
    )
    repeated = apply_failure_policy(
        recovered,
        _core_fault(),
        recovery_input=_recovery_input(),
        recover=lambda request: calls.append(request) or _valid_proof(request),
    )

    assert len(calls) == 1
    assert repeated.health is RuntimeHealthState.QUARANTINED
    assert repeated.assurance is AssuranceStatus.UNASSURED
    assert repeated.recovery_attempted_signatures == ("gap@ev-204",)
    assert repeated.native_path_enabled


def test_core_recovery_does_not_erase_an_existing_component_degradation() -> None:
    initial = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        RuntimeFault(
            code=FaultCode.EVIDENCE_PRODUCER_FAILED,
            component="caller_contract",
            signature="caller-contract@42",
            event_id="ev-42",
        ),
        recovery_input=_recovery_input(),
        recover=lambda request: _valid_proof(request),
    )
    recovered = apply_failure_policy(
        initial,
        _core_fault(),
        recovery_input=_recovery_input(),
        recover=_valid_proof,
    )

    assert recovered.health is RuntimeHealthState.DEGRADED
    assert recovered.assurance is AssuranceStatus.DEGRADED
    assert recovered.isolated_components == ("caller_contract",)
    assert not recovered.gt_certification_enabled


def test_every_declared_core_corruption_code_uses_attempt_recovery() -> None:
    expected_core_codes = {
        FaultCode.CAUSAL_EVENT_GAP,
        FaultCode.DUPLICATE_TERMINAL_OUTCOME,
        FaultCode.SNAPSHOT_HASH_MISMATCH,
        FaultCode.NONDETERMINISTIC_REPLAY,
        FaultCode.REDUCER_INVARIANT_VIOLATION,
        FaultCode.IMPOSSIBLE_LIFECYCLE_TRANSITION,
        FaultCode.REPOSITORY_REVISION_INCONSISTENCY,
        FaultCode.STATE_HASH_MISMATCH,
        FaultCode.UNKNOWN_PARTIAL_COMMIT,
    }

    for code in expected_core_codes:
        calls: list[RecoveryInput] = []
        disposition = apply_failure_policy(
            FailurePolicyState.initial(attempt_id=f"attempt-{code.value}"),
            RuntimeFault(
                code=code,
                component="canonical_runtime",
                signature=f"{code.value}@ev-9",
                event_id="ev-9",
            ),
            recovery_input=_recovery_input(),
            recover=lambda request: calls.append(request) or _valid_proof(request),
        )
        assert len(calls) == 1, code
        assert disposition.health is RuntimeHealthState.RECOVERED, code


def test_observation_join_fault_never_upgrades_delivery_or_quarantines_core() -> None:
    delivery = DeliveryAttempt(
        evidence_ids=("GT-E144",),
        capsule_hash="d" * 64,
        model_call_id="call-12a",
    )

    disposition = apply_failure_policy(
        FailurePolicyState.initial(attempt_id="attempt-1"),
        RuntimeFault(
            code=FaultCode.OBSERVATION_JOIN_FAILED,
            component="observation_compiler",
            signature="join@call-12a",
            event_id="ev-205",
        ),
        recovery_input=_recovery_input(),
        recover=lambda request: _valid_proof(request),
    )

    assert delivery.state is DeliveryState.SELECTED
    assert not is_delivered(delivery)
    assert disposition.health is RuntimeHealthState.DEGRADED
    assert disposition.assurance is AssuranceStatus.DEGRADED
    assert disposition.isolated_components == ("observation_compiler",)
    assert disposition.recovery_attempted_signatures == ()
    assert disposition.native_path_enabled
