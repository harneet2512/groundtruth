"""B-30 — the declared token cap must be an ENFORCED rail, never merely advisory.

Finding (GT_BUG_JOURNEY_BRIEF B-30): the brief budget loops stop when one entry
remains / the body-line cap hits its floor, with NO final over-budget action, so
declared caps of 100/20/1 produced ~244/242/242 tokens. The rail was decorative.

Fix: ``_enforce_token_rail`` guarantees ``_count_tokens(result) <= budget`` by
dropping whole rendered brief blocks LOWEST-priority first (recording each
suppression), then rebuilding highest-priority-first, then hard-truncating only
as an absolute last resort. Keep-priority (highest survives): active obligation >
violated contract / localization steer > causal edge (Witness/Callers) > repo law
(Context/Spec) > companion (co-change/Calls/scope/related) > orientation
(graph-map, steer notes). ``.files`` / ranking are untouched (DOSE only).

RED on the pre-fix tree:
  * ``_enforce_token_rail`` does not exist (ImportError) — the unit tests error.
  * ``test_generate_v1r_brief_enforces_rail`` (behavioral) returns ~80 tokens for
    a cap of 20 on current code — proven live (caps 20/1 -> 80/80 tokens).

MUTATION proof: neutering the hard-truncate last resort (return the pre-truncate
text) makes ``test_rail_holds_at_pathological_budget`` RED at cap=1.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from groundtruth.pretask.v1r_brief import (
    _count_tokens,
    _enforce_token_rail,
    _segment_brief_blocks,
    generate_v1r_brief,
)


# A rendered-brief fixture that mirrors the real block structure + order: a
# localization header, a leading graph-map, two file entries with Witness /
# Contract / Context / Spec / Callers / Calls / Also-changes sub-lines, the
# proactive companion block, Expected behavior, EDIT-TARGET CONTRACTS, the
# obligations block, related-files, a scope chain, and a trailing steer note.
_BRIEF = "\n".join([
    '<gt-localization confidence="medium">',
    "primary edit target: src/parser.py :: parse_node",
    "</gt-localization>",
    "<gt-task-brief>",
    "<gt-graph-map>",
    "  parse_node calls merge_nodes",
    "  called by: driver.py, cli.py, api.py, plugin.py",
    "</gt-graph-map>",
    "1. src/parser.py (def parse_node(self, node), def merge_nodes(self))",
    "   Witness: parse_node calls merge_nodes [CALLS]",
    "   Contract: raises ParseError; returns Optional[Node]",
    "   Context: sibling normalize_node() in the same module",
    "   Spec: parse_node must coalesce adjacent semantic rules before returning",
    "   Callers: src/driver.py:88 (verified), src/cli.py:12 (verified)",
    "   Also changes: src/tree.py, src/errors.py",
    "   Calls: merge_nodes, validate, emit, finalize",
    "2. src/tree.py (def build_tree(nodes))",
    "   Contract: returns Tree",
    "   Callers: src/parser.py:140 (verified)",
    "",
    "Expected behavior: parse_node should merge adjacent semantic rules",
    "",
    "EDIT-TARGET CONTRACTS (parser.py):",
    "  parse_node -> calls merge_nodes(self, a: Node, b: Node)  [src/parser.py:210]",
    "",
    "<gt-obligations>",
    "  - [behavior] the parser must coalesce adjacent semantic rules when merging",
    "  - [error] parse_node raises ParseError on malformed input",
    "</gt-obligations>",
    "",
    "Related files to inspect: tree.py, errors.py",
    "",
    "Scope chain (graph-connected, check ALL): parser.py -> tree.py",
    "   Chain: parser.py -> tree.py (parse_node -> build_tree)",
    "",
    "Highest-confidence candidate (graph + issue signals): src/parser.py",
    "</gt-task-brief>",
])


def _clean_env(monkeypatch) -> None:
    # deterministic char/4 counting (no ambient tokenizer) + no stale anchors file.
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)


# --- the rail is an inviolable ceiling at every budget ------------------------

@pytest.mark.parametrize("cap", [120, 80, 40, 20, 10, 5, 1])
def test_rail_never_exceeds_budget(cap, monkeypatch) -> None:
    _clean_env(monkeypatch)
    assert _count_tokens(_BRIEF) > cap  # precondition: the fixture is over budget
    out, suppressed = _enforce_token_rail(_BRIEF, cap)
    assert _count_tokens(out) <= cap, (
        f"RAIL BREACH cap={cap}: {_count_tokens(out)} tokens > {cap}"
    )
    assert suppressed, "over-budget enforcement must record what it suppressed"


def test_under_budget_is_untouched(monkeypatch) -> None:
    """A brief already within budget is returned byte-identical with nothing
    suppressed (idempotent; the common default-600 path never trims)."""
    _clean_env(monkeypatch)
    big = _count_tokens(_BRIEF) + 50
    out, suppressed = _enforce_token_rail(_BRIEF, big)
    assert out == _BRIEF
    assert suppressed == []


def test_priority_obligation_survives_orientation(monkeypatch) -> None:
    """At a cap that fits the obligation but not the whole brief, the lowest-priority
    ORIENTATION (graph-map, steer note, scope chain) is dropped while the
    highest-priority ACTIVE OBLIGATION survives — the doctrine's fact order, not
    document order. Cap is derived from the obligation size so it never goes stale."""
    _clean_env(monkeypatch)
    blocks = _segment_brief_blocks(_BRIEF)
    oblig = next(b for b in blocks if b["label"] == "obligations")
    scaffold = _count_tokens("<gt-task-brief>\n</gt-task-brief>")
    cap = _count_tokens(oblig["text"]) + scaffold + 2  # room for obligation, not the next block
    assert cap < _count_tokens(_BRIEF)  # still forces enforcement
    out, suppressed = _enforce_token_rail(_BRIEF, cap)
    assert _count_tokens(out) <= cap
    assert "<gt-obligations>" in out, f"obligation dropped before orientation: {out!r}"
    assert "<gt-graph-map>" not in out
    assert "graph-map" in suppressed


def test_low_priority_dropped_before_high(monkeypatch) -> None:
    """Orientation (graph-map, scope chain, steer note) and companion surfaces are
    sacrificed BEFORE the highest-priority active obligation at an intermediate cap."""
    _clean_env(monkeypatch)
    out, suppressed = _enforce_token_rail(_BRIEF, 90)
    assert _count_tokens(out) <= 90
    assert "<gt-graph-map>" not in out
    assert "Scope chain" not in out
    assert "Related files" not in out
    # the active obligation (top fact) outlives every orientation/companion block.
    assert "<gt-obligations>" in out
    assert {"graph-map", "scope-chain", "related-files"} <= set(suppressed)


def test_no_localization_rank_inversion(monkeypatch) -> None:
    """File entries are kept as a rank-ordered prefix: file #2 is never present
    without file #1 (dropping the top-ranked file but keeping a lower one would
    invert the localizer's ranking)."""
    _clean_env(monkeypatch)
    for cap in range(20, 120, 7):
        out, _ = _enforce_token_rail(_BRIEF, cap)
        if "2. src/tree.py" in out:
            assert "1. src/parser.py" in out, (
                f"cap={cap}: file #2 kept without file #1 (rank inversion): {out!r}"
            )


def test_enforced_brief_is_well_formed(monkeypatch) -> None:
    """A pass that drops a trailing block must NOT swallow the </gt-task-brief>
    close tag (blocks are not always blank-separated). For any budget that can hold
    the scaffold, the enforced brief keeps balanced task-brief + obligations tags."""
    _clean_env(monkeypatch)
    scaffold = _count_tokens("<gt-task-brief>\n</gt-task-brief>")
    for cap in range(scaffold + 4, 200, 6):
        out, _ = _enforce_token_rail(_BRIEF, cap)
        assert _count_tokens(out) <= cap
        assert out.count("<gt-task-brief>") == 1
        assert out.count("</gt-task-brief>") == 1, f"close tag lost at cap={cap}: {out!r}"
        assert out.count("<gt-obligations>") == out.count("</gt-obligations>")
        assert out.count("<gt-graph-map>") == out.count("</gt-graph-map>")


def test_rail_holds_at_pathological_budget(monkeypatch) -> None:
    """MUTATION target — even a budget smaller than the protected core is honored
    via the hard-truncate last resort. Neutering that truncate breaks this."""
    _clean_env(monkeypatch)
    for cap in (1, 2, 3):
        out, suppressed = _enforce_token_rail(_BRIEF, cap)
        assert _count_tokens(out) <= cap, f"cap={cap} -> {_count_tokens(out)}"
        assert "truncated" in suppressed


# --- segmenter sanity ---------------------------------------------------------

def test_segmenter_priorities(monkeypatch) -> None:
    _clean_env(monkeypatch)
    blocks = _segment_brief_blocks(_BRIEF)
    by_label = {b["label"]: b["priority"] for b in blocks}
    # obligations strictly outrank the localization header, which outranks the
    # graph-map / orientation note.
    assert by_label["obligations"] < by_label["localization-header"]
    assert by_label["localization-header"] < by_label["graph-map"]
    assert by_label["graph-map"] == by_label["orientation-note"]
    # both file entries share one priority band (rank-ordered prefix keeping).
    assert by_label["file-entry-1"] == by_label["file-entry-2"]


# --- BEHAVIORAL RED->GREEN through the real product entry point ---------------
# Pre-fix live measurement (this session, tiny synthetic graph): cap 20 -> 80
# tokens, cap 1 -> 80 tokens. Post-fix: <= cap. Slow (loads the localizer once);
# kept because it is the authoritative proof the rail is wired into the product.

def _tiny_graph(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN "
        "DEFAULT 0, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT, "
        "resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT);"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,end_line,"
        "signature,return_type,is_exported,is_test,language,parent_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Class", "SafeWatchdog", None, "patroni/watchdog.py", 10, 80, "class SafeWatchdog", None, 1, 0, "python", None),
            (2, "Method", "_fd", None, "patroni/watchdog.py", 30, 45, "def _fd(self)", None, 0, 0, "python", 1),
            (3, "Class", "Postmaster", None, "patroni/postmaster.py", 5, 120, "class Postmaster", None, 1, 0, "python", None),
            (5, "Function", "format_value", None, "patroni/utils.py", 1, 10, "def format_value(x)", None, 1, 0, "python", None),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,source_line,source_file,type,resolution_method,confidence) "
        "VALUES (?,?,?,?,'CALLS','name_match',?)",
        [(3, 1, 100, "patroni/postmaster.py", 0.9), (1, 2, 35, "patroni/watchdog.py", 1.0)],
    )
    conn.commit()
    conn.close()
    return db


def test_generate_v1r_brief_enforces_rail(tmp_path, monkeypatch) -> None:
    # Behavioral RED->GREEN through the product entry point (slow: loads the
    # localizer once). Pre-fix: cap 20 -> 80 tokens; post-fix: <= cap.
    _clean_env(monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    db = _tiny_graph(tmp_path)
    repo = str(tmp_path)
    issue = (
        "SafeWatchdog._fd must guard against a missing watchdog device. "
        "The Postmaster should format_value correctly."
    )
    for cap in (20, 1):
        res = generate_v1r_brief(issue, repo, db, max_brief_tokens=cap)
        assert res.token_estimate <= cap, (
            f"cap={cap}: brief reported {res.token_estimate} tokens (rail not enforced)"
        )
        assert _count_tokens(res.brief_text) <= cap
