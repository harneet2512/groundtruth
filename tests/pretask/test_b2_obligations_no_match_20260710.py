"""B-2 — the issue's behavioral obligations must survive the no-match early-return.

Finding (GT_BUG_JOURNEY_BRIEF B-2): when localization ranks NO existing file
(new-file / behavioral no-match issues), ``generate_v1r_brief`` returned an EMPTY
``<gt-task-brief>`` container BEFORE the obligations block — the issue's parsed
behavioral spec was lost at step 0, exactly when a no-match task most needs it.

Fix: on the ``not v74.ranked_full`` path the brief now renders the issue's own
obligations (leak-screened) plus an honest-uncertainty note. There is no located
file to couple the spec to, so ``_render_obligations_block`` is called with
``require_anchor=False`` (the focus-anchor relevance gate is bypassed) — but the
leak screen (``_obligation_is_leaky``) still runs, so zero test-name /
FAIL_TO_PASS / gold-path tokens can leak.

RED on the pre-fix tree: the no-match brief was exactly ``"<gt-task-brief>\\n"
"</gt-task-brief>"`` with no obligations. MUTATION proof: reverting to
``require_anchor=True`` on the no-match path (or restoring the empty container)
makes ``test_no_match_renders_obligations`` RED.

Hermetic: ``run_v74`` is monkeypatched to an empty result (no embedder, no repo),
and ``GT_ANCHORS_PATH`` points at a nonexistent file so obligation extraction is
LIVE from the issue text (never a stale per-task artifact).
"""

from __future__ import annotations

import os
import sqlite3
import types

import pytest

import groundtruth.pretask.v1r_brief as vb
from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _render_obligations_block,
    generate_v1r_brief,
)


def _empty_graph(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported BOOLEAN, is_test BOOLEAN, "
        "language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT);"
    )
    conn.commit()
    conn.close()
    return db


def _no_match_env(tmp_path, monkeypatch) -> str:
    """Force the no-match branch (run_v74 -> empty) with a hermetic, live-extraction
    obligation path. Returns the graph_db path."""
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    stub = types.SimpleNamespace(
        ranked_full=[], effective_w_sem=0.31, k_sem_top_effective=7
    )
    monkeypatch.setattr(vb, "run_v74", lambda *a, **k: stub)
    return _empty_graph(tmp_path)


_ISSUE = (
    "The parser must coalesce adjacent semantics.\n"
    "parse_node should merge nodes and raise ParseError on bad input.\n"
)


def test_no_match_renders_obligations(tmp_path, monkeypatch) -> None:
    """A no-match issue still delivers the parsed behavioral obligations at step 0."""
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert res.files == []
    assert res.rendered_candidate_count == 0
    assert "<gt-obligations>" in res.brief_text, (
        f"obligations LOST on the no-match path: {res.brief_text!r}"
    )
    assert "</gt-obligations>" in res.brief_text
    assert "coalesce adjacent semantics" in res.brief_text
    # not the old empty container.
    assert res.brief_text != "<gt-task-brief>\n</gt-task-brief>"


def test_no_match_emits_honest_uncertainty(tmp_path, monkeypatch) -> None:
    """The no-match brief carries an honest 'localize with grep' steer (never a
    false confident file)."""
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    low = res.brief_text.lower()
    assert "no indexed file ranked" in low
    assert "grep" in low or "code-search" in low


def test_no_match_token_estimate_is_real_count(tmp_path, monkeypatch) -> None:
    """The reported token count reflects the actual brief bytes, not the old
    hard-coded 4."""
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert res.token_estimate == vb._count_tokens(res.brief_text)
    assert res.token_estimate > 4


def test_no_match_leak_screened_test_name(tmp_path, monkeypatch) -> None:
    """Even with the relevance gate bypassed, a test-name obligation is dropped —
    GT surfaces zero grader-coupled tokens."""
    db = _no_match_env(tmp_path, monkeypatch)
    issue = (
        "The parser must coalesce adjacent semantics.\n"
        "The fix must make test_parse_node_coalesces pass.\n"
    )
    res = generate_v1r_brief(issue, str(tmp_path), db)
    assert "<gt-obligations>" in res.brief_text  # the clean obligation still renders
    assert "test_parse_node_coalesces" not in res.brief_text, (
        f"a pytest test name leaked on the no-match path: {res.brief_text!r}"
    )


def test_no_match_leak_screened_fail_to_pass(tmp_path, monkeypatch) -> None:
    db = _no_match_env(tmp_path, monkeypatch)
    issue = (
        "The parser must coalesce adjacent semantics.\n"
        "parse_node should satisfy the FAIL_TO_PASS contract for the suite.\n"
    )
    res = generate_v1r_brief(issue, str(tmp_path), db)
    assert "FAIL_TO_PASS" not in res.brief_text


def test_no_match_quiet_when_no_obligations(tmp_path, monkeypatch) -> None:
    """An issue with no extractable behavioral spec still degrades cleanly: a
    well-formed container + the honest note, no crash, no obligations block."""
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief("random noise xyzzy", str(tmp_path), db)
    assert res.brief_text.startswith("<gt-task-brief>")
    assert res.brief_text.rstrip().endswith("</gt-task-brief>")
    assert "no indexed file ranked" in res.brief_text.lower()


# --- default require_anchor path is byte-identical (has-ranked-file behavior) --

def _cap(s: str) -> str:
    return s


def test_require_anchor_default_true_still_gates(tmp_path, monkeypatch) -> None:
    """The DEFAULT (require_anchor=True) behavior is unchanged: an obligation that
    overlaps neither the focus nor the anchor symbols stays quiet — the normal
    has-ranked-file path is byte-identical."""
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    issue = (
        "validate_schema must reject malformed input.\n"
        "It should always raise a SchemaError when the version is missing.\n"
    )
    files = [FileEntry(path="src/render.rs", score=0.9,
                       functions=["draw_pixel"], function_names=["draw_pixel"])]
    assert _render_obligations_block(issue, files, _cap) == []
    # and with an overlapping focus it DOES render (default path intact).
    files_hit = [FileEntry(path="src/schema.rs", score=0.9,
                           functions=["validate_schema"], function_names=["validate_schema"])]
    assert _render_obligations_block(issue, files_hit, _cap), (
        "default focus-overlap render regressed"
    )


def test_require_anchor_false_bypasses_gate_but_screens_leak(tmp_path, monkeypatch) -> None:
    """require_anchor=False renders leak-clean obligations with EMPTY files (no
    anchor), yet still drops a test-name obligation. This is the exact call the
    no-match branch makes."""
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    issue = (
        "The engine must emit a trap event on overflow.\n"
        "The fix must make test_trap_overflow pass.\n"
    )
    out = _render_obligations_block(issue, [], _cap, require_anchor=False)
    assert out, "obligations did not render on the bypassed no-match path"
    joined = "\n".join(out)
    assert "trap event on overflow" in joined
    assert "test_trap_overflow" not in joined, f"leak on bypass path: {joined!r}"


def test_require_anchor_false_needs_no_focus(tmp_path, monkeypatch) -> None:
    """MUTATION target: with require_anchor=True (the default) and EMPTY files the
    block is quiet (no anchor); with require_anchor=False it renders. Reverting the
    no-match branch to the default gate would therefore drop the obligations."""
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    issue = "The parser must coalesce adjacent semantics before returning.\n"
    assert _render_obligations_block(issue, [], _cap, require_anchor=True) == []
    assert _render_obligations_block(issue, [], _cap, require_anchor=False)
