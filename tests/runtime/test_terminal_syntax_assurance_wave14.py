from __future__ import annotations

from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.commitment_control import CommitmentDecision


class _Model:
    def _prepare_messages_for_api(self, messages):
        return messages

    def _query(self, messages, **kwargs):
        return SimpleNamespace(id="", status="failed", choices=[])


class _Agent:
    def __init__(self) -> None:
        self.executed = 0

    def add_messages(self, *messages):
        return list(messages)

    def execute_actions(self, message):
        self.executed += 1
        return [{"output": "native submit executed"}]


@pytest.mark.parametrize(
    (
        "submit_refusal_on",
        "verify_execute_on",
        "certificate_on",
        "expected_owner",
    ),
    (
        (True, True, False, "GT_SS_SUBMIT_RED"),
        (False, False, True, "GT_CERT_DELIVERY"),
    ),
)
def test_canonical_syntax_failure_withholds_clean_terminal_assurance(
    tmp_path,
    monkeypatch,
    submit_refusal_on: bool,
    verify_execute_on: bool,
    certificate_on: bool,
    expected_owner: str,
) -> None:
    """A parser-confirmed failure is terminal evidence without covering RED."""

    for name, enabled in (
        ("GT_SS_SUBMIT_RED", submit_refusal_on),
        ("GT_VERIFY_EXECUTE", verify_execute_on),
        ("GT_CERT_DELIVERY", certificate_on),
    ):
        if enabled:
            monkeypatch.setenv(name, "1")
        else:
            monkeypatch.delenv(name, raising=False)

    source = tmp_path / "src" / "session.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def refresh_session():\n    return None\n",
        encoding="utf-8",
    )
    agent = _Agent()
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=agent,
        env={
            "GT_ATTEMPT_ID": "attempt-terminal-syntax-assurance",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="repair refresh_session",
    )
    assert attachment.attached is True

    view_action = {"command": "view", "path": "src/session.py"}
    attachment.observe_action_proposal(view_action)
    attachment.observe_action_result(
        view_action,
        {
            "output": source.read_text(encoding="utf-8"),
            "returncode": 0,
        },
    )

    syntax_result = {
        "verdict": "syntax_error",
        "reason": "parse_error",
        "language": ".py",
        "checker": ["ast.parse"],
        "diagnostic": (
            'File "src/session.py", line 2\n'
            "    return (\n"
            "           ^\n"
            "SyntaxError: '(' was never closed"
        ),
    }
    validation_event = attachment._append_internal_validation_event(
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
    assert validation_event is not None
    attachment.last_syntax_result = syntax_result
    assert attachment.last_covering_result is None
    assert attachment.last_native_test_outcome == ""
    assert attachment.attempt_runtime.work_state.compile_count == 1

    result = agent.execute_actions(
        {
            "extra": {
                "response": {"id": "provider-call-syntax-submit"},
                "actions": [
                    {
                        "operation": "SUBMIT",
                        "command": "submit",
                    }
                ],
            }
        }
    )

    assert result == []
    assert agent.executed == 0
    assert attachment.commitment_boundary.plans[-1].decision is (
        CommitmentDecision.FRESH_INFERENCE
    )
    assert (
        attachment.commitment_boundary.plans[-1].gt_certificate_allowed
        is False
    )
    records = tuple(attachment.attempt_runtime._evidence.values())
    assert not any(item.feature_id == "covering_red" for item in records)
    refusal = next(
        item for item in records if item.feature_id == "submit_refusal"
    )
    assert refusal.owner_feature_ids == (expected_owner,)
    assert refusal.grade is rr.EvidenceGrade.VERIFIED
    assert refusal.mandatory_reason is rr.MandatoryReason.BLOCKER
    assert refusal.claim == "Completion is refused because hygiene."
    assert refusal.lifecycle is rr.EvidenceLifecycle.RELEASED
    assert attachment.provider_boundary._active is not None
    attachment.attempt_runtime.journal.close()
