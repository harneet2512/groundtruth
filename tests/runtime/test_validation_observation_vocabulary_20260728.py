"""C19 RED: GT is blind to every validation signal that is not a formal runner.

Observed live in run 30390877219, task ``aws-cloudformation__cfn-lint-3764``: the
agent ran a plain ``python -c`` reproduction, WATCHED IT FAIL, talked itself out
of the failure, and submitted. The hidden tests then failed on exactly that
scenario. GT had seen a green ``pytest`` earlier and nothing since, because
``classify_test_observation`` only recognises an allow-listed runner in the
COMMAND (``TEST_RUNNER_RE``) or a Rust result frame in the OUTPUT
(``TEST_PROTOCOL_RE``). Everything downstream — including the SS-2 submit-RED
latch — is gated on that classification, so the disconfirming evidence was
invisible.

This module pins the blindness (``test_red_*``), then pins the vocabulary that
fixes it, and — the load-bearing half — pins everything the new classifier must
DELIBERATELY REFUSE to call validation. Correct-or-quiet: a classifier that
fires on a non-validation command is worse than one that misses.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime.patterns import (
    ValidationKind,
    classify_test_observation,
    classify_validation_observation,
)


# --------------------------------------------------------------------------- #
# The live bytes. Trimmed from the cfn-lint-3764 trajectory.
# --------------------------------------------------------------------------- #
CFN_PROBE_CMD = (
    """python -c "from cfnlint import api; print('Matches:', """
    """api.lint_all(open('tpl.yaml').read()))\""""
)
CFN_PROBE_OUT = (
    "Matches: [[E0001: Error found when parsing template: "
    "Fn::ForEach could not be resolved]]\n"
    "Traceback (most recent call last):\n"
    '  File "<string>", line 1, in <module>\n'
    '  File "/testbed/src/cfnlint/api.py", line 61, in lint_all\n'
    "    return [m.message for m in matches]\n"
    "AttributeError: 'list' object has no attribute 'message'\n"
)

REPRO_SCRIPT_CMD = "python repro.py"
REPRO_SCRIPT_OUT = (
    "Traceback (most recent call last):\n"
    '  File "repro.py", line 12, in <module>\n'
    "    assert resolved == expected\n"
    "AssertionError\n"
)

COMPILER_CMD = "go build ./..."
COMPILER_OUT = (
    "# example.com/pkg/lint\n"
    "pkg/lint/rules.go:88:14: undefined: ResolveForEach\n"
)


# --------------------------------------------------------------------------- #
# RED — the shipped classifier is silent on all three.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    (
        pytest.param(CFN_PROBE_CMD, CFN_PROBE_OUT, 1, id="runtime_probe"),
        pytest.param(REPRO_SCRIPT_CMD, REPRO_SCRIPT_OUT, 1, id="ad_hoc_repro"),
        pytest.param(COMPILER_CMD, COMPILER_OUT, 1, id="compiler_check"),
    ),
)
def test_red_formal_runner_classifier_is_blind_to_real_disconfirmation(
    command: str,
    output: str,
    returncode: int,
) -> None:
    """Documents the defect: authoritative failing evidence classifies as nothing."""
    assert classify_test_observation(command, output, returncode) == ("", "")


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    (
        pytest.param(CFN_PROBE_CMD, CFN_PROBE_OUT, 1, id="runtime_probe"),
        pytest.param(REPRO_SCRIPT_CMD, REPRO_SCRIPT_OUT, 1, id="ad_hoc_repro"),
        pytest.param(COMPILER_CMD, COMPILER_OUT, 1, id="compiler_check"),
    ),
)
def test_validation_classifier_sees_the_failure_the_test_classifier_missed(
    command: str,
    output: str,
    returncode: int,
) -> None:
    observation = classify_validation_observation(command, output, returncode)

    assert observation.kind is not ValidationKind.NONE
    assert observation.outcome == "fail"
    assert bool(observation) is True


# --------------------------------------------------------------------------- #
# The vocabulary.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "output", "returncode", "kind"),
    (
        pytest.param(
            "pytest -q",
            "1 passed in 0.01s\n",
            0,
            ValidationKind.FORMAL_TEST,
            id="formal-pytest",
        ),
        pytest.param(
            "python -m pytest tests/test_api.py::test_for_each",
            "1 failed in 0.01s\n",
            1,
            ValidationKind.FOCUSED_TEST,
            id="focused-nodeid",
        ),
        pytest.param(
            "pytest -k for_each",
            "1 failed in 0.01s\n",
            1,
            ValidationKind.FOCUSED_TEST,
            id="focused-dash-k",
        ),
        pytest.param(
            "go test ./pkg -run TestResolveForEach",
            "--- FAIL: TestResolveForEach\nFAIL\n",
            1,
            ValidationKind.FOCUSED_TEST,
            id="focused-go-run",
        ),
        pytest.param(
            "python -m unittest tests.test_api.ApiCase.test_for_each",
            "FAILED (failures=1)\n",
            1,
            ValidationKind.FOCUSED_TEST,
            id="focused-unittest-dotted",
        ),
        pytest.param(
            REPRO_SCRIPT_CMD,
            REPRO_SCRIPT_OUT,
            1,
            ValidationKind.AD_HOC_REPRO,
            id="adhoc-python",
        ),
        pytest.param(
            "node scripts/repro.js",
            "AssertionError [ERR_ASSERTION]\n",
            1,
            ValidationKind.AD_HOC_REPRO,
            id="adhoc-node",
        ),
        pytest.param(
            CFN_PROBE_CMD,
            CFN_PROBE_OUT,
            1,
            ValidationKind.RUNTIME_PROBE,
            id="probe-python-dash-c",
        ),
        pytest.param(
            """node -e "require('./lib').run()\"""",
            "TypeError: run is not a function\n",
            1,
            ValidationKind.RUNTIME_PROBE,
            id="probe-node-dash-e",
        ),
        pytest.param(
            COMPILER_CMD,
            COMPILER_OUT,
            1,
            ValidationKind.COMPILER_CHECK,
            id="compiler-go-build",
        ),
        pytest.param(
            "npx tsc --noEmit",
            "src/a.ts(3,5): error TS2322: Type 'string' is not assignable.\n",
            2,
            ValidationKind.COMPILER_CHECK,
            id="compiler-tsc-noemit",
        ),
        pytest.param(
            "cargo check",
            "error[E0308]: mismatched types\n",
            101,
            ValidationKind.COMPILER_CHECK,
            id="compiler-cargo-check",
        ),
        pytest.param(
            "mvn -q compile",
            "[ERROR] compilation error\n",
            1,
            ValidationKind.COMPILER_CHECK,
            id="compiler-mvn-compile",
        ),
        pytest.param(
            "make",
            "src/x.c:4:1: error: could not compile\n",
            2,
            ValidationKind.COMPILER_CHECK,
            id="compiler-by-output-only",
        ),
        pytest.param(
            "mypy src/cfnlint",
            "src/cfnlint/api.py:61: error: Missing return\n",
            1,
            ValidationKind.STATIC_CHECK,
            id="static-mypy",
        ),
        pytest.param(
            "ruff check src",
            "src/a.py:1:1: F401 unused import\n",
            1,
            ValidationKind.STATIC_CHECK,
            id="static-ruff",
        ),
        pytest.param(
            "npx eslint src",
            "  3:1  error  Unexpected var\n",
            1,
            ValidationKind.STATIC_CHECK,
            id="static-eslint",
        ),
        pytest.param(
            "golangci-lint run",
            "pkg/x.go:9:2: ineffectual assignment\n",
            1,
            ValidationKind.STATIC_CHECK,
            id="static-golangci",
        ),
        pytest.param(
            "./scripts/check.sh",
            "Traceback (most recent call last):\n"
            '  File "src/cfnlint/api.py", line 61, in lint_all\n'
            "    assert False\n"
            "AssertionError\n",
            1,
            ValidationKind.ASSERTION_SCRIPT,
            id="assertion-script-by-output",
        ),
    ),
)
def test_validation_kind_vocabulary(
    command: str,
    output: str,
    returncode: int,
    kind: ValidationKind,
) -> None:
    assert classify_validation_observation(command, output, returncode).kind is kind


def test_focused_test_reports_its_selector() -> None:
    observation = classify_validation_observation(
        "pytest -k for_each", "1 failed\n", 1
    )

    assert observation.kind is ValidationKind.FOCUSED_TEST
    assert observation.selector == "for_each"


def test_ad_hoc_repro_reports_the_deepest_repo_relative_frame() -> None:
    observation = classify_validation_observation(
        REPRO_SCRIPT_CMD, REPRO_SCRIPT_OUT, 1
    )

    assert observation.frame == "repro.py"


def test_runtime_probe_frame_skips_the_inline_body_pseudo_file() -> None:
    observation = classify_validation_observation(CFN_PROBE_CMD, CFN_PROBE_OUT, 1)

    assert observation.frame == "/testbed/src/cfnlint/api.py"


# --------------------------------------------------------------------------- #
# Correct-or-quiet — everything DELIBERATELY excluded.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    (
        pytest.param("ls -la", "a.py\nb.py\n", 0, id="listing"),
        pytest.param("cat src/api.py", "def lint_all():\n", 0, id="view"),
        pytest.param(
            "grep -rn ForEach src", "src/api.py:61: ForEach\n", 0, id="search"
        ),
        pytest.param("git diff", "+++ b/src/api.py\n", 0, id="git-diff"),
        pytest.param("git status", "modified: src/api.py\n", 0, id="git-status"),
        pytest.param(
            "pip install -e .", "Successfully installed cfn-lint\n", 0, id="install"
        ),
        pytest.param(
            "python -m pip install pytest", "Successfully installed\n", 0, id="pip-m"
        ),
        pytest.param("python manage.py migrate", "OK\n", 0, id="manage-py-nontest"),
        pytest.param("python setup.py build", "running build\n", 0, id="setup-py"),
        pytest.param("python", ">>> \n", 0, id="bare-repl"),
        pytest.param("echo hi", "hi\n", 0, id="echo"),
        pytest.param("cd /testbed", "", 0, id="cd"),
        pytest.param(
            "sed -i 's/a/b/' src/api.py", "", 0, id="edit"
        ),
        pytest.param("submit", "", 0, id="submit"),
    ),
)
def test_non_validation_commands_stay_quiet(
    command: str,
    output: str,
    returncode: int,
) -> None:
    observation = classify_validation_observation(command, output, returncode)

    assert observation.kind is ValidationKind.NONE
    assert observation.outcome == ""
    assert bool(observation) is False


def test_traceback_whose_deepest_frame_is_a_dependency_is_not_agent_validation() -> None:
    """A crash inside site-packages is env truth, not a disconfirmed hypothesis."""
    observation = classify_validation_observation(
        "./tool.sh",
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.11/site-packages/yaml/__init__.py", line 8\n'
        "ModuleNotFoundError: No module named 'yaml'\n",
        1,
    )

    assert observation.kind is ValidationKind.NONE


@pytest.mark.parametrize(
    ("command", "output"),
    (
        pytest.param(
            "cat build.log",
            "src/x.c:4:1: error: could not compile\n",
            id="cat-a-build-log",
        ),
        pytest.param(
            "grep -C3 Traceback app.log",
            "Traceback (most recent call last):\n"
            '  File "src/api.py", line 61, in lint_all\n'
            "AssertionError\n",
            id="grep-a-stored-traceback",
        ),
        pytest.param(
            "git log -p",
            "-    assert False\n" "AssertionError\n" '  File "src/api.py", line 61\n',
            id="git-log-carrying-old-source",
        ),
        pytest.param(
            "tail -50 nohup.out",
            "src/a.ts(3,5): error TS2322: bad\n",
            id="tail-a-log",
        ),
    ),
)
def test_failing_viewer_commands_cannot_borrow_someone_elses_diagnostic(
    command: str,
    output: str,
) -> None:
    """`cat`/`grep`/`git` print text they did not produce — never validation."""
    observation = classify_validation_observation(command, output, 1)

    assert observation.kind is ValidationKind.NONE


def test_a_leading_cd_does_not_suppress_output_driven_classification() -> None:
    """The LAST segment owns the output; a `cd` prefix is not a viewer command."""
    observation = classify_validation_observation(
        "cd /testbed && ./scripts/check.sh",
        "Traceback (most recent call last):\n"
        '  File "src/cfnlint/api.py", line 61, in lint_all\n'
        "AssertionError\n",
        1,
    )

    assert observation.kind is ValidationKind.ASSERTION_SCRIPT
    assert observation.outcome == "fail"


def test_a_repro_segment_still_classifies_after_a_viewer_segment() -> None:
    observation = classify_validation_observation(
        "cat tpl.yaml && python repro.py", REPRO_SCRIPT_OUT, 1
    )

    assert observation.kind is ValidationKind.AD_HOC_REPRO
    assert observation.outcome == "fail"


def test_repo_root_narrows_frame_admission_and_never_widens_it() -> None:
    """The optional checkout path rejects absolute frames from outside the repo."""
    foreign = (
        "Traceback (most recent call last):\n"
        '  File "/other/checkout/a.py", line 1, in <module>\n'
        "AssertionError\n"
    )
    inside = (
        "Traceback (most recent call last):\n"
        '  File "/testbed/a.py", line 1, in <module>\n'
        "AssertionError\n"
    )

    assert (
        classify_validation_observation("./run.sh", foreign, 1).kind
        is ValidationKind.ASSERTION_SCRIPT
    )
    assert (
        classify_validation_observation(
            "./run.sh", foreign, 1, repo_root="/testbed"
        ).kind
        is ValidationKind.NONE
    )
    assert (
        classify_validation_observation(
            "./run.sh", inside, 1, repo_root="/testbed"
        ).kind
        is ValidationKind.ASSERTION_SCRIPT
    )


def test_environment_failure_is_named_env_fail_not_a_disconfirming_result() -> None:
    observation = classify_validation_observation(
        "python repro.py",
        "Traceback (most recent call last):\n"
        '  File "repro.py", line 1, in <module>\n'
        "ModuleNotFoundError: No module named 'cfnlint'\n",
        1,
    )

    assert observation.kind is ValidationKind.AD_HOC_REPRO
    assert observation.outcome == "env_fail"


def test_semantically_wrong_but_clean_exit_is_a_pass_never_a_fail() -> None:
    """GT cannot judge the MEANING of printed output; it judges execution truth."""
    observation = classify_validation_observation(
        CFN_PROBE_CMD,
        "Matches: [[E0001: Fn::ForEach could not be resolved]]\n",
        0,
    )

    assert observation.kind is ValidationKind.RUNTIME_PROBE
    assert observation.outcome == "pass"


def test_known_zero_exit_cannot_be_called_a_failure_from_quoted_markers() -> None:
    observation = classify_validation_observation(
        "python repro.py",
        "printing a sample diagnostic: SyntaxError: invalid syntax\n",
        0,
    )

    assert observation.outcome == "pass"


def test_unknown_returncode_without_markers_stays_unobserved() -> None:
    observation = classify_validation_observation(
        "python repro.py", "reticulating splines\n", None
    )

    assert observation.kind is ValidationKind.AD_HOC_REPRO
    assert observation.outcome == ""


# --------------------------------------------------------------------------- #
# The hard constraint: classify_test_observation is behaviourally frozen.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "output", "returncode", "expected"),
    (
        pytest.param("pytest -q", "1 passed in 0.01s\n", 0, ("pass", "command")),
        pytest.param("pytest -q", "1 failed in 0.01s\n", 1, ("fail", "command")),
        pytest.param(
            "pytest -q", "collected 0 items\n", 0, ("executed_no_tests", "command")
        ),
        pytest.param(
            "pytest -q",
            "ERROR collecting tests/t.py\nModuleNotFoundError: No module named 'x'\n",
            2,
            ("env_fail", "command"),
        ),
        pytest.param(
            "./target/debug/deps/lib-abc",
            "running 2 tests\ntest result: FAILED. 1 passed; 1 failed\n",
            101,
            ("fail", "native"),
        ),
        pytest.param("pytest -q", "collecting ...\n", 0, ("", "command")),
        pytest.param(CFN_PROBE_CMD, CFN_PROBE_OUT, 1, ("", "")),
        pytest.param(REPRO_SCRIPT_CMD, REPRO_SCRIPT_OUT, 1, ("", "")),
        pytest.param(COMPILER_CMD, COMPILER_OUT, 1, ("", "")),
        pytest.param("ls", "a\n", 0, ("", "")),
    ),
)
def test_classify_test_observation_behaviour_is_unchanged(
    command: str,
    output: str,
    returncode: int,
    expected: tuple[str, str],
) -> None:
    assert classify_test_observation(command, output, returncode) == expected


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    (
        pytest.param("pytest -q", "1 passed in 0.01s\n", 0, id="formal-pass"),
        pytest.param("pytest -k x", "1 failed in 0.01s\n", 1, id="focused-fail"),
        pytest.param("pytest -q", "collected 0 items\n", 0, id="zero-tests"),
    ),
)
def test_formal_paths_agree_byte_for_byte_with_the_frozen_classifier(
    command: str,
    output: str,
    returncode: int,
) -> None:
    legacy_outcome, legacy_protocol = classify_test_observation(
        command, output, returncode
    )
    observation = classify_validation_observation(command, output, returncode)

    assert observation.kind in {
        ValidationKind.FORMAL_TEST,
        ValidationKind.FOCUSED_TEST,
    }
    assert (observation.outcome, observation.protocol) == (
        legacy_outcome,
        legacy_protocol,
    )
