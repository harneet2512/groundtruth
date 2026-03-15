"""Tests for adaptive briefing enhancements."""

from __future__ import annotations

from groundtruth.ai.briefing import BriefingResult
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


def _insert_sym(
    store: SymbolStore,
    name: str,
    file_path: str = "src/a.py",
    kind: str = "function",
    is_exported: bool = True,
    usage_count: int = 0,
) -> int:
    """Helper to insert a symbol and return its ID."""
    result = store.insert_symbol(
        name=name,
        kind=kind,
        language="python",
        file_path=file_path,
        line_number=1,
        end_line=10,
        is_exported=is_exported,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(result, Ok)
    sid = result.value
    if usage_count > 0:
        store.update_usage_count(sid, usage_count)
    return sid


class TestAdaptiveBriefing:
    def test_no_enhancement_for_low_risk(self, in_memory_store: SymbolStore) -> None:
        """Low risk file → briefing unchanged."""
        _insert_sym(in_memory_store, "safe_func", "src/safe.py", usage_count=10)

        scorer = RiskScorer(in_memory_store)
        adaptive = AdaptiveBriefing(in_memory_store, scorer)
        base = BriefingResult(
            briefing="Original briefing.",
            relevant_symbols=[{"name": "safe_func", "file": "src/safe.py"}],
            warnings=[],
        )

        result = adaptive.enhance_briefing(base, "src/safe.py")
        assert isinstance(result, Ok)
        assert result.value.briefing == "Original briefing."

    def test_naming_ambiguity_adds_paths(self, in_memory_store: SymbolStore) -> None:
        """High naming ambiguity → import paths appended + warning added."""
        _insert_sym(in_memory_store, "getUser", "src/target.py")
        _insert_sym(in_memory_store, "getUsers", "src/other.py")  # distance 1

        scorer = RiskScorer(in_memory_store)
        adaptive = AdaptiveBriefing(in_memory_store, scorer)
        base = BriefingResult(
            briefing="Base.",
            relevant_symbols=[{"name": "getUser", "file": "src/target.py"}],
            warnings=[],
        )

        result = adaptive.enhance_briefing(base, "src/target.py")
        assert isinstance(result, Ok)
        br = result.value
        assert "Exact import paths" in br.briefing
        assert any("naming ambiguity" in w for w in br.warnings)

    def test_overloaded_paths_adds_warning(self, in_memory_store: SymbolStore) -> None:
        """Overloaded paths → warning about confusable modules."""
        _insert_sym(in_memory_store, "func1", "src/auth.py")
        _insert_sym(in_memory_store, "func2", "src/middleware/auth.py")

        scorer = RiskScorer(in_memory_store)
        adaptive = AdaptiveBriefing(in_memory_store, scorer)
        base = BriefingResult(briefing="Base.", relevant_symbols=[], warnings=[])

        result = adaptive.enhance_briefing(base, "src/auth.py")
        assert isinstance(result, Ok)
        assert any("similar names" in w for w in result.value.warnings)

    def test_past_failures_appended(self, in_memory_store: SymbolStore) -> None:
        """Past hallucinations for file → negative examples appended."""
        _insert_sym(in_memory_store, "func", "src/target.py")

        # Insert a past briefing log with hallucinations
        log_result = in_memory_store.insert_briefing_log(
            timestamp=1000,
            intent="past intent",
            briefing_text="past briefing",
            briefing_symbols=["badSymbol"],
            target_file="src/target.py",
        )
        assert isinstance(log_result, Ok)
        in_memory_store.update_briefing_compliance(
            log_id=log_result.value,
            compliance_rate=0.0,
            symbols_used_correctly=[],
            symbols_ignored=[],
            hallucinated_despite_briefing=["badSymbol"],
        )

        scorer = RiskScorer(in_memory_store)
        adaptive = AdaptiveBriefing(in_memory_store, scorer)
        base = BriefingResult(briefing="Base.", relevant_symbols=[], warnings=[])

        result = adaptive.enhance_briefing(base, "src/target.py")
        assert isinstance(result, Ok)
        assert "Previously hallucinated" in result.value.briefing
        assert "badSymbol" in result.value.briefing

    def test_import_depth_adds_reexport_warning(self, in_memory_store: SymbolStore) -> None:
        """Deep re-export chains → warning about re-exports."""
        sid = _insert_sym(in_memory_store, "deep_func", "src/deep.py")
        # Add multiple exports to simulate chain depth
        in_memory_store.insert_export(sid, "src/deep")
        in_memory_store.insert_export(sid, "src/reexport")

        scorer = RiskScorer(in_memory_store)
        adaptive = AdaptiveBriefing(in_memory_store, scorer)
        base = BriefingResult(briefing="Base.", relevant_symbols=[], warnings=[])

        result = adaptive.enhance_briefing(base, "src/deep.py")
        assert isinstance(result, Ok)
        assert "re-export" in result.value.briefing
