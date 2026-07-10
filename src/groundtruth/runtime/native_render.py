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
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|spec|__tests__|testing)(/|$)", re.IGNORECASE)

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


_RE_CASE_TOKEN = re.compile(r"\b(test_\w+|Test\w+|\w+_test|it_\w+)\b")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./").strip()


_RE_CARGO_THREAD = re.compile(r"thread\s+'[^']*'")
_RE_RUST_TESTPATH = re.compile(r"\b[\w]+::tests?::[\w:]+")
_RE_ANY_NODEID = re.compile(r"([\w./\\+\-]+\.(?:py|go|rs|js|jsx|ts|tsx|rb|java))::[\w:.\[\]\-]+")


def _strip_case_tokens(msg: str) -> str:
    """Replace test-function-name tokens with <test> so a value/error MESSAGE that
    a test-file line carries (go/cargo print the delta there) can be kept without
    leaking the test's identity."""
    return _RE_CASE_TOKEN.sub("<test>", msg)


def _final_scrub(line: str) -> str:
    """Fail-closed belt applied to EVERY kept line: no cargo thread-quote, no rust
    ``::tests::`` path, no ``file::nodeid``, no test-name token survives. Guarantees
    contains_test_identity(output) is False regardless of runner quirks."""
    line = _RE_CARGO_THREAD.sub("thread", line)
    line = _RE_RUST_TESTPATH.sub("<test>", line)
    line = _RE_ANY_NODEID.sub(r"\1", line)
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


def render_covering_failure_native(
    result: dict[str, Any],
    *,
    edited_symbol: str | None = None,
    test_files: list[str] | set[str] | None = None,
) -> str:
    """Format D: the anonymized native failure block. Returns "" when nothing
    signal-bearing survives the firewall (correct-or-quiet)."""
    if not result:
        return ""
    tf = {_norm(t) for t in (test_files or [])}
    raw = ((result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or ""))
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
    if not kept:
        return ""
    # final fail-closed identity scrub on EVERY line, then de-dupe + cap.
    out: list[str] = []
    for ln in kept:
        ln = _final_scrub(ln)
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
    Leak-safe by CALLER contract: the seam checks only NON-test source edits (the
    agent's own just-written file), so the diagnostic names no hidden test identity."""
    if not result or result.get("verdict") != "syntax_error":
        return ""
    diag = _RE_GT_TAG.sub("", (result.get("diagnostic") or "")).strip()
    if not diag:
        return ""
    return _tail(diag, _MAX_BODY).strip()


def deepest_agent_frame(result: dict[str, Any], test_files: list[str] | set[str] | None = None
                        ) -> tuple[str, int] | None:
    """The deepest NON-test traceback frame (agent's own source) as (path, line),
    or None. Used by the attribution gate (was this red caused by the edit)."""
    tf = {_norm(t) for t in (test_files or [])}
    raw = ((result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or ""))
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
    red DIFFERENTIAL second) — the ONE question the seam should ask."""
    ef = {_norm(f) for f in (edited_files or [])}
    if not ef:
        return False
    frame = deepest_agent_frame(result, test_files)
    if frame is None:
        return False
    fp = frame[0]
    return fp in ef or os.path.basename(fp) in {os.path.basename(e) for e in ef}


def render_submit_rejection(reason: str, detail: str = "") -> str:
    """A submit refusal as a pre-commit / CI style failure (native, in-distribution).
    Staged for B2 (the submit gate); the model reacts to a failed action + retries."""
    lines = ["pre-commit hook failed:"]
    if reason:
        lines.append(reason)
    if detail:
        lines.append(_tail(detail))
    lines.append("commit aborted (exit 1)")
    return "\n".join(lines)


def contains_gt_tag(text: str) -> bool:
    """Stage-1 guard: True if a GT-voice tag leaked onto a model-facing surface."""
    return "<gt-" in (text or "").lower()


def contains_test_identity(text: str, test_files: list[str] | set[str] | None = None) -> bool:
    """Stage-1 leak guard: True if any test-file path, ``::nodeid``, or ``def test_``
    echo survived onto the model-facing surface. Format D must always be False."""
    t = text or ""
    if "::" in t and re.search(r"\.\w+::", t):
        return True
    if _RE_DEF_TEST_ECHO.search(t):
        return True
    for ln in t.splitlines():
        for tok in re.findall(r"[\w./\\+\-]+\.(?:py|go|rs|js|jsx|ts|tsx|rb|java)\b", ln):
            if _is_test_path(tok, {_norm(x) for x in (test_files or [])}):
                return True
    return False
