"""Tests for the 3D Code City visualization data generation."""

from __future__ import annotations

import os
import tempfile

from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok
from groundtruth.viz.generate_graph_data import (
    GraphData,
    GraphEdge,
    GraphMetadata,
    GraphNode,
    _risk_tag,
    generate_graph_data,
    render_risk_map,
)


def _insert_sym(
    store: SymbolStore,
    name: str,
    file_path: str = "src/a.py",
    kind: str = "function",
    is_exported: bool = True,
    signature: str | None = None,
    params: str | None = None,
    usage_count: int = 0,
) -> int:
    """Helper to insert a symbol and return its ID."""
    result = store.insert_symbol(
        name=name, kind=kind, language="python",
        file_path=file_path, line_number=1, end_line=10,
        is_exported=is_exported, signature=signature, params=params,
        return_type=None, documentation=None, last_indexed_at=1000,
    )
    assert isinstance(result, Ok)
    sid = result.value
    if usage_count > 0:
        store.update_usage_count(sid, usage_count)
    return sid


def _insert_ref(
    store: SymbolStore,
    symbol_id: int,
    referenced_in_file: str,
    reference_type: str = "call",
    line: int = 5,
) -> None:
    """Helper to insert a reference."""
    result = store.insert_ref(
        symbol_id=symbol_id,
        referenced_in_file=referenced_in_file,
        referenced_at_line=line,
        reference_type=reference_type,
    )
    assert isinstance(result, Ok)


class TestRiskTag:
    def test_critical(self) -> None:
        assert _risk_tag(0.9) == "CRITICAL"
        assert _risk_tag(0.7) == "CRITICAL"

    def test_high(self) -> None:
        assert _risk_tag(0.5) == "HIGH"
        assert _risk_tag(0.45) == "HIGH"

    def test_moderate(self) -> None:
        assert _risk_tag(0.3) == "MODERATE"
        assert _risk_tag(0.25) == "MODERATE"

    def test_low(self) -> None:
        assert _risk_tag(0.1) == "LOW"
        assert _risk_tag(0.0) == "LOW"


class TestGenerateGraphData:
    def test_empty_store(self, in_memory_store: SymbolStore) -> None:
        """Empty store produces empty graph data."""
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        data = result.value
        assert len(data.nodes) == 0
        assert len(data.edges) == 0
        assert data.metadata.risk_summary == {
            "critical": 0, "high": 0, "moderate": 0, "low": 0,
        }

    def test_single_file(self, in_memory_store: SymbolStore) -> None:
        """Single file produces one node, no edges."""
        _insert_sym(in_memory_store, "hello", "src/main.py", usage_count=5)
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        data = result.value
        assert len(data.nodes) == 1
        node = data.nodes[0]
        assert node.id == "src/main.py"
        assert node.label == "main.py"
        assert node.directory == "src"
        assert node.usage_count >= 0
        assert node.risk_score >= 0.0
        assert node.risk_tag in ("LOW", "MODERATE", "HIGH", "CRITICAL")

    def test_multiple_files_with_edges(self, in_memory_store: SymbolStore) -> None:
        """Multiple files with cross-references produce nodes and edges."""
        sid1 = _insert_sym(in_memory_store, "getUserById", "src/users.py", usage_count=3)
        _insert_sym(in_memory_store, "handleRequest", "src/routes.py", usage_count=1)
        _insert_ref(in_memory_store, sid1, "src/routes.py", "call")

        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        data = result.value
        assert len(data.nodes) == 2

        file_ids = {n.id for n in data.nodes}
        assert "src/users.py" in file_ids
        assert "src/routes.py" in file_ids

        # Should have at least one edge from routes → users
        if len(data.edges) > 0:
            edge_pairs = {(e.source, e.target) for e in data.edges}
            assert ("src/routes.py", "src/users.py") in edge_pairs

    def test_directory_grouping(self, in_memory_store: SymbolStore) -> None:
        """Nodes get their directory from file path."""
        _insert_sym(in_memory_store, "a", "src/utils/helpers.py")
        _insert_sym(in_memory_store, "b", "src/models/user.py")

        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)

        dirs = {n.directory for n in result.value.nodes}
        assert "src/utils" in dirs
        assert "src/models" in dirs

    def test_symbol_info_populated(self, in_memory_store: SymbolStore) -> None:
        """Exported symbols are included in node data."""
        _insert_sym(
            in_memory_store, "greet", "src/hello.py",
            signature="(name: str) -> str", usage_count=2,
        )
        _insert_sym(
            in_memory_store, "_internal", "src/hello.py",
            is_exported=False, usage_count=0,
        )

        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)

        node = result.value.nodes[0]
        # Only exported symbols should appear
        assert len(node.symbols) == 1
        assert node.symbols[0].name == "greet"
        assert node.symbols[0].signature == "(name: str) -> str"
        assert node.symbols[0].usage_count == 2

    def test_limit_respected(self, in_memory_store: SymbolStore) -> None:
        """Limit parameter caps the number of files."""
        for i in range(10):
            _insert_sym(in_memory_store, f"fn_{i}", f"src/file_{i}.py")

        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer, limit=3)
        assert isinstance(result, Ok)
        assert len(result.value.nodes) <= 3

    def test_dead_code_detection(self, in_memory_store: SymbolStore) -> None:
        """Exported symbols with zero usage are marked as dead."""
        _insert_sym(in_memory_store, "alive", "src/a.py", usage_count=5)
        _insert_sym(in_memory_store, "dead", "src/a.py", usage_count=0)

        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)

        node = result.value.nodes[0]
        sym_map = {s.name: s for s in node.symbols}
        assert sym_map["dead"].is_dead is True
        assert sym_map["alive"].is_dead is False


class TestRenderRiskMap:
    def test_render_creates_file(self) -> None:
        """render_risk_map writes an HTML file with embedded data."""
        data = GraphData(
            nodes=[
                GraphNode(
                    id="src/a.py", label="a.py", directory="src",
                    risk_score=0.5, risk_tag="HIGH", usage_count=3,
                    symbol_count=1, risk_factors={"naming_ambiguity": 0.5},
                    symbols=[], imports_from=[], imported_by=[],
                    confusions=[], hallucination_rate=None,
                ),
            ],
            edges=[],
            metadata=GraphMetadata(
                total_files=1, total_symbols=1, total_refs=0,
                risk_summary={"critical": 0, "high": 1, "moderate": 0, "low": 0},
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "risk_map.html")
            result = render_risk_map(data, out_path)
            assert isinstance(result, Ok)
            assert os.path.exists(out_path)

            content = open(out_path, encoding="utf-8").read()
            assert "GROUNDTRUTH" in content
            assert "three" in content.lower()
            assert "src/a.py" in content

    def test_render_nested_dir(self) -> None:
        """render_risk_map creates parent directories as needed."""
        data = GraphData(
            nodes=[], edges=[],
            metadata=GraphMetadata(
                total_files=0, total_symbols=0, total_refs=0,
                risk_summary={"critical": 0, "high": 0, "moderate": 0, "low": 0},
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "sub", "dir", "risk_map.html")
            result = render_risk_map(data, out_path)
            assert isinstance(result, Ok)
            assert os.path.exists(out_path)

    def test_render_contains_json_data(self) -> None:
        """The rendered HTML contains the serialized graph data."""
        data = GraphData(
            nodes=[
                GraphNode(
                    id="test/file.py", label="file.py", directory="test",
                    risk_score=0.8, risk_tag="CRITICAL", usage_count=10,
                    symbol_count=3, risk_factors={"naming_ambiguity": 0.9},
                    symbols=[], imports_from=[], imported_by=[],
                    confusions=[], hallucination_rate=None,
                ),
            ],
            edges=[
                GraphEdge(source="test/file.py", target="test/other.py", edge_type="import"),
            ],
            metadata=GraphMetadata(
                total_files=2, total_symbols=5, total_refs=10,
                risk_summary={"critical": 1, "high": 0, "moderate": 0, "low": 1},
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "map.html")
            render_risk_map(data, out_path)

            content = open(out_path, encoding="utf-8").read()
            # The JSON data should be embedded in the HTML
            assert "test/file.py" in content
            assert "CRITICAL" in content
            assert "naming_ambiguity" in content

    def test_render_with_config(self) -> None:
        """render_risk_map injects __CONFIG_JSON__ with theme and bloom."""
        data = GraphData(
            nodes=[], edges=[],
            metadata=GraphMetadata(
                total_files=0, total_symbols=0, total_refs=0,
                risk_summary={"critical": 0, "high": 0, "moderate": 0, "low": 0},
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "map.html")
            render_risk_map(data, out_path, theme="light", bloom=False)

            content = open(out_path, encoding="utf-8").read()
            assert '"theme": "light"' in content
            assert '"bloom": false' in content


class TestGraphNodeNewFields:
    def test_directory_depth(self, in_memory_store: SymbolStore) -> None:
        """directory_depth is computed from path separators."""
        _insert_sym(in_memory_store, "a", "src/deep/nested/file.py")
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        node = result.value.nodes[0]
        assert node.directory_depth >= 2

    def test_normalized_height_and_width(self, in_memory_store: SymbolStore) -> None:
        """normalized_height and normalized_width are in [0, 1]."""
        _insert_sym(in_memory_store, "a", "src/a.py", usage_count=10)
        _insert_sym(in_memory_store, "b", "src/b.py", usage_count=5)
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        for node in result.value.nodes:
            assert 0.0 <= node.normalized_height <= 1.0
            assert 0.0 <= node.normalized_width <= 1.0

    def test_has_dead_code(self, in_memory_store: SymbolStore) -> None:
        """has_dead_code is True when file has dead exported symbols."""
        _insert_sym(in_memory_store, "alive", "src/a.py", usage_count=5)
        _insert_sym(in_memory_store, "dead", "src/a.py", usage_count=0)
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        node = result.value.nodes[0]
        assert node.has_dead_code is True

    def test_risk_distribution_four_tiers(self, in_memory_store: SymbolStore) -> None:
        """Metadata risk_summary uses 4-tier system."""
        _insert_sym(in_memory_store, "x", "src/x.py")
        scorer = RiskScorer(in_memory_store)
        result = generate_graph_data(in_memory_store, scorer)
        assert isinstance(result, Ok)
        rs = result.value.metadata.risk_summary
        assert "critical" in rs
        assert "high" in rs
        assert "moderate" in rs
        assert "low" in rs
