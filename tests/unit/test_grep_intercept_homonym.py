"""TTD — TASK #44: grep-intercept homonym (path-scope the symbol lookup).

Artifact-first reference (real beets trajectory, ev9):
  - The agent greps a symbol IN a specific file:  `grep -rn set_fields beets/importer.py`
  - `importer.py::set_fields` is the real target (calls db.py set_parse).
  - A SECOND identical `set_fields` exists in SingletonImportTask (same importer.py).
  - `zero.py` defines an UNRELATED `set_fields(self, item, tags)` (homonym, wrong arity).

Observed defect: the on-grep intercept dropped the grepped file path and resolved the
bare name repo-wide. The flat-SQL fallback did `WHERE nt.name=?` with no file scope, so
the most-called homonym (zero.py) was surfaced as authoritative for an importer.py grep.

This test builds a minimal in-memory-shaped temp graph.db (NOT derived from reading the
implementation) and asserts:
  (red)   the unscoped repo-wide query CAN return the zero.py homonym (defect reproduced)
  (green) the path-scoped lookup returns the importer.py symbol's callers and NEVER zero.py;
          and returns SILENCE (empty) when the grepped file has no such symbol.
"""
from __future__ import annotations

import os
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


def _build_homonym_db(path: str) -> None:
    """beets homonym fixture: set_fields in importer.py (x2) AND zero.py (unrelated)."""
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
    # Definitions.
    # 1: ImportTask.set_fields in importer.py  (the REAL grep target)
    # 2: SingletonImportTask.set_fields in importer.py (same-file twin)
    # 3: zero.py set_fields(self, item, tags)  (UNRELATED homonym, wrong arity)
    nodes = [
        ("Method", "set_fields", "beets/importer.py", 600,
         "set_fields(self, key, string: str)", "import"),  # id 1
        ("Method", "set_fields", "beets/importer.py", 980,
         "set_fields(self, key, string: str)", "import"),  # id 2
        ("Method", "set_fields", "beetsplug/zero.py", 120,
         "set_fields(self, item, tags)", "import"),         # id 3
        # callers
        ("Function", "_apply_choice", "beets/importer.py", 700, "", "import"),     # id 4 (same-file caller)
        ("Function", "run_import", "beets/ui/commands.py", 300, "", "import"),      # id 5 (cross-file caller of importer set_fields)
        ("Function", "_set_fields", "beetsplug/zero.py", 60, "", "import"),         # id 6 (caller of zero homonym)
        ("Function", "apply_zero", "beetsplug/zero.py", 200, "", "import"),         # id 7 (caller of zero homonym)
        ("Function", "zero_main", "beetsplug/zero.py", 230, "", "import"),          # id 8 (caller of zero homonym)
    ]
    for label, name, fp, line, sig, _rm in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, "
            "end_line, signature, return_type, is_exported, is_test, language) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,'python')",
            (label, name, name, fp, line, line + 10, sig, ""),
        )
    # Edges: give zero.py homonym (id 3) MORE callers than importer.py set_fields (id 1)
    # so a repo-wide most-called resolution would pick zero.py — exactly the defect.
    edges = [
        # importer.py set_fields (id 1): 2 callers (1 same-file, 1 cross-file)
        (4, 1, 705, "beets/importer.py", "import", 1.0),
        (5, 1, 320, "beets/ui/commands.py", "import", 1.0),
        # zero.py set_fields (id 3): 3 callers -> most-called repo-wide
        (6, 3, 65, "beetsplug/zero.py", "import", 1.0),
        (7, 3, 205, "beetsplug/zero.py", "import", 1.0),
        (8, 3, 235, "beetsplug/zero.py", "import", 1.0),
    ]
    for src, tgt, line, sfile, rm, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
            "resolution_method, confidence) VALUES (?,?,'CALLS',?,?,?,?)",
            (src, tgt, line, sfile, rm, conf),
        )
    conn.commit()
    conn.close()


def test_unscoped_query_reproduces_homonym_defect(tmp_path):
    """RED-anchor: prove the bare-name (no file scope) resolution picks the zero.py homonym.

    The real intercept resolves the bare symbol to its most-referenced definition node
    repo-wide. With zero.py's set_fields having the most callers, the unscoped resolution
    selects the WRONG file (zero.py) for an importer.py-scoped grep.
    """
    db = str(tmp_path / "graph.db")
    _build_homonym_db(db)
    conn = sqlite3.connect(db)
    # Shape of the OLD bare-name resolution: most-referenced node wins, no file scope.
    row = conn.execute(
        "SELECT n.file_path, "
        "(SELECT COUNT(*) FROM edges WHERE target_id = n.id) AS refs "
        "FROM nodes n WHERE n.name = ? AND n.is_test = 0 "
        "ORDER BY refs DESC LIMIT 1",
        ("set_fields",),
    ).fetchone()
    conn.close()
    # The defect: the most-called set_fields repo-wide is the zero.py homonym.
    assert row is not None and "zero.py" in row[0], (
        f"fixture sanity: unscoped most-called resolution must select zero.py, got {row}"
    )


def test_grep_file_scope_extracted_from_command():
    """The grepped file path must be recoverable from the grep args."""
    assert hasattr(ohgt, "_extract_grep_file_scope"), (
        "fix must add _extract_grep_file_scope to recover the grepped file path"
    )
    assert ohgt._extract_grep_file_scope("grep -rn set_fields beets/importer.py") == "beets/importer.py"
    assert ohgt._extract_grep_file_scope("rg set_fields src/beets/importer.py") == "src/beets/importer.py"
    # A bare repo-wide grep (no file arg) -> no scope.
    assert ohgt._extract_grep_file_scope("grep -rn set_fields") in (None, "")


def test_scoped_lookup_returns_importer_never_zero(tmp_path):
    """GREEN: path-scoped lookup returns importer.py callers, NEVER the zero.py homonym."""
    db = str(tmp_path / "graph.db")
    _build_homonym_db(db)
    assert hasattr(ohgt, "_grep_intercept_callers"), (
        "fix must add _grep_intercept_callers(db, symbol, file_scope, ...) path-scoped helper"
    )
    callers = ohgt._grep_intercept_callers(
        db, "set_fields", file_scope="beets/importer.py", limit=5, min_conf=0.6,
    )
    files = {c[0] for c in callers}
    # The importer.py symbol's cross-file caller must appear.
    assert any("commands.py" in f for f in files), f"expected importer caller, got {files}"
    # The zero.py homonym's callers must NEVER appear for an importer.py-scoped grep.
    assert not any("zero.py" in f for f in files), f"homonym leaked: {files}"


def test_scoped_lookup_silent_when_symbol_absent_in_grepped_file(tmp_path):
    """CORRECT-OR-QUIET: no match in the grepped file -> SILENCE, not a repo-wide homonym."""
    db = str(tmp_path / "graph.db")
    _build_homonym_db(db)
    callers = ohgt._grep_intercept_callers(
        db, "set_fields", file_scope="beets/library.py", limit=5, min_conf=0.6,
    )
    assert callers == [], (
        "symbol not defined in grepped file must yield silence, never the zero.py homonym"
    )
