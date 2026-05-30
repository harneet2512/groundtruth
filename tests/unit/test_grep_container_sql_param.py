"""TTD — TASK SQLPARAM (#44 follow-up): parameterize the container-fallback grep SQL.

Reviewer concern: the container-fallback grep path (oh_gt_full_wrapper.py ~4051-4099)
builds its file_path LIKE scope predicate and the symbol predicate via f-string
interpolation (``_scope_pred = f"AND nt.file_path LIKE '{_scope_like}' ..."`` and
``WHERE nt.name = '{_grep_sym_esc}'``). CLAUDE.md mandates *parameterized* SQLite for
every query. ``_container_query`` already accepts a ``params_json`` argument and runs
``c.execute(sql, params)`` inside the container — so this path CAN bind parameters.

Artifact-first reference: a repo file path can legitimately contain a single quote
(e.g. ``a'b/c.py``). Under the old hand-escaping, a single quote in the scope must be
doubled or the query breaks / is injectable. The robust contract is: the scope and
symbol values flow as BOUND PARAMETERS, never interpolated into the SQL string.

  (red)   a naive f-string build with a single-quote path produces SQL that, executed
          against a real sqlite, raises (broken) — proving raw interpolation is unsafe.
  (green) the builder returns SQL whose symbol/scope predicates are ``?`` placeholders
          (no interpolated path value), and executing it with the bound params against
          a real sqlite returns the correct cross-file caller and never injects.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "swebench"))
sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={}, success_callback=[],
        completion=lambda *a, **k: None, acompletion=None,
        completion_cost=lambda *a, **k: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt  # noqa: E402

_QUOTE_PATH = "pkg/a'b/c.py"  # a legitimate repo path containing a single quote


def _build_quote_db(path: str) -> None:
    """Fixture: a callee defined in a single-quote-bearing file, with one cross-file
    caller and one same-file caller; plus an unrelated homonym to prove scoping."""
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
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    nodes = [
        ("Function", "do_thing", _QUOTE_PATH),                # id 1  (real target, quote path)
        ("Function", "do_thing", "other/unrelated.py"),       # id 2  (homonym, other file)
        ("Function", "cross_caller", "pkg/caller.py"),        # id 3  (cross-file caller of id 1)
        ("Function", "same_caller", _QUOTE_PATH),             # id 4  (same-file caller of id 1)
        ("Function", "homonym_caller", "other/x.py"),         # id 5  (caller of id 2)
    ]
    for label, name, fp in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, "
            "end_line, signature, return_type, is_exported, is_test, language) "
            "VALUES (?,?,?,?,1,10,'','',0,0,'python')",
            (label, name, name, fp),
        )
    edges = [
        (3, 1, 5, "pkg/caller.py", "import", 1.0),    # cross-file caller of the quote-path target
        (4, 1, 7, _QUOTE_PATH, "same_file", 1.0),     # same-file caller (must be excluded)
        (5, 2, 9, "other/x.py", "import", 1.0),       # caller of the homonym (must NOT surface)
    ]
    for src, tgt, line, sfile, rm, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
            "resolution_method, confidence) VALUES (?,?,'CALLS',?,?,?,?)",
            (src, tgt, line, sfile, rm, conf),
        )
    conn.commit()
    conn.close()


def test_raw_interpolation_breaks_on_quote_path():
    """RED-anchor (pure-sqlite): a naive f-string LIKE predicate with a raw single-quote
    path produces a SQL syntax error — proving interpolation is unsafe."""
    db = str(Path(__import__("tempfile").mkdtemp()) / "g.db")
    _build_quote_db(db)
    conn = sqlite3.connect(db)
    # Mimic the OLD code but WITHOUT the .replace("'", "''") doubling, i.e. the failure
    # mode interpolation invites: an unescaped quote terminates the string literal.
    scope_like = "%" + _QUOTE_PATH  # contains a single quote
    bad_sql = (
        "SELECT nsrc.file_path FROM edges e "
        "JOIN nodes nt ON e.target_id = nt.id "
        "JOIN nodes nsrc ON e.source_id = nsrc.id "
        f"WHERE nt.file_path LIKE '{scope_like}' AND e.type = 'CALLS'"
    )
    raised = False
    try:
        conn.execute(bad_sql).fetchall()
    except sqlite3.OperationalError:
        raised = True
    finally:
        conn.close()
    assert raised, "raw interpolation of a single-quote path must break (proves the risk)"


def test_builder_exists():
    assert hasattr(ohgt, "_build_grep_intercept_query"), (
        "fix must add _build_grep_intercept_query(symbol, file_scope, min_conf, limit) "
        "returning (sql, params) with no interpolated symbol/scope values"
    )


def test_builder_uses_placeholders_no_interpolated_path():
    """GREEN: the symbol and scope values must NOT appear interpolated in the SQL — they
    flow as bound parameters (``?``)."""
    sql, params = ohgt._build_grep_intercept_query(
        "do_thing", file_scope=_QUOTE_PATH, min_conf=0.6, limit=5,
    )
    # The raw path value must never be baked into the SQL string.
    assert _QUOTE_PATH not in sql, f"scope value interpolated into SQL: {sql!r}"
    assert "do_thing" not in sql, f"symbol value interpolated into SQL: {sql!r}"
    # Symbol + scope + min_conf + limit -> 4 bound params (LIKE scope present).
    assert sql.count("?") == len(params) == 4, (
        f"expected 4 placeholders bound to 4 params, got {sql.count('?')} / {len(params)}"
    )
    # The path param carries the literal single quote, un-mangled.
    assert any(_QUOTE_PATH in str(p) for p in params), f"scope param missing/mangled: {params}"


def test_builder_query_executes_correctly_with_quote_path():
    """GREEN end-to-end: bound params execute against a real sqlite, return the cross-file
    caller of the quote-path target, exclude the same-file caller, and never surface the
    unrelated homonym — and a single quote never injects."""
    db = str(Path(__import__("tempfile").mkdtemp()) / "g.db")
    _build_quote_db(db)
    sql, params = ohgt._build_grep_intercept_query(
        "do_thing", file_scope=_QUOTE_PATH, min_conf=0.6, limit=5,
    )
    conn = sqlite3.connect(db)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    files = {r[0] for r in rows}
    assert "pkg/caller.py" in files, f"cross-file caller of quote-path target missing: {files}"
    assert _QUOTE_PATH not in files, f"same-file caller must be excluded: {files}"
    assert "other/x.py" not in files, f"unrelated homonym caller leaked: {files}"


def test_builder_unscoped_has_three_params():
    """GREEN: a repo-wide (no file_scope) build omits the scope predicate -> 3 params."""
    sql, params = ohgt._build_grep_intercept_query(
        "do_thing", file_scope=None, min_conf=0.6, limit=5,
    )
    assert "do_thing" not in sql and "file_path LIKE" not in sql
    assert sql.count("?") == len(params) == 3, (
        f"unscoped build must have 3 bound params, got {sql.count('?')} / {len(params)}"
    )
