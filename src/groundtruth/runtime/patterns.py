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
    r"|(?:npx|bunx?)\s+|(?:yarn|pnpm)\s+(?:dlx\s+)?"  # JS package-runner wrappers: `npx jest`, `yarn jest`, `pnpm dlx vitest`
    r"|python[\d.]*\s+(?=\S*\.py\b))*(?:"
    r"python[\d.]*\s+-m\s+(?:pytest|unittest|nose2?|tox)\b"
    r"|pytest\b|py\.test\b|tox\b|nose2?\b"
    r"|(?:\S*/)?(?:runtests?|run_tests?)\.py\b"
    r"|(?:\S*/)?manage\.py\s+test\b"
    r"|go\s+test\b|cargo\s+test\b"
    r"|npm\s+(?:run\s+)?test\b|yarn\s+(?:run\s+)?test\b|pnpm\s+(?:run\s+)?test\b"
    r"|bun\s+test\b|deno\s+test\b|node\s+--test\b"  # JS-native test runners
    r"|jest\b|mocha\b|vitest\b|rspec\b|rake\s+test\b|phpunit\b|ctest\b"
    r"|mvn\s+\S*\s*test\b|gradlew?\s+\S*\s*test\b|make\s+(?:check|test)\b"
    r")", re.I)

# ---------------------------------------------------------------------------
# TEST PASS / FAIL markers — an observed RESULT either way latches
# test_evidence_seen. A bare Traceback / "Error:" is NOT proof a test failed.
# ---------------------------------------------------------------------------
TEST_PASS_RE = re.compile(
    r"(test result: ok\b|\b\d+ passed\b|\b\d+ passing\b|\bPASSED\b"
    r"|^OK\b|^ok\s+\S+\s+[\d.]+s|^PASS$|^PASS\b|BUILD SUCCESS"
    r"|OK \(\d+ tests?\)|Tests:\s+\d+ passed|\bpassed\b.*\b0 failed\b)",
    re.M)

TEST_FAIL_RE = re.compile(
    r"(\bFAILED\b|\bAssertionError\b|\b\d+ failed\b|\bFAIL: "
    r"|FAILED \(failures=|--- FAIL:|test result: FAILED"
    r"|\b\d+ failing\b|Tests:\s+\d+ failed)")

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
    r"|errors? during collection|ERROR collecting|Interrupted: \d+ error)", re.I)

# ---------------------------------------------------------------------------
# COMPILE FAILURE — a build/compile error (actionable feedback, not blindness).
# ---------------------------------------------------------------------------
COMPILE_FAIL_RE = re.compile(
    r"(error\[E\d+\]|error: could not compile|\bSyntaxError\b"
    r"|cannot find (?:value|function|type|module|symbol)"
    r"|undefined:\s|\bTS\d{4,}:|compilation error)")

# ---------------------------------------------------------------------------
# INFRA / TEARDOWN NOISE (W4 guard 1) — a failure marker that is NOT the agent's
# regression: it originates in the TEST HARNESS ITSELF (session config/teardown,
# fixture setup/teardown, a third-party pytest plugin's finalizer, or an internal
# runner error), NOT in the code under edit. The motivating live false-fire
# (facebookresearch/hydra-3005, smoke30 ss128): a fully PASSING run
# (``389 passed in 1.51s``, returncode 0) whose tail carried a stray
# ``AssertionError: plugin is not registered`` raised deep inside
# ``pytest_unconfigure`` (the ``pytest_snail`` plugin's session-teardown) — the
# governor's ``AssertionError`` marker matched and fired l5.failure ("your
# hypothesis is likely wrong, reconsider the target file") on a green run.
#
# These frames name the HARNESS's OWN MACHINERY — the pytest session driver, its
# config (un)configure path, its internal error, or the plugin registry. They can
# NOT reflect the agent's source. DELIBERATELY EXCLUDED: pytest fixture
# ``ERROR at setup/teardown of <test>`` — a fixture runs the AGENT'S code (measured:
# jupyterlab/jupyter-ai-1294's ``ERROR at setup`` was a real ``ValueError`` in the
# agent's ``config_manager.py:295``), so it is NOT harness noise. ENV_FAIL_RE already
# covers COLLECTION errors + import shims; this is the complementary SESSION-teardown
# / plugin-finalizer / INTERNALERROR class it does not carry.
# ---------------------------------------------------------------------------
INFRA_NOISE_RE = re.compile(
    r"pytest_unconfigure|_ensure_unconfigure|\bwrap_session\b"
    r"|\bINTERNALERROR\b"
    r"|pluginmanager\.(?:unregister|register)\b"
    r"|plugin is not registered",
    re.IGNORECASE,
)

# A GENUINE test failure/error the run really had — a summary count ("3 failed",
# "= 2 errors ="), a per-test node result ("test_x FAILED"/"FAILED test_x"), or a
# short-summary "ERROR" node. When ANY is present the observation is NOT pure infra
# noise even if a harness-machinery frame also appears, so is_infra_noise returns
# False and a real regression still steers. The "0 failed" pass line never matches
# (the count leg requires a [1-9] lead), so a fully green run stays noise.
_GENUINE_FAILURE_RE = re.compile(
    r"\b[1-9]\d* (?:failed|error|errors|failing)\b"   # summary count
    r"|\bFAILED\b"                                     # per-test / short-summary FAILED
    r"|::\S+\s+ERROR\b",                               # per-test node ERROR (`test::x ERROR`)
    re.IGNORECASE)


def is_infra_noise(text: str) -> bool:
    """True when a failure marker in ``text`` is HARNESS infra/session-teardown noise,
    not the agent's source regression. Requires (a) a harness-own-machinery signature
    AND (b) NO genuine test failure/error (summary count, per-test FAILED, or per-test
    ERROR node) — a run where a real test failed/errored is never suppressed even if a
    plugin-finalizer frame coexists. Conservative + correct-or-quiet: fires only on the
    harness-machinery shape with an otherwise-clean result, so a real assertion in a
    test body (which does not carry these frames) is never called noise, and a fixture
    error running agent code (``ERROR at setup of`` -> not in the signature set) is
    never suppressed. Language-uniform on the dominant Python/pytest surface; other
    runners fall through to False (unchanged behavior)."""
    t = text or ""
    if not INFRA_NOISE_RE.search(t):
        return False
    if _GENUINE_FAILURE_RE.search(t):
        return False
    return True


__all__ = [
    "TEST_RUNNER_RE",
    "TEST_PASS_RE",
    "TEST_FAIL_RE",
    "ENV_FAIL_RE",
    "COMPILE_FAIL_RE",
    "INFRA_NOISE_RE",
    "is_infra_noise",
]
