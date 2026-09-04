"""Regression: the depth increase must NOT leak into RANK (BRIEFING §4.2 — reach
over-promotes hubs; the architecture subordinates it on purpose).

Found by the per-layer LIPI (wx2eywcer): untyped edge JOINs in v1r_brief began
counting promoted READS/WRITES/PRECEDES/DATA_FLOW edges into function ranking and the
hub p80 once the promote pass landed. Fixed by adding type='CALLS'. These tests build a
graph where a function is reached ONLY by promoted edges and assert it does not outrank
a CALLS-reached function.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.v1r_brief import _top_functions, _hub_degree_fn


def _graph(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER, "
        "language TEXT, parent_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT)"
    )
    nodes = [
        (
            1,
            "Function",
            "funcCALLS",
            "",
            "a.py",
            1,
            5,
            "def funcCALLS():",
            None,
            0,
            0,
            "python",
            None,
        ),
        (
            2,
            "Function",
            "funcPROMOTED",
            "",
            "a.py",
            6,
            10,
            "def funcPROMOTED():",
            None,
            0,
            0,
            "python",
            None,
        ),
    ]
    for i in range(3, 6):  # 3 CALLS-callers -> funcCALLS
        nodes.append(
            (i, "Function", f"c{i}", "", "b.py", 1, 2, f"def c{i}():", None, 0, 0, "python", None)
        )
    for i in range(6, 16):  # 10 PRECEDES-sources -> funcPROMOTED
        nodes.append(
            (i, "Function", f"p{i}", "", "b.py", 1, 2, f"def p{i}():", None, 0, 0, "python", None)
        )
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", nodes)
    edges = [(i, 1, "CALLS", "import", 1.0) for i in range(3, 6)]
    edges += [(i, 2, "PRECEDES", "promote_precedes", 0.5) for i in range(6, 16)]
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (?,?,?,?,?)",
        edges,
    )
    conn.commit()
    conn.close()
    return db


def test_top_functions_rank_ignores_promoted_edges(tmp_path):
    db = _graph(tmp_path)
    out = _top_functions(db, "a.py")
    # funcCALLS (3 CALLS) must rank ABOVE funcPROMOTED (0 CALLS, 10 PRECEDES). With the
    # untyped JOIN, the 10 PRECEDES would have made funcPROMOTED rank first.
    assert "def funcCALLS():" in out and "def funcPROMOTED():" in out
    assert out.index("def funcCALLS():") < out.index("def funcPROMOTED():")


def test_hub_degree_counts_calls_only(tmp_path):
    db = _graph(tmp_path)
    p80, degree_of = _hub_degree_fn(db)
    # b.py has 0 incoming CALLS (it only holds callers); a.py has 3 incoming CALLS.
    # The promoted PRECEDES edges (10, into a.py) must NOT inflate a.py's hub degree.
    assert degree_of("a.py") == 3, degree_of("a.py")
