"""Tests for InterventionTracker and StatsReporter."""

from __future__ import annotations

import json

from groundtruth.index.store import SymbolStore
from groundtruth.stats.reporter import StatsReporter
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.result import Ok


def _make_tracker() -> tuple[InterventionTracker, SymbolStore]:
    store = SymbolStore(":memory:")
    store.initialize()
    tracker = InterventionTracker(store)
    return tracker, store


class TestInterventionTracker:
    def test_record_intervention(self) -> None:
        tracker, _store = _make_tracker()
        result = tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="fixed_deterministic",
            file_path="src/foo.py",
            language="python",
            errors_found=2,
            errors_fixed=1,
        )
        assert isinstance(result, Ok)
        assert result.value is None

    def test_get_stats_empty(self) -> None:
        tracker, _store = _make_tracker()
        result = tracker.get_stats()
        assert isinstance(result, Ok)
        stats = result.value
        assert stats.total == 0
        assert stats.hallucinations_caught == 0
        assert stats.ai_calls == 0
        assert stats.tokens_used == 0

    def test_get_stats_aggregation(self) -> None:
        tracker, _store = _make_tracker()

        tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="fixed_deterministic",
            errors_found=2,
            tokens_used=100,
        )
        tracker.record(
            tool="groundtruth_brief",
            phase="brief",
            outcome="valid",
            ai_called=True,
            tokens_used=200,
        )
        tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="fixed_ai",
            ai_called=True,
            tokens_used=150,
        )

        result = tracker.get_stats()
        assert isinstance(result, Ok)
        stats = result.value
        assert stats.total == 3
        assert stats.hallucinations_caught == 2  # non-'valid' outcomes
        assert stats.ai_calls == 2
        assert stats.tokens_used == 450

    def test_error_types_json_serialization(self) -> None:
        tracker, store = _make_tracker()
        tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="fixed_deterministic",
            error_types=["wrong_module_path", "missing_package"],
        )
        # Verify the JSON was stored correctly
        cursor = store.connection.execute(
            "SELECT error_types FROM interventions ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        assert row is not None
        parsed = json.loads(row["error_types"])
        assert parsed == ["wrong_module_path", "missing_package"]

    def test_record_with_none_error_types(self) -> None:
        tracker, store = _make_tracker()
        tracker.record(
            tool="groundtruth_trace",
            phase="trace",
            outcome="valid",
            error_types=None,
        )
        cursor = store.connection.execute(
            "SELECT error_types FROM interventions ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["error_types"] is None


class TestStatsReporter:
    def test_generate_report(self) -> None:
        tracker, _store = _make_tracker()
        tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="fixed_deterministic",
            tokens_used=100,
        )
        tracker.record(
            tool="groundtruth_brief",
            phase="brief",
            outcome="valid",
            ai_called=True,
            tokens_used=200,
        )

        reporter = StatsReporter(tracker)
        result = reporter.generate_report()
        assert isinstance(result, Ok)
        report = result.value
        assert "Total interventions:" in report
        assert "2" in report
        assert "Hallucinations caught:" in report
        assert "AI calls:" in report
        assert "Tokens used:" in report

    def test_reporter_empty_stats(self) -> None:
        tracker, _store = _make_tracker()
        reporter = StatsReporter(tracker)
        result = reporter.generate_report()
        assert isinstance(result, Ok)
        report = result.value
        assert "Total interventions:" in report
        assert "0" in report
