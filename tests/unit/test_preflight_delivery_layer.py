"""Delivery-layer preflight — proves it FAILS fast on each C6/C7 runtime fuckup."""
import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "verify"))
import preflight_delivery_layer as pf  # noqa: E402


def _db(*, closure_table: bool, closure_rows: int, lsp_edges: int,
        name_match_edges: int, verified_calls: int) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, label TEXT)")
    c.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
              "type TEXT, resolution_method TEXT, confidence REAL)")
    eid = 0
    for _ in range(lsp_edges):
        eid += 1
        c.execute("INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence) "
                  "VALUES (?,1,2,'CALLS','lsp',1.0)", (eid,))
    for _ in range(name_match_edges):
        eid += 1
        c.execute("INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence) "
                  "VALUES (?,1,2,'CALLS','name_match',0.3)", (eid,))
    # extra verified same_file calls (not lsp) to populate the closure universe
    for _ in range(max(0, verified_calls - lsp_edges)):
        eid += 1
        c.execute("INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence) "
                  "VALUES (?,1,2,'CALLS','same_file',1.0)", (eid,))
    if closure_table:
        c.execute("CREATE TABLE closure (source_id INT, target_id INT, depth INT, min_confidence REAL)")
        for _ in range(closure_rows):
            c.execute("INSERT INTO closure VALUES (1,2,1,1.0)")
    c.commit()
    c.close()
    return path


def _args(**kw) -> argparse.Namespace:
    base = dict(schema=False, closure=False, closure_populated=False, require_lsp=False, lsp_report=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_all_pass_when_healthy():
    db = _db(closure_table=True, closure_rows=5, lsp_edges=3, name_match_edges=2, verified_calls=4)
    try:
        fails = pf.run(db, _args(schema=True, closure=True, closure_populated=True, require_lsp=True))
        assert fails == [], fails
    finally:
        os.unlink(db)


def test_closure_table_missing_fails():
    db = _db(closure_table=False, closure_rows=0, lsp_edges=1, name_match_edges=0, verified_calls=2)
    try:
        fails = pf.run(db, _args(closure=True))
        assert any("closure' table missing" in f for f in fails)
    finally:
        os.unlink(db)


def test_closure_empty_with_verified_calls_fails():
    db = _db(closure_table=True, closure_rows=0, lsp_edges=1, name_match_edges=0, verified_calls=3)
    try:
        fails = pf.run(db, _args(closure_populated=True))
        assert any("EMPTY but" in f for f in fails)
    finally:
        os.unlink(db)


def test_closure_empty_with_no_verified_calls_ok():
    # no verified CALLS edges -> empty closure is legitimate (e.g. tiny preflight repo)
    db = _db(closure_table=True, closure_rows=0, lsp_edges=0, name_match_edges=2, verified_calls=0)
    try:
        fails = pf.run(db, _args(closure_populated=True))
        assert fails == [], fails
    finally:
        os.unlink(db)


def test_require_lsp_fails_on_unpromoted_db():
    # the silent '|| WARN'-swallowed un-promoted case: zero lsp edges
    db = _db(closure_table=True, closure_rows=1, lsp_edges=0, name_match_edges=5, verified_calls=1)
    try:
        fails = pf.run(db, _args(require_lsp=True))
        assert any("ZERO resolution_method='lsp'" in f for f in fails)
    finally:
        os.unlink(db)


def test_require_lsp_passes_when_promoted():
    db = _db(closure_table=True, closure_rows=1, lsp_edges=4, name_match_edges=1, verified_calls=4)
    try:
        fails = pf.run(db, _args(require_lsp=True))
        assert fails == [], fails
    finally:
        os.unlink(db)


def test_schema_missing_columns_fails():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT)")
    c.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT)")
    c.commit(); c.close()
    try:
        fails = pf.run(path, _args(schema=True))
        assert any("resolution_method" in f for f in fails)
        assert any("confidence" in f for f in fails)
    finally:
        os.unlink(path)
