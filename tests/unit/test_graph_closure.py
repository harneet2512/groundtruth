"""C7 (RF-4) closure-table fast path + BFS-fallback equivalence for ImportGraph.

These tests build synthetic Go-indexer-schema graph.db files directly (so we
exercise the real GraphStore bridge that production uses) in two variants:

  (a) WITH a `closure` table  -> find_callers / get_impact_radius read it via
      the indexed SELECT and return the *transitive* verified reach.
  (b) WITHOUT a `closure` table -> the same calls fall back to the live 1-hop
      BFS over edges. For a 1-hop graph the two paths return the same files,
      proving zero regression on pre-C7 databases.

The closure table is what the Go indexer's internal/closure package writes;
here we populate it by hand to keep the test Go-free (Go is CI-verified).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Ok


def _make_graph_db(path: str, *, with_closure: bool) -> None:
    """Create a synthetic Go-indexer graph.db.

    Call graph (CALLS edges, all VERIFIED — same_file / import, conf 1.0):

        a()  ->  b()  ->  target()

    Files:
        node 1 = target  in src/target.py
        node 2 = b       in src/b.py        (direct caller of target)
        node 3 = a       in src/a.py        (transitive caller of target via b)

    The closure (when present) records who transitively REACHES each node:
        target (1) is reached by b (2) at depth 1 and a (3) at depth 2.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    # nodes
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_exported, is_test, language) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Function", "target", "src/target.py", 1, 10, 1, 0, "python"),
            (2, "Function", "b", "src/b.py", 1, 10, 1, 0, "python"),
            (3, "Function", "a", "src/a.py", 1, 10, 1, 0, "python"),
        ],
    )
    # edges: a -> b -> target, both VERIFIED (import, conf 1.0)
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        [
            (2, 1, "CALLS", 5, "src/b.py", "import", 1.0),  # b -> target
            (3, 2, "CALLS", 5, "src/a.py", "import", 1.0),  # a -> b
        ],
    )
    if with_closure:
        conn.executescript(
            """
            CREATE TABLE closure (
                source_id INTEGER,
                target_id INTEGER,
                depth INTEGER,
                min_confidence REAL,
                PRIMARY KEY(source_id, target_id, depth)
            );
            CREATE INDEX idx_closure_source ON closure(source_id);
            CREATE INDEX idx_closure_target ON closure(target_id);
            """
        )
        conn.executemany(
            "INSERT INTO closure (source_id, target_id, depth, min_confidence) "
            "VALUES (?,?,?,?)",
            [
                (2, 1, 1, 1.0),  # b transitively reaches target (1 hop)
                (3, 2, 1, 1.0),  # a transitively reaches b (1 hop)
                (3, 1, 2, 1.0),  # a transitively reaches target (2 hops)
            ],
        )
    conn.commit()
    conn.close()


def _open(path: str) -> GraphStore:
    store = GraphStore(db_path=path)
    res = store.initialize()
    assert isinstance(res, Ok), res
    return store


def _has_closure_table(store: GraphStore) -> bool:
    row = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='closure'"
    ).fetchone()
    return row is not None


class TestClosureFastPath:
    def test_db_has_closure_table(self, tmp_path: Path) -> None:
        db = str(tmp_path / "with_closure.db")
        _make_graph_db(db, with_closure=True)
        store = _open(db)
        assert _has_closure_table(store) is True

    def test_impact_uses_closure_transitive(self, tmp_path: Path) -> None:
        """With a closure table, impact of target() includes the TRANSITIVE
        caller a() (2 hops away) — proof the closure (not just 1-hop BFS) is
        the source of the answer."""
        db = str(tmp_path / "with_closure.db")
        _make_graph_db(db, with_closure=True)
        store = _open(db)
        graph = ImportGraph(store)

        result = graph.get_impact_radius("target")
        assert isinstance(result, Ok)
        # b.py (direct) AND a.py (transitive) both impacted via the closure.
        assert sorted(result.value.impacted_files) == ["src/a.py", "src/b.py"]
        assert result.value.impact_radius == 2

    def test_trace_callers_uses_closure_transitive(self, tmp_path: Path) -> None:
        db = str(tmp_path / "with_closure.db")
        _make_graph_db(db, with_closure=True)
        store = _open(db)
        graph = ImportGraph(store)

        result = graph.find_callers("target")
        assert isinstance(result, Ok)
        files = sorted(r.file_path for r in result.value)
        assert files == ["src/a.py", "src/b.py"]


class TestBfsFallbackNoClosure:
    def test_db_lacks_closure_table(self, tmp_path: Path) -> None:
        db = str(tmp_path / "no_closure.db")
        _make_graph_db(db, with_closure=False)
        store = _open(db)
        assert _has_closure_table(store) is False

    def test_impact_falls_back_to_bfs(self, tmp_path: Path) -> None:
        """Without a closure table, impact of target() falls back to the live
        1-hop edge query — only the DIRECT caller b() is returned (no
        transitive a()). The query must not raise on the missing table."""
        db = str(tmp_path / "no_closure.db")
        _make_graph_db(db, with_closure=False)
        store = _open(db)
        graph = ImportGraph(store)

        result = graph.get_impact_radius("target")
        assert isinstance(result, Ok)
        assert sorted(result.value.impacted_files) == ["src/b.py"]
        assert result.value.impact_radius == 1

    def test_callers_falls_back_to_bfs(self, tmp_path: Path) -> None:
        db = str(tmp_path / "no_closure.db")
        _make_graph_db(db, with_closure=False)
        store = _open(db)
        graph = ImportGraph(store)

        result = graph.find_callers("target")
        assert isinstance(result, Ok)
        files = sorted(r.file_path for r in result.value)
        assert files == ["src/b.py"]


class TestClosureBfsEquivalenceOneHop:
    """On a strictly 1-hop graph, the closure path and the BFS fallback must
    return the SAME files (the task's (a)/(b) equivalence requirement)."""

    def _make_one_hop_db(self, path: str, *, with_closure: bool) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
                name TEXT NOT NULL, qualified_name TEXT, file_path TEXT NOT NULL,
                start_line INTEGER, end_line INTEGER, signature TEXT,
                return_type TEXT, is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
                source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0,
                metadata TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
            "is_exported, is_test, language) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, "Function", "shared", "src/shared.py", 1, 10, 1, 0, "python"),
                (2, "Function", "x", "src/x.py", 1, 10, 1, 0, "python"),
                (3, "Function", "y", "src/y.py", 1, 10, 1, 0, "python"),
            ],
        )
        conn.executemany(
            "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
            "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
            [
                (2, 1, "CALLS", 1, "src/x.py", "same_file", 1.0),
                (3, 1, "CALLS", 1, "src/y.py", "import", 1.0),
            ],
        )
        if with_closure:
            conn.executescript(
                "CREATE TABLE closure (source_id INTEGER, target_id INTEGER, "
                "depth INTEGER, min_confidence REAL, "
                "PRIMARY KEY(source_id, target_id, depth));"
            )
            conn.executemany(
                "INSERT INTO closure (source_id, target_id, depth, min_confidence) "
                "VALUES (?,?,?,?)",
                [(2, 1, 1, 1.0), (3, 1, 1, 1.0)],
            )
        conn.commit()
        conn.close()

    def test_same_answer_both_paths(self, tmp_path: Path) -> None:
        db_with = str(tmp_path / "one_hop_closure.db")
        db_without = str(tmp_path / "one_hop_bfs.db")
        self._make_one_hop_db(db_with, with_closure=True)
        self._make_one_hop_db(db_without, with_closure=False)

        graph_with = ImportGraph(_open(db_with))
        graph_without = ImportGraph(_open(db_without))

        impact_with = graph_with.get_impact_radius("shared")
        impact_without = graph_without.get_impact_radius("shared")
        assert isinstance(impact_with, Ok) and isinstance(impact_without, Ok)
        assert (
            sorted(impact_with.value.impacted_files)
            == sorted(impact_without.value.impacted_files)
            == ["src/x.py", "src/y.py"]
        )

        callers_with = graph_with.find_callers("shared")
        callers_without = graph_without.find_callers("shared")
        assert isinstance(callers_with, Ok) and isinstance(callers_without, Ok)
        files_with = sorted(r.file_path for r in callers_with.value)
        files_without = sorted(r.file_path for r in callers_without.value)
        assert files_with == files_without == ["src/x.py", "src/y.py"]
