"""C17: canonical validation truth must distinguish pass from zero executed tests."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters.miniswe import normalize_event
from groundtruth.runtime.commitment_control import (
    CommitmentDecision,
    decide_commitment_control,
)
from groundtruth.runtime.patterns import (
    TEST_FAIL_RE,
    TEST_NO_TESTS_RE,
    TEST_PASS_RE,
    classify_test_observation,
)


ZERO_TEST_OUTPUT = (
    "============================= test session starts =============================\n"
    "collected 0 items\n\n"
    "============================ no tests ran in 0.01s ============================\n"
)
PASS_OUTPUT = (
    "============================= test session starts =============================\n"
    "collected 1 item\n\n"
    "tests/test_widget.py .                                                [100%]\n\n"
    "============================== 1 passed in 0.01s ==============================\n"
)
UNOBSERVED_TEST_OUTPUT = (
    "============================= test session starts =============================\n"
    "collecting ...\n"
)


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


def _inline_fallback_classifier():
    """Execute the seam's ImportError fallback in isolation."""
    tree = ast.parse(Path(seam.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not (
                isinstance(handler.type, ast.Name)
                and handler.type.id == "ImportError"
            ):
                continue
            if not any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "_classify_test_observation"
                for item in handler.body
            ):
                continue
            namespace = {"re": re}
            module = ast.fix_missing_locations(ast.Module(body=handler.body))
            exec(compile(module, seam.__file__, "exec"), namespace)
            assert namespace["_TEST_FAIL_RE"].pattern == TEST_FAIL_RE.pattern
            assert namespace["_TEST_FAIL_RE"].flags == TEST_FAIL_RE.flags
            assert namespace["_TEST_NO_TESTS_RE"].pattern == TEST_NO_TESTS_RE.pattern
            assert namespace["_TEST_NO_TESTS_RE"].flags == TEST_NO_TESTS_RE.flags
            assert namespace["_TEST_PASS_RE"].pattern == TEST_PASS_RE.pattern
            assert namespace["_TEST_PASS_RE"].flags == TEST_PASS_RE.flags
            return namespace["_classify_test_observation"]
    raise AssertionError("inline test-observation fallback not found")


@pytest.fixture(params=("product", "inline_fallback"))
def classifier(request):
    if request.param == "product":
        return classify_test_observation
    return _inline_fallback_classifier()


def _attachment(tmp_path, monkeypatch, attempt_id: str):
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": attempt_id,
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="repair the widget and verify the change",
    )
    assert attachment.attached is True
    return attachment


def _observe_native_test(attachment, output: str, *, returncode: int = 0):
    action = {"operation": "TEST", "command": "pytest -q"}
    native_result = {"output": output, "returncode": returncode}
    native_before = dict(native_result)
    attachment.observe_action_proposal(action)
    attachment.observe_action_result(action, native_result)
    assert native_result == native_before
    events = attachment.attempt_runtime.journal.events(
        attachment.attempt_runtime.attempt_id
    )
    return events[-1]


def _submit_control(attachment):
    context = attachment._commitment_context(
        {
            "extra": {
                "response": {"id": "provider-submit"},
                "actions": [{"operation": "SUBMIT", "command": "submit"}],
            }
        }
    )
    return context, decide_commitment_control(context)


def test_normalization_names_zero_collection_as_non_success_truth() -> None:
    event = normalize_event("pytest -q", ZERO_TEST_OUTPUT, 0, 1)

    assert event.test_protocol == "command"
    assert event.test_outcome == "executed_no_tests"
    assert event.test_outcome not in {"pass", "success"}


def test_zero_total_summary_takes_precedence_over_zero_passed_text() -> None:
    event = normalize_event(
        "npm test",
        "Tests: 0 passed, 0 total\n",
        0,
        1,
    )

    assert event.test_protocol == "command"
    assert event.test_outcome == "executed_no_tests"


@pytest.mark.parametrize(
    ("command", "output"),
    (
        ("npm test", "0 passing (2ms)\n"),
        ("npx mocha", "0 passing (2ms)\n"),
        (
            "mvn test",
            "Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\n"
            "BUILD SUCCESS\n",
        ),
        ("ctest", "Test project /repo/build\nNo tests were found!!!\n"),
        ("phpunit", "PHPUnit 10.5.0\nNo tests executed!\n"),
        (
            "mvn test",
            "[INFO] Tests are skipped.\n[INFO] BUILD SUCCESS\n",
        ),
        (
            "go test ./...",
            "testing: warning: no tests to run\n"
            "PASS\n"
            "ok  example/pkg  0.003s [no tests to run]\n",
        ),
        (
            "npm test",
            "No tests found, exiting with code 0\nTests: 0 total\n",
        ),
        (
            "npx vitest",
            "No test files found, exiting with code 1\n",
        ),
        ("rspec", "0 examples, 0 failures\n"),
    ),
)
def test_numeric_zero_execution_summaries_are_not_passes(
    classifier,
    command,
    output,
) -> None:
    outcome, protocol = classifier(command, output)

    assert protocol == "command"
    assert outcome == "executed_no_tests"


@pytest.mark.parametrize(
    ("command", "output"),
    (
        ("pytest -q", "2 passed, 0 failed in 0.02s\n"),
        ("npx mocha", "2 passing (4ms)\n0 failing\n"),
        ("npm test", "Tests: 2 passed, 0 failed, 2 total\n"),
    ),
)
def test_zero_failure_counts_do_not_override_positive_execution(
    classifier,
    command,
    output,
) -> None:
    outcome, protocol = classifier(command, output)

    assert protocol == "command"
    assert outcome == "pass"


@pytest.mark.parametrize("returncode", (1, 2))
def test_known_nonzero_exit_cannot_become_pass_from_positive_summary(
    classifier,
    returncode,
) -> None:
    outcome, protocol = classifier(
        "pytest -q",
        "2 passed in 0.02s\n",
        returncode,
    )

    assert protocol == "command"
    assert outcome == ""


def test_collection_environment_error_precedes_zero_collection(
    classifier,
) -> None:
    outcome, protocol = classifier(
        "pytest -q",
        "collected 0 items / 1 error\n"
        "ERROR collecting tests/test_widget.py\n"
        "ModuleNotFoundError: No module named 'fixture_dependency'\n",
        2,
    )

    assert protocol == "command"
    assert outcome == "env_fail"


def test_known_clean_pass_is_not_overridden_by_harmless_env_token(
    classifier,
) -> None:
    outcome, protocol = classifier(
        "pytest -q",
        "tests/test_widget.py::test_formats_missing_import PASSED\n"
        "captured example: ModuleNotFoundError: No module named 'optional'\n"
        "1 passed in 0.02s\n",
        0,
    )

    assert protocol == "command"
    assert outcome == "pass"


def test_zero_collection_cannot_become_pass_count_or_clean_certificate(
    tmp_path,
    monkeypatch,
) -> None:
    attachment = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-c17-zero-tests",
    )
    try:
        result_event = _observe_native_test(attachment, ZERO_TEST_OUTPUT)
        context, plan = _submit_control(attachment)

        assert result_event.result is not None
        actual = (
            result_event.result.status,
            tuple(outcome.kind for outcome in result_event.outcomes),
            attachment.last_native_test_outcome,
            attachment.attempt_runtime.work_state.test_count,
            context.certificate_requirements_met,
            plan.gt_certificate_allowed,
            plan.decision,
        )
        expected = (
            "executed_no_tests",
            tuple(
                outcome.kind
                for outcome in result_event.outcomes
                if outcome.kind is not rr.SemanticKind.TEST_PASS
            ),
            "executed_no_tests",
            0,
            False,
            False,
            CommitmentDecision.BLOCK_CERTIFICATE,
        )
        assert actual == expected
    finally:
        attachment.attempt_runtime.journal.close()


@pytest.mark.parametrize("returncode", (1, 2))
def test_nonzero_positive_summary_cannot_authorize_clean_certificate(
    tmp_path,
    monkeypatch,
    returncode,
) -> None:
    attachment = _attachment(
        tmp_path,
        monkeypatch,
        f"attempt-c17-nonzero-positive-pass-{returncode}",
    )
    try:
        result_event = _observe_native_test(
            attachment,
            "2 passed in 0.02s\n",
            returncode=returncode,
        )
        context, plan = _submit_control(attachment)

        assert result_event.result is not None
        assert result_event.result.status == "unobserved"
        assert result_event.result.exit_code == returncode
        assert result_event.outcomes == ()
        assert attachment.last_native_test_outcome == "unobserved"
        assert attachment.attempt_runtime.work_state.test_count == 0
        assert context.certificate_requirements_met is False
        assert plan.gt_certificate_allowed is False
        assert plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    finally:
        attachment.attempt_runtime.journal.close()


def test_unobserved_runner_result_cannot_borrow_success_from_exit_zero(
    tmp_path,
    monkeypatch,
) -> None:
    attachment = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-c17-unobserved-test",
    )
    try:
        result_event = _observe_native_test(
            attachment,
            UNOBSERVED_TEST_OUTPUT,
        )
        context, plan = _submit_control(attachment)

        assert result_event.result is not None
        assert result_event.result.status == "unobserved"
        assert result_event.outcomes == ()
        assert attachment.last_native_test_outcome == "unobserved"
        assert attachment.attempt_runtime.work_state.test_count == 0
        assert context.certificate_requirements_met is False
        assert plan.gt_certificate_allowed is False
        assert plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    finally:
        attachment.attempt_runtime.journal.close()


def test_unclassified_nonzero_process_exit_cannot_fabricate_test_failure(
    tmp_path,
    monkeypatch,
) -> None:
    attachment = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-c17-unobserved-process-error",
    )
    try:
        result_event = _observe_native_test(
            attachment,
            "pytest terminated before reporting a test verdict\n",
            returncode=2,
        )
        context, plan = _submit_control(attachment)

        assert result_event.result is not None
        assert result_event.result.status == "unobserved"
        assert result_event.result.exit_code == 2
        assert result_event.outcomes == ()
        assert attachment.last_native_test_outcome == "unobserved"
        assert attachment.attempt_runtime.work_state.test_count == 0
        assert context.certificate_requirements_met is False
        assert plan.gt_certificate_allowed is False
        assert plan.decision is CommitmentDecision.BLOCK_CERTIFICATE
    finally:
        attachment.attempt_runtime.journal.close()


def test_observed_pass_remains_successful_and_preserves_native_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    attachment = _attachment(
        tmp_path,
        monkeypatch,
        "attempt-c17-real-pass",
    )
    try:
        result_event = _observe_native_test(attachment, PASS_OUTPUT)

        assert result_event.result is not None
        assert result_event.result.status == "pass"
        assert any(
            outcome.kind is rr.SemanticKind.TEST_PASS
            for outcome in result_event.outcomes
        )
        assert attachment.last_native_test_outcome == "pass"
        assert attachment.attempt_runtime.work_state.test_count == 1

        context, plan = _submit_control(attachment)
        assert context.certificate_requirements_met is True
        assert plan.gt_certificate_allowed is True
        assert plan.decision is CommitmentDecision.ALLOW
    finally:
        attachment.attempt_runtime.journal.close()
