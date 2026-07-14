#!/usr/bin/env python3
"""Canonical, executable inventory for the 128 SS feature rows.

Only the acquisition family is declared here: it is a product-level taxonomy,
not a runtime flag registry.  The other three families are derived from their
executable authorities on every call so profile, FACT, and mandatory-metric
drift fails loudly instead of silently changing the denominator.
"""
from __future__ import annotations

from collections import Counter


ACQ_FEATURES: tuple[str, ...] = (
    "graph_validity",
    "structural_depth",
    "resolution_honesty",
    "type_intelligence",
    "lexical_FTS5",
    "body_retrieval",
    "semantic_embedder",
    "LSP",
    "freshness_basis",
    "repo_scope",
    "cochange_history",
    "determinism",
)

EXPECTED_FAMILY_COUNTS = {"ACQ": 12, "CAP": 47, "FACT": 11, "PERF": 58}


def performance_metric_definitions() -> dict[str, tuple[tuple[str, str], ...]]:
    """Return the exact sections 1-9 PERF contract used by the run aggregator."""
    from gt_run_metrics import _MANDATORY_METRICS

    return {
        section: tuple(definitions)
        for section, definitions in _MANDATORY_METRICS.items()
    }


def canonical_feature_inventory() -> dict[str, tuple[str, ...]]:
    """Return and validate the exact 12+47+11+58 feature universe."""
    from groundtruth.runtime.fact_registry import all_fact_classes
    from groundtruth.runtime.rl_profile import PROFILE_MEMBERS

    performance = performance_metric_definitions()
    inventory = {
        "ACQ": tuple(ACQ_FEATURES),
        "CAP": tuple(sorted(PROFILE_MEMBERS["2"])),
        "FACT": tuple(all_fact_classes()),
        "PERF": tuple(
            name
            for definitions in performance.values()
            for name, _ in definitions
        ),
    }
    for family, expected in EXPECTED_FAMILY_COUNTS.items():
        actual = len(inventory[family])
        if actual != expected:
            raise ValueError(
                f"gt_feature_inventory: {family} expected {expected} rows, got {actual}"
            )

    flattened = [name for names in inventory.values() for name in names]
    duplicates = sorted(name for name, count in Counter(flattened).items() if count > 1)
    if duplicates:
        raise ValueError(
            "gt_feature_inventory: feature names must be globally unique; duplicate rows "
            f"{duplicates}"
        )
    if len(flattened) != 128:
        raise ValueError(
            f"gt_feature_inventory: expected 128 globally unique rows, got {len(flattened)}"
        )
    return inventory
