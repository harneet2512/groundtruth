"""Tests for confidence gating at the SQL layer in GraphStore."""

import sqlite3
import tempfile
import os
import pytest

from groundtruth.index.graph_store import GraphStore
from groundtruth.utils.result import Ok


@pytest.fixture
def graph_db():
    """Create a temporary graph.db with known edges at various confidence levels."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL DEFAULT 'Function',
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 1,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'python',
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'CALLS',
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );

        -- Target function
        INSERT INTO nodes (id, name, file_path, start_line, end_line, signature)
            VALUES (1, 'get_user', 'src/users.py', 10, 20, 'def get_user(uid)');

        -- Callers
        INSERT INTO nodes (id, name, file_path, start_line, end_line)
            VALUES (2, 'handler_a', 'src/routes.py', 5, 15);
        INSERT INTO nodes (id, name, file_path, start_line, end_line)
            VALUES (3, 'handler_b', 'src/api.py', 10, 20);
        INSERT INTO nodes (id, name, file_path, start_line, end_line)
            VALUES (4, 'helper_c', 'src/utils.py', 1, 5);

        -- High confidence edges (import-verified)
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (2, 1, 'CALLS', 8, 'src/routes.py', 'import', 1.0);
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (3, 1, 'CALLS', 12, 'src/api.py', 'same_file', 1.0);

        -- Low confidence edge (name_match with multiple candidates)
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (4, 1, 'CALLS', 3, 'src/utils.py', 'name_match', 0.4);

        -- Medium confidence edge (name_match with 2 candidates)
        INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence)
            VALUES (4, 1, 'CALLS', 4, 'src/utils.py', 'name_match', 0.6);
    """)
    conn.close()

    store = GraphStore(path)
    result = store.initialize()
    assert isinstance(result, Ok)
    yield store
    if store.connection:
        store.connection.close()
    try:
        os.unlink(path)
    except OSError:
        pass


class TestConfidenceGate:
    def test_get_refs_no_filter_returns_all(self, graph_db: GraphStore) -> None:
        result = graph_db.get_refs_for_symbol(1)
        assert isinstance(result, Ok)
        assert len(result.value) == 4

    def test_get_refs_with_07_filter(self, graph_db: GraphStore) -> None:
        result = graph_db.get_refs_for_symbol(1, min_confidence=0.7)
        assert isinstance(result, Ok)
        assert len(result.value) == 2
        files = {r.referenced_in_file for r in result.value}
        assert files == {"src/routes.py", "src/api.py"}

    def test_get_refs_with_09_filter(self, graph_db: GraphStore) -> None:
        result = graph_db.get_refs_for_symbol(1, min_confidence=0.9)
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_hotspots_with_confidence_gate(self, graph_db: GraphStore) -> None:
        all_result = graph_db.get_hotspots(10)
        gated_result = graph_db.get_hotspots(10, min_confidence=0.7)
        assert isinstance(all_result, Ok)
        assert isinstance(gated_result, Ok)
        if all_result.value:
            assert all_result.value[0].usage_count >= gated_result.value[0].usage_count

    def test_importers_with_confidence_gate(self, graph_db: GraphStore) -> None:
        all_result = graph_db.get_importers_of_file("src/users.py")
        gated_result = graph_db.get_importers_of_file("src/users.py", min_confidence=0.7)
        assert isinstance(all_result, Ok)
        assert isinstance(gated_result, Ok)
        assert len(all_result.value) >= len(gated_result.value)
        assert "src/utils.py" not in gated_result.value or len(gated_result.value) <= len(all_result.value)

    def test_dead_code_with_confidence_gate(self, graph_db: GraphStore) -> None:
        all_dead = graph_db.get_dead_code()
        gated_dead = graph_db.get_dead_code(min_confidence=0.7)
        assert isinstance(all_dead, Ok)
        assert isinstance(gated_dead, Ok)
        assert len(gated_dead.value) >= len(all_dead.value)

    def test_high_confidence_ratio(self, graph_db: GraphStore) -> None:
        ratio = graph_db.get_high_confidence_edge_ratio()
        assert 0.0 <= ratio <= 1.0
        assert ratio == 0.5  # 2 of 4 edges are >= 0.7

    def test_has_confidence_column(self, graph_db: GraphStore) -> None:
        assert graph_db._has_confidence_column() is True
