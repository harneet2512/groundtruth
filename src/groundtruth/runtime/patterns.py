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
from dataclasses import dataclass
from enum import Enum

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
    r"(test result: ok\b|\b[1-9]\d* passed\b|\b[1-9]\d* passing\b|\bPASSED\b"
    r"|^OK\b|^ok\s+\S+\s+[\d.]+s|^PASS$|^PASS\b|BUILD SUCCESS"
    r"|OK \([1-9]\d* tests?\)|Tests:\s+[1-9]\d* passed"
    r"|\b[1-9]\d* passed\b.*\b0 failed\b)",
    re.M)

TEST_FAIL_RE = re.compile(
    r"(\bFAILED\b|\bAssertionError\b|\b[1-9]\d* failed\b|\bFAIL: "
    r"|FAILED \(failures=|--- FAIL:|test result: FAILED"
    r"|\b[1-9]\d* failing\b|Tests:\s+[1-9]\d* failed"
    r"|Failures:\s*[1-9]\d*|Errors:\s*[1-9]\d*)")

# Explicit runner-owned proof that the command executed no tests. This is
# evaluated after fail but before pass, and only after a test protocol is
# established: summaries such as "Tests: 0 passed, 0 total" contain a lexical
# pass marker but are not positive execution evidence.
TEST_NO_TESTS_RE = re.compile(
    r"(\bcollected\s+0\s+items?\b|\bno tests? ran\b|\bran\s+0\s+tests?\b"
    r"|(?:^|\n)\s*running\s+0\s+tests?\b|\[no test files\]"
    r"|\b0\s+passing\b|\bTests run:\s*0\b"
    r"|\bNo tests? (?:were )?(?:found|executed|to run)\b"
    r"|\bNo test files? (?:were )?found\b|\bTests? are skipped\b"
    r"|\b0\s+examples?\b|\bTests:\s*0\s+total\b"
    r"|\bOK\s*\(0 tests?\)|\bTests:\s*0\s+(?:passed,\s*)?0\s+total\b)",
    re.I,
)

# Native test protocols can outlive their launcher command.  Rust is the
# canonical example: after ``cargo test --no-run`` an agent may execute
# ``target/{debug,release}/deps/<test-binary>`` directly.  The executable name
# is intentionally opaque, so command spelling cannot establish that this was
# a test.  These result-frame markers do: they are emitted by the runner
# protocol itself and are narrower than generic ``FAILED``/``AssertionError``.
TEST_PROTOCOL_RE = re.compile(
    r"(?:^|\n)\s*running\s+\d+\s+tests?\b"
    r"|(?:^|\n)\s*test result:\s*(?:ok|FAILED)\b",
    re.I,
)


def _classify_formal_test(
    command: str,
    output: str,
    returncode: int | None = None,
) -> tuple[str, str]:
    """The FROZEN formal-runner classifier body.

    Extracted verbatim from ``classify_test_observation`` so that both the
    public (behaviourally frozen) entry point and the broader
    :func:`classify_validation_observation` share ONE implementation instead of
    forking it. Not exported: callers use ``classify_test_observation``.
    """
    cmd = command or ""
    out = output or ""
    if TEST_RUNNER_RE.search(cmd):
        protocol = "command"
    elif TEST_PROTOCOL_RE.search(out):
        protocol = "native"
    else:
        return "", ""
    failed = TEST_FAIL_RE.search(out)
    passed = TEST_PASS_RE.search(out)
    if failed:
        return "fail", protocol
    # Environment truth outranks a lexical zero-test summary. A known-clean
    # positive result is the sole exception: test bodies may legitimately print
    # error examples without making the runner fail.
    if ENV_FAIL_RE.search(out) and not (returncode == 0 and passed):
        return "env_fail", protocol
    if TEST_NO_TESTS_RE.search(out):
        return "executed_no_tests", protocol
    if passed and (returncode is None or returncode == 0):
        return "pass", protocol
    return "", protocol


def classify_test_observation(
    command: str,
    output: str,
    returncode: int | None = None,
) -> tuple[str, str]:
    """Return ``(outcome, protocol)`` for an observed test result.

    ``outcome`` is ``"pass"`` / ``"fail"`` / ``"executed_no_tests"`` / ``""``.
    ``protocol`` records which independently observable surface established
    that the command was a test: ``"command"`` for a recognized runner
    invocation or ``"native"`` for a runner-owned result frame (for example a
    directly executed Rust test binary). A generic non-zero return code or the
    word ``FAILED`` alone is not a test protocol and stays quiet.

    BEHAVIOURALLY FROZEN. Every existing caller (the mini seam's
    ``_classify_test_observation`` import, ``adapters.miniswe.normalize_event``)
    depends on this exact contract, and the seam carries a byte-mirrored inline
    fallback. This is a thin wrapper over :func:`_classify_formal_test`, which
    holds the unmodified body; the WIDER validation vocabulary lives in
    :func:`classify_validation_observation` and never changes this result.
    """
    return _classify_formal_test(command, output, returncode)

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


# ===========================================================================
# VALIDATION OBSERVATION — the SUPERSET of authoritative validation evidence.
#
# WHY (live defect, run 30390877219, task aws-cloudformation__cfn-lint-3764):
# `classify_test_observation` above answers exactly one question — "was this an
# allow-listed TEST RUNNER, and what did it report?". Everything downstream is
# gated on that answer, including the SS-2 submit-RED latch. On cfn-lint-3764
# the agent ran a plain `python -c` reproduction, WATCHED IT FAIL (a resolver
# error plus an AttributeError traceback), talked itself out of the failure and
# submitted; the hidden tests then failed on exactly that scenario. GT had seen
# a green `pytest` earlier and nothing since, because a `python -c` probe is not
# a recognised runner. The disconfirming evidence was structurally invisible.
#
# An agent validates its hypothesis with far more than pytest: ad-hoc repro
# scripts, inline interpreter probes, compilers, and type/lint checkers are all
# authoritative about the code under edit. This vocabulary names them.
#
# CORRECT-OR-QUIET is the governing constraint. A classifier that fires on a
# non-validation command is worse than one that misses, so:
#   * COMMAND-driven kinds require a recognised validation SHAPE at a shell
#     segment head — never a substring match anywhere in the line.
#   * OUTPUT-driven kinds (COMPILER_CHECK by diagnostic, ASSERTION_SCRIPT)
#     additionally require a KNOWN NON-ZERO returncode, so `cat` of a file that
#     merely CONTAINS "SyntaxError" can never be read as a compiler failure.
#   * A known-ZERO returncode can never produce "fail", mirroring the existing
#     rule that a known-non-zero exit can never produce "pass".
#   * A traceback whose deepest frame lives in site-packages/node_modules/stdlib
#     is dependency/env truth, not the agent's disconfirmed hypothesis.
#
# DELIBERATELY NOT CLASSIFIED (each is a false-fire this module refuses):
#   * The MEANING of printed output. A probe that exits 0 while printing a wrong
#     answer is `pass` — GT judges execution truth, not semantics. Judging
#     semantics would require an oracle GT does not have.
#   * `manage.py` / `setup.py` / `conftest.py` script invocations. `python
#     manage.py migrate` is repository administration, not validation; the
#     genuine `manage.py test` form is already a FORMAL_TEST above.
#   * Formatters (`ruff format`, `black`, `prettier`, `gofmt`). Formatting says
#     nothing about behavioural correctness.
#   * Package/build management (`pip install`, `npm install`, `poetry lock`),
#     VCS (`git diff/status`), navigation, search, view and edit commands.
#   * `-e`-style eval flags outside python/node (`ruby -e`, `perl -e`) and bare
#     interpreters (`python` with no body). Narrow beats speculative.
#   * A bare test-FILE argument (`pytest tests/test_x.py`) is FORMAL_TEST, not
#     FOCUSED_TEST: file scope is not a node-id/`-k` selector.
# ===========================================================================

# Shell-segment head + the same wrapper prefixes TEST_RUNNER_RE accepts.
_SEGMENT_HEAD = (
    r"(?:^|[|&;]\s*)(?:timeout\s+(?:-\S+\s+|\d+\S*\s+)+|time\s+|env\s+(?:\S+=\S+\s+)+"
    r"|(?:npx|bunx?)\s+|(?:yarn|pnpm)\s+(?:dlx\s+)?"
    r"|poetry\s+run\s+|uv\s+run\s+)*"
)

# RUNTIME PROBE — an inline interpreter body: `python -c '<expr>'`, `node -e`.
# Requires a non-empty body token; a bare `python` REPL launch does not match.
RUNTIME_PROBE_RE = re.compile(
    _SEGMENT_HEAD + r"(?:python[\d.]*\s+(?:-[A-Za-z]+\s+)*-c\s+\S"
    r"|node\s+(?:-[A-Za-z]+\s+)*(?:-e|--eval)\s+\S)",
    re.I,
)

# AD-HOC REPRO — `python <script>.py` / `node <script>.js`. TEST_RUNNER_RE is
# consulted FIRST, so `manage.py test` / `runtests.py` never reach here.
AD_HOC_REPRO_RE = re.compile(
    _SEGMENT_HEAD + r"(?:python[\d.]*|node|ts-node|tsx|bun|deno\s+run)\s+"
    r"(?:-[A-Za-z]+\s+)*(?P<script>[\w./\\-]+\.(?:py|js|mjs|cjs|ts))(?:\s|$)",
    re.I,
)

# Script basenames that are repository machinery, not a reproduction.
_NON_REPRO_SCRIPTS = frozenset({
    "manage.py", "setup.py", "conftest.py", "runtests.py", "run_tests.py",
    "runtest.py", "run_test.py", "setup.js", "gulpfile.js", "webpack.config.js",
})

# STATIC CHECK — type checkers and linters. `ruff format` is excluded (a
# formatter says nothing about behaviour); `ruff check` / `ruff <path>` is not.
STATIC_CHECK_RE = re.compile(
    _SEGMENT_HEAD + r"(?:mypy\b|pyright\b|pytype\b|flake8\b|pylint\b"
    r"|ruff\s+(?!format\b)\S|eslint\b|tslint\b|biome\s+lint\b"
    r"|golangci-lint\b|go\s+vet\b|staticcheck\b|cargo\s+clippy\b)",
    re.I,
)

# COMPILER CHECK — a build/typecheck invocation. `make` is intentionally absent:
# it is classified only when its OUTPUT carries a compiler diagnostic.
COMPILER_CHECK_RE = re.compile(
    _SEGMENT_HEAD + r"(?:go\s+build\b|cargo\s+(?:check|build)\b|tsc\b"
    r"|mvn\s+\S*\s*compile\b|gradlew?\s+\S*\s*(?:compile|assemble)\w*\b"
    r"|javac\b|g\+\+\b|gcc\b|clang\+*\b|cmake\s+--build\b)",
    re.I,
)

# FOCUSED selectors: an explicit node-id or name filter, never a bare path.
_FOCUS_NODEID_RE = re.compile(r"(?P<sel>\S+::\S+)")
_FOCUS_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-k|--test-name-pattern|--testNamePattern|--grep|--filter"
    r"|--run|-run|-t)[=\s]+(?P<sel>\S+)"
)
_FOCUS_UNITTEST_RE = re.compile(
    r"-m\s+unittest\b(?:\s+-\S+)*\s+(?P<sel>[\w]+(?:\.[\w]+)+)"
)

# Deepest-frame extraction. `<string>` / `<stdin>` / `<frozen ...>` are the
# interpreter's own pseudo-files, not repository sources.
_PY_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+', re.M)
_JS_FRAME_RE = re.compile(
    r"\bat\s+(?:[^\s(]+\s+)?\(?([^\s()]+\.(?:js|mjs|cjs|ts)):\d+:\d+"
)
_NON_REPO_FRAME_RE = re.compile(
    r"site-packages|dist-packages|node_modules|/usr/lib/python|\\lib\\python"
    r"|/lib/python3|[/\\]python3\.\d+[/\\](?!site)|[/\\]Lib[/\\]|\.rustup|\.cargo[/\\]registry",
    re.I,
)
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\)|\bAssertionError\b|\bERR_ASSERTION\b"
)

# Commands whose OUTPUT IS SOMEONE ELSE'S TEXT. `cat build.log`, `grep -C3
# Traceback app.log` and `git log -p` can all surface a compiler diagnostic or a
# stack frame that the command did NOT produce. Attributing that text to the
# command is exactly the false-fire this module must refuse, so a match here
# suppresses OUTPUT-driven classification. It is tested against the LAST shell
# segment only — the command that actually produced the tail of the output — so
# a leading `cd /repo && ./run.sh` is still classified on `./run.sh`. It never
# suppresses command-shape kinds: in `cat x && python repro.py` the repro
# segment is recognised on its own shape at step 2.
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&]")
_NON_VALIDATION_CMD_RE = re.compile(
    r"^\s*(?:cat|bat|less|more|head|tail|nl|od|xxd|strings"
    r"|grep|egrep|fgrep|rg|ag|ack|find|fd|locate|ls|dir|tree|stat|file|wc|du|df"
    r"|git|hg|svn|diff|sed|awk|cut|sort|uniq|tr|echo|printf"
    r"|pip[\d.]*|pip3|npm|yarn|pnpm|apt|apt-get|brew|conda|poetry|uv"
    r"|cd|pwd|mkdir|touch|cp|mv|rm|chmod|export|which|whereis|env)\b",
    re.I,
)


class ValidationKind(str, Enum):
    """What KIND of authoritative validation the agent just performed."""

    NONE = ""
    FORMAL_TEST = "formal_test"
    FOCUSED_TEST = "focused_test"
    AD_HOC_REPRO = "ad_hoc_repro"
    RUNTIME_PROBE = "runtime_probe"
    COMPILER_CHECK = "compiler_check"
    STATIC_CHECK = "static_check"
    ASSERTION_SCRIPT = "assertion_script"


@dataclass(frozen=True)
class ValidationObservation:
    """One classified validation observation.

    ``outcome`` reuses the frozen test vocabulary — ``"pass"`` / ``"fail"`` /
    ``"env_fail"`` / ``"executed_no_tests"`` / ``""`` (unobserved) — so a
    consumer that already understands ``classify_test_observation`` needs no new
    outcome grammar. ``protocol`` names the surface that established the kind:
    ``"command"`` (recognised invocation shape), ``"native"`` (runner-owned
    result frame) or ``"output"`` (diagnostic/traceback truth).
    """

    kind: ValidationKind = ValidationKind.NONE
    outcome: str = ""
    protocol: str = ""
    selector: str = ""
    frame: str = ""

    def __bool__(self) -> bool:
        return self.kind is not ValidationKind.NONE

    @property
    def is_formal_test(self) -> bool:
        return self.kind in (ValidationKind.FORMAL_TEST, ValidationKind.FOCUSED_TEST)

    @property
    def disconfirming(self) -> bool:
        """True only for a genuine negative result — env failures excluded."""
        return self.outcome == "fail"


def _is_repo_frame(path: str, repo_root: str = "") -> bool:
    """True when a traceback frame names repository source, not a dependency.

    ``repo_root``, when the caller knows it, only NARROWS admission: an ABSOLUTE
    path outside the checkout is rejected outright. Relative paths are always
    judged by the dependency-marker rule alone, because a relative frame is
    already resolved against the process cwd (the checkout).
    """
    if not path or path.startswith("<"):
        return False
    if _NON_REPO_FRAME_RE.search(path):
        return False
    if repo_root:
        normalized = path.replace("\\", "/")
        root = repo_root.replace("\\", "/").rstrip("/")
        is_absolute = normalized.startswith("/") or (
            len(normalized) > 1 and normalized[1] == ":"
        )
        if is_absolute and not normalized.lower().startswith(root.lower() + "/"):
            return False
    return True


def deepest_repo_frame(output: str, repo_root: str = "") -> str:
    """Return the DEEPEST repository-relative traceback frame, or ``""``.

    Python frames are printed outermost-first, so the deepest frame is the last
    one; JS stacks are printed innermost-first, so the first is used. Frames in
    site-packages / node_modules / the stdlib are skipped: a crash inside a
    dependency is environment truth, not the agent's hypothesis being wrong.
    """
    out = output or ""
    for path in reversed(_PY_FRAME_RE.findall(out)):
        if _is_repo_frame(path, repo_root):
            return path
    for path in _JS_FRAME_RE.findall(out):
        if _is_repo_frame(path, repo_root):
            return path
    return ""


def _focus_selector(command: str) -> str:
    for pattern in (_FOCUS_NODEID_RE, _FOCUS_UNITTEST_RE, _FOCUS_FLAG_RE):
        match = pattern.search(command)
        if match:
            return match.group("sel")
    return ""


def _non_test_outcome(
    kind: ValidationKind,
    output: str,
    returncode: int | None,
) -> str:
    """Outcome for the NON-runner kinds. Execution truth only, never semantics."""
    out = output or ""
    if returncode == 0:
        # A clean exit cannot be a failure. An env marker on a clean exit is
        # contradictory (printed text, not a real import error) -> stay quiet.
        return "" if ENV_FAIL_RE.search(out) else "pass"
    if returncode is not None:
        # Known non-zero: an explicit compiler diagnostic outranks the env
        # heuristics (a real `fatal error:` from the code under edit is the
        # agent's problem, not the environment's).
        if kind is ValidationKind.COMPILER_CHECK and COMPILE_FAIL_RE.search(out):
            return "fail"
        if ENV_FAIL_RE.search(out):
            return "env_fail"
        return "fail"
    # Unknown returncode: marker-driven only.
    if COMPILE_FAIL_RE.search(out):
        return "fail"
    if ENV_FAIL_RE.search(out):
        return "env_fail"
    if TEST_FAIL_RE.search(out) or (
        _TRACEBACK_RE.search(out) and deepest_repo_frame(out)
    ):
        return "fail"
    return ""


def classify_validation_observation(
    command: str,
    output: str,
    returncode: int | None = None,
    *,
    repo_root: str = "",
) -> ValidationObservation:
    """Classify ANY authoritative validation the agent performed.

    This is the superset of :func:`classify_test_observation`, which sees only
    an allow-listed test runner. The formal-test path here delegates to the same
    frozen implementation, so ``FORMAL_TEST``/``FOCUSED_TEST`` observations carry
    byte-identical ``(outcome, protocol)`` values; every other kind is new
    evidence that used to be discarded as ``("", "")``.

    ``repo_root``, when the caller knows the checkout path, only NARROWS frame
    admission: an absolute traceback frame outside the checkout is rejected.
    Omitting it is safe — the dependency-marker rule already applies.
    """
    cmd = command or ""
    out = output or ""

    # 1. Formal runners first — the frozen contract wins every overlap.
    outcome, protocol = _classify_formal_test(cmd, out, returncode)
    if protocol:
        selector = _focus_selector(cmd) if protocol == "command" else ""
        return ValidationObservation(
            kind=(
                ValidationKind.FOCUSED_TEST if selector
                else ValidationKind.FORMAL_TEST
            ),
            outcome=outcome,
            protocol=protocol,
            selector=selector,
            frame=deepest_repo_frame(out, repo_root),
        )

    # 2. Command-shape kinds. Static checks precede compilers (`go vet` vs
    #    `go build`); probes precede scripts (`python -c` carries no .py arg).
    kind = ValidationKind.NONE
    if STATIC_CHECK_RE.search(cmd):
        kind = ValidationKind.STATIC_CHECK
    elif COMPILER_CHECK_RE.search(cmd):
        kind = ValidationKind.COMPILER_CHECK
    elif RUNTIME_PROBE_RE.search(cmd):
        kind = ValidationKind.RUNTIME_PROBE
    else:
        script = AD_HOC_REPRO_RE.search(cmd)
        if script:
            path = script.group("script")
            basename = path.replace("\\", "/").rsplit("/", 1)[-1]
            if basename not in _NON_REPRO_SCRIPTS and _is_repo_frame(path, repo_root):
                kind = ValidationKind.AD_HOC_REPRO
    if kind is not ValidationKind.NONE:
        return ValidationObservation(
            kind=kind,
            outcome=_non_test_outcome(kind, out, returncode),
            protocol="command",
            frame=deepest_repo_frame(out, repo_root),
        )

    # 3. Output-driven kinds. These require a KNOWN NON-ZERO exit so that merely
    #    VIEWING text containing a diagnostic can never be read as validation.
    if returncode is None or returncode == 0:
        return ValidationObservation()
    tail_segment = _SEGMENT_SPLIT_RE.split(cmd)[-1]
    if _NON_VALIDATION_CMD_RE.search(tail_segment):
        return ValidationObservation()
    if COMPILE_FAIL_RE.search(out):
        return ValidationObservation(
            kind=ValidationKind.COMPILER_CHECK,
            outcome="fail",
            protocol="output",
            frame=deepest_repo_frame(out, repo_root),
        )
    frame = deepest_repo_frame(out, repo_root)
    if frame and _TRACEBACK_RE.search(out):
        return ValidationObservation(
            kind=ValidationKind.ASSERTION_SCRIPT,
            outcome=_non_test_outcome(ValidationKind.ASSERTION_SCRIPT, out, returncode),
            protocol="output",
            frame=frame,
        )
    return ValidationObservation()


__all__ = [
    "TEST_RUNNER_RE",
    "TEST_PASS_RE",
    "TEST_FAIL_RE",
    "TEST_NO_TESTS_RE",
    "TEST_PROTOCOL_RE",
    "classify_test_observation",
    "ENV_FAIL_RE",
    "COMPILE_FAIL_RE",
    "INFRA_NOISE_RE",
    "is_infra_noise",
    "ValidationKind",
    "ValidationObservation",
    "classify_validation_observation",
    "deepest_repo_frame",
    "RUNTIME_PROBE_RE",
    "AD_HOC_REPRO_RE",
    "STATIC_CHECK_RE",
    "COMPILER_CHECK_RE",
]
