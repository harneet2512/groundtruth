"""Consumption tests for indexer edge types that were persisted but dark.

The Go indexer writes more edge kinds than the Python surfaces used to read:
``taxonomy.DeriveEdges`` (gt-index/internal/taxonomy/edges.go) persists
DECLARED_IMPLEMENTS, OVERRIDES, METHOD_OVERRIDES, DECORATES, RETURNS_TYPE,
PARAM_TYPE, ACCESSES and INJECTS; ``resolver/relationships.go`` persists
INJECTS and QUERIES; the resolution_v2 publication persists the fact links
HAS_CALLSITE, CANDIDATE_TARGET, SELECTED_TARGET, HAS_DERIVATION_FACT,
HAS_COMPLETENESS_FACT and HAS_UNRESOLVED_FACT.

These tests pin the lit behavior:

* ``graph_store._EDGE_TYPE_TO_REF`` maps every emitted kind to an honest ref
  kind (type_usage / data_flow / decorator / resolution_meta) instead of
  silently laundering it as a "call".
* ``deterministic_queries._references`` groups incoming edges by their raw
  persisted type — no edge-type whitelist — but only from source-symbol or
  file-anchor nodes: resolution-substrate rows (Callsite/*Fact) are not
  code references, so their bookkeeping links never appear.
* ``_symbol_context``/``_callers`` stay CALLS-only (a caller must be a call).
* ``localization_vnext`` traverses the real code relations
  (_SUPPORTED_RELATIONS, census ``trusted_edge_types``, ``_edge_evidence``)
  while resolver bookkeeping and never-emitted kinds stay unclaimed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from groundtruth.index.graph_store import _EDGE_TYPE_TO_REF, GraphStore
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
    EvidenceSemantics,
    RepositorySnapshot,
    RequestedFidelity,
    RevisionVector,
)
from groundtruth.utils.result import Ok

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
    confidence REAL,
    metadata TEXT
);
"""

# (id, label, name, qualified_name, file_path, start, end, signature,
#  return_type, is_exported, is_test, language, parent_id)
_NODES = [
    (1, "Function", "handler", "web.handler", "web/handlers.py", 5, 40,
     "def handler(svc)", "", 1, 0, "python", None),
    (2, "Class", "Service", "app.service.Service", "app/service.py", 1, 60,
     "class Service", "", 1, 0, "python", None),
    (3, "Class", "User", "app.models.User", "app/models.py", 1, 40,
     "class User", "", 1, 0, "python", None),
    (4, "Function", "login_required", "app.decorators.login_required",
     "app/decorators.py", 1, 20, "def login_required(fn)", "", 1, 0, "python", None),
    (5, "Function", "dashboard_view", "web.views.dashboard_view", "web/views.py",
     10, 30, "def dashboard_view(req)", "", 1, 0, "python", None),
    (6, "Method", "run", "app.service.Service.run", "app/service.py", 20, 35,
     "def run(self)", "", 1, 0, "python", 2),
    (7, "Method", "run", "app.base.Base.run", "app/base.py", 15, 25,
     "def run(self)", "", 1, 0, "python", 8),
    (8, "Class", "Base", "app.base.Base", "app/base.py", 1, 40,
     "class Base", "", 1, 0, "python", None),
    (9, "Class", "Child", "app.child.Child", "app/child.py", 5, 30,
     "class Child", "", 1, 0, "python", None),
    (10, "Interface", "Iface", "app.iface.Iface", "app/iface.py", 1, 20,
     "class Iface", "", 1, 0, "python", None),
    (11, "Function", "make_user", "app.factory.make_user", "app/factory.py",
     3, 10, "def make_user() -> User", "User", 1, 0, "python", None),
    (12, "Function", "find_user", "app.repo.find_user", "app/repo.py", 5, 15,
     "def find_user(u: User)", "", 1, 0, "python", None),
    # resolution_v2 synthetic nodes (labels the Go indexer actually writes).
    (13, "Callsite", "cs:a1", "", "app/service.py", 30, 30, "", "",
     0, 0, "python", None),
    (14, "DerivationFact", "df:a1", "", "app/service.py", 0, 0, "", "",
     0, 0, "python", None),
    (15, "CompletenessFact", "cf:a1", "", "app/service.py", 0, 0, "", "",
     0, 0, "python", None),
    (16, "Function", "caller_fn", "app.service.caller_fn", "app/service.py",
     45, 55, "def caller_fn()", "", 1, 0, "python", None),
    (17, "UnresolvedFact", "uf:a1", "", "app/service.py", 0, 0, "", "",
     0, 0, "python", None),
]

# (source_id, target_id, type, source_line, source_file, resolution_method,
#  trust_tier, confidence)
_EDGES = [
    # resolver/relationships.go — DI + ORM data access.
    (1, 2, "INJECTS", 8, "web/handlers.py", "depends_injection", "CANDIDATE", 0.85),
    (1, 3, "QUERIES", 12, "web/handlers.py", "orm_data_access", "CANDIDATE", 0.85),
    # resolver/framework_wiring.go — middleware wired onto a route handler.
    (4, 1, "MIDDLEWARE_ON", 9, "web/handlers.py", "django_middleware",
     "CERTIFIED", 0.9),
    # taxonomy.DeriveEdges kinds.
    (4, 5, "DECORATES", 9, "web/views.py", "syntactic_decorator_applied",
     "CANDIDATE", 0.9),
    (6, 7, "OVERRIDES", 20, "app/service.py", "syntactic_override_marker",
     "CANDIDATE", 0.9),
    (6, 7, "METHOD_OVERRIDES", 20, "app/service.py", "syntactic_override_marker",
     "CANDIDATE", 0.9),
    (6, 2, "ACCESSES", 25, "app/service.py", "syntactic_field_read",
     "CANDIDATE", 0.9),
    (9, 10, "DECLARED_IMPLEMENTS", 6, "app/child.py",
     "syntactic_implements_clause", "CANDIDATE", 0.9),
    (11, 3, "RETURNS_TYPE", 3, "app/factory.py", "syntactic_return_annotation",
     "CANDIDATE", 0.9),
    (12, 3, "PARAM_TYPE", 5, "app/repo.py", "syntactic_param_annotation",
     "CANDIDATE", 0.9),
    # resolution_v2 fact links — provenance between synthetic Callsite/fact
    # nodes. NULL confidence is part of the producer's contract.
    (16, 13, "HAS_CALLSITE", 48, "app/service.py", "graph_native",
     "STRUCTURAL", None),
    (13, 2, "CANDIDATE_TARGET", 30, "app/service.py", "resolver_candidate",
     "CANDIDATE", None),
    (13, 5, "SELECTED_TARGET", 30, "app/service.py", "import_binding",
     "CERTIFIED", None),
    (13, 14, "HAS_DERIVATION_FACT", 0, "app/service.py", None,
     "STRUCTURAL", None),
    (13, 15, "HAS_COMPLETENESS_FACT", 0, "app/service.py", None,
     "STRUCTURAL", None),
    (13, 17, "HAS_UNRESOLVED_FACT", 0, "app/service.py", None,
     "STRUCTURAL", None),
    # Ordinary call + a genuinely unemitted type for the negative assertions.
    (1, 5, "CALLS", 18, "web/handlers.py", "same_file", "CERTIFIED", 1.0),
    (7, 6, "TEST_CALLS", 16, "app/base.py", "same_file", "CERTIFIED", 1.0),
]

# Taxonomy + relationship edges the localizer now traverses (OVERRIDES was
# already supported; the rest were dark before).
_LIT_RELATIONS = {
    "DECLARED_IMPLEMENTS",
    "METHOD_OVERRIDES",
    "OVERRIDES",
    "DECORATES",
    "RETURNS_TYPE",
    "PARAM_TYPE",
    "ACCESSES",
    "INJECTS",
    "QUERIES",
    "MIDDLEWARE_ON",
}

# resolution_v2 bookkeeping — persisted, mapped to "resolution_meta" in
# graph_store, but deliberately NOT a traversable localization relation.
_FACT_LINKS = {
    "HAS_CALLSITE",
    "CANDIDATE",
    "CANDIDATE_TARGET",
    "SELECTED_TARGET",
    "HAS_DERIVATION_FACT",
    "HAS_COMPLETENESS_FACT",
    "HAS_UNRESOLVED_FACT",
}


def _build_graph(tmp_path: Path, *, revision: str = GRAPH_REVISION) -> Path:
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO project_meta VALUES ('source_revision', ?)", (revision,))
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path,"
        " start_line, end_line, signature, return_type, is_exported, is_test,"
        " language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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


def _store(db_path: Path) -> GraphStore:
    store = GraphStore(str(db_path))
    result = store.initialize()
    assert isinstance(result, Ok), f"GraphStore.initialize() failed: {result}"
    return store


# ---------------------------------------------------------------------------
# graph_store: edge type -> ref kind mapping
# ---------------------------------------------------------------------------


def test_edge_type_map_covers_lit_relations():
    """Every emitted code-relation kind maps to an honest ref kind."""
    expected = {
        "DECLARED_IMPLEMENTS": "type_usage",
        "OVERRIDES": "type_usage",
        "METHOD_OVERRIDES": "type_usage",
        "RETURNS_TYPE": "type_usage",
        "PARAM_TYPE": "type_usage",
        "DECORATES": "decorator",
        "ACCESSES": "data_flow",
        "INJECTS": "type_usage",
        "QUERIES": "data_flow",
        "MIDDLEWARE_ON": "decorator",
    }
    for edge_type, ref_kind in expected.items():
        assert _EDGE_TYPE_TO_REF.get(edge_type) == ref_kind, (
            f"{edge_type} must map to {ref_kind!r}, got "
            f"{_EDGE_TYPE_TO_REF.get(edge_type)!r}"
        )


def test_edge_type_map_labels_fact_links_as_resolution_meta():
    """Fact links are bookkeeping — mapped so they stop reading as "call"."""
    for edge_type in _FACT_LINKS:
        assert _EDGE_TYPE_TO_REF.get(edge_type) == "resolution_meta", edge_type


def test_refs_for_symbol_surfaces_dark_types(tmp_path):
    """get_refs_for_symbol returns the persisted edges with honest ref kinds."""
    db = _build_graph(tmp_path)
    store = _store(db)

    refs = store.get_refs_for_symbol(2)  # Service
    assert isinstance(refs, Ok)
    by_type = {r.reference_type for r in refs.value}
    # INJECTS -> type_usage, ACCESSES -> data_flow, CANDIDATE_TARGET -> meta.
    assert {"type_usage", "data_flow", "resolution_meta"} <= by_type

    refs = store.get_refs_for_symbol(3)  # User
    assert isinstance(refs, Ok)
    # QUERIES -> data_flow; RETURNS_TYPE/PARAM_TYPE -> type_usage.
    assert {r.reference_type for r in refs.value} == {"data_flow", "type_usage"}

    refs = store.get_refs_for_symbol(5)  # dashboard_view
    assert isinstance(refs, Ok)
    # DECORATES -> decorator, SELECTED_TARGET -> resolution_meta, CALLS -> call.
    assert {r.reference_type for r in refs.value} == {
        "decorator",
        "resolution_meta",
        "call",
    }

    refs = store.get_refs_for_symbol(7)  # Base.run
    assert isinstance(refs, Ok)
    # OVERRIDES + METHOD_OVERRIDES -> type_usage.
    assert {r.reference_type for r in refs.value} == {"type_usage"}


def test_refs_for_symbol_confidence_gate_keeps_lit_edges(tmp_path):
    """The 0.5 confidence floor admits CANDIDATE-tier dark edges (0.85/0.9)."""
    db = _build_graph(tmp_path)
    store = _store(db)
    refs = store.get_refs_for_symbol(2, min_confidence=0.5)
    assert isinstance(refs, Ok)
    kinds = {r.reference_type for r in refs.value}
    assert "type_usage" in kinds  # INJECTS @0.85
    assert "data_flow" in kinds  # ACCESSES @0.9
    # CANDIDATE_TARGET carries NULL confidence by contract -> gated out.
    assert "resolution_meta" not in kinds


def test_get_refs_from_file_reverse_maps_new_ref_kinds(tmp_path):
    """reference_type filters reverse-map to the Go edge types."""
    db = _build_graph(tmp_path)
    store = _store(db)

    refs = store.get_refs_from_file("web/handlers.py", "data_flow")
    assert isinstance(refs, Ok)
    assert [r.symbol_id for r in refs.value] == [3]  # QUERIES -> User
    assert refs.value[0].reference_type == "data_flow"

    refs = store.get_refs_from_file("web/handlers.py", "type_usage")
    assert isinstance(refs, Ok)
    assert [r.symbol_id for r in refs.value] == [2]  # INJECTS -> Service

    refs = store.get_refs_from_file("app/service.py", "resolution_meta")
    assert isinstance(refs, Ok)
    # HAS_CALLSITE + CANDIDATE_TARGET + SELECTED_TARGET + 3 fact links.
    assert len(refs.value) == 6
    assert {r.reference_type for r in refs.value} == {"resolution_meta"}

    refs = store.get_refs_from_file("web/handlers.py", "call")
    assert isinstance(refs, Ok)
    assert [r.symbol_id for r in refs.value] == [5]  # CALLS only


# ---------------------------------------------------------------------------
# deterministic_queries: _references groups every persisted type; callers
# stay CALLS-only by contract.
# ---------------------------------------------------------------------------


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


def test_references_groups_all_persisted_edge_types(tmp_path):
    """_references has no type whitelist: every persisted kind is a group key."""
    db = _build_graph(tmp_path)
    ctx = _context(tmp_path, db)

    artifact = execute_query(
        _request(ActionKind.REFERENCES, {"symbol": "User"}), ctx
    )
    assert artifact.semantics is EvidenceSemantics.EXACT
    by_type = _answer(artifact)["references_by_type"]
    assert {"QUERIES", "RETURNS_TYPE", "PARAM_TYPE"} <= set(by_type)
    assert {r["name"] for r in by_type["QUERIES"]} == {"handler"}

    artifact = execute_query(
        _request(ActionKind.REFERENCES, {"symbol": "Service"}), ctx
    )
    by_type = _answer(artifact)["references_by_type"]
    assert {"INJECTS", "ACCESSES"} <= set(by_type)
    # Resolution-substrate bookkeeping (Callsite -> CANDIDATE_TARGET) is not
    # a code reference: its source is an analysis-layer node, not a symbol.
    assert "CANDIDATE_TARGET" not in by_type

    artifact = execute_query(
        _request(ActionKind.REFERENCES, {"symbol": "dashboard_view"}), ctx
    )
    by_type = _answer(artifact)["references_by_type"]
    assert {"CALLS", "DECORATES"} <= set(by_type)
    assert "SELECTED_TARGET" not in by_type


def test_symbol_context_callers_stay_calls_only(tmp_path):
    """callers/callees are CALLS-scoped — typed edges must NOT widen them."""
    db = _build_graph(tmp_path)
    ctx = _context(tmp_path, db)

    artifact = execute_query(
        _request(ActionKind.SYMBOL_CONTEXT, {"symbol": "dashboard_view"}), ctx
    )
    answer = _answer(artifact)
    # DECORATES (login_required) and SELECTED_TARGET (callsite) are not callers.
    assert {c["name"] for c in answer["callers"]} == {"handler"}

    artifact = execute_query(
        _request(ActionKind.SYMBOL_CONTEXT, {"symbol": "handler"}), ctx
    )
    answer = _answer(artifact)
    # INJECTS->Service and QUERIES->User are not callees.
    assert {c["name"] for c in answer["callees"]} == {"dashboard_view"}


def test_callers_ignore_dark_edge_types(tmp_path):
    """_callers BFS follows CALLS only — an INJECTS source is not a caller."""
    db = _build_graph(tmp_path)
    artifact = execute_query(
        _request(ActionKind.CALLERS, {"symbol": "Service"}), _context(tmp_path, db)
    )
    answer = _answer(artifact)
    assert answer["caller_count"] == 0  # no CALLS edge targets Service


# ---------------------------------------------------------------------------
# localization_vnext engine: supported relations, census, traversal
# ---------------------------------------------------------------------------


def _engine_request(tmp_path: Path, db: Path, issue_text: str):
    from groundtruth.pretask.localization_vnext.model import LocalizationRequest

    return LocalizationRequest(
        issue_text=issue_text,
        repository_root=str(tmp_path),
        graph_db=str(db),
        revision_identity="fixture",
    )


def test_supported_relations_cover_lit_types():
    from groundtruth.pretask.localization_vnext import engine

    assert _LIT_RELATIONS <= engine._SUPPORTED_RELATIONS


def test_fact_links_and_unemitted_types_not_supported():
    """Bookkeeping edges and never-emitted kinds stay dark in the localizer."""
    from groundtruth.pretask.localization_vnext import engine

    assert not _FACT_LINKS & engine._SUPPORTED_RELATIONS
    # INSTANTIATES is in the Go taxonomy vocabulary but is never emitted;
    # TEST_CALLS exists only in Go-side test fixtures.
    assert "INSTANTIATES" not in engine._SUPPORTED_RELATIONS
    assert "TEST_CALLS" not in engine._SUPPORTED_RELATIONS
    assert "TEST_CALLS" not in _EDGE_TYPE_TO_REF


def test_census_reports_lit_edge_types(tmp_path):
    """trusted_edge_types lists persisted kinds the localizer can traverse."""
    from groundtruth.pretask.localization_vnext.engine import census_capabilities

    db = _build_graph(tmp_path)
    caps = census_capabilities(_engine_request(tmp_path, db, "handler"))
    trusted = set(caps.details["trusted_edge_types"])
    assert _LIT_RELATIONS <= trusted
    assert "CALLS" in trusted
    # Bookkeeping + unemitted types are present in the DB but NOT claimed.
    assert not _FACT_LINKS & trusted
    assert "TEST_CALLS" not in trusted


def test_edge_evidence_traverses_dark_relations(tmp_path):
    """_edge_evidence emits GRAPH evidence for the newly-lit relations."""
    from groundtruth.pretask.localization_vnext import engine
    from groundtruth.pretask.localization_vnext.model import BehaviorFacet

    db = _build_graph(tmp_path)
    request = _engine_request(
        tmp_path, db, "handler returns stale dashboard output"
    )
    facets = BehaviorFacet(
        operation="handler",
        architectural_boundary=(
            "web/views.py, app/models.py, app/service.py, app/child.py, app/base.py"
        ),
    )
    node_ids = {1, 2, 3, 5, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17}
    con = engine._open_graph(str(db))
    assert con is not None
    try:
        units = engine._edge_evidence(con, facets, node_ids, request)
    finally:
        con.close()

    relations = {u.relation for u in units}
    assert _LIT_RELATIONS <= relations, relations
    assert "CALLS" in relations
    # Fact links + the unemitted TEST_CALLS are filtered, not traversed.
    assert not (_FACT_LINKS | {"TEST_CALLS"}) & relations

    # Traversal reaches the right surface: the QUERIES edge surfaces the
    # queried model, DECORATES surfaces the decorated function.
    queries_files = {u.file_path for u in units if u.relation == "QUERIES"}
    assert "app/models.py" in queries_files
    decor = [u for u in units if u.relation == "DECORATES"]
    assert any(u.file_path == "web/views.py" for u in decor)
    injects = [u for u in units if u.relation == "INJECTS"]
    assert any(u.file_path == "web/handlers.py" for u in injects)


def test_discover_candidates_includes_lit_relations(tmp_path):
    """End-to-end discovery surfaces dark-relation evidence, not bookkeeping."""
    from groundtruth.pretask.localization_vnext.engine import discover_candidates
    from groundtruth.pretask.localization_vnext.model import BehaviorFacet

    db = _build_graph(tmp_path)
    request = _engine_request(
        tmp_path, db, "handler returns stale dashboard output"
    )
    facets = BehaviorFacet(
        operation="handler",
        architectural_boundary=(
            "web/views.py, app/models.py, app/service.py, app/child.py, app/base.py"
        ),
    )
    evidence = discover_candidates(request, facets)
    relations: set[str] = set()
    for unit in evidence:
        if unit.relation:
            relations.add(unit.relation)
        for key, value in unit.metadata:
            if key == "supporting_relations":
                relations.update(v for v in value.split(",") if v)
    assert {"QUERIES", "DECORATES", "INJECTS"} <= relations
    assert not (_FACT_LINKS | {"TEST_CALLS"}) & relations


def test_unsupported_relation_rejected_at_admission(tmp_path):
    """A unit carrying an unclaimed relation is REJECTed UNSUPPORTED_RELATION."""
    from groundtruth.pretask.localization_vnext.engine import _decision_for_rejection
    from groundtruth.pretask.localization_vnext.model import (
        CandidateAction,
        EvidenceFamily,
        EvidenceUnit,
        LocalizationPolicy,
        ReasonCode,
    )

    policy = LocalizationPolicy()
    dark = EvidenceUnit.create(
        file_path="app/base.py",
        family=EvidenceFamily.GRAPH,
        relation="TEST_CALLS",
        confidence=1.0,
    )
    decision = _decision_for_rejection(
        dark, previous_rejected=set(), policy=policy
    )
    assert decision is not None
    assert decision.action is CandidateAction.REJECT
    assert ReasonCode.UNSUPPORTED_RELATION in decision.reason_codes

    lit = EvidenceUnit.create(
        file_path="web/handlers.py",
        family=EvidenceFamily.GRAPH,
        relation="INJECTS",
        confidence=1.0,
    )
    assert (
        _decision_for_rejection(lit, previous_rejected=set(), policy=policy)
        is None
    )


def test_unlisted_edge_falls_back_to_call(tmp_path):
    """Unmapped types still default to "call" — TEST_CALLS is not claimed."""
    db = _build_graph(tmp_path)
    store = _store(db)
    refs = store.get_refs_for_symbol(6)  # Service.run
    assert isinstance(refs, Ok)
    # TEST_CALLS (unmapped) degrades to the "call" fallback — surfaced but
    # never claimed as a supported kind by any of the assertions above.
    assert "call" in {r.reference_type for r in refs.value}
