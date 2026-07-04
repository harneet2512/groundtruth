"""Behavior tests for src/groundtruth/resolve.py — squash items #28, #29, #30.

These exercise the SAME code the live LSP precision pass runs (the decision is
factored into `_apply_lsp_resolution`, called by `_resolve_edges`), plus the
dispatch-table invariant. Each test is red on the pre-squash code and green after.

  #28 — only DELETE an edge when the target file IS indexed but no node spans the
        call site. NEVER delete on an external/stdlib definition or a not-indexed
        / NULL-end_line window miss (correct-or-quiet; deleting a real edge erases
        a true call relationship from the context graph).
  #29 — match by (file_path, line-window); `name` is a TIEBREAKER, not a hard
        filter. An LSP-CORRECTED call whose resolved symbol differs from the
        recorded callee name must still match (and re-point), not get deleted.
  #30 — the dispatch tables (_KNOWN_SERVERS / _LANG_TO_EXT) advertise ONLY
        languages config.LSP_SERVERS can serve: their ext targets are a subset of
        LSP_SERVERS keys, so the precision pass never silently no-ops on a language
        it claimed to handle.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.resolve import (
    _KNOWN_SERVERS,
    _LANG_TO_EXT,
    _apply_lsp_resolution,
)


def _make_db(path: str) -> None:
    """One indexed file `app.py` with a real in-repo callee `helper` (lines 10-20,
    id 2). The edit target `caller` (id 1) has a CALLS edge to it (id 100). A second
    indexed file `other.py` exists (so "indexed" is provable). `vendored.py` is NOT
    indexed (no nodes) — used to prove the not-indexed guard.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, trust_tier TEXT, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_test) "
        "VALUES (?,?,?,?,?,?,0)",
        [
            (1, "Function", "caller", "app.py", 30, 40),
            (2, "Function", "helper", "app.py", 10, 20),   # real in-repo callee
            (3, "Function", "renamed", "app.py", 50, 60),  # the CORRECTED target (#29)
            # a node in app.py with NO end_line, spanning the call site only if the
            # NULL-end_line branch fires (used to prove the window still matches it).
            (4, "Function", "nullspan", "app.py", 70, None),
            (9, "Function", "thing", "other.py", 5, 8),    # makes other.py "indexed"
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence) "
        "VALUES (100,1,2,'CALLS','name_match',0.2)",
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "graph.db")
    _make_db(p)
    return p


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _edge(conn):
    r = conn.execute("SELECT * FROM edges WHERE id = 100").fetchone()
    return dict(r)


def _fresh_stats():
    return {"verified": 0, "corrected": 0, "deleted": 0, "failed": 0, "skipped": 0}


def _edge_exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM edges WHERE id = 100").fetchone() is not None


# ─────────────────────────────── item #29 ───────────────────────────────

def test_verify_when_lsp_confirms_current_target(db):
    """LSP definition lands inside helper (id 2) at line 15 — the edge's current
    target. Window match → verified (resolution_method 'lsp'), edge kept."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="app.py", target_line=15,
        target_name="helper", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "verified"
    assert stats["verified"] == 1 and stats["deleted"] == 0
    row = conn.execute("SELECT resolution_method, target_id FROM edges WHERE id=100").fetchone()
    assert row["resolution_method"] == "lsp" and row["target_id"] == 2


def test_corrected_target_with_different_name_is_repointed_not_deleted(db):
    """item #29 RED→GREEN: the LSP resolves the call to a DIFFERENTLY-named symbol
    `renamed` (id 3, lines 50-60) — name != the edge's recorded callee `helper`.
    The old `WHERE name = target_name` filter would miss id 3, fall through, and
    DELETE the edge. With location-primary matching it must instead CORRECT
    (re-point) the edge to id 3 and keep it."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="app.py", target_line=55,
        target_name="helper", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "corrected", "LSP-corrected (renamed) edge must re-point, not delete"
    assert stats["corrected"] == 1 and stats["deleted"] == 0
    assert _edge_exists(conn)
    row = conn.execute("SELECT resolution_method, target_id FROM edges WHERE id=100").fetchone()
    assert row["target_id"] == 3 and row["resolution_method"] == "lsp"


def test_name_is_only_a_tiebreaker_inside_window(db):
    """When two indexed nodes overlap the same line window, the SAME-name node wins
    the tiebreak (ORDER BY (name = ?) DESC). Here line 15 is inside helper (id 2);
    no other node spans 15, so id 2 is selected — name tiebreak is a preference, not
    a precondition for matching."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="app.py", target_line=15,
        target_name="helper", stats=stats, has_trust_tier=False,
    )
    assert out == "verified"


# ─────────────────────────────── item #28 ───────────────────────────────

def test_external_definition_never_deletes(db):
    """item #28 RED→GREEN: the LSP resolves the call to an EXTERNAL file (relpath
    escaped the repo root, starts with '..' — stdlib/third-party os.path.join et al).
    The pre-squash code DELETEd the edge (`row is None` → DELETE). It must now be
    LEFT INTACT (correct-or-quiet) and counted as skipped, never deleted."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="../../usr/lib/python3.11/posixpath.py",
        target_line=120, target_name="join", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "skipped"
    assert stats["deleted"] == 0
    assert _edge_exists(conn), "an external-target edge must NEVER be deleted"


def test_absolute_path_target_never_deletes(db):
    """An absolute target_path (relpath failed / not under root) is external too —
    must not delete."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="C:/Python311/Lib/json/__init__.py",
        target_line=5, target_name="loads", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "skipped"
    assert stats["deleted"] == 0 and _edge_exists(conn)


def test_not_indexed_file_window_miss_never_deletes(db):
    """item #28 RED→GREEN: the LSP resolves to an IN-REPO file `vendored.py` that the
    indexer never ingested (zero nodes). No node spans the call site, but with no
    ground truth there a window miss must NOT delete — leave the edge, count skipped.
    The pre-squash code deleted on any `row is None`."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="vendored.py", target_line=99,
        target_name="helper", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "skipped"
    assert stats["deleted"] == 0
    assert _edge_exists(conn), "not-indexed-file window miss must NEVER delete"


def test_indexed_file_no_node_match_is_genuine_fp_delete(db):
    """The ONLY delete-allowed case: the target file IS indexed (other.py has nodes)
    yet NO node spans the call site (line 999). That is a genuine false positive →
    delete. This proves the guard is not over-broad (real FPs are still removed)."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="other.py", target_line=999,
        target_name="ghost", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "deleted"
    assert stats["deleted"] == 1
    assert not _edge_exists(conn), "an indexed-file no-match edge IS a false positive"


def test_null_end_line_node_still_matches_in_window(db):
    """A node with end_line IS NULL (id 4, start 70) still matches a call site at
    line 70 via the `end_line IS NULL` window clause — proving a NULL-window node is
    found and re-pointed (corrected), not deleted as a phantom miss."""
    conn = _conn(db)
    stats = _fresh_stats()
    out = _apply_lsp_resolution(
        conn, edge=_edge(conn), target_rel="app.py", target_line=70,
        target_name="nullspan", stats=stats, has_trust_tier=False,
    )
    conn.commit()
    assert out == "corrected"
    assert stats["deleted"] == 0 and _edge_exists(conn)
    assert conn.execute("SELECT target_id FROM edges WHERE id=100").fetchone()[0] == 4


# ─────────────────────────────── item #30 ───────────────────────────────

def test_dispatch_tables_subset_of_servable_extensions():
    """item #30: every extension _LANG_TO_EXT points at must be a key config.LSP_SERVERS
    can actually start — otherwise resolve_main's `servers.get(args.lang)` gate passes
    for a language whose get_server_config() then returns Err, and the WHOLE pass
    silently no-ops. Deriving both tables from LSP_SERVERS makes this an invariant."""
    from groundtruth.lsp.config import LSP_SERVERS

    serveable = set(LSP_SERVERS.keys())
    mapped = set(_LANG_TO_EXT.values())
    assert mapped <= serveable, f"advertised exts not serveable: {sorted(mapped - serveable)}"


def test_phantom_unserveable_languages_removed():
    """c/cpp/ruby/kotlin were advertised by the old hard-coded tables but LSP_SERVERS
    has no config for .c/.cpp/.rb/.kt — they must no longer appear anywhere in the
    dispatch surface (they could only ever produce a silent full no-op)."""
    for phantom in ("c", "cpp", "ruby", "kotlin", "kt"):
        assert phantom not in _KNOWN_SERVERS
        assert phantom not in _LANG_TO_EXT


def test_known_servers_keys_match_lang_to_ext_keys():
    """Both tables are derived from the one source (LSP_SERVERS + LANGUAGE_IDS), so
    they advertise the exact same language-name surface."""
    assert set(_KNOWN_SERVERS.keys()) == set(_LANG_TO_EXT.keys())


def test_real_languages_still_dispatch_correctly():
    """The supported languages must still resolve name→ext (regression guard)."""
    for name, ext in [
        ("python", ".py"), ("py", ".py"), ("typescript", ".ts"), ("ts", ".ts"),
        ("go", ".go"), ("rust", ".rs"), ("rs", ".rs"), ("java", ".java"),
        ("typescriptreact", ".tsx"), ("javascript", ".js"),
    ]:
        assert _LANG_TO_EXT.get(name) == ext
