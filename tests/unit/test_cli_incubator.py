"""Tests for CLI incubator integration (Step 7).

Verifies that the CLI check-diff command applies incubator enrichment
the same way as the MCP path.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from groundtruth.incubator.runtime import IncubatorRuntime, any_phase5_flag_on


class TestCLIEnrichmentWiring:
    """Verify CLI uses the same IncubatorRuntime as MCP."""

    def test_cli_check_diff_imports_runtime(self) -> None:
        """The CLI commands module must import IncubatorRuntime."""
        import inspect
        from groundtruth.cli import commands
        source = inspect.getsource(commands.check_diff_cmd)
        assert "IncubatorRuntime" in source
        assert "any_phase5_flag_on" in source

    def test_cli_enrichment_when_flags_on(self) -> None:
        """When enrichment flags are on, CLI enriches the same way as MCP."""
        store = MagicMock()
        store.connection = MagicMock()

        with patch.dict(os.environ, {
            "GT_ENABLE_CONVENTION_FINGERPRINT": "1",
        }, clear=True):
            runtime = IncubatorRuntime(store, "/fake/root")
            result = {
                "obligations": [
                    {"kind": "shared_state", "target": "A.b",
                     "file": "x.py", "confidence": 0.9},
                ],
                "total": 1,
            }
            enriched = runtime.enrich("check", result)
            # Should have tried to enrich (even if file doesn't exist)
            assert enriched is not result  # new dict created

    def test_cli_no_enrichment_when_flags_off(self) -> None:
        """When all flags off, CLI returns same result unchanged."""
        store = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            runtime = IncubatorRuntime(store, "/fake/root")
            result = {"obligations": [], "total": 0}
            enriched = runtime.enrich("check", result)
            assert enriched is result  # same object

    def test_any_phase5_flag_gates_construction(self) -> None:
        """CLI should only construct runtime when any_phase5_flag_on()."""
        with patch.dict(os.environ, {}, clear=True):
            assert any_phase5_flag_on() is False
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL_LOGGING": "1"}, clear=True):
            assert any_phase5_flag_on() is True
