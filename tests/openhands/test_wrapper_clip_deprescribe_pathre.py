"""TDD tests for wrapper group C1 + C6A + malformed-path fixes.

Scope file: scripts/swebench/oh_gt_full_wrapper.py

Three structural defects, each proven red-before-green with a negative control
that proves no over-suppression (valid input passes through unchanged):

C1  -- SEMANTIC WARNING guard text shipped truncated (e.g. ``...,"sent``), an
       unterminated fragment that violates correct-or-quiet. The fix wraps the
       guard slice in ``_core_clip_balanced`` so the agent only ever sees a
       balanced prefix. Negative control: a balanced guard is unchanged.

C6A -- [GT_ADVISORY] / brief-fallback prose was imperative/prescriptive
       ("Edit these source files instead:", " Focus on:", "Start with:").
       GT is a curation layer, not a controller; the fix states the observed
       fact and drops the imperative. Negative control: trigger/telemetry
       (the [GT_GATE] block) is untouched and the advisory still fires.

PathRE -- ``_FILE_PATH_RE`` used a greedy ``\\S+`` that swallowed a leading
       ``(`` (or other non-path punctuation) from a caller-code snippet into
       brief_candidates ("(file" entries). The fix restricts a path token to
       path-legal characters only. Negative control: a normal path
       ``foo/bar.py`` is still captured unchanged.

All fixes are structural properties of code/repos in general -- no hardcoded
names, paths, repo IDs, or benchmark shape.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.swebench import oh_gt_full_wrapper as ohgt
from groundtruth.runtime.sanitizer import is_well_formed_clause


# --------------------------------------------------------------------------
# C1 -- SEMANTIC WARNING guard text must be balanced (correct-or-quiet)
# --------------------------------------------------------------------------

def test_c1_guard_text_is_balanced_after_clip():
    """A truncated guard slice (as produced by a byte-budget cut upstream)
    must be repaired to a balanced prefix before it ships in a SEMANTIC
    WARNING. This is the contract the wrapper relies on."""
    # Representative truncated guard: ends inside an open paren + open string.
    truncated = 'if status == 200 and resp.get(,"sent'
    assert not is_well_formed_clause(truncated)  # red precondition: malformed

    clipped = ohgt._core_clip_balanced(truncated)
    # The repaired text must be well-formed (balanced quotes/brackets).
    assert is_well_formed_clause(clipped), repr(clipped)
    # And it must not carry the unterminated tail.
    assert '"sent' not in clipped
    assert clipped  # non-empty: there IS a recoverable balanced prefix


def test_c1_clip_is_noop_on_balanced_guard():
    """NEGATIVE CONTROL: a valid, balanced guard condition is passed through
    unchanged -- the repair never trims valid content."""
    valid = "if user is not None and user.active"
    assert is_well_formed_clause(valid)
    assert ohgt._core_clip_balanced(valid) == valid


def test_c1_source_wraps_guard_slices_in_clip_balanced():
    """Guard against regression: the wrapper source must wrap BOTH guard
    slices (_sl[12:] for GUARD_ADDED, _sl[14:] for GUARD_REMOVED) in
    _core_clip_balanced, never ship the raw slice."""
    src = Path(ohgt.__file__).read_text(encoding="utf-8")
    assert "_core_clip_balanced(_sl[12:])" in src, "GUARD_ADDED slice not clipped"
    assert "_core_clip_balanced(_sl[14:])" in src, "GUARD_REMOVED slice not clipped"
    # The raw (unclipped) forms must be gone.
    assert "New guard: {_sl[12:]}" not in src
    assert "Guard removed: {_sl[14:]}" not in src


# --------------------------------------------------------------------------
# C6A -- advisory / fallback prose is diagnostic, not prescriptive
# --------------------------------------------------------------------------

_IMPERATIVES = ("Edit these", "Focus on", "Start with")


def _make_config(**kwargs):
    return ohgt.GTRuntimeConfig(**kwargs)


def test_c6a_scaffold_advisory_is_diagnostic_not_imperative():
    """L5 scaffold-redirect: must state the observed fact and DROP the
    imperative ('Edit these source files instead:')."""
    config = _make_config()
    config.edited_files = {"reproduce_a.py", "repro_b.py", "debug_c.py"}
    config.brief_candidates = {"src/auth.py", "src/cache.py"}
    out = ohgt.render_l5_advisory(config)
    assert out  # advisory still fires (trigger unchanged)
    for imp in _IMPERATIVES:
        assert imp not in out, f"imperative {imp!r} leaked: {out!r}"
    # The observed fact (scaffolding count) is still surfaced.
    assert "scaffolding" in out.lower()


def test_c6a_edit_loop_advisory_is_diagnostic_not_imperative():
    """L5 edit-loop redirect prose must drop the ' Focus on:' imperative.

    The edit-loop branch keys on ``edit_counts[f] >= 3`` built from
    ``config.edited_files`` (a set, which cannot hold duplicate entries), so the
    branch is not directly reachable via ``render_l5_advisory`` with public
    state. Assert on the source text: the imperative literal must be gone, and
    the diagnostic replacement ('Graph-ranked source candidates:') present."""
    src = Path(ohgt.__file__).read_text(encoding="utf-8")
    assert "Focus on: {}" not in src, "edit-loop ' Focus on:' imperative present"
    assert "Graph-ranked source candidates: {}" in src


def test_c6a_brief_fallback_is_diagnostic_not_imperative():
    """The 0-candidate brief fallback must not instruct 'Start with: gt_search
    function {keyword}'. It states the observed gap + the keyword."""
    src = Path(ohgt.__file__).read_text(encoding="utf-8")
    assert "Start with: " not in src, "'Start with:' imperative present in fallback"
    assert "gt_search function {keyword}" not in src


def test_c6a_gate_block_preserved():
    """NEGATIVE CONTROL: the [GT_GATE] pre-submit telemetry block (the trigger
    surface) is untouched -- valid advisory structure still emits."""
    config = _make_config()
    config.edited_files = {"src/real_module.py"}
    config.viewed_files = {"src/other.py"}
    out = ohgt.render_l5_advisory(config)
    assert "[GT_GATE] Pre-submit review:" in out
    assert 'layer="L5"' in out


# --------------------------------------------------------------------------
# PathRE -- a path token cannot start with / contain grouping punctuation
# --------------------------------------------------------------------------

def test_pathre_does_not_capture_leading_paren_from_snippet():
    """A caller-code snippet like 'x = (foo/bar.py, baz)' must NOT yield a
    '(foo/bar.py' candidate."""
    files = ohgt._extract_candidate_files("x = (foo/bar.py, baz)")
    assert files == ["foo/bar.py"], files
    assert all(not f.startswith("(") for f in files)


def test_pathre_does_not_capture_embedded_paren():
    """A path embedded inside a call snippet must be captured without the
    swallowed '(' -- 'call(loguru/_logger.py)' -> 'loguru/_logger.py'."""
    files = ohgt._extract_candidate_files("call(loguru/_logger.py)")
    assert files == ["loguru/_logger.py"], files


def test_pathre_negative_control_normal_path_unchanged():
    """NEGATIVE CONTROL: a normal path 'foo/bar.py' is still captured exactly,
    and common valid path shapes (relative, underscore, hyphen) survive."""
    assert ohgt._extract_candidate_files("foo/bar.py") == ["foo/bar.py"]
    assert ohgt._extract_candidate_files("./rel/mod.py") == ["./rel/mod.py"]
    assert ohgt._extract_candidate_files("src/_private.py") == ["src/_private.py"]
    assert ohgt._extract_candidate_files("a-b.py") == ["a-b.py"]
    # Mid-sentence path (as it appears in a real brief line).
    assert ohgt._extract_candidate_files("TARGET src/service.py here") == [
        "src/service.py"
    ]


def test_pathre_brief_candidates_never_contain_punct_prefixed_paths():
    """End-to-end on the regex contract: no captured candidate begins with a
    grouping/quote character regardless of surrounding snippet noise."""
    noisy = 'See (a/b.py, "c/d.ts") and [e/f.go]; call(g/h.rs)'
    for f in ohgt._extract_candidate_files(noisy):
        assert f[0] not in "([{\"',", f"punct-prefixed candidate: {f!r}"
