"""Contract DRIFT engine (contract_map.build_drift / snapshot_contract).

The NEW capability (not the already-shipped contract context): after the agent
edits a file and GT re-indexes it, diff the edit-target's behavioral contract
pre vs post and surface only MATERIAL drift ("return shape: list -> None;
N callers depend on this", "dropped raise: KeyError"). Correct-or-quiet: empty
when nothing material changed.

Keying is by (file, name) NOT node_id — an incremental reindex is DELETE+INSERT
so node ids change; these tests simulate that by re-inserting the post-edit node
with a FRESH id and assert drift is still detected.

Zero test contact: drift reads only nodes/edges/properties (is_test=0 callers);
never the assertions table, never test names.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.contract_map import build_drift, snapshot_contract

_FILE = "importer.py"
_FUNC = "set_fields"


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, type TEXT NOT NULL,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT, trust_tier TEXT DEFAULT 'SPECULATIVE',
            candidate_count INTEGER DEFAULT 1, evidence_type TEXT,
            verification_status TEXT DEFAULT 'unverified'
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            line INTEGER, confidence REAL DEFAULT 1.0
        );
        """
    )
    conn.commit()
    conn.close()


def _add_func(
    path: str,
    *,
    file_path: str = _FILE,
    name: str = _FUNC,
    return_shape: str = "",
    return_type: str = "",
    raises: tuple[str, ...] = (),
    guards: tuple[str, ...] = (),
) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
        "signature, return_type, is_test, language) "
        "VALUES ('Function', ?, ?, 10, 30, ?, ?, 0, 'python')",
        (name, file_path, f"def {name}(self, key)", return_type),
    )
    nid = int(cur.lastrowid)
    for sh in ([return_shape] if return_shape else []):
        conn.execute(
            "INSERT INTO properties (node_id, kind, value, line) VALUES (?, 'return_shape', ?, 12)",
            (nid, sh),
        )
    for exc in raises:
        conn.execute(
            "INSERT INTO properties (node_id, kind, value, line) VALUES (?, 'exception_type', ?, 14)",
            (nid, exc),
        )
    for g in guards:
        conn.execute(
            "INSERT INTO properties (node_id, kind, value, line) VALUES (?, 'guard_clause', ?, 11)",
            (nid, g),
        )
    conn.commit()
    conn.close()
    return nid


def _reindex_func(path: str, **kwargs) -> int:
    """Simulate `gt-index -file`: delete the function's node+props, reinsert with
    a FRESH node id. Proves drift keys by (file, name), not node_id."""
    conn = sqlite3.connect(path)
    conn.execute(
        "DELETE FROM properties WHERE node_id IN "
        "(SELECT id FROM nodes WHERE file_path = ? AND name = ?)",
        (kwargs.get("file_path", _FILE), kwargs.get("name", _FUNC)),
    )
    conn.execute(
        "DELETE FROM nodes WHERE file_path = ? AND name = ?",
        (kwargs.get("file_path", _FILE), kwargs.get("name", _FUNC)),
    )
    conn.commit()
    conn.close()
    return _add_func(path, **kwargs)


def test_drift_return_shape_and_dropped_raise(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _add_func(db, return_shape="list", raises=("KeyError",))

    pre = snapshot_contract(db, _FILE, [_FUNC])
    assert pre[_FUNC]["return_shape"] == "list"
    assert "KeyError" in pre[_FUNC]["raises"]

    # Agent edits: return becomes a dict, the KeyError guard is gone. Reindex.
    _reindex_func(db, return_shape="dict")  # no raises now

    out = build_drift(db, _FILE, [_FUNC], pre_snapshot=pre)
    assert "return shape: list" in out and "dict" in out
    assert "dropped raise: KeyError" in out
    assert "<gt-drift>" in out


def test_drift_quiet_on_noop(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _add_func(db, return_shape="list", raises=("KeyError",))
    pre = snapshot_contract(db, _FILE, [_FUNC])
    # No edit, no reindex change.
    assert build_drift(db, _FILE, [_FUNC], pre_snapshot=pre) == ""


def test_drift_new_raise(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _add_func(db, return_shape="list")
    pre = snapshot_contract(db, _FILE, [_FUNC])
    _reindex_func(db, return_shape="list", raises=("ValueError",))
    out = build_drift(db, _FILE, [_FUNC], pre_snapshot=pre)
    assert "new raise: ValueError" in out


def test_drift_caller_count_in_header(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    target_id = _add_func(db, return_shape="list")
    # A verified (import) caller of set_fields, in a non-test file.
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
        "is_test, language) VALUES ('Function', 'run', 'cli.py', 5, 9, 0, 'python')",
    )
    caller_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence, trust_tier) "
        "VALUES (?, ?, 'CALLS', 'import', 1.0, 'CERTIFIED')",
        (caller_id, target_id),
    )
    conn.commit()
    conn.close()

    pre = snapshot_contract(db, _FILE, [_FUNC])
    # Mutate in place (same node id) so the verified caller edge stays valid;
    # node-id instability is already covered by the reindex tests above.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE properties SET value = 'dict' WHERE node_id = ? AND kind = 'return_shape'",
        (target_id,),
    )
    conn.commit()
    conn.close()
    out = build_drift(db, _FILE, [_FUNC], pre_snapshot=pre)
    assert "return shape: list -> dict" in out
    assert "1 verified caller" in out


def test_drift_function_removed(tmp_path):
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _add_func(db, return_shape="list", raises=("KeyError",))
    pre = snapshot_contract(db, _FILE, [_FUNC])
    # Agent renamed/removed the function: delete the node, no reinsert.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM properties WHERE node_id IN "
                 "(SELECT id FROM nodes WHERE file_path = ? AND name = ?)", (_FILE, _FUNC))
    conn.execute("DELETE FROM nodes WHERE file_path = ? AND name = ?", (_FILE, _FUNC))
    conn.commit()
    conn.close()
    out = build_drift(db, _FILE, [_FUNC], pre_snapshot=pre)
    assert out and ("removed" in out.lower() or "renamed" in out.lower())


def test_drift_unknown_func_no_baseline(tmp_path):
    """A func with no pre-snapshot baseline produces no drift (nothing to diff)."""
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _add_func(db, return_shape="list")
    pre = snapshot_contract(db, _FILE, [_FUNC])
    # Diff a different function name we never snapshotted.
    assert build_drift(db, _FILE, ["other_func"], pre_snapshot=pre) == ""
