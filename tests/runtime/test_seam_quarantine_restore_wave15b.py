"""Seam-level guard: a sealed terminal marker must survive process restart.

WHY THIS FILE EXISTS (LIPI / Integration).  ``tests/runtime/
test_recovery_assurance_lipi_wave15.py`` is green, but each of its restart
cases calls ``restore_persisted_quarantine(restarted)`` EXPLICITLY.  That
proves the function, not the wiring.  ``restore_persisted_quarantine`` is
defined at ``recovery_assurance.py:292`` and has zero call sites in
``artifact_deepswe/gt_mini_patch.py``, so the live seam
(``install_canonical_runtime``) never consults the marker: a quarantined
attempt restarts HEALTHY and GT resumes emitting.

Placement note (LIPI / Integration): the restore call belongs in
``install_canonical_runtime`` immediately after construction, NOT inside
``AttemptReasoningRuntime.__init__`` -- ``recovery_assurance`` imports
``reasoning_runtime`` (line 20), so calling it from the constructor would be a
circular import.

This drives the real seam entry point, so it cannot be satisfied by calling the
restore helper directly from a test.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.recovery_assurance import (
    _quarantine_marker_path,
    handle_runtime_fault,
)


ATTEMPT = "attempt-wave15b-quarantine"
REVISION = rr.RevisionVector(
    repository_content="repo-wave15b-q",
    graph="graph-wave15b-q",
    lsp="lsp-wave15b-q",
    runtime_evidence="runtime-wave15b-q",
)


def _core_fault() -> rr.RuntimeFault:
    return rr.RuntimeFault(
        code=rr.FaultCode.STATE_HASH_MISMATCH,
        component="canonical_runtime",
        signature="state-hash-mismatch@wave15b",
        event_id="ev-wave15b",
    )


def _quarantine_a_prior_process(tmp_path, monkeypatch) -> str:
    """Fault a first runtime so a sealed terminal marker exists on disk."""
    journal_path = tmp_path / "gt_reasoning_runtime.sqlite3"
    journal = rr.RuntimeJournal(journal_path)
    journal.open()
    try:
        runtime = rr.AttemptReasoningRuntime(
            attempt_id=ATTEMPT,
            journal=journal,
            initial_revision=REVISION,
        )
        monkeypatch.setattr(
            runtime,
            "recovery_input",
            lambda: (_ for _ in ()).throw(
                rr.StateIntegrityError("snapshot unreadable")
            ),
        )
        monkeypatch.setattr(
            journal,
            "append_failure_state",
            lambda _state: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            ),
        )
        state = handle_runtime_fault(runtime, _core_fault())
        assert state.health is rr.RuntimeHealthState.QUARANTINED
        marker = _quarantine_marker_path(runtime)
        assert marker.exists(), "precondition: prior process wrote no marker"
    finally:
        journal.close()
    return str(journal_path)


def _fake_model():
    """The minimum surface ``MiniSweProviderBoundary`` wraps (see its __init__)."""
    return SimpleNamespace(
        _prepare_messages_for_api=lambda *a, **k: [],
        _query=lambda *a, **k: {},
        query=lambda *a, **k: {},
    )


def _fake_agent():
    """``MiniSweCommitmentBoundary`` requires ``execute_actions`` (it raises
    ``ValueError`` otherwise); the provider boundary requires ``add_messages``."""
    return SimpleNamespace(
        add_messages=lambda *a, **k: None,
        execute_actions=lambda *a, **k: None,
    )


def _install(tmp_path, monkeypatch, journal_path: str):
    """Call the real seam entry point the runner uses."""
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None, raising=False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    env = {
        "GT_ATTEMPT_ID": ATTEMPT,
        "GT_CANONICAL_JOURNAL": journal_path,
        "GT_RUNTIME_LEDGER": str(tmp_path / "gt_runtime_ledger.jsonl"),
    }
    attachment = seam.install_canonical_runtime(
        model=_fake_model(),
        agent=_fake_agent(),
        env=env,
        task="restart after a quarantined attempt",
    )
    # Guard against a vacuous RED: if the seam bailed out entirely, these tests
    # would "fail" without ever reaching the quarantine question.
    assert getattr(attachment, "attempt_runtime", None) is not None, (
        "install_canonical_runtime returned an unattached object; the harness "
        "is wrong, not the runtime under test"
    )
    return attachment


def test_seam_restores_terminal_quarantine_on_restart(
    tmp_path,
    monkeypatch,
) -> None:
    """RED until ``install_canonical_runtime`` consults the durable marker."""
    journal_path = _quarantine_a_prior_process(tmp_path, monkeypatch)

    attachment = _install(tmp_path, monkeypatch, journal_path)

    runtime = getattr(attachment, "attempt_runtime", None)
    if runtime is None:
        pytest.fail("seam returned no attempt_runtime; cannot assess health")
    state = runtime.failure_state
    assert state.health is rr.RuntimeHealthState.QUARANTINED, (
        "restart resurrected a quarantined attempt as "
        f"{state.health!r}; the durable marker was never consulted"
    )
    assert state.assurance is rr.AssuranceStatus.UNASSURED
    assert state.gt_emission_enabled is False
    assert state.gt_interruption_enabled is False
    assert state.gt_certification_enabled is False


def test_seam_restart_preserves_the_native_agent_path(
    tmp_path,
    monkeypatch,
) -> None:
    """Quarantine must never disable the agent's own tooling."""
    journal_path = _quarantine_a_prior_process(tmp_path, monkeypatch)

    attachment = _install(tmp_path, monkeypatch, journal_path)

    runtime = getattr(attachment, "attempt_runtime", None)
    if runtime is None:
        pytest.fail("seam returned no attempt_runtime; cannot assess health")
    assert runtime.failure_state.native_path_enabled is True


def test_clean_attempt_is_not_quarantined_by_the_restore_hook(
    tmp_path,
    monkeypatch,
) -> None:
    """Correct-or-quiet: no marker means the restore hook changes nothing.

    This is the biting half of the pair -- an implementation that quarantines
    unconditionally passes the two tests above and fails this one.
    """
    journal_path = str(tmp_path / "gt_reasoning_runtime.sqlite3")

    attachment = _install(tmp_path, monkeypatch, journal_path)

    runtime = getattr(attachment, "attempt_runtime", None)
    if runtime is None:
        pytest.fail("seam returned no attempt_runtime; cannot assess health")
    assert runtime.failure_state.health is not rr.RuntimeHealthState.QUARANTINED
