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
from groundtruth.pretask.v1r_brief import _edit_target_guard, _resolved_witnesses_for_file


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


def _schema_legacy_no_method(conn: sqlite3.Connection) -> None:
    """Legacy edges schema with NO ``resolution_method`` column.

    On this schema ``_has_columns`` returns ``has_method=False``; the curation map
    cannot read provenance, so EVERY edge is unverified by construction and must be
    rendered ``(unverified)`` even when its confidence clears the floor.
    """
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
            source_line INTEGER, source_file TEXT, confidence REAL, metadata TEXT
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


def test_fmt_edge_marks_non_fact_unverified_and_leaves_fact_bare():
    """L8 (unit, _fmt_edge directly): the rendering honesty marker.

    A FACT edge (deterministic resolution_method) renders BARE; a non-fact edge (here a
    name_match that cleared the floor) renders with the ``(unverified)`` honesty marker the
    module docstring promises. This pins _fmt_edge's contract independently of the SQL admission
    path (which legitimately suppresses name_match when provenance IS known — see the legacy test
    for the reachable end-to-end path where provenance is UNKNOWN)."""
    from groundtruth.pretask.curation_map import Edge, _fmt_edge

    fact = Edge(name="resolve_command", file="sh.py", confidence=1.0, resolution_method="import")
    guess = Edge(name="phantom", file="sh.py", confidence=0.9, resolution_method="name_match")
    assert _fmt_edge(fact) == "resolve_command (sh.py)", "a deterministic fact must render bare"
    # THE FIX: the non-fact guess carries the honesty marker, never bare like a fact.
    assert _fmt_edge(guess) == "phantom (sh.py) (unverified)", (
        "a non-fact edge must render (unverified), not indistinguishably from a fact"
    )
    # A 2-hop edge is verified-only (always a fact) -> (2-hop), no (unverified).
    hop2 = Edge(
        name="deep", file="x.py", confidence=1.0, resolution_method="type_flow", hops=2
    )
    assert _fmt_edge(hop2) == "deep (x.py) (2-hop)", "a verified 2-hop fact: (2-hop), no (unverified)"


def test_legacy_db_without_resolution_method_marks_edges_unverified(tmp_path):
    """L8 (Half B): on a legacy edges schema with NO ``resolution_method`` column, provenance
    is unknown -> ``has_method=False`` -> every floor-clearing edge is is_fact=False and MUST
    render ``(unverified)``. Without the marker a bare name (unknown provenance) would read as a
    structurally-resolved fact."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema_legacy_no_method(conn)  # edges table has confidence but NO resolution_method
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            (1, "Function", "focus_fn", "mod.py", 20),
            (2, "Function", "legacy_caller", "mod.py", 5),  # provenance unknown on legacy schema
        ],
    )
    # legacy_caller -> focus_fn, confidence 0.95 clears the floor; no resolution_method column.
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,confidence) "
        "VALUES (2,1,'CALLS',8,0.95)"
    )
    conn.commit()
    conn.close()

    out = render_map(build_function_map(db, [("mod.py", "focus_fn")]))
    assert "legacy_caller" in out, f"floor-clearing legacy caller should be visible; got:\n{out}"
    # THE FIX: unknown-provenance (no method column) -> honestly tagged (unverified), never bare.
    assert "legacy_caller (mod.py) (unverified)" in out, (
        "on a legacy DB with no resolution_method column the edge has unknown provenance and "
        f"must render (unverified), not bare; got:\n{out}"
    )


def _schema_with_properties(conn: sqlite3.Connection) -> None:
    """nodes + the ``properties`` table _edit_target_guard reads its guard/return text from."""
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT,
            line INTEGER, confidence REAL
        );
        """
    )


def test_edit_target_guard_binds_to_candidate_file_not_same_named_collision(tmp_path):
    """Bug L4: two files share a basename (a/db.py and b/db.py), each defining the SAME
    function name at a DIFFERENT line. The HIGH-tier "Edit target: a/db.py :: connect" header
    must be backed by a/db.py's guard line — never b/db.py's. The old "%basename" LIKE +
    LIMIT 1 (no ORDER BY) could return EITHER node, leaking a different file's fact under the
    chosen file's header. The fix binds to the candidate's full normalized path."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema_with_properties(conn)
    # The COLLIDING file's node is inserted FIRST (lower rowid) on purpose: the old
    # "%basename" LIKE + LIMIT 1 (no ORDER BY) returns the first rowid -> b/db.py's
    # guard under a request for a/db.py. That makes this a deterministic RED on old code
    # (verified by direct repro), not a rowid-luck pass.
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) VALUES (?,?,?,?,?,0)",
        [
            (1, "Function", "connect", "b/db.py", 200),  # same-named def in a DIFFERENT file
            (2, "Function", "connect", "a/db.py", 10),   # the candidate file's def
        ],
    )
    conn.executemany(
        "INSERT INTO properties (id,node_id,kind,value,line) VALUES (?,?,?,?,?)",
        [
            (1, 1, "guard_clause", "if not b_handle: raise ValueError('b/db.py guard')", 205),
            (2, 2, "guard_clause", "if not a_handle: raise ValueError('a/db.py guard')", 12),
        ],
    )
    conn.commit()
    conn.close()

    # Ask for a/db.py's guard. The header upstream renders "Edit target: a/db.py :: connect".
    txt, line = _edit_target_guard(db, "a/db.py", "connect")
    assert "a/db.py guard" in txt, (
        f"guard text must come from the candidate file a/db.py, got: {txt!r} "
        "(a same-named function in b/db.py leaked its guard under a/db.py's header)"
    )
    assert "b/db.py guard" not in txt
    assert line == 12, f"line must be a/db.py's guard line 12, got {line}"


def test_edit_target_guard_basename_substring_does_not_match_gtdb(tmp_path):
    """Bug L4 (substring half): "%db.py" wrongly matched "gtdb.py". The "%/" || rel suffix
    LIKE enforces a path-separator boundary, so a request for the bare top-level db.py must
    NOT pull gtdb.py's guard. Here ONLY gtdb.py exists -> the bare db.py request must miss
    (correct-or-quiet: no guard line rather than a wrong-file one)."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema_with_properties(conn)
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) "
        "VALUES (1,'Function','open','pkg/gtdb.py',10,0)"
    )
    conn.execute(
        "INSERT INTO properties (id,node_id,kind,value,line) "
        "VALUES (1,1,'guard_clause','if x: return',12)"
    )
    conn.commit()
    conn.close()

    txt, line = _edit_target_guard(db, "db.py", "open")
    assert txt == "" and line is None, (
        f"a bare 'db.py' request must NOT substring-match gtdb.py; got {txt!r} @ {line}"
    )


def test_edit_target_guard_returns_real_fact_for_correct_file(tmp_path):
    """Correct-or-quiet positive control: when the candidate file IS the one with the def,
    the real deterministic guard fact is still delivered (the fix suppresses a WRONG-file
    guess, it never removes a real fact)."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    _schema_with_properties(conn)
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,is_test) "
        "VALUES (1,'Function','set_fields','beets/importer.py',640,0)"
    )
    conn.execute(
        "INSERT INTO properties (id,node_id,kind,value,line) "
        "VALUES (1,1,'conditional_return','if not self.item: return None',645)"
    )
    conn.commit()
    conn.close()

    txt, line = _edit_target_guard(db, "beets/importer.py", "set_fields")
    assert "return None" in txt, f"real guard fact must still be delivered; got {txt!r}"
    assert line == 645
