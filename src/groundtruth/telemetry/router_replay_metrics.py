"""Offline metric helpers for FINAL_ARCH_V2 router-replay reports.

These parse the JSON produced by ``scripts/shadow_replay.py`` and recompute
the §6 metric set in repaired form. They are intentionally narrow — every
metric here describes what happened in a replay, never claims GT helps.

Repaired vs the archived ``METRICS_CONTRACT.md`` definitions:

- ``files_viewed_before_gold`` is now the count of *distinct* files viewed
  before the agent's first gold-file read (not the action index).
- ``late_guidance_count`` is computed from the replay (router emitted on an
  edit whose target was already in ``edited_files`` BEFORE this event).
- ``stale_guidance_count`` is computed by re-checking the router's
  ``primary_edge_file`` against the AgentState ``visited_files_set`` at
  emission time.
- ``action_economy_vs_baseline`` is exposed *parser-shape only*; we do not
  claim a value until paired GT-vs-baseline runs land.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReplayMetrics:
    input_count: int = 0
    graph_resolved_count: int = 0
    graph_unresolved_count: int = 0
    router_emit_total: int = 0
    old_hook_emit_total: int = 0
    provider_request_total: int = 0
    provider_empty_total: int = 0
    bridge_event_before_gold_total: int = 0
    stale_guidance_total: int = 0
    late_guidance_total: int = 0
    injections_per_task_total: int = 0
    agent_followed_gt_edge_total: int = 0
    distinct_files_before_gold: list[int] = field(default_factory=list)
    distinct_files_before_gold_median: float = 0.0
    distinct_files_before_gold_mean: float = 0.0
    suppression_distribution: dict[str, int] = field(default_factory=dict)
    old_vs_new_distribution: dict[str, int] = field(default_factory=dict)
    per_task: list[dict[str, Any]] = field(default_factory=list)

    @property
    def action_economy_vs_baseline(self) -> dict[str, Any]:
        """Parser-shape only; cannot be filled without paired baseline data."""
        return {
            "available": False,
            "reason": "paired_baseline_required",
            "per_task": [],
            "median_delta": None,
        }


def parse_replay_report(path: str | Path) -> ReplayMetrics:
    """Parse a shadow_replay report into a ``ReplayMetrics`` object."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    rm = ReplayMetrics(
        input_count=int(data.get("input_count", 0)),
        graph_resolved_count=int(data.get("graph_resolved_count", 0)),
        graph_unresolved_count=int(data.get("graph_unresolved_count", 0)),
        router_emit_total=int(totals.get("router_emit", 0)),
        old_hook_emit_total=int(totals.get("old_hook_emit", 0)),
        provider_request_total=int(totals.get("provider_request", 0)),
        provider_empty_total=int(totals.get("provider_empty", 0)),
        bridge_event_before_gold_total=int(totals.get("bridge_event_before_gold", 0)),
        stale_guidance_total=int(totals.get("stale_guidance_count", 0)),
        late_guidance_total=int(totals.get("late_guidance_count", 0)),
        injections_per_task_total=int(totals.get("injections_per_task_total", 0)),
        agent_followed_gt_edge_total=int(totals.get("agent_followed_gt_edge", 0)),
        suppression_distribution=dict(data.get("suppression_distribution", {})),
        old_vs_new_distribution=dict(data.get("old_vs_new_distribution", {})),
        per_task=list(data.get("tasks", [])),
    )
    rm.distinct_files_before_gold = [
        int(t.get("distinct_files_viewed_before_gold", 0))
        for t in rm.per_task
    ]
    if rm.distinct_files_before_gold:
        rm.distinct_files_before_gold_median = float(
            statistics.median(rm.distinct_files_before_gold)
        )
        rm.distinct_files_before_gold_mean = float(
            statistics.mean(rm.distinct_files_before_gold)
        )
    return rm


def summarize_provider_request_log(per_task: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-event provider_request_log into compact counts."""
    by_kind: dict[str, int] = {"on_view": 0, "on_edit": 0}
    empty_by_kind: dict[str, int] = {"on_view": 0, "on_edit": 0}
    files_by_kind: dict[str, set[str]] = {"on_view": set(), "on_edit": set()}
    for t in per_task:
        for entry in t.get("provider_request_log", []) or []:
            kind = entry.get("kind", "")
            if kind in by_kind:
                by_kind[kind] += 1
                if not entry.get("items", entry.get("callers", 0) + entry.get("callees", 0) + entry.get("importers", 0)):
                    empty_by_kind[kind] += 1
                if entry.get("file"):
                    files_by_kind[kind].add(entry["file"])
    return {
        "requests_by_kind": by_kind,
        "empty_by_kind": empty_by_kind,
        "distinct_files_by_kind": {k: len(v) for k, v in files_by_kind.items()},
    }


__all__ = [
    "ReplayMetrics",
    "parse_replay_report",
    "summarize_provider_request_log",
]
