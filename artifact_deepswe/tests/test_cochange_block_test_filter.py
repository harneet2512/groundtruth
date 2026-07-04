"""Pin: the LIVE <gt-cochange> producer (gt_mini_patch._cochange_block) must drop
test/demo co-change partners. The csstree witness (2026-06-15) leaked `test/lexer.js`
into the brief's "Also changes:"; the live block had the SAME gap — it filtered
_is_vendored_path but not test/demo (BUG-A 4th leak site)."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _make_cochange_graph(path: str, partner: str) -> None:
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE cochanges (file_a TEXT, file_b TEXT, count INTEGER);")
    con.execute("INSERT INTO cochanges VALUES (?,?,?)", ("lib/lexer/Lexer.js", partner, 5))
    con.commit()
    con.close()


def _wire(monkeypatch, db: str) -> None:
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_cochange_fired", False)
    monkeypatch.setattr(g, "_db_path", lambda: db)


def test_cochange_block_drops_test_partner(monkeypatch, tmp_path):
    # the exact csstree leak: a top-level test/ dir co-change partner
    db = str(tmp_path / "g.db")
    _make_cochange_graph(db, "test/lexer.js")
    _wire(monkeypatch, db)
    out = g._cochange_block("lib/lexer/Lexer.js")
    # only partner is a test file -> excluded -> correct-or-quiet (empty)
    assert out == "" and "test/lexer.js" not in out


def test_cochange_block_drops_demo_partner(monkeypatch, tmp_path):
    db = str(tmp_path / "gd.db")
    _make_cochange_graph(db, "examples/demo/run.js")
    _wire(monkeypatch, db)
    out = g._cochange_block("lib/lexer/Lexer.js")
    assert out == "" and "examples/" not in out


def test_cochange_block_keeps_real_source_partner(monkeypatch, tmp_path):
    # control: a real source co-change partner must STILL be delivered
    db = str(tmp_path / "gs.db")
    _make_cochange_graph(db, "lib/lexer/match.js")
    _wire(monkeypatch, db)
    out = g._cochange_block("lib/lexer/Lexer.js")
    assert "<gt-cochange>" in out and "match.js" in out
