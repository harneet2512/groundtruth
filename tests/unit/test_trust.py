"""Tests for the Python-specific runtime introspection trust substrate."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import threading
import time

import pytest

from groundtruth.core.trust import RuntimeIntrospector, TrustLevel, TrustResult


class TestTrustLevelEnum:
    def test_values_are_correct_strings(self) -> None:
        assert TrustLevel.GREEN == "green"
        assert TrustLevel.YELLOW == "yellow"
        assert TrustLevel.RED == "red"

    def test_enum_members_count(self) -> None:
        assert len(TrustLevel) == 3


class TestTrustResult:
    def test_defaults(self) -> None:
        result = TrustResult(
            level=TrustLevel.GREEN,
            symbol_name="foo.Bar",
            checked_via="runtime",
        )
        assert result.members == []
        assert result.error is None


class TestCheckClass:
    def test_stdlib_module_returns_green(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_class("collections", "OrderedDict")
        assert result.level == TrustLevel.GREEN
        assert result.checked_via == "runtime"
        assert "keys" in result.members
        assert "values" in result.members
        assert result.error is None

    def test_nonexistent_module_returns_yellow(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_class("nonexistent_module_xyz_123", "Foo")
        assert result.level == TrustLevel.YELLOW
        assert result.checked_via == "ast"
        assert "ModuleNotFoundError" in (result.error or "")

    def test_nonexistent_class_in_valid_module_returns_yellow(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_class("collections", "TotallyFakeClass")
        assert result.level == TrustLevel.YELLOW
        assert result.checked_via == "ast"
        assert "AttributeError" in (result.error or "")

    def test_symbol_name_format(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_class("collections", "OrderedDict")
        assert result.symbol_name == "collections.OrderedDict"

    def test_module_with_side_effects_no_crash(self) -> None:
        """Importing a module that raises during import should return YELLOW, not crash."""
        ri = RuntimeIntrospector()
        with patch("importlib.import_module", side_effect=RuntimeError("side effect!")):
            result = ri.check_class("bad_module", "Cls")
        assert result.level == TrustLevel.YELLOW
        assert "RuntimeError" in (result.error or "")


class TestCheckMember:
    def test_known_member_returns_green(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_member("collections", "OrderedDict", "keys")
        assert result.level == TrustLevel.GREEN
        assert result.checked_via == "runtime"

    def test_nonexistent_member_returns_red(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_member("collections", "OrderedDict", "totally_fake_method")
        assert result.level == TrustLevel.RED
        assert result.checked_via == "runtime"
        assert "not found" in (result.error or "")

    def test_unimportable_module_returns_yellow_for_member(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_member("nonexistent_xyz", "Cls", "method")
        assert result.level == TrustLevel.YELLOW
        assert result.checked_via == "ast"

    def test_member_symbol_name_format(self) -> None:
        ri = RuntimeIntrospector()
        result = ri.check_member("collections", "OrderedDict", "keys")
        assert result.symbol_name == "collections.OrderedDict.keys"


class TestTimeout:
    def test_slow_import_triggers_timeout(self) -> None:
        ri = RuntimeIntrospector(timeout_seconds=0.1)

        def slow_import(name: str) -> None:
            time.sleep(5)

        with patch("importlib.import_module", side_effect=slow_import):
            result = ri.check_class("slow_module", "Cls")

        assert result.level == TrustLevel.YELLOW
        assert "timeout" in (result.error or "").lower()
