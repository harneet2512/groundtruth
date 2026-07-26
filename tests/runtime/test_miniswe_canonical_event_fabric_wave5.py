"""Wave-5 RED contracts for Mini-SWE's two-sided canonical event fabric.

The native carrier is audit data only.  Structured action proposals establish
the open decision before execution; structured results establish outcome truth
after execution.  Neither boundary may recover semantics by reparsing command
or output text.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REV_1 = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)
REV_2 = rr.RevisionVector(
    repository_content="repo-2",
    graph="graph-2",
    lsp="lsp-2",
    runtime_evidence="runtime-2",
)


def _action(
    operation_name: str,
    *,
    action_id: str = "action-17",
    subject: str = "src/auth/session.py",
    query: str = "",
    targets: tuple[str, ...] = (),
    raw_command: str = "opaque-native-carrier",
):
    return rr.CanonicalAction(
        action_id=action_id,
        operation=rr.ActionOperation[operation_name],
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation=operation_name.lower(),
        subject=subject,
        query=query,
        targets=targets,
        raw_command=raw_command,
    )


def _proposal(action, *, sequence: int = 1):
    return miniswe.canonicalize_action_proposal(
        action,
        event_id=f"ev-{sequence}-proposal",
        attempt_id="attempt-wave5",
        sequence=sequence,
        model_turn_id="model-call-11",
        observation_id="obs-10",
        revision=REV_1,
        previous_event_hash="",
    )


def _native_carrier(
    *,
    kind: str = "other",
    command: str = "carrier must not decide semantics",
    output: str = "carrier output must not decide semantics",
) -> ToolEvent:
    return ToolEvent(
        kind=kind,
        carrier_kind=kind,
        command=command,
        output=output,
        exit_status=99,
        semantic_events=(),
        primary_boundary="",
        test_outcome="",
        test_protocol="",
        semantics_authoritative=True,
    )


def _result(
    proposal,
    *,
    status: str = "success",
    exit_code: int | None = 0,
    hit_count: int | None = None,
    files_hit: tuple[str, ...] = (),
    changed_files: tuple[str, ...] = (),
    failure_fingerprint: str = "",
    signature_before: str = "",
    signature_after: str = "",
    revision_after: rr.RevisionVector = REV_1,
    carrier: ToolEvent | None = None,
):
    structured = rr.CanonicalResult(
        status=status,
        exit_code=exit_code,
        changed=(bool(changed_files) if changed_files else None),
        hit_count=hit_count,
        files_hit=files_hit,
        changed_files=changed_files,
        failure_fingerprint=failure_fingerprint,
        signature_before=signature_before,
        signature_after=signature_after,
    )
    return miniswe.canonicalize_tool_result(
        carrier or _native_carrier(),
        proposal=proposal,
        result=structured,
        event_id="ev-2-result",
        sequence=2,
        observation_id="obs-12",
        revision_after=revision_after,
        previous_event_hash=proposal.content_hash,
    )


def test_canonical_schema_has_typed_action_result_and_two_sided_event_kinds() -> None:
    assert {"ACTION_PROPOSED", "ACTION_RESULT"} <= {
        kind.value for kind in rr.EventKind
    }
    assert {
        "SEARCH",
        "VIEW_SOURCE",
        "VIEW_SYMBOL",
        "EDIT",
        "TEST",
        "COMPILE",
        "SIGNATURE_CHANGE",
        "FILE_CREATE",
        "FILE_DELETE",
        "FILE_RENAME",
        "SUBMIT",
    } <= {operation.value for operation in rr.ActionOperation}
    assert {
        "action_id",
        "operation",
        "tool_family",
        "tool_name",
        "structured_operation",
        "subject",
        "query",
        "targets",
        "raw_command",
    } <= {field.name for field in fields(rr.CanonicalAction)}
    assert {
        "status",
        "exit_code",
        "changed",
        "hit_count",
        "files_hit",
        "changed_files",
        "failure_fingerprint",
        "signature_before",
        "signature_after",
    } <= {field.name for field in fields(rr.CanonicalResult)}
    assert {"action", "result"} <= {
        field.name for field in fields(rr.CanonicalEvent)
    }


@pytest.mark.parametrize(
    ("operation_name", "proposal_semantic"),
    (
        ("SEARCH", rr.SemanticKind.SEARCH_REQUESTED),
        ("VIEW_SOURCE", rr.SemanticKind.SOURCE_READ_REQUESTED),
        ("VIEW_SYMBOL", rr.SemanticKind.SOURCE_READ_REQUESTED),
        ("EDIT", rr.SemanticKind.EDIT_PROPOSED),
        ("TEST", rr.SemanticKind.TEST_REQUESTED),
        ("SIGNATURE_CHANGE", rr.SemanticKind.EDIT_PROPOSED),
        ("FILE_CREATE", rr.SemanticKind.EDIT_PROPOSED),
        ("FILE_DELETE", rr.SemanticKind.EDIT_PROPOSED),
        ("FILE_RENAME", rr.SemanticKind.EDIT_PROPOSED),
        ("SUBMIT", rr.SemanticKind.SUBMIT_PROPOSED),
    ),
)
def test_action_proposals_open_the_precommit_boundary(
    operation_name: str,
    proposal_semantic: rr.SemanticKind,
) -> None:
    action = _action(
        operation_name,
        subject=f"subject:{operation_name}",
        query="refreshSession" if operation_name == "SEARCH" else "",
        targets=("src", "lib") if operation_name == "SEARCH" else (),
        raw_command="pytest -q && rg wrong-carrier-shape",
    )

    event = _proposal(action)

    assert event.kind is rr.EventKind.ACTION_PROPOSED
    assert event.action == action
    assert event.result is None
    assert event.revision_before == event.revision_after == REV_1
    assert event.action.subject == f"subject:{operation_name}"
    assert event.action.raw_command == "pytest -q && rg wrong-carrier-shape"
    assert proposal_semantic in {outcome.kind for outcome in event.outcomes}
    assert all(
        outcome.authority is rr.Authority.STRUCTURED
        for outcome in event.outcomes
    )
    assert rr.CausalRef(
        rr.CausalRefKind.MODEL_CALL,
        "model-call-11",
    ) in event.parents
    assert rr.CausalRef(
        rr.CausalRefKind.OBSERVATION,
        "obs-10",
    ) in event.parents


def test_compile_proposal_is_precommit_even_without_a_regex_command_shape() -> None:
    action = _action(
        "COMPILE",
        subject="src/auth/session.py",
        raw_command="totally opaque structured tool invocation",
    )
    event = _proposal(action)

    assert event.kind is rr.EventKind.ACTION_PROPOSED
    assert event.action.operation is rr.ActionOperation.COMPILE
    assert event.action.subject == "src/auth/session.py"
    assert event.authority is rr.Authority.STRUCTURED


@pytest.mark.parametrize(
    ("operation_name", "status", "expected_semantics", "mutates"),
    (
        ("SEARCH", "success", (rr.SemanticKind.SEARCH_RESULT,), False),
        ("VIEW_SOURCE", "success", (rr.SemanticKind.SOURCE_VIEWED,), False),
        ("VIEW_SYMBOL", "success", (rr.SemanticKind.SYMBOL_VIEWED,), False),
        (
            "EDIT",
            "success",
            (rr.SemanticKind.EDIT_EXECUTED, rr.SemanticKind.DIFF_CREATED),
            True,
        ),
        (
            "TEST",
            "fail",
            (rr.SemanticKind.TEST_RESULT, rr.SemanticKind.TEST_FAIL),
            False,
        ),
        ("COMPILE", "fail", (rr.SemanticKind.COMPILE_RESULT,), False),
        (
            "SIGNATURE_CHANGE",
            "success",
            (rr.SemanticKind.EDIT_EXECUTED, rr.SemanticKind.SIGNATURE_CHANGED),
            True,
        ),
        ("FILE_CREATE", "success", (rr.SemanticKind.FILE_CREATED,), True),
        ("FILE_DELETE", "success", (rr.SemanticKind.FILE_DELETED,), True),
        ("FILE_RENAME", "success", (rr.SemanticKind.FILE_RENAMED,), True),
        ("SUBMIT", "accepted", (rr.SemanticKind.SUBMIT_ACCEPTED,), False),
    ),
)
def test_structured_result_maps_every_operation_without_carrier_inference(
    operation_name: str,
    status: str,
    expected_semantics: tuple[rr.SemanticKind, ...],
    mutates: bool,
) -> None:
    if operation_name in {"SEARCH", "VIEW_SYMBOL", "SIGNATURE_CHANGE"}:
        subject = "refreshSession"
    elif operation_name == "TEST":
        subject = "tests/auth/test_session.py"
    elif operation_name in {
        "VIEW_SOURCE",
        "EDIT",
        "COMPILE",
        "FILE_CREATE",
        "FILE_DELETE",
        "FILE_RENAME",
    }:
        subject = "src/auth/session.py"
    else:
        subject = "attempt-wave5"
    action = _action(operation_name, subject=subject)
    proposal = _proposal(action)
    changed_files = (
        ("src/auth/session.py",)
        if mutates
        else ()
    )
    result = _result(
        proposal,
        status=status,
        changed_files=changed_files,
        failure_fingerprint=(
            "failure:auth-session"
            if status == "fail"
            else ""
        ),
        revision_after=REV_2 if mutates else REV_1,
        carrier=_native_carrier(
            kind="search" if operation_name != "SEARCH" else "test",
            command="pytest -q && rg this text deliberately contradicts the action",
            output="99 passed; 88 failed; fake grep hit",
        ),
    )

    assert result.kind is rr.EventKind.ACTION_RESULT
    assert result.action == action
    assert result.result.status == status
    assert tuple(outcome.kind for outcome in result.outcomes) == expected_semantics
    assert all(outcome.subject == subject for outcome in result.outcomes)
    assert result.carrier == (
        "search" if operation_name != "SEARCH" else "test"
    )


def test_submit_blocked_is_a_structured_terminal_result() -> None:
    proposal = _proposal(
        _action(
            "SUBMIT",
            subject="attempt-wave5",
            raw_command="echo this is not a submit command",
        )
    )
    result = _result(
        proposal,
        status="blocked",
        exit_code=1,
        failure_fingerprint="completion-assurance:red-test",
        carrier=_native_carrier(
            kind="view",
            command="sed -n 1p README.md",
            output="submission accepted",
        ),
    )

    assert tuple(outcome.kind for outcome in result.outcomes) == (
        rr.SemanticKind.SUBMIT_BLOCKED,
    )
    assert result.outcomes[0].subject == "attempt-wave5"
    assert (
        result.outcomes[0].failure_fingerprint
        == "completion-assurance:red-test"
    )


def test_search_query_targets_hits_and_files_are_structured_not_reparsed() -> None:
    action = _action(
        "SEARCH",
        subject="refreshSession",
        query="refreshSession",
        targets=("src/auth", "src/routes"),
        raw_command="pytest -q # deliberately not a search command",
    )
    proposal = _proposal(action)
    result = _result(
        proposal,
        hit_count=3,
        files_hit=(
            "src/auth/session.py",
            "src/routes/token.py",
        ),
        carrier=_native_carrier(
            kind="test",
            command="pytest -q",
            output="0 search hits",
        ),
    )

    assert proposal.action.query == "refreshSession"
    assert proposal.action.targets == ("src/auth", "src/routes")
    assert result.result.hit_count == 3
    assert result.result.files_hit == (
        "src/auth/session.py",
        "src/routes/token.py",
    )
    assert result.outcomes == (
        rr.SemanticOutcome(
            kind=rr.SemanticKind.SEARCH_RESULT,
            subject="refreshSession",
            status="success",
            metadata=(
                ("query", "refreshSession"),
                ("hit_count", "3"),
                ("files_hit", "src/auth/session.py|src/routes/token.py"),
            ),
            authority=rr.Authority.STRUCTURED,
            provenance=("canonical_action", "canonical_result"),
        ),
    )


def test_signature_delta_and_file_operation_targets_remain_structured() -> None:
    signature = _proposal(
        _action(
            "SIGNATURE_CHANGE",
            subject="refreshSession",
            targets=("src/auth/session.py",),
            raw_command="opaque-editor-operation",
        )
    )
    signature_result = _result(
        signature,
        changed_files=("src/auth/session.py",),
        signature_before="refreshSession(token) -> Session",
        signature_after="refreshSession(token, store) -> Session",
        revision_after=REV_2,
    )
    assert signature.action.subject == "refreshSession"
    assert signature.action.targets == ("src/auth/session.py",)
    assert signature_result.result.signature_before == (
        "refreshSession(token) -> Session"
    )
    assert signature_result.result.signature_after == (
        "refreshSession(token, store) -> Session"
    )

    rename = _proposal(
        _action(
            "FILE_RENAME",
            subject="src/auth/legacy_session.py",
            targets=("src/auth/session.py",),
            raw_command="opaque-file-operation",
        )
    )
    renamed = _result(
        rename,
        changed_files=(
            "src/auth/legacy_session.py",
            "src/auth/session.py",
        ),
        revision_after=REV_2,
    )
    assert rename.action.subject == "src/auth/legacy_session.py"
    assert rename.action.targets == ("src/auth/session.py",)
    assert renamed.result.changed_files == (
        "src/auth/legacy_session.py",
        "src/auth/session.py",
    )
    assert tuple(outcome.kind for outcome in renamed.outcomes) == (
        rr.SemanticKind.FILE_RENAMED,
    )


@pytest.mark.parametrize(
    ("operation_name", "subject"),
    (
        ("TEST", "tests/auth/test_session.py"),
        ("COMPILE", "src/auth/session.py"),
    ),
)
def test_failure_fingerprint_is_bound_to_structured_test_and_compile_results(
    operation_name: str,
    subject: str,
) -> None:
    proposal = _proposal(_action(operation_name, subject=subject))
    event = _result(
        proposal,
        status="fail",
        exit_code=1,
        failure_fingerprint="sha256:failure-auth-session",
        carrier=_native_carrier(
            kind="view",
            command="sed -n 1p README.md",
            output="all good; no failure",
        ),
    )

    assert event.result.failure_fingerprint == "sha256:failure-auth-session"
    assert any(
        outcome.failure_fingerprint == "sha256:failure-auth-session"
        for outcome in event.outcomes
    )
    assert all(
        outcome.subject == subject
        for outcome in event.outcomes
    )


def test_result_causality_binds_proposal_action_model_call_and_observation() -> None:
    proposal = _proposal(
        _action(
            "EDIT",
            action_id="action-causal",
            subject="src/auth/session.py",
        )
    )
    result = _result(
        proposal,
        changed_files=("src/auth/session.py",),
        revision_after=REV_2,
    )

    assert result.attempt_id == proposal.attempt_id
    assert result.action_id == proposal.action_id == "action-causal"
    assert result.model_turn_id == proposal.model_turn_id == "model-call-11"
    assert result.observation_id == "obs-12"
    assert result.previous_event_hash == proposal.content_hash
    assert result.revision_before == proposal.revision_after
    assert result.revision_after == REV_2
    assert rr.CausalRef(
        rr.CausalRefKind.EVENT,
        proposal.event_id,
    ) in result.parents
    assert rr.CausalRef(
        rr.CausalRefKind.ACTION,
        "action-causal",
    ) in result.parents
    assert rr.CausalRef(
        rr.CausalRefKind.MODEL_CALL,
        "model-call-11",
    ) in result.parents
    assert rr.CausalRef(
        rr.CausalRefKind.OBSERVATION,
        "obs-12",
    ) in result.parents


@pytest.mark.parametrize(
    ("status", "hit_count", "semantic"),
    (
        ("success", 0, rr.SemanticKind.SEARCH_EMPTY),
        ("failed", None, rr.SemanticKind.SEARCH_FAILED),
    ),
)
def test_search_empty_and_failed_come_only_from_structured_result(
    status: str,
    hit_count: int | None,
    semantic: rr.SemanticKind,
) -> None:
    proposal = _proposal(
        _action(
            "SEARCH",
            subject="refreshSession",
            query="refreshSession",
            targets=("src",),
            raw_command="echo not-search",
        )
    )
    result = _result(
        proposal,
        status=status,
        exit_code=1 if status == "failed" else 0,
        hit_count=hit_count,
        carrier=_native_carrier(
            kind="search",
            command='rg "refreshSession" src',
            output=(
                "src/session.py:99:refreshSession"
                if semantic is rr.SemanticKind.SEARCH_EMPTY
                else ""
            ),
        ),
    )

    assert tuple(outcome.kind for outcome in result.outcomes) == (semantic,)
