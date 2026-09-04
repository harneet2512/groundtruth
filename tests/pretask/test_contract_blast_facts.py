"""Regression (G17, gt_new §6:73 — REQUIRES-I2-LIPI): the contract pillar owed
depth trickle-down. The edit-target's promoted WRITES (the class fields it mutates)
plus the count of VERIFIED readers of that state surface as a node-local
SCOPE/COMPLETENESS blast fact ("writes to <field>; <N> verified readers").

Two contracts under test:
  1. FUNCTIONAL — a WRITES edge from the edit-target surfaces the scope line in BOTH
     the inline `contract_line` and the full `render_contract` block, with the right
     verified-reader count; correct-or-quiet when no WRITES is promoted; READS/WRITES
     are confidence-gated >= 0.5.
  2. I2 (depth-never-enters-RANK) — adding the READS/WRITES depth edges leaves the
     ranking surfaces (`_top_functions`, `_hub_degree_fn`) byte-identical. The blast
     facts are DISPLAY-ONLY; they touch no `_rrf3`/`_total_score`/reach/degree term.
     (contract_map has no rank path at all — `_rrf3` lives in graph_localizer/
     curation_map, `_total_score` in v7_4_brief; this proves the boundary holds.)
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.contract_map import (
    build_contract,
    contract_line,
    render_contract,
)
from groundtruth.pretask.v1r_brief import _hub_degree_fn, _top_functions


_SCHEMA = """
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
    CREATE TABLE properties (
        id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT,
        line INTEGER, confidence REAL
    );
"""

# node 10: class Counter  (owning class — the WRITES/READS edge TARGET)
# node 1:  increment  (EDIT TARGET) -- WRITES _count
# node 2:  value      (reader)      -- READS  _count   (verified, conf 0.9)
# node 3:  total      (reader)      -- READS  _count   (verified, conf 0.9)
# node 4:  peek       (reader)      -- READS  _count   (conf 0.4 -> BELOW floor, NOT counted)
_NODES = [
    (10, "Class", "Counter", "", "counter.py", 1, 40, "class Counter:", "", 0, 0, "python", None),
    (
        1,
        "Method",
        "increment",
        "",
        "counter.py",
        5,
        9,
        "def increment(self):",
        "",
        0,
        0,
        "python",
        10,
    ),
    (
        2,
        "Method",
        "value",
        "",
        "counter.py",
        11,
        13,
        "def value(self) -> int:",
        "int",
        0,
        0,
        "python",
        10,
    ),
    (
        3,
        "Method",
        "total",
        "",
        "counter.py",
        15,
        17,
        "def total(self) -> int:",
        "int",
        0,
        0,
        "python",
        10,
    ),
    (
        4,
        "Method",
        "peek",
        "",
        "counter.py",
        19,
        21,
        "def peek(self) -> int:",
        "int",
        0,
        0,
        "python",
        10,
    ),
]
# WRITES: increment(1) -> Counter(10), field=_count, verified
# READS:  value(2)/total(3)/peek(4) -> Counter(10), field=_count
_DEPTH_EDGES = [
    (1, 10, "WRITES", "promote_write", 0.9, "_count"),
    (2, 10, "READS", "promote_field_read", 0.9, "_count"),
    (3, 10, "READS", "promote_field_read", 0.9, "_count"),
    (4, 10, "READS", "promote_field_read", 0.4, "_count"),  # below 0.5 floor
]


def _make_db(path: str, *, with_depth: bool) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _NODES)
    # increment has its own contract signal so the block always renders.
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,'guard_clause','raise: self is None',6,1.0)"
    )
    if with_depth:
        conn.executemany(
            "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
            "VALUES (?,?,?,?,?,?)",
            _DEPTH_EDGES,
        )
    conn.commit()
    conn.close()


# ── 1. FUNCTIONAL ───────────────────────────────────────────────────────────


def test_blast_fact_in_contract_line(tmp_path):
    db = str(tmp_path / "g.db")
    _make_db(db, with_depth=True)
    line = contract_line(db, "counter.py", ["increment"])
    # Two readers (value, total) clear the 0.5 floor; peek (0.4) does not.
    assert "scope writes to _count; 2 verified readers" in line


def test_blast_fact_in_full_block(tmp_path):
    db = str(tmp_path / "g.db")
    _make_db(db, with_depth=True)
    block = render_contract(build_contract(db, [("counter.py", "increment")]))
    assert block.startswith("<gt-contract>")
    assert "scope: writes to _count; 2 verified readers" in block


def test_blast_fact_singular_reader(tmp_path):
    """One verified reader -> 'reader' (singular), grammatical correctness."""
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _NODES[:3]
    )  # class+increment+value
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,'guard_clause','raise: x',6,1.0)"
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (?,?,?,?,?,?)",
        [_DEPTH_EDGES[0], _DEPTH_EDGES[1]],  # one WRITES, one READS
    )
    conn.commit()
    conn.close()
    line = contract_line(db, "counter.py", ["increment"])
    assert "scope writes to _count; 1 verified reader" in line
    assert "readers" not in line  # singular, not plural


def test_no_writes_no_blast_fact(tmp_path):
    """Correct-or-quiet: with no promoted WRITES the scope line is absent entirely."""
    db = str(tmp_path / "nodepth.db")
    _make_db(db, with_depth=False)
    line = contract_line(db, "counter.py", ["increment"])
    assert "scope" not in line
    block = render_contract(build_contract(db, [("counter.py", "increment")]))
    assert "scope" not in block


def test_blast_fact_excludes_edit_target_self_as_reader(tmp_path):
    """The edit-target's own nodes must never be counted among the readers."""
    db = str(tmp_path / "selfread.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _NODES[:2]
    )  # class + increment only
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,'guard_clause','raise: x',6,1.0)"
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1, 10, "WRITES", "promote_write", 0.9, "_count"),
            (1, 10, "READS", "promote_field_read", 0.9, "_count"),  # increment also READS _count
        ],
    )
    conn.commit()
    conn.close()
    line = contract_line(db, "counter.py", ["increment"])
    # increment writes _count but is the only reader (itself) -> 0 OTHER readers ->
    # bare "writes to _count", never "1 verified reader".
    assert "scope writes to _count" in line
    assert "verified reader" not in line


def test_blast_fact_reader_count_is_field_exact(tmp_path):
    """G17 §2.9 field-exactness: the verified-reader count must count readers of the
    SPECIFIC written field, not every reader of the owning class. A reader of a
    DIFFERENT field of the same class must NOT inflate the count.

    promote.go stores the bare field name in edge.metadata for both READS and WRITES, so
    the reader query joins on `e.metadata = field`. Pre-fix (no metadata predicate) this
    counted BOTH readers of the class -> "2 verified readers" (over-attribution). The
    single-field fixtures above could not catch it. Field-exact -> exactly 1."""
    db = str(tmp_path / "twofield.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    nodes = [
        (
            10,
            "Class",
            "Counter",
            "",
            "counter.py",
            1,
            40,
            "class Counter:",
            "",
            0,
            0,
            "python",
            None,
        ),
        (
            1,
            "Method",
            "increment",
            "",
            "counter.py",
            5,
            9,
            "def increment(self):",
            "",
            0,
            0,
            "python",
            10,
        ),
        (
            2,
            "Method",
            "value",
            "",
            "counter.py",
            11,
            13,
            "def value(self) -> int:",
            "int",
            0,
            0,
            "python",
            10,
        ),
        (
            5,
            "Method",
            "name",
            "",
            "counter.py",
            15,
            17,
            "def name(self) -> str:",
            "str",
            0,
            0,
            "python",
            10,
        ),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", nodes)
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (1,'guard_clause','raise: x',6,1.0)"
    )
    conn.executemany(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,metadata) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1, 10, "WRITES", "promote_write", 0.9, "_count"),  # increment writes _count
            (2, 10, "READS", "promote_field_read", 0.9, "_count"),  # value reads _count  -> COUNTED
            (
                5,
                10,
                "READS",
                "promote_field_read",
                0.9,
                "_label",
            ),  # name reads a DIFFERENT field -> NOT counted
        ],
    )
    conn.commit()
    conn.close()
    line = contract_line(db, "counter.py", ["increment"])
    assert "scope writes to _count; 1 verified reader" in line
    assert "2 verified readers" not in line


# ── 2. I2 — DEPTH NEVER ENTERS RANK ─────────────────────────────────────────


def test_depth_edges_do_not_change_top_functions_rank(tmp_path):
    """A graph WITH the promoted READS/WRITES must produce byte-identical
    `_top_functions` output to the same graph WITHOUT them. Blast facts are
    display-only; they feed no ranking term."""
    db_with = str(tmp_path / "with.db")
    db_without = str(tmp_path / "without.db")
    _make_db(db_with, with_depth=True)
    _make_db(db_without, with_depth=False)
    assert _top_functions(db_with, "counter.py") == _top_functions(db_without, "counter.py")


def test_depth_edges_do_not_change_hub_degree(tmp_path):
    """The hub-degree surface (the p80 rank gate) counts CALLS only — promoted
    READS/WRITES into Counter must NOT inflate counter.py's degree."""
    db_with = str(tmp_path / "with.db")
    db_without = str(tmp_path / "without.db")
    _make_db(db_with, with_depth=True)
    _make_db(db_without, with_depth=False)
    _p80_w, deg_with = _hub_degree_fn(db_with)
    _p80_wo, deg_without = _hub_degree_fn(db_without)
    assert deg_with("counter.py") == deg_without("counter.py")
    # No CALLS edges at all in either graph -> degree is 0 with OR without depth.
    assert deg_with("counter.py") == 0


def test_contract_map_has_no_rank_usage():
    """Structural I2 guard: contract_map must never USE a rank/score term as code.
    If a future edit wires a blast fact into ranking (imports or calls a ranking
    symbol), this fails loudly. We scan AST names, not prose, so a docstring may
    still REFER to the boundary without tripping the guard."""
    import ast

    import groundtruth.pretask.contract_map as cm

    tree = ast.parse(open(cm.__file__, encoding="utf-8").read())
    forbidden = {"_rrf3", "_total_score", "W_REACH", "compute_reach", "reach_score"}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                used.add(alias.name)
    leaked = used & forbidden
    assert not leaked, f"contract_map must not USE rank term(s) {leaked!r}"
