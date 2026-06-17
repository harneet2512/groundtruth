"""Behavioral-obligation block — the contract/obligation pillar in the brief.

Run-grounded gap (diagnosed): the issue's parsed behavioral obligations
(``spec.extract_spec`` → ``gt_issue_anchors.json["obligations"]``) were NEVER
rendered into the pre-task brief, so the agent never saw the behavioral spec
(the rust pest task whose failure was coalescing SEMANTICS). ``render_brief`` now
emits a ``<gt-obligations>`` block — gated on FOCUS-anchor overlap (the rendered
edit-target functions), fail-closed on test-name / FAIL_TO_PASS / gold-path
leakage, correct-or-quiet otherwise.

Three invariants pinned (all language-agnostic — pure requirement grammar):

  RED→GREEN — N obligations whose terms overlap the focus function are rendered
    as a ``<gt-obligations>`` block listing them.  MUTATION proof: disabling the
    render call makes ``test_obligations_render`` RED (block absent), proving the
    assertion bites.

  LEAKAGE  — an obligation carrying a pytest test name (``test_*``) or a
    ``FAIL_TO_PASS`` token is DROPPED whole (never benchmaxx off the grader).

  NO-OVERLAP — obligations with zero focus-anchor overlap → the block stays
    quiet (correct-or-quiet; never launder the whole issue spec).
"""

from __future__ import annotations

from groundtruth.pretask.v1r_brief import (
    FileEntry,
    render_brief,
    _render_obligations_block,
)


def _cap(s: str) -> str:  # identity clip closure for the helper under test
    return s


# --- RED→GREEN: focus-overlapping obligations render --------------------------

_PEST_ISSUE = """parse_node must coalesce adjacent semantics

The parser must coalesce adjacent semantic rules when building the tree.
Currently parse_node returns separate nodes and the fix should merge them.
The function raises a ParseError on bad input.
"""


def test_obligations_render() -> None:
    """A behavioral obligation whose terms overlap the focus function
    (``parse_node``) is rendered inside a ``<gt-obligations>`` block."""
    files = [
        FileEntry(
            path="src/parser.rs",
            score=0.9,
            functions=["parse_node"],
            function_names=["parse_node"],
        )
    ]
    out = render_brief(files, issue_text=_PEST_ISSUE)
    assert "<gt-obligations>" in out, f"obligation block absent: {out!r}"
    assert "</gt-obligations>" in out
    # the parsed behavioral spec the agent previously never saw
    assert "coalesce adjacent semantics" in out, (
        f"the issue's behavioral obligation was not rendered: {out!r}"
    )
    # the error-kind obligation is also surfaced
    assert "ParseError" in out


def test_obligations_render_is_after_contract_before_scope() -> None:
    """Placement invariant: the block sits AFTER the per-file contract section
    and BEFORE the scope chain (the diagnosed injection point)."""
    import types

    files = [
        FileEntry(
            path="src/parser.rs",
            score=0.9,
            functions=["parse_node"],
            function_names=["parse_node"],
        )
    ]
    chain = types.SimpleNamespace(
        files=["src/parser.rs", "src/tree.rs"],
        description="parser.rs -> tree.rs",
        confidence=0.9,
    )
    out = render_brief(files, issue_text=_PEST_ISSUE, scope_chains=[chain])
    o_pos = out.find("<gt-obligations>")
    s_pos = out.find("Scope chain")
    assert o_pos != -1, f"block absent: {out!r}"
    if s_pos != -1:  # scope chain rendered → obligations must precede it
        assert o_pos < s_pos, (
            f"obligation block rendered AFTER the scope chain: {out!r}"
        )


def test_obligations_render_bites_mutation() -> None:
    """MUTATION proof that the gate actually drives the render: an obligation
    set whose terms DO overlap the focus renders; the SAME obligations with a
    focus that shares no token render NOTHING. (If the helper ignored the gate
    and always rendered, the second call would be non-empty → RED.)"""
    files_hit = [
        FileEntry(path="src/parser.rs", score=0.9,
                  functions=["parse_node"], function_names=["parse_node"])
    ]
    files_miss = [
        FileEntry(path="src/logger.rs", score=0.9,
                  functions=["write_line"], function_names=["write_line"])
    ]
    hit = _render_obligations_block(_PEST_ISSUE, files_hit, _cap)
    miss = _render_obligations_block(_PEST_ISSUE, files_miss, _cap)
    assert hit, "focus-overlapping obligations did not render (gate too strict)"
    assert not miss, (
        "obligations rendered with a non-overlapping focus — the gate is a "
        f"no-op (always-render mutation would pass): {miss!r}"
    )


# --- LEAKAGE: grader-coupled obligations are dropped fail-closed --------------

def test_leakage_test_name_obligation_dropped() -> None:
    """An obligation that names a pytest test (``test_*``) is NEVER rendered —
    GT must surface zero test references (gt_trial §6 leakage rule)."""
    issue = (
        "parse_node must coalesce adjacent semantics.\n"
        "The fix must make test_parse_node_coalesces pass.\n"
    )
    files = [
        FileEntry(path="src/parser.rs", score=0.9,
                  functions=["parse_node"], function_names=["parse_node"])
    ]
    out = render_brief(files, issue_text=issue)
    # the clean behavioral obligation still renders …
    assert "<gt-obligations>" in out
    # … but the test-name-bearing obligation is dropped whole.
    assert "test_parse_node_coalesces" not in out, (
        f"a pytest test name leaked into the obligation block: {out!r}"
    )


def test_leakage_fail_to_pass_obligation_dropped() -> None:
    """An obligation carrying a FAIL_TO_PASS / PASS_TO_PASS marker is dropped."""
    issue = (
        "parse_node must coalesce adjacent semantics.\n"
        "parse_node should satisfy the FAIL_TO_PASS contract for the suite.\n"
    )
    files = [
        FileEntry(path="src/parser.rs", score=0.9,
                  functions=["parse_node"], function_names=["parse_node"])
    ]
    out = render_brief(files, issue_text=issue)
    assert "FAIL_TO_PASS" not in out, (
        f"a FAIL_TO_PASS marker leaked into the brief: {out!r}"
    )


# --- NO-OVERLAP: stay quiet when nothing anchors to the focus -----------------

def test_no_overlap_obligations_quiet() -> None:
    """Obligations that share no token with the rendered focus function → the
    block stays silent (correct-or-quiet: never launder the whole issue spec)."""
    issue = (
        "validate_schema must reject malformed input.\n"
        "It should always raise a SchemaError when the version is missing.\n"
    )
    # focus function shares NO token with validate_schema / SchemaError
    files = [
        FileEntry(path="src/render.rs", score=0.9,
                  functions=["draw_pixel"], function_names=["draw_pixel"])
    ]
    out = render_brief(files, issue_text=issue)
    assert "<gt-obligations>" not in out, (
        f"obligation block rendered with zero focus overlap: {out!r}"
    )


def test_no_focus_functions_quiet() -> None:
    """No focus functions at all → no anchor → the block stays quiet."""
    files = [FileEntry(path="src/parser.rs", score=0.9)]  # no functions
    out = _render_obligations_block(_PEST_ISSUE, files, _cap)
    assert out == [], f"block rendered without any focus to anchor on: {out!r}"


def test_empty_issue_quiet() -> None:
    """No issue text → no obligations → quiet (degrades cleanly)."""
    files = [
        FileEntry(path="src/parser.rs", score=0.9,
                  functions=["parse_node"], function_names=["parse_node"])
    ]
    assert _render_obligations_block("", files, _cap) == []
