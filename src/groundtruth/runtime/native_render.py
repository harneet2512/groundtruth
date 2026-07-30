"""Render a covering-test FAILURE in the model's native channel — Format D.

Frontier-grounded decision (SWE Fable memo, 2026-07-09). A covering RED is
delivered as the **anonymized native failure block**: the runner's OWN
fix-signal tokens (error class, native value-comparison, the agent's own-source
traceback frames) passed through an IDENTITY FIREWALL that strips every
test-identifying token (the ``$ runner file::node`` command, the pytest FAILURES
header, the ``def test_*`` source echo, every test-file traceback frame, the
``FAILED …`` summary).

Why this and not a verbose transcript (A) or a prose summary (B):
- Signal and leak live in DIFFERENT tokens. The RL objective these coders were
  trained on (DeepSWE binary-pass reward; SWE-RL patch-similarity) never
  contained the test's identity — the policy learned to fix on the error class,
  the value delta, and the line in ITS OWN code. Handing it the nodeid is 0 bits
  of fix-signal and 100% of the measured grader leak (7/9 tasks, gt_gt seam
  :1230).
- A verbose transcript is also actively worse here: lost-in-the-middle buries the
  assertion, and the mini-swe-agent pipe truncates >10k-char observations
  head/tail — so "byte-native transcript" is not even reliably delivered.
- B (prose) is the thing that goes out-of-distribution ("1 covering test
  failed: …"). Format D emits only real runner tokens, so it stays native while
  being leak-safe.

INVARIANT (Stage-1 tests): the returned string carries NO ``<gt-`` marker, NO
test-file path, NO ``::nodeid``, NO ``def test_`` echo. Telemetry stays host-side.
"""

from __future__ import annotations

import os
import re
from typing import Any

_MAX_BODY = 1400  # Format D is short by design (lost-in-the-middle; ≤1 steer/turn)

# Leak-robustness (2026-07-19): ANSI/VT escape sequences (colorized pytest/cargo output —
# tty containers, FORCE_COLOR/PY_COLORS repos) prefix the very lines the strip/keep
# recognizers match as PLAIN text, so a colored banner/summary line evades _RE_SUMMARY /
# _RE_PYTEST_BANNER, falls into the permissive pass, and can carry a ``::nodeid`` through
# the belt. Strip ALL CSI sequences BEFORE any recognition. No-op on colorless input —
# every frozen byte pin is unmoved.
_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MAX_KEEP_LINES = 14

# --- identity classification -------------------------------------------------
# A path is a TEST file if it is one GT actually ran, or matches the standard
# test-file conventions across languages. Fail-closed: when unsure, treat a path
# as test (drop its identity) rather than risk leaking the grader target.
_TEST_BASENAME_RE = re.compile(
    r"(^test_.*)|(.*_test\.(py|go|rb)$)|(.*\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$)"
    r"|(^.*Test\.java$)|(.*_spec\.rb$)|(^conftest\.py$)",
    re.IGNORECASE,
)
# Seam-F7 (bounce 2026-07-10): SINGLE-SOURCE the test-DIR segment set from the delivery
# path policy so this leak predicate and the delivery excluder can NOT diverge. path_policy
# deliberately REMOVED `testing` (Fable P11: production-ambiguous — numpy/testing, Django
# shipped test UTILITIES, Go `testing` helpers are real SOURCE), so a legit `numpy/testing/
# utils.py` fact is no longer whole-dropped at the chokepoint. The local fallback MIRRORS
# path_policy exactly (no `testing`) for the in-container import-absent case. Leak=0 is
# preserved: the basename markers below still catch `testing/test_x.py`, and the `tests`/
# `spec`/etc. segments are unchanged.
try:
    from groundtruth.delivery.path_policy import _TEST_DIR_SEGMENTS as _PP_TEST_DIR_SEGMENTS
except Exception:  # noqa: BLE001 — path_policy absent: local mirror (Fable P11: no `testing`)
    _PP_TEST_DIR_SEGMENTS = frozenset({
        "test", "tests", "__tests__", "__test__", "__tests", "spec", "specs", "e2e"})
_TEST_DIR_RE = re.compile(
    r"(^|/)(" + "|".join(re.escape(s) for s in sorted(_PP_TEST_DIR_SEGMENTS)) + r")(/|$)",
    re.IGNORECASE,
)

# --- firewall line patterns --------------------------------------------------
_RE_CMD_ECHO = re.compile(r"^\s*\$\s")
_RE_PYTEST_BANNER = re.compile(r"^\s*[=_\-]{3,}.*[=_\-]{3,}\s*$")
_RE_DEF_TEST_ECHO = re.compile(r"^\s*(async\s+)?def\s+test\w*\s*\(")
_RE_SUMMARY = re.compile(r"^\s*(FAILED|PASSED|ERROR|SKIPPED)\s")
_RE_PYTEST_NOISE = re.compile(
    r"^\s*(platform\s|rootdir|plugins:|cachedir:|collected\s|configfile:"
    r"|===|test session starts|short test summary)", re.IGNORECASE)
_RE_GO_FAIL_HDR = re.compile(r"^\s*---\s+(FAIL|PASS|SKIP):")
# a `path:line: rest` traceback / message frame (py/go)
_RE_FRAME = re.compile(r"^\s*(?P<path>[\w./\\+\-]+?):(?P<line>\d+):\s?(?P<rest>.*)$")
# a pytest error/assertion line
_RE_E_LINE = re.compile(r"^\s*E\s{2,}(?P<msg>.*)$")
# native value comparisons that carry the delta WITHOUT any identity
_RE_VALUE_LINE = re.compile(
    r"(got\s+.+?\s+want\s+.+)|(want\s+.+?\s+got\s+.+)"
    r"|(Expected:.*)|(Received:.*)|(left:.*)|(right:.*)"
    r"|(assertion\s+`?.*`?\s+failed)|(thread\s+'.*panicked)",
    re.IGNORECASE,
)
# SM-10 Item C (2026-07-12): a RUNNER-AGNOSTIC failure/error marker. The specific
# grammars above (pytest E lines, path:line frames, got/want value deltas) cover the
# common runners; a covering RED emitted by a NON-standard runner (a bespoke test
# harness, a Makefile check, a language whose failure text matches none of them) would
# fall through to the default STRIP on EVERY line -> kept empty -> "" -> the flagship
# EXECUTED world-fact is SWALLOWED (audit finding: native_render default-STRIP at the
# fail-closed tail). This marker rescues those lines in a SECOND, permissive pass that
# runs ONLY when the specific pass kept nothing (so every recognized grammar is
# byte-identical). Leak stays CLOSED: a rescued line is still routed through
# ``_is_test_path`` here and ``_final_scrub`` below (identity-scrub belt).
_RE_GENERIC_FAIL = re.compile(
    r"\b(?:error|errored|fail|failed|failure|failing|assert|assertion|exception"
    r"|panic|panicked|traceback|expected|unexpected|actual|mismatch|not\s+ok"
    r"|fatal|abort|aborted|did\s+not|does\s+not|doesn't|wasn't|isn't)\b"
    r"|!=|<>|≠",
    re.IGNORECASE,
)
# A bare runner COUNT-SUMMARY line ("1 failed", "3 passed, 1 failed in 0.34s", "= 2 errors =")
# — it carries a generic-fail WORD but ZERO fix-signal (no error class, no value, no frame),
# so the permissive pass must STILL skip it (parity with _RE_SUMMARY on the ``FAILED node``
# form). Anchored on a leading integer + status word so a real detail line ("assertion failed",
# "Check FAILED: produced 5") — which starts with a word, not a digit — is NOT skipped.
_RE_COUNT_SUMMARY = re.compile(
    r"^\s*=*\s*(?:\d+\s+(?:passed|failed|error|errors|skipped|deselected|warning|warnings"
    r"|xfailed|xpassed)\b[\s,]*)+(?:in\s+[\d.]+\s*s.*)?=*\s*$",
    re.IGNORECASE,
)


_RE_CASE_TOKEN = re.compile(r"\b(test_\w+|Test\w+|\w+_test|it_\w+)\b")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./").strip()


_RE_CARGO_THREAD = re.compile(r"thread\s+'[^']*'")
_RE_RUST_TESTPATH = re.compile(r"\b[\w]+::tests?::[\w:]+")
_RE_ANY_NODEID = re.compile(r"([\w./\\+\-]+\.(?:py|go|rs|js|jsx|ts|tsx|rb|java))::[\w:.\[\]\-]+")

# --- CANONICAL PROSE leak predicate (single-source; Fable-LIPI round-2, 2026-07-11) --------
# `contains_test_identity` above is a runner-TRANSCRIPT belt-check (nodeid / test-file path /
# `def test_` echo) — correct there, because `_strip_case_tokens`/`_final_scrub` already scrub
# bare names. But ISSUE PROSE is not a scrubbed transcript: a SWE-bench issue names a failing
# test or assertion by BARE token, in ANY language convention. BOTH model-facing prose surfaces —
# the seam (`gt_mini_patch._prose_leaks_test_identity`, obligation-resurface) AND the brief
# (`v1r_brief._obligation_is_leaky`) — import THESE so the two screens cannot DRIFT (round-1 tuned
# each to its own examples and each missed a different half; this is the fixpoint). Confirmed
# leaks now caught: `Test_Reconnect`/`TEST_LOGIN` (case+underscore), `assert_eq!` (rust macro),
# `assertEqual` (seam had no assert leg), `utils.test.ts` (JS/TS file), `crate::tests::fn`,
# `#[test]`. Word boundaries keep production near-misses (`contest_handler`, `latest_value`,
# `std::collections`, `shouldRetry`, `Testing*`/`testing*`). Deliberately NOT caught (over-match
# cost > leak severity, would drop the near-misses): JUnit `shouldX`, mocha `it(`.
PROSE_TEST_NAME_RE = re.compile(
    r"(?i:\btest_\w+\b)"                                            # snake, any case: test_x / TEST_X / Test_X
    r"|(?i:\b\w+_test\b)"                                           # suffix, any case: widget_test / FOO_TEST
    r"|\bTest[A-Z]\w*\b"                                            # PascalCase (excl. Testing): TestReconnect
    r"|\btest[A-Z]\w*\b"                                            # camelCase (excl. testing): testShouldReconnect
    r"|\b\w[\w.\-]*\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs)\b"    # JS/TS test file: utils.test.ts
    r"|[\w/\\+\-]+\.[A-Za-z0-9_]+::[\w.\[\]\-]+"                    # pytest/go nodeid: a/b.py::TestX
    r"|\b\w+::tests?::\w"                                           # rust test path: crate::tests::fn
    r"|\btests?[\\/]"                                               # test path segment: tests/ or test/
    r"|\#\[\s*tests?\b"                                             # rust attribute: #[test]
    r"|\b(?:it|describe|context)\(\s*['\"]"                         # JS/mocha BDD decl: it('...')
)
# Deliberately NOT a leg: bare JUnit-style `shouldReturnX` (camelCase `should\w+`). It collides
# with legitimate production booleans on the near-miss keep-list (`shouldRetry`/`shouldClose`) and
# with spaced behavioral prose an obligation SHOULD carry — catching it over-drops real obligations.
# A discriminator (should + >=3 camel segments) is heuristic; gate that on real DeepSWE issue data,
# not a guess. Residual documented in the LIPI report.
# assertion CALL/macro: unittest `assertEqual`, rust `assert_eq!`/`assert_ne!`/`assert!`, a bare
# `assert(` call. Fable-LIPI round-2 brief Finding-2 (2026-07-11): the tail is now MANDATORY (the
# `?` is gone) so the bare RFC-2119/EARS verb "assert" — the exact requirement grammar the
# obligations extractor TARGETS ("the code must assert that …") — is RELEASED, not whole-dropped,
# while every real assertion form is still caught: `_\w+`→assert_eq!, `[A-Z]\w*`→assertEqual,
# `!`→assert!, `\s*\(`→assert(. `asserts`/`assertion` never match (a word char follows `assert`,
# so none of the four legs fire). Leak-safe: a bare verb carries ZERO test identity.
PROSE_ASSERT_RE = re.compile(r"\bassert(?:_\w+|[A-Z]\w*|!|\s*\()")


def prose_leaks_test_identity(text: str) -> bool:
    r"""True iff PROSE ``text`` carries a test NAME (5 language conventions), a nodeid, a rust test
    path/attribute, an assertion call/macro, OR a test-DIR file path in ANY convention. For
    model-facing PROSE surfaces (issue text / obligations) — NOT runner transcripts (use
    ``contains_test_identity`` there). Fail-closed by construction: the two canonical regexes PLUS
    the path belt are the ONE source both the seam and the brief screen with, so neither surface
    can leak a class the other catches.

    Fable-LIPI round-2 seam Finding-1 (2026-07-11): the regex dir leg is only ``\btests?[\\/]`` — it
    MISSED ``spec/`` ``specs/`` ``__tests__/`` ``e2e/`` etc., so the brief leaked those file paths
    at step 0 while the seam (which screens with ``_payload_leaks_test_identity``) dropped them —
    the two prose screens DRIFTED. ``contains_test_identity`` routes each path token through
    ``_is_test_path`` -> ``_TEST_DIR_RE`` built from path_policy's FULL ``_TEST_DIR_SEGMENTS``, so
    folding it in single-sources the dir belt with path_policy (the authority) and auto-tracks
    future segment additions. It fires only on tokens carrying a source extension, so bare
    production prose (``std::collections``, ``manifest.json``) is untouched."""
    t = text or ""
    return bool(
        PROSE_TEST_NAME_RE.search(t)
        or PROSE_ASSERT_RE.search(t)
        or contains_test_identity(t)
    )


def _strip_case_tokens(msg: str) -> str:
    """Replace test-function-name tokens with <test> so a value/error MESSAGE that
    a test-file line carries (go/cargo print the delta there) can be kept without
    leaking the test's identity."""
    return _RE_CASE_TOKEN.sub("<test>", msg)


# a `path[:line[:col]]` location token (any of the source exts) — used to scrub an EMBEDDED
# test-file path from a kept value/message line (Fable-LIPI round-2 Invariant-1, 2026-07-11).
_RE_PATH_LOC = re.compile(
    r"([\w./\\+\-]+\.(?:py|go|rs|js|jsx|ts|tsx|rb|java))(?::\d+){0,2}")


def _scrub_test_path_tokens(line: str, test_files: set[str]) -> str:
    """Replace any embedded ``path[:line[:col]]`` token whose FILE is a test file — by the ran-set
    ``test_files`` OR by convention (``_is_test_path`` -> path_policy segments) — with ``<test>``.
    Closes the mid-line leak the whole-line ``_is_test_path`` guard misses: a kept value/panic line
    (``right: 3', tests/x.rs:88:5``) keeps its delta but loses the covering test's path. Honors the
    ran-set so a covering test in a NON-conventional dir (``qa/``) is caught too; a NON-test source
    frame (``src/pool.rs`` — the where-to-fix) is left untouched."""
    return _RE_PATH_LOC.sub(
        lambda m: "<test>" if _is_test_path(m.group(1), test_files) else m.group(0), line)


# Strips BOTH an opening (``<gt-x ...>``) AND a closing (``</gt-x>``) GT tag. The module-level
# ``_RE_GT_TAG`` (used by the byte-frozen ``render_syntax_error_native``) matches only the opening
# form; this catches both so a field-injected ``<gt-fact>…</gt-fact>`` can never survive
# ``_final_scrub`` onto a model-facing surface (SM-1 LIPI close-vector 2, 2026-07-11).
_RE_GT_TAG_ANY = re.compile(r"</?gt-[^>]*>", re.IGNORECASE)


def _final_scrub(line: str, test_files: set[str] | None = None) -> str:
    """Fail-closed belt applied to EVERY kept line: no ``<gt-*>`` tag, no cargo thread-quote, no
    rust ``::tests::`` path, no ``file::nodeid``, no embedded test-DIR/ran-set path, no test-name
    token survives. Guarantees BOTH contains_test_identity(output, test_files) is False AND
    contains_gt_tag(output) is False regardless of runner quirks OR a field-injected tag —
    including a test path sitting MID-LINE in a kept value comparison (Invariant-1 fix).

    Byte-identity note (SM-1 LIPI close-vector 2): the ``_RE_GT_TAG_ANY`` strip is a NO-OP on the
    frozen ``render_covering_failure_native`` path — anonymized runner tokens never contain
    ``<gt-`` — so the covering byte pins are unmoved (proven, not assumed)."""
    line = _RE_ANSI.sub("", line)   # belt: a colorized token can never mask an identity
    line = _RE_GT_TAG_ANY.sub("", line)
    line = _RE_CARGO_THREAD.sub("thread", line)
    line = _RE_RUST_TESTPATH.sub("<test>", line)
    line = _RE_ANY_NODEID.sub(r"\1", line)
    line = _scrub_test_path_tokens(line, test_files or set())
    return _strip_case_tokens(line)


def _is_test_path(path: str, test_files: set[str]) -> bool:
    n = _norm(path)
    if not n:
        return False
    if n in test_files or os.path.basename(n) in {os.path.basename(t) for t in test_files}:
        return True
    if _TEST_DIR_RE.search("/" + n):
        return True
    return bool(_TEST_BASENAME_RE.match(os.path.basename(n)))


def _tail(text: str, limit: int = _MAX_BODY) -> str:
    text = (text or "").rstrip("\n")
    return text if len(text) <= limit else text[-limit:]


def _frame_under_repo(path: str, repo_root: str | None) -> bool:
    """D-K (run6 audit): a covering-RED 'where-to-fix' frame must be a real repo
    file. A third-party dep frame (pyasn1/codec/ber/encoder.py while fixing
    privacyidea) is not the agent's source — delivering it as the fix site is
    false. When repo_root is known, require the frame to resolve to a file under
    it; unknown repo_root keeps the legacy behavior (backward-compatible).
    """
    if not repo_root:
        return True  # no root available -> cannot gate; legacy behavior
    p = (path or "").replace("\\", "/")
    if p.startswith("<"):
        return False  # pseudo-file (<stdin>, <string>, <frozen ...>)
    try:
        if os.path.isabs(p):
            rr = os.path.realpath(repo_root)
            return os.path.realpath(p).startswith(rr.rstrip("/") + "/")
        return os.path.isfile(os.path.join(repo_root, p))
    except (OSError, ValueError):
        return False


def render_covering_failure_native(
    result: dict[str, Any],
    *,
    edited_symbol: str | None = None,
    test_files: list[str] | set[str] | None = None,
    repo_root: str | None = None,
) -> str:
    """Format D: the anonymized native failure block. Returns "" when nothing
    signal-bearing survives the firewall (correct-or-quiet)."""
    if not result:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    raw = ((result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or ""))
    # Leak-robustness: de-colorize BEFORE any recognition (see _RE_ANSI above).
    raw = _RE_ANSI.sub("", raw)
    kept: list[str] = []
    for line in raw.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        # --- STRIP: pure test-identity / ceremony -------------------------
        if (_RE_CMD_ECHO.match(s) or _RE_PYTEST_BANNER.match(s)
                or _RE_DEF_TEST_ECHO.match(s) or _RE_SUMMARY.match(s)
                or _RE_PYTEST_NOISE.match(s) or _RE_GO_FAIL_HDR.match(s)):
            continue
        # --- KEEP: pytest error / assertion line (scrubbed) ---------------
        m = _RE_E_LINE.match(s)
        if m:
            msg = m.group("msg").strip()
            # drop an E line that echoes the test's own call/expression identity
            if _is_test_path(msg, tf) or _RE_DEF_TEST_ECHO.match(msg):
                continue
            kept.append(msg)
            continue
        # --- frame / message line: path:line: rest ------------------------
        fm = _RE_FRAME.match(s)
        if fm:
            path, rest = fm.group("path"), (fm.group("rest") or "").strip()
            if _is_test_path(path, tf):
                # test frame — strip its identity (the path:line prefix + the
                # `in <test_fn>` continuation), but KEEP a value/error MESSAGE it
                # carries (go/cargo print the delta on the test-file line),
                # scrubbed of any test-name token.
                if rest and not rest.startswith("in ") and not _RE_DEF_TEST_ECHO.match(rest):
                    msg = _strip_case_tokens(rest).strip()
                    if msg:
                        kept.append(msg)
                continue
            # D-K: a third-party dep / pseudo-file frame is NOT the agent's source;
            # never deliver it as the where-to-fix (correct-or-quiet).
            if not _frame_under_repo(path, repo_root):
                continue
            # agent's OWN source frame — the where-to-fix. Reformat, keep.
            fn = ""
            mfn = re.search(r"\bin\s+(\w+)", rest)
            if mfn:
                fn = f", in {mfn.group(1)}"
            kept.append(f"  at {_norm(path)}:{fm.group('line')}{fn}")
            continue
        # --- KEEP: identity-free native value comparison ------------------
        if _RE_VALUE_LINE.search(s):
            if not _is_test_path(s, tf):
                kept.append(s.strip())
            continue
        # default: STRIP (fail closed — only explicit signal tokens pass)
    # SM-10 Item C (2026-07-12): SECOND, permissive pass — runs ONLY when the specific
    # recognizers above kept nothing, so every recognized grammar is byte-identical (a
    # covering RED that already produced a block is untouched). It rescues a real RED whose
    # runner grammar matched none of the specific patterns (bespoke harness / uncommon
    # language) instead of fail-closing to "". Leak stays CLOSED: the same ceremony strip,
    # the `_is_test_path` whole-line guard, and the `_final_scrub` belt below apply.
    if not kept:
        for line in raw.splitlines():
            s = line.rstrip()
            if not s.strip():
                continue
            if (_RE_CMD_ECHO.match(s) or _RE_PYTEST_BANNER.match(s)
                    or _RE_DEF_TEST_ECHO.match(s) or _RE_SUMMARY.match(s)
                    or _RE_PYTEST_NOISE.match(s) or _RE_GO_FAIL_HDR.match(s)
                    or _RE_COUNT_SUMMARY.match(s)):
                continue
            if _RE_GENERIC_FAIL.search(s) and not _is_test_path(s, tf):
                kept.append(s.strip())
        kept = kept[:_MAX_KEEP_LINES]  # bound the permissive pass (a noisy log can't blow up)
    if not kept:
        return ""
    # final fail-closed identity scrub on EVERY line, then de-dupe + cap.
    out: list[str] = []
    for ln in kept:
        ln = _final_scrub(ln, tf)
        if ln.strip() and (not out or out[-1] != ln):
            out.append(ln)
    out = out[:_MAX_KEEP_LINES]
    # §0 native voice: the environment reports the failure, GT never speaks in its
    # own voice ("GT ran ...") on a model-facing surface.
    head = (f"Your change to `{edited_symbol}()` fails a covering test:"
            if edited_symbol else "A covering test fails:")
    return _tail(head + "\n" + "\n".join(out))


_RE_GT_TAG = re.compile(r"<gt-[^>]*>", re.IGNORECASE)


def render_syntax_error_native(result: dict[str, Any]) -> str:
    """Render a ``check_edit_syntax`` syntax_error as the toolchain's OWN native
    diagnostic — the compiler/parser output the model was RL-trained to fix on
    (Python's ``File "x.py", line N … SyntaxError: …``; gofmt's ``x.go:5:1: expected
    …``; ``node --check``'s SyntaxError). §0 native voice: the environment reports the
    parse failure; GT adds NO voice, NO ``<gt-*>`` tag, NO head.

    Correct-or-quiet: "" for any verdict other than ``syntax_error`` and for an empty
    diagnostic. The engine already bounds + ``<gt-*>``-scrubs the diagnostic; this
    re-applies the model-facing invariants (tag-strip + bound) as defense in depth.
    Leak-safety ENFORCED here too (W5-sweep, 2026-07-17): both live callers already
    guard test-path edits, but the renderer itself now runs ``_final_scrub`` per line
    so leak-safety never again rests on a caller contract alone. On the callers'
    real inputs (non-test source diagnostics) the scrub is a byte no-op."""
    # AUDIT 2026-07-24: accept the GT_EDIT_CHECK_NAMES `name_error` verdict (a definite undefined-name
    # diagnostic from pyflakes — the SAME executed-toolchain-diagnostic class) so it goes through the
    # SAME tag-strip + per-line _final_scrub + bound as a syntax_error (defense-in-depth leak guard),
    # instead of a caller-side raw fallback that skips the scrub. Byte-identical off (name_error never
    # produced unless GT_EDIT_CHECK_NAMES is set).
    if not result or result.get("verdict") not in ("syntax_error", "name_error"):
        return ""
    diag = _RE_GT_TAG.sub("", (result.get("diagnostic") or "")).strip()
    if not diag:
        return ""
    diag = "\n".join(_final_scrub(ln) for ln in diag.splitlines())
    return _tail(diag, _MAX_BODY).strip()


def deepest_agent_frame(result: dict[str, Any], test_files: list[str] | set[str] | None = None
                        ) -> tuple[str, int] | None:
    """The deepest NON-test traceback frame (agent's own source) as (path, line),
    or None. Used by the attribution gate (was this red caused by the edit)."""
    tf = {_norm(t) for t in (test_files or [])}
    raw = ((result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or ""))
    raw = _RE_ANSI.sub("", raw)   # colorized frames must still attribute (leak-robustness class)
    found: tuple[str, int] | None = None
    for line in raw.splitlines():
        fm = _RE_FRAME.match(line.rstrip())
        if not fm:
            continue
        path = fm.group("path")
        if _is_test_path(path, tf):
            continue
        try:
            found = (_norm(path), int(fm.group("line")))  # last (deepest) wins
        except ValueError:
            continue
    return found


def _edit_frame_path_match(frame_path: str, edited_path: str) -> bool:
    """True iff a (normalized) traceback frame names a (normalized) edited file:
    exact path equality, or one path ending with ``"/" + other`` — a suffix
    ANCHORED at a path separator whose shorter side itself carries a directory
    segment. NEVER bare basename equality: a single-segment path can only match
    exactly, so a dependency's same-basename file never attributes (W2-R3)."""
    if not frame_path or not edited_path:
        return False
    if frame_path == edited_path:
        return True
    shorter, longer = (
        (frame_path, edited_path)
        if len(frame_path) <= len(edited_path)
        else (edited_path, frame_path)
    )
    return "/" in shorter and longer.endswith("/" + shorter)


def is_edit_attributed(
    result: dict[str, Any],
    edited_files: set[str] | list[str],
    *,
    test_files: list[str] | set[str] | None = None,
) -> bool:
    """Attribution gate (Fable §5, MANDATORY): deliver a covering RED only when the
    edit plausibly CAUSED it — the deepest agent-source frame lands in a file the
    agent just edited. GT cannot see FAIL_TO_PASS, so this is causation-by-frame,
    not target-matching. Correct-or-quiet: no agent frame -> not attributed.

    Frames-only by design (pure, no environment reads): a CRASH carries an
    agent-source frame; an ASSERTION failure does not. The value-failure case is
    handled by ``covering_runner.is_red_attributable`` (frames FIRST, green->base->
    red DIFFERENTIAL second) — the ONE question the seam should ask.

    PATH-ANCHORED, never basename (W2-R3 fix, 2026-07-29): the old basename
    fallback attributed a RED whose deepest frame was a DEPENDENCY file merely
    sharing a basename with an edited file (``site-packages/somedep/utils.py``
    vs the agent's ``src/mypkg/utils.py``) — false edit-blame shipped as
    Format-D. Attribution now requires an exact normalized-path match or a
    ``/``-anchored path-suffix match (:func:`_edit_frame_path_match`), which
    keeps the honest container-absolute-frame case (``/testbed/<edited_rel>``)
    without the basename collision. Unattributed stays quiet downstream."""
    ef = {_norm(f) for f in (edited_files or [])}
    if not ef:
        return False
    frame = deepest_agent_frame(result, test_files)
    if frame is None:
        return False
    fp = frame[0]
    return any(_edit_frame_path_match(fp, e) for e in ef)


def render_submit_rejection(reason: str, detail: str = "") -> str:
    """A submit refusal as a pre-commit / CI style failure (native, in-distribution).
    Staged for B2 (the submit gate); the model reacts to a failed action + retries.

    W5-sweep enforcement (2026-07-17): ``detail`` forwards gate_verdict hygiene text
    that embeds agent-diff PATHS (``refusing to commit binary artifact(s): tests/x``),
    and this renderer's fallback delivery path has no contains_test_identity
    chokepoint backstop — so the firewall is enforced HERE, not asserted of callers.
    Non-test reasons/details are byte-identical (``_final_scrub`` is a no-op)."""
    lines = ["pre-commit hook failed:"]
    if reason:
        lines.append(_final_scrub(reason))
    if detail:
        lines.append(_final_scrub(_tail(detail)))
    lines.append("commit aborted (exit 1)")
    return "\n".join(lines)


def render_ss_submit_red(test_command: str) -> str:
    """SS-2 (GT_SS_SUBMIT_RED, 2026-07-13): a pre-submit refusal that consumes the agent's
    OWN unresolved test RED — the conan-17092 class (the agent observed a gold-relevant test
    FAIL, rationalized it away, and submitted a clean run). Native pre-commit/CI shape, ZERO
    ``<gt-*>`` tag.

    LEAK-SAFE BY ENFORCEMENT (W5, live leak run 29594276655): the caller passes the agent's
    own observed test COMMAND — but that command essentially always NAMES the failing test
    file (``pytest tests/x/test_y.py``), and a hidden-test FILE identity on a GT-delivered
    surface violates the structural leak=0 invariant even when self-acquired (both live
    firings, haystack-8997 + llama-factory-7505, leaked exactly this way). So the echoed
    command now passes the SAME ``_final_scrub`` firewall every sibling renderer uses —
    the test-path token becomes ``<test>`` while the RED-status signal survives (the agent
    has its own concrete command in its history). Belt: if identity still survives the
    scrub, the renderer goes silent rather than leak (correct-or-quiet)."""
    cmd = (test_command or "").strip()
    if not cmd:
        return ""
    cmd = _final_scrub(_tail(cmd, 200))
    if contains_test_identity(cmd):
        return ""
    return (
        "pre-commit hook failed:\n"
        f"pre-submit check: `{cmd}` was last observed FAILING and never re-run green\n"
        "resolve the failing test (or state why it is unrelated) before submitting\n"
        "commit aborted (exit 1)"
    )


def contains_gt_tag(text: str) -> bool:
    """Stage-1 guard: True if a GT-voice tag leaked onto a model-facing surface."""
    return "<gt-" in (text or "").lower()


def contains_test_identity(text: str, test_files: list[str] | set[str] | None = None) -> bool:
    """Stage-1 leak guard: True if any test-file path, ``::nodeid``, or ``def test_``
    echo survived onto the model-facing surface. Format D must always be False."""
    t = text or ""
    # Seam-F7 (bounce 2026-07-10): the `::` test is NARROWED to a real source-file nodeid
    # (`file.py::node`, `_RE_ANY_NODEID`) plus a rust module test-path (`crate::tests::fn`,
    # `_RE_RUST_TESTPATH`). The prior broad `\.\w+::` flagged ANY dotted `::` — false-
    # positive on production dotted paths (Rust `std.io.Stdout::lock`) that carry 0 test
    # identity, so a legit fact was whole-dropped at the chokepoint. This is stricter on
    # false-positives AND catches MORE real leaks (adds the rust `::tests::` form the old
    # `\.\w+::` missed) — net leak coverage improves, not regresses.
    if _RE_ANY_NODEID.search(t) or _RE_RUST_TESTPATH.search(t):
        return True
    if _RE_DEF_TEST_ECHO.search(t):
        return True
    for ln in t.splitlines():
        for tok in re.findall(r"[\w./\\+\-]+\.(?:py|go|rs|js|jsx|ts|tsx|rb|java)\b", ln):
            if _is_test_path(tok, {_norm(x) for x in (test_files or [])}):
                return True
    return False


# ===========================================================================
# SUPER MODE — per-fact-class NATIVE renderers (SM-1, 2026-07-11)
# ===========================================================================
# "Tag-free" is NOT "native". Stripping `<gt-fact>` off "co-changed with the edit
# in 3 commits" leaves GT NARRATION the RL policy was never trained to consume.
# Only output shaped like a REAL tool channel is native. Each renderer below names
# the tool channel it imitates, is PURE / deterministic / LLM-free, is
# correct-or-quiet ("" on unclear input — NEVER narration), and is identity-
# firewalled so ``contains_test_identity(out) is False`` and no ``<gt-`` tag
# survives. These are ADDITIVE — the three existing renderers above
# (render_covering_failure_native / render_syntax_error_native /
# render_submit_rejection) are byte-unchanged.
# ---------------------------------------------------------------------------


def _int_or_none(v: Any) -> int | None:
    """``int(v)`` or ``None`` — never raises (correct-or-quiet gate for a line/count)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Per-field length cap applied BEFORE a field reaches ``_final_scrub`` (SM-1 LIPI close-vector 3,
# 2026-07-11). ``_RE_PATH_LOC`` / ``_RE_ANY_NODEID`` backtrack O(n²) on a long DOTLESS token
# (~2s at 16k, hangs at 100k), so an un-bounded structured field would turn the renderers'
# correct-or-quiet contract into a hang. A real path/symbol/func is far under the cap; a
# pathological-length field is bounded (returns quiet-shaped), never a hang.
_FIELD_CAP = 512


def _cap(s: Any) -> str:
    """Coerce to ``str`` and bound to ``_FIELD_CAP`` chars — the ReDoS guard for a structured
    field. ``None``/non-str -> ``""`` (correct-or-quiet)."""
    if s is None:
        return ""
    return str(s)[:_FIELD_CAP]


def render_trace_frame_native(
    path: str,
    line: Any,
    func: str | None = None,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``trace`` (``trace_frame``) — a native TRACEBACK FRAME line, the
    interpreter/runtime's OWN crash grammar the model was RL-trained to read:
      * Python:    ``  File "<path>", line N, in <fn>``  (CPython traceback frame)
      * go/rust/…: ``<path>:N``                          (compiler/panic location)
    The FORM is chosen by the source extension (``.py``/``.pyi`` -> CPython frame;
    else the ``path:line`` location token). This is the deepest IN-REPO frame — the
    where-to-fix — NOT the GT narration ``deepest in-repo frame: …`` (which is prose
    the policy never consumed).

    Identity-firewalled: a TEST-file frame is quiet ("") — GT never points the model
    at the grader target — and every kept line is run through :func:`_final_scrub`, so
    ``contains_test_identity(out) is False`` holds. Correct-or-quiet: a missing path or
    an unparseable line -> "".
    """
    n = _norm(_cap(path))
    if not n:
        return ""
    ln = _int_or_none(line)
    if ln is None:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    if _is_test_path(n, tf):
        return ""  # never a test frame (identity firewall)
    if n.endswith(".py") or n.endswith(".pyi"):
        fn = _cap(func).strip()
        out = f'  File "{n}", line {ln}, in {fn}' if fn else f'  File "{n}", line {ln}'
    else:
        out = f"{n}:{ln}"
    return _final_scrub(out, tf)


def render_signature_delta_native(
    caller_file: str,
    caller_line: Any,
    symbol: str,
    *,
    expected_min: Any,
    expected_max: Any,
    given: Any,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``signature_delta`` — a TYPE-CHECKER / COMPILER DIAGNOSTIC, the
    exact grammar mypy / gopls / tsc / gcc emit for an arity mismatch:
      ``<caller_file>:<L>: error: <fn>() takes N positional argument(s) but M given``
    (universal ``path:line: error: <msg>`` diagnostic prefix). This is the channel
    the policy was RL-trained to FIX ON — NOT the prose "the signature changed to …"
    / "call passes M positional arg(s)" narration.

    ``N`` collapses to a single count when ``expected_min == expected_max``, else a
    ``min-max`` range. Identity-firewalled (a test-file caller is quiet) + scrubbed;
    correct-or-quiet on a missing symbol/file or an unparseable line/count.
    """
    cf = _norm(_cap(caller_file))
    sym = _cap(symbol).strip()
    if not cf or not sym:
        return ""
    cl = _int_or_none(caller_line)
    lo = _int_or_none(expected_min)
    hi = _int_or_none(expected_max)
    m = _int_or_none(given)
    if cl is None or lo is None or hi is None or m is None:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    if _is_test_path(cf, tf):
        return ""
    n = str(lo) if lo == hi else f"{lo}-{hi}"
    out = f"{cf}:{cl}: error: {sym}() takes {n} positional argument(s) but {m} given"
    return _final_scrub(out, tf)


def render_caller_contract_native(
    symbol: str,
    n_callers: Any,
    n_files: Any,
    *,
    def_file: str | None = None,
    sig_delta: str = "",
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``caller_contract`` (gateway ``caller_break``) — a COMPILER / TYPE-CHECKER
    CONTRACT DIAGNOSTIC, the ``path: error: <msg>`` grammar gcc / gopls / tsc / mypy emit when
    an edit breaks a symbol's public contract:
      ``<def_file>: error: <symbol>() signature changed; N caller(s) in M file(s) must update the call sites``
    (the universal ``path: error: <msg>`` diagnostic prefix; ``: error:`` because a
    signature change that breaks callers is a contract VIOLATION, not a hint). This is the
    channel the policy was RL-trained to FIX ON — NOT the tagged ``[CALLERS] N verified
    caller(s) ... — preserve this interface`` narration the legacy Lane-A block shipped.

    This is the CROSS-LANGUAGE caller-break the graph-based gateway producer delivers on a
    signature-changing edit (Go/Rust/TS/JS as well as Python) — the honest §26.4 replacement
    for the retired l3.contract/l3b.evidence that patch_delta (ast-only) cannot produce.

    ``N``/``M`` collapse to their integers; ``def_file`` is the edited symbol's own file (the
    where-the-change-is). ``sig_delta`` (optional, default "" -> byte-identical to the prior
    two-arg call) is the ``old→new`` parameter-name delta the W-B19 verified-signature-change
    gate derives; when non-empty it is inlined as ``signature changed (<sig_delta>)`` so the
    diagnostic carries WHICH params changed (the seam GT_CONTRACT_NATIVE arm passes it). It is
    ``_cap``-bounded before scrubbing (ReDoS guard) and firewalled by :func:`_final_scrub` like
    every other field, so a test identity smuggled into a delta still cannot cross.
    Identity-firewalled: a TEST-file ``def_file`` is quiet ("") — GT never points the model at a
    grader target — and the line is run through :func:`_final_scrub`, so
    ``contains_test_identity(out) is False`` holds. Correct-or-quiet: a missing symbol, a
    non-positive caller/file count, or an unparseable count -> "".
    """
    sym = _cap(symbol).strip()
    nc = _int_or_none(n_callers)
    nf = _int_or_none(n_files)
    if not sym or nc is None or nf is None or nc <= 0 or nf <= 0:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    df = _norm(_cap(def_file))
    if df and _is_test_path(df, tf):
        return ""  # never name a test file as the edit site (identity firewall)
    delta = _cap(sig_delta).strip()
    changed = f"signature changed ({delta})" if delta else "signature changed"
    msg = (f"{sym}() {changed}; {nc} caller(s) in {nf} file(s) "
           f"must update the call sites")
    out = f"{df}: error: {msg}" if df else f"error: {msg}"
    return _final_scrub(out, tf)


_CALLER_USAGE_NATIVE_PHRASES: dict[str, str] = {
    "boolean_check": "boolean-checked",
    "destructure_tuple": "unpacked into multiple values",
    "exception_guard": "used inside an exception guard",
    "iterated": "iterated (expects an iterable)",
}


def render_caller_usage_native(
    caller_file: str,
    caller_line: Any,
    symbol: str,
    usage_kind: str,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    """Render one typed caller-consumption fact as a compiler ``note:``.

    Only the finite usage vocabulary emitted by gt-index's AST-parent
    classifier has authority.  Unknown/malformed rows are quiet; no free-form
    call-site text reaches the model observation.
    """
    path = _norm(_cap(caller_file))
    sym = _cap(symbol).strip()
    line = _int_or_none(caller_line)
    phrase = _CALLER_USAGE_NATIVE_PHRASES.get(_cap(usage_kind).strip())
    if not path or not sym or line is None or line <= 0 or phrase is None:
        return ""
    tf = {_norm(item) for item in (test_files or [])}
    if _is_test_path(path, tf):
        return ""
    return _final_scrub(
        f"{path}:{line}: note: {sym}() result is {phrase}", tf,
    )


def render_registration_native(
    file: str,
    line: Any,
    symbol: str,
    siblings: list[str] | tuple[str, ...] | None,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-classes ``registration`` / ``companion_surface`` — a LINTER DIAGNOSTIC in
    the ruff / eslint / golangci-lint voice:
      ``<file>:<L>: warning: registers <X, Y> but not '<symbol>'``
    (the universal ``path:line: warning: <msg>`` linter prefix). A registration/
    companion surface that lists its siblings but omits the just-added symbol is a
    lint finding, not the bare-prose "registers siblings X, Y but not 'Z'".

    Identity-firewalled + scrubbed; correct-or-quiet on a missing symbol, an empty
    sibling set, or an unparseable line.
    """
    fp = _norm(_cap(file))
    sym = _cap(symbol).strip()
    sibs = [_cap(s).strip() for s in (siblings or []) if s and str(s).strip()]
    if not fp or not sym or not sibs:
        return ""
    ln = _int_or_none(line)
    if ln is None:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    if _is_test_path(fp, tf):
        return ""
    out = f"{fp}:{ln}: warning: registers {', '.join(sibs)} but not '{sym}'"
    return _final_scrub(out, tf)


def render_def_rows_native(
    rows: Any,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``def_partition`` — a RIPGREP ROW block: one ``<path>:<line>:<sym>``
    per definition site (ripgrep's own ``path:line:match`` grammar), so the answer
    rides the agent's OWN grep channel in-distribution. A shared primitive next to the
    seam's ``gt_mini_patch._fmt_def_facts_native`` (same row grammar) for the gateway's
    ``def_partition`` path.

    ``rows`` is an iterable of ``(path, line, sym)``. A TEST-file row is dropped
    (identity firewall) and every emitted row is scrubbed. Correct-or-quiet: no
    signal-bearing row -> "".
    """
    tf = {_norm(t) for t in (test_files or [])}
    out: list[str] = []
    try:
        it = list(rows or [])
    except TypeError:
        return ""
    for row in it:
        try:
            path, line, sym = row
        except (TypeError, ValueError):
            continue
        n = _norm(_cap(path))
        s = _cap(sym).strip()
        ln = _int_or_none(line)
        if not n or not s or ln is None:
            continue
        if _is_test_path(n, tf):
            continue  # ripgrep would show it, but GT never surfaces a test row
        row_s = _final_scrub(f"{n}:{ln}:{s}", tf)
        if row_s.strip():
            out.append(row_s)
    return "\n".join(out)


def render_note_rows_native(
    rows: Any,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""D-S (the "0-consumption" root): the SAME def_partition / caller rows as
    ``render_def_rows_native``, but in the COMPILER-``note:`` grammar RL models have
    seen from every compiler/linter — ``<path>:<line>: note: <sym> — verify your
    change is consistent here`` — instead of the bare ``<path>:<line>:<sym>`` grep
    rows, which came back RL-inert (passive grep-shaped content on a native pipe).
    The ``path:line`` stays LEADING (grep-compatible, entity-present for SS-0, and
    still parseable as the caller file), and this renderer INHERITS the EXACT
    identity firewall (``_is_test_path`` drop + ``_final_scrub`` per row) so leak
    safety is identical to the bare form. Relationship-agnostic wording
    (correct-or-quiet: it never overclaims "caller"/"signature" for a witness/def
    row). ``rows`` is an iterable of ``(path, line, sym)``.
    """
    tf = {_norm(t) for t in (test_files or [])}
    out: list[str] = []
    try:
        it = list(rows or [])
    except TypeError:
        return ""
    for row in it:
        try:
            path, line, sym = row
        except (TypeError, ValueError):
            continue
        n = _norm(_cap(path))
        s = _cap(sym).strip()
        ln = _int_or_none(line)
        if not n or not s or ln is None:
            continue
        if _is_test_path(n, tf):
            continue  # ripgrep would show it, but GT never surfaces a test row
        row_s = _final_scrub(
            f"{n}:{ln}: note: {s} - verify your change is consistent here", tf)
        if row_s.strip():
            out.append(row_s)
    return "\n".join(out)


def render_ranked_list_native(
    rows: Any,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``localization`` (T0->T2 re-slot, 2026-07-12) — GT's RANKED localization
    ANSWER as a RIPGREP ROW block: one ``<path>:<line>:<sym>`` per ranked candidate FILE
    (ripgrep's own ``path:line:match`` grammar), so the answer rides the agent's OWN grep
    channel in-distribution at the POST-SEARCH boundary. This is the registry-DECLARED
    ``ranked-list`` renderer (fact_registry ``localization.native_renderer``) that did not
    previously exist.

    HEDGE-FREE by construction: the native voice is the environment LISTING candidate files
    — there is NO "confirm the edit target with grep" prose (that hedge is correct at the T0
    brief, but the T2 rows ARE the grep channel, so re-confirmation prose is out of voice).

    ``rows`` is an iterable of ``(path, line, sym)``. The identity firewall is INHERITED
    VERBATIM from :func:`render_def_rows_native` (per-row ``_is_test_path`` DROP +
    ``_final_scrub``), not re-implemented — a test-file candidate is never a row and no
    ``<gt-*>`` tag / ``::nodeid`` survives, so ``contains_test_identity(out) is False`` holds.
    Correct-or-quiet: no signal-bearing row -> "".
    """
    return render_def_rows_native(rows, test_files=test_files)


def render_related_files_native(
    relations: Any,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    """Render certified scope relations as hedged search-time continuations.

    These rows share the localization dose; they are suggestions to inspect, not
    edit obligations.  The producer owns certification and revision binding.  The
    renderer independently enforces the normal path-identity firewall.
    """
    tf = {_norm(t) for t in (test_files or [])}
    out: list[str] = []
    for relation in relations or ():
        path = _norm(_cap(getattr(relation, "related_file", "")))
        method = str(getattr(relation, "resolution_method", "") or "").strip().lower()
        if not path or not method or _is_test_path(path, tf):
            continue
        row = _final_scrub(
            f"{path}: note: related file to inspect (certified {method} relation)", tf)
        if row.strip():
            out.append(row)
    return "\n".join(out)


def render_body_concept_native(
    rows: Any,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``body_concept`` (a post_search ``def_partition`` variant, 2026-07-12) — the
    concept-hit function BODIES as a RIPGREP ROW block: one ``<path>:<line>:<sym>`` per function
    whose BODY mentions the queried concept but whose NAME/PATH did not match (ripgrep's own
    ``path:line:match`` grammar), so a vocabulary-gap answer rides the agent's OWN grep channel
    in-distribution instead of the prose ``N function bodies mention "x" …`` narration.

    ``body_concept`` aliases to the registered ``def_partition`` class whose declared renderer is
    ``grep-native`` (fact_registry), and its hits ARE def sites of concept-bearing bodies, so the
    identity firewall + row grammar are INHERITED VERBATIM from :func:`render_def_rows_native`
    (per-row ``_is_test_path`` DROP + ``_final_scrub``) — not re-implemented. A test-file row is
    never surfaced and no ``<gt-*>`` / ``::nodeid`` survives, so ``contains_test_identity(out) is
    False`` holds. ``rows`` is an iterable of ``(path, line, sym)`` (the symbol = the
    concept-bearing function). Correct-or-quiet: no signal-bearing row -> "".
    """
    return render_def_rows_native(rows, test_files=test_files)


def render_recovery_native(reason: str, imperative: str) -> str:
    """fact-class ``recovery`` — the proven-consumed SHORT IMPERATIVE (the boa
    ``no_test_evidence`` nudge: SHORT · ACTIVE · at the decision point). Native form =
    ONE imperative line, no ``<gt-`` tag, no GT-voice wrapper. ``reason`` is the
    fire-class identity (host-side telemetry); it is NOT emitted onto the model surface.

    Correct-or-quiet: an empty imperative -> "". Any ``<gt-*>`` tag is stripped and the
    text is collapsed to a single line (defense: the native channel is a one-liner, not
    a nudge block). Symmetry with the other 5 renderers (SM-1 LIPI close-vector 1): the
    collapsed text is routed through :func:`_final_scrub` so a test identity embedded in
    the imperative (a nodeid / ``crate::tests::fn`` / ``def test_`` / ``spec/x.js``) can
    NOT leak — ``contains_test_identity(out) is False`` holds here too.
    """
    text = _RE_GT_TAG_ANY.sub("", (imperative or "")).strip()
    if not text:
        return ""
    text = " ".join(text.split())     # collapse to a single imperative line
    text = text[:_MAX_BODY]           # bound BEFORE the O(n²) scrub (ReDoS guard)
    return _final_scrub(text, set())  # symmetry: no test identity survives an imperative


def render_scope_constraint_native(
    must_touch_file: str | None,
    *,
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``scope`` — INTERNAL unless it names a CONCRETE edit decision. When
    the scope fact carries a specific file the edit must ALSO touch, render it as a
    compiler ``note:`` constraint the agent can act on:
      ``<file>: note: your change must also update this file``
    (gcc/clang ``note:`` continuation grammar). When scope names NO decision (no file),
    it is a ranking/internal signal with NO model-facing form -> "" (never the prose
    "scope is broad …").

    Identity-firewalled: never names a test file. Correct-or-quiet.
    """
    n = _norm(_cap(must_touch_file))
    if not n:
        return ""  # scope with no named decision = INTERNAL only (no narration)
    tf = {_norm(t) for t in (test_files or [])}
    if _is_test_path(n, tf):
        return ""
    return _final_scrub(f"{n}: note: your change must also update this file", tf)


def render_cochange_native(*_args: Any, **_kwargs: Any) -> str:
    """fact-class ``cochange`` (``cochange_prior``) — INTERNAL RANKING ONLY. Co-change
    priors bias localization/companion RANKING; they are NOT a delivered model-facing
    fact. There is NO native form for cochange: this function ALWAYS returns "" and
    MUST NEVER emit the "co-changed with the edit in N commits" narration. It exists to
    make the internal-only decision executable + pinned (correct-or-quiet, always
    quiet), so a future wiring that reaches for a cochange renderer stays silent.
    """
    return ""


# The static, LEAK-FREE hook label for each certificate SDLC head (a CompletionCertificate
# ``unresolved_failures`` field name). The covering head reports only its VERDICT (Failed) —
# NEVER a failing-test name (the names ride ``head_record`` for host telemetry, never here).
_CERT_HOOK_LABEL: dict[str, str] = {
    "selected_test_status": "run covering tests",
    "syntax_status": "check syntax",
    "type_status": "type-check",
    "build_status": "build",
    "scope_compliance": "scope guard",
    "reproduction_status": "reproduce issue",
}
_CERT_HYGIENE_LABEL = "diff hygiene"   # the head's hygiene block (not an SDLC field)
_CERT_LEADER_WIDTH = 56                # pre-commit dotted-leader column


def render_completion_cert_native(
    failing_heads: Any,
    *,
    hygiene_blocked: bool = False,
    hygiene_detail: str = "",
    test_files: list[str] | set[str] | None = None,
) -> str:
    r"""fact-class ``submit_refusal`` — the D7 CompletionCertificate rendered as a native
    PRE-COMMIT HOOK FAILURE block, the pre-commit framework's OWN ``<hook id>....Failed``
    grammar the RL policy reads when a commit is refused. ONE line per FAILING certificate
    head — a WORLD-FACT (this check failed), NOT the prose "the certificate reports N
    unresolved failures" and NOT an instruction ("re-run the tests …").

    ``failing_heads``   the certificate's ``unresolved_failures`` (SDLC field names). The
                        covering head reports its VERDICT (Failed), NEVER a test name.
    ``hygiene_blocked`` the head blocked on diff hygiene (not an SDLC field) -> its own line.
    ``hygiene_detail``  the WORLD-FACT reason for a hygiene block (e.g. "vendored file
                        changed"), scrubbed + bounded; "" to omit.

    Identity-firewalled: EVERY emitted line is routed through :func:`_final_scrub`, so a test
    identity that rode a detail string is scrubbed — ``contains_test_identity(out) is False``
    and no ``<gt-*>`` tag survives. Correct-or-quiet: a CLEAN certificate (no failing head)
    renders NOTHING (no praise line) -> "".
    """
    tf = {_norm(t) for t in (test_files or [])}
    labels: list[str] = []
    if hygiene_blocked:
        labels.append(_CERT_HYGIENE_LABEL)
    for name in (failing_heads or []):
        lbl = _CERT_HOOK_LABEL.get(str(name))
        if lbl and lbl not in labels:
            labels.append(lbl)
    if not labels:
        return ""  # clean cert -> deliver nothing (correct-or-quiet)
    lines = ["pre-commit hook failed:"]
    for lbl in labels:
        dots = "." * max(3, _CERT_LEADER_WIDTH - len(lbl))
        lines.append(_final_scrub(f"{lbl}{dots}Failed", tf))
    det = _cap(hygiene_detail).strip()
    if det:
        lines.append(_final_scrub(det, tf))  # a world-fact reason, identity-scrubbed
    lines.append("commit aborted (exit 1)")
    return _tail("\n".join(lines))
