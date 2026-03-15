"""Unit tests for the pre-benchmark verification components."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure benchmarks package is importable
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from benchmarks.verify.hallucination_cases import (  # noqa: E402
    HallucinationCase,
    _mangle_name,
    generate_dynamic_cases,
    get_static_cases,
)
from benchmarks.verify.verify import CheckResult, VerifyReport  # noqa: E402
from groundtruth.index.store import SymbolStore  # noqa: E402
from groundtruth.utils.result import Ok  # noqa: E402


class TestStaticCases:
    def test_static_cases_nonempty(self) -> None:
        """get_static_cases() returns at least 3 cases with .py file paths."""
        cases = get_static_cases()
        assert len(cases) >= 3
        for case in cases:
            assert case.file_path.endswith(".py")
            assert case.id
            assert case.category
            assert case.code

    def test_static_cases_are_frozen(self) -> None:
        """Cases are frozen dataclasses."""
        cases = get_static_cases()
        with pytest.raises(AttributeError):
            cases[0].id = "changed"  # type: ignore[misc]


class TestDynamicCases:
    def test_dynamic_cases_with_populated_store(
        self, in_memory_store: SymbolStore,
    ) -> None:
        """generate_dynamic_cases() returns valid cases from a populated store."""
        import time

        now = int(time.time())
        # Insert a few symbols
        in_memory_store.insert_symbol(
            name="getUserById",
            kind="function",
            language="python",
            file_path="src/users/queries.py",
            line_number=10,
            end_line=20,
            is_exported=True,
            signature="(user_id: int) -> User",
            params=None,
            return_type="User",
            documentation="Get user by ID",
            last_indexed_at=now,
        )
        in_memory_store.insert_symbol(
            name="NotFoundError",
            kind="class",
            language="python",
            file_path="src/utils/errors.py",
            line_number=5,
            end_line=15,
            is_exported=True,
            signature=None,
            params=None,
            return_type=None,
            documentation="Error for missing resources",
            last_indexed_at=now,
        )
        # Add a ref so get_hotspots can return something
        sym_result = in_memory_store.find_symbol_by_name("getUserById")
        assert isinstance(sym_result, Ok)
        sym = sym_result.value[0]
        in_memory_store.insert_ref(
            symbol_id=sym.id,
            referenced_in_file="src/routes/users.py",
            referenced_at_line=47,
            reference_type="call",
        )
        # Update usage count
        in_memory_store.connection.execute(
            "UPDATE symbols SET usage_count = 1 WHERE id = ?", (sym.id,)
        )

        cases = generate_dynamic_cases(in_memory_store)
        assert len(cases) > 0
        for case in cases:
            assert isinstance(case, HallucinationCase)
            assert case.id.startswith("dynamic-")
            assert case.code
            assert case.category

    def test_dynamic_cases_with_empty_store(
        self, in_memory_store: SymbolStore,
    ) -> None:
        """generate_dynamic_cases() returns empty list for empty store, no crash."""
        cases = generate_dynamic_cases(in_memory_store)
        assert cases == []


class TestCheckResult:
    def test_check_result_dataclass(self) -> None:
        """CheckResult fields are correct."""
        result = CheckResult(
            check_number=1,
            name="Index",
            passed=True,
            duration_ms=42.5,
            details={"symbols": 100},
        )
        assert result.check_number == 1
        assert result.name == "Index"
        assert result.passed is True
        assert result.duration_ms == 42.5
        assert result.details == {"symbols": 100}
        assert result.error is None

    def test_check_result_with_error(self) -> None:
        """CheckResult with error field."""
        result = CheckResult(
            check_number=2,
            name="Risk Score",
            passed=False,
            duration_ms=10.0,
            error="Something went wrong",
        )
        assert result.passed is False
        assert result.error == "Something went wrong"


class TestVerifyReport:
    def test_verify_report_counts(self) -> None:
        """passed + failed == total."""
        report = VerifyReport(
            repo_path="/tmp/test",
            checks=[
                CheckResult(check_number=1, name="A", passed=True, duration_ms=1),
                CheckResult(check_number=2, name="B", passed=False, duration_ms=2),
                CheckResult(check_number=3, name="C", passed=True, duration_ms=3),
                CheckResult(check_number=4, name="D", passed=False, duration_ms=4),
            ],
        )
        assert report.passed == 2
        assert report.failed == 2
        assert report.total == 4
        assert report.passed + report.failed == report.total

    def test_verify_report_empty(self) -> None:
        """Empty report has zero counts."""
        report = VerifyReport(repo_path="/tmp/empty")
        assert report.passed == 0
        assert report.failed == 0
        assert report.total == 0


class TestMangledSymbol:
    def test_mangled_symbol_helper(self) -> None:
        """char-swap produces different string."""
        original = "getUserById"
        mangled = _mangle_name(original)
        assert mangled != original
        assert len(mangled) == len(original)

    def test_mangle_short_name(self) -> None:
        """Short names still produce a different result."""
        mangled = _mangle_name("ab")
        assert mangled != "ab"

    def test_mangle_identical_chars(self) -> None:
        """Mangling a name with identical adjacent chars still produces different result."""
        mangled = _mangle_name("aabbcc")
        assert mangled != "aabbcc"
