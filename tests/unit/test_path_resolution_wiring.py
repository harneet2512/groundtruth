"""C2 — canonicalize path resolution onto the universal path_resolver.

DOC_OF_HONOR §1.1: ONE universal resolver, no per-query reinvention. These tests
pin the *behavioral contract* that all consumers must obey after delegating to
``path_resolver.resolve_to_stored_path``:

  1. A basename query must NOT be answered by an unanchored ``LIKE '%name'`` that
     matches a different file whose basename merely *ends with* the query
     (``foo.py`` must never resolve to ``pkg/barfoo.py``).
  2. When a basename is ambiguous (>1 stored path), resolution returns ``None``
     (correct-or-quiet) — never an arbitrary planner-first row.
  3. A basename that uniquely identifies one stored path resolves to it.
  4. A path that matches nothing returns ``None`` — never the (possibly
     path-shaped) input string.

Red-before-green: assertions (1) and (2) FAIL against the current unanchored
``WHERE file_path LIKE ? LIMIT 1`` (returns ``pkg/barfoo.py`` or an arbitrary
ambiguous row) and against the inline hook resolvers that ``return norm`` on a
total no-match. They pass only after delegation to the boundary-anchored,
uniqueness-gated universal resolver.

Run from the worktree root with the worktree src first on the path, e.g.:
    PYTHONPATH="$(pwd)/src" python -m pytest tests/unit/test_path_resolution_wiring.py -q
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from groundtruth.index.graph_store import GraphStore


# --- Schema columns the bridge expects (subset of the Go indexer schema) ---
_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT 'Function',
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported BOOLEAN DEFAULT 1,
    is_test BOOLEAN DEFAULT 0,
    language TEXT NOT NULL DEFAULT 'python',
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'CALLS',
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT
);
"""


@pytest.fixture()
def trap_db():
    """graph.db with a basename-suffix trap and an ambiguous + a unique basename.

    Stored file paths:
      - src/foo.py        (the file a 'foo.py' query *should* resolve to,
                           but only when it is unambiguous)
      - pkg/barfoo.py     (suffix trap: an unanchored LIKE '%foo.py' WRONGLY
                           matches this because 'barfoo.py' ends with 'foo.py')
      - src/uniq.py       (unique basename -> must resolve)
      - app/dup.py        (ambiguous basename 'dup.py' #1)
      - lib/dup.py        (ambiguous basename 'dup.py' #2)
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (name, file_path, start_line, end_line) VALUES (?, ?, ?, ?)",
        [
            ("foo_fn", "src/foo.py", 1, 5),
            ("barfoo_fn", "pkg/barfoo.py", 1, 5),
            ("uniq_fn", "src/uniq.py", 1, 5),
            ("dup_a", "app/dup.py", 1, 5),
            ("dup_b", "lib/dup.py", 1, 5),
        ],
    )
    conn.commit()
    conn.close()
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# graph_store._match_file_path
# ---------------------------------------------------------------------------

def test_match_file_path_does_not_return_suffix_trap(trap_db):
    """'foo.py' must NEVER resolve to 'pkg/barfoo.py'.

    RED on current code: exact match on 'foo.py' misses, then the unanchored
    LIKE '%foo.py' matches BOTH src/foo.py and pkg/barfoo.py and LIMIT 1 returns
    an arbitrary one — frequently the trap file.
    """
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    matched = store._match_file_path("foo.py")
    assert matched != "pkg/barfoo.py", (
        "unanchored LIKE matched the suffix-trap file 'pkg/barfoo.py'"
    )


def test_match_file_path_ambiguous_basename_returns_none(trap_db):
    """An ambiguous basename ('foo.py' matches >1 boundary-anchored path?...)

    Here 'foo.py' is boundary-unique to src/foo.py, but the suffix trap means a
    NAIVE '%foo.py' is ambiguous. The contract: when the *boundary-anchored*
    candidate set is not exactly one, resolution must be None (correct-or-quiet),
    never an arbitrary row. We use the genuinely-ambiguous 'dup.py' to pin this.
    """
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    matched = store._match_file_path("dup.py")
    assert matched is None, (
        f"ambiguous basename 'dup.py' must resolve to None, got {matched!r}"
    )


def test_match_file_path_unique_basename_resolves(trap_db):
    """A unique basename resolves to its single stored path."""
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    assert store._match_file_path("uniq.py") == "src/uniq.py"


def test_match_file_path_exact_match_still_works(trap_db):
    """An exact stored path still resolves to itself (no regression)."""
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    assert store._match_file_path("src/foo.py") == "src/foo.py"


def test_match_file_path_total_no_match_returns_none(trap_db):
    """A path with no stored match returns None, NOT the input string.

    RED on current code: line ~403 `return file_path` echoes the input back.
    """
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    matched = store._match_file_path("nonexistent/totally_absent.py")
    assert matched is None, (
        f"no-match must return None, got the echoed input {matched!r}"
    )


def test_match_file_path_boundary_unique_basename_resolves(trap_db):
    """'foo.py' IS boundary-unique (only src/foo.py ends in '/foo.py').

    After the boundary anchor, the suffix trap 'pkg/barfoo.py' is excluded, so
    'foo.py' resolves cleanly to 'src/foo.py'. This proves the anchor does not
    over-suppress legitimate unique matches.
    """
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    assert store._match_file_path("foo.py") == "src/foo.py"


# ---------------------------------------------------------------------------
# graph_store.get_symbols_in_file — routed through resolution
# ---------------------------------------------------------------------------

def test_get_symbols_in_file_resolves_basename(trap_db):
    """get_symbols_in_file must find symbols for a unique basename via resolution.

    RED on current code: bare `WHERE file_path = 'uniq.py'` finds nothing because
    the stored path is 'src/uniq.py'.
    """
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    res = store.get_symbols_in_file("uniq.py")
    assert res.is_ok()
    names = {s.name for s in res.value}
    assert "uniq_fn" in names, f"expected uniq_fn via basename resolution, got {names}"


def test_get_symbols_in_file_no_match_is_empty(trap_db):
    """get_symbols_in_file on a totally-absent path returns Ok([]) (quiet)."""
    store = GraphStore(trap_db)
    assert store.initialize().is_ok()
    res = store.get_symbols_in_file("nonexistent/gone.py")
    assert res.is_ok()
    assert res.value == []


# ---------------------------------------------------------------------------
# Hook inline resolvers (post_edit / post_view) — must return None on no-match
# ---------------------------------------------------------------------------

def test_post_edit_resolver_returns_none_on_no_match(trap_db):
    """post_edit._resolve_file_path must return None on a total no-match.

    RED on current code: the inline resolver ends with `return norm`, echoing a
    path-shaped string instead of None.
    """
    from groundtruth.hooks import post_edit

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_edit._resolve_file_path(conn, "nonexistent/absent.py")
    finally:
        conn.close()
    assert result is None, f"expected None on no-match, got {result!r}"


def test_post_edit_resolver_does_not_return_suffix_trap(trap_db):
    """post_edit resolver must not answer 'foo.py' with the suffix trap."""
    from groundtruth.hooks import post_edit

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_edit._resolve_file_path(conn, "foo.py")
    finally:
        conn.close()
    assert result != "pkg/barfoo.py"
    # boundary-unique -> resolves to the real file
    assert result == "src/foo.py"


def test_post_edit_resolver_ambiguous_returns_none(trap_db):
    """post_edit resolver returns None for an ambiguous basename."""
    from groundtruth.hooks import post_edit

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_edit._resolve_file_path(conn, "dup.py")
    finally:
        conn.close()
    assert result is None, f"ambiguous basename must be None, got {result!r}"


def test_post_view_resolver_returns_none_on_no_match(trap_db):
    """post_view._resolve_file_path must return None on a total no-match."""
    from groundtruth.hooks import post_view

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_view._resolve_file_path(conn, "nonexistent/absent.py")
    finally:
        conn.close()
    assert result is None, f"expected None on no-match, got {result!r}"


def test_post_view_resolver_does_not_return_suffix_trap(trap_db):
    """post_view resolver must not answer 'foo.py' with the suffix trap."""
    from groundtruth.hooks import post_view

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_view._resolve_file_path(conn, "foo.py")
    finally:
        conn.close()
    assert result != "pkg/barfoo.py"
    assert result == "src/foo.py"


def test_post_edit_resolver_exact_match_preserved(trap_db):
    """Negative control: an exact stored path round-trips unchanged."""
    from groundtruth.hooks import post_edit

    conn = sqlite3.connect(trap_db)
    conn.row_factory = sqlite3.Row
    try:
        result = post_edit._resolve_file_path(conn, "src/foo.py")
    finally:
        conn.close()
    assert result == "src/foo.py"
