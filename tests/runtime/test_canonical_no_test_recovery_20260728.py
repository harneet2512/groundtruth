"""RED contracts for canonical recovery after repeated zero-test executions.

``classify_test_observation`` already distinguishes an explicit runner-owned
``executed_no_tests`` result from an unobserved process. The canonical adapter
currently drops that truth, so WorkState and the deep recovery producer cannot
observe the decision boundary. These tests keep the signal typed and replayable;
they never invoke the legacy no-test globals or prose nudge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import gateway
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe


ZERO_TEST_OUTPUT = (
    "============================= test session starts =============================\n"
    "collected 0 items\n\n"
    "============================ no tests ran in 0.01s ============================\n"
)
PASS_OUTPUT = "============================== 1 passed in 0.01s ==============================\n"
FAIL_OUTPUT = "============================== 1 failed in 0.01s ==============================\n"
ENV_FAIL_OUTPUT = "ModuleNotFoundError: No module named 'fixture_dependency'\n"
COLLECTION_ENV_FAIL_OUTPUT = (
    "collected 0 items / 1 error\n"
    "ERROR collecting tests/test_widget.py\n"
    "ModuleNotFoundError: No module named 'fixture_dependency'\n"
)
COMPILE_FAIL_OUTPUT = "SyntaxError: invalid syntax\n"


class _Model:
    def _prepare_messages_for_api(self, messages):
        return messages

    def _query(self, messages, **kwargs):
        return SimpleNamespace(id="", status="failed", choices=[])


class _Agent:
    def add_messages(self, *messages):
        return list(messages)

    def execute_actions(self, message):
        return []


def _attachment(tmp_path: Path, monkeypatch, attempt_id: str):
    repo = tmp_path / "repo"
    source = repo / "src" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "no-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "No-test fixture"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "src/a.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=repo,
        check=True,
    )

    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    monkeypatch.setattr(
        seam,
        "_invalidate_on_edit",
        lambda *_args, **_kwargs: seam._L6RefreshOutcome(
            True,
            "test_refresh",
            str(tmp_path / "graph.db"),
        ),
    )
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    monkeypatch.delenv("GT_EDIT_CHECK", raising=False)
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)

    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": attempt_id,
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="edit src/a.py and verify the behavior",
    )
    assert attachment.attached is True
    return attachment, source


def _observe_source_edit(
    attachment,
    source: Path,
    *,
    before: str,
    after: str,
) -> None:
    action = {
        "command": "str_replace",
        "path": "src/a.py",
        "old_str": before,
        "new_str": after,
    }
    attachment.observe_action_proposal(action)
    seam._gateway_capture_edit_preimage(action)
    source.write_text(after, encoding="utf-8")
    native_result = {"output": "edit applied", "returncode": 0}
    attachment.observe_action_result(action, native_result)

    assert native_result == {"output": "edit applied", "returncode": 0}
    assert attachment.attempt_runtime.work_state.edited_files[-1] == "src/a.py"


def _observe_test(
    attachment,
    output: str,
    *,
    returncode: int,
    operation: str = "TEST",
) -> rr.CanonicalEvent:
    before_ids = {
        event.event_id
        for event in attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
    }
    canonical_operation = rr.ActionOperation(operation)
    action = {"operation": operation, "command": "pytest -q"}
    native_result = {"output": output, "returncode": returncode}
    native_before = dict(native_result)

    attachment.observe_action_proposal(action)
    attachment.observe_action_result(action, native_result)

    assert native_result == native_before
    new_events = tuple(
        event
        for event in attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
        if event.event_id not in before_ids
        and event.kind is rr.EventKind.ACTION_RESULT
        and event.action is not None
        and event.action.operation is canonical_operation
    )
    assert len(new_events) == 1
    return new_events[0]


def _observe_typed_environment_failure(
    attachment,
) -> rr.CanonicalEvent:
    runtime = attachment.attempt_runtime
    prior = runtime.journal.events(runtime.attempt_id)
    previous_hash = prior[-1].content_hash if prior else ""
    revision = runtime.work_state.revision
    sequence = runtime.work_state.sequence + 1
    event = rr.CanonicalEvent(
        event_id=f"{runtime.attempt_id}:typed-env-failure:{sequence}",
        attempt_id=runtime.attempt_id,
        sequence=sequence,
        kind=rr.EventKind.OBSERVATION_COMMITTED,
        authority=rr.Authority.STRUCTURED,
        outcomes=(
            rr.SemanticOutcome(
                kind=rr.SemanticKind.TEST_RESULT,
                status="env_fail",
                authority=rr.Authority.STRUCTURED,
                provenance=("test_protocol",),
            ),
            rr.SemanticOutcome(
                kind=rr.SemanticKind.TEST_ENV_FAIL,
                status="env_fail",
                failure_fingerprint="fixture-environment-failure",
                authority=rr.Authority.STRUCTURED,
                provenance=("test_protocol",),
            ),
        ),
        revision_before=revision,
        revision_after=revision,
        previous_event_hash=previous_hash,
        model_turn_id=f"{runtime.attempt_id}:typed-env-model:{sequence}",
        observation_id=f"{runtime.attempt_id}:typed-env-observation:{sequence}",
        carrier="test",
    )
    runtime.append_event(event)
    return event


def _recovery_records(attachment) -> tuple[rr.EvidenceRecord, ...]:
    return tuple(
        record
        for record in attachment.attempt_runtime._evidence.values()
        if record.feature_id == "recovery"
    )


def _recovery_compilations(
    attachment,
    records: tuple[rr.EvidenceRecord, ...],
) -> tuple[rr.CapsuleCompilation, ...]:
    ids = {record.evidence_id for record in records}
    return tuple(
        compilation
        for compilation in attachment.attempt_runtime._compilations.values()
        if ids.intersection(compilation.evidence_ids)
    )


def test_executed_no_tests_is_a_typed_canonical_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-typed-boundary",
    )
    try:
        event = _observe_test(
            attachment,
            ZERO_TEST_OUTPUT,
            returncode=0,
        )

        expected_kind = rr.SemanticKind.TEST_EXECUTED_NO_TESTS
        assert event.result is not None
        assert event.result.status == "executed_no_tests"
        assert tuple(outcome.kind for outcome in event.outcomes) == (
            expected_kind,
        )
        assert event.outcomes[0].status == "executed_no_tests"
        assert attachment.attempt_runtime.work_state.test_count == 0

        state = attachment.attempt_runtime.work_state
        assert rr.WorkState.from_json(state.canonical_json()) == state

        tool_event = miniswe.normalize_event(
            "pytest -q",
            ZERO_TEST_OUTPUT,
            0,
            1,
        )
        assert tool_event.semantic_events == ("test_executed_no_tests",)
        monkeypatch.setenv("GT_GATEWAY", "1")
        assert gateway.augment(
            tool_event,
            gateway.GatewayState(
                graph_db=str(tmp_path / "absent-graph.db"),
                repo_root=str(tmp_path),
                canonical_revision=state.revision,
            ),
        ) == []
        assert tool_event.semantic_events == ("test_executed_no_tests",)
        assert tool_event.primary_boundary == "test_executed_no_tests"
        parity_event = miniswe.canonicalize_tool_event(
            tool_event,
            event_id="parity-no-tests",
            attempt_id="parity-attempt",
            sequence=1,
            action_id="parity-action",
            model_turn_id="parity-model",
            observation_id="parity-observation",
            revision_before=state.revision,
            revision_after=state.revision,
            previous_event_hash="",
        )
        assert tuple(outcome.kind for outcome in parity_event.outcomes) == (
            expected_kind,
        )
        assert rr.SemanticKind.TEST_RESULT not in {
            outcome.kind for outcome in parity_event.outcomes
        }
    finally:
        attachment.attempt_runtime.journal.close()


def test_first_boundary_is_quiet_second_produces_one_recovery_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-threshold",
    )
    try:
        _observe_source_edit(
            attachment,
            source,
            before="value = 1\n",
            after="value = 2\n",
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)

        assert _recovery_records(attachment) == ()
        assert _recovery_compilations(attachment, ()) == ()

        second = _observe_test(
            attachment,
            ZERO_TEST_OUTPUT,
            returncode=0,
        )
        records = _recovery_records(attachment)
        compilations = _recovery_compilations(attachment, records)

        assert len(records) == 1
        assert records[0].decision_context is rr.DecisionContext.FAILURE_RECOVERY
        assert records[0].roles == (
            rr.EvidenceRole.CONTRADICTION,
            rr.EvidenceRole.VALIDATION,
        )
        assert records[0].subject == "src/a.py"
        assert len(compilations) == 1
        assert compilations[0].state is rr.CapsuleCompilationState.COMPILED
        assert compilations[0].evidence_ids == (records[0].evidence_id,)
        assert compilations[0].source_model_call_id == second.model_turn_id
    finally:
        attachment.attempt_runtime.journal.close()


@pytest.mark.parametrize(
    ("disqualifying_output", "returncode", "operation", "expected_kind"),
    (
        (PASS_OUTPUT, 0, "TEST", rr.SemanticKind.TEST_PASS),
        (FAIL_OUTPUT, 1, "TEST", rr.SemanticKind.TEST_FAIL),
        (ENV_FAIL_OUTPUT, 1, "ENV", rr.SemanticKind.TEST_ENV_FAIL),
        (COMPILE_FAIL_OUTPUT, 1, "COMPILE", rr.SemanticKind.COMPILE_RESULT),
    ),
)
def test_observed_validation_boundary_resets_the_consecutive_streak(
    tmp_path: Path,
    monkeypatch,
    disqualifying_output: str,
    returncode: int,
    operation: str,
    expected_kind: rr.SemanticKind,
) -> None:
    attachment, source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-reset",
    )
    try:
        _observe_source_edit(
            attachment,
            source,
            before="value = 1\n",
            after="value = 2\n",
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        disqualifying_event = (
            _observe_typed_environment_failure(attachment)
            if operation == "ENV"
            else _observe_test(
                attachment,
                disqualifying_output,
                returncode=returncode,
                operation=operation,
            )
        )
        assert expected_kind in tuple(
            outcome.kind for outcome in disqualifying_event.outcomes
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)

        # A stale streak would fire here. The post-reset zero is a new first boundary.
        assert _recovery_records(attachment) == ()

        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        assert len(_recovery_records(attachment)) == 1
    finally:
        attachment.attempt_runtime.journal.close()


def test_real_collection_environment_failure_resets_instead_of_incrementing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-live-env-reset",
    )
    try:
        _observe_source_edit(
            attachment,
            source,
            before="value = 1\n",
            after="value = 2\n",
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        env_failure = _observe_test(
            attachment,
            COLLECTION_ENV_FAIL_OUTPUT,
            returncode=2,
        )

        assert tuple(outcome.kind for outcome in env_failure.outcomes) == (
            rr.SemanticKind.TEST_RESULT,
            rr.SemanticKind.TEST_ENV_FAIL,
        )
        assert (
            attachment.attempt_runtime.work_state.consecutive_no_test_results
            == 0
        )

        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        assert _recovery_records(attachment) == ()
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        assert len(_recovery_records(attachment)) == 1
    finally:
        attachment.attempt_runtime.journal.close()


def test_pre_edit_zero_does_not_count_toward_post_edit_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-edit-epoch",
    )
    try:
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_source_edit(
            attachment,
            source,
            before="value = 1\n",
            after="value = 2\n",
        )
        first_post_edit = _observe_test(
            attachment,
            ZERO_TEST_OUTPUT,
            returncode=0,
        )

        assert _recovery_records(attachment) == ()
        assert (
            attachment.attempt_runtime.work_state.no_test_recovery_event_id
            == ""
        )
        assert (
            attachment.attempt_runtime.work_state.consecutive_no_test_results
            == 1
        )

        second_post_edit = _observe_test(
            attachment,
            ZERO_TEST_OUTPUT,
            returncode=0,
        )
        records = _recovery_records(attachment)
        assert len(records) == 1
        assert (
            attachment.attempt_runtime.work_state.no_test_recovery_event_id
            == second_post_edit.event_id
        )
        assert records[0].decision_context is rr.DecisionContext.FAILURE_RECOVERY
        assert first_post_edit.event_id != second_post_edit.event_id
    finally:
        attachment.attempt_runtime.journal.close()


def test_repeated_zero_test_boundaries_without_a_source_edit_stay_quiet(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-without-edit",
    )
    try:
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)

        assert _recovery_records(attachment) == ()
        assert _recovery_compilations(attachment, ()) == ()
    finally:
        attachment.attempt_runtime.journal.close()


def test_recovery_is_single_dose_across_later_boundaries_and_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, source = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-no-test-single-dose",
    )
    try:
        _observe_source_edit(
            attachment,
            source,
            before="value = 1\n",
            after="value = 2\n",
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        first_records = _recovery_records(attachment)
        first_compilations = _recovery_compilations(
            attachment,
            first_records,
        )
        assert len(first_records) == 1
        assert len(first_compilations) == 1

        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_source_edit(
            attachment,
            source,
            before="value = 2\n",
            after="value = 3\n",
        )
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)
        _observe_test(attachment, ZERO_TEST_OUTPUT, returncode=0)

        assert tuple(
            record.evidence_id for record in _recovery_records(attachment)
        ) == tuple(record.evidence_id for record in first_records)
        assert tuple(
            compilation.capsule_hash
            for compilation in _recovery_compilations(
                attachment,
                _recovery_records(attachment),
            )
        ) == tuple(
            compilation.capsule_hash
            for compilation in first_compilations
        )
    finally:
        attachment.attempt_runtime.journal.close()
