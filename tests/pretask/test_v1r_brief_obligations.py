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
        assert o_pos < s_pos, f"obligation block rendered AFTER the scope chain: {out!r}"


def test_obligations_render_bites_mutation() -> None:
    """MUTATION proof that the gate actually drives the render: an obligation
    set whose terms DO overlap the focus renders; the SAME obligations with a
    focus that shares no token render NOTHING. (If the helper ignored the gate
    and always rendered, the second call would be non-empty → RED.)"""
    files_hit = [
        FileEntry(
            path="src/parser.rs", score=0.9, functions=["parse_node"], function_names=["parse_node"]
        )
    ]
    files_miss = [
        FileEntry(
            path="src/logger.rs", score=0.9, functions=["write_line"], function_names=["write_line"]
        )
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
        FileEntry(
            path="src/parser.rs", score=0.9, functions=["parse_node"], function_names=["parse_node"]
        )
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
        FileEntry(
            path="src/parser.rs", score=0.9, functions=["parse_node"], function_names=["parse_node"]
        )
    ]
    out = render_brief(files, issue_text=issue)
    assert "FAIL_TO_PASS" not in out, f"a FAIL_TO_PASS marker leaked into the brief: {out!r}"


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
        FileEntry(
            path="src/render.rs", score=0.9, functions=["draw_pixel"], function_names=["draw_pixel"]
        )
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
        FileEntry(
            path="src/parser.rs", score=0.9, functions=["parse_node"], function_names=["parse_node"]
        )
    ]
    assert _render_obligations_block("", files, _cap) == []


# --- ISSUE-SUBJECT ANCHOR: obligations about a symbol NOT in the focus ---------
#
# Run-grounded gap (2 rust data points: pest-character-class-coalescing AND
# wasmi-trap-coredumps): both rust issues carry obligation-language yet rendered
# count=0, because the obligation's subject symbol was NOT among the top-N focus
# functions. Two distinct sub-cases the focus-only gate could never anchor:
#   * FEATURE-ADD — the subject is a net-new symbol (wasmi ``coredump``) that does
#     not exist in ANY indexed function yet, so it can never be a focus token.
#   * TRUNCATION — the subject IS a function (pest ``range``) but ranks below the
#     top-N (MAX_FUNCTIONS_PER_FILE=3), so it is truncated out of the focus list.
# The fix anchors obligations on the issue's OWN curated anchor symbols too.

# Mirrors pest: the obligation's only content anchor ("ranges") is the issue
# symbol ``range``, NOT the focus function (``optimize``/``new``). Verbatim has no
# test-name / FAIL_TO_PASS / focus token — so the focus-only gate drops it.
_FEATURE_ADD_ISSUE = (
    "Add a CharClass variant. A coalesced result is emitted only when merging "
    "produces fewer ranges than the original alternative count.\n"
)


def test_obligations_render_on_anchor_symbol_not_in_focus() -> None:
    """GREEN: an obligation whose subject is an ISSUE ANCHOR SYMBOL (``range``)
    that is NOT among the focus functions renders when ``anchor_symbols`` carries
    the issue's curated code identifiers. This is the rust pest/wasmi gap."""
    files = [
        # focus functions deliberately exclude any token in the obligation —
        # exactly the live pest case (focus = optimize/new, subject = range).
        FileEntry(
            path="meta/src/optimizer/mod.rs",
            score=0.9,
            functions=["optimize", "new"],
            function_names=["optimize", "new"],
        )
    ]
    out = _render_obligations_block(
        _FEATURE_ADD_ISSUE, files, _cap, anchor_symbols={"range", "CharClass"}
    )
    assert out, "obligation did not render despite an issue-anchor-symbol overlap"
    assert any("fewer ranges" in ln for ln in out), (
        f"the issue-subject obligation was not rendered: {out!r}"
    )


def test_anchor_symbol_param_bites_mutation() -> None:
    """MUTATION proof that ``anchor_symbols`` DRIVES the new render: the SAME
    obligation + focus that renders WITH anchor_symbols renders NOTHING WITHOUT
    it. Reverting the fix (focus-only gate) makes this RED — proving the param,
    not some incidental focus-token overlap, is what surfaces the obligation."""
    files = [
        FileEntry(
            path="meta/src/optimizer/mod.rs",
            score=0.9,
            functions=["optimize", "new"],
            function_names=["optimize", "new"],
        )
    ]
    with_anchor = _render_obligations_block(
        _FEATURE_ADD_ISSUE, files, _cap, anchor_symbols={"range", "CharClass"}
    )
    focus_only = _render_obligations_block(_FEATURE_ADD_ISSUE, files, _cap)
    assert with_anchor, "anchor-symbol overlap failed to render (fix is a no-op)"
    assert not focus_only, (
        "obligation rendered with focus-only gate — the anchor_symbols param is "
        f"not what drives it (mutation would pass): {focus_only!r}"
    )


def test_anchor_symbol_gate_still_correct_or_quiet() -> None:
    """NO-LAUNDER preservation: supplying anchor_symbols does NOT launder an
    obligation that overlaps NEITHER the focus NOR the anchor symbols. The gate
    still bites — an unrelated-subsystem obligation stays quiet."""
    issue = (
        "validate_schema must reject malformed input.\n"
        "It should always raise a SchemaError when the version is missing.\n"
    )
    files = [
        FileEntry(
            path="src/render.rs", score=0.9, functions=["draw_pixel"], function_names=["draw_pixel"]
        )
    ]
    # anchor symbols about a DIFFERENT subsystem — no overlap with the obligation.
    out = _render_obligations_block(
        issue, files, _cap, anchor_symbols={"draw_pixel", "framebuffer"}
    )
    assert out == [], f"unrelated obligation laundered through anchor_symbols: {out!r}"


def test_render_brief_threads_anchor_symbols() -> None:
    """INTEGRATION: render_brief forwards anchor_symbols to the obligation block,
    so an issue-subject obligation reaches the brief end-to-end (the live wiring
    gap — anchors were computed but never reached the gate)."""
    files = [
        FileEntry(
            path="meta/src/optimizer/mod.rs",
            score=0.9,
            functions=["optimize", "new"],
            function_names=["optimize", "new"],
        )
    ]
    out_with = render_brief(
        files, issue_text=_FEATURE_ADD_ISSUE, anchor_symbols={"range", "CharClass"}
    )
    out_without = render_brief(files, issue_text=_FEATURE_ADD_ISSUE)
    assert "<gt-obligations>" in out_with, (
        f"render_brief did not thread anchor_symbols into the block: {out_with!r}"
    )
    assert "<gt-obligations>" not in out_without, (
        "obligation rendered without anchor_symbols — focus-only path changed "
        f"(unexpected regression): {out_without!r}"
    )
