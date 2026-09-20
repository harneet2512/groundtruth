"""Fixture contract tests for the topology/typed-surface producers.

Covers route_map, api_impact, taint, rename, shape_check and tool_map against
a real SQLite graph.db carrying HANDLES_ROUTE / API_CALL / DECORATES /
IMPLEMENTS / METHOD_OVERRIDES edges — plus a real source file on disk because
route_map re-reads the decorator line through the producer's own patterns.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime.deterministic_queries import (
    DeterministicQueryContext,
    execute_query,
)
from groundtruth.runtime.observation_compiler import (
    CONFIGURATION_BINDING_SCHEMA,
    REPOSITORY_SNAPSHOT_SCHEMA,
    ActionKind,
    ActionRequest,
    ConfigurationBinding,
    Coverage,
    EvidenceSemantics,
    RepositorySnapshot,
    RequestedFidelity,
    RevisionVector,
)

GRAPH_REVISION = "fac220b53ca460ac7dcb0af582206559feeab6c7"
CONTENT_REVISION = "repo-content-rev"
WORKING_TREE = "c" * 64

_SCHEMA = """
CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported INTEGER DEFAULT 0,
    is_test INTEGER DEFAULT 0,
    language TEXT NOT NULL,
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    trust_tier TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT,
    access_sites TEXT
);
CREATE TABLE file_hashes (file_path TEXT PRIMARY KEY, content_hash TEXT);
CREATE TABLE cfg_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    block_index INTEGER NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    statement_lines TEXT
);
CREATE INDEX idx_cfg_blocks_node ON cfg_blocks(node_id);
CREATE TABLE cfg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    from_block INTEGER NOT NULL,
    to_block INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_cfg_edges_node ON cfg_edges(node_id);
CREATE TABLE cfg_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    block_index INTEGER NOT NULL,
    var_name TEXT NOT NULL,
    line INTEGER
);
CREATE INDEX idx_cfg_defs_node ON cfg_defs(node_id);
CREATE TABLE properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    line INTEGER,
    confidence REAL DEFAULT 1.0
);
CREATE INDEX idx_properties_node ON properties(node_id, kind);
"""

# The route decorator line must exist on disk at api.py line 11.
_API_SOURCE = """\
from fastapi import FastAPI

app = FastAPI()


def helper() -> str:
    return "ok"


@app.get("/api/items")
def list_items():
    return {"items": helper()}
"""

_CALC_SOURCE = """\
def calc_total(items, tax):
    subtotal = 0
    for it in items:
        subtotal += it
    total = subtotal * (1 + tax)
    return total


def report(items):
    t = calc_total(items, 0.2)
    return t
"""

# gofn: entry/if-true/if-else/merge/exit shape — a def in each arm plus a
# merge-point use, so both data and control dependence are exercised.
_GO_SOURCE = """\
package main

func gofn(x int) int {
	a := 0
	if x > 0 {
		b := 1
		println(b)
	} else {
		a = 2
	}
	return a
}

func nocfg() int {
	return 1
}

func gocaller(n int) int {
	return gofn(n)
}
"""

# tsfn: straight-line function — def chain only, no control dependence.
# Svc.use: named-receiver field read (r: Repo) for owner-bound taint via
# param annotation.  tsSpread: variadic callee for signature-proven
# actual binding; tsCaller calls it with extra positionals.
_TS_SOURCE = """\
function tsfn(p: number): number {
	const q = p + 1;
	return q;
}

class Svc {
	use(r: Repo): number {
		return r.state;
	}
}

function tsSpread(first: number, ...rest: number[]): number {
	let total = first;
	for (const n of rest) { total += n; }
	return total;
}

function tsCaller(): number {
	return tsSpread(1, 2, 3);
}

function check(r: Repo): number {
	return r.state ? 1 : 0;
}
"""

# (id, label, name, qualified, path, start, end, sig, ret, exported, is_test, lang)
_NODES = [
    (1, "File", "api.py", "api.py", "api.py", 1, 13, "", "", 0, 0, "python"),
    (2, "Function", "list_items", "api.list_items", "api.py", 11, 12,
     "def list_items()", "dict", 1, 0, "python"),
    (3, "Function", "helper", "api.helper", "api.py", 6, 7,
     "def helper()", "str", 1, 0, "python"),
    (4, "File", "client.py", "client.py", "client.py", 1, 20, "", "", 0, 0, "python"),
    (5, "Function", "fetch_items", "client.fetch_items", "client.py", 3, 9,
     "def fetch_items()", "dict", 1, 0, "python"),
    # tool_map: @tool-decorated function
    (6, "Function", "tool", "mcp.tool", "mcp_server.py", 1, 3,
     "def tool()", "", 1, 0, "python"),
    (7, "Function", "search_docs", "mcp_server.search_docs", "mcp_server.py", 5, 12,
     "def search_docs(q)", "list", 1, 0, "python"),
    # shape_check: interface + partial implementer
    (8, "Interface", "Animal", "zoo.Animal", "zoo.py", 1, 8, "", "", 1, 0, "python"),
    (9, "Method", "speak", "zoo.Animal.speak", "zoo.py", 3, 4,
     "def speak(self)", "str", 1, 0, "python", 8),
    (10, "Method", "eat", "zoo.Animal.eat", "zoo.py", 6, 7,
     "def eat(self, food)", "None", 1, 0, "python", 8),
    (11, "Class", "Dog", "zoo.Dog", "zoo.py", 10, 20, "", "", 1, 0, "python"),
    (12, "Method", "speak", "zoo.Dog.speak", "zoo.py", 12, 13,
     "def speak(self)", "str", 1, 0, "python", 11),
    # shape_check: override arity mismatch (Base.run(self, x) vs Sub.run(self))
    (13, "Method", "run", "base.Base.run", "base.py", 3, 5,
     "def run(self, x)", "None", 1, 0, "python", 15),
    (14, "Method", "run", "sub.Sub.run", "sub.py", 3, 5,
     "def run(self)", "None", 1, 0, "python", 16),
    (15, "Class", "Base", "base.Base", "base.py", 1, 10, "", "", 1, 0, "python"),
    (16, "Class", "Sub", "sub.Sub", "sub.py", 1, 10, "", "", 1, 0, "python"),
    # taint: get_input -> process -> execute
    (17, "Function", "get_input", "srv.get_input", "srv.py", 2, 6,
     "def get_input()", "str", 1, 0, "python"),
    (18, "Function", "process", "srv.process", "srv.py", 8, 12,
     "def process(v)", "str", 1, 0, "python"),
    (19, "Function", "execute", "db.execute", "db.py", 4, 8,
     "def execute(sql)", "cursor", 1, 0, "python"),
    # slice: multi-statement function on disk + non-Python function
    (20, "Function", "calc_total", "calc.calc_total", "calc.py", 1, 6,
     "def calc_total(items, tax)", "float", 1, 0, "python"),
    (21, "Function", "gofn", "main.gofn", "main.go", 3, 12,
     "func gofn(x int) int", "int", 1, 0, "go"),
    # interprocedural slice: report calls calc_total at line 10
    (22, "Function", "report", "calc.report", "calc.py", 9, 11,
     "def report(items)", "float", 1, 0, "python"),
    # slice: go function with no persisted CFG -> no_persisted_cfg
    (23, "Function", "nocfg", "main.nocfg", "main.go", 14, 16,
     "func nocfg() int", "int", 1, 0, "go"),
    # slice: language outside the persisted-CFG set -> unsupported_language
    (24, "Function", "rustfn", "lib.rustfn", "lib.rs", 1, 3,
     "fn rustfn()", "", 1, 0, "rust"),
    # slice: typescript function with a persisted CFG (linear def chain)
    (25, "Function", "tsfn", "app.tsfn", "app.ts", 1, 4,
     "function tsfn(p: number): number", "number", 1, 0, "typescript"),
    # gocaller calls gofn at line 19 — persisted interprocedural slice hop.
    (26, "Function", "gocaller", "main.gocaller", "main.go", 18, 20,
     "func gocaller(n int) int", "int", 1, 0, "go"),
    # Owner-bound taint: two classes with same-named field "state" —
    # Repo.set/get (owner Repo) must channel; Other.peek (owner Other)
    # must NOT join Repo's writer.
    (27, "Class", "Repo", "db.Repo", "db.py", 1, 10,
     "class Repo", "", 0, 0, "python", None),
    (28, "Method", "set", "db.Repo.set", "db.py", 2, 4,
     "def set(self, v)", "", 0, 0, "python", 27),
    (29, "Method", "get", "db.Repo.get", "db.py", 5, 7,
     "def get(self)", "", 0, 0, "python", 27),
    (30, "Class", "Other", "db.Other", "db.py", 11, 15,
     "class Other", "", 0, 0, "python", None),
    (31, "Method", "peek", "db.Other.peek", "db.py", 12, 14,
     "def peek(self)", "", 0, 0, "python", 30),
    # Named-receiver taint: Svc.use reads r.state where param r: Repo —
    # owner resolves through the param annotation to class Repo.
    (32, "Class", "Svc", "app.Svc", "app.ts", 6, 10,
     "class Svc", "", 0, 0, "typescript", None),
    (33, "Method", "use", "app.Svc.use", "app.ts", 7, 9,
     "use(r: Repo): number", "number", 0, 0, "typescript", 32),
    # Variadic callee + caller for signature-proven actual binding.
    (34, "Function", "tsSpread", "app.tsSpread", "app.ts", 12, 16,
     "function tsSpread(first: number, ...rest: number[]): number",
     "number", 1, 0, "typescript"),
    (35, "Function", "tsCaller", "app.tsCaller", "app.ts", 18, 20,
     "function tsCaller(): number", "number", 1, 0, "typescript"),
    # Persisted-receiver taint: check(r: Repo) reads r.state — the v2
    # access_sites JSON carries "receiver":"r" and there is deliberately
    # NO field_read prop, so only the persisted receiver can bind it.
    (36, "Function", "check", "app.check", "app.ts", 22, 24,
     "function check(r: Repo): number", "number", 1, 0, "typescript"),
]

# (src, dst, type, line, file, method, tier, confidence, metadata)
_EDGES = [
    (2, 1, "HANDLES_ROUTE", 10, "api.py", "decorator_route", "CERTIFIED", 0.95, ""),
    (2, 3, "CALLS", 13, "api.py", "same_file", "CERTIFIED", 1.0, ""),
    (5, 1, "API_CALL", 6, "client.py", "route_match", "CERTIFIED", 0.7,
     '{"route": "/api/items", "method": "GET", "framework": "requests"}'),
    (6, 7, "DECORATES", 4, "mcp_server.py", "decorator_applied", "CERTIFIED", 1.0, ""),
    (11, 8, "IMPLEMENTS", 10, "zoo.py", "declared_implements", "CERTIFIED", 1.0, ""),
    (14, 13, "METHOD_OVERRIDES", 3, "sub.py", "method_override", "CERTIFIED", 1.0, ""),
    (17, 18, "CALLS", 4, "srv.py", "same_file", "CERTIFIED", 1.0, ""),
    (18, 19, "CALLS", 10, "srv.py", "import", "CERTIFIED", 1.0, ""),
    # field-mediated channel: get_input WRITES payload, execute READS it —
    # a data channel invisible to CALLS traversal.
    (17, 17, "WRITES", 5, "srv.py", "field_write", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"payload","access":"write","line":5,"scope_name":"get_input"}'),
    (19, 19, "READS", 6, "db.py", "field_read", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"payload","access":"read","line":6,"scope_name":"execute"}'),
    (5, 2, "CALLS", 6, "client.py", "import", "CERTIFIED", 1.0, ""),
    (22, 20, "CALLS", 10, "calc.py", "same_file", "CERTIFIED", 1.0, ""),
    (26, 21, "CALLS", 19, "main.go", "same_file", "CERTIFIED", 1.0, ""),
    # Owner-bound channel: Repo.set WRITES state, Repo.get READS state.
    (28, 28, "WRITES", 3, "db.py", "field_write", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"state","access":"write","line":3,"scope_node_id":28}'),
    (29, 29, "READS", 6, "db.py", "field_read", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"state","access":"read","line":6,"scope_node_id":29}'),
    # Same field name, different owner — must never join Repo's channel.
    (31, 31, "READS", 13, "db.py", "field_read", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"state","access":"read","line":13,"scope_node_id":31}'),
    # Named receiver r: Repo — joins Repo.set's writer via the param type.
    (33, 33, "READS", 8, "app.ts", "field_read", "CERTIFIED", 1.0, "",
     '{"v":1,"field":"state","access":"read","line":8,"scope_node_id":33}'),
    # tsCaller calls tsSpread at line 19 — variadic binding hop.
    (35, 34, "CALLS", 19, "app.ts", "same_file", "CERTIFIED", 1.0, ""),
    # v2 access site: receiver persisted, no field_read prop — binds only
    # through the persisted-receiver path (owner Repo via param r: Repo).
    (36, 36, "READS", 23, "app.ts", "field_read", "CERTIFIED", 1.0, "",
     '{"v":2,"field":"state","access":"read","line":23,"scope_node_id":36,'
     '"receiver":"r"}'),
]

# Persisted CFG rows mirroring what gt-index emits for the fixtures above.
# (node_id, block_index, kind, start_line, end_line, statement_lines)
_CFG_BLOCKS = [
    # gofn: entry -> block(a := 0; if x > 0) -> if_then/if_else -> merge -> exit
    (21, 0, "entry", 3, 3, "[3]"),
    (21, 1, "exit", None, None, "[]"),
    (21, 2, "block", 4, 10, "[4, 5]"),
    (21, 3, "if_then", 6, 7, "[6, 7]"),
    (21, 4, "if_else", 9, 9, "[9]"),
    (21, 5, "block", 11, 11, "[11]"),
    # tsfn: entry -> block(const q = ...; return q) -> exit
    (25, 0, "entry", 1, 1, "[1]"),
    (25, 1, "exit", None, None, "[]"),
    (25, 2, "block", 2, 3, "[2, 3]"),
    # gocaller: entry -> block(return gofn(n)) -> exit
    (26, 0, "entry", 18, 18, "[18]"),
    (26, 1, "exit", None, None, "[]"),
    (26, 2, "block", 19, 19, "[19]"),
    # tsSpread: entry -> block(body) -> exit
    (34, 0, "entry", 12, 12, "[12]"),
    (34, 1, "exit", None, None, "[]"),
    (34, 2, "block", 13, 15, "[13, 14, 15]"),
    # tsCaller: entry -> block(return tsSpread(1,2,3)) -> exit
    (35, 0, "entry", 18, 18, "[18]"),
    (35, 1, "exit", None, None, "[]"),
    (35, 2, "block", 19, 19, "[19]"),
]

# (node_id, from_block, to_block, label)
_CFG_EDGES = [
    (21, 0, 2, "entry"),
    (21, 2, 3, "true"),
    (21, 2, 4, "false"),
    (21, 3, 5, ""),
    (21, 4, 5, ""),
    (21, 5, 1, "end"),
    (25, 0, 2, "entry"),
    (25, 2, 1, "end"),
    (26, 0, 2, "entry"),
    (26, 2, 1, "end"),
    (34, 0, 2, "entry"),
    (34, 2, 1, "end"),
    (35, 0, 2, "entry"),
    (35, 2, 1, "end"),
]

# (node_id, block_index, var_name, line)
_CFG_DEFS = [
    (21, 0, "x", 3),   # parameter
    (21, 2, "a", 4),   # a := 0
    (21, 3, "b", 6),   # b := 1
    (21, 4, "a", 9),   # a = 2
    (25, 0, "p", 1),   # parameter
    (25, 2, "q", 2),   # const q = p + 1
    (26, 0, "n", 18),  # parameter
    (34, 0, "first", 12),  # parameter
    (34, 0, "rest", 12),   # ...rest variadic parameter
    (34, 2, "total", 13),  # let total = first
]

# Persisted properties mirroring the producer's param / data_flow /
# field_read / side_effect kinds — consumed by signature-proven formal
# binding, receiver-bound taint, and supplemental use evidence.
# (node_id, kind, value, line)
_PROPERTIES = [
    (21, "param", "x:int [required]", 3),
    (25, "param", "p:number [required]", 1),
    (26, "param", "n:int [required]", 18),
    (33, "param", "r:Repo [required]", 7),
    (34, "param", "first:number [required]", 12),
    (34, "param", "...rest:number[] [required]", 12),
    (28, "side_effect", "mutates: self.state = v", 3),
    (29, "field_read", "reads: self.state", 6),
    (31, "field_read", "reads: self.state", 13),
    (33, "field_read", "reads: r.state", 8),
    (36, "param", "r:Repo [required]", 22),
    (26, "data_flow", "n -> gofn(n)", 19),
]

# Persisted cfg_uses rows (schema v15.3+) — parser-exact identifier reads,
# mirroring what the Go producer emits for the fixture functions.
# (node_id, block_index, var_name, line)
_CFG_USES = [
    (21, 2, "x", 5),         # if x > 0
    (21, 3, "println", 7),   # println(b)
    (21, 3, "b", 7),
    (21, 5, "a", 11),        # return a
    (25, 2, "p", 2),         # const q = p + 1
    (25, 2, "q", 3),         # return q
    (26, 2, "gofn", 19),     # return gofn(n)
    (26, 2, "n", 19),
    (34, 2, "first", 13),    # let total = first
    (34, 2, "rest", 14),     # for (const n of rest)
    (34, 2, "total", 14),    # total += n (aug LHS read)
    (34, 2, "n", 14),
    (34, 2, "total", 15),    # return total
    (35, 2, "tsSpread", 19), # return tsSpread(1, 2, 3)
]


def _build_graph(tmp_path: Path) -> Path:
    (tmp_path / "api.py").write_text(_API_SOURCE)
    (tmp_path / "calc.py").write_text(_CALC_SOURCE)
    (tmp_path / "main.go").write_text(_GO_SOURCE)
    (tmp_path / "app.ts").write_text(_TS_SOURCE)
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project_meta VALUES ('git_commit', ?)", (GRAPH_REVISION,))
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path,"
        " start_line, end_line, signature, return_type, is_exported, is_test,"
        " language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(n) + (None,) * (13 - len(n)) for n in _NODES],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line,"
        " source_file, resolution_method, trust_tier, confidence, metadata,"
        " access_sites) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [tuple(e) + ("",) * (10 - len(e)) for e in _EDGES],
    )
    conn.executemany(
        "INSERT INTO cfg_blocks (node_id, block_index, kind, start_line,"
        " end_line, statement_lines) VALUES (?,?,?,?,?,?)",
        _CFG_BLOCKS,
    )
    conn.executemany(
        "INSERT INTO cfg_edges (node_id, from_block, to_block, label)"
        " VALUES (?,?,?,?)",
        _CFG_EDGES,
    )
    conn.executemany(
        "INSERT INTO cfg_defs (node_id, block_index, var_name, line)"
        " VALUES (?,?,?,?)",
        _CFG_DEFS,
    )
    conn.executemany(
        "INSERT INTO properties (node_id, kind, value, line)"
        " VALUES (?,?,?,?)",
        _PROPERTIES,
    )
    conn.commit()
    conn.close()
    return db


def _build_graph_v15x(tmp_path: Path) -> Path:
    """The shared fixture migrated to the v15.4 substrate: cfg_uses rows +
    edges.actual_args populated, exactly what the new producer writes."""
    db = _build_graph(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE cfg_uses ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " node_id INTEGER NOT NULL,"
        " block_index INTEGER NOT NULL,"
        " var_name TEXT NOT NULL,"
        " line INTEGER)"
    )
    conn.executemany(
        "INSERT INTO cfg_uses (node_id, block_index, var_name, line)"
        " VALUES (?,?,?,?)",
        _CFG_USES,
    )
    conn.execute("ALTER TABLE edges ADD COLUMN actual_args TEXT")
    conn.execute(
        "UPDATE edges SET actual_args = ?"
        " WHERE source_id = 26 AND target_id = 21 AND source_line = 19",
        ('["n"]',),
    )
    conn.execute(
        "UPDATE edges SET actual_args = ?"
        " WHERE source_id = 35 AND target_id = 34 AND source_line = 19",
        ('["1","2","3"]',),
    )
    conn.commit()
    conn.close()
    return db


def _snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        schema=REPOSITORY_SNAPSHOT_SCHEMA,
        repository_id="fixture",
        root_sha256="a" * 64,
        git_revision=GRAPH_REVISION,
        dirty_diff_sha256="b" * 64,
        working_tree_sha256=WORKING_TREE,
        revisions=RevisionVector(
            repository_content=CONTENT_REVISION,
            graph=GRAPH_REVISION,
            lsp="lsp-rev",
            runtime_evidence="rt-rev",
        ),
        configuration=ConfigurationBinding(
            schema=CONFIGURATION_BINDING_SCHEMA,
            configuration_id="fixture-config",
            inputs_sha256="d" * 64,
            language_manifest_sha256="e" * 64,
            build_system="pytest",
        ),
    )


def _context(tmp_path: Path, graph_db: Path | None) -> DeterministicQueryContext:
    return DeterministicQueryContext(
        repository_root=tmp_path,
        graph_db=graph_db,
        repository_content_revision=CONTENT_REVISION,
        working_tree_sha256=WORKING_TREE,
        snapshot_files=(),
        snapshot_complete=True,
    )


def _request(kind: ActionKind, arguments: dict) -> ActionRequest:
    return ActionRequest.build(
        action_id="fixture-1",
        kind=kind,
        arguments=arguments,
        snapshot=_snapshot(),
        requested_fidelity=RequestedFidelity.EXACT,
    )


def _answer(artifact) -> dict:
    return json.loads(artifact.direct_answer_json)


# ---------------------------------------------------------------------------
# route_map
# ---------------------------------------------------------------------------


def test_route_map_surfaces_route_handler_and_consumers(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.ROUTE_MAP, {}), _context(tmp_path, db)
    )
    answer = _answer(artifact)
    assert answer["route_count"] == 1
    route = answer["routes"][0]
    assert route["route"] == "/api/items"
    assert route["method"] == "GET"
    assert route["handler"] == "list_items"
    assert route["handler_file"] == "api.py"
    assert route["discovered_via"] == "handles_route"
    assert route["consumers"][0]["file"] == "client.py"
    assert route["consumers"][0]["attribution"] == "route_level"
    assert route["downstream_calls"][0]["symbol"] == "helper"


def test_route_map_no_routes_is_honest(tmp_path):
    db = _build_graph(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM edges WHERE type IN ('HANDLES_ROUTE','API_CALL')")
    conn.commit()
    conn.close()
    artifact = execute_query(
        _request(ActionKind.ROUTE_MAP, {}), _context(tmp_path, db)
    )
    answer = _answer(artifact)
    assert answer["route_count"] == 0


def test_route_map_missing_graph(tmp_path):
    artifact = execute_query(
        _request(ActionKind.ROUTE_MAP, {}), _context(tmp_path, None)
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


# ---------------------------------------------------------------------------
# api_impact
# ---------------------------------------------------------------------------


def test_api_impact_consumers_and_affected_files(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.API_IMPACT, {"route": "/api/items"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["route_count"] == 1
    route = answer["routes"][0]
    assert route["consumer_count"] == 1
    assert "client.py" in route["affected_files"]


def test_api_impact_unknown_route_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.API_IMPACT, {"route": "/api/nope"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["route_count"] == 0
    assert "route_not_found" in artifact.omissions


# ---------------------------------------------------------------------------
# taint
# ---------------------------------------------------------------------------


def test_taint_finds_source_to_sink_path(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "get_input", "sink": "execute"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "statement_level_dataflow_unavailable" in artifact.omissions
    answer = _answer(artifact)
    assert answer["paths_found"] == 1
    assert answer["paths"][0]["path"] == ["get_input", "process", "execute"]
    # Per-hop evidence: the CALLS edge lines are real and reported.
    hops = answer["paths"][0]["hop_detail"]
    assert hops[0]["call_line"] == 4
    assert hops[1]["call_line"] == 10


def test_taint_surfaces_field_mediated_channel(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "get_input", "sink": "execute"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    channel = [c for c in answer["field_channels"] if c["field"] == "payload"]
    assert len(channel) == 1
    assert channel[0]["writer"]["symbol"] == "get_input"
    assert channel[0]["reader"]["symbol"] == "execute"
    assert "field_flow_name_matched" in artifact.omissions


def test_taint_heuristic_sinks_when_unspecified(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "get_input"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert "execute" in answer["heuristic_sinks_reached"]
    assert "sink_pattern_heuristic" in artifact.omissions


def test_taint_unresolved_source_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "nonexistent"}),
        _context(tmp_path, db),
    )
    assert "symbol_not_found" in artifact.omissions
    assert _answer(artifact)["paths_found"] == 0


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_enumerates_graph_edit_sites(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.RENAME, {"symbol": "helper", "new_name": "util"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "text_references_not_enumerated" in artifact.omissions
    answer = _answer(artifact)
    assert answer["definitions"][0]["file_path"] == "api.py"
    assert "CALLS" in answer["edit_sites_by_type"]
    callers = answer["edit_sites_by_type"]["CALLS"]
    assert callers[0]["referencing_symbol"] == "list_items"


def test_rename_unresolved_symbol(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.RENAME, {"symbol": "ghost"}), _context(tmp_path, db)
    )
    assert "symbol_not_found" in artifact.omissions


# ---------------------------------------------------------------------------
# shape_check
# ---------------------------------------------------------------------------


def test_shape_check_flags_missing_interface_method(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SHAPE_CHECK, {"symbol": "Dog"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    conformance = [c for c in answer["checks"] if c["check"] == "interface_conformance"]
    assert conformance[0]["status"] == "fail"
    assert conformance[0]["missing_methods"] == ["eat"]


def test_shape_check_flags_override_arity(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SHAPE_CHECK, {"symbol": "run", "path": "sub.py"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    arity = [c for c in answer["checks"] if c["check"] == "override_arity"]
    assert arity[0]["status"] == "fail"


def test_shape_check_no_contracts_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SHAPE_CHECK, {"symbol": "helper"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "no_verifiable_contracts" in artifact.omissions


# ---------------------------------------------------------------------------
# tool_map
# ---------------------------------------------------------------------------


def test_tool_map_detects_decorated_tool(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TOOL_MAP, {}), _context(tmp_path, db)
    )
    answer = _answer(artifact)
    assert answer["tool_count"] == 1
    assert answer["tools"][0]["tool"] == "search_docs"
    assert answer["tools"][0]["decorator"] == "tool"
    assert "registration_sites_untracked" in artifact.omissions


def test_tool_map_empty_is_honest(tmp_path):
    db = _build_graph(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM edges WHERE type='DECORATES'")
    conn.commit()
    conn.close()
    artifact = execute_query(
        _request(ActionKind.TOOL_MAP, {}), _context(tmp_path, db)
    )
    assert "no_tools_detected" in artifact.omissions
    assert _answer(artifact)["tool_count"] == 0


# ---------------------------------------------------------------------------
# slice — statement-level backward/forward over the real CFG substrate
# ---------------------------------------------------------------------------


def test_slice_backward_includes_data_and_control_deps(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "calc_total", "line": 6, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["slice_count"] == 1
    sl = answer["slices"][0]
    assert sl["function"] == "calc_total"
    # `total` at line 6 depends on `subtotal` (def at 2, updated at 4 under
    # the loop control dep at 3) and `tax` (param def at the signature).
    assert {1, 2, 3, 4, 5, 6} <= set(sl["slice_lines"])


def test_slice_forward_from_assignment(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "calc_total", "line": 2, "direction": "forward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    # `subtotal = 0` reaches the += at 4 (under the loop at 3) and the
    # multiplication at 5 feeding the return at 6.
    assert {4, 5, 6} <= set(sl["slice_lines"])


def test_slice_variables_narrows_criterion(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "calc_total", "line": 6, "direction": "backward",
             "variables": ["tax"]},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    # Only the tax chain: def site (line 1 param) + use at line 5 + line 6.
    assert set(sl["slice_lines"]) <= {1, 5, 6}
    assert 4 not in sl["slice_lines"]


def test_slice_unsupported_language_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SLICE, {"symbol": "rustfn", "line": 2}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "unsupported_language:rust" in artifact.omissions
    assert _answer(artifact)["slice_count"] == 0


# ---------------------------------------------------------------------------
# slice via the persisted cfg_* sidecar (cfg_store substrate)
# ---------------------------------------------------------------------------


def test_slice_go_backward_via_persisted_cfg(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gofn", "line": 11, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["slice_count"] == 1
    sl = answer["slices"][0]
    assert sl["substrate"] == "persisted_cfg"
    # `return a` at 11 reaches defs a@4 (a := 0) and a@9 (a = 2 in the else
    # arm); the else block's control dep pulls in the whole predicate block
    # (lines 4 + the `if x > 0` header at 5); the header's x use reaches the
    # param def at the signature line 3.  Then-branch statements 6-7 do not
    # feed the criterion and stay out.
    assert sl["slice_lines"] == [3, 4, 5, 9, 11]


def test_slice_go_control_dependence_scopes_branch(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gofn", "line": 7, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    # `println(b)` at 7: data dep on b@6, control dep on the `if` header at
    # 5 — and crucially the else arm (line 9) and merge use (line 11) are
    # NOT pulled into a then-branch slice.
    assert {4, 5, 6, 7} <= set(sl["slice_lines"])
    assert 9 not in sl["slice_lines"]
    assert 11 not in sl["slice_lines"]


def test_slice_go_forward_via_persisted_cfg(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gofn", "line": 4, "direction": "forward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    # From `a := 0`: its def reaches the use at 11, and everything control-
    # dependent on the predicate block enters (both if arms).
    assert 11 in sl["slice_lines"]
    assert {6, 7, 9} <= set(sl["slice_lines"])


def test_slice_typescript_via_persisted_cfg(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "tsfn", "line": 3, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    # `return q` <- q@2 (`const q = p + 1`) <- p@1 (param def).
    assert sl["slice_lines"] == [1, 2, 3]


def test_slice_persisted_cfg_reports_approximation(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gofn", "line": 11, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    # Uses are text-scanned, not parsed: the approximation is surfaced in
    # the slice and the artifact stays INCOMPLETE.
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    sl = _answer(artifact)["slices"][0]
    assert "approximate_use_detection" in sl["limitations"]
    assert "slice_limitations_present" in artifact.omissions


def test_slice_no_persisted_cfg_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SLICE, {"symbol": "nocfg", "line": 15}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "no_persisted_cfg" in artifact.omissions
    assert _answer(artifact)["slice_count"] == 0


def test_slice_line_outside_function_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SLICE, {"symbol": "calc_total", "line": 40}),
        _context(tmp_path, db),
    )
    assert "line_outside_function:calc_total" in artifact.omissions


def test_patch_impact_emits_statement_slices(tmp_path):
    db = _build_graph(tmp_path)
    before = _CALC_SOURCE
    after = _CALC_SOURCE.replace(
        "    total = subtotal * (1 + tax)",
        "    total = subtotal * (1 + tax) * 2",
    )
    artifact = execute_query(
        _request(
            ActionKind.PATCH_IMPACT,
            {"edited_files": {"calc.py": {"before": before, "after": after}}},
        ),
        _context(tmp_path, db),
    )
    slices = _answer(artifact)["impact"]["statement_slices"]
    assert len(slices) == 1
    assert slices[0]["function"] == "calc_total"
    assert slices[0]["line"] == 5
    # The edit at line 5 flows into the return at line 6.
    assert 6 in slices[0]["affected_lines"]


def test_slice_interprocedural_composes_through_calls_edge(tmp_path):
    """interprocedural=True composes the slice through the graph's CALLS
    edges: report's criterion pulls in calc_total's return slice."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "report", "line": 11, "direction": "backward",
             "interprocedural": True},
        ),
        _context(tmp_path, db),
    )
    # Name/positional argument mapping is never exact.
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "interprocedural_name_matched" in artifact.omissions
    sl = _answer(artifact)["slices"][0]
    assert sl["interprocedural"] is True
    (hop,) = sl["cross_function"]
    assert hop["caller_fn"] == "report" and hop["callee_fn"] == "calc_total"
    assert hop["call_line"] == 10
    # Positional + literal actuals bound to calc_total's formals.
    assert hop["mapped_vars"] == {"items": "items", "tax": "0.2"}
    merged = set(sl["per_file"]["calc.py"])
    assert {9, 10, 11} <= merged       # caller slice (def line, call, return)
    assert {1, 2, 3, 4, 5, 6} <= merged  # calc_total's return slice merged


def test_slice_intraprocedural_default_unchanged(tmp_path):
    """Without interprocedural the slice still stops at the call site."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "report", "line": 11, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    assert "cross_function" not in sl
    assert set(sl["slice_lines"]) <= {9, 10, 11}
    assert [c["name"] for c in sl["call_sites"]] == ["calc_total"]


def test_slice_interprocedural_persisted_cfg_composes(tmp_path):
    """The persisted-CFG path composes through CALLS edges too:
    gocaller's criterion pulls in gofn's return slice and maps the
    formal x back to the caller's actual n."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gocaller", "line": 19, "direction": "backward",
             "interprocedural": True},
        ),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    sl = _answer(artifact)["slices"][0]
    assert sl["interprocedural"] is True
    assert "interprocedural_unavailable:go" not in (
        sl.get("limitations") or []
    )
    (hop,) = sl["cross_function"]
    assert hop["caller_fn"] == "gocaller" and hop["callee_fn"] == "gofn"
    assert hop["call_line"] == 19
    # gofn's sole formal x is reached by the return slice; the actual is n.
    assert hop["mapped_vars"] == {"x": "n"}
    assert hop["reached_formals"] == ["x"]
    merged = set(sl["per_file"]["main.go"])
    assert {18, 19} <= merged          # caller: param n + the call line
    assert 11 in merged                # callee gofn's `return a`


def test_slice_interprocedural_persisted_cfg_bounded(tmp_path):
    """The persisted composer honours the hop budget."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gocaller", "line": 19, "direction": "backward",
             "interprocedural": True, "max_hops": 0},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    assert sl.get("cross_function") in (None, [])
    assert any(
        lim.startswith("hop_budget") for lim in (sl.get("limitations") or [])
    )


def test_taint_field_channel_owner_bound_and_mismatch_excluded(tmp_path):
    """Same-named fields on different classes never join: Repo.set ->
    Repo.get and Repo.set -> Svc.use (receiver r: Repo via param type)
    form owner_bound channels; Other.peek (different owner) reading the
    same field name must not appear."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "set"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    channels = [
        c for c in answer["field_channels"] if c["field"] == "state"
    ]
    readers = {c["reader"]["symbol"] for c in channels}
    assert readers == {"get", "use", "check"}
    for ch in channels:
        assert ch["identity"] == "owner_bound"
        assert ch["owner"] == "db.Repo"
        assert ch["writer"]["symbol"] == "set"


def test_taint_named_receiver_binds_via_param_annotation(tmp_path):
    """r.state where param r: Repo — the receiver name resolves through
    the persisted param annotation, so the channel is owner_bound to
    Repo, not owner_unknown and not enclosing-class Svc."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "set"}),
        _context(tmp_path, db),
    )
    channels = [
        c
        for c in _answer(artifact)["field_channels"]
        if c["field"] == "state" and c["reader"]["symbol"] == "use"
    ]
    (ch,) = channels
    assert ch["identity"] == "owner_bound"
    assert ch["owner"] == "db.Repo"


def test_taint_persisted_receiver_binds_without_prop(tmp_path):
    """access_sites v2 carries ``receiver`` — check(r: Repo) has NO
    field_read prop, so only the persisted receiver can resolve its owner.
    Without it the owner falls back to unresolved (owner_unknown)."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.TAINT, {"source": "set"}),
        _context(tmp_path, db),
    )
    channels = [
        c
        for c in _answer(artifact)["field_channels"]
        if c["field"] == "state" and c["reader"]["symbol"] == "check"
    ]
    (ch,) = channels
    assert ch["identity"] == "owner_bound"
    assert ch["owner"] == "db.Repo"


def test_slice_interprocedural_persisted_cfg_variadic_binding(tmp_path):
    """Signature-proven formals: tsSpread's ``...rest`` absorbs the extra
    positionals — mapped_vars carries first/rest with no arity flag."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "tsCaller", "line": 19, "direction": "backward",
             "interprocedural": True},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    (hop,) = sl["cross_function"]
    assert hop["callee_fn"] == "tsSpread"
    assert hop["mapped_vars"] == {"first": "1", "rest": "2,3"}
    assert not any(
        lim.startswith("arity_mismatch")
        for lim in (sl.get("limitations") or [])
    )


# ---------------------------------------------------------------------------
# signature-proven formal/actual binding — unit level
# (kwargs/spreads never occur in go/java/ts/js source, so these exercise
# _bind_actuals / _param_properties / _split_actuals directly)
# ---------------------------------------------------------------------------


def _props_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " node_id INT, kind TEXT, value TEXT, line INT)"
    )
    conn.executemany(
        "INSERT INTO properties (node_id, kind, value, line)"
        " VALUES (?,?,?,?)",
        rows,
    )
    return conn


def test_param_properties_parses_kinds_and_order():
    from groundtruth.runtime.cfg_store import _param_properties

    conn = _props_conn(
        [
            (1, "param", "cb:func() string [required]", 2),
            (1, "param", "db opt=Depends(get_db)", 2),
            (1, "param", "...rest:number[] [required]", 2),
            (1, "param", "**opts [required]", 2),
            (2, "param", "args:...int [required]", 2),
        ]
    )
    props = _param_properties(conn, 1)
    assert [p["name"] for p in props] == ["cb", "db", "rest", "opts"]
    assert [p["kind"] for p in props] == [
        "required",
        "optional",
        "variadic",
        "kw_variadic",
    ]
    assert props[0]["type"] == "func() string"
    assert props[1]["default"] == "Depends(get_db)"
    go_props = _param_properties(conn, 2)
    assert go_props[0]["name"] == "args"
    assert go_props[0]["kind"] == "variadic"


def test_bind_actuals_keyword_positional_and_variadic():
    from groundtruth.runtime.cfg_store import _bind_actuals

    formals = [
        {"name": "a", "kind": "required"},
        {"name": "b", "kind": "required"},
        {"name": "c", "kind": "optional"},
    ]
    bound, reasons = _bind_actuals(formals, ["1", "b=2"])
    assert bound == {"a": "1", "b": "2"}
    assert reasons == []

    bound, reasons = _bind_actuals(formals, ["1", "zzz=9"])
    assert "unknown_kw:zzz" in reasons

    bound, reasons = _bind_actuals(formals, ["1", "2", "3", "4"])
    assert "arity_mismatch" in reasons

    formals_var = formals + [{"name": "rest", "kind": "variadic"}]
    bound, reasons = _bind_actuals(formals_var, ["1", "2", "3", "4", "5"])
    assert bound["rest"] == "4,5"
    assert reasons == []

    formals_kw = formals + [{"name": "kw", "kind": "kw_variadic"}]
    bound, reasons = _bind_actuals(formals_kw, ["1", "x=7", "y=8"])
    assert bound["kw"] == "x=7,y=8"
    assert reasons == []

    bound, reasons = _bind_actuals(formals_var, ["*seq", "1"])
    assert bound["rest"] == "*seq,1" or bound["rest"] == "*seq"


def test_split_actuals_prefers_caller_formal_on_multicall_line():
    from groundtruth.runtime.cfg_store import _split_actuals

    # Resolved callee is `goHelper` but the text calls the caller's formal
    # `cb` — the callable-value fallback must pick cb(), not helper().
    clean = ["\tres := helper(); cb(arg1)", ""]
    actuals = _split_actuals("goHelper", 1, clean, prefer_names=frozenset({"cb"}))
    assert actuals == ["arg1"]

    # Without the preference the first call on the line wins — helper()
    # carries no args, so the mapping degrades honestly to empty.
    actuals = _split_actuals("goHelper", 1, clean)
    assert actuals == []


def test_split_actuals_comparison_is_not_keyword():
    from groundtruth.runtime.cfg_store import _split_actuals

    clean = ["\tif check(x == y, flag) {", ""]
    actuals = _split_actuals("check", 1, clean)
    assert actuals == ["x == y", "flag"]


# ---------------------------------------------------------------------------
# schema v15.4 substrate — persisted cfg_uses + actual_args + access receiver
# ---------------------------------------------------------------------------


def test_slice_persisted_cfg_uses_parser_exact(tmp_path):
    """With cfg_uses persisted, uses come from the producer's parser-exact
    rows — the limitation narrows from approximate_use_detection to
    approximate_use_coverage, and the slice itself is unchanged."""
    db = _build_graph_v15x(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gofn", "line": 11, "direction": "backward"},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    assert "approximate_use_coverage" in sl["limitations"]
    assert "approximate_use_detection" not in sl["limitations"]
    # The slice is the same set the lexical path found: return a pulls in
    # a's defs in both arms plus the if-condition line.
    merged = set(sl["slice_lines"])
    assert {4, 5, 9, 11} <= merged


def test_slice_interprocedural_actual_args_exact(tmp_path):
    """edges.actual_args carries the callsite's argument texts — the hop
    binds without re-splitting source text, so the extraction flag is
    gone while the mapping is identical."""
    db = _build_graph_v15x(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gocaller", "line": 19, "direction": "backward",
             "interprocedural": True},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    (hop,) = sl["cross_function"]
    assert hop["mapped_vars"] == {"x": "n"}
    assert "approximate_actual_extraction" not in (
        sl.get("limitations") or []
    )


def test_slice_interprocedural_text_fallback_flag(tmp_path):
    """On a pre-v15.4 graph (no actual_args column) the text-splitting
    fallback runs and stays honestly flagged."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.SLICE,
            {"symbol": "gocaller", "line": 19, "direction": "backward",
             "interprocedural": True},
        ),
        _context(tmp_path, db),
    )
    sl = _answer(artifact)["slices"][0]
    (hop,) = sl["cross_function"]
    assert hop["mapped_vars"] == {"x": "n"}
    assert "approximate_actual_extraction" in (sl.get("limitations") or [])
