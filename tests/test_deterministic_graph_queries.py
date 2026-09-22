"""Fixture contract tests for the graph-backed deterministic query producers.

Every test builds a real SQLite graph.db with the producer schema, issues an
``ActionRequest`` through ``execute_query``, and asserts on the returned
``EvidenceArtifact`` — answer payload, semantics, coverage, omissions, anchors,
and the interception decision. Nothing is certified by schema checks alone.
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
    InterceptionMode,
    RepositorySnapshot,
    RequestedFidelity,
    RevisionVector,
    evaluate_interception,
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
    confidence REAL DEFAULT 0.0
);
CREATE TABLE file_hashes (file_path TEXT PRIMARY KEY, content_hash TEXT);
"""

# (id, label, name, qualified, path, start, end, sig, ret, exported, is_test, lang)
_NODES = [
    (1, "Function", "main", "main", "main.py", 1, 40, "def main()", "", 1, 0, "python"),
    (2, "Function", "run_pipeline", "run_pipeline", "pkg/pipeline.py", 5, 30,
     "def run_pipeline(cfg)", "Result", 1, 0, "python"),
    (3, "Function", "helper", "pkg.util.helper", "pkg/util.py", 3, 12,
     "def helper(x)", "int", 1, 0, "python"),
    (4, "Function", "helper", "tests.helper", "tests/test_util.py", 8, 20,
     "def helper()", "", 0, 1, "python"),
    (5, "Function", "main", "main", "cmd/main.go", 10, 50, "func main()", "", 1, 0, "go"),
    (6, "Function", "DoThing", "internal.DoThing", "internal/x.go", 4, 22,
     "func DoThing() error", "error", 1, 0, "go"),
    (7, "Function", "render", "app.render", "web/app.ts", 12, 44,
     "function render(props)", "VNode", 1, 0, "typescript"),
    (8, "Function", "mount", "app.mount", "web/app.js", 2, 18, "function mount()", "",
     1, 0, "javascript"),
    (9, "Method", "processOrder", "Shop.processOrder", "Shop.java", 30, 60,
     "void processOrder()", "void", 1, 0, "java"),
    (10, "Function", "fetchData", "api.fetchData", "web/api.ts", 5, 25,
     "function fetchData(url)", "Promise", 1, 0, "typescript"),
    (11, "Function", "run", "engine.run", "src/engine.rs", 9, 33, "fn run()", "",
     1, 0, "rust"),
    (12, "Function", "emit", "pkg.util.emit", "pkg/util.py", 20, 30,
     "def emit(v)", "None", 0, 0, "python"),
]

# (src, dst, type, line, file, method, tier, confidence)
_EDGES = [
    (1, 2, "CALLS", 15, "main.py", "same_file", "CERTIFIED", 1.0),
    (2, 3, "CALLS", 9, "pkg/pipeline.py", "import", "CERTIFIED", 1.0),
    (5, 6, "CALLS", 20, "cmd/main.go", "same_file", "CERTIFIED", 1.0),
    (7, 8, "CALLS", 14, "web/app.ts", "import", "CERTIFIED", 1.0),
    (7, 10, "CALLS", 16, "web/app.ts", "import", "CERTIFIED", 1.0),
    (3, 12, "CALLS", 8, "pkg/util.py", "same_file", "CERTIFIED", 1.0),
    (1, 3, "IMPORTS", 2, "main.py", "import", "CERTIFIED", 1.0),
]


def _build_graph(tmp_path: Path, *, revision: str = GRAPH_REVISION) -> Path:
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project_meta VALUES ('source_revision', ?)", (revision,))
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path,"
        " start_line, end_line, signature, return_type, is_exported, is_test,"
        " language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        _NODES,
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line,"
        " source_file, resolution_method, trust_tier, confidence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        _EDGES,
    )
    conn.commit()
    conn.close()
    return db


def _snapshot(graph_revision: str = GRAPH_REVISION) -> RepositorySnapshot:
    return RepositorySnapshot(
        schema=REPOSITORY_SNAPSHOT_SCHEMA,
        repository_id="fixture",
        root_sha256="a" * 64,
        git_revision=graph_revision,
        dirty_diff_sha256="b" * 64,
        working_tree_sha256=WORKING_TREE,
        revisions=RevisionVector(
            repository_content=CONTENT_REVISION,
            graph=graph_revision,
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


def _context(
    tmp_path: Path,
    graph_db: Path | None,
    *,
    complete: bool = True,
    content_revision: str = CONTENT_REVISION,
) -> DeterministicQueryContext:
    return DeterministicQueryContext(
        repository_root=tmp_path,
        graph_db=graph_db,
        repository_content_revision=content_revision,
        working_tree_sha256=WORKING_TREE,
        snapshot_files=(),
        snapshot_complete=complete,
    )


def _request(kind: ActionKind, arguments: dict, snapshot=None) -> ActionRequest:
    return ActionRequest.build(
        action_id="fixture-1",
        kind=kind,
        arguments=arguments,
        snapshot=snapshot or _snapshot(),
        requested_fidelity=RequestedFidelity.EXACT,
    )


def _answer(artifact) -> dict:
    return json.loads(artifact.direct_answer_json)


# ---------------------------------------------------------------------------
# definition
# ---------------------------------------------------------------------------


def test_definition_exact_single_site(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "run_pipeline"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.EXACT
    assert artifact.coverage is Coverage.COMPLETE
    answer = _answer(artifact)
    assert answer["definition_count"] == 1
    site = answer["definitions"][0]
    assert site["file_path"] == "pkg/pipeline.py"
    assert site["start_line"] == 5
    assert site["signature"] == "def run_pipeline(cfg)"
    assert artifact.producer_revision == GRAPH_REVISION


def test_definition_prefers_non_test_node(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "helper"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    sites = answer["definitions"]
    assert sites[0]["file_path"] == "pkg/util.py"
    assert sites[0]["is_test"] is False
    assert any(d["file_path"] == "tests/test_util.py" for d in sites[1:])


def test_definition_qualified_name(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "Shop.processOrder"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["definitions"][0]["file_path"] == "Shop.java"
    assert answer["definitions"][0]["language"] == "java"


def test_definition_language_hint_narrows(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.DEFINITION, {"symbol": "main", "language": "go"}
        ),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["definitions"][0]["file_path"] == "cmd/main.go"


def test_definition_unresolved_symbol_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "nonexistent_xyz"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "symbol_not_found" in artifact.omissions


def test_definition_missing_graph_abstains(tmp_path):
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "main"}),
        _context(tmp_path, None),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "graph_unavailable" in artifact.omissions


def test_definition_stale_graph_revision_omitted(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "main"},
                 snapshot=_snapshot(graph_revision="different-rev")),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "graph_revision_mismatch" in artifact.omissions


def test_definition_incomplete_snapshot_authority(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "main"}),
        _context(tmp_path, db, complete=False),
    )
    assert "snapshot_authority_unavailable" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


# ---------------------------------------------------------------------------
# references / callers
# ---------------------------------------------------------------------------


def test_references_grouped_by_edge_type(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.REFERENCES, {"symbol": "helper"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    grouped = answer["references_by_type"]
    assert "CALLS" in grouped
    assert "IMPORTS" in grouped
    caller_names = {r["name"] for r in grouped["CALLS"]}
    assert "run_pipeline" in caller_names
    assert "main" in {r["name"] for r in grouped["IMPORTS"]}


def test_callers_depth_banded(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "helper"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    bands = answer["callers_by_depth"]
    assert {c["name"] for c in bands["1"]} == {"run_pipeline"}
    assert "main" in {c["name"] for c in bands["2"]}


def test_callers_depth_limit(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "helper", "depth": 1}),
        _context(tmp_path, db),
    )
    bands = _answer(artifact)["callers_by_depth"]
    assert set(bands) == {"1"}


def test_callers_unresolved_symbol_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "ghost_fn"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "symbol_not_found" in artifact.omissions


# ---------------------------------------------------------------------------
# symbol_context
# ---------------------------------------------------------------------------


def test_symbol_context_composite(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SYMBOL_CONTEXT, {"symbol": "render"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["definition"]["file_path"] == "web/app.ts"
    callee_names = {c["name"] for c in answer["callees"]}
    assert {"mount", "fetchData"} <= callee_names
    assert "signature" in answer["definition"]


def test_symbol_context_unresolved_abstains(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.SYMBOL_CONTEXT, {"symbol": "nope"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


def test_processes_reports_library_stats(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.PROCESSES, {}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert "process_count" in answer
    assert "processes" in answer
    for proc in answer["processes"]:
        assert proc["step_count"] == len(proc["steps"]) - 1
        assert proc["steps"][0] == proc["entry"]
        assert proc["steps"][-1] == proc["terminal"]


def test_processes_concept_filter(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.PROCESSES, {"concept": "run_pipeline"}),
        _context(tmp_path, db),
    )
    answer = _answer(artifact)
    assert answer["matched"] >= 1
    assert any("run_pipeline" in s for p in answer["processes"] for s in p["steps"])


def test_processes_limit_bounds_output(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.PROCESSES, {"limit": 1}),
        _context(tmp_path, db),
    )
    assert len(_answer(artifact)["processes"]) <= 1


def test_processes_missing_graph_abstains(tmp_path):
    artifact = execute_query(
        _request(ActionKind.PROCESSES, {}),
        _context(tmp_path, None),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "graph_unavailable" in artifact.omissions


# ---------------------------------------------------------------------------
# contract-level: arguments, decisions, determinism
# ---------------------------------------------------------------------------


def test_disallowed_argument_rejected(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "main", "injected": "x"}),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "unsupported_argument:injected" in artifact.omissions


def test_exact_artifact_decision_is_replace(tmp_path):
    db = _build_graph(tmp_path)
    request = _request(ActionKind.DEFINITION, {"symbol": "run_pipeline"})
    artifact = execute_query(request, _context(tmp_path, db))
    decision = evaluate_interception(request, (artifact,))
    assert decision.mode is InterceptionMode.REPLACE


def test_incomplete_artifact_decision_is_not_replace(tmp_path):
    db = _build_graph(tmp_path)
    request = _request(ActionKind.DEFINITION, {"symbol": "ghost"})
    artifact = execute_query(request, _context(tmp_path, db))
    decision = evaluate_interception(request, (artifact,))
    assert decision.mode is not InterceptionMode.REPLACE


def test_artifact_is_deterministic(tmp_path):
    db = _build_graph(tmp_path)
    ctx = _context(tmp_path, db)
    a = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "helper"}), ctx
    )
    b = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "helper"}), ctx
    )
    assert a.direct_answer_json == b.direct_answer_json
    assert a.artifact_id == b.artifact_id


def test_patch_impact_maps_symbols_callers_flows(tmp_path):
    db = _build_graph(tmp_path)
    # Change lands inside helper's body (pkg/util.py lines 3-12).
    before = "\n".join(f"# line {i}" for i in range(1, 31)) + "\n"
    after = before.replace("# line 8", "x = helper(1)  # changed")
    artifact = execute_query(
        _request(
            ActionKind.PATCH_IMPACT,
            {"edited_files": {"pkg/util.py": {"before": before, "after": after}}},
        ),
        _context(tmp_path, db),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    answer = _answer(artifact)
    impact = answer["impact"]
    changed = {s["name"] for s in impact["changed_symbols"]}
    assert "helper" in changed
    bands = impact["callers_by_depth"]
    assert {c["name"] for c in bands["1"]} == {"run_pipeline"}
    assert "main" in {c["name"] for c in bands["2"]}
    assert impact["affected_flows"], "flow through main->run_pipeline->helper"
    assert artifact.producer_revision == GRAPH_REVISION


def test_patch_impact_missing_graph_names_omission(tmp_path):
    artifact = execute_query(
        _request(
            ActionKind.PATCH_IMPACT,
            {"edited_files": {"pkg/util.py": {"before": "a\n", "after": "b\n"}}},
        ),
        _context(tmp_path, None),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert "graph_unavailable" in artifact.omissions


def test_patch_impact_unmapped_file_named(tmp_path):
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(
            ActionKind.PATCH_IMPACT,
            {"edited_files": {"docs/README.md": {"before": "a\n", "after": "b\n"}}},
        ),
        _context(tmp_path, db),
    )
    assert "changed_symbols_unmapped:docs/README.md" in artifact.omissions


def test_all_certified_languages_represented(tmp_path):
    """The six certified languages each have at least one node whose
    definition resolves — the producer is language-agnostic over the graph."""
    db = _build_graph(tmp_path)
    ctx = _context(tmp_path, db)
    cases = {
        "python": "run_pipeline",
        "go": "DoThing",
        "typescript": "render",
        "javascript": "mount",
        "java": "Shop.processOrder",
        "rust": "run",
    }
    for language, symbol in cases.items():
        artifact = execute_query(
            _request(ActionKind.DEFINITION, {"symbol": symbol}), ctx
        )
        answer = _answer(artifact)
        assert answer["definitions"], language
        assert answer["definitions"][0]["language"] == language


def test_processes_label_keeps_lower_bound_marker(tmp_path):
    db = _build_graph(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE edges SET trust_tier='CANDIDATE', confidence=0.6"
        " WHERE source_id=2 AND target_id=3"
    )
    conn.commit()
    conn.close()
    artifact = execute_query(
        _request(ActionKind.PROCESSES, {"concept": "run_pipeline"}),
        _context(tmp_path, db),
    )
    labels = [p["label"] for p in _answer(artifact)["processes"]]
    assert labels and all(label.endswith("(lower bound)") for label in labels)
    ctx = execute_query(
        _request(ActionKind.SYMBOL_CONTEXT, {"symbol": "run_pipeline"}),
        _context(tmp_path, db),
    )
    flows = _answer(ctx)["flows"]
    assert flows and all(f.endswith("(lower bound)") for f in flows)


def test_graph_revision_ignores_build_commit(tmp_path):
    """Only project_meta.source_revision identifies the indexed source;
    git_commit is producer provenance and must never bind an answer."""
    db = _build_graph(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM project_meta WHERE key='source_revision'")
    conn.execute(
        "INSERT OR REPLACE INTO project_meta VALUES ('git_commit', ?)", (GRAPH_REVISION,)
    )
    conn.commit()
    conn.close()
    artifact = execute_query(
        _request(ActionKind.DEFINITION, {"symbol": "run_pipeline"}),
        _context(tmp_path, db),
    )
    assert "graph_revision_unavailable" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
