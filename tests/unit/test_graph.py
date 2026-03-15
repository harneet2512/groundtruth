"""Tests for ImportGraph traversal."""

from __future__ import annotations

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


def _insert_sym(store: SymbolStore, name: str, file_path: str, **kwargs: object) -> int:
    """Helper to insert a symbol and return its ID."""
    result = store.insert_symbol(
        name=name,
        kind=str(kwargs.get("kind", "function")),
        language="python",
        file_path=file_path,
        line_number=int(kwargs.get("line_number", 1)),
        end_line=int(kwargs.get("end_line", 10)),
        is_exported=True,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(result, Ok)
    return result.value


class TestFindConnectedFiles:
    def test_single_entry_no_refs(self, in_memory_store: SymbolStore) -> None:
        """Entry file with no imports returns just itself."""
        _insert_sym(in_memory_store, "main", "src/main.py")
        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/main.py"])
        assert isinstance(result, Ok)
        nodes = result.value
        assert len(nodes) == 1
        assert nodes[0].path == "src/main.py"
        assert nodes[0].distance == 0

    def test_forward_traversal(self, in_memory_store: SymbolStore) -> None:
        """Entry file imports a symbol → finds the defining file."""
        sid = _insert_sym(in_memory_store, "helper", "src/utils.py")
        in_memory_store.insert_ref(sid, "src/main.py", 5, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/main.py"])
        assert isinstance(result, Ok)
        paths = {n.path for n in result.value}
        assert "src/main.py" in paths
        assert "src/utils.py" in paths

    def test_backward_traversal(self, in_memory_store: SymbolStore) -> None:
        """File referenced by another file discovers the importer."""
        sid = _insert_sym(in_memory_store, "api_func", "src/api.py")
        in_memory_store.insert_ref(sid, "src/client.py", 3, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/api.py"])
        assert isinstance(result, Ok)
        paths = {n.path for n in result.value}
        assert "src/api.py" in paths
        assert "src/client.py" in paths

    def test_multiple_entry_files(self, in_memory_store: SymbolStore) -> None:
        """Two entry files are both at distance 0."""
        _insert_sym(in_memory_store, "a", "src/a.py")
        _insert_sym(in_memory_store, "b", "src/b.py")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/a.py", "src/b.py"])
        assert isinstance(result, Ok)
        nodes = result.value
        assert len(nodes) == 2
        for n in nodes:
            assert n.distance == 0

    def test_depth_limit(self, in_memory_store: SymbolStore) -> None:
        """BFS stops at max_depth."""
        # Chain: main → lib → deep → verydeep
        sid_deep = _insert_sym(in_memory_store, "deep_fn", "src/deep.py")
        sid_lib = _insert_sym(in_memory_store, "lib_fn", "src/lib.py")
        sid_vdeep = _insert_sym(in_memory_store, "vdeep_fn", "src/verydeep.py")
        in_memory_store.insert_ref(sid_lib, "src/main.py", 1, "import")
        in_memory_store.insert_ref(sid_deep, "src/lib.py", 1, "import")
        in_memory_store.insert_ref(sid_vdeep, "src/deep.py", 1, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/main.py"], max_depth=2)
        assert isinstance(result, Ok)
        paths = {n.path for n in result.value}
        assert "src/main.py" in paths
        assert "src/lib.py" in paths
        assert "src/deep.py" in paths
        # verydeep is at depth 3, should be excluded
        assert "src/verydeep.py" not in paths

    def test_cycle_detection(self, in_memory_store: SymbolStore) -> None:
        """Cycle in import graph doesn't cause infinite loop."""
        sid_a = _insert_sym(in_memory_store, "fn_a", "src/a.py")
        sid_b = _insert_sym(in_memory_store, "fn_b", "src/b.py")
        # a imports b, b imports a
        in_memory_store.insert_ref(sid_b, "src/a.py", 1, "import")
        in_memory_store.insert_ref(sid_a, "src/b.py", 1, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/a.py"])
        assert isinstance(result, Ok)
        paths = {n.path for n in result.value}
        assert paths == {"src/a.py", "src/b.py"}

    def test_distance_correctness(self, in_memory_store: SymbolStore) -> None:
        """Distances are correct at each BFS level."""
        sid1 = _insert_sym(in_memory_store, "f1", "src/level1.py")
        sid2 = _insert_sym(in_memory_store, "f2", "src/level2.py")
        in_memory_store.insert_ref(sid1, "src/entry.py", 1, "import")
        in_memory_store.insert_ref(sid2, "src/level1.py", 1, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/entry.py"])
        assert isinstance(result, Ok)
        dist_map = {n.path: n.distance for n in result.value}
        assert dist_map["src/entry.py"] == 0
        assert dist_map["src/level1.py"] == 1
        assert dist_map["src/level2.py"] == 2

    def test_symbols_tracked(self, in_memory_store: SymbolStore) -> None:
        """Connected files include the symbol names involved."""
        sid = _insert_sym(in_memory_store, "myHelper", "src/helpers.py")
        in_memory_store.insert_ref(sid, "src/app.py", 2, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_connected_files(["src/app.py"])
        assert isinstance(result, Ok)
        helpers_node = [n for n in result.value if n.path == "src/helpers.py"]
        assert len(helpers_node) == 1
        assert "myHelper" in helpers_node[0].symbols


class TestFindCallers:
    def test_callers_found(self, in_memory_store: SymbolStore) -> None:
        """Find all files that reference a symbol."""
        sid = _insert_sym(in_memory_store, "target", "src/target.py")
        in_memory_store.insert_ref(sid, "src/a.py", 10, "call")
        in_memory_store.insert_ref(sid, "src/b.py", 20, "import")

        graph = ImportGraph(in_memory_store)
        result = graph.find_callers("target")
        assert isinstance(result, Ok)
        files = {r.file_path for r in result.value}
        assert files == {"src/a.py", "src/b.py"}

    def test_callers_not_found(self, in_memory_store: SymbolStore) -> None:
        """No references returns empty list."""
        graph = ImportGraph(in_memory_store)
        result = graph.find_callers("nonexistent")
        assert isinstance(result, Ok)
        assert result.value == []

    def test_callers_deduplication(self, in_memory_store: SymbolStore) -> None:
        """Same file+line from multiple symbols deduplicates."""
        sid1 = _insert_sym(in_memory_store, "overloaded", "src/a.py")
        sid2 = _insert_sym(in_memory_store, "overloaded", "src/b.py")
        in_memory_store.insert_ref(sid1, "src/caller.py", 5, "call")
        in_memory_store.insert_ref(sid2, "src/caller.py", 5, "call")

        graph = ImportGraph(in_memory_store)
        result = graph.find_callers("overloaded")
        assert isinstance(result, Ok)
        assert len(result.value) == 1


class TestFindCallees:
    def test_callees_with_refs(self, in_memory_store: SymbolStore) -> None:
        """Find symbols referenced by a file."""
        sid = _insert_sym(in_memory_store, "dep_fn", "src/dep.py", line_number=5)
        in_memory_store.insert_ref(sid, "src/consumer.py", 10, "call")

        graph = ImportGraph(in_memory_store)
        result = graph.find_callees("consumer_fn", "src/consumer.py")
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].file_path == "src/dep.py"

    def test_callees_no_refs(self, in_memory_store: SymbolStore) -> None:
        """File with no outgoing refs returns empty list."""
        graph = ImportGraph(in_memory_store)
        result = graph.find_callees("fn", "src/isolated.py")
        assert isinstance(result, Ok)
        assert result.value == []


class TestImpactRadius:
    def test_impact_nonzero(self, in_memory_store: SymbolStore) -> None:
        """Symbol referenced in multiple files returns correct count."""
        sid = _insert_sym(in_memory_store, "shared", "src/shared.py")
        in_memory_store.insert_ref(sid, "src/x.py", 1, "import")
        in_memory_store.insert_ref(sid, "src/y.py", 1, "import")
        in_memory_store.insert_ref(sid, "src/z.py", 1, "call")

        graph = ImportGraph(in_memory_store)
        result = graph.get_impact_radius("shared")
        assert isinstance(result, Ok)
        assert result.value.impact_radius == 3
        assert sorted(result.value.impacted_files) == ["src/x.py", "src/y.py", "src/z.py"]

    def test_impact_zero(self, in_memory_store: SymbolStore) -> None:
        """Symbol with no references has impact radius 0."""
        _insert_sym(in_memory_store, "unused", "src/unused.py")

        graph = ImportGraph(in_memory_store)
        result = graph.get_impact_radius("unused")
        assert isinstance(result, Ok)
        assert result.value.impact_radius == 0
        assert result.value.impacted_files == []
