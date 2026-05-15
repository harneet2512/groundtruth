#!/usr/bin/env python3
"""30-Task Comparison: GT+Agent (L1+L3+L3b) vs Historical Baseline.

Loads output.jsonl from both arms, computes per-task metrics, produces a
comparison table, flags regressions/flips, and outputs aggregate statistics.

Cost estimate (header):
  LLM:  30 tasks × max_iter=100 × ~$0.12/task = ~$3.60
  VM:   ~2 hours total at ~$1.50/hr = ~$3.00
  Total: ~$6.60
  Budget remaining before: ~$75
  Budget after: ~$68.40

Usage:
  python scripts/analysis/compare_30task.py \\
    --gt-t0 ~/results/30task_comparison_*/gt_t0/output.jsonl \\
    --gt-v1 ~/results/30task_comparison_*/gt_v1/output.jsonl \\
    --baseline-t0 D:\\tmp\\gt_test\\results_final/baseline_t0.jsonl \\
    --baseline-v1 D:\\tmp\\gt_test\\results_final/baseline_v1.jsonl

If --gt-t0 and --gt-v1 are not provided, looks for merged output.jsonl via --gt.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TaskMetrics:
    """Per-task comparison metrics."""

    instance_id: str
    # Baseline
    bl_has_patch: bool = False
    bl_patch_len: int = 0
    bl_total_actions: int = 0
    bl_first_gold_read: int | None = None
    bl_first_gold_edit: int | None = None
    bl_first_scaffold: int | None = None
    bl_resolved: bool = False
    bl_cost: float = 0.0
    # GT arm
    gt_has_patch: bool = False
    gt_patch_len: int = 0
    gt_total_actions: int = 0
    gt_first_gold_read: int | None = None
    gt_first_gold_edit: int | None = None
    gt_first_scaffold: int | None = None
    gt_resolved: bool = False
    gt_cost: float = 0.0
    gt_evidence_blocks: int = 0
    gt_brief_status: str = "unknown"
    # Comparison
    flip: str = ""  # "gt_gain", "gt_regression", "both", "neither"


# ---------------------------------------------------------------------------
# Gold file detection (heuristic — uses instance_id to guess repo structure)
# ---------------------------------------------------------------------------

SCAFFOLD_PATTERNS = re.compile(
    r"(^|/)(reproduce_|repro_|debug_|verify_fix|verify_implementation|"
    r"test_fix|scratch_|temp_|run_test)"
)

TEST_PATTERNS = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$"
)


def _is_scaffold(path: str) -> bool:
    return bool(SCAFFOLD_PATTERNS.search(path))


def _is_test(path: str) -> bool:
    return bool(TEST_PATTERNS.search(path))


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------

def _extract_actions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract agent actions from OpenHands history format."""
    actions = []
    for event in history:
        # OpenHands history has (action, observation) pairs or flat events
        if isinstance(event, dict):
            action = event.get("action", "")
            if action and action != "message":
                actions.append(event)
            # Also check nested format
            if "pairs" in event:
                for pair in event["pairs"]:
                    if isinstance(pair, dict) and pair.get("action"):
                        actions.append(pair)
    return actions


def _extract_edited_files(history: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Extract (step_index, file_path) for edits from history."""
    edits: list[tuple[int, str]] = []
    for i, event in enumerate(history):
        if not isinstance(event, dict):
            continue
        # Look for file_editor or edit actions
        action = event.get("action", "")
        args = event.get("args", {})
        if isinstance(args, dict):
            path = args.get("path", "") or args.get("file", "")
            command = args.get("command", "")
            # Mutating edit
            if action in ("edit", "file_editor") and command in (
                "create", "str_replace", "insert", "write", ""
            ):
                if path:
                    edits.append((i, path))
            elif action == "run" and (">" in str(args.get("command", "")) or
                                      "tee" in str(args.get("command", ""))):
                # Shell writes — too noisy, skip
                pass
    return edits


def _extract_read_files(history: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Extract (step_index, file_path) for reads from history."""
    reads: list[tuple[int, str]] = []
    for i, event in enumerate(history):
        if not isinstance(event, dict):
            continue
        action = event.get("action", "")
        args = event.get("args", {})
        if isinstance(args, dict):
            path = args.get("path", "") or args.get("file", "")
            command = args.get("command", "")
            if action in ("read", "file_editor") and command in ("view", ""):
                if path:
                    reads.append((i, path))
    return reads


def _count_gt_evidence(history: list[dict[str, Any]]) -> int:
    """Count GT evidence blocks in history observations."""
    count = 0
    for event in history:
        if not isinstance(event, dict):
            continue
        obs = event.get("observation", "") or event.get("content", "") or ""
        if isinstance(obs, str):
            count += obs.count("[GT_") + obs.count("<gt-evidence>")
    return count


def _detect_brief_status(history: list[dict[str, Any]]) -> str:
    """Check if GT brief was present in instruction/first messages."""
    for event in history[:5]:
        if not isinstance(event, dict):
            continue
        content = str(event.get("content", "") or event.get("args", {}).get("content", ""))
        if "<gt-task-brief>" in content or "gt-task-brief" in content:
            return "injected"
        if "[GT_BRIEF_FAILED]" in content:
            return "failed"
    return "absent"


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    """Load output.jsonl → {instance_id: record}."""
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        print(f"WARNING: {path} not found", file=sys.stderr)
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                iid = rec.get("instance_id", "")
                if iid:
                    records[iid] = rec
            except json.JSONDecodeError:
                continue
    return records


def analyze_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Extract metrics from a single output.jsonl record."""
    result: dict[str, Any] = {}

    # Patch
    test_result = rec.get("test_result", {})
    if isinstance(test_result, dict):
        patch = test_result.get("git_patch", "") or ""
    else:
        patch = ""
    result["has_patch"] = bool(patch.strip())
    result["patch_len"] = len(patch)

    # Resolved (if eval results are present)
    result["resolved"] = bool(rec.get("resolved", False))

    # History analysis
    history = rec.get("history", [])
    if not isinstance(history, list):
        history = []
    result["total_actions"] = len(history)

    # File operations
    edits = _extract_edited_files(history)
    reads = _extract_read_files(history)

    # First scaffold
    first_scaffold = None
    for step, path in edits:
        if _is_scaffold(path):
            first_scaffold = step
            break
    result["first_scaffold"] = first_scaffold

    # First gold read/edit — placeholder (need gold files per-task to fill)
    result["first_gold_read"] = None
    result["first_gold_edit"] = None

    # GT-specific
    result["evidence_blocks"] = _count_gt_evidence(history)
    result["brief_status"] = _detect_brief_status(history)

    # Cost
    metrics = rec.get("metrics", {})
    if isinstance(metrics, dict):
        result["cost"] = float(metrics.get("accumulated_cost", 0) or 0)
    else:
        result["cost"] = 0.0

    return result


def compare(
    baseline: dict[str, dict[str, Any]],
    gt_arm: dict[str, dict[str, Any]],
) -> list[TaskMetrics]:
    """Compare GT arm against baseline on shared tasks."""
    # All 30 task IDs (union of both)
    all_ids = sorted(set(list(baseline.keys()) + list(gt_arm.keys())))

    results: list[TaskMetrics] = []
    for iid in all_ids:
        tm = TaskMetrics(instance_id=iid)

        if iid in baseline:
            bl = analyze_record(baseline[iid])
            tm.bl_has_patch = bl["has_patch"]
            tm.bl_patch_len = bl["patch_len"]
            tm.bl_total_actions = bl["total_actions"]
            tm.bl_first_gold_read = bl["first_gold_read"]
            tm.bl_first_gold_edit = bl["first_gold_edit"]
            tm.bl_first_scaffold = bl["first_scaffold"]
            tm.bl_resolved = bl["resolved"]
            tm.bl_cost = bl["cost"]

        if iid in gt_arm:
            gt = analyze_record(gt_arm[iid])
            tm.gt_has_patch = gt["has_patch"]
            tm.gt_patch_len = gt["patch_len"]
            tm.gt_total_actions = gt["total_actions"]
            tm.gt_first_gold_read = gt["first_gold_read"]
            tm.gt_first_gold_edit = gt["first_gold_edit"]
            tm.gt_first_scaffold = gt["first_scaffold"]
            tm.gt_resolved = gt["resolved"]
            tm.gt_cost = gt["cost"]
            tm.gt_evidence_blocks = gt["evidence_blocks"]
            tm.gt_brief_status = gt["brief_status"]

        # Flip classification
        if tm.gt_has_patch and not tm.bl_has_patch:
            tm.flip = "gt_gain"
        elif tm.bl_has_patch and not tm.gt_has_patch:
            tm.flip = "gt_regression"
        elif tm.gt_has_patch and tm.bl_has_patch:
            tm.flip = "both"
        else:
            tm.flip = "neither"

        results.append(tm)

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(results: list[TaskMetrics]) -> None:
    """Print comparison table."""
    # Header
    print()
    print("=" * 120)
    print(f"{'Task ID':<45} {'BL Patch':<9} {'GT Patch':<9} {'Flip':<14} "
          f"{'BL Acts':<8} {'GT Acts':<8} {'GT Evid':<8} {'Brief':<10}")
    print("-" * 120)

    for tm in results:
        bl_patch = "YES" if tm.bl_has_patch else "no"
        gt_patch = "YES" if tm.gt_has_patch else "no"
        flip_marker = ""
        if tm.flip == "gt_gain":
            flip_marker = "+GT_GAIN"
        elif tm.flip == "gt_regression":
            flip_marker = "-REGRESS"
        elif tm.flip == "both":
            flip_marker = "=both"
        else:
            flip_marker = "=neither"

        print(f"{tm.instance_id:<45} {bl_patch:<9} {gt_patch:<9} {flip_marker:<14} "
              f"{tm.bl_total_actions:<8} {tm.gt_total_actions:<8} "
              f"{tm.gt_evidence_blocks:<8} {tm.gt_brief_status:<10}")

    print("=" * 120)


def print_aggregates(results: list[TaskMetrics]) -> None:
    """Print aggregate comparison statistics."""
    n = len(results)
    if n == 0:
        print("No tasks to compare.")
        return

    bl_patches = sum(1 for r in results if r.bl_has_patch)
    gt_patches = sum(1 for r in results if r.gt_has_patch)
    gains = sum(1 for r in results if r.flip == "gt_gain")
    regressions = sum(1 for r in results if r.flip == "gt_regression")
    both = sum(1 for r in results if r.flip == "both")
    neither = sum(1 for r in results if r.flip == "neither")

    bl_actions = [r.bl_total_actions for r in results if r.bl_total_actions > 0]
    gt_actions = [r.gt_total_actions for r in results if r.gt_total_actions > 0]
    gt_evidence = [r.gt_evidence_blocks for r in results if r.gt_evidence_blocks > 0]

    # Scaffold timing
    bl_scaff = [r.bl_first_scaffold for r in results if r.bl_first_scaffold is not None]
    gt_scaff = [r.gt_first_scaffold for r in results if r.gt_first_scaffold is not None]

    # Brief status
    brief_injected = sum(1 for r in results if r.gt_brief_status == "injected")
    brief_failed = sum(1 for r in results if r.gt_brief_status == "failed")
    brief_absent = sum(1 for r in results if r.gt_brief_status == "absent")

    # Cost
    bl_cost_total = sum(r.bl_cost for r in results)
    gt_cost_total = sum(r.gt_cost for r in results)

    print()
    print("=" * 70)
    print(" AGGREGATE COMPARISON")
    print("=" * 70)
    print()
    print(f"  Tasks compared:          {n}")
    print()
    print("  --- Patch Rate ---")
    print(f"  Baseline patches:        {bl_patches}/{n} ({100*bl_patches/n:.1f}%)")
    print(f"  GT patches:              {gt_patches}/{n} ({100*gt_patches/n:.1f}%)")
    print(f"  GT gains (flips):        {gains}")
    print(f"  GT regressions:          {regressions}")
    print(f"  Both have patch:         {both}")
    print(f"  Neither has patch:       {neither}")
    print()
    print("  --- Action Efficiency ---")
    if bl_actions:
        print(f"  BL mean actions:         {sum(bl_actions)/len(bl_actions):.1f}")
    if gt_actions:
        print(f"  GT mean actions:         {sum(gt_actions)/len(gt_actions):.1f}")
    print()
    print("  --- Scaffold Delay ---")
    if bl_scaff:
        print(f"  BL mean first scaffold:  step {sum(bl_scaff)/len(bl_scaff):.1f} "
              f"({len(bl_scaff)}/{n} tasks scaffold)")
    if gt_scaff:
        print(f"  GT mean first scaffold:  step {sum(gt_scaff)/len(gt_scaff):.1f} "
              f"({len(gt_scaff)}/{n} tasks scaffold)")
    print()
    print("  --- GT Layer Health ---")
    print(f"  Brief injected:          {brief_injected}/{n}")
    print(f"  Brief failed:            {brief_failed}/{n}")
    print(f"  Brief absent/unknown:    {brief_absent}/{n}")
    if gt_evidence:
        print(f"  Mean GT evidence blocks: {sum(gt_evidence)/len(gt_evidence):.1f} "
              f"(across {len(gt_evidence)} tasks with evidence)")
    print()
    print("  --- Cost ---")
    print(f"  BL total LLM cost:       ${bl_cost_total:.2f}")
    print(f"  GT total LLM cost:       ${gt_cost_total:.2f}")
    print()

    # Verdict
    print("  --- VERDICT ---")
    if gt_patches > bl_patches and regressions == 0:
        print(f"  CLEAR WIN: GT +{gt_patches - bl_patches} patches, 0 regressions")
    elif gt_patches > bl_patches:
        print(f"  NET POSITIVE: GT +{gt_patches - bl_patches} patches, "
              f"but {regressions} regression(s) to investigate")
    elif gt_patches == bl_patches and regressions > 0:
        print(f"  NEUTRAL-TO-NEGATIVE: same patch count but {regressions} regression(s)")
    elif gt_patches < bl_patches:
        print(f"  REGRESSION: GT lost {bl_patches - gt_patches} patches vs baseline")
    else:
        print(f"  NO CHANGE: {gt_patches} patches in both arms")
    print("=" * 70)


def print_flips(results: list[TaskMetrics]) -> None:
    """Print detailed flip analysis."""
    gains = [r for r in results if r.flip == "gt_gain"]
    regressions = [r for r in results if r.flip == "gt_regression"]

    if gains:
        print()
        print("  GT GAINS (baseline missed, GT found):")
        for r in gains:
            print(f"    + {r.instance_id}  "
                  f"(GT: {r.gt_total_actions} acts, {r.gt_evidence_blocks} evid, "
                  f"brief={r.gt_brief_status})")

    if regressions:
        print()
        print("  GT REGRESSIONS (baseline found, GT missed):")
        for r in regressions:
            print(f"    - {r.instance_id}  "
                  f"(GT: {r.gt_total_actions} acts, {r.gt_evidence_blocks} evid, "
                  f"brief={r.gt_brief_status})")
        print()
        print("  ACTION REQUIRED: Investigate each regression trajectory.")
        print("  Possible causes: bad brief misdirecting, evidence noise, hook overhead.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compare 30-task GT run vs baseline")
    parser.add_argument("--gt", type=Path, help="Merged GT output.jsonl (alternative to --gt-t0/--gt-v1)")
    parser.add_argument("--gt-t0", type=Path, help="GT output.jsonl from gt-t0 VM")
    parser.add_argument("--gt-v1", type=Path, help="GT output.jsonl from gt-v1 VM")
    parser.add_argument(
        "--baseline-t0", type=Path,
        default=Path(r"D:\tmp\gt_test\results_final\baseline_t0.jsonl"),
        help="Baseline output.jsonl (t0 shard)",
    )
    parser.add_argument(
        "--baseline-v1", type=Path,
        default=Path(r"D:\tmp\gt_test\results_final\baseline_v1.jsonl"),
        help="Baseline output.jsonl (v1 shard)",
    )
    parser.add_argument("--json", type=Path, help="Write full results as JSON to this path")
    args = parser.parse_args()

    # Load baseline
    print("Loading baseline...")
    baseline = {}
    baseline.update(load_jsonl(args.baseline_t0))
    baseline.update(load_jsonl(args.baseline_v1))
    print(f"  Baseline tasks: {len(baseline)}")

    # Load GT arm
    print("Loading GT arm...")
    gt_arm: dict[str, dict[str, Any]] = {}
    if args.gt:
        gt_arm.update(load_jsonl(args.gt))
    else:
        if args.gt_t0:
            gt_arm.update(load_jsonl(args.gt_t0))
        if args.gt_v1:
            gt_arm.update(load_jsonl(args.gt_v1))
    print(f"  GT tasks: {len(gt_arm)}")

    if not gt_arm:
        print("\nERROR: No GT results loaded. Provide --gt or --gt-t0/--gt-v1.", file=sys.stderr)
        sys.exit(1)

    # Compare
    results = compare(baseline, gt_arm)

    # Output
    print_table(results)
    print_aggregates(results)
    print_flips(results)

    # Optional JSON export
    if args.json:
        export = []
        for r in results:
            export.append({
                "instance_id": r.instance_id,
                "bl_has_patch": r.bl_has_patch,
                "gt_has_patch": r.gt_has_patch,
                "flip": r.flip,
                "bl_total_actions": r.bl_total_actions,
                "gt_total_actions": r.gt_total_actions,
                "gt_evidence_blocks": r.gt_evidence_blocks,
                "gt_brief_status": r.gt_brief_status,
                "bl_cost": r.bl_cost,
                "gt_cost": r.gt_cost,
            })
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2)
        print(f"\nJSON results written to: {args.json}")


if __name__ == "__main__":
    main()
