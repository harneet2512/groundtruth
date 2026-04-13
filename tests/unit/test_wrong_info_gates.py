from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.contracts.engine import ContractEngine
from groundtruth.contracts.extractors.exception_extractor import ExceptionExtractor
from groundtruth.contracts.extractors.negative_extractor import NegativeExtractor
from groundtruth.contracts.extractors.roundtrip_extractor import RoundtripExtractor
from groundtruth.substrate.graph_reader_impl import GraphStoreReader


class _FakeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.connection = conn

    def get_sibling_functions(self, node_id: int) -> list[dict]:
        return []

    def get_properties(self, node_id: int, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.connection.execute(
                "SELECT id, node_id, kind, value, line, confidence FROM properties WHERE node_id = ? AND kind = ?",
                (node_id, kind),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT id, node_id, kind, value, line, confidence FROM properties WHERE node_id = ?",
                (node_id,),
            ).fetchall()
        return [
            {"id": row[0], "node_id": row[1], "kind": row[2], "value": row[3], "line": row[4], "confidence": row[5]}
            for row in rows
        ]

    def get_assertions(self, test_node_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id, test_node_id, target_node_id, kind, expression, expected, line FROM assertions WHERE test_node_id = ?",
            (test_node_id,),
        ).fetchall()
        return [
            {"id": row[0], "test_node_id": row[1], "target_node_id": row[2], "kind": row[3], "expression": row[4], "expected": row[5], "line": row[6]}
            for row in rows
        ]

    def get_assertions_for_target(self, target_name: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT a.id, a.test_node_id, a.target_node_id, a.kind, a.expression, a.expected, a.line, n.file_path
            FROM assertions a
            JOIN nodes n ON a.test_node_id = n.id
            WHERE a.expression LIKE ? OR a.expected LIKE ?
            """,
            (f"%{target_name}%", f"%{target_name}%"),
        ).fetchall()
        return [
            {
                "id": row[0],
                "test_node_id": row[1],
                "target_node_id": row[2],
                "kind": row[3],
                "expression": row[4],
                "expected": row[5],
                "line": row[6],
                "file_path": row[7],
            }
            for row in rows
        ]

    def get_all_files(self) -> list[str]:
        return []


def _make_graph_db(tmp_path: Path) -> GraphStoreReader:
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
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
            confidence REAL DEFAULT 1.0,
            metadata TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            node_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            value TEXT,
            line INTEGER,
            confidence REAL DEFAULT 1.0
        );
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY,
            test_node_id INTEGER,
            target_node_id INTEGER,
            kind TEXT,
            expression TEXT,
            expected TEXT,
            line INTEGER
        );
        """
    )
    return GraphStoreReader(_FakeStore(conn))


def test_roundtrip_ignores_naming_only_tests(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Function', 'encode', 'mod.encode', 'src/mod.py', 1, 10, 'def encode(x)', 'str', 1, 0, 'python', NULL),
          (2, 'Function', 'decode', 'mod.decode', 'src/mod.py', 12, 20, 'def decode(x)', 'str', 1, 0, 'python', NULL),
          (3, 'Function', 'test_encode', 'tests.test_encode', 'tests/test_mod.py', 1, 10, 'def test_encode()', NULL, 0, 1, 'python', NULL);
        """
    )

    contracts = RoundtripExtractor().extract(reader, 1)
    runtime_contracts = [
        contract for contract in ContractEngine(reader).extract_all(1)
        if contract.contract_type == "roundtrip"
    ]

    assert len(contracts) == 1
    assert contracts[0].tier == "possible"
    assert runtime_contracts == []


def test_exception_extractor_rejects_invalid_property_exception_name(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Function', 'validate', 'mod.validate', 'src/mod.py', 1, 10, 'def validate(x)', 'bool', 1, 0, 'python', NULL);
        INSERT INTO properties VALUES
          (1, 1, 'exception_type', 'ErrorType', 4, 1.0),
          (2, 1, 'raise_type', 'ValidationError', 5, 1.0);
        """
    )

    contracts = ExceptionExtractor().extract(reader, 1)

    assert len(contracts) == 1
    assert contracts[0].predicate == "raises ValidationError"


def test_negative_extractor_rejects_invalid_assertion_exception_name(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Function', 'validate', 'mod.validate', 'src/mod.py', 1, 10, 'def validate(x)', 'bool', 1, 0, 'python', NULL),
          (2, 'Function', 'test_validate', 'tests.test_validate', 'tests/test_mod.py', 1, 10, 'def test_validate()', NULL, 0, 1, 'python', NULL);
        INSERT INTO assertions VALUES
          (1, 2, 1, 'assertRaises', 'assertRaises(ErrorType, validate, x)', 'ErrorType', 7);
        """
    )

    contracts = NegativeExtractor().extract(reader, 1)

    assert contracts == []


def test_graph_reader_exact_then_unique_suffix_then_ambiguous(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Function', 'foo', 'pkg.foo', 'src/a/foo.py', 1, 10, 'def foo()', 'int', 1, 0, 'python', NULL),
          (2, 'Function', 'foo', 'pkg.foo', 'lib/a/foo.py', 1, 10, 'def foo()', 'int', 1, 0, 'python', NULL),
          (3, 'Function', 'bar', 'pkg.bar', 'src/only/bar.py', 1, 10, 'def bar()', 'int', 1, 0, 'python', NULL);
        """
    )

    exact = reader.get_node_by_name("foo", "src/a/foo.py")
    unique_suffix = reader.get_node_by_name("bar", "only/bar.py")
    ambiguous_suffix = reader.get_node_by_name("foo", "a/foo.py")

    assert exact is not None and exact["file_path"] == "src/a/foo.py"
    assert unique_suffix is not None and unique_suffix["file_path"] == "src/only/bar.py"
    assert ambiguous_suffix is None


def test_graph_reader_resolves_qualified_name(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Method', '__init__', 'RST.__init__', 'src/rst.py', 10, 20, 'def __init__(self)', NULL, 1, 0, 'python', NULL),
          (2, 'Method', '__init__', 'Config.__init__', 'src/config.py', 5, 12, 'def __init__(self)', NULL, 1, 0, 'python', NULL);
        """
    )

    scoped = reader.get_node_by_name("RST.__init__", "src/rst.py")
    unscoped = reader.get_node_by_name("RST.__init__")

    assert scoped is not None and scoped["file_path"] == "src/rst.py"
    assert unscoped is not None and unscoped["file_path"] == "src/rst.py"


def test_graph_reader_nodes_in_file_abstains_on_ambiguous_suffix(tmp_path: Path) -> None:
    reader = _make_graph_db(tmp_path)
    conn = reader._conn
    conn.executescript(
        """
        INSERT INTO nodes VALUES
          (1, 'Function', 'foo', 'pkg.foo', 'src/a/foo.py', 1, 10, 'def foo()', 'int', 1, 0, 'python', NULL),
          (2, 'Function', 'bar', 'pkg.bar', 'lib/a/foo.py', 1, 10, 'def bar()', 'int', 1, 0, 'python', NULL);
        """
    )

    ambiguous = reader.get_nodes_in_file("a/foo.py")
    exact = reader.get_nodes_in_file("src/a/foo.py")

    assert ambiguous == []
    assert len(exact) == 1
    assert exact[0]["file_path"] == "src/a/foo.py"
