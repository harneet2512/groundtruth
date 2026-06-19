"""Byte-parity / fact-parity pin for the DUPLICATED witness-fact renderer.

There are TWO live copies of the deterministic caller/callee witness producer
``_resolved_witnesses_for_file``:

  * BRIEF side  : ``groundtruth.pretask.v1r_brief._resolved_witnesses_for_file``
                  signature ``(graph_db: str, file_path, repo_root, max_each)``
                  — opens its OWN sqlite connection from the db PATH.
  * PER-TURN    : ``artifact_deepswe.gt_mini_patch._resolved_witnesses_for_file``
    (live DeepSWE) signature ``(con, file_path, repo_root, max_each)`` — takes an
                  ALREADY-OPEN connection. THIS is the copy that ships in DeepSWE,
                  so it is the de-dup CANONICAL.

This test exists so the eventual serial de-dup (one copy importing the other) is
red/green-checkable. It PINS the structural fact both copies must agree on for the
SAME graph.db + file: the same (caller_symbol, callee_symbol, direction, file:line)
tuple, SAME direction. Where the two LEGITIMATELY differ (the ``relation`` key that
only the brief copy emits; the ``code`` snippet, whose source-line lookup uses
DIFFERENT path-resolution between the two copies — gt_mini_patch joins repo_root,
v1r_brief's nested ``_code_at`` is CWD-relative), the comparison normalizes to the
load-bearing tuple so those presentation deltas do not mask a real fact drift.

After de-dup this test must STILL pass: the producer the survivor delegates to must
report the byte-identical fact tuple in both directions.

Recon basis (the byte-diff the de-dup must preserve), captured live on a
single-edge graph (caller_fn in pkg/a.py CALLS callee_fn in pkg/b.py, import edge):

  gt_mini_patch render (line ~2345): ``[WITNESS] callee_fn called by -> pkg/a.py:4 `...` ``
                                      subject = w["target"]  (the symbol IN this file)
  v1r_brief tail render (line ~2213): ``resolved caller: caller_fn() in pkg/a.py:4``
                                      subject = w["symbol"]  (the EXTERNAL caller)

i.e. the two RENDER layers headline OPPOSITE endpoints of the same edge — a
documented format drift, pinned below as ``test_render_subject_drift_is_documented``
so de-dup cannot silently move it. The DICT layer (the de-dup seam) agrees, which
is what ``test_witness_dicts_agree_*`` enforces.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

# Sibling-test convention (see test_lane_a_content_dedup.py): gt_mini_patch is a
# top-level module under artifact_deepswe/, reached by prepending that dir.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as gm  # noqa: E402
from groundtruth.pretask import v1r_brief as vb  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture: one controlled repo + graph.db with ONE cross-file CALLS edge.
# pkg/a.py :: caller_fn  --CALLS(import)-->  pkg/b.py :: callee_fn
# --------------------------------------------------------------------------- #
_FILE_A = "pkg/a.py"
_FILE_B = "pkg/b.py"
_SRC_A = "from pkg.b import callee_fn\n\ndef caller_fn():\n    return callee_fn(3)\n"
_SRC_B = "def callee_fn(x):\n    return x + 1\n"
# call site is line 4 of A; definition is line 1 of B.
_CALL_LINE = 4
_DEF_LINE = 1


@pytest.fixture()
def graph(tmp_path):
    """Build a real-schema graph.db + on-disk repo with one import CALLS edge.

    Returns (db_path, repo_root). The schema mirrors CLAUDE.md's graph.db spec
    (nodes + edges with resolution_method/confidence) — the minimum both
    producers query.
    """
    repo = str(tmp_path)
    os.makedirs(os.path.join(repo, "pkg"), exist_ok=True)
    with open(os.path.join(repo, _FILE_A), "w", encoding="utf-8") as fh:
        fh.write(_SRC_A)
    with open(os.path.join(repo, _FILE_B), "w", encoding="utf-8") as fh:
        fh.write(_SRC_B)

    db = os.path.join(repo, "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0,
            language TEXT, parent_id INTEGER);
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT);
        """
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES (1,'Function','caller_fn',?,3,4,0,'python')",
        (_FILE_A,),
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES (2,'Function','callee_fn',?,1,2,0,'python')",
        (_FILE_B,),
    )
    # caller_fn(A) CALLS callee_fn(B), resolved via import => DETERMINISTIC fact.
    con.execute(
        "INSERT INTO edges(source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,2,'CALLS',?,?,'import',1.0)",
        (_CALL_LINE, _FILE_A),
    )
    con.commit()
    con.close()
    return db, repo


def _fact_tuple(w: dict) -> tuple:
    """Normalize a witness dict to the LOAD-BEARING fact, dropping presentation
    deltas (``relation`` key is brief-only; ``code`` uses divergent path lookup).

    The tuple is exactly what the de-dup must preserve: which symbol, called by /
    calling which symbol, in which direction, at which file:line.
    """
    return (
        w.get("direction"),
        w.get("symbol"),
        w.get("target"),
        (w.get("file_path") or "").replace("\\", "/"),
        int(w.get("line") or 0),
    )


def _facts(witnesses) -> set:
    return {_fact_tuple(w) for w in witnesses}


def _gm_witnesses(db, file_path, repo_root, max_each=2):
    """gt_mini_patch copy takes an OPEN connection (the per-turn calling convention)."""
    con = sqlite3.connect(db)
    try:
        return gm._resolved_witnesses_for_file(con, file_path, repo_root, max_each=max_each)
    finally:
        con.close()


def _vb_witnesses(db, file_path, repo_root, max_each=2):
    """v1r_brief copy takes the db PATH (opens/closes its own connection)."""
    return vb._resolved_witnesses_for_file(db, file_path, repo_root, max_each=max_each)


# --------------------------------------------------------------------------- #
# The parity pin: both copies report the SAME fact, SAME direction.
# --------------------------------------------------------------------------- #
def test_witness_dicts_agree_caller_direction(graph):
    """File B DEFINES callee_fn => the cross-file edge is a CALLER witness.

    Both copies must report: direction=caller, symbol=caller_fn (the external
    caller), target=callee_fn (the symbol defined in B), at pkg/a.py:4.
    """
    db, repo = graph
    gm_w = _gm_witnesses(db, _FILE_B, repo)
    vb_w = _vb_witnesses(db, _FILE_B, repo)

    # Neither copy may be silent — the edge is a deterministic import fact.
    assert gm_w, "gt_mini_patch emitted no witness for a deterministic import edge"
    assert vb_w, "v1r_brief emitted no witness for a deterministic import edge"

    expected = {("caller", "caller_fn", "callee_fn", _FILE_A, _CALL_LINE)}
    assert _facts(gm_w) == expected, f"gt_mini_patch fact drift: {_facts(gm_w)}"
    assert _facts(vb_w) == expected, f"v1r_brief fact drift: {_facts(vb_w)}"
    # The seam: the two copies agree on the fact tuple.
    assert _facts(gm_w) == _facts(vb_w)


def test_witness_dicts_agree_callee_direction(graph):
    """File A CALLS callee_fn cross-file => the edge is a CALLEE witness.

    Both copies must report: direction=callee, symbol=callee_fn (the called
    symbol, defined in B), target=caller_fn (the source), at pkg/b.py:1.
    """
    db, repo = graph
    gm_w = _gm_witnesses(db, _FILE_A, repo)
    vb_w = _vb_witnesses(db, _FILE_A, repo)

    assert gm_w, "gt_mini_patch emitted no callee witness"
    assert vb_w, "v1r_brief emitted no callee witness"

    expected = {("callee", "callee_fn", "caller_fn", _FILE_B, _DEF_LINE)}
    assert _facts(gm_w) == expected, f"gt_mini_patch fact drift: {_facts(gm_w)}"
    assert _facts(vb_w) == expected, f"v1r_brief fact drift: {_facts(vb_w)}"
    assert _facts(gm_w) == _facts(vb_w)


def test_direction_not_inverted(graph):
    """R5 inversion guard: the caller is ALWAYS the external symbol, the target the
    in-file symbol. An inverted copy would report symbol==callee_fn for the caller
    direction (reading "caller called by callee"). Pin that it does NOT.
    """
    db, repo = graph
    for producer in (_gm_witnesses, _vb_witnesses):
        w = producer(db, _FILE_B, repo)[0]
        assert w["direction"] == "caller"
        # The EXTERNAL caller is caller_fn; the symbol DEFINED in this file is callee_fn.
        assert w["symbol"] == "caller_fn", "caller/callee inverted in the dict"
        assert w["target"] == "callee_fn"


# --------------------------------------------------------------------------- #
# The documented RENDER drift (the byte-diff). Both renders describe the SAME
# edge but headline OPPOSITE endpoints. Pinned so de-dup cannot silently move it;
# this is the format divergence the prompt's byte-diff table records.
# --------------------------------------------------------------------------- #
def _gm_render(w: dict) -> str:
    """The exact gt_mini_patch [WITNESS] render (gt_mini_patch.py ~line 2345)."""
    arrow = "called by" if w["direction"] == "caller" else "calls"
    loc = f"{w['file_path']}:{w['line']}" if w["line"] else w["file_path"]
    sym = (w["target"] if w["direction"] == "caller" else w["symbol"]) or "?"
    snippet = f" `{w['code']}`" if w.get("code") else ""
    return f"[WITNESS] {sym} {arrow} -> {loc}{snippet}".rstrip()


def test_render_subject_drift_is_documented(graph):
    """The two render layers headline OPPOSITE endpoints of the one edge.

    gt_mini_patch ``[WITNESS]`` headlines the in-file symbol (w['target']).
    v1r_brief ``_resolved_witness_tail`` headlines the external caller (w['symbol']).
    Both are 'correct' descriptions of the same edge; the divergence is in WHICH
    endpoint leads. This test PINS the current strings so de-dup is forced to make
    a conscious choice, not an accidental one.
    """
    db, repo = graph
    gm_w = _gm_witnesses(db, _FILE_B, repo)[0]
    gm_line = _gm_render(gm_w)
    vb_line = vb._resolved_witness_tail(db, _FILE_B)

    # gt_mini_patch headlines the in-file (target) symbol; path uses forward slashes.
    assert gm_line.startswith("[WITNESS] callee_fn called by -> pkg/a.py:4")
    # v1r_brief headlines the EXTERNAL caller (symbol).
    assert vb_line == "resolved caller: caller_fn() in pkg/a.py:4"

    # The drift, stated as a fact: different subject symbol, SAME underlying edge.
    assert "callee_fn" in gm_line and "caller_fn" in vb_line
    assert gm_line != vb_line  # the byte-diff the de-dup must reconcile deliberately
