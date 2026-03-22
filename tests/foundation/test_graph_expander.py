"""Tests for the graph expander module."""

from __future__ import annotations

import json
import time

import pytest

from groundtruth.foundation.graph.expander import ExpandedNode, ExpansionRule, GraphExpander
from groundtruth.foundation.graph.rules import (
    ALL_RULES,
    CALLERS,
    CALLEES,
    CONSTRUCTOR_PAIR,
    IMPORT_DEPENDENTS,
    OVERRIDE_CHAIN,
    SAME_CLASS,
    SHARED_STATE,
)
from groundtruth.index.store import SymbolStore


@pytest.fixture
def store() -> SymbolStore:
    """Create an in-memory store with test data."""
    s = SymbolStore(":memory:")
    result = s.initialize()
    assert result is not None  # Ok(None)

    conn = s.connection
    now = int(time.time())

    # --- Insert symbols ---
    # File: src/models/user.py (class with methods)
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "User", "class", "python", "src/models/user.py", 1, 50, True, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (2, "__init__", "method", "python", "src/models/user.py", 3, 10, False, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (3, "__eq__", "method", "python", "src/models/user.py", 12, 15, False, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (4, "__repr__", "method", "python", "src/models/user.py", 17, 20, False, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (5, "to_dict", "method", "python", "src/models/user.py", 22, 30, False, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (6, "from_dict", "method", "python", "src/models/user.py", 32, 40, False, now),
    )

    # File: src/services/user_service.py
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (10, "get_user", "function", "python", "src/services/user_service.py", 1, 20, True, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (11, "create_user", "function", "python", "src/services/user_service.py", 22, 40, True, now),
    )

    # File: src/routes/users.py
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (20, "user_router", "function", "python", "src/routes/users.py", 1, 30, True, now),
    )

    # File: src/models/admin.py — has __init__ override for same-name test
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (30, "Admin", "class", "python", "src/models/admin.py", 1, 40, True, now),
    )
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (31, "__init__", "method", "python", "src/models/admin.py", 3, 10, False, now),
    )

    # Disconnected symbol (no refs, no relationships)
    conn.execute(
        "INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (99, "isolated_func", "function", "python", "src/isolated.py", 1, 5, False, now),
    )

    # --- Insert refs ---
    # user_service.py calls User (symbol 1)
    conn.execute(
        "INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type) "
        "VALUES (?, ?, ?, ?)",
        (1, "src/services/user_service.py", 5, "import"),
    )
    # routes/users.py calls get_user (symbol 10)
    conn.execute(
        "INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type) "
        "VALUES (?, ?, ?, ?)",
        (10, "src/routes/users.py", 3, "import"),
    )
    # routes/users.py calls create_user (symbol 11)
    conn.execute(
        "INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type) "
        "VALUES (?, ?, ?, ?)",
        (11, "src/routes/users.py", 4, "import"),
    )

    # --- Insert attributes for shared_state testing ---
    # User class (symbol_id=1) has attribute 'name' used by methods 2 and 3
    conn.execute(
        "INSERT INTO attributes (symbol_id, name, method_ids) VALUES (?, ?, ?)",
        (1, "name", json.dumps([2, 3, 5])),
    )
    # User class has attribute 'email' used by methods 2 and 4
    conn.execute(
        "INSERT INTO attributes (symbol_id, name, method_ids) VALUES (?, ?, ?)",
        (1, "email", json.dumps([2, 4])),
    )

    conn.commit()
    return s


class TestGraphExpanderCallers:
    """Test CALLERS expansion rule."""

    def test_callers_returns_known_callers(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # User (id=1) is referenced from user_service.py, which has symbols 10, 11
        results = expander.expand([1], [CALLERS], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 10 in found_ids or 11 in found_ids, f"Expected caller symbols from user_service.py, got {found_ids}"
        for node in results:
            assert node.relation == "caller"
            assert node.depth == 1
            assert node.source_seed == 1

    def test_callers_depth_2(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # Depth 2: User -> user_service -> routes (via get_user being referenced from routes)
        results_d1 = expander.expand([1], [CALLERS], max_depth=1)
        results_d2 = expander.expand([1], [CALLERS], max_depth=2)
        # Depth 2 should find more or equal nodes
        assert len(results_d2) >= len(results_d1)


class TestGraphExpanderSameClass:
    """Test SAME_CLASS expansion rule."""

    def test_same_class_returns_siblings(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # __init__ (id=2) in user.py should find __eq__, __repr__, to_dict, from_dict, User
        results = expander.expand([2], [SAME_CLASS], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        # Should find other symbols in the same file
        assert 3 in found_ids  # __eq__
        assert 4 in found_ids  # __repr__
        assert 5 in found_ids  # to_dict
        assert 6 in found_ids  # from_dict
        assert 1 in found_ids  # User class itself
        for node in results:
            assert node.relation == "same_class"


class TestGraphExpanderConstructorPair:
    """Test CONSTRUCTOR_PAIR expansion rule."""

    def test_init_finds_paired_methods(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # __init__ (id=2) should find __eq__, __repr__, to_dict, from_dict
        results = expander.expand([2], [CONSTRUCTOR_PAIR], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 3 in found_ids, "__eq__ should be found"
        assert 4 in found_ids, "__repr__ should be found"
        assert 5 in found_ids, "to_dict should be found"
        assert 6 in found_ids, "from_dict should be found"

    def test_to_dict_finds_from_dict(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # to_dict (id=5) should find from_dict and __init__
        results = expander.expand([5], [CONSTRUCTOR_PAIR], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 6 in found_ids, "from_dict should be found"
        assert 2 in found_ids, "__init__ should be found"

    def test_non_constructor_returns_empty(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # get_user (id=10) has no constructor pair pattern
        results = expander.expand([10], [CONSTRUCTOR_PAIR], max_depth=1)
        assert len(results) == 0


class TestGraphExpanderDepth:
    """Test depth control."""

    def test_max_depth_1_vs_2(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        results_d1 = expander.expand([1], [CALLERS], max_depth=1)
        results_d2 = expander.expand([1], [CALLERS], max_depth=2)
        # With depth=2, we follow callers of callers, so more results
        d1_ids = {n.symbol_id for n in results_d1}
        d2_ids = {n.symbol_id for n in results_d2}
        assert d1_ids.issubset(d2_ids), "Depth 1 results should be subset of depth 2"

    def test_depth_0_returns_nothing(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # max_depth=0 means no expansion at all
        # The BFS starts seeds at depth 0 but skips since depth >= max_depth
        results = expander.expand([1], [CALLERS], max_depth=0)
        assert len(results) == 0


class TestGraphExpanderMaxExpanded:
    """Test max_expanded limit."""

    def test_max_expanded_limits_output(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # Use ALL_RULES to find many nodes, then cap at 5
        results = expander.expand([2], ALL_RULES, max_depth=2, max_expanded=5)
        assert len(results) <= 5

    def test_max_expanded_1(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        results = expander.expand([2], ALL_RULES, max_depth=2, max_expanded=1)
        assert len(results) == 1


class TestGraphExpanderEdgeCases:
    """Test edge cases — nonexistent seeds, disconnected symbols."""

    def test_nonexistent_seed_returns_empty(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        results = expander.expand([9999], [CALLERS], max_depth=2)
        assert results == []

    def test_disconnected_symbol_callers_empty(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # isolated_func (id=99) has no refs
        results = expander.expand([99], [CALLERS], max_depth=2)
        assert results == []

    def test_disconnected_symbol_constructor_pair_empty(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # isolated_func has no constructor pair pattern
        results = expander.expand([99], [CONSTRUCTOR_PAIR], max_depth=2)
        assert results == []

    def test_empty_seed_list(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        results = expander.expand([], [CALLERS], max_depth=2)
        assert results == []


class TestGraphExpanderOverrideChain:
    """Test OVERRIDE_CHAIN expansion rule."""

    def test_override_finds_same_name_different_file(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # __init__ in user.py (id=2) should find __init__ in admin.py (id=31)
        results = expander.expand([2], [OVERRIDE_CHAIN], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 31 in found_ids, "Should find __init__ override in admin.py"


class TestGraphExpanderSharedState:
    """Test SHARED_STATE expansion rule."""

    def test_shared_state_finds_co_methods(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # __init__ (id=2) shares 'name' attr with __eq__(3) and to_dict(5),
        # and shares 'email' attr with __repr__(4)
        results = expander.expand([2], [SHARED_STATE], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 3 in found_ids, "__eq__ shares 'name' attribute"
        assert 5 in found_ids, "to_dict shares 'name' attribute"
        assert 4 in found_ids, "__repr__ shares 'email' attribute"


class TestGraphExpanderCallees:
    """Test CALLEES expansion rule."""

    def test_callees_finds_referenced_symbols(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # user_router (id=20) is in routes/users.py which references get_user(10) and create_user(11)
        results = expander.expand([20], [CALLEES], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 10 in found_ids, "get_user should be a callee"
        assert 11 in found_ids, "create_user should be a callee"


class TestGraphExpanderImportDependents:
    """Test IMPORT_DEPENDENTS expansion rule."""

    def test_import_dependents_finds_downstream_files(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # User (id=1) is in user.py. user_service.py imports User, so symbols
        # from user_service.py should appear
        results = expander.expand([1], [IMPORT_DEPENDENTS], max_depth=1)
        found_ids = {n.symbol_id for n in results}
        assert 10 in found_ids or 11 in found_ids, \
            f"Should find symbols from user_service.py, got {found_ids}"


class TestGraphExpanderMultipleRules:
    """Test expansion with multiple rules simultaneously."""

    def test_multiple_rules_combine(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        # __init__ with both CONSTRUCTOR_PAIR and CALLERS
        results = expander.expand(
            [2], [CONSTRUCTOR_PAIR, CALLERS], max_depth=1
        )
        relations = {n.relation for n in results}
        # Should find constructor pairs
        assert "constructor_pair" in relations
        # Seeds themselves should not be in results
        seed_in_results = any(n.symbol_id == 2 for n in results)
        assert not seed_in_results, "Seed should not appear in results"

    def test_results_sorted_by_depth_then_weight(self, store: SymbolStore) -> None:
        expander = GraphExpander(store)
        results = expander.expand([2], ALL_RULES, max_depth=2, max_expanded=30)
        # All depth-1 nodes should come before depth-2 nodes
        depths = [n.depth for n in results]
        for i in range(len(depths) - 1):
            assert depths[i] <= depths[i + 1], \
                f"Results not sorted by depth at index {i}: {depths}"
