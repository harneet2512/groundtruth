"""RED tests for the gt_* composite endpoints over the derived graph.db tables.

Seeds a synthetic Go-indexer graph.db directly (producer-verbatim DDL for the
derived tables — closure / processes / process_steps / communities /
community_members / resolution_symbols / assertions — copied from
gt-index/internal/store/sqlite.go, internal/process/persist.go and
internal/community/persist.go) so the endpoints read the real surfaces the
producer publishes.

Fixture call graph (all CALLS CERTIFIED, conf 1.0):

    entry_fn(1) -> mid_fn(2) -> leaf_fn(3)
    entry_fn(1) -> Foo(5) -[HAS_METHOD via parent_id]-> bar(6)
    list_users(9) -> leaf_fn(3)

Service boundary:

    list_users(9)  -HANDLES_ROUTE->  api.py file node (10)   @ line 4
    create_order(14) -HANDLES_ROUTE-> api2.py file node (13) @ line 4
    client.py file node (11) -API_CALL-> api.py  (route /api/users, GET)
    client.py file node (11) -API_CALL-> api2.py (route /api/orders, POST)

Derived tables:

    closure:        transitive reach of the call graph above
    process_steps:  proc-1 = sym.entry -> sym.mid -> sym.leaf
    communities:    comm-1 {src/api.py, src/client.py}
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Ok

# ---------------------------------------------------------------------------
# Fixture: producer-verbatim DDL for the derived/overlay tables.
# nodes/edges use the established reduced test subset (same columns the
# producer and the GraphStore bridge actually read) plus stable_id/parent_id.
# ---------------------------------------------------------------------------

_NODES_DDL = """
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
    parent_id INTEGER REFERENCES nodes(id),
    stable_id TEXT
);
"""

_EDGES_DDL = """
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id),
    target_id INTEGER NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT,
    trust_tier TEXT DEFAULT 'SPECULATIVE',
    candidate_count INTEGER DEFAULT 1
);
"""

# Producer-verbatim: gt-index/internal/store/sqlite.go
_RESOLUTION_SYMBOLS_DDL = """
CREATE TABLE IF NOT EXISTS resolution_symbols (
    stable_id TEXT PRIMARY KEY,
    native_id TEXT NOT NULL UNIQUE,
    native_kind TEXT NOT NULL,
    normalized_kind TEXT NOT NULL,
    language TEXT NOT NULL,
    path TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    export_status TEXT NOT NULL
);
"""

# Producer-verbatim: gt-index/internal/store/sqlite.go
_ASSERTIONS_DDL = """
CREATE TABLE IF NOT EXISTS assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_node_id INTEGER NOT NULL REFERENCES nodes(id),
    target_node_id INTEGER DEFAULT 0,
    resolution_score REAL DEFAULT 0.0,
    kind TEXT NOT NULL,
    expression TEXT NOT NULL,
    expected TEXT,
    line INTEGER
);
"""

# Producer-verbatim: gt-index/internal/store/sqlite.go (C7 / RF-4 sidecar)
_CLOSURE_DDL = """
CREATE TABLE IF NOT EXISTS closure (
    source_id INTEGER,
    target_id INTEGER,
    depth INTEGER,
    min_confidence REAL,
    PRIMARY KEY(source_id, target_id, depth)
);
"""

# Producer-verbatim: gt-index/internal/process/persist.go
_PROCESSES_DDL = """
CREATE TABLE IF NOT EXISTS processes (
    id                   TEXT PRIMARY KEY,
    entry_stable_id      TEXT NOT NULL,
    terminal_stable_id   TEXT NOT NULL,
    witness_assertion_id INTEGER NOT NULL REFERENCES assertions(id),
    test_stable_id       TEXT NOT NULL,
    kind                 TEXT NOT NULL DEFAULT '',
    depth                INTEGER NOT NULL,
    trust_floor          TEXT NOT NULL,
    CHECK (depth >= 1),
    CHECK (trust_floor <> '')
);
"""

_PROCESS_STEPS_DDL = """
CREATE TABLE IF NOT EXISTS process_steps (
    process_id TEXT NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    stable_id  TEXT NOT NULL,
    PRIMARY KEY(process_id, ordinal)
);
"""

# Producer-verbatim: gt-index/internal/community/persist.go
_COMMUNITIES_DDL = """
CREATE TABLE IF NOT EXISTS communities (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    heuristic_label TEXT NOT NULL,
    keywords TEXT NOT NULL,
    description TEXT NOT NULL,
    enriched_by TEXT NOT NULL,
    cohesion REAL,
    cohesion_lo REAL,
    cohesion_hi REAL,
    cohesion_n INTEGER NOT NULL DEFAULT 0,
    cohesion_reason TEXT NOT NULL,
    structural_cohesion REAL NOT NULL,
    member_count INTEGER NOT NULL,
    internal_weight REAL NOT NULL,
    external_weight REAL NOT NULL,
    evidence_edge_ids TEXT NOT NULL,
    evidence_truncated INTEGER NOT NULL DEFAULT 0,
    algorithm TEXT NOT NULL,
    resolution REAL NOT NULL,
    w_call REAL NOT NULL,
    w_cochange REAL NOT NULL,
    holdout_commits INTEGER NOT NULL DEFAULT 0
);
"""

_COMMUNITY_MEMBERS_DDL = """
CREATE TABLE IF NOT EXISTS community_members (
    community_id TEXT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    member TEXT NOT NULL,
    member_kind TEXT NOT NULL DEFAULT 'file',
    PRIMARY KEY(community_id, member)
);
"""

_PROJECT_META_DDL = "CREATE TABLE IF NOT EXISTS project_meta (key TEXT PRIMARY KEY, value TEXT);"

_NODE_ROWS = [
    # id, label, name, qualified_name, file_path, start, end, exported, test, lang, parent_id, stable_id
    (1, "Function", "entry_fn", "a.entry_fn", "src/a.py", 1, 10, 1, 0, "python", None, "sym.entry"),
    (2, "Function", "mid_fn", "b.mid_fn", "src/b.py", 1, 12, 1, 0, "python", None, "sym.mid"),
    (3, "Function", "leaf_fn", "c.leaf_fn", "src/c.py", 1, 8, 1, 0, "python", None, "sym.leaf"),
    (4, "Function", "orphan_fn", "d.orphan_fn", "src/d.py", 1, 6, 1, 0, "python", None, "sym.orphan"),
    (5, "Class", "Foo", "e.Foo", "src/e.py", 1, 20, 1, 0, "python", None, "sym.Foo"),
    (6, "Method", "bar", "e.Foo.bar", "src/e.py", 5, 10, 1, 0, "python", 5, "sym.Foo.bar"),
    (7, "Function", "dup", "d1.dup", "src/dup1.py", 1, 6, 1, 0, "python", None, "sym.dup1"),
    (8, "Function", "dup", "d2.dup", "src/dup2.py", 1, 6, 1, 0, "python", None, "sym.dup2"),
    (9, "Function", "list_users", "api.list_users", "src/api.py", 5, 15, 1, 0, "python", None, "sym.list_users"),
    (10, "File", "src/api.py", "src/api.py", "src/api.py", 1, 2, 0, 0, "python", None, "sym.file.api"),
    (11, "File", "src/client.py", "src/client.py", "src/client.py", 1, 2, 0, 0, "python", None, "sym.file.client"),
    (12, "Function", "test_leaf_flow", "tests.test_leaf_flow", "tests/test_flow.py", 1, 10, 0, 1, "python", None, "sym.testleaf"),
    (13, "File", "src/api2.py", "src/api2.py", "src/api2.py", 1, 2, 0, 0, "python", None, "sym.file.api2"),
    (14, "Function", "create_order", "api2.create_order", "src/api2.py", 5, 14, 1, 0, "python", None, "sym.create_order"),
]

_EDGE_ROWS = [
    # source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata, trust_tier, candidate_count
    (1, 2, "CALLS", 4, "src/a.py", "same_file", 1.0, None, "CERTIFIED", 1),
    (2, 3, "CALLS", 5, "src/b.py", "import", 1.0, None, "CERTIFIED", 1),
    (1, 5, "CALLS", 6, "src/a.py", "import", 1.0, None, "CERTIFIED", 1),
    (9, 3, "CALLS", 10, "src/api.py", "same_file", 1.0, None, "CERTIFIED", 1),
    (9, 10, "HANDLES_ROUTE", 5, "src/api.py", "decorator_route", 0.95, None, "CERTIFIED", 1),
    (14, 13, "HANDLES_ROUTE", 5, "src/api2.py", "decorator_route", 0.95, None, "CERTIFIED", 1),
    (
        11,
        10,
        "API_CALL",
        8,
        "src/client.py",
        "route_match",
        0.7,
        json.dumps(
            {
                "route": "/api/users",
                "method": "GET",
                "framework": "FastAPI/Flask",
                "language": "Python",
                "mechanism": "route_decorator",
            }
        ),
        "CANDIDATE",
        1,
    ),
    (
        11,
        13,
        "API_CALL",
        9,
        "src/client.py",
        "route_match",
        0.7,
        json.dumps(
            {
                "route": "/api/orders",
                "method": "POST",
                "framework": "FastAPI/Flask",
                "language": "Python",
                "mechanism": "route_decorator",
            }
        ),
        "CANDIDATE",
        1,
    ),
]

_RESOLUTION_SYMBOL_ROWS = [
    # stable_id, native_id (= nodes.id as str), native_kind, normalized_kind, language, path, qualified_name, start, end, export_status
    ("sym.entry", "1", "function", "function", "python", "src/a.py", "a.entry_fn", 1, 10, "exported"),
    ("sym.mid", "2", "function", "function", "python", "src/b.py", "b.mid_fn", 1, 12, "exported"),
    ("sym.leaf", "3", "function", "function", "python", "src/c.py", "c.leaf_fn", 1, 8, "exported"),
    ("sym.orphan", "4", "function", "function", "python", "src/d.py", "d.orphan_fn", 1, 6, "exported"),
    ("sym.Foo", "5", "class", "class", "python", "src/e.py", "e.Foo", 1, 20, "exported"),
    ("sym.Foo.bar", "6", "method", "method", "python", "src/e.py", "e.Foo.bar", 5, 10, "exported"),
    ("sym.list_users", "9", "function", "function", "python", "src/api.py", "api.list_users", 5, 15, "exported"),
    ("sym.testleaf", "12", "function", "function", "python", "tests/test_flow.py", "tests.test_leaf_flow", 1, 10, "exported"),
    ("sym.create_order", "14", "function", "function", "python", "src/api2.py", "api2.create_order", 5, 14, "exported"),
]

_ASSERTION_ROWS = [
    # id, test_node_id, target_node_id, resolution_score, kind, expression, expected, line
    (1, 12, 1, 0.9, "equals", "assert entry_fn() == 1", "1", 5),
]

_CLOSURE_ROWS = [
    # source_id reaches target_id in `depth` hops at min_confidence
    (2, 3, 1, 1.0),
    (1, 2, 1, 1.0),
    (1, 3, 2, 1.0),
    (1, 5, 1, 1.0),
    (9, 3, 1, 1.0),
]

_PROCESS_ROWS = [
    # id, entry_stable_id, terminal_stable_id, witness_assertion_id, test_stable_id, kind, depth, trust_floor
    ("proc-1", "sym.entry", "sym.leaf", 1, "sym.testleaf", "equals", 2, "CERTIFIED"),
]

_PROCESS_STEP_ROWS = [
    ("proc-1", 0, "sym.entry"),
    ("proc-1", 1, "sym.mid"),
    ("proc-1", 2, "sym.leaf"),
]

_COMMUNITY_ROWS = [
    # 22 columns, schema order
    (
        "comm-1",  # id
        "api layer",  # label
        "src/api",  # heuristic_label
        '["api", "users"]',  # keywords
        "files on the api request path",  # description
        "heuristic",  # enriched_by
        0.75,  # cohesion
        0.5,  # cohesion_lo
        0.9,  # cohesion_hi
        8,  # cohesion_n
        "measured",  # cohesion_reason
        0.6,  # structural_cohesion
        2,  # member_count
        4.0,  # internal_weight
        1.0,  # external_weight
        "[]",  # evidence_edge_ids
        0,  # evidence_truncated
        "leiden",  # algorithm
        1.0,  # resolution
        1.0,  # w_call
        0.5,  # w_cochange
        20,  # holdout_commits
    ),
]

_COMMUNITY_MEMBER_ROWS = [
    ("comm-1", "src/api.py", "file"),
    ("comm-1", "src/client.py", "file"),
]

_API_PY = '''from fastapi import FastAPI

app = FastAPI()

@app.get("/api/users")
def list_users():
    return []
'''

_API2_PY = '''from fastapi import FastAPI

app = FastAPI()

@app.post("/api/orders")
def create_order():
    return {}
'''


def _write_source_files(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "api.py").write_text(_API_PY, encoding="utf-8")
    (root / "src" / "api2.py").write_text(_API2_PY, encoding="utf-8")
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "client.py"):
        (root / "src" / name).write_text("def f():\n    pass\n", encoding="utf-8")


def _make_db(
    path: str,
    *,
    with_closure: bool = True,
    with_processes: bool = True,
    with_communities: bool = True,
) -> None:
    """Seed the fixture graph.db described in the module docstring."""
    conn = sqlite3.connect(path)
    conn.executescript(_NODES_DDL + _EDGES_DDL + _RESOLUTION_SYMBOLS_DDL + _ASSERTIONS_DDL + _PROJECT_META_DDL)
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, "
        "end_line, is_exported, is_test, language, parent_id, stable_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        _NODE_ROWS,
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence, metadata, trust_tier, candidate_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        _EDGE_ROWS,
    )
    conn.executemany(
        "INSERT INTO resolution_symbols (stable_id, native_id, native_kind, "
        "normalized_kind, language, path, qualified_name, start_line, end_line, "
        "export_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        _RESOLUTION_SYMBOL_ROWS,
    )
    conn.executemany(
        "INSERT INTO assertions (id, test_node_id, target_node_id, resolution_score, "
        "kind, expression, expected, line) VALUES (?,?,?,?,?,?,?,?)",
        _ASSERTION_ROWS,
    )
    if with_closure:
        conn.executescript(_CLOSURE_DDL)
        conn.executemany(
            "INSERT INTO closure (source_id, target_id, depth, min_confidence) "
            "VALUES (?,?,?,?)",
            _CLOSURE_ROWS,
        )
        conn.execute(
            "INSERT INTO project_meta (key, value) VALUES ('closure_count', ?)",
            (str(len(_CLOSURE_ROWS)),),
        )
    if with_processes:
        conn.executescript(_PROCESSES_DDL + _PROCESS_STEPS_DDL)
        conn.executemany(
            "INSERT INTO processes (id, entry_stable_id, terminal_stable_id, "
            "witness_assertion_id, test_stable_id, kind, depth, trust_floor) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _PROCESS_ROWS,
        )
        conn.executemany(
            "INSERT INTO process_steps (process_id, ordinal, stable_id) VALUES (?,?,?)",
            _PROCESS_STEP_ROWS,
        )
    if with_communities:
        conn.executescript(_COMMUNITIES_DDL + _COMMUNITY_MEMBERS_DDL)
        conn.executemany(
            "INSERT INTO communities (id, label, heuristic_label, keywords, "
            "description, enriched_by, cohesion, cohesion_lo, cohesion_hi, "
            "cohesion_n, cohesion_reason, structural_cohesion, member_count, "
            "internal_weight, external_weight, evidence_edge_ids, "
            "evidence_truncated, algorithm, resolution, w_call, w_cochange, "
            "holdout_commits) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _COMMUNITY_ROWS,
        )
        conn.executemany(
            "INSERT INTO community_members (community_id, member, member_kind) "
            "VALUES (?,?,?)",
            _COMMUNITY_MEMBER_ROWS,
        )
    conn.commit()
    conn.close()


def _open(db_path: str) -> GraphStore:
    store = GraphStore(db_path=db_path)
    res = store.initialize()
    assert isinstance(res, Ok), res
    return store


@pytest.fixture()
def seeded(tmp_path: Path) -> dict:
    """A seeded graph.db + its on-disk source root."""
    db = str(tmp_path / "graph.db")
    _make_db(db)
    _write_source_files(tmp_path)
    store = _open(db)
    return {"db": db, "root": str(tmp_path), "store": store, "graph": ImportGraph(store)}


# ── gt_trace ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGtTrace:
    async def test_path_found_calls_only(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        result = await handle_gt_trace(
            "entry_fn", "leaf_fn",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        symbols = [step["symbol"] for step in result["path"]]
        assert symbols == ["entry_fn", "mid_fn", "leaf_fn"]
        assert result["path"][0]["relation"] is None
        assert result["path"][1]["relation"] == "CALLS"
        assert result["path"][2]["file"] == "src/c.py"
        assert result["path"][2]["line"] == 1
        assert result["truncated"] is False

    async def test_path_found_via_containment(self, seeded: dict) -> None:
        """entry_fn CALLS class Foo; reaching method bar requires the
        class->method containment hop (nodes.parent_id)."""
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        result = await handle_gt_trace(
            "entry_fn", "bar",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        symbols = [step["symbol"] for step in result["path"]]
        assert symbols == ["entry_fn", "Foo", "bar"]
        assert result["path"][2]["relation"] == "HAS_METHOD"
        assert result["path"][2]["confidence"] == 1.0

    async def test_no_path(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        result = await handle_gt_trace(
            "leaf_fn", "entry_fn",  # directed: leaf never reaches entry
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "no_path"
        assert result["path"] == []

    async def test_from_not_found(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        result = await handle_gt_trace(
            "missing_fn", "leaf_fn",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "not_found"
        assert result["endpoint"] == "from"

    async def test_to_ambiguous(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        result = await handle_gt_trace(
            "entry_fn", "dup",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ambiguous"
        assert result["endpoint"] == "to"
        assert len(result["candidates"]) == 2

    async def test_not_graph_db_abstains(self) -> None:
        """A store not backed by nodes/edges returns a typed unavailable, not an error."""
        from groundtruth.index.store import SymbolStore
        from groundtruth.mcp.endpoints.trace_path import handle_gt_trace

        store = SymbolStore(":memory:")
        store.initialize()
        result = await handle_gt_trace(
            "a", "b", store=store, graph=ImportGraph(store), root_path="/tmp",
        )
        assert result["status"] == "unavailable"
        assert result["path"] == []


# ── gt_detect_changes ───────────────────────────────────────────────────────

_DIFF_MID = """diff --git a/src/b.py b/src/b.py
index 1111111..2222222 100644
--- a/src/b.py
+++ b/src/b.py
@@ -4,5 +4,6 @@
 context
-old_line
+new_line
+newer_line
 context
"""

_DIFF_UNMAPPED = """diff --git a/docs/guide.md b/docs/guide.md
index 1111111..2222222 100644
--- a/docs/guide.md
+++ b/docs/guide.md
@@ -1,2 +1,2 @@
-old
+new
"""


@pytest.mark.asyncio
class TestGtDetectChanges:
    async def test_changed_symbol_joins_process(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=seeded["store"], graph=seeded["graph"],
            root_path=seeded["root"], diff=_DIFF_MID,
        )
        assert result["changed_count"] == 1
        assert result["changed_symbols"][0]["name"] == "mid_fn"
        assert result["affected_count"] == 1
        assert result["affected_processes"][0]["id"] == "proc-1"
        assert result["affected_processes"][0]["entry"] == "a.entry_fn"
        assert result["affected_processes"][0]["witnessed_by"] == "tests.test_leaf_flow"
        assert result["risk_level"] == "high"
        assert result["partial"] is False
        assert result["truncated"] is False

    async def test_unmapped_file_partial(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=seeded["store"], graph=seeded["graph"],
            root_path=seeded["root"], diff=_DIFF_UNMAPPED,
        )
        assert result["changed_count"] == 0
        assert result["affected_count"] == 0
        assert result["risk_level"] == "low"
        assert result["partial"] is True
        assert "docs/guide.md" in result["unmapped_files"]

    async def test_garbage_diff_unknown_risk(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=seeded["store"], graph=seeded["graph"],
            root_path=seeded["root"], diff="this is not a unified diff at all",
        )
        assert result["risk_level"] == "unknown"
        assert result["changed_symbols"] == []

    async def test_empty_diff_is_low_not_unknown(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=seeded["store"], graph=seeded["graph"],
            root_path=seeded["root"], diff="",
        )
        assert result["risk_level"] == "low"
        assert result["changed_count"] == 0
        assert result["partial"] is False

    async def test_no_process_tables_still_reports_changes(self, seeded: dict) -> None:
        """Without processes/process_steps the surface degrades, never fabricates."""
        db = str(Path(seeded["root"]) / "no_proc.db")
        _make_db(db, with_processes=False)
        store = _open(db)
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=store, graph=ImportGraph(store),
            root_path=seeded["root"], diff=_DIFF_MID,
        )
        assert result["changed_count"] == 1
        assert result["affected_processes"] == []
        assert "processes" in result["degraded"]

    @pytest.mark.skipif(
        subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
        reason="git not on PATH",
    )
    async def test_live_git_diff(self, tmp_path: Path, seeded: dict) -> None:
        """No diff argument -> the endpoint reads the working tree's git diff."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        target = repo / "src" / "b.py"
        target.write_text("def mid_fn():\n    return 1\n", encoding="utf-8")
        for args in (["init"], ["add", "."]):
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        target.write_text("def mid_fn():\n    return 2\n", encoding="utf-8")

        # The graph indexes repo-relative paths, so point the store's file_paths
        # at src/b.py (the fixture already does) and run against repo root.
        from groundtruth.mcp.endpoints.detect_changes import handle_gt_detect_changes

        result = await handle_gt_detect_changes(
            store=seeded["store"], graph=seeded["graph"], root_path=str(repo),
        )
        assert result["changed_count"] == 1
        assert result["changed_symbols"][0]["file"] == "src/b.py"


# ── gt_route_map / gt_api_impact ────────────────────────────────────────────


@pytest.mark.asyncio
class TestGtRouteMap:
    async def test_routes_with_consumers_and_flows(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.route_map import handle_gt_route_map

        result = await handle_gt_route_map(
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        assert len(result["routes"]) == 2
        by_name = {r["name"]: r for r in result["routes"]}
        users = by_name["/api/users"]
        assert users["method"] == "GET"
        assert users["handler_file"] == "src/api.py"
        assert users["handler"] == "list_users"
        assert users["middleware"] == []  # producer stores no middleware facts
        consumer_files = [c["file"] for c in users["consumers"]]
        assert "src/client.py" in consumer_files
        flow_symbols = [f["symbol"] for f in users["flows"]]
        assert "leaf_fn" in flow_symbols
        orders = by_name["/api/orders"]
        assert orders["method"] == "POST"

    async def test_missing_edges_table_unavailable(self, seeded: dict) -> None:
        from groundtruth.index.store import SymbolStore
        from groundtruth.mcp.endpoints.route_map import handle_gt_route_map

        store = SymbolStore(":memory:")
        store.initialize()
        result = await handle_gt_route_map(
            store=store, graph=ImportGraph(store), root_path=seeded["root"],
        )
        assert result["status"] == "unavailable"
        assert result["routes"] == []


@pytest.mark.asyncio
class TestGtApiImpact:
    async def test_multi_fetch_consumer_attribution_note(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.route_map import handle_gt_api_impact

        result = await handle_gt_api_impact(
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        by_name = {r["name"]: r for r in result["routes"]}
        consumers = by_name["/api/users"]["consumers"]
        client = next(c for c in consumers if c["file"] == "src/client.py")
        # src/client.py fetches both /api/users and /api/orders.
        assert client["routes_called"] == 2
        assert "attributionNote" in client

    async def test_route_filter(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.route_map import handle_gt_api_impact

        result = await handle_gt_api_impact(
            route="/api/users",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        assert [r["name"] for r in result["routes"]] == ["/api/users"]

    async def test_route_not_found(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.route_map import handle_gt_api_impact

        result = await handle_gt_api_impact(
            route="/nope",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "not_found"


# ── gt_closure ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGtClosure:
    async def test_transitive_callers_and_callees(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.closure import handle_gt_closure

        result = await handle_gt_closure(
            "leaf_fn",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        callers = {c["symbol"]: c["depth"] for c in result["callers"]}
        assert callers["mid_fn"] == 1
        assert callers["entry_fn"] == 2
        assert callers["list_users"] == 1
        callees = {c["symbol"]: c["depth"] for c in result["callees"]}
        assert callees == {}  # leaf is a sink

    async def test_callee_direction(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.closure import handle_gt_closure

        result = await handle_gt_closure(
            "entry_fn",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        callees = {c["symbol"]: c["depth"] for c in result["callees"]}
        assert callees["mid_fn"] == 1
        assert callees["leaf_fn"] == 2

    async def test_table_absent_unavailable(self, seeded: dict) -> None:
        db = str(Path(seeded["root"]) / "no_closure.db")
        _make_db(db, with_closure=False)
        store = _open(db)
        from groundtruth.mcp.endpoints.closure import handle_gt_closure

        result = await handle_gt_closure(
            "leaf_fn", store=store, graph=ImportGraph(store), root_path=seeded["root"],
        )
        assert result["status"] == "unavailable"
        assert result["callers"] == []
        assert result["callees"] == []

    async def test_not_found(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.closure import handle_gt_closure

        result = await handle_gt_closure(
            "missing_fn",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "not_found"


# ── gt_community ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGtCommunity:
    async def test_communities_listed(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.community import handle_gt_community

        result = await handle_gt_community(
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        assert len(result["communities"]) == 1
        comm = result["communities"][0]
        assert comm["name"] == "api layer"
        assert comm["cohesion"] == pytest.approx(0.75)
        assert comm["member_count"] == 2
        assert comm["top_members"] == ["src/api.py", "src/client.py"]

    async def test_member_filter(self, seeded: dict) -> None:
        from groundtruth.mcp.endpoints.community import handle_gt_community

        result = await handle_gt_community(
            member="src/client.py",
            store=seeded["store"], graph=seeded["graph"], root_path=seeded["root"],
        )
        assert result["status"] == "ok"
        assert len(result["communities"]) == 1

    async def test_table_absent_unavailable(self, seeded: dict) -> None:
        db = str(Path(seeded["root"]) / "no_comm.db")
        _make_db(db, with_communities=False)
        store = _open(db)
        from groundtruth.mcp.endpoints.community import handle_gt_community

        result = await handle_gt_community(
            store=store, graph=ImportGraph(store), root_path=seeded["root"],
        )
        assert result["status"] == "unavailable"
        assert result["communities"] == []


# ── composite impl + server registration ────────────────────────────────────


class TestCompositeWiring:
    def test_gt_trace_impl_renders_evidence_block(self, seeded: dict) -> None:
        from groundtruth.mcp.composite import gt_trace_impl

        out = gt_trace_impl(
            "entry_fn", "leaf_fn",
            db_path=seeded["db"], root_path=seeded["root"],
        )
        assert out.startswith('<gt-evidence tool="gt_trace">')
        payload = json.loads(out.split("\n", 1)[1].rsplit("\n</gt-evidence>", 1)[0])
        assert payload["status"] == "ok"

    def test_gt_detect_changes_impl(self, seeded: dict) -> None:
        from groundtruth.mcp.composite import gt_detect_changes_impl

        out = gt_detect_changes_impl(
            db_path=seeded["db"], root_path=seeded["root"], diff=_DIFF_MID,
        )
        payload = json.loads(out.split("\n", 1)[1].rsplit("\n</gt-evidence>", 1)[0])
        assert payload["affected_count"] == 1

    def test_composite_server_registers_tools(self, seeded: dict) -> None:
        import asyncio

        from groundtruth.mcp.composite_server import create_composite_server

        app = create_composite_server(seeded["root"], seeded["db"])
        names = {t.name for t in asyncio.run(app.list_tools())}
        for tool in (
            "gt_lookup", "gt_impact", "gt_check",  # pre-existing
            "gt_trace", "gt_detect_changes", "gt_route_map",
            "gt_api_impact", "gt_closure", "gt_community",
        ):
            assert tool in names

    def test_tools_py_registers_handlers(self, seeded: dict) -> None:
        from groundtruth.mcp import tools

        for name in (
            "handle_gt_trace", "handle_gt_detect_changes", "handle_gt_route_map",
            "handle_gt_api_impact", "handle_gt_closure", "handle_gt_community",
        ):
            assert callable(getattr(tools, name, None)), name
        for step in (
            "trace_path", "detect_changes", "route_map",
            "api_impact", "closure", "community",
        ):
            assert step in tools._VALID_STEPS
