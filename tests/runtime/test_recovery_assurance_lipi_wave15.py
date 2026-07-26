"""Adversarial LIPI contracts for live fault routing and durable quarantine."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.recovery_assurance import (
    _quarantine_marker_path,
    handle_runtime_fault,
    restore_persisted_quarantine,
)


REVISION = rr.RevisionVector(
    repository_content="repo-wave15",
    graph="graph-wave15",
    lsp="lsp-wave15",
    runtime_evidence="runtime-wave15",
)


def _lightweight_attachment() -> seam.CanonicalRuntimeAttachment:
    runtime = SimpleNamespace(
        attempt_id="attempt-wave15-live",
        work_state=SimpleNamespace(sequence=0),
        failure_state=rr.FailurePolicyState.initial(
            attempt_id="attempt-wave15-live"
        ),
    )
    return seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=runtime,
        provider_boundary=object(),
        gateway_state=object(),
        graph_revision="graph-wave15",
    )


def _runtime(tmp_path):
    journal = rr.RuntimeJournal(tmp_path / "recovery-wave15.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-wave15-restart",
        journal=journal,
        initial_revision=REVISION,
    )
    return runtime, journal


def _core_fault() -> rr.RuntimeFault:
    return rr.RuntimeFault(
        code=rr.FaultCode.STATE_HASH_MISMATCH,
        component="canonical_runtime",
        signature="state-hash-mismatch@wave15",
        event_id="ev-wave15",
    )


def test_producer_database_read_failure_is_not_core_partial_commit(
    monkeypatch,
) -> None:
    attachment = _lightweight_attachment()
    captured: list[rr.RuntimeFault] = []
    monkeypatch.setattr(
        "groundtruth.runtime.recovery_assurance.handle_runtime_fault",
        lambda _runtime, fault: captured.append(fault),
    )

    attachment._record_fault(
        sqlite3.OperationalError("no such table: graph_edges"),
        component="covering_red",
    )

    assert len(captured) == 1
    assert captured[0].code is rr.FaultCode.SUBSTRATE_FAILED
    assert captured[0].component == "covering_red"


def test_fault_handler_failure_cannot_leave_core_runtime_marked_healthy(
    monkeypatch,
) -> None:
    attachment = _lightweight_attachment()

    def handler_failed(*_args, **_kwargs):
        raise RuntimeError("fault handler failed")

    monkeypatch.setattr(
        "groundtruth.runtime.recovery_assurance.handle_runtime_fault",
        handler_failed,
    )

    attachment._record_fault(
        rr.EventIntegrityError("canonical event gap"),
        component="canonical_observer",
    )

    state = attachment.attempt_runtime.failure_state
    assert state.health is rr.RuntimeHealthState.QUARANTINED
    assert state.assurance is rr.AssuranceStatus.UNASSURED
    assert state.gt_emission_enabled is False
    assert state.gt_interruption_enabled is False
    assert state.gt_certification_enabled is False
    assert state.native_path_enabled is True


def test_persisted_quarantine_survives_same_attempt_restart(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        monkeypatch.setattr(
            runtime,
            "recovery_input",
            lambda: (_ for _ in ()).throw(
                rr.StateIntegrityError("snapshot unreadable")
            ),
        )
        state = handle_runtime_fault(runtime, _core_fault())
        assert state.health is rr.RuntimeHealthState.QUARANTINED

        restarted = rr.AttemptReasoningRuntime(
            attempt_id=runtime.attempt_id,
            journal=journal,
            initial_revision=REVISION,
        )
        restore_persisted_quarantine(restarted)
        assert (
            restarted.failure_state.health
            is rr.RuntimeHealthState.QUARANTINED
        )
        assert restarted.failure_state.native_path_enabled is True
    finally:
        journal.close()


def test_quarantine_marker_is_scoped_to_one_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        monkeypatch.setattr(
            runtime,
            "recovery_input",
            lambda: (_ for _ in ()).throw(
                rr.StateIntegrityError("snapshot unreadable")
            ),
        )
        handle_runtime_fault(runtime, _core_fault())

        unrelated = rr.AttemptReasoningRuntime(
            attempt_id="attempt-wave15-unrelated",
            journal=journal,
            initial_revision=REVISION,
        )
        restored = restore_persisted_quarantine(unrelated)

        assert restored.attempt_id == unrelated.attempt_id
        assert restored.health is rr.RuntimeHealthState.HEALTHY
        assert restored.assurance is rr.AssuranceStatus.ASSURED
        assert restored.gt_emission_enabled is True
        assert restored.gt_interruption_enabled is True
        assert restored.gt_certification_enabled is True
    finally:
        journal.close()


def test_quarantine_survives_restart_when_canonical_health_append_fails(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        monkeypatch.setattr(
            runtime,
            "recovery_input",
            lambda: (_ for _ in ()).throw(
                rr.StateIntegrityError("snapshot unreadable")
            ),
        )
        original_append = journal.append_failure_state

        def fail_health_append(_state):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            journal,
            "append_failure_state",
            fail_health_append,
        )
        state = handle_runtime_fault(runtime, _core_fault())
        assert state.health is rr.RuntimeHealthState.QUARANTINED
        monkeypatch.setattr(
            journal,
            "append_failure_state",
            original_append,
        )

        restarted = rr.AttemptReasoningRuntime(
            attempt_id=runtime.attempt_id,
            journal=journal,
            initial_revision=REVISION,
        )
        marker = _quarantine_marker_path(restarted)
        before = marker.read_bytes()
        restore_persisted_quarantine(restarted)
        assert (
            restarted.failure_state.health
            is rr.RuntimeHealthState.QUARANTINED
        )
        assert restarted.failure_state.native_path_enabled is True
        assert marker.read_bytes() == before
    finally:
        journal.close()


def test_tampered_quarantine_marker_fails_closed_on_restart(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, journal = _runtime(tmp_path)
    try:
        monkeypatch.setattr(
            runtime,
            "recovery_input",
            lambda: (_ for _ in ()).throw(
                rr.StateIntegrityError("snapshot unreadable")
            ),
        )
        original_append = journal.append_failure_state
        monkeypatch.setattr(
            journal,
            "append_failure_state",
            lambda _state: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            ),
        )
        handle_runtime_fault(runtime, _core_fault())
        marker = _quarantine_marker_path(runtime)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["state"]["failed_event_id"] = "forged-event"
        marker.write_bytes(
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        )
        monkeypatch.setattr(
            journal,
            "append_failure_state",
            original_append,
        )

        restarted = rr.AttemptReasoningRuntime(
            attempt_id=runtime.attempt_id,
            journal=journal,
            initial_revision=REVISION,
        )
        restored = restore_persisted_quarantine(restarted)

        assert restored.health is rr.RuntimeHealthState.QUARANTINED
        assert restored.assurance is rr.AssuranceStatus.UNASSURED
        assert restored.quarantine_reason is rr.FaultCode.STATE_HASH_MISMATCH
        assert restored.failed_event_id != "forged-event"
        assert restored.gt_emission_enabled is False
        assert restored.gt_interruption_enabled is False
        assert restored.gt_certification_enabled is False
        assert restored.native_path_enabled is True
    finally:
        journal.close()
