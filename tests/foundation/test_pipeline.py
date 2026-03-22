"""Tests for the Foundation v2 integration pipeline.

Tests cover:
- Pipeline produces obligation candidates from similarity + graph data
- Freshness filtering suppresses stale candidates
- enhance_obligations is additive (never removes existing)
- Flag-OFF parity: when foundation is disabled, behavior is identical
- Deduplication: foundation candidates don't duplicate existing obligations
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.foundation.graph.expander import GraphExpander
from groundtruth.foundation.integration.pipeline import (
    PipelineResult,
    enhance_obligations,
    run_pipeline,
)
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.index.store import SymbolStore
from groundtruth.validators.obligations import Obligation


# ---- Fixtures ----


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory DB with both GT schema and foundation schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Core GT tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            language TEXT NOT NULL,
            file_path TEXT NOT NULL,
            line_number INTEGER,
            end_line INTEGER,
            is_exported BOOLEAN DEFAULT FALSE,
            signature TEXT,
            params TEXT,
            return_type TEXT,
            documentation TEXT,
            usage_count INTEGER DEFAULT 0,
            last_indexed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER REFERENCES symbols(id),
            referenced_in_file TEXT NOT NULL,
            referenced_at_line INTEGER,
            reference_type TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attributes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER REFERENCES symbols(id),
            name TEXT NOT NULL,
            method_ids TEXT
        );
        CREATE TABLE IF NOT EXISTS exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER,
            module_path TEXT NOT NULL,
            is_default BOOLEAN DEFAULT FALSE,
            is_named BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            version TEXT,
            package_manager TEXT NOT NULL,
            is_dev_dependency BOOLEAN DEFAULT FALSE,
            UNIQUE(name, package_manager)
        );
    """)

    # Insert test symbols
    conn.executescript("""
        INSERT INTO symbols (id, name, kind, language, file_path, line_number, end_line, is_exported, last_indexed_at)
        VALUES
            (1, 'UserService', 'class', 'python', 'src/users/service.py', 0, 50, 1, 1000),
            (2, '__init__', 'method', 'python', 'src/users/service.py', 1, 5, 0, 1000),
            (3, 'get_user', 'method', 'python', 'src/users/service.py', 7, 15, 1, 1000),
            (4, 'update_user', 'method', 'python', 'src/users/service.py', 17, 25, 1, 1000),
            (5, '__eq__', 'method', 'python', 'src/users/service.py', 27, 30, 0, 1000),
            (6, 'helper_func', 'function', 'python', 'src/utils/helpers.py', 0, 10, 1, 1000),
            (7, 'test_user', 'function', 'python', 'tests/test_users.py', 0, 20, 1, 1000);

        INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type)
        VALUES
            (3, 'src/routes/users.py', 10, 'call'),
            (3, 'tests/test_users.py', 5, 'call'),
            (6, 'src/users/service.py', 12, 'call');
    """)

    return conn


@pytest.fixture
def test_db():
    conn = _create_test_db()
    yield conn
    conn.close()


@pytest.fixture
def repr_store(test_db):
    return RepresentationStore(test_db)


@pytest.fixture
def graph_expander(test_db):
    store = SymbolStore.__new__(SymbolStore)
    store._conn = test_db
    return GraphExpander(store)


# ---- Pipeline tests ----


class TestRunPipeline:
    def test_returns_pipeline_result(self, repr_store, graph_expander):
        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        assert isinstance(result, PipelineResult)
        assert result.latency_ms >= 0

    def test_graph_expansion_finds_candidates(self, repr_store, graph_expander):
        """Graph expansion should find connected symbols even without similarity data."""
        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        # Graph expansion should find something (callers, same-class, etc.)
        assert result.graph_expanded > 0

    def test_with_similarity_data(self, repr_store, graph_expander):
        """When similarity data exists, pipeline uses it."""
        # Import extractors to register them
        import groundtruth.foundation.similarity.fingerprint  # noqa: F401
        import groundtruth.foundation.similarity.astvec  # noqa: F401
        import groundtruth.foundation.similarity.tokensketch  # noqa: F401

        # Store some representations for symbols 3 and 4
        blob = b"\x00" * 31  # fingerprint
        repr_store.store_representation(3, "fingerprint_v1", "1.0", blob, None, "h1", 1)
        repr_store.store_representation(4, "fingerprint_v1", "1.0", blob, None, "h2", 1)

        # Store metadata
        repr_store.store_metadata(3, "method", "src/users/service.py", "python", class_name="UserService")
        repr_store.store_metadata(4, "method", "src/users/service.py", "python", class_name="UserService")

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        # Should find candidates from similarity and/or graph
        assert result.similarity_candidates > 0 or result.graph_expanded > 0

    def test_freshness_filtering(self, repr_store, graph_expander):
        """Stale files should have their candidates suppressed."""
        repr_store.store_metadata(
            4, "method", "src/users/service.py", "python", class_name="UserService"
        )

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
            stale_files={"src/users/service.py"},
        )
        # All candidates from the stale file should be filtered
        for cand in result.candidates:
            assert cand.target_file != "src/users/service.py"

    def test_max_candidates_respected(self, repr_store, graph_expander):
        result = run_pipeline(
            symbol_id=1,
            symbol_name="UserService",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
            max_candidates=2,
        )
        assert len(result.candidates) <= 2

    def test_nonexistent_symbol(self, repr_store, graph_expander):
        """Pipeline should handle nonexistent symbols gracefully."""
        result = run_pipeline(
            symbol_id=999,
            symbol_name="nonexistent",
            file_path="nowhere.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        assert isinstance(result, PipelineResult)
        assert result.candidates == []

    def test_candidates_have_similarity_sourced_kind(self, repr_store, graph_expander):
        """All foundation candidates should be tagged as similarity_sourced."""
        # Store metadata so candidates can be resolved
        for sid in range(1, 8):
            meta = repr_store.get_metadata(sid)
            if meta is None:
                repr_store.store_metadata(sid, "method", f"file_{sid}.py", "python")

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        for cand in result.candidates:
            assert cand.kind == "similarity_sourced"
            assert "[foundation]" in cand.reason

    def test_confidence_capped_below_attribute_traced(self, repr_store, graph_expander):
        """Foundation confidence should never exceed 0.7 (below attribute-traced)."""
        for sid in range(1, 8):
            repr_store.store_metadata(sid, "method", f"file_{sid}.py", "python")

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        for cand in result.candidates:
            assert cand.confidence <= 0.7


# ---- enhance_obligations tests ----


class TestEnhanceObligations:
    def test_additive_never_removes(self, repr_store, graph_expander):
        """Foundation candidates are additive — existing obligations are preserved."""
        existing = [
            Obligation(
                kind="shared_state",
                source="get_user",
                target="update_user",
                target_file="src/users/service.py",
                target_line=17,
                reason="shared attribute: self.db",
                confidence=0.9,
            )
        ]

        enhanced = enhance_obligations(
            existing_obligations=existing,
            symbol_name="get_user",
            symbol_id=3,
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )

        # Original obligation is preserved
        assert any(o.kind == "shared_state" and o.target == "update_user" for o in enhanced)
        assert len(enhanced) >= len(existing)

    def test_deduplication(self, repr_store, graph_expander):
        """Foundation candidates that overlap with existing are not duplicated."""
        for sid in range(1, 8):
            repr_store.store_metadata(
                sid, "method", f"file_{sid}.py", "python"
            )

        existing = [
            Obligation(
                kind="caller_contract",
                source="get_user",
                target="symbol_id:6",  # same target as a possible foundation candidate
                target_file="file_6.py",
                target_line=0,
                reason="caller",
                confidence=0.8,
            )
        ]

        enhanced = enhance_obligations(
            existing_obligations=existing,
            symbol_name="get_user",
            symbol_id=3,
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )

        # Count how many have target "symbol_id:6" + "file_6.py"
        dupes = [o for o in enhanced if o.target == "symbol_id:6" and o.target_file == "file_6.py"]
        assert len(dupes) == 1  # not duplicated

    def test_returns_existing_when_no_foundation(self):
        """When foundation components are None, returns existing unchanged."""
        existing = [
            Obligation("shared_state", "a", "b", "f.py", 1, "reason", 0.9)
        ]
        result = enhance_obligations(
            existing_obligations=existing,
            symbol_name="a",
            symbol_id=None,
            file_path="f.py",
            repr_store=None,
            graph_expander=None,
        )
        assert result == existing

    def test_returns_existing_when_symbol_id_none(self, repr_store, graph_expander):
        """When symbol_id is None (unresolved), returns existing unchanged."""
        existing = [Obligation("x", "a", "b", "f.py", 1, "r", 0.5)]
        result = enhance_obligations(
            existing_obligations=existing,
            symbol_name="a",
            symbol_id=None,
            file_path="f.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        assert result == existing


# ---- Flag-OFF parity ----


class TestFlagParity:
    """When GT_ENABLE_FOUNDATION is not set, the foundation pipeline is never invoked.

    This is tested by verifying that enhance_obligations with None components
    returns the exact same obligations, and that the flag check in the
    integration point would skip the pipeline entirely.
    """

    def test_enhance_with_none_is_identity(self):
        """enhance_obligations(existing, ..., repr_store=None, graph_expander=None)
        returns existing unchanged."""
        obligations = [
            Obligation("constructor_symmetry", "Foo.__init__", "__eq__", "a.py", 10, "r", 0.95),
            Obligation("shared_state", "Foo.get", "Foo.set", "a.py", 20, "r", 0.85),
            Obligation("caller_contract", "bar", "baz", "b.py", 5, "r", 0.7),
        ]
        result = enhance_obligations(
            existing_obligations=obligations,
            symbol_name="Foo",
            symbol_id=1,
            file_path="a.py",
            repr_store=None,
            graph_expander=None,
        )
        assert result is obligations  # same object, not just equal
        assert len(result) == 3

    def test_flag_check_pattern(self):
        """Verify the flag exists and defaults to OFF."""
        from groundtruth.core.flags import foundation_enabled
        import os

        # Ensure flag is not set
        os.environ.pop("GT_ENABLE_FOUNDATION", None)
        assert foundation_enabled() is False

    def test_existing_obligation_kinds_unchanged(self, repr_store, graph_expander):
        """Foundation-sourced obligations use 'similarity_sourced' kind,
        never overwriting existing kind values."""
        existing = [
            Obligation("constructor_symmetry", "A", "B", "f.py", 1, "r", 0.9),
        ]

        for sid in range(1, 8):
            repr_store.store_metadata(sid, "method", f"file_{sid}.py", "python")

        enhanced = enhance_obligations(
            existing_obligations=existing,
            symbol_name="get_user",
            symbol_id=3,
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )

        # Original obligation kind is untouched
        assert enhanced[0].kind == "constructor_symmetry"
        # Any new ones are similarity_sourced
        for o in enhanced[1:]:
            assert o.kind == "similarity_sourced"


# ---- Process metrics ----


class TestProcessMetrics:
    def test_metrics_populated(self, repr_store, graph_expander):
        for sid in range(1, 8):
            repr_store.store_metadata(sid, "method", f"file_{sid}.py", "python")

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        assert result.latency_ms >= 0  # may be 0.0 on fast in-memory DBs
        assert isinstance(result.similarity_candidates, int)
        assert isinstance(result.graph_expanded, int)
        assert isinstance(result.freshness_filtered, int)
        assert isinstance(result.validation_passed, int)

    def test_evidence_attached(self, repr_store, graph_expander):
        for sid in range(1, 8):
            repr_store.store_metadata(sid, "method", f"file_{sid}.py", "python")

        result = run_pipeline(
            symbol_id=3,
            symbol_name="get_user",
            file_path="src/users/service.py",
            repr_store=repr_store,
            graph_expander=graph_expander,
        )
        # Each candidate should have corresponding evidence
        assert len(result.evidence) == len(result.candidates)
