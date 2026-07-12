r"""SM-1 (Super Mode, 2026-07-11) — the per-fact-class NATIVE-GRAMMAR pins.

Thesis under test: "tag-free" is NOT "native". A native fact must be shaped like a
REAL tool channel the RL policy already reads — a traceback frame, a type-checker /
compiler diagnostic, a linter diagnostic, a ripgrep row, a short imperative — NOT GT
narration with the ``<gt-*>`` tag merely stripped off. Each renderer in
``native_render`` is pinned here by THREE assertions the SM-1 charter mandates:
  (a) the output matches that channel's GRAMMAR (a shape/regex assertion);
  (b) it carries ZERO ``<gt-`` tag and ``contains_test_identity(out) is False`` (leak law);
  (c) a NEGATIVE assertion that tag-free ENGLISH NARRATION is NOT a valid native form
      for that class (the renderer never emits the prose the gateway used to ship).

Plus the INTERNAL-ONLY pins: ``cochange`` (ranking signal, no model-facing form) and
``scope`` without a named decision (no external narration); and the additive guard that
the three PRE-EXISTING renderers are byte-unchanged.

RED-before-green + the ≥2 biting mutations per renderer are recorded in the mutation
notes above each cluster (each was applied, observed to bite the named assertion, and
reverted). Windows: run with ``PYTHONIOENCODING=utf-8``.
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.join(r"D:\Groundtruth", "src"))

from groundtruth.runtime import native_render as nr  # noqa: E402
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_gt_tag,
    contains_test_identity,
    render_cochange_native,
    render_covering_failure_native,
    render_def_rows_native,
    render_recovery_native,
    render_registration_native,
    render_scope_constraint_native,
    render_signature_delta_native,
    render_submit_rejection,
    render_syntax_error_native,
    render_trace_frame_native,
)

# --- channel grammars (the tool-shape assertions) ---------------------------
_TRACE_PY_RE = re.compile(r'^\s*File "[^"]+", line \d+(?:, in \S+)?$')       # CPython frame
_TRACE_LOC_RE = re.compile(r"^[\w./\\+\-]+:\d+$")                            # go/rust location
_SIG_RE = re.compile(r"^[\w./\\+\-]+:\d+: error: \w+\(\) takes .+ but .+ given$")  # mypy/gopls/tsc
_LINT_RE = re.compile(r"^[\w./\\+\-]+:\d+: warning: registers .+ but not '.+'$")   # ruff/eslint
_ROW_RE = re.compile(r"^[\w./\\+\-]+:\d+:\S")                                # ripgrep path:line:match
_SCOPE_RE = re.compile(r"^[\w./\\+\-]+: note: .+$")                          # gcc/clang note:


def _leak_clean(out: str, test_files=None) -> None:
    """(b) the leak law: no ``<gt-`` tag, no surviving test identity — every renderer."""
    assert out, "expected non-empty output"
    assert not contains_gt_tag(out), out
    assert contains_test_identity(out, test_files) is False, out


# ===========================================================================
# trace_frame -> a native TRACEBACK FRAME line
# ===========================================================================
# MUTATION-1 (grammar): body -> f"deepest in-repo frame: {n}:{ln}"  (the gateway
#   narration) => _TRACE_PY_RE.match fails AND the "deepest in-repo frame" negative
#   assertion fires. BITE (observed).
# MUTATION-2 (firewall/leak): drop `if _is_test_path(n, tf): return ""` => a test
#   frame is delivered => test_frame_is_quiet's `== ""` fails. BITE (observed).
def test_trace_python_frame_grammar():
    out = render_trace_frame_native("src/pool.py", 88, "transform")
    assert _TRACE_PY_RE.match(out), repr(out)               # (a) CPython frame shape
    assert 'File "src/pool.py", line 88, in transform' in out
    _leak_clean(out)                                        # (b) leak law
    # (c) NOT the GT narration form
    assert "deepest in-repo frame" not in out, out


def test_trace_python_frame_no_func():
    out = render_trace_frame_native("pkg/mod.py", 5)
    assert _TRACE_PY_RE.match(out), repr(out)
    assert out == '  File "pkg/mod.py", line 5'


def test_trace_native_location_for_go_rust():
    assert render_trace_frame_native("src/pool.rs", 88, "do_it") == "src/pool.rs:88"
    assert render_trace_frame_native("internal/srv.go", 42) == "internal/srv.go:42"
    assert _TRACE_LOC_RE.match(render_trace_frame_native("a/b.ts", 9))


def test_trace_frame_is_quiet_on_test_frame_and_bad_input():
    # identity firewall: a covering-test frame must be QUIET, not delivered.
    assert render_trace_frame_native("tests/pool_test.py", 5, "x") == ""
    assert render_trace_frame_native("foo/bar_test.go", 5) == ""
    # correct-or-quiet: no path / unparseable line
    assert render_trace_frame_native("", 5) == ""
    assert render_trace_frame_native("src/pool.py", "NaN") == ""


# ===========================================================================
# signature_delta -> a TYPE-CHECKER / COMPILER DIAGNOSTIC
# ===========================================================================
# MUTATION-1 (grammar): out -> f"the signature of {sym} changed to take {n}" =>
#   _SIG_RE fails AND the "changed"/"the signature" negative assertion fires. BITE.
# MUTATION-2 (firewall): drop the caller `_is_test_path` return "" => a test-file
#   caller diagnostic is emitted => quiet-on-test `== ""` fails. BITE.
def test_signature_delta_diagnostic_grammar():
    out = render_signature_delta_native(
        "app/handlers.py", 10, "transform",
        expected_min=2, expected_max=2, given=3)
    assert _SIG_RE.match(out), repr(out)                   # (a) mypy/gopls/tsc shape
    assert out == "app/handlers.py:10: error: transform() takes 2 positional argument(s) but 3 given"
    _leak_clean(out)                                       # (b)
    # (c) never the prose the gateway used to ship
    assert "changed" not in out and "the signature" not in out.lower()
    assert "call passes" not in out


def test_signature_delta_arity_range():
    out = render_signature_delta_native(
        "svc/api.go", 21, "Build", expected_min=1, expected_max=3, given=4)
    assert "takes 1-3 positional argument(s) but 4 given" in out
    assert _SIG_RE.match(out), repr(out)


def test_signature_delta_quiet_on_test_caller_and_bad_input():
    assert render_signature_delta_native(
        "tests/test_api.py", 10, "f", expected_min=1, expected_max=1, given=2) == ""
    assert render_signature_delta_native(
        "a.py", 10, "", expected_min=1, expected_max=1, given=2) == ""
    assert render_signature_delta_native(
        "a.py", 10, "f", expected_min=1, expected_max=1, given=None) == ""


# ===========================================================================
# registration / companion_surface -> a LINTER DIAGNOSTIC
# ===========================================================================
# MUTATION-1 (grammar): out -> f"{sym} is not registered next to its siblings" (bare
#   prose, no path:line: warning: prefix) => _LINT_RE fails. BITE.
# MUTATION-2 (firewall): drop the `_is_test_path` return "" => a test-file registry
#   diagnostic is emitted => quiet-on-test `== ""` fails. BITE.
def test_registration_linter_grammar():
    out = render_registration_native(
        "app/registry.py", 20, "baz", ["foo", "bar"])
    assert _LINT_RE.match(out), repr(out)                  # (a) ruff/eslint shape
    assert out == "app/registry.py:20: warning: registers foo, bar but not 'baz'"
    _leak_clean(out)                                       # (b)
    # (c) it is a diagnostic (has the linter prefix), NOT bare narration
    assert out.startswith("app/registry.py:20: warning:")


def test_registration_quiet_on_empty_and_test_file():
    assert render_registration_native("app/r.py", 20, "baz", []) == ""       # no siblings
    assert render_registration_native("app/r.py", 20, "", ["a"]) == ""       # no symbol
    assert render_registration_native("app/r.py", None, "baz", ["a"]) == ""  # no line
    assert render_registration_native(
        "tests/test_reg.py", 20, "baz", ["a", "b"]) == ""                    # test file


# ===========================================================================
# def_partition -> a RIPGREP ROW block (path:line:sym)
# ===========================================================================
# MUTATION-1 (grammar): row -> f"def of {sym} at {n} line {ln}" (prose) => _ROW_RE
#   fails per line. BITE.
# MUTATION-2 (firewall): drop `if _is_test_path(n, tf): continue` => a test row
#   survives => rows_drops_test_rows leaves a non-empty output where "" is asserted. BITE.
def test_def_rows_ripgrep_grammar():
    out = render_def_rows_native([
        ("src/users.py", 12, "get_user"),
        ("src/users.py", 40, "get_user"),
    ])
    lines = out.splitlines()
    assert lines == ["src/users.py:12:get_user", "src/users.py:40:get_user"]
    for ln in lines:
        assert _ROW_RE.match(ln), repr(ln)                 # (a) ripgrep row shape
    _leak_clean(out)                                       # (b)
    # (c) NOT prose
    assert "def of" not in out and " line " not in out


def test_def_rows_drops_test_rows_and_quiet_on_empty():
    # a test-file def row is dropped (identity firewall); only the source row remains.
    out = render_def_rows_native([
        ("tests/test_users.py", 5, "get_user"),
        ("src/users.py", 12, "get_user"),
    ])
    assert out == "src/users.py:12:get_user"
    # ALL-test input -> "" (nothing model-facing survives)
    assert render_def_rows_native([("tests/test_users.py", 5, "get_user")]) == ""
    assert render_def_rows_native([]) == ""
    assert render_def_rows_native(None) == ""


# ===========================================================================
# recovery -> the proven SHORT IMPERATIVE (no tag, one line)
# ===========================================================================
# MUTATION-1 (tag): drop `_RE_GT_TAG.sub(...)` => a `<gt-nudge>` wrapper survives =>
#   contains_gt_tag(out) True => (b) bites.
# MUTATION-2 (one-line): drop the `" ".join(text.split())` collapse => a multi-line
#   nudge survives => the "\n" not in out assertion bites.
def test_recovery_short_imperative_native():
    out = render_recovery_native(
        "no_test_evidence",
        "Run one narrow test target before you submit.")
    assert out == "Run one narrow test target before you submit."
    assert "\n" not in out                                 # (a) one imperative line
    assert not contains_gt_tag(out)                        # (b) no tag
    assert contains_test_identity(out) is False, out       # (b) leak law
    assert "no_test_evidence" not in out                   # reason is host-side, not emitted


def test_recovery_strips_tag_and_collapses_lines():
    out = render_recovery_native(
        "loop",
        '<gt-nudge reason="loop">\nStop repeating the same command;\nmake a concrete edit now.\n</gt-nudge>')
    assert not contains_gt_tag(out), out                   # tag stripped
    assert "\n" not in out                                 # collapsed to one line
    assert out == "Stop repeating the same command; make a concrete edit now."


def test_recovery_quiet_on_empty():
    assert render_recovery_native("x", "") == ""
    assert render_recovery_native("x", "   ") == ""
    assert render_recovery_native("x", "<gt-nudge></gt-nudge>") == ""


# ===========================================================================
# scope -> INTERNAL unless it names a CONCRETE edit decision
# ===========================================================================
# MUTATION-1 (grammar): out -> f"scope note: also update {n}" (drop the path: note:
#   prefix) => _SCOPE_RE fails. BITE.
# MUTATION-2 (internal-only): make it emit a line when `must_touch_file` is empty =>
#   scope_without_decision_is_internal's `== ""` fails. BITE.
def test_scope_constraint_grammar_when_named():
    out = render_scope_constraint_native("config/settings.py")
    assert _SCOPE_RE.match(out), repr(out)                 # (a) gcc/clang note: shape
    assert out == "config/settings.py: note: your change must also update this file"
    _leak_clean(out)                                       # (b)


def test_scope_without_decision_is_internal_no_narration():
    # (c) scope that names NO decision has NO model-facing form (internal ranking only).
    assert render_scope_constraint_native(None) == ""
    assert render_scope_constraint_native("") == ""
    assert render_scope_constraint_native("   ") == ""
    # never names a test file
    assert render_scope_constraint_native("tests/test_x.py") == ""


# ===========================================================================
# cochange -> INTERNAL RANKING ONLY (no external native form)
# ===========================================================================
# MUTATION (would-be leak): make render_cochange_native return the gateway body
#   f"co-changed with the edit in {n} commits" => this test's `== ""` AND the
#   "co-changed" negative assertion both bite. BITE.
def test_cochange_has_no_external_native_form():
    # ANY input -> "" (co-change is a ranking signal, never a delivered fact).
    assert render_cochange_native() == ""
    assert render_cochange_native("src/a.py", 3) == ""
    assert render_cochange_native(file="src/a.py", count=3) == ""
    # it must NEVER emit the narration the gateway body carries
    for spew in (render_cochange_native(), render_cochange_native("src/a.py", 3)):
        assert "co-changed" not in spew
        assert "commits" not in spew


# ===========================================================================
# obligation -> KEEP the proven `<gt-obligations>` form; NO new native renderer here
# ===========================================================================
def test_obligation_has_no_new_native_renderer():
    # The obligations fact retains its consumed `<gt-obligations>` form (the ONE
    # sanctioned tag — it has a proven consumption receipt); SM-1 does NOT convert it,
    # so native_render must expose no obligation renderer.
    assert not hasattr(nr, "render_obligation_native")
    assert not hasattr(nr, "render_obligations_native")


# ===========================================================================
# ADDITIVE guard — the three PRE-EXISTING renderers are byte-unchanged
# ===========================================================================
def test_existing_renderers_present_and_behaving():
    # covering RED transcript still renders its native block.
    cov = render_covering_failure_native(
        {"stdout_tail": "E   assert 1 == 2"}, edited_symbol="transform")
    assert cov.startswith("Your change to `transform()` fails a covering test:")
    assert not contains_gt_tag(cov) and contains_test_identity(cov) is False
    # syntax diagnostic unchanged (correct-or-quiet off-verdict).
    assert render_syntax_error_native({"verdict": "ok", "diagnostic": "x"}) == ""
    syn = render_syntax_error_native(
        {"verdict": "syntax_error", "diagnostic": 'File "m.py", line 1\nSyntaxError: bad'})
    assert "SyntaxError: bad" in syn and not contains_gt_tag(syn)
    # submit rejection pre-commit shape unchanged.
    sub = render_submit_rejection("a covering test is failing", "detail")
    assert sub.startswith("pre-commit hook failed:") and sub.endswith("commit aborted (exit 1)")


# ===========================================================================
# SM-1 LIPI close-out fixes (2026-07-11) — RED→GREEN pins for the 3 vectors.
# ===========================================================================

# --- Fix 1: recovery firewall symmetry (route the imperative through _final_scrub) ---
# RED/MUTATION: drop the `_final_scrub(text, set())` route in render_recovery_native
#   (revert to `_tail(text, _MAX_BODY)`) => a raw test identity in the imperative passes
#   through => contains_test_identity(out) True => every assert below bites.
def test_recovery_scrubs_embedded_test_identity():
    # a bare nodeid in the imperative -> scrubbed, no test identity survives.
    out = render_recovery_native("r", "check a/b.py::TestX before you submit")
    assert contains_test_identity(out) is False, out
    assert "::TestX" not in out
    # every leak convention the two prose/transcript screens know:
    for imp in (
        "compare against crate::tests::round_trip first",
        "look at def test_login( for the expected shape",
        "mirror the setup in spec/login.spec.js",
        "the failure is in tests/pool_test.py",
    ):
        o = render_recovery_native("r", imp)
        assert contains_test_identity(o) is False, (imp, o)
        assert not contains_gt_tag(o), (imp, o)


# --- Fix 2: _final_scrub strips a field-injected <gt-fact> in EVERY structured renderer ---
# RED/MUTATION: drop the `_RE_GT_TAG_ANY.sub("", line)` line inside _final_scrub =>
#   a `<gt-fact>` injected into a path/symbol/sibling field survives =>
#   contains_gt_tag(out) True for each renderer below => the asserts bite.
def test_structured_renderers_strip_field_injected_gt_tag():
    T = "<gt-fact>"  # the injected opening tag; closing </gt-fact> is also stripped
    cases = [
        render_trace_frame_native(f"{T}src/x.py</gt-fact>", 5, "fn"),
        render_trace_frame_native("src/x.py", 5, f"{T}fn</gt-fact>"),
        render_signature_delta_native(
            "app/h.py", 10, f"{T}transform</gt-fact>",
            expected_min=2, expected_max=2, given=3),
        render_registration_native(
            "app/r.py", 20, "baz", [f"{T}foo</gt-fact>", "bar"]),
        render_def_rows_native([("src/x.py", 5, f"{T}get_user</gt-fact>")]),
        render_scope_constraint_native(f"{T}config.py</gt-fact>"),
    ]
    for out in cases:
        assert out, "renderer went quiet unexpectedly"
        assert not contains_gt_tag(out), out                 # (b) no field-injected tag
        assert contains_test_identity(out) is False, out


# --- Fix 3: ReDoS — a pathological-length field returns bounded/fast, never a hang ---
# RED/MUTATION: raise `_FIELD_CAP` to a huge value (un-bounding the field) => the 100k
#   dotless field reaches _final_scrub un-capped => the O(n²) path/nodeid regexes hang
#   (>> the time bound) => the elapsed assertion bites. (Demonstrated out-of-suite via a
#   `timeout`-guarded subprocess so the mutation cannot stall the test process.)
def test_pathological_field_is_bounded_not_a_hang():
    big = "a" * 100_000
    for call in (
        lambda: render_trace_frame_native(big, 5, "fn"),
        lambda: render_signature_delta_native(
            big, 10, "f", expected_min=1, expected_max=1, given=2),
        lambda: render_registration_native("app/r.py", 20, "f", [big]),
        lambda: render_def_rows_native([("src/x.py", 5, big)]),
        lambda: render_scope_constraint_native(big),
        lambda: render_recovery_native("r", big),
    ):
        t0 = time.perf_counter()
        out = call()
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"took {elapsed:.3f}s (ReDoS — field not bounded)"
        # output is bounded (the field was capped BEFORE scrubbing)
        assert len(out) < 4 * nr._FIELD_CAP, len(out)
