"""Tests for the grounding gap analyzer."""

from __future__ import annotations

import json

from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer, GroundingResult
from groundtruth.index.store import BriefingLogRecord, SymbolStore
from groundtruth.utils.result import Ok


def _make_briefing_log(
    store: SymbolStore,
    symbols: list[str],
    target_file: str | None = None,
) -> BriefingLogRecord:
    """Helper: insert a briefing log and return the record."""
    result = store.insert_briefing_log(
        timestamp=1000,
        intent="test intent",
        briefing_text="test briefing",
        briefing_symbols=symbols,
        target_file=target_file,
    )
    assert isinstance(result, Ok)
    log_id = result.value
    get_result = store.get_briefing_log(log_id)
    assert isinstance(get_result, Ok)
    assert get_result.value is not None
    return get_result.value


class TestGroundingGapAnalyzer:
    def test_all_symbols_used_correctly(self, in_memory_store: SymbolStore) -> None:
        """All briefed symbols appear in code with no errors → 100% compliance."""
        analyzer = GroundingGapAnalyzer(in_memory_store)
        log = _make_briefing_log(in_memory_store, ["getUserById", "NotFoundError"])

        result = analyzer.compare_briefing_to_output(
            log,
            validation_errors=[],
            proposed_code="user = getUserById(id)\nraise NotFoundError()",
        )
        assert isinstance(result, Ok)
        gr = result.value
        assert gr.compliance_rate == 1.0
        assert gr.correct_usages == 2
        assert gr.ignored_symbols == 0
        assert gr.hallucinated_despite_briefing == 0

    def test_symbol_ignored(self, in_memory_store: SymbolStore) -> None:
        """Symbol briefed but not used in code → counted as ignored."""
        analyzer = GroundingGapAnalyzer(in_memory_store)
        log = _make_briefing_log(in_memory_store, ["getUserById", "NotFoundError"])

        result = analyzer.compare_briefing_to_output(
            log,
            validation_errors=[],
            proposed_code="user = getUserById(id)",
        )
        assert isinstance(result, Ok)
        gr = result.value
        assert gr.correct_usages == 1
        assert gr.ignored_symbols == 1
        assert gr.compliance_rate == 0.5

    def test_hallucinated_despite_briefing(self, in_memory_store: SymbolStore) -> None:
        """Symbol in code but has validation error → hallucinated despite briefing."""
        analyzer = GroundingGapAnalyzer(in_memory_store)
        log = _make_briefing_log(in_memory_store, ["getUserById"])

        errors = [{"type": "wrong_module_path", "symbol_name": "getUserById", "message": "not found"}]
        result = analyzer.compare_briefing_to_output(
            log,
            validation_errors=errors,
            proposed_code="from wrong import getUserById",
        )
        assert isinstance(result, Ok)
        gr = result.value
        assert gr.hallucinated_despite_briefing == 1
        assert gr.correct_usages == 0
        assert gr.compliance_rate == 0.0

    def test_empty_briefing_symbols(self, in_memory_store: SymbolStore) -> None:
        """Empty briefing symbols → compliance 1.0 by convention."""
        analyzer = GroundingGapAnalyzer(in_memory_store)
        log = _make_briefing_log(in_memory_store, [])

        result = analyzer.compare_briefing_to_output(log, [], "some code")
        assert isinstance(result, Ok)
        assert result.value.compliance_rate == 1.0

    def test_compliance_persisted_to_store(self, in_memory_store: SymbolStore) -> None:
        """After comparison, compliance data is saved back to the store."""
        analyzer = GroundingGapAnalyzer(in_memory_store)
        log = _make_briefing_log(in_memory_store, ["foo", "bar"])

        analyzer.compare_briefing_to_output(log, [], "foo()")

        updated = in_memory_store.get_briefing_log(log.id)
        assert isinstance(updated, Ok)
        assert updated.value is not None
        assert updated.value.compliance_rate == 0.5
        assert updated.value.symbols_used_correctly == ["foo"]
        assert updated.value.symbols_ignored == ["bar"]

    def test_aggregate_compliance(self, in_memory_store: SymbolStore) -> None:
        """Aggregate stats from multiple briefing logs."""
        analyzer = GroundingGapAnalyzer(in_memory_store)

        # Create two logs with known compliance
        log1 = _make_briefing_log(in_memory_store, ["a", "b"])
        analyzer.compare_briefing_to_output(log1, [], "a() and b()")  # 1.0

        log2 = _make_briefing_log(in_memory_store, ["c", "d"])
        analyzer.compare_briefing_to_output(log2, [], "c()")  # 0.5

        report_result = analyzer.aggregate_compliance()
        assert isinstance(report_result, Ok)
        report = report_result.value
        assert report.total_briefings == 2
        assert report.total_with_validation == 2
        assert report.mean_compliance_rate == 0.75
        assert report.median_compliance_rate == 0.75
