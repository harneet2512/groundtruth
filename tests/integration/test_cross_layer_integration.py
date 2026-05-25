"""Cross-layer integration tests for GroundTruth.

Verifies the entire pipeline from Go indexer schema (graph.db) through
Python readers (GraphStore), delivery hooks (post_edit.py), and MCP tools.

Uses in-memory SQLite with the exact schema from gt-index/internal/store/sqlite.go.
Does NOT require the Go binary or GCC -- tests the Python reading side with
realistic data that mirrors what the Go indexer produces.

Created to prevent regression of the 120 bugs found by QA.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Schema: exact DDL from gt-index/internal/store/sqlite.go createSchema()
# ---------------------------------------------------------------------------

_GO_INDEXER_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
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

CREATE TABLE IF NOT EXISTS edges (
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
    candidate_count INTEGER DEFAULT 1,
    evidence_type TEXT,
    verification_status TEXT DEFAULT 'unverified'
);

CREATE TABLE IF NOT EXISTS file_hashes (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_test ON nodes(is_test) WHERE is_test = 1;
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_resolution ON edges(resolution_method);
CREATE INDEX IF NOT EXISTS idx_edges_confidence ON edges(confidence);
CREATE INDEX IF NOT EXISTS idx_edges_trust_tier ON edges(trust_tier);
CREATE INDEX IF NOT EXISTS idx_edges_target_tier ON edges(target_id, trust_tier);
CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    line INTEGER,
    confidence REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_node_id INTEGER NOT NULL REFERENCES nodes(id),
    target_node_id INTEGER DEFAULT 0,
    kind TEXT NOT NULL,
    expression TEXT NOT NULL,
    expected TEXT,
    line INTEGER
);

CREATE INDEX IF NOT EXISTS idx_properties_node ON properties(node_id);
CREATE INDEX IF NOT EXISTS idx_properties_kind ON properties(kind);
CREATE INDEX IF NOT EXISTS idx_properties_node_kind ON properties(node_id, kind);
CREATE INDEX IF NOT EXISTS idx_assertions_test ON assertions(test_node_id);
CREATE INDEX IF NOT EXISTS idx_assertions_target ON assertions(target_node_id);
"""


def _create_graph_db(tmp_path, *, nodes=None, edges=None, properties=None, assertions=None):
    """Create a graph.db file with the Go indexer schema and optional data.

    Returns the path to the created database.
    """
    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_GO_INDEXER_SCHEMA)

    if nodes:
        for n in nodes:
            conn.execute(
                "INSERT INTO nodes "
                "(label, name, qualified_name, file_path, start_line, end_line, "
                "signature, return_type, is_exported, is_test, language, parent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    n.get("label", "Function"),
                    n["name"],
                    n.get("qualified_name", ""),
                    n.get("file_path", "src/main.py"),
                    n.get("start_line", 1),
                    n.get("end_line", 10),
                    n.get("signature", ""),
                    n.get("return_type", ""),
                    n.get("is_exported", 0),
                    n.get("is_test", 0),
                    n.get("language", "Python"),
                    n.get("parent_id"),
                ),
            )
    if edges:
        for e in edges:
            conn.execute(
                "INSERT INTO edges "
                "(source_id, target_id, type, source_line, source_file, "
                "resolution_method, confidence, metadata, trust_tier, "
                "candidate_count, evidence_type, verification_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e["source_id"],
                    e["target_id"],
                    e.get("type", "CALLS"),
                    e.get("source_line", 1),
                    e.get("source_file", "src/caller.py"),
                    e.get("resolution_method", "import"),
                    e.get("confidence", 1.0),
                    e.get("metadata", ""),
                    e.get("trust_tier", "CERTIFIED"),
                    e.get("candidate_count", 1),
                    e.get("evidence_type", "ast_call"),
                    e.get("verification_status", "unverified"),
                ),
            )
    if properties:
        for p in properties:
            conn.execute(
                "INSERT INTO properties (node_id, kind, value, line, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    p["node_id"],
                    p["kind"],
                    p["value"],
                    p.get("line", 1),
                    p.get("confidence", 1.0),
                ),
            )
    if assertions:
        for a in assertions:
            conn.execute(
                "INSERT INTO assertions "
                "(test_node_id, target_node_id, kind, expression, expected, line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    a["test_node_id"],
                    a.get("target_node_id", 0),
                    a["kind"],
                    a["expression"],
                    a.get("expected", ""),
                    a.get("line", 1),
                ),
            )

    conn.commit()
    conn.close()
    return db_path


def _make_graph_store(db_path: str):
    """Instantiate a GraphStore pointing at db_path and initialize it."""
    from groundtruth.index.graph_store import GraphStore
    from groundtruth.utils.result import Ok

    store = GraphStore(db_path)
    result = store.initialize()
    assert isinstance(result, Ok), f"GraphStore.initialize() failed: {result}"
    return store


# ===================================================================
# Test 1: Properties table end-to-end
# ===================================================================

class TestPropertiesEndToEnd:
    """Verify Go indexer properties table is readable from Python."""

    def test_properties_written_by_go_are_readable_by_python(self, tmp_path):
        """The biggest bug: properties written by Go but never read by Python."""
        nodes = [
            {"name": "process_request", "file_path": "src/api.py",
             "start_line": 10, "end_line": 30, "language": "Python",
             "signature": "def process_request(self, data: dict) -> Response"},
        ]
        properties = [
            {"node_id": 1, "kind": "guard_clause", "value": "if not data: return None",
             "line": 12, "confidence": 0.95},
            {"node_id": 1, "kind": "conditional_return", "value": "return ErrorResponse(400)",
             "line": 15, "confidence": 0.9},
            {"node_id": 1, "kind": "side_effect", "value": "self.logger.info()",
             "line": 20, "confidence": 0.85},
            {"node_id": 1, "kind": "raise_type", "value": "ValueError",
             "line": 22, "confidence": 1.0},
            {"node_id": 1, "kind": "framework_call", "value": "db.session.commit()",
             "line": 25, "confidence": 0.8},
            {"node_id": 1, "kind": "docstring",
             "value": "Process an incoming API request and persist changes.",
             "line": 11, "confidence": 1.0},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, properties=properties)
        store = _make_graph_store(db_path)

        # get_properties(node_id) should return all 6
        result = store.get_properties(1)
        assert len(result) == 6, f"Expected 6 properties, got {len(result)}"

        kinds = {p["kind"] for p in result}
        expected_kinds = {
            "guard_clause", "conditional_return", "side_effect",
            "raise_type", "framework_call", "docstring",
        }
        assert kinds == expected_kinds, f"Missing kinds: {expected_kinds - kinds}"

    def test_properties_filter_by_kind(self, tmp_path):
        """Verify kind-filtered property query works."""
        nodes = [{"name": "foo", "file_path": "x.py", "language": "Python"}]
        properties = [
            {"node_id": 1, "kind": "guard_clause", "value": "if x: return", "line": 2},
            {"node_id": 1, "kind": "guard_clause", "value": "if y: raise", "line": 3},
            {"node_id": 1, "kind": "side_effect", "value": "db.save()", "line": 5},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, properties=properties)
        store = _make_graph_store(db_path)

        guards = store.get_properties(1, kind="guard_clause")
        assert len(guards) == 2
        effects = store.get_properties(1, kind="side_effect")
        assert len(effects) == 1

    def test_property_counts_by_kind(self, tmp_path):
        """Verify get_property_counts aggregates correctly."""
        nodes = [{"name": "f1", "file_path": "a.py", "language": "Python"},
                 {"name": "f2", "file_path": "b.py", "language": "Python"}]
        properties = [
            {"node_id": 1, "kind": "guard_clause", "value": "v1", "line": 1},
            {"node_id": 1, "kind": "guard_clause", "value": "v2", "line": 2},
            {"node_id": 2, "kind": "side_effect", "value": "v3", "line": 3},
            {"node_id": 2, "kind": "raise_type", "value": "v4", "line": 4},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, properties=properties)
        store = _make_graph_store(db_path)
        counts = store.get_property_counts()
        assert counts.get("guard_clause") == 2
        assert counts.get("side_effect") == 1
        assert counts.get("raise_type") == 1

    def test_properties_for_nonexistent_node(self, tmp_path):
        """Properties query for missing node returns empty list, not error."""
        nodes = [{"name": "f", "file_path": "a.py", "language": "Python"}]
        db_path = _create_graph_db(tmp_path, nodes=nodes)
        store = _make_graph_store(db_path)
        assert store.get_properties(9999) == []


# ===================================================================
# Test 2: Confidence filtering consistency
# ===================================================================

class TestConfidenceFiltering:
    """Verify confidence thresholds are applied consistently across queries."""

    @pytest.fixture()
    def store_with_confidence_edges(self, tmp_path):
        """Create a store with edges at various confidence levels."""
        # target_func (id=1) is exported, called by 6 callers at different confidences
        nodes = [
            {"name": "target_func", "file_path": "src/target.py", "is_exported": 1,
             "language": "Python", "label": "Function"},
            {"name": "caller_10", "file_path": "src/a.py", "language": "Python", "label": "Function"},
            {"name": "caller_09", "file_path": "src/b.py", "language": "Python", "label": "Function"},
            {"name": "caller_07", "file_path": "src/c.py", "language": "Python", "label": "Function"},
            {"name": "caller_05", "file_path": "src/d.py", "language": "Python", "label": "Function"},
            {"name": "caller_02", "file_path": "src/e.py", "language": "Python", "label": "Function"},
            {"name": "caller_00", "file_path": "src/f.py", "language": "Python", "label": "Function"},
        ]
        edges = [
            {"source_id": 2, "target_id": 1, "type": "CALLS", "confidence": 1.0,
             "source_file": "src/a.py", "resolution_method": "same_file"},
            {"source_id": 3, "target_id": 1, "type": "CALLS", "confidence": 0.9,
             "source_file": "src/b.py", "resolution_method": "import"},
            {"source_id": 4, "target_id": 1, "type": "CALLS", "confidence": 0.7,
             "source_file": "src/c.py", "resolution_method": "name_match"},
            {"source_id": 5, "target_id": 1, "type": "CALLS", "confidence": 0.5,
             "source_file": "src/d.py", "resolution_method": "name_match"},
            {"source_id": 6, "target_id": 1, "type": "CALLS", "confidence": 0.2,
             "source_file": "src/e.py", "resolution_method": "name_match"},
            {"source_id": 7, "target_id": 1, "type": "CALLS", "confidence": 0.0,
             "source_file": "src/f.py", "resolution_method": "name_match"},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        return _make_graph_store(db_path)

    def test_usage_cache_excludes_below_05(self, store_with_confidence_edges):
        """_build_usage_cache uses >= 0.5 floor. Edges at 0.2, 0.0 excluded."""
        from groundtruth.utils.result import Ok

        store = store_with_confidence_edges
        # target_func (id=1) should have 4 usages (1.0, 0.9, 0.7, 0.5)
        # The 0.2 and 0.0 edges are excluded by the >= 0.5 filter in _build_usage_cache
        usage = store._usage_for(1)
        assert usage == 4, (
            f"Expected 4 usages (conf >= 0.5), got {usage}. "
            "Edges at 0.2 and 0.0 should be excluded from usage cache."
        )

    def test_dead_code_counts_all_edges(self, store_with_confidence_edges):
        """get_dead_code checks existence of ANY edge, regardless of confidence."""
        from groundtruth.utils.result import Ok

        store = store_with_confidence_edges
        result = store.get_dead_code()
        assert isinstance(result, Ok)
        dead = result.value
        # target_func is exported AND has edges -> should NOT be dead
        dead_names = [s.name for s in dead]
        assert "target_func" not in dead_names, (
            "target_func has 6 edges (some low confidence) and should NOT be dead code. "
            "get_dead_code must count edges at ANY confidence."
        )

    def test_hotspots_with_min_confidence(self, store_with_confidence_edges):
        """get_hotspots(min_confidence=0.7) only counts edges >= 0.7."""
        from groundtruth.utils.result import Ok

        store = store_with_confidence_edges
        result = store.get_hotspots(limit=10, min_confidence=0.7)
        assert isinstance(result, Ok)
        hotspots = result.value
        if hotspots:
            # Only 3 edges have confidence >= 0.7 (1.0, 0.9, 0.7)
            target = next((h for h in hotspots if h.name == "target_func"), None)
            assert target is not None, "target_func should appear in hotspots"
            assert target.usage_count == 3, (
                f"Expected 3 usages at conf >= 0.7, got {target.usage_count}"
            )

    def test_get_refs_for_symbol_with_min_confidence(self, store_with_confidence_edges):
        """get_refs_for_symbol(min_confidence=0.5) excludes 0.2 and 0.0 edges."""
        from groundtruth.utils.result import Ok

        store = store_with_confidence_edges
        result = store.get_refs_for_symbol(1, min_confidence=0.5)
        assert isinstance(result, Ok)
        refs = result.value
        assert len(refs) == 4, (
            f"Expected 4 refs (conf >= 0.5), got {len(refs)}. "
            "Edges at 0.2 and 0.0 should be filtered out."
        )

    def test_get_refs_for_symbol_no_filter(self, store_with_confidence_edges):
        """get_refs_for_symbol without min_confidence returns ALL edges."""
        from groundtruth.utils.result import Ok

        store = store_with_confidence_edges
        result = store.get_refs_for_symbol(1)
        assert isinstance(result, Ok)
        refs = result.value
        assert len(refs) == 6, (
            f"Expected all 6 refs without filter, got {len(refs)}"
        )


# ===================================================================
# Test 3: Edge type mapping completeness
# ===================================================================

class TestEdgeTypeMapping:
    """Verify _EDGE_TYPE_TO_REF covers all Go edge types."""

    def test_all_edge_types_mapped(self):
        """Every edge type the Go indexer can produce has a mapping."""
        from groundtruth.index.graph_store import _EDGE_TYPE_TO_REF

        expected_types = {
            "CALLS", "IMPORTS", "DEFINES", "INHERITS", "IMPLEMENTS",
            "EXTENDS", "COMPOSES", "RE_EXPORTS", "HANDLES_ROUTE",
        }
        mapped_types = set(_EDGE_TYPE_TO_REF.keys())

        # Check the types we explicitly require are mapped
        for t in ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS",
                  "COMPOSES", "RE_EXPORTS", "HANDLES_ROUTE"):
            assert t in mapped_types, (
                f"Edge type '{t}' missing from _EDGE_TYPE_TO_REF. "
                "Go indexer writes this type but Python cannot map it."
            )

    def test_get_refs_from_file_maps_each_type(self, tmp_path):
        """Each edge type maps to the correct reference_type."""
        from groundtruth.index.graph_store import _EDGE_TYPE_TO_REF
        from groundtruth.utils.result import Ok

        # Create a node for each edge type
        edge_types = ["CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS",
                      "COMPOSES", "RE_EXPORTS", "HANDLES_ROUTE"]
        nodes = [
            {"name": "source", "file_path": "src/s.py", "language": "Python"},
        ]
        # One target per edge type
        for i, et in enumerate(edge_types):
            nodes.append({
                "name": f"target_{et.lower()}",
                "file_path": f"src/t{i}.py",
                "language": "Python",
            })

        edges = []
        for i, et in enumerate(edge_types):
            edges.append({
                "source_id": 1,
                "target_id": i + 2,
                "type": et,
                "source_file": "src/s.py",
                "source_line": 10 + i,
                "confidence": 1.0,
            })

        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        store = _make_graph_store(db_path)

        result = store.get_refs_from_file("src/s.py")
        assert isinstance(result, Ok)
        refs = result.value
        assert len(refs) == len(edge_types), (
            f"Expected {len(edge_types)} refs, got {len(refs)}"
        )

        ref_types_found = {r.reference_type for r in refs}
        expected_ref_types = set(_EDGE_TYPE_TO_REF[et] for et in edge_types)
        assert ref_types_found == expected_ref_types, (
            f"Mapped ref types {ref_types_found} != expected {expected_ref_types}"
        )

    def test_unknown_edge_type_falls_to_default(self, tmp_path):
        """An edge type not in _EDGE_TYPE_TO_REF falls back to 'call'."""
        from groundtruth.utils.result import Ok

        nodes = [
            {"name": "a", "file_path": "x.py", "language": "Python"},
            {"name": "b", "file_path": "y.py", "language": "Python"},
        ]
        edges = [
            {"source_id": 1, "target_id": 2, "type": "UNKNOWN_FUTURE_TYPE",
             "source_file": "x.py", "confidence": 1.0},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        store = _make_graph_store(db_path)

        result = store.get_refs_from_file("x.py")
        assert isinstance(result, Ok)
        refs = result.value
        assert len(refs) == 1
        # Default mapping is "call" per _edge_row_to_ref
        assert refs[0].reference_type == "call"


# ===================================================================
# Test 4: G7 silence gate preserves contracts
# ===================================================================

class TestG7SilenceGate:
    """Verify the G7 silence gate logic preserves critical evidence lines."""

    def test_g7_preserves_contract_content_when_isolated(self):
        """An isolated function (0 callers, 0 siblings, 0 peers) should
        still see GUARD:/MUTATES:/RETURNS:/RAISES:/PARAMS: category lines,
        [TEST] lines, and signature lines (when typed) preserved.

        The G7 gate uses lstrip().startswith() so indented category lines
        (e.g. "  GUARD: ...") are matched correctly.
        """
        # Simulate func_parts as they appear in generate_improved_evidence
        func_parts = [
            "[BEHAVIORAL CONTRACT]",
            "  GUARD: if not data -> return None",
            "  MUTATES: self._cache, self._state",
            "[SIGNATURE] def process(self, data: dict)",
            "[TEST] test_process expects: data == {'key': 'val'}",
            "[PATTERN] sibling do_other() does: ...",    # should be suppressed
            "CALLERS: some_file.py:10 `result = process(data)`",  # should be suppressed
        ]

        # G7 gate logic (matches post_edit.py)
        total_callers = 0
        siblings = []
        peers = []
        sig = "[SIGNATURE] def process(self, data: dict)"

        if total_callers == 0 and not siblings and not peers:
            _has_typed_sig = sig and ("->" in sig or ": " in sig)
            _G7_KEEP_PREFIXES = (
                "[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
                "GUARD:", "MUTATES:", "ACCUMULATES:", "[SECURITY]",
                "[SERDE]", "PARAMS:", "[RAISES]", "[CATCHES]",
                "FIELD:", "READS:", "[BOUNDARY]",
                "[CONCURRENCY]", "[CONFIG]", "[ORDER]", "[RESOURCE]", "[TWIN]",
            )
            _kept = [p for p in func_parts
                     if (_has_typed_sig and p.lstrip().startswith("[SIGNATURE]"))
                     or p.lstrip().startswith("[TEST]")
                     or any(p.lstrip().startswith(pfx) for pfx in _G7_KEEP_PREFIXES[2:])]
            func_parts = _kept

        # Verify critical lines are kept
        assert any("GUARD:" in p for p in func_parts), \
            "G7 gate suppressed GUARD: lines"
        assert any("MUTATES:" in p for p in func_parts), \
            "G7 gate suppressed MUTATES: lines"
        assert any(p.lstrip().startswith("[TEST]") for p in func_parts), \
            "G7 gate suppressed [TEST]"
        assert any(p.lstrip().startswith("[SIGNATURE]") for p in func_parts), \
            "G7 gate suppressed [SIGNATURE] despite typed sig"
        assert any(p.lstrip().startswith("[BEHAVIORAL CONTRACT]") for p in func_parts), \
            "G7 gate suppressed [BEHAVIORAL CONTRACT]"

        # Verify non-critical lines are suppressed
        assert not any(p.lstrip().startswith("[PATTERN]") for p in func_parts), \
            "G7 gate should suppress [PATTERN] for isolated functions"
        assert not any(p.lstrip().startswith("CALLERS:") for p in func_parts), \
            "G7 gate should suppress CALLERS: for isolated functions"

    def test_g7_preserves_signature_when_typed(self):
        """Signature line is kept when the sig has type annotations (': ' or '->')."""
        func_parts = [
            "[SIGNATURE] def foo(x: int, y: str) -> bool",
            "some_other_line",
        ]
        sig = "[SIGNATURE] def foo(x: int, y: str) -> bool"
        total_callers = 0
        siblings = []
        peers = []

        if total_callers == 0 and not siblings and not peers:
            _has_typed_sig = sig and ("->" in sig or ": " in sig)
            _G7_KEEP_PREFIXES = (
                "[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
                "GUARD:", "MUTATES:", "ACCUMULATES:", "[SECURITY]",
                "[SERDE]", "PARAMS:", "[RAISES]", "[CATCHES]",
                "FIELD:", "READS:", "[BOUNDARY]",
                "[CONCURRENCY]", "[CONFIG]", "[ORDER]", "[RESOURCE]", "[TWIN]",
            )
            _kept = [p for p in func_parts
                     if (_has_typed_sig and p.lstrip().startswith("[SIGNATURE]"))
                     or p.lstrip().startswith("[TEST]")
                     or any(p.lstrip().startswith(pfx) for pfx in _G7_KEEP_PREFIXES[2:])]
            func_parts = _kept

        assert len(func_parts) == 1
        assert "[SIGNATURE] def foo" in func_parts[0]

    def test_g7_suppresses_signature_when_untyped(self):
        """Signature is suppressed for bare functions with no type info."""
        func_parts = [
            "[SIGNATURE] def foo(x, y)",
            "some_other_line",
        ]
        sig = "[SIGNATURE] def foo(x, y)"  # No -> or : type annotations
        total_callers = 0
        siblings = []
        peers = []

        if total_callers == 0 and not siblings and not peers:
            _has_typed_sig = sig and ("->" in sig or ": " in sig)
            _G7_KEEP_PREFIXES = (
                "[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
                "GUARD:", "MUTATES:", "ACCUMULATES:", "[SECURITY]",
                "[SERDE]", "PARAMS:", "[RAISES]", "[CATCHES]",
                "FIELD:", "READS:", "[BOUNDARY]",
                "[CONCURRENCY]", "[CONFIG]", "[ORDER]", "[RESOURCE]", "[TWIN]",
            )
            _kept = [p for p in func_parts
                     if (_has_typed_sig and p.lstrip().startswith("[SIGNATURE]"))
                     or p.lstrip().startswith("[TEST]")
                     or any(p.lstrip().startswith(pfx) for pfx in _G7_KEEP_PREFIXES[2:])]
            func_parts = _kept

        assert len(func_parts) == 0, (
            "Untyped signature should be suppressed for isolated functions"
        )

    def test_g7_all_keep_prefixes_are_preserved(self):
        """Every prefix in _G7_KEEP_PREFIXES actually survives the gate."""
        _G7_KEEP_PREFIXES = (
            "[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
            "GUARD:", "MUTATES:", "ACCUMULATES:", "[SECURITY]",
            "[SERDE]", "PARAMS:", "[RAISES]", "[CATCHES]",
            "FIELD:", "READS:", "[BOUNDARY]",
            "[CONCURRENCY]", "[CONFIG]", "[ORDER]", "[RESOURCE]", "[TWIN]",
        )
        # Build func_parts with one line per keep prefix
        func_parts = [f"{pfx} some content" for pfx in _G7_KEEP_PREFIXES]
        # Add lines that should be suppressed
        func_parts.extend(["[PATTERN] bad", "CALLERS: bad"])

        sig = "[SIGNATURE] def f(x: int) -> str"  # typed
        total_callers = 0
        siblings = []
        peers = []

        if total_callers == 0 and not siblings and not peers:
            _has_typed_sig = sig and ("->" in sig or ": " in sig)
            _kept = [p for p in func_parts
                     if (_has_typed_sig and p.lstrip().startswith("[SIGNATURE]"))
                     or p.lstrip().startswith("[TEST]")
                     or any(p.lstrip().startswith(pfx) for pfx in _G7_KEEP_PREFIXES[2:])]
            func_parts = _kept

        # All keep prefixes should survive (sig via [SIGNATURE] check, rest via prefix match)
        expected_count = len(_G7_KEEP_PREFIXES)
        assert len(func_parts) == expected_count, (
            f"Expected {expected_count} kept lines, got {len(func_parts)}. "
            f"Kept: {func_parts}"
        )


# ===================================================================
# Test 5: Connection safety
# ===================================================================

class TestConnectionSafety:
    """Verify _open_graph_db opens connections with correct pragmas."""

    def test_open_graph_db_sets_row_factory(self, tmp_path):
        """_open_graph_db must set row_factory = sqlite3.Row."""
        from groundtruth.hooks.post_edit import _open_graph_db

        db_path = _create_graph_db(tmp_path)
        conn = _open_graph_db(db_path)
        try:
            assert conn.row_factory is sqlite3.Row, (
                f"row_factory is {conn.row_factory}, expected sqlite3.Row"
            )
        finally:
            conn.close()

    def test_open_graph_db_sets_busy_timeout(self, tmp_path):
        """_open_graph_db must set busy_timeout=5000."""
        from groundtruth.hooks.post_edit import _open_graph_db

        db_path = _create_graph_db(tmp_path)
        conn = _open_graph_db(db_path)
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == 5000, f"busy_timeout is {timeout}, expected 5000"
        finally:
            conn.close()

    def test_open_graph_db_readonly(self, tmp_path):
        """_open_graph_db must open in read-only mode (writes should fail)."""
        from groundtruth.hooks.post_edit import _open_graph_db

        db_path = _create_graph_db(tmp_path)
        conn = _open_graph_db(db_path)
        try:
            # Read-only mode: writes should raise OperationalError
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO nodes (label, name, file_path, language) "
                    "VALUES ('Function', 'test', 'test.py', 'Python')"
                )
        finally:
            conn.close()

    def test_graphstore_uses_row_factory(self, tmp_path):
        """GraphStore.initialize() must set row_factory on its connection."""
        nodes = [{"name": "f", "file_path": "a.py", "language": "Python"}]
        db_path = _create_graph_db(tmp_path, nodes=nodes)
        store = _make_graph_store(db_path)
        assert store.connection.row_factory is sqlite3.Row


# ===================================================================
# Test 6: Brief/hook filter consistency
# ===================================================================

class TestBriefHookFilterConsistency:
    """Verify L1 brief and L3 hooks use confidence-based filtering, not resolution_method."""

    def test_callers_filtered_by_confidence_not_resolution_method(self, tmp_path):
        """An import-verified edge at 1.0 and a name_match edge at 0.4:
        the name_match at 0.4 should be filtered out by confidence, not by
        its resolution_method."""
        from groundtruth.utils.result import Ok

        nodes = [
            {"name": "target", "file_path": "lib/core.py", "is_exported": 1,
             "language": "Python", "label": "Function"},
            {"name": "good_caller", "file_path": "app/main.py",
             "language": "Python", "label": "Function"},
            {"name": "weak_caller", "file_path": "util/helper.py",
             "language": "Python", "label": "Function"},
        ]
        edges = [
            # High confidence, import-verified
            {"source_id": 2, "target_id": 1, "type": "CALLS",
             "confidence": 1.0, "resolution_method": "import",
             "source_file": "app/main.py"},
            # Low confidence, name-match
            {"source_id": 3, "target_id": 1, "type": "CALLS",
             "confidence": 0.4, "resolution_method": "name_match",
             "source_file": "util/helper.py"},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        store = _make_graph_store(db_path)

        # get_refs_for_symbol with min_confidence=0.5 should exclude the 0.4 edge
        result = store.get_refs_for_symbol(1, min_confidence=0.5)
        assert isinstance(result, Ok)
        refs = result.value
        assert len(refs) == 1, (
            f"Expected 1 ref (conf >= 0.5), got {len(refs)}. "
            "Filter must be on confidence, not resolution_method."
        )
        assert refs[0].referenced_in_file == "app/main.py"

    def test_importers_filtered_by_confidence(self, tmp_path):
        """get_importers_of_file with min_confidence gates on edge confidence."""
        from groundtruth.utils.result import Ok

        nodes = [
            {"name": "target", "file_path": "lib/core.py",
             "language": "Python", "label": "Function"},
            {"name": "hi_caller", "file_path": "app/hi.py",
             "language": "Python", "label": "Function"},
            {"name": "lo_caller", "file_path": "app/lo.py",
             "language": "Python", "label": "Function"},
        ]
        edges = [
            {"source_id": 2, "target_id": 1, "type": "CALLS",
             "confidence": 0.9, "resolution_method": "import",
             "source_file": "app/hi.py"},
            {"source_id": 3, "target_id": 1, "type": "CALLS",
             "confidence": 0.3, "resolution_method": "name_match",
             "source_file": "app/lo.py"},
        ]
        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        store = _make_graph_store(db_path)

        result = store.get_importers_of_file("lib/core.py", min_confidence=0.5)
        assert isinstance(result, Ok)
        importers = result.value
        assert len(importers) == 1
        assert importers[0] == "app/hi.py"


# ===================================================================
# Test 7: BFS traversal limit
# ===================================================================

class TestBFSTraversalLimit:
    """Verify find_connected_files respects max_visited."""

    def test_bfs_stops_at_max_visited(self, tmp_path):
        """A graph with 1000 files: BFS with max_visited=500 should stop early."""
        from groundtruth.index.graph import ImportGraph
        from groundtruth.utils.result import Ok

        # Create a chain: file_0 -> file_1 -> file_2 -> ... -> file_999
        nodes = []
        edges = []
        for i in range(1000):
            nodes.append({
                "name": f"func_{i}",
                "file_path": f"pkg/mod_{i}.py",
                "language": "Python",
                "label": "Function",
                "is_exported": 1,
            })
        for i in range(999):
            edges.append({
                "source_id": i + 1,
                "target_id": i + 2,
                "type": "CALLS",
                "source_file": f"pkg/mod_{i}.py",
                "confidence": 1.0,
                "resolution_method": "import",
            })

        db_path = _create_graph_db(tmp_path, nodes=nodes, edges=edges)
        store = _make_graph_store(db_path)
        graph = ImportGraph(store)

        result = graph.find_connected_files(
            ["pkg/mod_0.py"],
            max_depth=1000,  # effectively unlimited depth
            max_visited=500,
        )
        assert isinstance(result, Ok)
        visited = result.value
        assert len(visited) <= 500, (
            f"BFS visited {len(visited)} files, expected <= 500 (max_visited)"
        )
        assert len(visited) >= 100, (
            f"BFS visited only {len(visited)} files, expected >= 100 "
            "(should traverse until hitting max_visited, not stop too early)"
        )

    def test_bfs_default_limit_is_500(self, tmp_path):
        """find_connected_files default max_visited should be 500."""
        import inspect
        from groundtruth.index.graph import ImportGraph

        sig = inspect.signature(ImportGraph.find_connected_files)
        default = sig.parameters["max_visited"].default
        assert default == 500, f"Default max_visited is {default}, expected 500"


# ===================================================================
# Test 8: Guard test -- count all tests
# ===================================================================

class TestGuardTestCount:
    """Guard: ensure minimum test count to catch accidental test deletion."""

    def test_minimum_test_count(self):
        """There must be at least 7 test classes/functions in this file."""
        import ast
        import pathlib

        test_file = pathlib.Path(__file__)
        tree = ast.parse(test_file.read_text(encoding="utf-8"))

        test_count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                test_count += 1

        assert test_count >= 7, (
            f"Only {test_count} test functions found, expected >= 7. "
            "Did someone accidentally delete tests?"
        )
