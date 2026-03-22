"""Tests for IncubatorRuntime — Phase 5 facade.

Real scenarios: byte-parity when flags OFF, enrichment when flags ON,
_finalize() mutation order, no side effects on construction.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.incubator.runtime import IncubatorRuntime, any_phase5_flag_on


class TestAnyPhase5FlagOn:
    """Gate check: should IncubatorRuntime be constructed at all?"""

    def test_no_flags_set(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert any_phase5_flag_on() is False

    def test_logging_only(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL_LOGGING": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_foundation_only(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOUNDATION": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_hnsw_only(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_HNSW": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_response_state_machine_only(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_RESPONSE_STATE_MACHINE": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_old_repo_intel_flag(self) -> None:
        """Old flag maps to logging → phase5 flag is on."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            assert any_phase5_flag_on() is True

    def test_non_phase5_flags_ignored(self) -> None:
        """Contradictions/abstention are Phase 1-4 — don't trigger Phase 5."""
        with patch.dict(os.environ, {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
        }, clear=True):
            assert any_phase5_flag_on() is False


class TestByteParity:
    """Core contract: when no enrichment flags → same object returned."""

    def _make_runtime(self) -> IncubatorRuntime:
        store = MagicMock()
        return IncubatorRuntime(store, "/fake/root")

    def test_enrich_returns_same_object_when_no_enrichment_flags(self) -> None:
        """THE critical test: flags OFF → exact same dict object, not a copy."""
        with patch.dict(os.environ, {}, clear=True):
            runtime = self._make_runtime()
            inp = {"obligations": [{"kind": "shared_state"}], "total": 1}
            out = runtime.enrich("check", inp)
            assert out is inp  # same object, not a copy

    def test_enrich_returns_same_object_when_only_logging(self) -> None:
        """Logging doesn't enrich — it only observes."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            runtime = self._make_runtime()
            inp = {"corrections": [], "total": 0}
            out = runtime.enrich("check", inp)
            assert out is inp

    def test_enrich_returns_copy_when_decisions_on(self) -> None:
        """Decisions flag adds data → must return a new dict."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            runtime = self._make_runtime()
            inp = {"obligations": [], "total": 0}
            out = runtime.enrich("check", inp)
            assert out is not inp  # new dict
            # Original unchanged
            assert "_incubator" not in str(inp)

    def test_enrich_does_not_modify_existing_keys(self) -> None:
        """Enrichment adds new keys but NEVER modifies existing ones."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            runtime = self._make_runtime()
            inp = {"obligations": [{"kind": "shared_state"}], "total": 1}
            out = runtime.enrich("check", inp)
            assert out["obligations"] == inp["obligations"]
            assert out["total"] == inp["total"]

    def test_no_keys_added_when_flags_off(self) -> None:
        """Flags OFF → output has exactly the same keys as input."""
        with patch.dict(os.environ, {}, clear=True):
            runtime = self._make_runtime()
            inp = {"a": 1, "b": 2}
            out = runtime.enrich("impact", inp)
            assert set(out.keys()) == {"a", "b"}


class TestLogInteraction:
    """log_interaction must be a no-op when logger not configured."""

    def _make_runtime(self) -> IncubatorRuntime:
        store = MagicMock()
        return IncubatorRuntime(store, "/fake/root")

    def test_log_is_noop_when_no_logger(self) -> None:
        """No intel_logger → log_interaction does nothing, no crash."""
        runtime = self._make_runtime()
        assert runtime._intel_logger is None
        # Must not raise
        runtime.log_interaction("check", {"total": 0})

    def test_log_calls_logger_when_configured(self) -> None:
        """When intel_logger is set, it gets called."""
        runtime = self._make_runtime()
        mock_logger = MagicMock()
        runtime._intel_logger = mock_logger

        result = {"obligations": [], "total": 0}
        runtime.log_interaction("check", result)
        mock_logger.record.assert_called_once_with("check", result)


class TestConstructionSideEffects:
    """IncubatorRuntime construction must have minimal side effects."""

    def test_construction_does_not_create_tables(self) -> None:
        """Runtime init must NOT run DDL on the database."""
        mock_store = MagicMock()
        runtime = IncubatorRuntime(mock_store, "/fake/root")
        # Connection should not be accessed during init
        mock_store.connection.execute.assert_not_called()
        assert runtime is not None

    def test_construction_does_not_import_foundation(self) -> None:
        """Foundation imports are lazy — must not happen at construction."""
        import sys
        mock_store = MagicMock()
        # Track which modules are imported
        modules_before = set(sys.modules.keys())
        IncubatorRuntime(mock_store, "/fake/root")
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        # No foundation modules should be imported during construction
        foundation_modules = [m for m in new_modules if "foundation" in m]
        assert foundation_modules == [], f"Unexpected foundation imports: {foundation_modules}"
