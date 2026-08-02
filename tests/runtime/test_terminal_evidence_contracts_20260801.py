from __future__ import annotations

import hashlib

import pytest

from groundtruth.runtime.terminal_evidence import (
    BuildConfigurationSlice,
    ClosedBlockerRegistry,
    EvidenceStatus,
    FailureIdentity,
    FailureRecoveryLedger,
    NewFilePrecedent,
    ObligationState,
    SyntaxReceipt,
    TerminalEvidenceSession,
    bind_episode_terminal_evidence,
    compile_submit_suppression,
    detect_build_configuration_slices,
    diff_obligations,
    obligation_states_from_issue,
    record_episode_failure,
    stable_obligation_id,
)


SHA = "a" * 64


def test_syntax_receipt_binds_exact_postimage_and_preserves_diagnostics() -> None:
    source = b"def broken(:\n"
    receipt = SyntaxReceipt.build(
        path="src/a.py",
        source_bytes=source,
        repository_revision="worktree:1",
        configuration_sha256=SHA,
        producer="cpython.ast",
        producer_version="3.12",
        status=EvidenceStatus.EXECUTION_SPECIFIC,
        verdict="syntax_error",
        native_diagnostics=b"SyntaxError: invalid syntax\r\n",
    )
    assert receipt.source_sha256 == hashlib.sha256(source).hexdigest()
    assert receipt.native_diagnostics == b"SyntaxError: invalid syntax\r\n"
    assert receipt.path == "src/a.py"


def test_syntax_receipt_rejects_absolute_or_parent_path() -> None:
    for path in ("C:/repo/a.py", "../a.py", "/repo/a.py"):
        with pytest.raises(ValueError):
            SyntaxReceipt.build(
                path=path,
                source_bytes=b"x = 1\n",
                repository_revision="r1",
                configuration_sha256=SHA,
                producer="parser",
                producer_version="1",
                status=EvidenceStatus.EXACT,
                verdict="valid",
                native_diagnostics=b"",
            )


def test_obligation_identity_and_delta_are_stable_and_invalidation_aware() -> None:
    oid = stable_obligation_id(
        task_sha256=SHA,
        start_byte=3,
        end_byte=11,
        parser_version="v1",
    )
    same = ObligationState(oid, "evidenced", "r1", "task:3-11")
    changed = ObligationState(oid, "invalidated", "r2", "task:3-11")
    delta = diff_obligations((same,), (changed,))
    assert delta.changed == (changed,)
    assert delta.removed_ids == ()


def test_failure_recovery_requires_identical_normalized_conditions() -> None:
    ledger = FailureRecoveryLedger()
    failure = FailureIdentity.build(
        action=("pytest", "-q"), cwd=".", environment={"MODE": "dev"},
        pre_state_revision="r1", exit_code=1, signal=None,
        diagnostics="FAILED  test_a.py::test_x   0.21s\nE assert 1 == 2",
    )
    ledger.record(failure, remedy="inspect fixture", outcome="failed")
    assert ledger.lookup(failure) is not None
    changed_revision = FailureIdentity.build(
        action=("pytest", "-q"), cwd=".", environment={"MODE": "dev"},
        pre_state_revision="r2", exit_code=1, signal=None,
        diagnostics="FAILED test_a.py::test_x 0.99s\nE assert 1 == 2",
    )
    assert ledger.lookup(changed_revision) is None


def test_failure_diagnostic_normalization_removes_duration_noise_only() -> None:
    one = FailureIdentity.build(
        action=("pytest",), cwd=".", environment={}, pre_state_revision="r1",
        exit_code=1, signal=None, diagnostics="FAILED x 0.21s\nline 12",
    )
    two = FailureIdentity.build(
        action=("pytest",), cwd=".", environment={}, pre_state_revision="r1",
        exit_code=1, signal=None, diagnostics="FAILED x 9.87s\nline 12",
    )
    assert one.diagnostic_sha256 == two.diagnostic_sha256


def test_configuration_slice_cannot_claim_exact_without_closed_coverage() -> None:
    with pytest.raises(ValueError):
        BuildConfigurationSlice(
            adapter="bazel", configuration_id="//:dev", inputs_sha256=SHA,
            status=EvidenceStatus.EXACT, coverage_closed=False,
            targets=("//app:lib",), source_membership=("src/a.py",),
            generated_inputs=(), dependency_edges=(), omissions=("select branches",),
        )


def test_new_file_precedent_is_advisory_and_source_anchored() -> None:
    precedent = NewFilePrecedent.build(
        new_path="src/new.py", precedent_path="src/old.py", revision="abc123",
        reasons=("same_language", "same_directory"), score=0.8,
    )
    assert precedent.status is EvidenceStatus.ADVISORY
    assert precedent.precedent_path == "src/old.py"
    with pytest.raises(ValueError):
        NewFilePrecedent.build(
            new_path="src/new.py", precedent_path="", revision="abc123",
            reasons=("same_language",), score=0.8,
        )


def test_closed_blocker_registry_suppresses_only_fresh_closed_enforce_blocker() -> None:
    registry = ClosedBlockerRegistry(enforce=True)
    blocker = registry.register(
        blocker_id="syntax:src/a.py", producer="syntax", witness="src/a.py:1",
        scope="file", creating_revision="r1", current_revision="r1",
        invalidation_rule="postimage_changed", invalidation_key=SHA,
        status=EvidenceStatus.EXACT, scope_closed=True,
    )
    assert blocker.suppression_eligible
    assert registry.should_suppress("r1", {"syntax:src/a.py": SHA})
    assert not registry.should_suppress("r2", {"syntax:src/a.py": SHA})
    assert not registry.should_suppress("r1", {"syntax:src/a.py": "b" * 64})
    registry.resolve("syntax:src/a.py", remediation="edited file")
    assert not registry.should_suppress("r1", {"syntax:src/a.py": SHA})


def test_advisory_or_open_scope_blocker_never_suppresses() -> None:
    for status, closed in ((EvidenceStatus.ADVISORY, True), (EvidenceStatus.EXACT, False)):
        registry = ClosedBlockerRegistry(enforce=True)
        blocker = registry.register(
            blocker_id=f"b:{status.value}:{closed}", producer="p", witness="src/a.py:1",
            scope="file", creating_revision="r1", current_revision="r1",
            invalidation_rule="witness_changed", invalidation_key=SHA,
            status=status, scope_closed=closed,
        )
        assert not blocker.suppression_eligible
        assert not registry.should_suppress("r1", {blocker.blocker_id: SHA})


def test_submit_suppression_kill_switch_is_immediate() -> None:
    registry = ClosedBlockerRegistry(enforce=False)
    blocker = registry.register(
        blocker_id="b", producer="p", witness="src/a.py:1", scope="file",
        creating_revision="r1", current_revision="r1",
        invalidation_rule="postimage_changed", invalidation_key=SHA,
        status=EvidenceStatus.EXACT, scope_closed=True,
    )
    assert blocker.suppression_eligible
    assert not registry.should_suppress("r1", {"b": SHA})


def test_issue_obligations_bind_utf8_source_byte_spans_and_deliver_only_delta() -> None:
    issue = "Préface: remove `old_url` parameter."
    states = obligation_states_from_issue(issue, task_revision="task-v1")
    assert len(states) == 1
    start, end = map(int, states[0].task_anchor.split(":", 1)[1].split("-"))
    assert issue.encode("utf-8")[start:end].decode("utf-8").startswith("remove")

    session = TerminalEvidenceSession.from_issue(issue, task_revision="task-v1")
    assert session.obligation_delta().changed == states
    assert session.obligation_delta().changed == ()
    session.set_obligation_state(states[0].obligation_id, "invalidated", "repo:r2")
    assert session.obligation_delta().changed[0].state == "invalidated"


def test_terminal_session_recovers_only_identical_failure() -> None:
    session = TerminalEvidenceSession.from_issue("remove `old_url`", task_revision="task")
    identity = FailureIdentity.build(
        action=("pytest",), cwd="repo", environment={"A": "1"},
        pre_state_revision="r1", exit_code=1, signal=None, diagnostics="FAILED x 0.2s",
    )
    session.record_failure(identity, remedy="inspect x", outcome="still_failed")
    assert session.recovery_for(identity).remedy == "inspect x"  # type: ignore[union-attr]


def test_episode_state_uses_terminal_obligation_and_exact_failure_session() -> None:
    from groundtruth.runtime.episode_state import EpisodeState

    episode = EpisodeState(episode_id="task-1")
    session = bind_episode_terminal_evidence(
        episode, issue_text="remove `old_url`", task_revision="task-v1"
    )
    assert episode.obligations is None
    assert episode._terminal_evidence_session is session
    assert session.obligation_delta().changed
    identity = FailureIdentity.build(
        action=("pytest",), cwd="repo", environment={}, pre_state_revision="r1",
        exit_code=1, signal=None, diagnostics="FAILED x",
    )
    record_episode_failure(episode, identity, remedy="inspect", outcome="failed")
    assert identity.sha256 in episode.failure_fingerprints
    assert episode.last_failure_record["failure_identity_sha256"] == identity.sha256


def test_detected_build_adapter_is_honest_overapproximation(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    slices = detect_build_configuration_slices(tmp_path)
    python = next(item for item in slices if item.adapter == "python")
    assert python.status is EvidenceStatus.SOUND_OVERAPPROX
    assert not python.coverage_closed
    assert python.source_membership == ("src/a.py",)
    assert "target_membership_not_resolved" in python.omissions


def test_submit_suppression_receipt_proves_zero_provider_delivery() -> None:
    registry = ClosedBlockerRegistry(enforce=True)
    registry.register(
        blocker_id="syntax:a", producer="syntax", witness="src/a.py:1", scope="file",
        creating_revision="r1", current_revision="r1", invalidation_rule="postimage_changed",
        invalidation_key=SHA, status=EvidenceStatus.EXACT, scope_closed=True,
    )
    receipt = compile_submit_suppression(
        registry=registry, current_revision="r1", current_invalidation_keys={"syntax:a": SHA},
        action_bytes=b"submit", provider_payload_bytes=b"",
    )
    assert receipt is not None
    assert receipt.provider_dispatched is False
    assert receipt.chars_delivered == 0
    assert receipt.blocker_ids == ("syntax:a",)
    registry.enforce = False
    assert compile_submit_suppression(
        registry=registry, current_revision="r1", current_invalidation_keys={"syntax:a": SHA},
        action_bytes=b"submit", provider_payload_bytes=b"",
    ) is None


def test_provider_boundary_persists_suppression_without_dispatch(monkeypatch) -> None:
    from groundtruth.runtime import miniswe_provider_boundary as boundary_module

    rows = []
    monkeypatch.setattr(boundary_module, "append_ledger_line", lambda row, _path: rows.append(row))
    boundary = object.__new__(boundary_module.MiniSweProviderBoundary)
    boundary._receipt_sink_path = "unused.jsonl"
    boundary._submit_suppression_receipts = []
    registry = ClosedBlockerRegistry(enforce=True)
    registry.register(
        blocker_id="syntax:a", producer="syntax", witness="src/a.py:1", scope="file",
        creating_revision="r1", current_revision="r1", invalidation_rule="postimage_changed",
        invalidation_key=SHA, status=EvidenceStatus.EXACT, scope_closed=True,
    )
    receipt = boundary.authorize_submit_suppression(
        registry=registry, current_revision="r1", current_invalidation_keys={"syntax:a": SHA},
        action_bytes=b"submit", provider_payload_bytes=b"",
    )
    assert receipt is not None
    assert rows == [{
        "layer": "provider.submit_suppression", "event_type": "submit_suppression",
        "outcome": "suppressed", "schema": "gt.submit_suppression_receipt.v1",
        "repository_revision": "r1", "action_sha256": receipt.action_sha256,
        "provider_payload_sha256": receipt.provider_payload_sha256,
        "blocker_ids": ("syntax:a",), "provider_dispatched": False, "chars_delivered": 0,
    }]

    monkeypatch.setattr(
        boundary_module, "append_ledger_line",
        lambda _row, _path: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert boundary.authorize_submit_suppression(
        registry=registry, current_revision="r1", current_invalidation_keys={"syntax:a": SHA},
        action_bytes=b"submit", provider_payload_bytes=b"",
    ) is None
