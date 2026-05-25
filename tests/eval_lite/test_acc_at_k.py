"""Tests for scripts.eval_lite.acc_at_k."""
from __future__ import annotations

from scripts.eval_lite.acc_at_k import (
    aggregate_acc,
    hit_at_k_files,
    hit_at_k_functions,
)


def test_hit_at_k_files_basic() -> None:
    predicted = ["a", "b", "c"]
    gold = {"b"}
    assert hit_at_k_files(predicted, gold, 2) is True
    assert hit_at_k_files(predicted, gold, 1) is False
    assert hit_at_k_files(predicted, gold, 5) is True


def test_hit_at_k_files_empty_gold() -> None:
    assert hit_at_k_files(["a", "b"], set(), 5) is False


def test_hit_at_k_files_empty_pred() -> None:
    assert hit_at_k_files([], {"a"}, 5) is False


def test_hit_at_k_functions() -> None:
    predicted = [("a.py", "f"), ("b.py", "g"), ("c.py", "h")]
    gold = {("b.py", "g")}
    assert hit_at_k_functions(predicted, gold, 2) is True
    assert hit_at_k_functions(predicted, gold, 1) is False
    assert hit_at_k_functions(predicted, gold, 5) is True


def test_hit_at_k_functions_empty_gold() -> None:
    assert hit_at_k_functions([("a.py", "f")], set(), 5) is False


def test_aggregate_acc() -> None:
    results = [
        {"hit_file_at_5": True},
        {"hit_file_at_5": False},
        {"hit_file_at_5": True},
    ]
    assert abs(aggregate_acc(results, "hit_file_at_5") - (2 / 3)) < 1e-9


def test_aggregate_acc_skips_none() -> None:
    results = [
        {"hit_file_at_5": True},
        {"hit_file_at_5": None},
        {"hit_file_at_5": False},
    ]
    assert abs(aggregate_acc(results, "hit_file_at_5") - 0.5) < 1e-9


def test_aggregate_acc_empty() -> None:
    assert aggregate_acc([], "hit_file_at_5") == 0.0
