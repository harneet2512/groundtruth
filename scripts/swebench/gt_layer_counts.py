#!/usr/bin/env python3
"""RC-10 — single canonical layer-count helper.

The repo previously had **four** L4 readers across four files (smoke runner,
Track 4 close-wrap, deep_util_gate, full_potential_analyzer) that each
counted "L4 invocations" with subtly different rules. The 2026-05-06
11-tool → 4-tool consolidation introduced ``gt_search_calls.jsonl`` and
``gt_navigate_calls.jsonl``; the smoke runner's L4 cell still counted only
``gt_query_calls.jsonl``, so any agent that exercised the new structural
surfaces showed L4=0 in the canonical line even when running normally.

This module is the **canonical reader** used by every L4-counting site.
It returns per-tool counts plus an aggregated L4 total, so callers can
choose to render the sum or the breakdown but cannot disagree on the
sum.

Generic — repo / language / benchmark agnostic. The contract is:

    {
        "gt_query":    int,   # 0 if file missing OR empty
        "gt_search":   int,
        "gt_navigate": int,
        "gt_validate": int,
        "L4_total":    int,   # sum of gt_query + gt_search + gt_navigate
        "L5_validate": int,   # gt_validate (separate; renders under L5 col)
        "L6_reindex":  int,   # gt_reindex.jsonl
        "L3_edits":    int,   # gt_evidence/edit_*.json count
    }

The 4 *_calls.jsonl files are written one JSON record per invocation by
``tools/sweagent/gt_*/lib/*.py:_emit_telemetry`` (best-effort append +
fsync). Empty file means zero invocations (a valid state). Missing file
also means zero (after the stub-init fix in RC-10 every task dir
pre-creates all six artifact stubs, so missing-file is rare).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

# Canonical per-tool JSONL filenames written by tools/sweagent/<tool>/lib
# bundles via _emit_telemetry(). Pre-created by gt_track4_pre_run.py
# _init_host_artifact_stubs and _init_container_artifact_stubs.
JSONL_NAMES = (
    "gt_query_calls.jsonl",
    "gt_search_calls.jsonl",
    "gt_navigate_calls.jsonl",
    "gt_validate_calls.jsonl",
)

# L4 (structural-tool surface) is the sum of the navigation tools.
# gt_validate is reported separately because it renders under L5 (the
# pre-finish gate column) — it's the validation tool, not the navigation
# tool, despite being a *_calls.jsonl artifact.
L4_TOOLS = ("gt_query", "gt_search", "gt_navigate")


def _count_jsonl_lines(path: Path) -> int:
    """Count non-empty newline-delimited records. Defensive against IO errors."""
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        return 0


def count_layer_calls(task_dir: Optional[Path]) -> Dict[str, int]:
    """Return per-tool + aggregated counts read from on-disk JSONL artifacts.

    Returns a dict with the keys documented in the module docstring. All
    keys are always present; values are 0 when the artifact is missing or
    empty. Never raises.

    A task_dir of ``None`` returns a zero dict — useful for callers that
    must report "no data" without branching.
    """
    out: Dict[str, int] = {
        "gt_query": 0,
        "gt_search": 0,
        "gt_navigate": 0,
        "gt_validate": 0,
        "L4_total": 0,
        "L5_validate": 0,
        "L6_reindex": 0,
        "L3_edits": 0,
    }
    if task_dir is None:
        return out
    td = Path(task_dir)
    if not td.is_dir():
        return out

    for name in JSONL_NAMES:
        # tool key is the prefix before "_calls.jsonl" — gt_query, gt_search, etc.
        tool = name[: -len("_calls.jsonl")]
        out[tool] = _count_jsonl_lines(td / name)

    out["L4_total"] = sum(out[t] for t in L4_TOOLS)
    out["L5_validate"] = out["gt_validate"]
    out["L6_reindex"] = _count_jsonl_lines(td / "gt_reindex.jsonl")

    ev = td / "gt_evidence"
    if ev.is_dir():
        out["L3_edits"] = sum(1 for f in ev.glob("edit_*.json") if f.is_file())

    return out


def disagreement_check(jsonl_count: int, traj_count: int,
                       tolerance: int = 0) -> Optional[str]:
    """Compare a JSONL count vs a trajectory count for the same tool.

    Returns ``None`` if the counts agree (within ``tolerance``).
    Returns a short reason string when they disagree — callers should
    FAIL-LOUD on this signal rather than silently picking one source.

    The 2026-05-06 audit found 4 separate L4 readers that *can* disagree
    (e.g. trajectory dropped a tool call due to history truncation, OR
    _emit_telemetry swallowed an OSError on the JSONL append). Surfacing
    the disagreement is more honest than picking a number.
    """
    if abs(jsonl_count - traj_count) <= tolerance:
        return None
    return (
        f"jsonl_vs_trajectory_disagreement: jsonl={jsonl_count} "
        f"trajectory={traj_count} tolerance={tolerance}"
    )
