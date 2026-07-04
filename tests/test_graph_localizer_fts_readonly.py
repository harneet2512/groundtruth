"""A8 integrity invariant — the FTS5 fallback must NEVER write graph.db.

`_fts5_candidates` builds nodes_fts on demand when the Go indexer's native table is
absent. Pre-fix it did `sqlite3.connect(graph.db)` + CREATE/INSERT INTO the source db,
mutating a read-only artifact (changed its sha256, could race a concurrent reader).
Post-fix it builds a PRIVATE in-memory index with graph.db ATTACHed read-only.

Permanent invariant: sha256(graph.db) is byte-identical across a localizer query, and no
-wal/-shm/-journal sidecar appears. RED pre-fix (sha256 changes), GREEN post-fix.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC))

from groundtruth.pretask import graph_localizer  # noqa: E402

_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
    parent_id INTEGER
);
"""


def _sha(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _build(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)  # NO nodes_fts -> forces the Python-side fallback
    c.execute("INSERT INTO nodes VALUES (1,'Function','parse_schema','parse_schema',"
              "'ark/json_schema/parse.ts',3,9,'(s: string)','Schema',1,0,'typescript',0)")
    c.execute("INSERT INTO nodes VALUES (2,'Function','unrelated','unrelated',"
              "'other/x.ts',1,4,'()','void',1,0,'typescript',0)")
    c.commit()
    c.close()


def test_fts5_fallback_never_writes_graph_db(tmp_path):
    gdb = tmp_path / "graph.db"
    _build(str(gdb))
    before = _sha(str(gdb))

    conn = sqlite3.connect(str(gdb))
    try:
        results = graph_localizer._fts5_candidates(conn, {"parse", "schema"}, limit=10)
    finally:
        conn.close()

    # the fallback fired and returned the matching symbol (index was built + queried)
    assert any("parse" in r[1] for r in results), f"FTS fallback returned nothing: {results}"
    # INVARIANT: graph.db is byte-identical — the query path wrote nothing
    assert _sha(str(gdb)) == before, "graph.db was MUTATED by the read-only FTS query path"
    # and no journal/wal sidecar leaked next to the source
    sidecars = [f for f in os.listdir(tmp_path) if f != "graph.db"]
    assert sidecars == [], f"query path left sidecar files: {sidecars}"
