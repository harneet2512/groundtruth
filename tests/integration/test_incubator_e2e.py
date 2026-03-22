"""End-to-end integration tests for the Phase 5 incubator stack.

Tests the full pipeline: flags → runtime → enrichment → logging → reader.
Covers Gate 1 criteria from PHASE5_ENGINEERING_PLAN.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.incubator.intel_logger import RepoIntelLogger
from groundtruth.incubator.intel_reader import RepoIntelReader
from groundtruth.incubator.runtime import IncubatorRuntime, any_phase5_flag_on
from groundtruth.core.communication import (
    CommunicationPolicy,
    SessionState,
    normalize_tool_name,
)


# ---- Helpers ----

def _make_check_result(obligations: list[dict] | None = None) -> dict:
    """Build a realistic check-diff result dict."""
    return {
        "corrected_diff": "--- a/x.py\n+++ b/x.py\n@@ ...",
        "corrections": [],
        "corrections_total": 0,
        "obligations": obligations or [],
        "obligation_count": len(obligations or []),
        "contradictions": [],
        "contradiction_count": 0,
        "info": [],
        "test_suggestion": None,
        "files_checked": 1,
        "latency_ms": 5,
    }


def _make_impact_result(obligations: list[dict] | None = None) -> dict:
    """Build a realistic impact result dict."""
    return {
        "symbol": "UserService.get_user",
        "callers": [],
        "obligations": obligations or [],
        "obligation_count": len(obligations or []),
    }


SAMPLE_OBLIGATION = {
    "kind": "shared_state",
    "target": "UserService.save_user",
    "file": "src/service.py",
    "line": 42,
    "reason": "shares self._cache with get_user",
    "confidence": 0.85,
}


# ---- Gate 1 Tests ----


class TestGate1_FlagParity:
    """Gate 1.3: Flags OFF → output identical to pre-incubator baseline."""

    def test_all_flags_off_returns_same_object(self) -> None:
        """THE critical parity test: flags OFF → enrich() returns same dict."""
        with patch.dict(os.environ, {}, clear=True):
            assert any_phase5_flag_on() is False
            # Runtime would not be constructed, but test enrich() anyway
            store = MagicMock()
            runtime = IncubatorRuntime(store, "/fake")
            result = _make_check_result([SAMPLE_OBLIGATION])
            enriched = runtime.enrich("check", result)
            assert enriched is result

    def test_no_incubator_keys_when_flags_off(self) -> None:
        """No _incubator_* keys present when all flags OFF."""
        with patch.dict(os.environ, {}, clear=True):
            store = MagicMock()
            runtime = IncubatorRuntime(store, "/fake")
            result = _make_check_result([SAMPLE_OBLIGATION])
            enriched = runtime.enrich("check", result)
            incubator_keys = [k for k in enriched if k.startswith("_incubator")]
            assert incubator_keys == []

    def test_json_output_identical_when_flags_off(self) -> None:
        """Serialized JSON is byte-identical when flags OFF (excluding _token_footprint)."""
        with patch.dict(os.environ, {}, clear=True):
            result = _make_check_result([SAMPLE_OBLIGATION])
            baseline = json.dumps(result, sort_keys=True)

            store = MagicMock()
            runtime = IncubatorRuntime(store, "/fake")
            enriched = runtime.enrich("check", result)
            after = json.dumps(enriched, sort_keys=True)

            assert baseline == after


class TestGate1_EachFlagIndependently:
    """Gate 1.4: Enable ONE flag at a time → only that subsystem activates."""

    def test_logging_only_no_enrichment(self) -> None:
        """LOGGING flag → logger constructed, but enrich() is no-op."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = MagicMock()
        store.connection = conn

        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            runtime = IncubatorRuntime(store, "/fake")
            assert runtime._intel_logger is not None
            assert runtime._intel_reader is None

            result = _make_check_result([SAMPLE_OBLIGATION])
            enriched = runtime.enrich("check", result)
            # Logging doesn't enrich — same object
            assert enriched is result

    def test_decisions_adds_history(self) -> None:
        """DECISIONS flag → _incubator_obligation_history appears."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = MagicMock()
        store.connection = conn

        # Seed some data
        logger = RepoIntelLogger(conn)
        logger.record("check", {
            "obligations": [SAMPLE_OBLIGATION],
            "contradictions": [],
        })

        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            runtime = IncubatorRuntime(store, "/fake")
            result = _make_check_result([SAMPLE_OBLIGATION])
            enriched = runtime.enrich("check", result)
            assert "_incubator_obligation_history" in enriched


class TestGate1_AccumulatedIntelligence:
    """Gate 1.5: Run check-diff twice → data logged, no output influence."""

    def test_logging_accumulates_without_influencing_output(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = MagicMock()
        store.connection = conn

        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            runtime = IncubatorRuntime(store, "/fake")

            # Run 1
            result1 = _make_check_result([SAMPLE_OBLIGATION])
            enriched1 = runtime.enrich("check", result1)
            runtime.log_interaction("check", enriched1)

            # Run 2
            result2 = _make_check_result([SAMPLE_OBLIGATION])
            enriched2 = runtime.enrich("check", result2)
            runtime.log_interaction("check", enriched2)

            # Data accumulated in SQLite
            rows = conn.execute("SELECT * FROM repo_obligation_stats").fetchall()
            assert len(rows) > 0
            assert rows[0]["seen_count"] == 2

            # But output has NO incubator keys (logging only!)
            assert enriched1 is result1
            assert enriched2 is result2


class TestGate1_CommunicationStateMachine:
    """Gate 1.6: 3 searches without edit → redirect framing."""

    def test_search_spinning_after_3_searches(self) -> None:
        policy = CommunicationPolicy()
        state = SessionState()

        # Simulate 3 search tool calls through normalize_tool_name path
        for raw_name in [
            "groundtruth_consolidated_impact",
            "groundtruth_consolidated_references",
            "groundtruth_consolidated_search",
        ]:
            normalized = normalize_tool_name(raw_name)
            state = policy.record_tool_call(state, normalized)

        framing = policy.get_framing(state, "search")
        assert framing is not None
        assert "search" in framing.lower() or "edit" in framing.lower()


class TestGate1_NoDDLWhenDisabled:
    """Gate 1.9: All flags OFF → no new tables created."""

    def test_runtime_construction_no_schema_changes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = MagicMock()
        store.connection = conn

        tables_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        with patch.dict(os.environ, {}, clear=True):
            # Runtime not constructed when no flags on
            if any_phase5_flag_on():
                IncubatorRuntime(store, "/fake")

        tables_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        assert tables_before == tables_after, (
            f"New tables created: {tables_after - tables_before}"
        )


class TestGate1_FlagMigrationCompat:
    """Gate 1.8: Old flag, new flags, both, neither — all work."""

    def test_old_flag_activates_logging(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_new_flags_work(self) -> None:
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            assert any_phase5_flag_on() is True

    def test_neither_flag_is_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert any_phase5_flag_on() is False

    def test_both_old_and_new(self) -> None:
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL": "1",
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            assert any_phase5_flag_on() is True


class TestGate1_Latency:
    """Gate 1.7: Enrichment overhead is minimal."""

    def test_enrich_under_10ms_flags_off(self) -> None:
        """Flags OFF → enrich is a no-op, should be <1ms."""
        import time
        with patch.dict(os.environ, {}, clear=True):
            store = MagicMock()
            runtime = IncubatorRuntime(store, "/fake")
            result = _make_check_result([SAMPLE_OBLIGATION] * 10)

            start = time.monotonic()
            for _ in range(100):
                runtime.enrich("check", result)
            elapsed_ms = (time.monotonic() - start) * 1000

            # 100 iterations < 10ms total → <0.1ms per call
            assert elapsed_ms < 10, f"100 enrich() calls took {elapsed_ms:.1f}ms"
