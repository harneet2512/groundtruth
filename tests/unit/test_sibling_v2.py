"""Tests for sibling_v2 evidence module.

Covers dunder filtering, ranking by shared symbols, top-2 limit,
structured output format, and feature flag OFF behavior.
"""

from __future__ import annotations

import os
import sqlite3

import pytest


# -- Helpers --

def _create_graph_db(path: str) -> sqlite3.Connection:
    """Create graph.db at the given path with Go indexer schema."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
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
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT
        );
    """)
    return conn


# -- Sample class source --

SAMPLE_CLASS_SOURCE = """\
class ItemProcessor:
    def __init__(self, queue):
        self.queue = queue
        self.results = []

    def __repr__(self):
        return f"ItemProcessor({len(self.queue)})"

    def __eq__(self, other):
        return self.queue == other.queue

    def __hash__(self):
        return hash(id(self))

    @property
    def count(self):
        return len(self.results)

    def process_item(self, item):
        result = self.transform(item)
        self.results.append(result)
        self.queue.remove(item)
        return result

    def process_batch(self, items):
        batch_results = []
        for item in items:
            result = self.transform(item)
            batch_results.append(result)
            self.results.append(result)
        self.queue.clear()
        return batch_results

    def transform(self, item):
        return item.upper()

    def validate_item(self, item):
        if not item:
            raise ValueError("empty item")
        if item in self.results:
            raise ValueError("duplicate")
        return True

    def reset(self):
        pass
"""


class TestDunderFiltering:
    """Test that dunder methods are filtered out."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "1")

    @pytest.fixture
    def db_with_class(self, tmp_path):
        """Create graph.db and source file with a class having dunders."""
        db_path = str(tmp_path / "graph.db")
        conn = _create_graph_db(db_path)

        source_file = tmp_path / "processor.py"
        source_file.write_text(SAMPLE_CLASS_SOURCE)
        fpath = str(source_file)

        # Insert parent class
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) "
            "VALUES (1, 'Class', 'ItemProcessor', ?, 1, 48, 'python')",
            (fpath,),
        )

        # Insert methods with parent_id = 1
        methods = [
            (2, "__init__", 2, 4, "(self, queue)"),
            (3, "__repr__", 6, 7, "(self)"),
            (4, "__eq__", 9, 10, "(self, other)"),
            (5, "__hash__", 12, 13, "(self)"),
            (6, "count", 16, 17, "(self)"),  # property getter
            (7, "process_item", 19, 23, "(self, item)"),
            (8, "process_batch", 25, 32, "(self, items)"),
            (9, "transform", 34, 35, "(self, item)"),
            (10, "validate_item", 37, 42, "(self, item)"),
            (11, "reset", 44, 45, "(self)"),
        ]
        for mid, mname, start, end, sig in methods:
            conn.execute(
                "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
                "signature, language, parent_id) "
                "VALUES (?, 'Method', ?, ?, ?, ?, ?, 'python', 1)",
                (mid, mname, fpath, start, end, sig),
            )
        conn.commit()
        conn.close()
        return db_path, fpath

    def test_no_dunders_in_results(self, db_with_class) -> None:
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path, fpath = db_with_class

        results = select_siblings_v2(db_path, fpath, "process_item", "")
        names = {r["name"] for r in results}
        assert "__init__" not in names
        assert "__repr__" not in names
        assert "__eq__" not in names
        assert "__hash__" not in names

    def test_trivial_methods_filtered(self, db_with_class) -> None:
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path, fpath = db_with_class

        results = select_siblings_v2(db_path, fpath, "process_item", "")
        names = {r["name"] for r in results}
        # reset() is just 'pass' -- should be filtered
        assert "reset" not in names


class TestRankingBySharedSymbols:
    """Test that siblings are ranked by behavioral similarity."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "1")

    @pytest.fixture
    def db_with_class(self, tmp_path):
        db_path = str(tmp_path / "graph.db")
        conn = _create_graph_db(db_path)

        source_file = tmp_path / "processor.py"
        source_file.write_text(SAMPLE_CLASS_SOURCE)
        fpath = str(source_file)

        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) "
            "VALUES (1, 'Class', 'ItemProcessor', ?, 1, 48, 'python')",
            (fpath,),
        )

        methods = [
            (7, "process_item", 19, 23, "(self, item)", "list"),
            (8, "process_batch", 25, 32, "(self, items)", "list"),
            (9, "transform", 34, 35, "(self, item)", None),
            (10, "validate_item", 37, 42, "(self, item)", "bool"),
        ]
        for mid, mname, start, end, sig, ret in methods:
            conn.execute(
                "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
                "signature, return_type, language, parent_id) "
                "VALUES (?, 'Method', ?, ?, ?, ?, ?, ?, 'python', 1)",
                (mid, mname, fpath, start, end, sig, ret),
            )
        conn.commit()
        conn.close()
        return db_path, fpath

    def test_process_batch_ranked_high_for_process_item(self, db_with_class) -> None:
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path, fpath = db_with_class

        results = select_siblings_v2(db_path, fpath, "process_item", "")
        assert len(results) > 0
        # process_batch shares the most symbols (self.results, self.queue, result, transform)
        assert results[0]["name"] == "process_batch"

    def test_shared_symbols_populated(self, db_with_class) -> None:
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path, fpath = db_with_class

        results = select_siblings_v2(db_path, fpath, "process_item", "")
        top = results[0]
        assert "shared_symbols" in top
        assert isinstance(top["shared_symbols"], list)
        assert len(top["shared_symbols"]) > 0


class TestTop2Limit:
    """Test that results are limited to top 2."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "1")

    @pytest.fixture
    def db_with_class(self, tmp_path):
        db_path = str(tmp_path / "graph.db")
        conn = _create_graph_db(db_path)

        source_file = tmp_path / "processor.py"
        source_file.write_text(SAMPLE_CLASS_SOURCE)
        fpath = str(source_file)

        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, language) "
            "VALUES (1, 'Class', 'ItemProcessor', ?, 1, 48, 'python')",
            (fpath,),
        )

        methods = [
            (7, "process_item", 19, 23, "(self, item)"),
            (8, "process_batch", 25, 32, "(self, items)"),
            (9, "transform", 34, 35, "(self, item)"),
            (10, "validate_item", 37, 42, "(self, item)"),
        ]
        for mid, mname, start, end, sig in methods:
            conn.execute(
                "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
                "signature, language, parent_id) "
                "VALUES (?, 'Method', ?, ?, ?, ?, ?, 'python', 1)",
                (mid, mname, fpath, start, end, sig),
            )
        conn.commit()
        conn.close()
        return db_path, fpath

    def test_max_two_results(self, db_with_class) -> None:
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path, fpath = db_with_class

        results = select_siblings_v2(db_path, fpath, "process_item", "")
        assert len(results) <= 2


class TestStructuredOutput:
    """Test the structured table output format."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "1")

    def test_format_sibling_table(self) -> None:
        from groundtruth.evidence.sibling_v2 import format_sibling_table
        siblings = [
            {"name": "process_item", "shared_symbols": ["queue", "result"], "return_type": "list"},
            {"name": "validate_item", "shared_symbols": ["item"], "return_type": "bool"},
        ]
        table = format_sibling_table(siblings)
        assert "[SIBLING PATTERN]" in table
        assert "process_item" in table
        assert "queue, result" in table
        assert "list" in table
        assert "validate_item" in table
        assert "| name |" in table  # header

    def test_format_empty_returns_empty(self) -> None:
        from groundtruth.evidence.sibling_v2 import format_sibling_table
        table = format_sibling_table([])
        assert table == ""


class TestSiblingV2FeatureFlag:
    """Test that GT_SIBLING_SELECTOR_V2_ENABLED=0 produces no output."""

    def test_select_disabled_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "0")
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path = str(tmp_path / "graph.db")
        _create_graph_db(db_path).close()
        result = select_siblings_v2(db_path, "test.py", "func", "")
        assert result == []

    def test_format_disabled_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_SIBLING_SELECTOR_V2_ENABLED", "0")
        from groundtruth.evidence.sibling_v2 import format_sibling_table
        siblings = [{"name": "x", "shared_symbols": [], "return_type": "str"}]
        result = format_sibling_table(siblings)
        assert result == ""

    def test_default_is_disabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.delenv("GT_SIBLING_SELECTOR_V2_ENABLED", raising=False)
        from groundtruth.evidence.sibling_v2 import select_siblings_v2
        db_path = str(tmp_path / "graph.db")
        _create_graph_db(db_path).close()
        result = select_siblings_v2(db_path, "test.py", "func", "")
        assert result == []
