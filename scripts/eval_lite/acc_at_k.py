"""Acc@k helpers for file-level and function-level localization."""
from __future__ import annotations

from typing import Any


def hit_at_k_files(predicted_files: list[str], gold: set[str], k: int) -> bool:
    if not gold:
        return False
    return bool(set(predicted_files[:k]) & gold)


def hit_at_k_functions(
    predicted_functions: list[tuple[str, str]],
    gold: set[tuple[str, str]],
    k: int,
) -> bool:
    if not gold:
        return False
    return bool(set(predicted_functions[:k]) & gold)


def aggregate_acc(per_task_results: list[dict[str, Any]], metric: str) -> float:
    valid = [r[metric] for r in per_task_results if r.get(metric) is not None]
    if not valid:
        return 0.0
    return sum(1 for v in valid if v) / len(valid)
