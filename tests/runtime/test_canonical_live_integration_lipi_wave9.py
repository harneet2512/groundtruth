"""Cross-layer RED contracts for the sole canonical Mini-SWE live path."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.reasoning_runtime import RevisionVector
from groundtruth.runtime import reasoning_runtime as rr


class _Runtime:
    attempt_id = "attempt-wave9"
    work_state = SimpleNamespace(
        sequence=0,
        revision=RevisionVector(
            repository_content="repo-before",
            graph="graph-before",
            lsp="lsp-before",
            runtime_evidence="runtime-before",
        ),
    )


def _attachment() -> seam.CanonicalRuntimeAttachment:
    return seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=_Runtime(),
        provider_boundary=object(),
        gateway_state=object(),
        graph_revision="startup-graph",
    )


def test_native_action_exception_is_not_falsely_recorded_as_success() -> None:
    attachment = _attachment()
    captured: dict[str, object] = {}

    def capture(action: object, result: object) -> None:
        captured["action"] = action
        captured["result"] = result

    attachment.observe_action_result = capture  # type: ignore[method-assign]
    attachment.observe_action_exception({"command": "false"}, RuntimeError("native action failed"))

    assert captured["result"] == {
        "output": "native action failed",
        "returncode": None,
    }


@pytest.mark.parametrize(
    ("error", "component", "expected_code"),
    (
        (
            rr.EventIntegrityError("canonical event gap"),
            "canonical_observer",
            rr.FaultCode.CAUSAL_EVENT_GAP,
        ),
        (
            rr.EventSchemaVersionError("unsupported canonical hash schema"),
            "canonical_observer",
            rr.FaultCode.SUBSTRATE_FAILED,
        ),
        (
            rr.StateIntegrityError("repository revision mismatch"),
            "canonical_observer",
            rr.FaultCode.REPOSITORY_REVISION_INCONSISTENCY,
        ),
        (
            ValueError("join mismatch"),
            "provider:OBSERVATION_JOIN",
            rr.FaultCode.OBSERVATION_JOIN_FAILED,
        ),
    ),
)
def test_live_faults_map_to_central_integrity_taxonomy(
    monkeypatch,
    error,
    component,
    expected_code,
) -> None:
    attachment = _attachment()
    attachment.attempt_runtime.failure_state = (
        rr.FailurePolicyState.initial(attempt_id=_Runtime.attempt_id)
    )
    captured = []
    monkeypatch.setattr(
        "groundtruth.runtime.recovery_assurance.handle_runtime_fault",
        lambda runtime, fault: captured.append((runtime, fault)),
    )

    attachment._record_fault(error, component=component)

    assert len(captured) == 1
    assert captured[0][0] is attachment.attempt_runtime
    assert captured[0][1].code is expected_code


def test_revision_rehashes_graph_instead_of_reusing_startup_digest(
    monkeypatch,
) -> None:
    attachment = _attachment()
    graph_digests = iter(("graph-after-1", "graph-after-2"))
    monkeypatch.setattr(
        seam,
        "_canonical_file_digest",
        lambda _path: next(graph_digests),
    )
    monkeypatch.setattr(
        seam,
        "_canonical_repository_digest",
        lambda _root: "repo-after",
    )
    monkeypatch.setattr(seam, "_db_path", lambda: "graph.db")

    assert attachment._revision(1).graph == "graph-after-1"
    assert attachment._revision(2).graph == "graph-after-2"


def test_repository_digest_moves_when_untracked_file_bytes_change(
    tmp_path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Fixture"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    untracked = tmp_path / "new.py"
    untracked.write_text("value = 1\n", encoding="utf-8")
    first = seam._canonical_repository_digest(str(tmp_path))
    untracked.write_text("value = 2\n", encoding="utf-8")
    second = seam._canonical_repository_digest(str(tmp_path))

    assert first != second


def test_real_installer_opens_attempt_journal_before_reconstruction(
    tmp_path,
    monkeypatch,
) -> None:
    class Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            return SimpleNamespace(id="", status="failed", choices=[])

    class Agent:
        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, message):
            return []

    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-installer",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        },
        task="native issue",
    )

    assert attachment.attached is True
    assert attachment.attempt_runtime.journal.connection is not None
    assert attachment.commitment_boundary is not None
    assert attachment.provider_boundary.fault_handler is not None
    attachment.attempt_runtime.journal.close()


def test_internal_validation_is_committed_as_canonical_event_truth(
    tmp_path,
) -> None:
    revision = rr.RevisionVector(
        repository_content="repo-internal",
        graph="graph-internal",
        lsp="lsp-internal",
        runtime_evidence="runtime-internal",
    )
    journal = rr.RuntimeJournal(tmp_path / "internal-validation.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-internal-validation",
        journal=journal,
        initial_revision=revision,
    )
    attachment = seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=runtime,
        provider_boundary=None,
        gateway_state=SimpleNamespace(canonical_revision=revision),
        graph_revision=revision.graph,
    )

    event = attachment._append_internal_validation_event(
        component="syntax_result",
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.COMPILE_RESULT,
                subject="src/session.py",
                status="syntax_error",
                authority=rr.Authority.RESULT_DERIVED,
                provenance=("edit_check",),
            ),
        ),
    )

    assert event is not None
    assert event.carrier == "gt_internal_validation"
    assert runtime.work_state.sequence == 1
    assert runtime.work_state.compile_count == 1
    assert runtime.work_state.phase is rr.Phase.VALIDATION
    assert journal.events(runtime.attempt_id) == (event,)
    journal.close()


def test_active_decision_comes_from_work_state_not_firing_feature() -> None:
    attachment = _attachment()
    state = SimpleNamespace(
        phase=rr.Phase.RECOVERY,
        focused_symbols=("refreshSession",),
        focused_files=("src/auth/session.py",),
    )
    contract = rr.feature_contract_for("localization")
    assert contract is not None
    record = rr.EvidenceRecord(
        evidence_id="GT-E-localization",
        feature_id="localization",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="src/auth/session.py",
        claim="A localization producer happened to fire.",
        actionable_consequence="Inspect the file.",
        provenance=("src/auth/session.py:1",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=_Runtime.work_state.revision,
        causal_neighborhood=("subject:src/auth/session.py",),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=10,
        failure_prevention=1,
        causal_value=1,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
    )

    active = attachment._active_decision(
        (record,),
        state,
        _Runtime.work_state.revision,
    )

    assert active.context is rr.DecisionContext.FAILURE_RECOVERY
    assert active.required_roles == (rr.EvidenceRole.CONTRADICTION,)
    assert active.useful_roles == (
        rr.EvidenceRole.VALIDATION,
        rr.EvidenceRole.EXECUTION_REACHABILITY,
        rr.EvidenceRole.MATERIAL_UNCERTAINTY,
    )


def test_unseen_obligation_causes_fresh_inference_before_native_edit(
    tmp_path,
    monkeypatch,
) -> None:
    class Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            return SimpleNamespace(id="", status="failed", choices=[])

    class Agent:
        def __init__(self) -> None:
            self.executed = 0

        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, message):
            self.executed += 1
            return [{"output": "native edit executed"}]

    agent = Agent()
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-precommit",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="native issue",
    )
    contract = rr.feature_contract_for("obligations")
    assert contract is not None
    revision = attachment.attempt_runtime.work_state.revision
    obligation = rr.EvidenceRecord(
        evidence_id="GT-E-obligation",
        feature_id="obligations",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="issue",
        claim="Preserve the returned Session.",
        actionable_consequence="Keep the return contract during the edit.",
        provenance=("issue:1",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=revision,
        causal_neighborhood=(
            "decision:PATCH_CONSTRUCTION",
            "subject:issue",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=rr.MandatoryReason.TASK_OBLIGATION,
        token_cost=12,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        authority=rr.Authority.RESULT_DERIVED,
        observed_substrates=("issue_text", "obligation_parser"),
    )
    attachment.attempt_runtime.ingest_evidence(obligation)
    event_count_before = len(
        attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
    )

    result = agent.execute_actions(
        {
            "extra": {
                "response": {"id": "provider-call-edit"},
                "actions": [
                    {
                        "operation": "EDIT",
                        "command": "python -c \"from pathlib import Path; "
                        "Path('x.py').write_text('x')\""
                    }
                ],
            }
        }
    )

    assert result == []
    assert agent.executed == 0
    assert len(
        attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
    ) == event_count_before
    assert attachment.pending_native_actions == {}
    assert attachment.provider_boundary._active is not None
    assert (
        attachment.attempt_runtime.evidence_record(
            "GT-E-obligation"
        ).lifecycle
        is rr.EvidenceLifecycle.RELEASED
    )
    attachment.attempt_runtime.journal.close()


@pytest.mark.parametrize(
    (
        "submit_refusal_on",
        "verify_execute_on",
        "certificate_on",
        "expected_owners",
    ),
    (
        (False, False, False, ()),
        (True, False, False, ()),
        (True, True, False, ("GT_SS_SUBMIT_RED",)),
        (False, False, True, ("GT_CERT_DELIVERY",)),
        (
            True,
            True,
            True,
            ("GT_CERT_DELIVERY", "GT_SS_SUBMIT_RED"),
        ),
    ),
)
def test_attributable_covering_red_becomes_submit_refusal_before_native_submit(
    tmp_path,
    monkeypatch,
    submit_refusal_on,
    verify_execute_on,
    certificate_on,
    expected_owners,
) -> None:
    if submit_refusal_on:
        monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    else:
        monkeypatch.delenv("GT_SS_SUBMIT_RED", raising=False)
    if verify_execute_on:
        monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    else:
        monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    if certificate_on:
        monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    else:
        monkeypatch.delenv("GT_CERT_DELIVERY", raising=False)
    class Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            return SimpleNamespace(id="", status="failed", choices=[])

    class Agent:
        def __init__(self) -> None:
            self.executed = 0

        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, message):
            self.executed += 1
            return [{"output": "native submit executed"}]

    source = tmp_path / "src" / "session.py"
    source.parent.mkdir(parents=True)
    source.write_text("def refresh_session():\n    return None\n", encoding="utf-8")
    agent = Agent()
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-submit-refusal",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="repair refresh_session",
    )
    view_action = {"command": "view", "path": "src/session.py"}
    attachment.observe_action_proposal(view_action)
    attachment.observe_action_result(
        view_action,
        {"output": source.read_text(encoding="utf-8"), "returncode": 0},
    )
    attachment.last_covering_result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/test_session.py"],
        "ran": ["tests/test_session.py"],
        "command": ["pytest", "-q", "tests/test_session.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_refresh_session"],
    }

    result = agent.execute_actions(
        {
            "extra": {
                "response": {"id": "provider-call-submit"},
                "actions": [
                    {
                        "operation": "SUBMIT",
                        "command": "submit",
                    }
                ],
            }
        }
    )

    if not expected_owners:
        assert result == [{"output": "native submit executed"}]
        assert agent.executed == 1
        assert not any(
            item.feature_id == "submit_refusal"
            for item in attachment.attempt_runtime._evidence.values()
        )
        attachment.attempt_runtime.journal.close()
        return

    assert result == []
    assert agent.executed == 0
    records = tuple(attachment.attempt_runtime._evidence.values())
    refusal = next(item for item in records if item.feature_id == "submit_refusal")
    assert refusal.owner_feature_ids == expected_owners
    assert refusal.lifecycle is rr.EvidenceLifecycle.RELEASED
    assert attachment.provider_boundary._active is not None
    attachment.attempt_runtime.journal.close()


def test_executed_structured_view_is_proposal_result_pair_with_subject(
    tmp_path,
    monkeypatch,
) -> None:
    class Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            return SimpleNamespace(id="", status="failed", choices=[])

    class Agent:
        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, message):
            return []

    source = tmp_path / "src" / "session.py"
    source.parent.mkdir(parents=True)
    source.write_text("def refresh_session():\n    return None\n", encoding="utf-8")
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-structured-view",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="inspect refresh_session",
    )
    action = {
        "command": "view",
        "path": "src/session.py",
    }

    proposal = attachment.observe_action_proposal(action)
    attachment.observe_action_result(
        action,
        {"output": source.read_text(encoding="utf-8"), "returncode": 0},
    )

    events = attachment.attempt_runtime.journal.events(
        attachment.attempt_runtime.attempt_id
    )
    assert events[-2] == proposal
    assert events[-1].parents[0].ref_id == proposal.event_id
    assert events[-1].action is not None
    assert events[-1].action.subject == "src/session.py"
    assert (
        attachment.attempt_runtime.work_state.focused_files[-1]
        == "src/session.py"
    )
    attachment.attempt_runtime.journal.close()


def test_unknown_native_action_is_not_fabricated_as_repository_edit() -> None:
    action = {"command": "python tools/custom_probe.py"}

    canonical = seam.CanonicalRuntimeAttachment._canonical_native_action(
        action,
        action_id="action-custom-probe",
    )

    assert canonical.operation is rr.ActionOperation.OTHER
