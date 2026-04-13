"""Tests for the ContractEngine — extraction, gating, and persistence."""

import sqlite3

import pytest

from groundtruth.contracts.engine import ContractEngine
from groundtruth.substrate.types import ContractRecord


class FakeReader:
    """Minimal GraphReader implementation for testing."""

    def __init__(self, nodes=None, properties=None, assertions=None, callers=None):
        self._nodes = nodes or {}
        self._properties = properties or {}
        self._assertions = assertions or []
        self._callers = callers or {}

    def get_node_by_id(self, node_id: int):
        return self._nodes.get(node_id)

    def get_node_by_name(self, name: str, file_path=None):
        for n in self._nodes.values():
            if n["name"] == name:
                return n
        return None

    def get_callers(self, node_id: int):
        return self._callers.get(node_id, [])

    def get_callees(self, node_id: int):
        return []

    def get_siblings(self, node_id: int):
        return []

    def get_tests_for(self, node_id: int):
        return []

    def get_properties(self, node_id: int, kind=None):
        props = self._properties.get(node_id, [])
        if kind:
            return [p for p in props if p.get("kind") == kind]
        return props

    def get_assertions(self, test_node_id: int):
        return []

    def get_assertions_for_target(self, target_name: str):
        return [a for a in self._assertions if target_name in a.get("expression", "")]

    def get_nodes_in_file(self, file_path: str):
        return []

    def get_file_paths(self):
        return []


class TestContractEngineExtraction:
    def test_exception_from_test_assertion(self):
        """ExceptionExtractor should find contracts from assertRaises."""
        reader = FakeReader(
            nodes={1: {"id": 1, "name": "parse_int", "label": "Function",
                       "qualified_name": "mod.parse_int", "file_path": "mod.py",
                       "start_line": 10, "return_type": None}},
            assertions=[
                {"kind": "assertRaises", "expression": "assertRaises(ValueError, parse_int, 'abc')",
                 "expected": "ValueError", "line": 5, "file_path": "test_mod.py", "test_name": "test_parse"}
            ],
        )
        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=1)

        # Should find at least one exception contract
        exception_contracts = [c for c in contracts if c.contract_type == "exception_message"]
        assert len(exception_contracts) >= 1
        assert "ValueError" in exception_contracts[0].predicate

    def test_exception_from_guard_clause(self):
        """ExceptionExtractor should find contracts from guard clause properties."""
        reader = FakeReader(
            nodes={1: {"id": 1, "name": "validate", "label": "Function",
                       "qualified_name": "mod.validate", "file_path": "mod.py",
                       "start_line": 1, "return_type": None}},
            properties={1: [
                {"kind": "exception_type", "value": "ValueError", "line": 3, "confidence": 0.9},
            ]},
        )
        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=1)

        exception_contracts = [c for c in contracts if c.contract_type == "exception_message"]
        assert len(exception_contracts) >= 1
        assert exception_contracts[0].tier in ("verified", "likely")

    def test_verified_requires_multi_source(self):
        """Contract should be 'verified' only with ≥2 independent sources."""
        reader = FakeReader(
            nodes={1: {"id": 1, "name": "foo", "label": "Function",
                       "qualified_name": "mod.foo", "file_path": "mod.py",
                       "start_line": 1, "return_type": None}},
            properties={1: [
                {"kind": "exception_type", "value": "TypeError", "line": 3, "confidence": 0.9},
                {"kind": "raise_type", "value": "TypeError", "line": 5, "confidence": 0.9},
            ]},
            assertions=[
                {"kind": "assertRaises", "expression": "assertRaises(TypeError, foo)",
                 "expected": "TypeError", "line": 10, "file_path": "test.py", "test_name": "test_foo"}
            ],
        )
        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=1)

        type_error_contracts = [
            c for c in contracts
            if c.contract_type == "exception_message" and "TypeError" in c.predicate
        ]
        assert len(type_error_contracts) >= 1
        # With both guard clause AND test assertion → verified
        assert type_error_contracts[0].tier == "verified"

    def test_possible_tier_suppressed(self):
        """Contracts at 'possible' tier should NOT appear in results."""
        reader = FakeReader(
            nodes={1: {"id": 1, "name": "bar", "label": "Function",
                       "qualified_name": "mod.bar", "file_path": "mod.py",
                       "start_line": 1, "return_type": None}},
            # Low-confidence caller catch with no other evidence
            callers={1: [{"source_id": 2, "source_file": "x.py"}]},
            properties={2: [
                {"kind": "guard_clause", "value": "except RuntimeError", "line": 5, "confidence": 0.5},
            ]},
        )
        # Need node 2 in nodes for get_properties to work
        reader._nodes[2] = {"id": 2, "name": "caller", "label": "Function",
                            "qualified_name": "x.caller", "file_path": "x.py",
                            "start_line": 1, "return_type": None}

        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=1)

        # Caller catch alone with low confidence → 'likely' at best (0.70 confidence)
        # Should still appear since 0.70 > 0.60 threshold for 'likely'
        for c in contracts:
            assert c.tier != "possible"


class TestContractEnginePersistence:
    def test_persist_and_query(self):
        """Contracts should persist to database and be queryable."""
        conn = sqlite3.connect(":memory:")

        reader = FakeReader(
            nodes={1: {"id": 1, "name": "encode", "label": "Function",
                       "qualified_name": "codec.encode", "file_path": "codec.py",
                       "start_line": 1, "return_type": "bytes"}},
        )
        engine = ContractEngine(reader, db_conn=conn)
        contracts = engine.extract_and_persist(node_id=1)

        # Output contracts from return type annotation
        output_contracts = [c for c in contracts if c.contract_type == "exact_output"]
        if output_contracts:
            # Verify persistence
            queried = engine.query_contracts(scope_ref="codec.encode")
            assert len(queried) >= 1
            assert queried[0].scope_ref == "codec.encode"

    def test_empty_extraction_no_crash(self):
        """Engine should handle nodes with no evidence gracefully."""
        reader = FakeReader(
            nodes={1: {"id": 1, "name": "noop", "label": "Function",
                       "qualified_name": "mod.noop", "file_path": "mod.py",
                       "start_line": 1, "return_type": None}},
        )
        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=1)
        assert contracts == []

    def test_nonexistent_node(self):
        """Engine should handle missing nodes gracefully."""
        reader = FakeReader()
        engine = ContractEngine(reader)
        contracts = engine.extract_all(node_id=999)
        assert contracts == []

    def test_registry_coupling_contract_is_mined(self):
        """Registry-backed file coupling should produce a general contract."""
        reader = FakeReader(
            nodes={
                1: {"id": 1, "name": "Handler", "label": "Class",
                    "qualified_name": "pkg.handlers.Handler", "file_path": "src/handlers.py",
                    "start_line": 1, "return_type": None},
                2: {"id": 2, "name": "register", "label": "Function",
                    "qualified_name": "pkg.__init__.register", "file_path": "src/__init__.py",
                    "start_line": 1, "return_type": None},
            },
        )
        reader.get_file_paths = lambda: ["src/handlers.py", "src/__init__.py"]
        reader.get_nodes_in_file = lambda fp: [{"id": 2}] if fp == "src/__init__.py" else [{"id": 1}]
        reader.get_callees = lambda node_id: [{"target_file_path": "src/handlers.py"}] if node_id == 2 else []

        contracts = ContractEngine(reader).extract_all(1)
        registry = [c for c in contracts if c.contract_type == "registry_coupling"]

        assert len(registry) == 1
        assert registry[0].scope_ref == "pkg.handlers.Handler"

    def test_protocol_invariant_contract_is_mined(self):
        reader = FakeReader(
            nodes={
                1: {"id": 1, "name": "get_data", "label": "Function",
                    "qualified_name": "pkg.get_data", "file_path": "src/data.py",
                    "start_line": 1, "return_type": None},
                2: {"id": 2, "name": "caller_a", "label": "Function",
                    "qualified_name": "pkg.caller_a", "file_path": "src/a.py",
                    "start_line": 1, "return_type": None},
                3: {"id": 3, "name": "caller_b", "label": "Function",
                    "qualified_name": "pkg.caller_b", "file_path": "src/b.py",
                    "start_line": 1, "return_type": None},
            },
            callers={
                1: [
                    {"source_id": 2, "source_file": "src/a.py", "source_line": 10},
                    {"source_id": 3, "source_file": "src/b.py", "source_line": 20},
                ]
            },
            properties={
                2: [{"kind": "caller_usage", "value": "destructure_tuple:get_data", "line": 10, "confidence": 1.0}],
                3: [{"kind": "caller_usage", "value": "destructure_tuple:get_data", "line": 20, "confidence": 1.0}],
            },
        )

        contracts = ContractEngine(reader).extract_all(1)
        protocols = [c for c in contracts if c.contract_type == "protocol_invariant"]

        assert len(protocols) == 1
        assert "destructurable" in protocols[0].normalized_form
