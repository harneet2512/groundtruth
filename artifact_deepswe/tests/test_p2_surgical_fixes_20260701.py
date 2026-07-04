"""P2 surgical fixes (LIPI review 2026-07-01T2116Z) — RED->GREEN + mutation checks.

Three independent P2 defects in gt_mini_patch.py, each pinned by a biting unit
test. UNIT tests (NOT a benchmark/agent run); repo/language-agnostic; no task IDs,
no gold labels.

  P2-1  Debug write `/logs/gt_resurf_debug.txt` fired on EVERY post_edit turn with
        no env gate (prod disk churn on an unowned path). FIX: gate behind
        GT_RESURF_DEBUG=1.  Test: default (unset) never opens the path; the
        mutation direction (env=1) DOES open it — proving the code path is really
        exercised in the setup, so the "unset -> no open" assertion actually bites.

  P2-2  `_query_scope` / `_consensus_block` ordered `SELECT DISTINCT file_path ...
        ORDER BY e.confidence` — a column absent from the DISTINCT projection ->
        implementation-defined which neighbours survive LIMIT 6 and in what order
        -> nondeterministic <gt-scope>.  FIX: MAX(confidence) GROUP BY file with a
        file_path tiebreak.  Test: (a) equal-confidence neighbours come back in a
        stable lexicographic order that is IDENTICAL across two physically
        different indexings (forward vs reversed row insertion); (b) MAX
        aggregation ranks a file by its BEST edge, not an arbitrary per-row one.

  P2-3  `_STRUCT_BODY_KEYS` included old_str/old_string, so a str_replace that
        DELETES code embedded the removed symbols in the obligation edit-CREDIT
        token set -> a symbol the agent REMOVED could be credited "edited".  FIX:
        `_edit_credit_body_tokens` credits only ADDED content.  Test: the new-side
        symbol is credited, the old-side (removed) symbol is NOT; the OLD path
        (via _effective_cmd + _edit_body_tokens) still leaks it (mutation); a pure
        deletion yields zero credit tokens yet is still detected as a write
        (legitimate use of old_str preserved).
"""
from __future__ import annotations

import builtins
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


# =========================================================================== #
# P2-1 — the /logs/gt_resurf_debug.txt write is OPT-IN (GT_RESURF_DEBUG=1).
# =========================================================================== #
_DEBUG_PATH_MARK = "gt_resurf_debug"


def _drive_one_post_edit_turn(monkeypatch, tmp_path):
    """Drive the REAL _augment_output through one post_edit turn (the path that
    reaches the resurface debug write). Mirrors the end-to-end setup used by the
    scope/steer suite."""
    anchors = tmp_path / "anchors.json"
    anchors.write_text('{"symbols": ["frob"]}', encoding="utf-8")
    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            file_path TEXT, language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL, source_file TEXT, source_line INTEGER);
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','frob','src/frob.py','python',NULL,0)")
    con.commit()
    con.close()
    monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.delenv("GT_BASELINE", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_oracle_focus_cache", None)
    monkeypatch.setattr(g, "_oblig_syms_cache", None)
    g._reset_oracle_state()
    # a real source edit -> a post_edit turn (reaches the resurface debug write).
    g._augment_output({"command": "sed -i '1s/a/b/' src/frob.py"}, {"output": "patched"})


@pytest.fixture
def open_spy(monkeypatch):
    """Record every path passed to builtins.open, delegating to the real open so
    the code under test behaves exactly as in production."""
    opened: list[str] = []
    _real_open = builtins.open

    def _spy(file, *a, **k):
        try:
            opened.append(str(file))
        except Exception:  # noqa: BLE001 — never let the spy alter behavior
            pass
        return _real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", _spy)
    return opened


def test_p2_1_debug_write_suppressed_by_default(open_spy, monkeypatch, tmp_path):
    """GREEN: with GT_RESURF_DEBUG unset, the resurface debug file is NEVER
    opened on a post_edit turn."""
    monkeypatch.delenv("GT_RESURF_DEBUG", raising=False)
    _drive_one_post_edit_turn(monkeypatch, tmp_path)
    hits = [p for p in open_spy if _DEBUG_PATH_MARK in p]
    assert hits == [], (
        "ungated debug write fired with GT_RESURF_DEBUG unset (prod disk churn): %r"
        % hits
    )


def test_p2_1_debug_write_opt_in_when_enabled(open_spy, monkeypatch, tmp_path):
    """MUTATION direction: with GT_RESURF_DEBUG=1 the debug file IS opened on the
    same turn. This proves the debug code path is genuinely reached in this
    setup, so the 'unset -> no open' assertion above is not vacuously true."""
    monkeypatch.setenv("GT_RESURF_DEBUG", "1")
    _drive_one_post_edit_turn(monkeypatch, tmp_path)
    hits = [p for p in open_spy if _DEBUG_PATH_MARK in p]
    assert hits, (
        "GT_RESURF_DEBUG=1 did not open the debug file — the write path is not "
        "exercised (the default-suppressed assertion would be vacuous), or the "
        "opt-in gate is broken"
    )


# =========================================================================== #
# P2-2 — deterministic <gt-scope> ordering (MAX-agg + file_path tiebreak).
# =========================================================================== #
def _make_edge_graph(path, source, edges):
    """edges: list of (neighbor_file, resolution_method, confidence).
    Every node is language='python' (no cross-language filtering); the source and
    all neighbours become nodes; each edge is source -> neighbor."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            file_path TEXT, language TEXT, parent_id INTEGER, is_test INTEGER DEFAULT 0);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, confidence REAL, source_file TEXT, source_line INTEGER);
        """
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','src_fn',?,'python',NULL,0)", (source,)
    )
    nid = 2
    file_to_id: dict[str, int] = {}
    for nb, _m, _c in edges:
        if nb not in file_to_id:
            con.execute(
                "INSERT INTO nodes VALUES (?,'Function',?,?,'python',NULL,0)",
                (nid, "fn_%d" % nid, nb),
            )
            file_to_id[nb] = nid
            nid += 1
    for i, (nb, method, conf) in enumerate(edges):
        con.execute(
            "INSERT INTO edges VALUES (NULL,1,?,'CALLS',?,?,?,?)",
            (file_to_id[nb], method, conf, source, 10 + i),
        )
    con.commit()
    con.close()


def _scope_db(monkeypatch, tmp_path, name, source, edges):
    db = str(tmp_path / name)
    _make_edge_graph(db, source, edges)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_offscope_views", 0)
    g._consensus_scope.clear()
    g._seen.clear()
    return db


def test_p2_2_equal_confidence_order_is_stable_across_indexings(monkeypatch, tmp_path):
    """8 equal-confidence (import, 1.0) fact neighbours; LIMIT 6 must return the
    6 lexicographically-smallest in ASC order — and that result must be IDENTICAL
    whether the rows were inserted forward or reversed (the physical-order
    sensitivity the old ORDER-BY-non-projected-column form had)."""
    source = "focus.py"
    nbrs = ["mod_%s.py" % c for c in "abcdefgh"]  # mod_a < mod_b < ... < mod_h
    fwd_edges = [(nb, "import", 1.0) for nb in nbrs]
    rev_edges = list(reversed(fwd_edges))
    expected = nbrs[:6]  # the 6 smallest, ASC

    _scope_db(monkeypatch, tmp_path, "fwd.db", source, fwd_edges)
    out_fwd = g._query_scope(source)
    _scope_db(monkeypatch, tmp_path, "rev.db", source, rev_edges)
    out_rev = g._query_scope(source)

    assert out_fwd == expected, "forward-insertion order not deterministic: %r" % out_fwd
    assert out_rev == expected, "reversed-insertion order not deterministic: %r" % out_rev
    assert out_fwd == out_rev, (
        "scope order depends on physical row order (nondeterministic <gt-scope>): "
        "%r vs %r" % (out_fwd, out_rev)
    )
    # idempotence: repeated calls are byte-stable.
    assert g._query_scope(source) == out_rev


def test_p2_2_max_aggregation_ranks_by_best_edge(monkeypatch, tmp_path):
    """fileA reached by TWO fact edges (import 1.0 AND type_flow 0.6); fileB by a
    single import 0.7. The delivered order must rank A above B (A's MAX=1.0 > 0.7).
    The old per-row DISTINCT could pick A's 0.6 (< 0.7) and invert the ranking."""
    source = "focus.py"
    edges = [
        ("a_file.py", "import", 1.0),
        ("a_file.py", "type_flow", 0.6),  # A's secondary (lower) edge
        ("b_file.py", "import", 0.7),
    ]
    _scope_db(monkeypatch, tmp_path, "max.db", source, edges)
    out = g._query_scope(source)
    assert out == ["a_file.py", "b_file.py"], (
        "MAX-confidence aggregation did not rank A (best=1.0) above B (0.7): %r" % out
    )


def test_p2_2_consensus_block_order_matches_query_scope(monkeypatch, tmp_path):
    """_consensus_block renders the same deterministically-ordered neighbours
    (top-4 after the viewed file), in the same stable order, across indexings."""
    source = "focus.py"
    nbrs = ["mod_%s.py" % c for c in "abcdefgh"]
    fwd = [(nb, "import", 1.0) for nb in nbrs]
    rev = list(reversed(fwd))

    _scope_db(monkeypatch, tmp_path, "cfwd.db", source, fwd)
    block_fwd = g._consensus_block(source, "/repo")
    _scope_db(monkeypatch, tmp_path, "crev.db", source, rev)
    block_rev = g._consensus_block(source, "/repo")

    assert block_fwd == block_rev, (
        "consensus <gt-scope> differs across indexings (nondeterministic):\n%r\n%r"
        % (block_fwd, block_rev)
    )
    # the top-4 neighbours rendered are the 4 lex-smallest, in ASC order.
    for nb in nbrs[:4]:
        assert nb in block_fwd, (nb, block_fwd)
    # a neighbour past the deterministic top-4/6 window is not smuggled in.
    assert "mod_h.py" not in block_fwd, block_fwd


# =========================================================================== #
# P2-3 — obligation edit-credit excludes removed (old_str) code.
# =========================================================================== #
def _str_replace_action(old_body, new_body, path="src/mod.py"):
    return {
        "command": "str_replace",
        "path": path,
        "old_str": old_body,
        "new_str": new_body,
    }


def test_p2_3_credit_tokens_exclude_removed_symbol(monkeypatch):
    """The new-side symbol is a credit token; the removed (old_str) symbol is NOT.
    MUTATION: the OLD path (_edit_body_tokens over _effective_cmd) still contains
    the removed symbol — that leak is exactly what fed the false 'edited' credit."""
    action = _str_replace_action(
        old_body="def removed_symbol():\n    return old_value",
        new_body="def added_symbol():\n    return new_value",
    )
    cmd = g._effective_cmd(action)

    credit = g._edit_credit_body_tokens(action, cmd)
    assert "added_symbol" in credit, credit
    assert "removed_symbol" not in credit, (
        "removed (old_str) symbol laundered into edit-credit tokens: %r" % credit
    )

    # MUTATION direction: the pre-fix credit path (old_str merged into the body)
    # DID carry the removed symbol -> proves the exclusion is load-bearing.
    old_path_tokens = g._edit_body_tokens(cmd)
    assert "removed_symbol" in old_path_tokens, (
        "the effective-cmd body no longer carries old_str at all — the test would "
        "not bite the regression it is meant to catch"
    )


def test_p2_3_struct_content_body_drops_old_side():
    """_struct_content_body returns only the added content; _struct_body (used for
    write-detection / classification) still includes the old side (legitimate use
    of old_str preserved)."""
    action = _str_replace_action("REMOVED_TOKEN", "ADDED_TOKEN")
    content = g._struct_content_body(action)
    assert "ADDED_TOKEN" in content and "REMOVED_TOKEN" not in content, content
    full = g._struct_body(action)
    assert "ADDED_TOKEN" in full and "REMOVED_TOKEN" in full, full


def test_p2_3_pure_deletion_credits_nothing_but_is_still_a_write():
    """A str_replace that only DELETES (empty new_str): no credit tokens (nothing
    added), yet the action is still classified as a post_edit write — the fix
    scopes to the credit domain only, it does not blind write-detection."""
    action = _str_replace_action("def gone_symbol():\n    return 1", "")
    credit = g._edit_credit_body_tokens(action, g._effective_cmd(action))
    assert credit == set(), (
        "a pure deletion produced credit tokens (removed code credited): %r" % credit
    )
    se = g._structured_edit(action)
    assert se is not None and se[0] == "post_edit", (
        "pure-deletion str_replace no longer detected as a write: %r" % (se,)
    )


def test_p2_3_end_to_end_removed_symbol_not_credited(monkeypatch, tmp_path):
    """End-to-end through edit_coverage_ratio (graph unreachable -> Signal-1/
    content-lexical credit): an obligation whose symbol was DELETED is not
    credited; one whose symbol was ADDED is credited."""
    action = _str_replace_action(
        old_body="def removed_symbol():\n    return 0",
        new_body="def added_symbol():\n    return 1",
    )
    cmd = g._effective_cmd(action)
    content = g._edit_credit_body_tokens(action, cmd)
    rel = "src/mod.py"

    # removed symbol -> NOT credited (0.0). Under the pre-fix leak it would be 1.0.
    ratio_removed = g.edit_coverage_ratio(
        {"removed_symbol"}, set(),
        content_toks=content,
        edited_lines={},
        db_path="/no/such/graph.db", edited_files={rel},
    )
    assert ratio_removed == 0.0, (
        "deleted symbol was credited as edited: %r" % ratio_removed
    )

    # added symbol -> credited (1.0). Proves the fix did not nuke real credit.
    ratio_added = g.edit_coverage_ratio(
        {"added_symbol"}, set(),
        content_toks=content,
        edited_lines={},
        db_path="/no/such/graph.db", edited_files={rel},
    )
    assert ratio_added == 1.0, (
        "added symbol lost its legitimate edit credit: %r" % ratio_added
    )


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
