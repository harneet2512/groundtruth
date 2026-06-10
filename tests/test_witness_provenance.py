"""P0 fix 2026-06-09 — witness-pipe provenance (correct-or-quiet at the witness level).

Two laundering holes in the L1 localizer's witness pipe, both fixed in
``groundtruth.pretask.graph_localizer``:

  1. ``Candidate.render_witness`` claimed-but-never-emitted the ``(unverified)``
     tag for non-deterministic (name_match-grade) edge witnesses — v1r_brief's
     renderer documents "a name_match witness carries its own '(unverified)' tag
     from the localizer", yet the localizer never minted one, so a name GUESS
     rendered exactly like a structural FACT.

  2. EVERY seed (exact-name, grep, path, FTS5) minted a ``DEFINES`` witness
     ``defines {name} (issue symbol)`` at confidence 1.0 — but only the
     exact-name seeder proves the issue NAMED that symbol. Grep/path/FTS5 seeds
     are retrieval ENTRY POINTS; minting DEFINES for them fabricated a
     verified-grade localization fact out of a string/path/BM25 match, which the
     [VERIFIED] gate / verified-first sort / HIGH header consumed as truth.

Plus the smaller `_grep_to_seeds` rg-vanish fix: a FileNotFoundError on ONE
token must skip THAT token (continue), not silently abort all remaining tokens
(the old ``break`` — with no fallback on that branch, recall was simply lost).

All inputs are synthetic and generalized — no benchmark task IDs, no gold files.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.graph_localizer import Candidate, Witness, localize


# ---------------------------------------------------------------------------
# (1) render_witness provenance tag — unverified edges marked, verified not
# ---------------------------------------------------------------------------


def _edge_witness(*, verified: bool, hop: int = 1) -> Witness:
    return Witness(
        file_path="app/a.py",
        anchor="set_fields",
        edge_type="CALLS",
        direction="calls_anchor",
        verified=verified,
        confidence=0.6,
        hop=hop,
        src_symbol="set_fields",
        dst_symbol="set_parse",
    )


def _cand(wits: list[Witness]) -> Candidate:
    return Candidate(
        file_path="app/a.py", score=1.0, witnesses=wits,
        lex_hits=0, degree=0, confidence=0.6,
    )


def test_render_witness_tags_unverified_edge():
    """RED before the fix: no tag was ever emitted — a name_match edge rendered
    identically to a deterministic fact."""
    out = _cand([_edge_witness(verified=False)]).render_witness()
    assert "(unverified)" in out, f"unverified edge rendered as fact: {out!r}"


def test_render_witness_verified_edge_has_no_tag():
    out = _cand([_edge_witness(verified=True)]).render_witness()
    assert "(unverified)" not in out, f"verified fact wrongly tagged: {out!r}"
    assert "set_fields" in out and "set_parse" in out


def test_render_witness_multihop_unverified_tagged():
    out = _cand([_edge_witness(verified=False, hop=2)]).render_witness()
    assert "(unverified)" in out
    assert "2-hop" in out


# ---------------------------------------------------------------------------
# (2) Seed provenance — only exact-name seeds mint the DEFINES issue-symbol fact
# ---------------------------------------------------------------------------

_SCHEMA = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
    " name TEXT, qualified_name TEXT, file_path TEXT NOT NULL,"
    " start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
    " is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,"
    " language TEXT, parent_id INTEGER);"
    "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER,"
    " target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT,"
    " resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT);"
)


def _mk_graph(path: str, nodes: list[tuple[str, str]]) -> None:
    """nodes = [(name, file_path), ...] — all Functions, non-test.

    Each node gets SLOC > 4 (start/end lines) so the Herbold trivial-function
    role discount (sloc<=4 AND fan_out==0 -> DEFINES demote, by design) does not
    fire — this test is about SEED provenance, not the role discount."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for name, fp in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
            "is_test, language) VALUES ('Function', ?, ?, 1, 40, 0, 'python')",
            (name, fp),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def _no_semantic(monkeypatch):
    """Keep the localizer's semantic ranker off (deterministic 2-signal path) so
    the test never loads/downloads a model."""
    from groundtruth.pretask import graph_localizer as gl

    monkeypatch.setattr(gl, "_EMBEDDER", None)
    monkeypatch.setattr(gl, "_EMBEDDER_TRIED", True)


def test_grep_seed_never_mints_defines_issue_symbol(tmp_path, _no_semantic):
    """A file that enters ONLY via grep (its content contains an issue token; the
    issue does NOT name any of its symbols) must carry a seed-typed, UNVERIFIED
    witness — never the fabricated 'defines {name} (issue symbol)' at conf 1.0.

    RED before the fix: pipeline.py rendered 'defines run_stage (issue symbol)'
    with a verified DEFINES witness."""
    db = str(tmp_path / "graph.db")
    _mk_graph(db, [("set_fields", "app/importer.py"), ("run_stage", "app/pipeline.py")])
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "importer.py").write_text(
        "def set_fields():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app" / "pipeline.py").write_text(
        "def run_stage():\n    zorbafrobnicate()\n", encoding="utf-8"
    )

    res = localize(
        "set_fields crashes when zorbafrobnicate mode is on",
        db,
        issue_anchors=IssueAnchors(symbols={"set_fields"}),
        repo_root=str(tmp_path),
    )
    by_file = {c.file_path: c for c in res.candidates}
    assert "app/importer.py" in by_file and "app/pipeline.py" in by_file

    # Exact-name seed: the issue NAMED set_fields -> the DEFINES fact is legitimate.
    imp = by_file["app/importer.py"]
    assert imp.render_witness() == "defines set_fields (issue symbol)"
    assert imp.has_verified_witness

    # Grep-only seed: never a DEFINES issue-symbol fact, never verified.
    pipe = by_file["app/pipeline.py"]
    assert not pipe.has_verified_witness, (
        "grep seed laundered into a verified witness"
    )
    w = pipe.render_witness()
    assert "defines" not in w, f"fabricated DEFINES witness rendered: {w!r}"
    assert w.startswith(("grep match", "path match", "fts5 match", "seed match")), w

    # INVARIANT: every DEFINES witness anchors a symbol the issue actually named.
    for c in res.candidates:
        for wt in c.witnesses:
            if wt.edge_type == "DEFINES":
                assert wt.anchor == "set_fields", (
                    f"DEFINES minted for non-issue symbol {wt.anchor!r} on {c.file_path}"
                )


def test_path_seed_witness_is_seed_typed(tmp_path, _no_semantic):
    """A file seeded by a PATH-component match ('flex' -> layout/flex.py, no
    symbol named in the issue) carries a seed-typed witness, not a DEFINES."""
    db = str(tmp_path / "graph.db")
    _mk_graph(db, [("flex_layout", "layout/flex.py")])
    (tmp_path / "layout").mkdir()
    (tmp_path / "layout" / "flex.py").write_text(
        "def flex_layout():\n    pass\n", encoding="utf-8"
    )

    res = localize(
        "the flex container overflows badly",
        db,
        issue_anchors=IssueAnchors(),  # NO symbol anchors — path/grep only
        repo_root=str(tmp_path),
    )
    by_file = {c.file_path: c for c in res.candidates}
    assert "layout/flex.py" in by_file
    c = by_file["layout/flex.py"]
    assert not c.has_verified_witness
    w = c.render_witness()
    assert "defines" not in w, f"fabricated DEFINES witness rendered: {w!r}"
    assert w.startswith(("path match", "grep match", "fts5 match", "seed match")), w
    # The path seeder ran first, so the witness should carry the matched token.
    if w.startswith("path match"):
        assert w == "path match: flex"


# ---------------------------------------------------------------------------
# (3) _grep_to_seeds rg-vanish: per-token skip, not silent abort
# ---------------------------------------------------------------------------


def test_grep_to_seeds_rg_vanish_skips_token_not_all(tmp_path, monkeypatch):
    """RED before the fix: a FileNotFoundError on token #1 hit `break`, silently
    dropping ALL remaining tokens' recall (no fallback runs on that branch). Now
    it `continue`s: token #2 still recalls its file."""
    import shutil
    import subprocess

    from groundtruth.pretask import graph_localizer as gl

    db = str(tmp_path / "graph.db")
    _mk_graph(db, [("do_thing", "hit.py")])
    (tmp_path / "hit.py").write_text("betatoken = 1\n", encoding="utf-8")

    calls = {"n": 0}

    class _R:
        returncode = 0
        stdout = str(tmp_path / "hit.py") + "\n"

    def _fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError("rg vanished mid-loop")
        return _R()

    monkeypatch.setattr(shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    conn = sqlite3.connect(db)
    try:
        seeds = gl._grep_to_seeds(
            {"alphalongtoken", "betatoken"}, str(tmp_path), conn, max_seeds=10
        )
    finally:
        conn.close()

    assert calls["n"] >= 2, "second token never attempted (silent abort)"
    assert seeds, "recall lost: surviving token's hit produced no seeds"
    assert seeds[0][2] == "hit.py"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
