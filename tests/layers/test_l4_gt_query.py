"""L4 gt_query bundle — pytest suite.

Validates the SWE-agent gt_query bundle at
``tools/sweagent/gt_query/lib/gt_query.py``. The CLI:

  - reads ``GT_GRAPH_DB`` (path to a SQLite graph.db),
  - resolves a symbol by name / qualified_name,
  - emits a family-tagged briefing block capped at ~30 lines,
  - tags edges ``[VERIFIED]`` (same_file/import) or ``[POSSIBLE]`` (name_match),
  - returns exit 2 on bad usage / missing GT_GRAPH_DB and exit 3 on missing
    db file.

The fixture builds a synthetic 5-file × 3-5-function graph.db that exercises:
  - a high-confidence multi-caller symbol (verified import edges),
  - a low-confidence single-caller symbol (name_match only),
  - an overloaded name shared across two files,
  - an isolated node with no callers.

The synthetic graph uses generic identifiers (``alpha``, ``beta``, ``utils``,
etc.) — no Live-Lite-specific symbol names — so the suite is anti-benchmaxxing
by construction.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# ── Locate the script under test ─────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
GT_QUERY_PY = REPO_ROOT / "tools" / "sweagent" / "gt_query" / "lib" / "gt_query.py"


# ── Synthetic graph.db builder ───────────────────────────────────────────────
SCHEMA = """
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
    parent_id INTEGER REFERENCES nodes(id)
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

# Node IDs — kept stable for cross-reference in edge rows below.
# Layout: 5 files × 3-5 functions.
#
#   pkg/alpha.py
#     1  hub_function    (multi-caller, signature + return_type)
#     2  alpha_helper
#     3  overloaded_name (overload #1, in alpha.py)
#
#   pkg/beta.py
#     4  beta_caller_a
#     5  beta_caller_b
#     6  beta_caller_c
#     7  overloaded_name (overload #2, in beta.py)
#
#   pkg/gamma.py
#     8  gamma_user      (single name_match caller of low_conf_target)
#     9  low_conf_target (only inbound edge is name_match)
#
#   pkg/orphan.py
#     10 lonely_island   (exists in nodes, no callers, no callees, no imports)
#     11 orphan_neighbor
#     12 orphan_helper
#
#   tests/test_alpha.py
#     13 test_hub_function (test caller of hub_function via import edge)

NODE_ROWS = [
    # id, label, name, qualified_name, file_path, start_line, end_line,
    # signature, return_type, is_exported, is_test, language, parent_id
    (1, "Function", "hub_function", "pkg.alpha.hub_function",
     "pkg/alpha.py", 10, 30,
     "def hub_function(x: int, y: int) -> int", "int",
     1, 0, "python", None),
    (2, "Function", "alpha_helper", "pkg.alpha.alpha_helper",
     "pkg/alpha.py", 32, 40, None, None, 0, 0, "python", None),
    (3, "Function", "overloaded_name", "pkg.alpha.overloaded_name",
     "pkg/alpha.py", 42, 50, None, None, 1, 0, "python", None),
    (4, "Function", "beta_caller_a", "pkg.beta.beta_caller_a",
     "pkg/beta.py", 5, 15, None, None, 1, 0, "python", None),
    (5, "Function", "beta_caller_b", "pkg.beta.beta_caller_b",
     "pkg/beta.py", 17, 27, None, None, 1, 0, "python", None),
    (6, "Function", "beta_caller_c", "pkg.beta.beta_caller_c",
     "pkg/beta.py", 29, 39, None, None, 1, 0, "python", None),
    (7, "Function", "overloaded_name", "pkg.beta.overloaded_name",
     "pkg/beta.py", 41, 50, None, None, 1, 0, "python", None),
    (8, "Function", "gamma_user", "pkg.gamma.gamma_user",
     "pkg/gamma.py", 5, 15, None, None, 1, 0, "python", None),
    (9, "Function", "low_conf_target", "pkg.gamma.low_conf_target",
     "pkg/gamma.py", 17, 25, None, None, 1, 0, "python", None),
    (10, "Function", "lonely_island", "pkg.orphan.lonely_island",
     "pkg/orphan.py", 5, 12, None, None, 1, 0, "python", None),
    (11, "Function", "orphan_neighbor", "pkg.orphan.orphan_neighbor",
     "pkg/orphan.py", 14, 20, None, None, 1, 0, "python", None),
    (12, "Function", "orphan_helper", "pkg.orphan.orphan_helper",
     "pkg/orphan.py", 22, 30, None, None, 1, 0, "python", None),
    (13, "Function", "test_hub_function", "tests.test_alpha.test_hub_function",
     "tests/test_alpha.py", 5, 18, None, None, 0, 1, "python", None),
]

# Edge rows.
# (source_id, target_id, type, source_line, source_file, resolution_method,
#  confidence, metadata)
EDGE_ROWS = [
    # hub_function: 4 verified import callers + 1 verified test import caller
    (4, 1, "CALLS", 8, "pkg/beta.py", "import", 1.0, None),
    (5, 1, "CALLS", 19, "pkg/beta.py", "import", 1.0, None),
    (6, 1, "CALLS", 31, "pkg/beta.py", "import", 1.0, None),
    (8, 1, "CALLS", 7, "pkg/gamma.py", "import", 1.0, None),
    (13, 1, "CALLS", 9, "tests/test_alpha.py", "import", 1.0, None),
    # hub_function calls alpha_helper (same_file) — gives a callee row
    (1, 2, "CALLS", 22, "pkg/alpha.py", "same_file", 1.0, None),
    # IMPORTS edge for hub_function (so [HALLUCINATED-IMPORT] line fires)
    (4, 1, "IMPORTS", 1, "pkg/beta.py", "import", 1.0, "from pkg.alpha import hub_function"),

    # low_conf_target: a single name_match caller (gamma_user). Confidence 0.9
    # so it survives the 0.7 admissibility floor but is tagged [POSSIBLE].
    (8, 9, "CALLS", 11, "pkg/gamma.py", "name_match", 0.9, None),

    # overloaded_name (id 3, alpha.py): one verified import caller from beta
    (4, 3, "CALLS", 9, "pkg/beta.py", "import", 1.0, None),
    # overloaded_name (id 7, beta.py): one name_match caller
    (8, 7, "CALLS", 12, "pkg/gamma.py", "name_match", 0.9, None),

    # A heavily-connected node to stress the 30-line cap: alpha_helper has
    # many same_file callers. We add 12 same-file CALLS edges from
    # synthetic anonymous callers… but the resolver only walks `nodes` — so
    # instead reuse hub_function as source for repeated CALLS to alpha_helper.
    # Each row counts as a separate caller edge; LIMIT MAX_CALLERS caps
    # rendered rows. To actually stress MAX_LINES we add real, distinct
    # caller nodes via inserting extra rows in a per-test fixture below.
]


def _build_db(path: Path, extra_nodes=(), extra_edges=()) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        list(NODE_ROWS) + list(extra_nodes),
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence, metadata) "
        "VALUES (?,?,?,?,?,?,?,?)",
        list(EDGE_ROWS) + list(extra_edges),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def graph_db(tmp_path) -> Path:
    """Standard synthetic graph.db for most tests."""
    db = tmp_path / "graph.db"
    _build_db(db)
    return db


@pytest.fixture
def graph_db_heavy(tmp_path) -> Path:
    """A graph with one heavily-connected node — enough to trigger
    the MAX_LINES truncation path."""
    db = tmp_path / "graph_heavy.db"
    # Add 30 extra caller nodes + 30 verified inbound CALLS edges to id=1
    # so the renderer would emit far more than 30 lines if uncapped.
    base_id = 1000
    extra_nodes = []
    extra_edges = []
    for i in range(30):
        nid = base_id + i
        extra_nodes.append(
            (nid, "Function", f"heavy_caller_{i}",
             f"pkg.heavy.heavy_caller_{i}",
             f"pkg/heavy_{i % 3}.py", 10 + i, 20 + i,
             None, None, 1, 0, "python", None)
        )
        extra_edges.append(
            (nid, 1, "CALLS", 11 + i, f"pkg/heavy_{i % 3}.py", "import", 1.0, None)
        )
    _build_db(db, extra_nodes=extra_nodes, extra_edges=extra_edges)
    return db


# ── Subprocess driver ────────────────────────────────────────────────────────
def _run(symbol: str, db: Path | None, *, env_overrides: dict | None = None,
         extra_env_unset: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if db is not None:
        env["GT_GRAPH_DB"] = str(db)
    else:
        env.pop("GT_GRAPH_DB", None)
    for k in extra_env_unset:
        env.pop(k, None)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [sys.executable, str(GT_QUERY_PY), symbol],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Tests ────────────────────────────────────────────────────────────────────
def test_script_exists():
    """Sanity: the gt_query script we're testing actually exists on disk."""
    assert GT_QUERY_PY.is_file(), f"missing: {GT_QUERY_PY}"


def test_known_symbol_high_conf(graph_db):
    """hub_function has 4+ verified import callers — output must be VERIFIED-tagged
    and list callers with file:line."""
    proc = _run("hub_function", graph_db)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Header line
    assert "hub_function" in out
    assert "pkg/alpha.py:10" in out
    # At least one VERIFIED tag fires (callers are import-resolved)
    assert "[VERIFIED]" in out
    # CALLER-BLIND-EDIT family rows show file:line for callers
    assert "[CALLER-BLIND-EDIT]" in out
    # File path + line number for at least one of the beta callers
    assert "pkg/beta.py:" in out
    # IMPACT line should fire (>= 3 callers)
    assert "[BLAST-RADIUS]" in out


def test_known_symbol_low_conf(graph_db):
    """low_conf_target has only one name_match caller — output must include
    the [POSSIBLE] tag (not just [VERIFIED])."""
    proc = _run("low_conf_target", graph_db)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "low_conf_target" in out
    # The single inbound caller is name_match, so its CALLER row is [POSSIBLE]
    assert "[POSSIBLE]" in out
    # CALLER family must appear with the gamma_user caller
    assert "[CALLER-BLIND-EDIT]" in out
    assert "gamma_user" in out


def test_missing_symbol(graph_db):
    """Querying a nonexistent symbol must exit 0 with a clean message — no crash."""
    proc = _run("ThisSymbolDoesNotExist123", graph_db)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "no symbol named 'ThisSymbolDoesNotExist123'" in out
    # No traceback / Python error leakage
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in out


def test_overloaded_name(graph_db):
    """`overloaded_name` exists in pkg/alpha.py (id=3) and pkg/beta.py (id=7).

    The resolver picks one (sorted by is_test ASC, id ASC → id=3, alpha.py),
    so the header must point at alpha.py. The callers row should still appear.
    The fact that the resolver disambiguates without crashing AND produces
    confidence-tagged caller evidence is the contract we test here.
    """
    proc = _run("overloaded_name", graph_db)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Resolver picks the first match (alpha.py per ORDER BY). Header points there.
    assert "overloaded_name" in out
    assert "pkg/alpha.py" in out
    # The chosen target (alpha.py overload) has one verified import caller
    # from beta_caller_a — which must be tagged VERIFIED, not POSSIBLE.
    assert "[VERIFIED]" in out
    assert "[CALLER-BLIND-EDIT]" in out
    # And critically: no crash on duplicate-name resolution.
    assert "Traceback" not in proc.stderr


def test_no_graph_db_unset(graph_db):
    """GT_GRAPH_DB unset → exit 2, error to stderr, no crash."""
    proc = _run("hub_function", db=None)
    assert proc.returncode == 2
    assert "GT_GRAPH_DB" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_no_graph_db_missing_file(tmp_path):
    """GT_GRAPH_DB pointing at a nonexistent file → exit 3, error to stderr."""
    fake = tmp_path / "does_not_exist.db"
    proc = _run("hub_function", fake)
    assert proc.returncode == 3
    assert "graph.db not found" in proc.stderr or "cannot open" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_output_30_line_cap(graph_db_heavy):
    """A heavily-connected node must produce ≤ MAX_LINES (30) lines, plus an
    optional trailing truncation marker."""
    proc = _run("hub_function", graph_db_heavy)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    # Hard-cap is enforced by render(): out[:MAX_LINES] + ["# (truncated...)"]
    # so total lines ≤ 31.
    assert len(lines) <= 31, f"got {len(lines)} lines, exceeds cap"
    # If truncation fired, the marker should be present.
    if len(lines) == 31:
        assert "truncated" in lines[-1]


def test_signature_mode(graph_db):
    """A function with signature + return_type populated in the nodes row
    must produce explicit `signature:` and `returns:` lines under the
    CONTRACT-BREAK family."""
    proc = _run("hub_function", graph_db)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "[CONTRACT-BREAK]" in out
    assert "signature:" in out
    assert "def hub_function(x: int, y: int) -> int" in out
    assert "returns:" in out
    # Return type literal "int" appears on the returns: line
    assert "returns: int" in out
