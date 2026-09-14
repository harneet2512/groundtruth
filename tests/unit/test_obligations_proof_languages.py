"""Per-language subject_bound_test_runner proof coverage.

The runner/pass/fail sets are canonical in runtime.patterns; this matrix
proves each benchmark language's real runner line produces a behavioral
proof, that real failure output for the same language refuses it, and that
path-prefixed / wrapper runners (./mvnw, vendor/bin/phpunit, bundle exec,
node_modules/.bin) bind the same way. This is the channel that was dead on
Java/TS/.NET tasks while the runner regexes drifted narrower than the
governor's own verification set.
"""

import pytest

from groundtruth.runtime.obligations import classify_checked_behavioral_proof
from groundtruth.runtime.patterns import TEST_RUNNER_RE

SUBJECTS = frozenset({"sorting_test"})

GREEN_CASES = [
    (
        "python",
        "pytest sorting_test -q",
        "tests/sorting_test.py::test_asc PASSED\n===== 5 passed in 0.42s =====",
    ),
    (
        "rust",
        "cargo test sorting_test",
        "running 3 tests\ntest sorting_test::asc ... ok\ntest result: ok. 3 passed; 0 failed",
    ),
    ("go", "go test ./sorting_test/...", "ok  \tgithub.com/x/sorting_test\t0.412s"),
    (
        "maven",
        "mvn test -Dtest=SortingTest#sorting_test",
        "[INFO] Running SortingTest.sorting_test\n"
        "[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS",
    ),
    (
        "maven_wrapper",
        "./mvnw test -Dtest=SortingTest#sorting_test",
        "[INFO] Tests run: 5, Failures: 0, Errors: 0\n[INFO] BUILD SUCCESS",
    ),
    (
        "gradle_wrapper",
        "./gradlew test --tests '*sorting_test*'",
        "SortingTest > sorting_test PASSED\nBUILD SUCCESSFUL in 4s",
    ),
    (
        "jest_npx",
        "npx jest sorting_test",
        "Tests:       5 passed, 5 total\n    ✓ sorting_test asc (3 ms)",
    ),
    (
        "vitest_bin",
        "node_modules/.bin/vitest run sorting_test",
        " ✓ sorting_test.spec.ts (5)\n Test Files  1 passed\n      Tests  5 passed",
    ),
    (
        "dotnet",
        "dotnet test --filter sorting_test",
        "Passed!  - Failed: 0, Passed: 5, Skipped: 0\n  sorting_test asc",
    ),
    ("deno", "deno test sorting_test.ts", "sorting_test ... ok (12ms)\nok | 5 passed | 0 failed"),
    (
        "phpunit_vendor",
        "vendor/bin/phpunit --filter sorting_test",
        "sorting_test\nOK (5 tests, 5 assertions)",
    ),
    (
        "rspec_bundle",
        "bundle exec rspec spec/sorting_test_spec.rb",
        "sorting_test\n5 examples, 0 failures",
    ),
    (
        "sbt",
        "sbt 'testOnly *sorting_test*'",
        "[info] + sorting_test asc\n[info] Passed: Total 5, Failed 0\n[success]",
    ),
]

RED_CASES = [
    ("python_partial", "pytest sorting_test -q", "===== 2 failed, 3 passed in 0.4s ====="),
    ("python_errors", "pytest sorting_test -q", "===== 5 passed, 2 errors in 0.4s ====="),
    ("rust", "cargo test sorting_test", "test result: FAILED. 2 passed; 1 failed; 0 ignored"),
    (
        "go",
        "go test ./sorting_test/...",
        "--- FAIL: TestSorting_test (0.01s)\nFAIL\tgithub.com/x/sorting_test\t0.412s",
    ),
    (
        "go_mixed_pkg",
        "go test ./...",
        "ok  \tgithub.com/x/other\t0.1s\n--- FAIL: TestSorting_test (0.01s)\nFAIL\tgithub.com/x/sorting_test\t0.4s",
    ),
    (
        "maven",
        "mvn test -Dtest=SortingTest#sorting_test",
        "Tests run: 5, Failures: 2, Errors: 0\n[ERROR] BUILD FAILURE",
    ),
    (
        "dotnet",
        "dotnet test --filter sorting_test",
        "Failed!  - Failed: 2, Passed: 3\n  sorting_test asc",
    ),
    (
        "jest",
        "npx jest sorting_test",
        "Tests:       1 failed, 4 passed, 5 total\n    ✕ sorting_test asc",
    ),
    ("nonzero_rc", "pytest sorting_test -q", "===== 5 passed in 0.42s ====="),
]


@pytest.mark.parametrize("lang,command,output", GREEN_CASES, ids=[c[0] for c in GREEN_CASES])
def test_passing_runner_output_proves(lang, command, output):
    proof = classify_checked_behavioral_proof(command, output, 0, SUBJECTS, turn=1)
    assert proof is not None, f"{lang}: green runner output produced no proof"
    assert proof.kind == "subject_bound_test_runner"


@pytest.mark.parametrize("lang,command,output", RED_CASES, ids=[c[0] for c in RED_CASES])
def test_failing_or_untrusted_output_refuses(lang, command, output):
    rc = 0 if lang != "nonzero_rc" else 1
    assert classify_checked_behavioral_proof(command, output, rc, SUBJECTS, turn=1) is None


def test_runner_pattern_recognizes_repo_local_and_wrapper_forms():
    """./mvnw, vendor/bin, node_modules/.bin and bundle exec all bind."""
    for command in (
        "./mvnw test",
        "mvnw test",
        "mvn test",
        "gradle test",
        "./gradlew test",
        "vendor/bin/phpunit",
        "node_modules/.bin/jest",
        "bundle exec rspec",
        "sbt test",
        "mix test",
        "dotnet test",
        "bazel test //...",
        "nx test",
        "turbo test",
        "composer test",
    ):
        assert TEST_RUNNER_RE.match(command), command


def test_runner_pattern_rejects_non_test_commands():
    for command in (
        "mvn package",
        "gradle build",
        "dotnet build",
        "sbt compile",
        "mix compile",
        "composer install",
        "turbo build",
        "nx build",
        "npm run build",
        "go build ./...",
        "cargo build",
        "cat test.log",
    ):
        assert not TEST_RUNNER_RE.match(command), command


def test_every_promotable_language_server_has_a_readiness_budget():
    """The _LANGUAGE_SERVERS ↔ _READY_BUDGET_S_BY_SERVER invariant.

    smoke20 bandit-taint: pyright was promotable but absent from the budget
    table, so every cold-start leg hit the 20s default mid-index and
    resolved zero call edges while receipts read 'succeeded'. A server
    added to the promotion map without a readiness budget reopens that
    failure on whatever language it serves.
    """
    from groundtruth.lsp.background_promotion import _LANGUAGE_SERVERS
    from groundtruth.resolve import _READY_BUDGET_S_BY_SERVER

    missing = set(_LANGUAGE_SERVERS.values()) - set(_READY_BUDGET_S_BY_SERVER)
    assert not missing, f"language server(s) without a readiness budget: {sorted(missing)}"
