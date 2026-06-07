"""Red->green for two WRONG-FACT bugs found in the amoffat__sh-744 brief (run 27105403162).

Both are correctness (plumbing/integration) defects, not logic — the intent is right, the code
read the wrong column / skipped the symmetric filter:

  1. The callee WITNESS rendered the CALL-SITE line paired with the CALLEE's file —
     "Command() in sh.py:96" where 96 is a line in the *caller* file. It must render the
     callee's DEFINITION line (nt.start_line) in the callee's file.

  2. The <gt-graph-map> "called by:" admitted name_match phantom callers (dynamic-dispatch
     names that are not real defs) identically to facts. It must be FACTS-ONLY, matching the
     witness path's resolution_method ∈ DETERMINISTIC gate.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.curation_map import build_function_map, render_map
from groundtruth.pretask.v1r_brief import _resolved_witnesses_for_file


def _schema(conn: sqlite3.Connection) -> None:
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
            confidence REAL, metadata TEXT
        );
        """
    )


def test_callee_witness_renders_callee_def_line_not_callsite(tmp_path):
    """Bug #1: the callee witness must show Command's DEFINITION line in the callee file,
    never the caller's call-site source_line."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema(conn)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            (1, "Function", "use_cmd", "caller.py", 10),
            (2, "Class", "Command", "callee.py", 1158),
        ],
    )
    # use_cmd (caller.py) CALLS Command (callee.py); the CALL happens at caller.py:96.
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,resolution_method,confidence) "
        "VALUES (1,2,'CALLS',96,'import',1.0)"
    )
    conn.commit()
    conn.close()

    ws = _resolved_witnesses_for_file(db, "caller.py", str(tmp_path))
    callee = [w for w in ws if w.get("direction") == "callee" and w.get("symbol") == "Command"]
    assert callee, f"expected a callee witness for Command, got {ws}"
    w = callee[0]
    assert w["file_path"].endswith("callee.py")
    # THE FIX: line is Command's DEF line (1158), not the caller's call-site (96).
    assert w["line"] == 1158, (
        f"witness line must be the callee DEF line 1158, got {w['line']} "
        "(call-site source_line leaked into the callee's file:line)"
    )


def test_graph_map_called_by_is_facts_only(tmp_path):
    """Bug #2: <gt-graph-map> 'called by:' must exclude name_match phantom callers. conf 0.9
    clears the visibility floor, so ONLY the deterministic-method gate removes them."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema(conn)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            (1, "Function", "bake", "sh.py", 1365),
            (2, "Function", "resolve_command", "sh.py", 624),  # real deterministic caller
            (3, "Function", "phantom", "sh.py", 50),           # name_match phantom caller
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,source_line,resolution_method,confidence) "
        "VALUES (?,?,?,?,?,?)",
        [
            (2, 1, "CALLS", 630, "import", 1.0),     # resolve_command -> bake (FACT)
            (3, 1, "CALLS", 55, "name_match", 0.9),  # phantom -> bake (name GUESS, above floor)
        ],
    )
    conn.commit()
    conn.close()

    out = render_map(build_function_map(db, [("sh.py", "bake")]))
    assert "resolve_command" in out, f"real deterministic caller must show; got:\n{out}"
    assert "phantom" not in out, f"name_match phantom caller must NOT show (facts-only); got:\n{out}"
