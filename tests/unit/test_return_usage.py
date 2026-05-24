"""Tests for return_usage evidence module.

Covers all classification labels, malformed input, and feature flag OFF behavior.
"""

from __future__ import annotations

import os
import pytest


class TestClassifyReturnUsage:
    """Mini-spike: 10 caller lines, require >=8/10 correct."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_RETURN_USAGE_ENABLED", "1")

    def test_check_truthiness(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("if get_user():", "get_user")
        assert result == "CHECK_TRUTHINESS"

    def test_destructure(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("a, b = split_pair()", "split_pair")
        assert result == "DESTRUCTURE"

    def test_discard(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("process_item(x)", "process_item")
        assert result == "DISCARD"

    def test_cast(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = int(get_value())", "get_value")
        assert result == "CAST"

    def test_compare(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = get_status() == 'ok'", "get_status")
        assert result == "COMPARE"

    def test_assign(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("result = fetch_data(url)", "fetch_data")
        assert result == "ASSIGN"

    def test_chain_call(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("get_connection().execute(sql)", "get_connection")
        assert result == "CHAIN_CALL"

    def test_conditional(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = default if is_empty() else value", "is_empty")
        assert result == "CONDITIONAL"

    def test_assign_simple(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("data = load_config(path)", "load_config")
        assert result == "ASSIGN"

    def test_compare_in_if(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("if count_items() == 0:", "count_items")
        assert result == "COMPARE"

    def test_spike_accuracy_gate(self) -> None:
        """Verify >=8/10 correct classifications (mini-spike gate)."""
        from groundtruth.evidence.return_usage import classify_return_usage

        cases = [
            ("if get_user():", "get_user", "CHECK_TRUTHINESS"),
            ("a, b = split_pair()", "split_pair", "DESTRUCTURE"),
            ("process_item(x)", "process_item", "DISCARD"),
            ("x = int(get_value())", "get_value", "CAST"),
            ("x = get_status() == 'ok'", "get_status", "COMPARE"),
            ("result = fetch_data(url)", "fetch_data", "ASSIGN"),
            ("get_connection().execute(sql)", "get_connection", "CHAIN_CALL"),
            ("x = default if is_empty() else value", "is_empty", "CONDITIONAL"),
            ("data = load_config(path)", "load_config", "ASSIGN"),
            ("if count_items() == 0:", "count_items", "COMPARE"),
        ]

        correct = sum(
            1 for line, func, expected in cases
            if classify_return_usage(line, func) == expected
        )
        assert correct >= 8, f"Only {correct}/10 correct, need >=8"


class TestReturnUsageUnknown:
    """Test UNKNOWN on malformed input."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_RETURN_USAGE_ENABLED", "1")

    def test_syntax_error_returns_unknown(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("def ??? broken syntax {{{{", "func")
        assert result == "UNKNOWN"

    def test_empty_line_returns_unknown(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("", "func")
        assert result == "UNKNOWN"

    def test_no_call_returns_unknown(self) -> None:
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = 42", "func")
        assert result == "UNKNOWN"


class TestReturnUsageFeatureFlag:
    """Test that GT_RETURN_USAGE_ENABLED=0 produces no output."""

    def test_classify_disabled_returns_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_RETURN_USAGE_ENABLED", "0")
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = func()", "func")
        assert result == "UNKNOWN"

    def test_annotate_disabled_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_RETURN_USAGE_ENABLED", "0")
        from groundtruth.evidence.return_usage import annotate_caller_lines
        result = annotate_caller_lines(["x = func()", "func()"], "func")
        assert result == []

    def test_default_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GT_RETURN_USAGE_ENABLED", raising=False)
        from groundtruth.evidence.return_usage import classify_return_usage
        result = classify_return_usage("x = func()", "func")
        assert result == "UNKNOWN"


class TestAnnotateCallerLines:
    """Test the batch annotation function."""

    @pytest.fixture(autouse=True)
    def _enable_feature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GT_RETURN_USAGE_ENABLED", "1")

    def test_annotate_multiple(self) -> None:
        from groundtruth.evidence.return_usage import annotate_caller_lines
        lines = ["x = func()", "func()", "if func():"]
        result = annotate_caller_lines(lines, "func")
        assert len(result) == 3
        assert result[0]["usage"] == "ASSIGN"
        assert result[1]["usage"] == "DISCARD"
        assert result[2]["usage"] == "CHECK_TRUTHINESS"

    def test_annotate_empty_list(self) -> None:
        from groundtruth.evidence.return_usage import annotate_caller_lines
        result = annotate_caller_lines([], "func")
        assert result == []
