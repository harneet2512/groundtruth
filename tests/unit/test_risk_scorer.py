"""Tests for the hallucination risk scorer."""

from __future__ import annotations

import json

from groundtruth.analysis.risk_scorer import RiskScorer, _detect_naming_convention
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


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


class TestDetectNamingConvention:
    def test_snake_case(self) -> None:
        assert _detect_naming_convention("get_user_by_id") == "snake_case"

    def test_camel_case(self) -> None:
        assert _detect_naming_convention("getUserById") == "camelCase"

    def test_pascal_case(self) -> None:
        assert _detect_naming_convention("UserService") == "PascalCase"

    def test_other(self) -> None:
        assert _detect_naming_convention("") == "other"


class TestRiskScorerFile:
    def test_empty_file_zero_risk(self, in_memory_store: SymbolStore) -> None:
        """File with no symbols → zero risk."""
        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/empty.py")
        assert isinstance(result, Ok)
        assert result.value.overall_risk == 0.0

    def test_naming_ambiguity(self, in_memory_store: SymbolStore) -> None:
        """Similar names (edit distance ≤ 3) increase naming_ambiguity."""
        _insert_sym(in_memory_store, "getUser", "src/a.py")
        _insert_sym(in_memory_store, "getUsers", "src/b.py")  # distance 1

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/a.py")
        assert isinstance(result, Ok)
        assert result.value.factors["naming_ambiguity"] > 0.0

    def test_convention_variance(self, in_memory_store: SymbolStore) -> None:
        """Mixed naming conventions in same file increase convention_variance."""
        _insert_sym(in_memory_store, "get_user", "src/mixed.py")
        _insert_sym(in_memory_store, "getUserById", "src/mixed.py")

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/mixed.py")
        assert isinstance(result, Ok)
        assert result.value.factors["convention_variance"] > 0.0

    def test_parameter_complexity(self, in_memory_store: SymbolStore) -> None:
        """Functions with many params increase parameter_complexity."""
        params = json.dumps([
            {"name": "a", "type": "int"},
            {"name": "b", "type": "str"},
            {"name": "c", "type": "bool"},
            {"name": "d", "type": "float"},
            {"name": "e", "type": "list"},
        ])
        _insert_sym(in_memory_store, "complex_func", "src/complex.py", params=params)

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/complex.py")
        assert isinstance(result, Ok)
        assert result.value.factors["parameter_complexity"] == 1.0

    def test_isolation_score(self, in_memory_store: SymbolStore) -> None:
        """Exported symbols with zero usage increase isolation_score."""
        _insert_sym(in_memory_store, "unused_func", "src/iso.py", usage_count=0)
        _insert_sym(in_memory_store, "used_func", "src/iso.py", usage_count=5)

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/iso.py")
        assert isinstance(result, Ok)
        assert result.value.factors["isolation_score"] == 0.5

    def test_overloaded_paths(self, in_memory_store: SymbolStore) -> None:
        """Multiple files with same base name increase overloaded_paths."""
        _insert_sym(in_memory_store, "func1", "src/auth.py")
        _insert_sym(in_memory_store, "func2", "src/middleware/auth.py")

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_file("src/auth.py")
        assert isinstance(result, Ok)
        assert result.value.factors["overloaded_paths"] > 0.0


class TestRiskScorerSymbol:
    def test_score_symbol(self, in_memory_store: SymbolStore) -> None:
        """Score a symbol returns risk for each instance."""
        _insert_sym(in_memory_store, "helper", "src/a.py")
        _insert_sym(in_memory_store, "helper", "src/b.py")

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_symbol("helper")
        assert isinstance(result, Ok)
        assert len(result.value) == 2


class TestRiskScorerCodebase:
    def test_score_codebase(self, in_memory_store: SymbolStore) -> None:
        """Score codebase returns files ranked by risk."""
        _insert_sym(in_memory_store, "safe", "src/safe.py", usage_count=10)
        _insert_sym(in_memory_store, "get_user", "src/risky.py")
        _insert_sym(in_memory_store, "getUserById", "src/risky.py")

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_codebase()
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_score_codebase_limit(self, in_memory_store: SymbolStore) -> None:
        """Limit parameter caps codebase results."""
        for i in range(5):
            _insert_sym(in_memory_store, f"sym{i}", f"src/f{i}.py")

        scorer = RiskScorer(in_memory_store)
        result = scorer.score_codebase(limit=2)
        assert isinstance(result, Ok)
        assert len(result.value) == 2
