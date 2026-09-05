"""Integrity-proven bounded recovery for the canonical reasoning runtime.

This module is deliberately separate from feature production and model-facing
delivery.  It reads only append-only runtime truth, reconstructs every
canonical projection twice, and resumes GT only when both reconstructions have
identical explicit hashes.  Callers do not supply recovery booleans.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from . import reasoning_runtime as rr


_QUARANTINE_MARKER_SCHEMA = "gt.attempt_health.quarantine.v1"
_FAILURE_STATE_MARKER_KEYS = frozenset(
    {
        "assurance",
        "attempt_id",
        "failed_event_id",
        "gt_certification_enabled",
        "gt_emission_enabled",
        "gt_interruption_enabled",
        "health",
        "isolated_components",
        "last_verified_snapshot_id",
        "native_path_enabled",
        "quarantine_reason",
        "recovery_attempted_signatures",
    }
)


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical_value(item) for item in value),
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _quarantine_marker_path(
    runtime: rr.AttemptReasoningRuntime,
) -> Path:
    attempt_hash = hashlib.sha256(runtime.attempt_id.encode("utf-8", "surrogatepass")).hexdigest()[
        :16
    ]
    journal_path = Path(runtime.journal.path)
    return journal_path.with_name(f"{journal_path.name}.gt_attempt_health_{attempt_hash}.json")


def _failure_state_payload(
    state: rr.FailurePolicyState,
) -> dict[str, Any]:
    return {
        "assurance": state.assurance.value,
        "attempt_id": state.attempt_id,
        "failed_event_id": state.failed_event_id,
        "gt_certification_enabled": state.gt_certification_enabled,
        "gt_emission_enabled": state.gt_emission_enabled,
        "gt_interruption_enabled": state.gt_interruption_enabled,
        "health": state.health.value,
        "isolated_components": list(state.isolated_components),
        "last_verified_snapshot_id": state.last_verified_snapshot_id,
        "native_path_enabled": state.native_path_enabled,
        "quarantine_reason": (
            state.quarantine_reason.value if state.quarantine_reason is not None else None
        ),
        "recovery_attempted_signatures": list(state.recovery_attempted_signatures),
    }


def _sealed_marker_bytes(state: rr.FailurePolicyState) -> bytes:
    unsigned = {
        "schema": _QUARANTINE_MARKER_SCHEMA,
        "state": _failure_state_payload(state),
    }
    sealed = {
        **unsigned,
        "record_sha256": _hash(unsigned),
    }
    return (_canonical_json(sealed) + "\n").encode("utf-8")


def _atomic_write_marker(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _persist_quarantine_marker(
    runtime: rr.AttemptReasoningRuntime,
    state: rr.FailurePolicyState,
) -> None:
    if (
        state.health is not rr.RuntimeHealthState.QUARANTINED
        or state.assurance is not rr.AssuranceStatus.UNASSURED
        or state.gt_emission_enabled
        or state.gt_interruption_enabled
        or state.gt_certification_enabled
        or not state.native_path_enabled
        or state.quarantine_reason is None
    ):
        raise rr.StateIntegrityError("attempt-health marker accepts only terminal quarantine state")
    path = _quarantine_marker_path(runtime)
    payload = _sealed_marker_bytes(state)
    if path.exists():
        try:
            if path.read_bytes() == payload:
                return
        except OSError:
            pass
    _atomic_write_marker(path, payload)


def _state_from_marker(
    runtime: rr.AttemptReasoningRuntime,
    payload: bytes,
) -> rr.FailurePolicyState:
    try:
        text = payload.decode("utf-8")
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("marker root must be an object")
        seal = raw.get("record_sha256")
        unsigned = {
            "schema": raw.get("schema"),
            "state": raw.get("state"),
        }
        if (
            raw.get("schema") != _QUARANTINE_MARKER_SCHEMA
            or not isinstance(seal, str)
            or seal != _hash(unsigned)
            or payload
            != (_canonical_json({**unsigned, "record_sha256": seal}) + "\n").encode("utf-8")
            or not isinstance(raw.get("state"), dict)
        ):
            raise ValueError("attempt-health marker seal invalid")
        state_raw = raw["state"]
        bool_fields = (
            "gt_certification_enabled",
            "gt_emission_enabled",
            "gt_interruption_enabled",
            "native_path_enabled",
        )
        list_fields = (
            "isolated_components",
            "recovery_attempted_signatures",
        )
        if (
            set(state_raw) != _FAILURE_STATE_MARKER_KEYS
            or any(type(state_raw.get(field_name)) is not bool for field_name in bool_fields)
            or any(
                not isinstance(state_raw.get(field_name), list)
                or any(not isinstance(item, str) for item in state_raw[field_name])
                or len(state_raw[field_name]) != len(set(state_raw[field_name]))
                for field_name in list_fields
            )
        ):
            raise ValueError("attempt-health marker state shape invalid")
        reason_raw = state_raw.get("quarantine_reason")
        state = rr.FailurePolicyState(
            attempt_id=str(state_raw["attempt_id"]),
            health=rr.RuntimeHealthState(state_raw["health"]),
            assurance=rr.AssuranceStatus(state_raw["assurance"]),
            isolated_components=tuple(
                str(item) for item in state_raw.get("isolated_components", ())
            ),
            recovery_attempted_signatures=tuple(
                str(item)
                for item in state_raw.get(
                    "recovery_attempted_signatures",
                    (),
                )
            ),
            last_verified_snapshot_id=str(state_raw.get("last_verified_snapshot_id", "")),
            gt_emission_enabled=state_raw["gt_emission_enabled"],
            gt_interruption_enabled=state_raw["gt_interruption_enabled"],
            gt_certification_enabled=state_raw["gt_certification_enabled"],
            native_path_enabled=state_raw["native_path_enabled"],
            quarantine_reason=(rr.FaultCode(reason_raw) if reason_raw is not None else None),
            failed_event_id=str(state_raw.get("failed_event_id", "")),
        )
        if (
            state.attempt_id != runtime.attempt_id
            or state.health is not rr.RuntimeHealthState.QUARANTINED
            or state.assurance is not rr.AssuranceStatus.UNASSURED
            or state.gt_emission_enabled
            or state.gt_interruption_enabled
            or state.gt_certification_enabled
            or not state.native_path_enabled
            or state.quarantine_reason is None
        ):
            raise ValueError("attempt-health marker state invalid")
        return state
    except (KeyError, TypeError, UnicodeError, ValueError):
        current = runtime.failure_state
        return replace(
            current,
            health=rr.RuntimeHealthState.QUARANTINED,
            assurance=rr.AssuranceStatus.UNASSURED,
            gt_emission_enabled=False,
            gt_interruption_enabled=False,
            gt_certification_enabled=False,
            native_path_enabled=True,
            quarantine_reason=rr.FaultCode.STATE_HASH_MISMATCH,
            failed_event_id=current.failed_event_id,
        )


def restore_persisted_quarantine(
    runtime: rr.AttemptReasoningRuntime,
) -> rr.FailurePolicyState:
    """Make a sealed terminal marker dominate journal-derived health once."""

    path = _quarantine_marker_path(runtime)
    if not path.exists():
        return runtime.failure_state
    try:
        marker_state = _state_from_marker(runtime, path.read_bytes())
    except OSError:
        marker_state = replace(
            runtime.failure_state,
            health=rr.RuntimeHealthState.QUARANTINED,
            assurance=rr.AssuranceStatus.UNASSURED,
            gt_emission_enabled=False,
            gt_interruption_enabled=False,
            gt_certification_enabled=False,
            native_path_enabled=True,
            quarantine_reason=rr.FaultCode.STATE_HASH_MISMATCH,
        )
    runtime.failure_state = marker_state
    try:
        runtime.journal.append_failure_state(marker_state)
    except Exception:
        pass
    return marker_state


@dataclass(frozen=True)
class _RuntimeProjection:
    work_state: rr.WorkState
    reasoning_graph: rr.ReasoningGraph
    evidence: tuple[tuple[str, rr.EvidenceRecord], ...]
    compilations: tuple[tuple[str, rr.CapsuleCompilation], ...]
    delivery_attempt_ids: tuple[tuple[str, str], ...]
    work_state_hash: str
    reasoning_graph_hash: str
    evidence_state_hash: str
    compilation_delivery_hash: str


@dataclass(frozen=True)
class RuntimeIntegrityAttestation:
    """Explicit hashes produced by two independent canonical replays."""

    attempt_id: str
    snapshot_id: str
    committed_event_ids: tuple[str, ...]
    committed_tail_hash: str
    work_state_hash: str
    reasoning_graph_hash: str
    evidence_state_hash: str
    compilation_delivery_hash: str
    aggregate_hash: str
    replay_count: int = 2


def _validate_request_against_journal(
    runtime: rr.AttemptReasoningRuntime,
    request: rr.RecoveryInput,
    events: tuple[rr.CanonicalEvent, ...],
) -> tuple[rr.WorkState, tuple[rr.CanonicalEvent, ...]]:
    """Bind recovery to the exact verified snapshot and committed event tail."""

    if events:
        snapshot, tail = runtime.journal.load_snapshot_and_tail(runtime.attempt_id)
        snapshot_id = f"{runtime.attempt_id}:{snapshot.sequence}"
        tail_ids = tuple(event.event_id for event in tail)
        tail_hash = events[-1].content_hash
    else:
        snapshot = rr.WorkState.initial(
            attempt_id=runtime.attempt_id,
            revision=runtime._initial_revision,
        )
        tail = ()
        snapshot_id = f"{runtime.attempt_id}:0"
        tail_ids = ()
        tail_hash = hashlib.sha256(b"").hexdigest()

    if (
        request.snapshot_id != snapshot_id
        or request.snapshot_state_hash != snapshot.state_hash
        or request.committed_event_ids != tail_ids
        or request.committed_tail_hash != tail_hash
    ):
        raise rr.StateIntegrityError(
            "recovery request does not bind the verified snapshot and tail"
        )
    return snapshot, tuple(tail)


def _replay_once(
    runtime: rr.AttemptReasoningRuntime,
    request: rr.RecoveryInput,
) -> _RuntimeProjection:
    """Reconstruct all projections using fresh journal reads."""

    events = runtime.journal.events(runtime.attempt_id)
    if events and events[0].revision_before != runtime._initial_revision:
        raise rr.StateIntegrityError(
            "attempt initial revision conflicts with canonical event truth"
        )
    snapshot, tail = _validate_request_against_journal(
        runtime,
        request,
        events,
    )

    work_state = rr.WorkState.initial(
        attempt_id=runtime.attempt_id,
        revision=runtime._initial_revision,
    )
    reasoning_graph = rr.ReasoningGraph.initial(
        attempt_id=runtime.attempt_id,
        revision=runtime._initial_revision,
    )
    for event in events:
        work_state = rr.reduce_event(work_state, event)
        reasoning_graph = rr.reduce_reasoning_event(
            reasoning_graph,
            event=event,
        )

    replayed_snapshot = snapshot
    for event in tail:
        replayed_snapshot = rr.reduce_event(replayed_snapshot, event)
    if replayed_snapshot != work_state:
        raise rr.StateIntegrityError("snapshot plus committed tail diverges from full event replay")
    if reasoning_graph.revision != work_state.revision or reasoning_graph.source_event_ids != tuple(
        event.event_id for event in events
    ):
        raise rr.StateIntegrityError("work-state and reasoning-graph causal projections diverge")

    evidence_records = runtime.journal.evidence_records_for_attempt(runtime.attempt_id)
    evidence: dict[str, rr.EvidenceRecord] = {}
    evidence_histories: list[tuple[str, tuple[rr.EvidenceRecord, ...]]] = []
    for item in evidence_records:
        if item.evidence_id in evidence:
            raise rr.StateIntegrityError("duplicate evidence identity during integrity replay")
        history = runtime.journal.evidence_history(
            item.evidence_id,
            attempt_id=runtime.attempt_id,
        )
        if not history or history[-1] != item:
            raise rr.StateIntegrityError("evidence latest projection diverges from its journal")
        evidence[item.evidence_id] = item
        evidence_histories.append((item.evidence_id, history))

    compilations: dict[str, rr.CapsuleCompilation] = {}
    delivery_attempt_ids: dict[str, str] = {}
    compilation_delivery_history: list[
        tuple[
            str,
            tuple[rr.CapsuleCompilation, ...],
            tuple[rr.DeliveryAttempt, ...],
        ]
    ] = []
    delivered_evidence: set[str] = set()
    for delivery_attempt_id, compilation in runtime.journal.compilations_for_attempt(
        runtime.attempt_id
    ):
        compilation_history = runtime.journal.compilation_history(delivery_attempt_id)
        delivery_history = runtime.journal.delivery_history(delivery_attempt_id)
        if (
            not compilation_history
            or not delivery_history
            or compilation_history[-1] != compilation
            or compilation.delivery_attempt != delivery_history[-1]
        ):
            raise rr.StateIntegrityError("compilation and delivery journals have divergent heads")
        if compilation.model_call_id in delivery_attempt_ids:
            raise rr.StateIntegrityError("model-call identity reused during integrity replay")
        compilations[delivery_attempt_id] = compilation
        delivery_attempt_ids[compilation.model_call_id] = delivery_attempt_id
        compilation_delivery_history.append(
            (
                delivery_attempt_id,
                compilation_history,
                delivery_history,
            )
        )
        if any(rr.is_delivered(attempt) for attempt in delivery_history):
            delivered_evidence.update(delivery_history[-1].evidence_ids)

    journal_delivery_ids = set(runtime.journal.delivery_attempt_ids_for_attempt(runtime.attempt_id))
    if journal_delivery_ids != set(compilations):
        raise rr.StateIntegrityError(
            "orphan delivery or compilation journal during integrity replay"
        )
    for item in evidence.values():
        if (
            item.lifecycle
            in {
                rr.EvidenceLifecycle.DELIVERED,
                rr.EvidenceLifecycle.ACTIVE,
                rr.EvidenceLifecycle.SATISFIED,
                rr.EvidenceLifecycle.SUPERSEDED,
            }
            and item.evidence_id not in delivered_evidence
        ):
            raise rr.StateIntegrityError(
                "evidence lifecycle has no provider-terminal delivery proof"
            )

    evidence_pairs = tuple(sorted(evidence.items()))
    compilation_pairs = tuple(sorted(compilations.items()))
    delivery_pairs = tuple(sorted(delivery_attempt_ids.items()))
    return _RuntimeProjection(
        work_state=work_state,
        reasoning_graph=reasoning_graph,
        evidence=evidence_pairs,
        compilations=compilation_pairs,
        delivery_attempt_ids=delivery_pairs,
        work_state_hash=work_state.state_hash,
        reasoning_graph_hash=reasoning_graph.graph_hash,
        evidence_state_hash=_hash(evidence_histories),
        compilation_delivery_hash=_hash(compilation_delivery_history),
    )


def _verify_with_request(
    runtime: rr.AttemptReasoningRuntime,
    request: rr.RecoveryInput,
) -> tuple[RuntimeIntegrityAttestation, _RuntimeProjection]:
    first = _replay_once(runtime, request)
    second = _replay_once(runtime, request)
    first_hashes = (
        first.work_state_hash,
        first.reasoning_graph_hash,
        first.evidence_state_hash,
        first.compilation_delivery_hash,
    )
    second_hashes = (
        second.work_state_hash,
        second.reasoning_graph_hash,
        second.evidence_state_hash,
        second.compilation_delivery_hash,
    )
    if first_hashes != second_hashes or first != second:
        raise rr.StateIntegrityError(
            "two independent canonical replays produced different projections"
        )
    aggregate_hash = _hash(
        {
            "attempt_id": runtime.attempt_id,
            "snapshot_id": request.snapshot_id,
            "committed_event_ids": request.committed_event_ids,
            "committed_tail_hash": request.committed_tail_hash,
            "projection_hashes": first_hashes,
        }
    )
    return (
        RuntimeIntegrityAttestation(
            attempt_id=runtime.attempt_id,
            snapshot_id=request.snapshot_id,
            committed_event_ids=request.committed_event_ids,
            committed_tail_hash=request.committed_tail_hash,
            work_state_hash=first.work_state_hash,
            reasoning_graph_hash=first.reasoning_graph_hash,
            evidence_state_hash=first.evidence_state_hash,
            compilation_delivery_hash=first.compilation_delivery_hash,
            aggregate_hash=aggregate_hash,
        ),
        first,
    )


def verify_runtime_integrity(
    runtime: rr.AttemptReasoningRuntime,
) -> RuntimeIntegrityAttestation:
    """Build a two-replay integrity attestation from append-only truth."""

    request = runtime.recovery_input()
    attestation, _projection = _verify_with_request(runtime, request)
    return attestation


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if not value or value in values else values + (value,)


def _quarantine(
    runtime: rr.AttemptReasoningRuntime,
    fault: rr.RuntimeFault,
    *,
    attempted: tuple[str, ...],
) -> rr.FailurePolicyState:
    state = replace(
        runtime.failure_state,
        health=rr.RuntimeHealthState.QUARANTINED,
        assurance=rr.AssuranceStatus.UNASSURED,
        recovery_attempted_signatures=attempted,
        gt_emission_enabled=False,
        gt_interruption_enabled=False,
        gt_certification_enabled=False,
        native_path_enabled=True,
        quarantine_reason=fault.code,
        failed_event_id=fault.event_id,
    )
    return _commit_failure_state(runtime, state)


def _commit_failure_state(
    runtime: rr.AttemptReasoningRuntime,
    state: rr.FailurePolicyState,
) -> rr.FailurePolicyState:
    """Update in-memory controls even when the corrupt journal cannot append."""

    runtime.failure_state = state
    if state.health is rr.RuntimeHealthState.QUARANTINED:
        try:
            _persist_quarantine_marker(runtime, state)
        except Exception:
            # The native path remains authoritative.  A marker failure must not
            # re-enable GT or escape into the host action path.
            pass
    try:
        runtime.journal.append_failure_state(state)
    except Exception:
        # The native path must remain available even when failure telemetry
        # itself cannot be written to the compromised canonical store.
        pass
    return state


def handle_runtime_fault(
    runtime: rr.AttemptReasoningRuntime,
    fault: rr.RuntimeFault,
) -> rr.FailurePolicyState:
    """Apply component isolation or one integrity-proven core recovery.

    This is the safe public entry point for the live observer.  It never lets a
    recovery-input or replay failure escape into the native agent path.
    """

    state = runtime.failure_state
    if state.health is rr.RuntimeHealthState.QUARANTINED:
        return state

    if fault.code not in rr.CORE_CORRUPTION_CODES:
        degraded = replace(
            state,
            health=rr.RuntimeHealthState.DEGRADED,
            assurance=rr.AssuranceStatus.DEGRADED,
            isolated_components=_append_unique(
                state.isolated_components,
                fault.component,
            ),
            gt_certification_enabled=False,
            native_path_enabled=True,
        )
        return _commit_failure_state(runtime, degraded)

    if fault.signature in state.recovery_attempted_signatures:
        return _quarantine(
            runtime,
            fault,
            attempted=state.recovery_attempted_signatures,
        )

    attempted = state.recovery_attempted_signatures + (fault.signature,)
    try:
        request = runtime.recovery_input()
        attestation, projection = _verify_with_request(runtime, request)
    except Exception:
        return _quarantine(runtime, fault, attempted=attempted)

    runtime.work_state = projection.work_state
    runtime.reasoning_graph = projection.reasoning_graph
    runtime._evidence = dict(projection.evidence)
    runtime._compilations = dict(projection.compilations)
    runtime._delivery_attempt_ids = dict(projection.delivery_attempt_ids)
    retains_degradation = bool(state.isolated_components)
    recovered = replace(
        state,
        health=(
            rr.RuntimeHealthState.DEGRADED
            if retains_degradation
            else rr.RuntimeHealthState.RECOVERED
        ),
        assurance=(
            rr.AssuranceStatus.DEGRADED if retains_degradation else rr.AssuranceStatus.ASSURED
        ),
        recovery_attempted_signatures=attempted,
        last_verified_snapshot_id=attestation.snapshot_id,
        gt_emission_enabled=True,
        gt_interruption_enabled=True,
        gt_certification_enabled=not retains_degradation,
        native_path_enabled=True,
        quarantine_reason=None,
        failed_event_id="",
    )
    return _commit_failure_state(runtime, recovered)


__all__ = [
    "RuntimeIntegrityAttestation",
    "handle_runtime_fault",
    "restore_persisted_quarantine",
    "verify_runtime_integrity",
]
