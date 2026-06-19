"""C5 — closure staleness after an incremental reindex (Option B).

Decision (locked): Option B — *staleness-aware reader* + *drop-on-incremental*.
The Go incremental path (``gt-index -file <relpath>``) DROPS the reparsed
file's closure rows but never recomputes the closure (recompute would
reintroduce the 29x BFS cost C7 deliberately avoided). The closure is
therefore a *full-index-only* sidecar: after any incremental reindex it is, by
construction, partial/stale.

This test pins the *reader* contract: ``ImportGraph._closure_sources_for_symbol``
must treat a present-but-stale closure table as "no closure available" and
return ``None`` (the signal that forces the caller back to the live BFS), and
must trust the closure ONLY when it is provably fresh.

Staleness signals the reader uses (all deterministic, no wall-clock heuristics
that could false-positive a healthy full-index DB):

  1. **No build marker** — ``project_meta.closure_count`` missing/unparseable
     ⇒ ``None``. (We never trust-when-unknown.)
  2. **Count mismatch (the Option-B drop signal)** — the writer records
     ``closure_count = N`` at full-index time (main.go SetMeta). The
     incremental DROP removes the reparsed file's rows, so live
     ``COUNT(*) FROM closure`` < N. Any mismatch ⇒ stale ⇒ ``None``.
  3. **Newer indexed file** — the freshest ``file_hashes.indexed_at`` is newer
     than the closure build (proxied by the count signal for Option-B drops,
     and directly asserted here via a strictly-newer file hash) ⇒ ``None``.

These tests build synthetic Go-indexer-schema graph.db files directly so they
exercise the real ``GraphStore`` bridge that production uses. The Go side is
CI-verified separately (no go/gcc in this env).

Red-before-green: with the pre-fix reader (no staleness check) the *stale*
cases below return a non-None source set (the bug). The fix makes them return
``None``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Ok

# Graph used throughout (CALLS edges, all VERIFIED — import, conf 1.0):
#
#     a()  ->  b()  ->  target()
#
#   node 1 = target  in src/target.py
#   node 2 = b       in src/b.py   (direct caller of target)
#   node 3 = a       in src/a.py   (transitive caller of target via b)
#
# Full-index closure (who transitively REACHES each node):
#   target(1) reached by b(2) @depth1 and a(3) @depth2; b(2) reached by a(3).
_FULL_CLOSURE_ROWS = [
    (2, 1, 1, 1.0),  # b -> target (1 hop)
    (3, 2, 1, 1.0),  # a -> b      (1 hop)
    (3, 1, 2, 1.0),  # a -> target (2 hops)
]


def _make_db(
    path: str,
    *,
    closure_rows: list[tuple[int, int, int, float]] | None,
    closure_count_marker: int | None,
    file_hashes: list[tuple[str, str]],
    build_time_utc: str | None = None,
) -> None:
    """Build a synthetic Go-indexer graph.db.

    Args:
        closure_rows: rows to insert into ``closure`` (None ⇒ no closure table).
        closure_count_marker: value to write to ``project_meta.closure_count``
            (None ⇒ do not write the marker at all).
        file_hashes: (file_path, indexed_at) rows for ``file_hashes``.
        build_time_utc: value to write to ``project_meta.build_time_utc``
            (None ⇒ do not write it; mirrors a binary built without ldflags
            stamping, where the timestamp signal is intentionally inert).
    """
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
            source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE file_hashes (
            file_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
            language TEXT, indexed_at TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_exported, is_test, language) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Function", "target", "src/target.py", 1, 10, 1, 0, "python"),
            (2, "Function", "b", "src/b.py", 1, 10, 1, 0, "python"),
            (3, "Function", "a", "src/a.py", 1, 10, 1, 0, "python"),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        [
            (2, 1, "CALLS", 5, "src/b.py", "import", 1.0),  # b -> target
            (3, 2, "CALLS", 5, "src/a.py", "import", 1.0),  # a -> b
        ],
    )
    if closure_rows is not None:
        conn.executescript(
            """
            CREATE TABLE closure (
                source_id INTEGER, target_id INTEGER, depth INTEGER,
                min_confidence REAL, PRIMARY KEY(source_id, target_id, depth)
            );
            CREATE INDEX idx_closure_source ON closure(source_id);
            CREATE INDEX idx_closure_target ON closure(target_id);
            """
        )
        if closure_rows:
            conn.executemany(
                "INSERT INTO closure (source_id, target_id, depth, min_confidence) "
                "VALUES (?,?,?,?)",
                closure_rows,
            )
    if closure_count_marker is not None:
        conn.execute(
            "INSERT INTO project_meta (key, value) VALUES ('closure_count', ?)",
            (str(closure_count_marker),),
        )
    if build_time_utc is not None:
        conn.execute(
            "INSERT INTO project_meta (key, value) VALUES ('build_time_utc', ?)",
            (build_time_utc,),
        )
    conn.executemany(
        "INSERT INTO file_hashes (file_path, content_hash, language, indexed_at) "
        "VALUES (?,?,?,?)",
        [(fp, "deadbeef", "python", ts) for fp, ts in file_hashes],
    )
    conn.commit()
    conn.close()


def _open(path: str) -> GraphStore:
    store = GraphStore(db_path=path)
    res = store.initialize()
    assert isinstance(res, Ok), res
    return store


# Full-index file hashes: all files indexed at the SAME (full-index) time, and
# none is newer than the closure build.
_FRESH_HASHES = [
    ("src/target.py", "2026-05-29T00:00:00Z"),
    ("src/b.py", "2026-05-29T00:00:00Z"),
    ("src/a.py", "2026-05-29T00:00:00Z"),
]


class TestClosureFreshIsTrusted:
    """A provably-fresh full-index closure is used (returns the source set)."""

    def test_fresh_closure_returns_sources(self, tmp_path: Path) -> None:
        db = str(tmp_path / "fresh.db")
        _make_db(
            db,
            closure_rows=_FULL_CLOSURE_ROWS,
            closure_count_marker=len(_FULL_CLOSURE_ROWS),  # marker == live count
            file_hashes=_FRESH_HASHES,
            # Stamped build, newer than every file_hash ⇒ timestamp signal also
            # confirms freshness (proves a fully-stamped fresh DB is trusted).
            build_time_utc="2026-05-29T06:00:00Z",
        )
        graph = ImportGraph(_open(db))
        # target() (node 1) is transitively reached by b(2) and a(3).
        sources = graph._closure_sources_for_symbol(1)
        assert sources is not None, "fresh closure must be trusted (non-None)"
        assert sources == {2, 3}

    def test_fresh_closure_without_build_marker_still_trusted(
        self, tmp_path: Path
    ) -> None:
        """A binary built without ldflags stamping has no build_time_utc; the
        timestamp signal is inert but signals 1+2 still positively prove
        freshness, so the closure is trusted. (Guards against an over-eager
        stale verdict on the common unstamped-binary case.)"""
        db = str(tmp_path / "fresh_no_buildts.db")
        _make_db(
            db,
            closure_rows=_FULL_CLOSURE_ROWS,
            closure_count_marker=len(_FULL_CLOSURE_ROWS),
            file_hashes=_FRESH_HASHES,
            build_time_utc=None,
        )
        graph = ImportGraph(_open(db))
        assert graph._closure_sources_for_symbol(1) == {2, 3}


class TestMarkerlessClosureIsTrusted:
    """An *absent* closure_count marker is NOT, by itself, staleness evidence.

    The real indexer always writes the marker (main.go SetMeta), so a markerless
    closure can only come from a hand-built / pre-marker database — never from
    the Option-B incremental drop (which leaves the marker in place while
    shrinking the table, and is therefore caught by the count-mismatch signal).
    Rejecting a provably-unmodified markerless closure would suppress correct
    context on a fresh DB for zero staleness benefit — the "confident
    suppression on a fresh DB" inversion. So we trust it.

    This also pins backward-compatibility with the pre-C5 closure fixtures
    (tests/unit/test_graph_closure.py), which build closure DBs without a
    project_meta marker and rely on the fast path being used.
    """

    def test_markerless_closure_is_used(self, tmp_path: Path) -> None:
        db = str(tmp_path / "markerless.db")
        _make_db(
            db,
            closure_rows=_FULL_CLOSURE_ROWS,
            closure_count_marker=None,  # no marker at all
            file_hashes=_FRESH_HASHES,
        )
        graph = ImportGraph(_open(db))
        assert graph._closure_sources_for_symbol(1) == {2, 3}


class TestClosureStaleReturnsNone:
    """Stale closures force the BFS fallback (return None)."""

    def test_count_mismatch_after_incremental_drop(self, tmp_path: Path) -> None:
        """Option-B drop: the incremental reindex deleted target.py's closure
        rows, so live COUNT(*) (here 1) < recorded closure_count (3). The
        marker is now inconsistent with the table ⇒ stale ⇒ None."""
        db = str(tmp_path / "stale_count.db")
        _make_db(
            db,
            # post-drop: only the a->b row survives (b/target rows removed).
            closure_rows=[(3, 2, 1, 1.0)],
            closure_count_marker=len(_FULL_CLOSURE_ROWS),  # stale marker = 3
            file_hashes=_FRESH_HASHES,
        )
        graph = ImportGraph(_open(db))
        assert graph._closure_sources_for_symbol(1) is None

    def test_corrupt_marker_returns_none(self, tmp_path: Path) -> None:
        """A present-but-unparseable closure_count marker ⇒ provenance is
        untrustworthy ⇒ stale ⇒ None. (Distinct from an *absent* marker, which
        is NOT by itself staleness evidence — see
        ``TestMarkerlessClosureIsTrusted`` — because the real indexer always
        writes the marker, so absence implies a hand-built/legacy DB, never the
        Option-B drop.)"""
        db = str(tmp_path / "corrupt_marker.db")
        _make_db(
            db,
            closure_rows=_FULL_CLOSURE_ROWS,
            closure_count_marker=None,  # injected as a bad value below
            file_hashes=_FRESH_HASHES,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO project_meta (key, value) VALUES ('closure_count', 'NaN')"
        )
        conn.commit()
        conn.close()
        graph = ImportGraph(_open(db))
        assert graph._closure_sources_for_symbol(1) is None

    def test_file_indexed_after_closure_build_returns_none(
        self, tmp_path: Path
    ) -> None:
        """A file hashed strictly AFTER the closure build (an incremental
        reindex bumps that file's indexed_at) ⇒ the closure no longer reflects
        that file ⇒ stale ⇒ None. Here the count marker still matches the live
        rows, so this isolates the timestamp signal."""
        db = str(tmp_path / "stale_ts.db")
        _make_db(
            db,
            closure_rows=_FULL_CLOSURE_ROWS,
            closure_count_marker=len(_FULL_CLOSURE_ROWS),
            # Count marker matches live rows, so signal 2 is satisfied — this
            # isolates the timestamp signal (signal 3).
            build_time_utc="2026-05-29T06:00:00Z",  # closure built at 06:00
            file_hashes=[
                ("src/target.py", "2026-05-29T00:00:00Z"),
                ("src/b.py", "2026-05-29T00:00:00Z"),
                # a.py reindexed at 12:00 — AFTER the 06:00 closure build:
                ("src/a.py", "2026-05-29T12:00:00Z"),
            ],
        )
        graph = ImportGraph(_open(db))
        assert graph._closure_sources_for_symbol(1) is None


class TestStaleClosureFallsBackToBfsEndToEnd:
    """Negative control: a stale closure must not silently drop transitive
    callers — the public find_callers must fall back to the live 1-hop BFS and
    still return the DIRECT caller (b.py), never raise, never return empty."""

    def test_find_callers_falls_back_on_stale(self, tmp_path: Path) -> None:
        db = str(tmp_path / "stale_e2e.db")
        _make_db(
            db,
            closure_rows=[(3, 2, 1, 1.0)],  # dropped target rows
            closure_count_marker=len(_FULL_CLOSURE_ROWS),  # stale marker
            file_hashes=_FRESH_HASHES,
        )
        graph = ImportGraph(_open(db))
        result = graph.find_callers("target")
        assert isinstance(result, Ok)
        files = sorted(r.file_path for r in result.value)
        # BFS fallback: direct caller b.py (transitive a.py is the closure-only
        # answer and is correctly NOT claimed once we distrust the stale table).
        assert files == ["src/b.py"]
