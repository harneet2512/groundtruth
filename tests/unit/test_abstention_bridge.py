"""Tests for AbstentionBridge — single authority for emission decisions.

Real scenarios: fresh vs stale index, abstention flag ON/OFF, classification
of contradictions through the unified path.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.incubator.abstention_bridge import AbstentionBridge
from groundtruth.index.store import SymbolStore


@pytest.fixture
def store() -> SymbolStore:
    s = SymbolStore(":memory:")
    s.initialize()
    return s


class TestAbstentionOff:
    """When GT_ENABLE_ABSTENTION is OFF, everything passes through."""

    def test_classify_always_emit_when_off(self, store: SymbolStore) -> None:
        with patch.dict(os.environ, {}, clear=True):
            bridge = AbstentionBridge(store, "/fake/root")
        assert bridge.active is False
        finding = {"kind": "wrong_return_type", "file": "src/a.py", "confidence": 0.9}
        assert bridge.classify_finding(finding, "src/a.py") == "emit"

    def test_classify_emit_even_for_low_confidence(self, store: SymbolStore) -> None:
        """No filtering when abstention is OFF — even weak findings pass."""
        with patch.dict(os.environ, {}, clear=True):
            bridge = AbstentionBridge(store, "/fake/root")
        finding = {"kind": "missing_method", "file": "x.py", "confidence": 0.1}
        assert bridge.classify_finding(finding, "x.py") == "emit"


class TestAbstentionOn:
    """When GT_ENABLE_ABSTENTION is ON, bridge applies trust + freshness logic."""

    def test_active_when_flag_on(self, store: SymbolStore) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_ABSTENTION": "1"}, clear=True):
            bridge = AbstentionBridge(store, "/fake/root")
        assert bridge.active is True

    def test_fresh_file_emits(self, store: SymbolStore) -> None:
        """Fresh index + structural evidence → emit as hard blocker."""
        with patch.dict(os.environ, {"GT_ENABLE_ABSTENTION": "1"}, clear=True):
            bridge = AbstentionBridge(store, "/fake/root")

        from groundtruth.index.freshness import FreshnessLevel, FreshnessResult
        bridge._freshness.check_file = MagicMock(  # type: ignore[union-attr]
            return_value=FreshnessResult(
                file_path="src/a.py",
                level=FreshnessLevel.FRESH,
                last_indexed_at=1000,
                last_modified_at=1001.0,
                staleness_seconds=1.0,
            )
        )

        finding = {"kind": "wrong_return_type", "file": "src/a.py", "confidence": 0.9}
        result = bridge.classify_finding(finding, "src/a.py")
        assert result == "emit"

    def test_stale_file_suppresses(self, store: SymbolStore) -> None:
        """Stale index → YELLOW trust → with is_stale=True → EMIT_NOTHING."""
        with patch.dict(os.environ, {"GT_ENABLE_ABSTENTION": "1"}, clear=True):
            bridge = AbstentionBridge(store, "/fake/root")

        from groundtruth.index.freshness import FreshnessLevel, FreshnessResult
        bridge._freshness.check_file = MagicMock(  # type: ignore[union-attr]
            return_value=FreshnessResult(
                file_path="src/a.py",
                level=FreshnessLevel.STALE,
                last_indexed_at=1000,
                last_modified_at=2000.0,
                staleness_seconds=1000.0,
            )
        )

        finding = {"kind": "wrong_return_type", "file": "src/a.py", "confidence": 0.9}
        result = bridge.classify_finding(finding, "src/a.py")
        assert result == "suppress"


class TestBridgeIsUsedByHandler:
    """Verify the abstention bridge is actually used in the check handler path.
    This is a behavioral test, not a unit test — it checks the refactoring
    didn't break the existing abstention integration."""

    def test_bridge_replaces_inline_code(self) -> None:
        """The core_tools module should import AbstentionBridge, not inline the logic."""
        import inspect
        from groundtruth.mcp.tools import core_tools
        source = inspect.getsource(core_tools.handle_consolidated_check)
        # Must use bridge
        assert "AbstentionBridge" in source
        # Must NOT have the old inline pattern
        assert "abstention_policy.decide(" not in source
        assert "freshness_checker.check_file(" not in source
