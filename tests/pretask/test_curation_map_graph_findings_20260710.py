"""Graph→consumer findings on curation_map (Fable bounce 2026-07-10).

Graph-F4 (LEAK FAMILY): ``_focus_depth_rels`` gated only ``nt.is_test = 0``, while the
sibling ``_neighbors`` ALSO drops ``_is_test_or_demo`` / ``_is_vendored_path`` targets.
On a frozen/stale-``is_test`` graph a depth-rel target in ``tests/helper.py`` (is_test=0)
or ``node_modules/…`` therefore rendered in ``<gt-graph-map>``. The fix applies the same
file-level drop to depth-rel targets.

Graph-F7 (cross-file contamination): ``_node_ids`` matched ``LIKE '%'||norm_fp`` with no
``/`` boundary, so focus ``app/db.py`` unioned nodes from ``webapp/db.py``. The fix keeps
the witness-twin suffix tolerance (a genuine nested ``src/app/db.py`` still matches) but
adds the path boundary (``= norm`` OR ``LIKE '%/'||norm``).
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask import curation_map as cm

_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
        is_test INTEGER DEFAULT 0
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL, metadata TEXT
    );
"""


# ── Graph-F4: depth-rel targets in test/vendored paths must not render ────────
def _f4_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (?,?,?,?,?)",
        [
            (1, "Method", "increment", "counter.py", 0),        # focus (non-test)
            (2, "Function", "helper_fn", "tests/helper.py", 0),  # TEST path, is_test=0 (stale)
            (3, "Function", "save", "src/model.py", 0),          # legit source target
            (4, "Function", "vend", "node_modules/x/index.js", 0),  # VENDORED path
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1, 2, "WRITES", "promote_write", 0.9, "_a"),  # -> tests/helper.py  (LEAK)
            (1, 3, "WRITES", "promote_write", 0.9, "_b"),  # -> src/model.py     (legit)
            (1, 4, "WRITES", "promote_write", 0.9, "_c"),  # -> node_modules     (LEAK)
        ],
    )
    conn.commit()


def test_depth_rel_drops_test_and_vendored_targets():
    conn = sqlite3.connect(":memory:")
    _f4_db(conn)
    has_conf, has_method = cm._has_columns(conn)
    rels = cm._focus_depth_rels(conn, [1], has_conf=has_conf, has_method=has_method)
    files = {r.target_file for r in rels}
    names = {r.target for r in rels}
    # Legit source target survives.
    assert "src/model.py" in files
    assert "save" in names
    # Test-path and vendored targets are dropped (leak-safe, parity with _neighbors).
    assert "tests/helper.py" not in files
    assert "helper_fn" not in names
    assert "node_modules/x/index.js" not in files
    assert "vend" not in names


# ── Graph-F7: _node_ids must not pull a foreign file across a suffix boundary ──
def _f7_db(conn: sqlite3.Connection, rows: list[tuple[int, str, str]]) -> None:
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (?,?,?,?,0)",
        [(nid, "Function", name, fp) for nid, name, fp in rows],
    )
    conn.commit()


def test_node_ids_suffix_boundary_no_cross_file_contamination():
    conn = sqlite3.connect(":memory:")
    _f7_db(
        conn,
        [
            (1, "handler", "app/db.py"),      # the focus file
            (2, "handler", "webapp/db.py"),   # a FOREIGN file that suffix-matches 'app/db.py'
        ],
    )
    ids = cm._node_ids(conn, "app/db.py", "handler")
    assert ids == [1], f"expected only app/db.py node 1, got {ids} (webapp/db.py leaked in)"


def test_node_ids_preserves_nested_suffix_tolerance():
    """The witness-twin suffix tolerance must SURVIVE: a genuinely nested
    ``src/app/db.py`` (a real path-boundary suffix of the focus key) still matches."""
    conn = sqlite3.connect(":memory:")
    _f7_db(conn, [(7, "handler", "src/app/db.py")])
    ids = cm._node_ids(conn, "app/db.py", "handler")
    assert ids == [7], f"nested src/app/db.py should still match via /-boundary suffix, got {ids}"


def test_node_ids_exact_match_still_works():
    conn = sqlite3.connect(":memory:")
    _f7_db(conn, [(3, "handler", "app/db.py")])
    assert cm._node_ids(conn, "app/db.py", "handler") == [3]


# ── LEGACY-SCHEMA back-compat (must not crash; stays quiet-or-correct) ────────
def test_depth_rel_legacy_schema_without_confidence_still_filters():
    """Graph-F4 back-compat: an edges table with NO ``confidence`` column (has_conf=False)
    must not crash AND must still drop test/vendored targets (the leak filter is
    column-independent)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,
            is_test INTEGER DEFAULT 0);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, resolution_method TEXT, metadata TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (?,?,?,?,0)",
        [(1, "Method", "increment", "counter.py"),
         (2, "Function", "helper_fn", "tests/helper.py"),
         (3, "Function", "save", "src/model.py")],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,metadata) VALUES (?,?,?,?,?)",
        [(1, 2, "WRITES", "promote_write", "_a"), (1, 3, "WRITES", "promote_write", "_b")],
    )
    conn.commit()
    has_conf, has_method = cm._has_columns(conn)
    assert has_conf is False  # legacy: no confidence column
    rels = cm._focus_depth_rels(conn, [1], has_conf=has_conf, has_method=has_method)
    files = {r.target_file for r in rels}
    assert "src/model.py" in files          # legit target survives (promote provenance trusted)
    assert "tests/helper.py" not in files    # leak filter still applies on legacy schema
