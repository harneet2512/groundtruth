"""Per-language coverage for classify_validation_observation.

The validation-observation channel is the evidence superset: a static check or
compiler invocation the agent performed that GT cannot classify is invisible
evidence — the same defect class as the dead-check gap, one layer deeper.
Every language family GT claims to serve must classify here, and every
non-check shape must stay NONE so prose can never counterfeit validation.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime.patterns import (
    ValidationKind,
    classify_validation_observation,
)

# ---------------------------------------------------------------------------
# STATIC_CHECK — linters and type checkers per language ecosystem.
# ---------------------------------------------------------------------------

_STATIC_CASES = [
    # python
    ("mypy src/", "Success: no issues found", 0),
    ("ruff check src/", "", 0),
    ("pyright --outputjson", "", 0),
    ("flake8 src/", "", 0),
    ("black --check src/", "would reformat 1 file", 1),
    # javascript / typescript
    ("npx eslint src/", "", 0),
    ("biome lint .", "", 0),
    # go
    ("go vet ./...", "", 0),
    ("golangci-lint run", "", 0),
    ("staticcheck ./...", "", 0),
    # rust
    ("cargo clippy", "", 0),
    # ruby / php / elixir / shell
    ("bundle exec rubocop", "", 0),
    ("vendor/bin/phpstan analyse", "", 0),
    ("psalm --no-cache", "", 0),
    ("mix credo", "", 0),
    ("shellcheck deploy.sh", "", 0),
    # java / kotlin / scala
    ("checkstyle -c checks.xml src/", "", 0),
    ("detekt --input src/", "", 0),
    ("ktlint 'src/**/*.kt'", "", 0),
    ("scalafmt --test", "", 0),
    # c/c++ / swift / .NET / haskell / lua
    ("clang-tidy src/x.cpp", "", 0),
    ("cppcheck --enable=all src/", "", 0),
    ("swiftlint", "", 0),
    ("dotnet format --verify-no-changes", "", 0),
    ("hlint src/", "", 0),
    ("luacheck src/", "", 0),
]


@pytest.mark.parametrize("command,out,rc", _STATIC_CASES)
def test_static_checkers_classify_per_language(command, out, rc):
    obs = classify_validation_observation(command, out, rc)
    assert obs.kind is ValidationKind.STATIC_CHECK, command
    assert obs.protocol == "command"


@pytest.mark.parametrize(
    "command",
    [
        "black src/",  # formatter without --check rewrites files
        "ruff format src/",  # explicitly excluded
        "prettier --write src/",  # write mode is not a check
        "dotnet format",  # no --verify flag: mutates
        "cat mypy_report.txt",  # viewing output is not running a check
        "isort src/",  # no --check flag
    ],
)
def test_non_check_shapes_never_read_as_static_check(command):
    obs = classify_validation_observation(command, "some output", 0)
    assert obs.kind is not ValidationKind.STATIC_CHECK, command


# ---------------------------------------------------------------------------
# COMPILER_CHECK — build/typecheck invocations per language ecosystem.
# ---------------------------------------------------------------------------

_COMPILER_CASES = [
    ("go build ./...", "", 0),
    ("cargo check", "", 0),
    ("cargo build --release", "", 0),
    ("tsc --noEmit", "", 0),
    ("javac src/Main.java", "", 0),
    ("gcc -o out src/x.c", "", 0),
    ("g++ -O2 x.cpp", "", 0),
    ("cmake --build build/", "", 0),
    ("mvn compile", "", 0),
    ("./mvnw compile", "", 0),  # path-prefixed wrapper — was invisible
    ("mvn -q compile", "", 0),
    ("gradle compileJava", "", 0),
    ("./gradlew assembleDebug", "", 0),  # path-prefixed wrapper — was invisible
    ("dotnet build", "", 0),
    ("dotnet publish -c Release", "", 0),
    ("msbuild solution.sln", "", 0),
    ("sbt compile", "", 0),
    ("mix compile", "", 0),
    ("bazel build //...", "", 0),
    ("swift build", "", 0),
    ("kotlinc src/Main.kt", "", 0),
]


@pytest.mark.parametrize("command,out,rc", _COMPILER_CASES)
def test_compiler_invocations_classify_per_language(command, out, rc):
    obs = classify_validation_observation(command, out, rc)
    assert obs.kind is ValidationKind.COMPILER_CHECK, command
    assert obs.protocol == "command"


def test_code_attributed_diagnostic_is_a_real_fail():
    """`file:line: error:` is the agent's problem — a genuine fail, never
    swallowed by the env heuristics."""
    obs = classify_validation_observation(
        "gcc x.c", "x.c:5:10: error: expected ';' before '}' token", 1
    )
    assert obs.kind is ValidationKind.COMPILER_CHECK
    assert obs.outcome == "fail"
    assert obs.disconfirming


def test_coded_compiler_errors_are_fails_per_language():
    """MSVC/C#/tsc-coded diagnostics classify as compile failures."""
    for out in (
        "Main.cs(5,10): error CS0246: type not found",
        "x.cpp(12): error C2143: syntax error",
        "src/app.ts:3:9: error TS2304: Cannot find name 'x'.",
        "Main.java:7: error: cannot find symbol",
    ):
        obs = classify_validation_observation("dotnet build", out, 1)
        assert obs.outcome == "fail", out


def test_fatal_error_missing_header_stays_env_fail():
    """`fatal error: missing.h` is a missing-header/toolchain truth —
    environment, not a wrong hypothesis. ENV_FAIL_RE owns it by design."""
    obs = classify_validation_observation(
        "gcc x.c", "x.c:5:10: fatal error: missing.h: No such file", 1
    )
    assert obs.kind is ValidationKind.COMPILER_CHECK
    assert obs.outcome == "env_fail"
    assert not obs.disconfirming  # env failures are not disconfirming


def test_clean_compiler_exit_is_a_pass_observation():
    obs = classify_validation_observation("go build ./...", "", 0)
    assert obs.kind is ValidationKind.COMPILER_CHECK
    assert obs.outcome == "pass"


def test_unknown_returncode_compiler_output_still_classifies():
    obs = classify_validation_observation("cargo build", "error[E0308]: mismatched types", None)
    assert obs.kind is ValidationKind.COMPILER_CHECK
    assert obs.outcome == "fail"


# ---------------------------------------------------------------------------
# RUNTIME_PROBE / AD_HOC_REPRO — interpreter validation the agent ran inline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import x; x.f()'",
        "python3 -c 'print(1)'",
        "node -e 'require(\"./x\")'",
        "node --eval 'f()'",
    ],
)
def test_inline_interpreter_probes_classify(command):
    obs = classify_validation_observation(command, "", 0)
    assert obs.kind is ValidationKind.RUNTIME_PROBE, command


@pytest.mark.parametrize(
    "command",
    ["python repro_bug.py", "python3 scripts/repro.py", "node repro.js"],
)
def test_ad_hoc_repro_scripts_classify(command):
    obs = classify_validation_observation(command, "AssertionError", 1)
    assert obs.kind is ValidationKind.AD_HOC_REPRO, command


def test_repo_machinery_scripts_are_not_repro():
    """manage.py / setup.py / conftest.py are repo machinery, never a repro."""
    for cmd in ("python manage.py migrate", "python setup.py build"):
        obs = classify_validation_observation(cmd, "", 0)
        assert obs.kind is not ValidationKind.AD_HOC_REPRO, cmd


# ---------------------------------------------------------------------------
# Ordering: formal tests win over every command-shape kind.
# ---------------------------------------------------------------------------


def test_formal_test_takes_precedence_over_static_and_compiler():
    obs = classify_validation_observation("pytest -q", "5 passed", 0)
    assert obs.kind is ValidationKind.FORMAL_TEST

    obs = classify_validation_observation("pytest tests/test_x.py::test_y", "1 passed", 0)
    assert obs.kind is ValidationKind.FOCUSED_TEST


def test_viewing_output_never_counts_as_validation():
    """cat/grep of a log carrying a diagnostic is not a validation act —
    output-driven kinds require a known non-zero exit AND a real command."""
    for cmd in ("cat build.log", "grep error app.log", "tail -f out.txt"):
        obs = classify_validation_observation(cmd, "error[E0308]: mismatched types", 1)
        assert obs.kind is ValidationKind.NONE, cmd


def test_zero_exit_with_output_driven_shape_stays_none():
    obs = classify_validation_observation("cat build.log", "error: something failed", 0)
    assert obs.kind is ValidationKind.NONE
