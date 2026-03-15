"""Tests for the grounding record system."""

from __future__ import annotations

import time

import pytest

from groundtruth.grounding.record import (
    Evidence,
    GroundingRecord,
    build_grounding_record,
)
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


@pytest.fixture()
def store() -> SymbolStore:
    """Create an in-memory store with test symbols."""
    s = SymbolStore(":memory:")
    s.initialize()
    now = int(time.time())

    # Insert symbols
    result = s.insert_symbol(
        name="get_user_by_id",
        kind="function",
        language="python",
        file_path="src/users/queries.py",
        line_number=5,
        end_line=15,
        is_exported=True,
        signature="(user_id: int) -> User",
        params=None,
        return_type="User",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(result, Ok)

    result = s.insert_symbol(
        name="NotFoundError",
        kind="class",
        language="python",
        file_path="src/utils/errors.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(result, Ok)

    result = s.insert_symbol(
        name="hash_password",
        kind="function",
        language="python",
        file_path="src/utils/crypto.py",
        line_number=1,
        end_line=5,
        is_exported=True,
        signature="(password: str) -> str",
        params=None,
        return_type="str",
        documentation=None,
        last_indexed_at=now,
    )
    assert isinstance(result, Ok)

    s.insert_package("flask", "3.0.0", "pip")
    return s


class TestEvidence:
    """Test Evidence dataclass."""

    def test_evidence_creation(self) -> None:
        ev = Evidence(
            type="symbol_resolved",
            source="symbol_store",
            assertion="'get_user_by_id' exists in src/users/queries.py",
            verified=True,
            detail="kind=function, line=5",
        )
        assert ev.type == "symbol_resolved"
        assert ev.verified is True

    def test_evidence_is_frozen(self) -> None:
        ev = Evidence(
            type="symbol_resolved",
            source="ast_validator",
            assertion="test",
            verified=True,
        )
        with pytest.raises(AttributeError):
            ev.verified = False  # type: ignore[misc]


class TestGroundingRecord:
    """Test GroundingRecord dataclass."""

    def test_to_dict(self) -> None:
        record = GroundingRecord(
            target_file="src/app.py",
            target_symbols=["get_user_by_id"],
            evidence=[
                Evidence(
                    type="symbol_resolved",
                    source="symbol_store",
                    assertion="exists",
                    verified=True,
                ),
                Evidence(
                    type="import_valid",
                    source="ast_validator",
                    assertion="wrong path",
                    verified=False,
                ),
            ],
            confidence=0.5,
            violated_invariants=["wrong path"],
        )
        d = record.to_dict()
        assert d["evidence_count"] == 2
        assert d["verified_count"] == 1
        assert d["violated_count"] == 1
        assert d["confidence"] == 0.5
        assert len(d["evidence"]) == 2

    def test_empty_record(self) -> None:
        record = GroundingRecord(target_file="test.py", target_symbols=[])
        d = record.to_dict()
        assert d["evidence_count"] == 0
        assert d["confidence"] == 1.0


class TestBuildGroundingRecord:
    """Test build_grounding_record()."""

    def test_valid_python_imports(self, store: SymbolStore) -> None:
        code = "from users.queries import get_user_by_id\n\nresult = get_user_by_id(1)\n"
        record = build_grounding_record(code, "src/app.py", store, language="python")
        assert record.target_file == "src/app.py"
        assert "get_user_by_id" in record.target_symbols
        # Should have some evidence entries
        assert len(record.evidence) > 0

    def test_invalid_import_produces_violation(self, store: SymbolStore) -> None:
        code = "from users.queries import nonexistent_func\n"
        record = build_grounding_record(code, "src/app.py", store, language="python")
        assert len(record.violated_invariants) > 0
        assert record.confidence < 1.0
        # Should have evidence showing the violation
        failed = [e for e in record.evidence if not e.verified]
        assert len(failed) > 0

    def test_missing_package_produces_violation(self, store: SymbolStore) -> None:
        code = "import numpy\n"
        record = build_grounding_record(code, "src/app.py", store, language="python")
        assert len(record.violated_invariants) > 0
        failed = [e for e in record.evidence if not e.verified]
        assert len(failed) > 0
        assert any(e.type == "package_available" for e in failed)

    def test_wrong_signature_produces_violation(self, store: SymbolStore) -> None:
        code = (
            "from users.queries import get_user_by_id\n\n"
            "result = get_user_by_id(1, True)\n"
        )
        record = build_grounding_record(code, "src/app.py", store, language="python")
        sig_violations = [
            e for e in record.evidence
            if not e.verified and e.type == "signature_match"
        ]
        assert len(sig_violations) > 0

    def test_unsupported_language_returns_empty(self, store: SymbolStore) -> None:
        code = "package main\n\nimport \"fmt\"\n"
        record = build_grounding_record(code, "main.go", store, language="go")
        assert record.confidence == 1.0
        assert len(record.evidence) == 0

    def test_confidence_all_valid(self, store: SymbolStore) -> None:
        code = "import flask\n"
        record = build_grounding_record(code, "src/app.py", store, language="python")
        # flask is in packages, so no errors
        assert record.confidence == 1.0
        assert len(record.violated_invariants) == 0
