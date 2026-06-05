"""Contract-drift transport helper (drift_hook).

Validates the frozen-original baseline plumbing without needing the gt-index
binary (run_incremental_index is monkeypatched): build a working graph.db (post-
edit state) + a frozen <working>.orig (session-start state) and assert drift_advisory
diffs them, stays quiet without a baseline, and skips test files (zero test contact).
"""
from __future__ import annotations

import os
import sqlite3

import groundtruth.hooks.drift_hook as dh

_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, name TEXT NOT NULL,
    qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
    signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER,
    type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
    confidence REAL DEFAULT 0.0, metadata TEXT, trust_tier TEXT DEFAULT 'SPECULATIVE',
    candidate_count INTEGER DEFAULT 1, evidence_type TEXT, verification_status TEXT
);
CREATE TABLE properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL, kind TEXT NOT NULL,
    value TEXT NOT NULL, line INTEGER, confidence REAL DEFAULT 1.0
);
"""


def _mkdb(path: str, *, file_path: str = "lib.py", name: str = "get_user",
          return_shape: str = "", raises: tuple[str, ...] = ()) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    cur = conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, is_test, language) "
        "VALUES ('Function', ?, ?, 1, 5, 0, 'python')",
        (name, file_path),
    )
    nid = int(cur.lastrowid)
    if return_shape:
        conn.execute("INSERT INTO properties (node_id, kind, value, line) "
                     "VALUES (?, 'return_shape', ?, 2)", (nid, return_shape))
    for exc in raises:
        conn.execute("INSERT INTO properties (node_id, kind, value, line) "
                     "VALUES (?, 'exception_type', ?, 3)", (nid, exc))
    conn.commit()
    conn.close()


def test_drift_advisory_diffs_vs_frozen_original(tmp_path, monkeypatch):
    working = str(tmp_path / "graph.db")
    _mkdb(working, return_shape="none")                       # post-edit: return None
    _mkdb(working + ".orig", return_shape="list", raises=("KeyError",))  # frozen original
    monkeypatch.setattr(dh, "run_incremental_index", lambda *a, **k: True)
    out = dh.drift_advisory(str(tmp_path), working, ["lib.py"])
    assert "return shape: list -> none" in out
    assert "dropped raise: KeyError" in out


def test_drift_advisory_quiet_without_baseline(tmp_path, monkeypatch):
    working = str(tmp_path / "graph.db")
    _mkdb(working, return_shape="list", raises=("KeyError",))
    monkeypatch.setattr(dh, "run_incremental_index", lambda *a, **k: True)
    assert dh.drift_advisory(str(tmp_path), working, ["lib.py"]) == ""


def test_drift_advisory_skips_test_files(tmp_path, monkeypatch):
    working = str(tmp_path / "graph.db")
    _mkdb(working, file_path="tests/test_lib.py", return_shape="none")
    _mkdb(working + ".orig", file_path="tests/test_lib.py", return_shape="list")
    monkeypatch.setattr(dh, "run_incremental_index", lambda *a, **k: True)
    assert dh.drift_advisory(str(tmp_path), working, ["tests/test_lib.py"]) == ""


def test_freeze_original_creates_baseline(tmp_path):
    working = str(tmp_path / "graph.db")
    _mkdb(working, return_shape="list")
    assert dh.freeze_original(working) is True
    assert os.path.exists(working + ".orig")


def _mk_twofunc_db(path, *, a_shape, b_shape):
    """Two functions in one file: edited() lines 10-20, untouched() lines 30-40."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for name, lo, hi, shape in (("edited", 10, 20, a_shape), ("untouched", 30, 40, b_shape)):
        cur = conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line, is_test, language) "
            "VALUES ('Function', ?, 'm.py', ?, ?, 0, 'python')",
            (name, lo, hi),
        )
        conn.execute("INSERT INTO properties (node_id, kind, value, line) "
                     "VALUES (?, 'return_shape', ?, ?)", (int(cur.lastrowid), shape, lo + 1))
    conn.commit()
    conn.close()


def test_drift_scoped_to_edited_function_only(tmp_path, monkeypatch):
    """Live failure mode (beets-5495): a per-edit reindex re-parses EVERY function,
    so an UNTOUCHED multi-return function can show a different return_shape (parse
    noise) and drift would falsely flag it. With edited-function scoping, only the
    function overlapping the changed line range is diffed."""
    working = str(tmp_path / "graph.db")
    # working (post-edit): BOTH functions differ from baseline (untouched = reparse noise)
    _mk_twofunc_db(working, a_shape="dict", b_shape="collection|{}")
    # baseline (frozen pre-edit)
    _mk_twofunc_db(working + ".orig", a_shape="list", b_shape="value|pickle.load(f)")
    monkeypatch.setattr(dh, "run_incremental_index", lambda *a, **k: True)
    # The agent edited ONLY `edited` (lines 10-20) -> changed range overlaps it.
    monkeypatch.setattr(dh, "_changed_ranges", lambda root, rel: [(14, 16)])

    out = dh.drift_advisory(str(tmp_path), working, ["m.py"])
    assert "edited" in out and "list -> dict" in out
    assert "untouched" not in out  # the false-positive that broke the live run
    assert "pickle" not in out
