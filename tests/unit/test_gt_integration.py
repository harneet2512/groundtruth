"""Tests for SWE-bench GT integration observability code."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from benchmarks.swebench.gt_integration import (
    GTIntegration,
    _is_likely_reexport,
)
from groundtruth.utils.result import Err, Ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeSymbol:
    file_path: str


def _make_store(symbols: list[FakeSymbol] | None = None) -> MagicMock:
    """Create a mock SymbolStore with find_symbol_by_name returning given symbols."""
    store = MagicMock()
    if symbols is None:
        store.find_symbol_by_name.return_value = Err("not found")
    else:
        store.find_symbol_by_name.return_value = Ok(symbols)
    return store


def _make_integration(injected_symbols: list[str] | None = None) -> GTIntegration:
    """Create a GTIntegration with injected symbol names set."""
    gt = GTIntegration.__new__(GTIntegration)
    gt._injected_symbol_names = injected_symbols or []
    gt._instrumentation = {}
    gt._validation_log = []
    return gt


# ---------------------------------------------------------------------------
# compute_context_utilization — word boundary fix
# ---------------------------------------------------------------------------

class TestContextUtilizationWordBoundary:
    def test_short_symbol_no_substring_match(self):
        """Symbol 'S' must NOT match 'Session' — word boundary required."""
        gt = _make_integration(["S"])
        result = gt.compute_context_utilization("class Session:\n    pass")
        assert result["symbols_used_in_patch"] == []
        assert result["utilization_rate"] == 0.0

    def test_exact_match(self):
        """Symbol 'QuerySet' SHOULD match 'qs = QuerySet()'."""
        gt = _make_integration(["QuerySet"])
        result = gt.compute_context_utilization("qs = QuerySet(model=Foo)")
        assert result["symbols_used_in_patch"] == ["QuerySet"]
        assert result["utilization_rate"] == 1.0

    def test_empty_patch(self):
        """None or empty patch returns utilization 0.0 without error."""
        gt = _make_integration(["Symbol1"])
        for patch in (None, ""):
            result = gt.compute_context_utilization(patch)
            assert result["utilization_rate"] == 0.0


# ---------------------------------------------------------------------------
# _is_likely_reexport
# ---------------------------------------------------------------------------

class TestIsLikelyReexport:
    def test_detected(self):
        """Symbol in submodule of same package → True."""
        store = _make_store([FakeSymbol(file_path="sympy/core/symbol.py")])
        assert _is_likely_reexport("S", "sympy.core", store) is True

    def test_not_detected(self):
        """Symbol in different package → False."""
        store = _make_store([FakeSymbol(file_path="django/db/models.py")])
        assert _is_likely_reexport("S", "sympy.core", store) is False

    def test_empty_input(self):
        """Empty/None args don't crash, return False."""
        store = _make_store()
        assert _is_likely_reexport("", "sympy.core", store) is False
        assert _is_likely_reexport("S", "", store) is False
