"""Canonical behavioral-classification patterns — ONE source of truth.

Every surface (product hooks, DeepSWE oracle/patch, metrics scripts) MUST import
these instead of redefining them. Divergent copies caused the governor to fire on
one surface and stay silent on another for the same command/output (audit RED #1, #2).

The patterns here are the SUPERSET — the most complete, LIPI-hardened versions
(migrated from artifact_deepswe/gt_mini_patch.py, which carried the battle-tested
forms: timeout/env wrappers, manage.py test, runtests.py, rake, phpunit, ctest).
Nothing was removed in the consolidation; only unified.

Research basis for the governor that consumes these: TIDE (arXiv 2602.02196),
TRAJEVAL (arXiv 2603.24631), "Beyond Resolution Rates" (arXiv 2604.02547).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# TEST RUNNER — a REAL test-runner invocation (the gate for failure_persisted,
# no_test_evidence, and verifier-retry). Accepts timeout/time/env wrappers and
# the `python <script>.py` prefix ONLY when the next token is a .py script
# (Django's `python manage.py test`); an arbitrary `python script.py` is NOT a
# runner — the runner-shape alternatives below still decide.
# ---------------------------------------------------------------------------
TEST_RUNNER_RE = re.compile(
    r"(?:^|[|&;]\s*)(?:timeout\s+(?:-\S+\s+|\d+\S*\s+)+|time\s+|env\s+(?:\S+=\S+\s+)+"
    r"|python[\d.]*\s+(?=\S*\.py\b))*(?:"
    r"python[\d.]*\s+-m\s+(?:pytest|unittest|nose2?|tox)\b"
    r"|pytest\b|py\.test\b|tox\b|nose2?\b"
    r"|(?:\S*/)?(?:runtests?|run_tests?)\.py\b"
    r"|(?:\S*/)?manage\.py\s+test\b"
    r"|go\s+test\b|cargo\s+test\b"
    r"|npm\s+(?:run\s+)?test\b|yarn\s+(?:run\s+)?test\b|pnpm\s+(?:run\s+)?test\b"
    r"|jest\b|mocha\b|vitest\b|rspec\b|rake\s+test\b|phpunit\b|ctest\b"
    r"|mvn\s+\S*\s*test\b|gradlew?\s+\S*\s*test\b|make\s+(?:check|test)\b"
    r")",
    re.I,
)

# ---------------------------------------------------------------------------
# TEST PASS / FAIL markers — an observed RESULT either way latches
# test_evidence_seen. A bare Traceback / "Error:" is NOT proof a test failed.
# ---------------------------------------------------------------------------
TEST_PASS_RE = re.compile(
    r"(test result: ok\b|\b\d+ passed\b|\b\d+ passing\b"
    r"|^OK\b|^ok\s+\S+\s+[\d.]+s|^PASS$|^PASS\b|BUILD SUCCESS"
    r"|OK \(\d+ tests?\)|Tests:\s+\d+ passed|\bpassed\b.*\b0 failed\b)",
    re.M,
)

TEST_FAIL_RE = re.compile(
    r"(\bFAILED\b|\bAssertionError\b|\b\d+ failed\b|\bFAIL: "
    r"|FAILED \(failures=|--- FAIL:|test result: FAILED"
    r"|\b\d+ failing\b|Tests:\s+\d+ failed)"
)

# ---------------------------------------------------------------------------
# ENV FAILURE — environment/tooling failure (NOT a test failure). Used to
# suppress governor false-positives: an env error is actionable feedback, not a
# model behavior signal. Broadest form (audit RED #2 canonical).
# ---------------------------------------------------------------------------
ENV_FAIL_RE = re.compile(
    r"(ModuleNotFoundError|No module named|ImportError"
    r"|ERROR: Could not find a version|No matching distribution found"
    r"|Could not build wheels|subprocess-exited-with-error|metadata-generation-failed"
    r"|error: command .* failed|fatal error: |compilation terminated"
    r"|undefined reference to|ld returned \d+ exit status|collect2: error"
    r"|command not found|is not recognized as an internal or external command"
    r"|Connection refused|Network is unreachable|Temporary failure in name resolution"
    r"|CERTIFICATE_VERIFY_FAILED|ReadTimeoutError|ProxyError"
    r"|error while loading shared libraries|cannot open shared object"
    r"|ImproperlyConfigured"
    r"|AttributeError: module '[\w.]+' has no attribute"  # py-version shims
    r"|errors? during collection|ERROR collecting|Interrupted: \d+ error)",
    re.I,
)

# ---------------------------------------------------------------------------
# COMPILE FAILURE — a build/compile error (actionable feedback, not blindness).
# ---------------------------------------------------------------------------
COMPILE_FAIL_RE = re.compile(
    r"(error\[E\d+\]|error: could not compile|\bSyntaxError\b"
    r"|cannot find (?:value|function|type|module|symbol)"
    r"|undefined:\s|\bTS\d{4,}:|compilation error)"
)


__all__ = [
    "TEST_RUNNER_RE",
    "TEST_PASS_RE",
    "TEST_FAIL_RE",
    "ENV_FAIL_RE",
    "COMPILE_FAIL_RE",
]
