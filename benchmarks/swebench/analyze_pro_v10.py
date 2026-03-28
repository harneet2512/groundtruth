#!/usr/bin/env python3
"""Analyze v10 Pro A/B results: resolution rates, GT utilization, gained/lost tasks.

Usage:
    python3 analyze_pro_v10.py --ab-dir=benchmarks/swebench/results/pro_v10_50task_YYYYMMDD_HHMM
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_preds(preds_path: Path) -> dict[str, str]:
    """Load instance_id -> patch from preds.json."""
    if not preds_path.exists():
        return {}
    with open(preds_path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return {item["instance_id"]: item.get("model_patch", "") for item in data}
    if isinstance(data, dict):
        return data
    return {}


def load_eval_results(eval_dir: Path) -> dict[str, bool]:
    """Load instance_id -> resolved from eval output."""
    results: dict[str, bool] = {}
    report_path = eval_dir / "report.json"
    if report_path.exists():
        with open(report_path) as f:
            report = json.load(f)
        for instance_id, outcome in report.items():
            if isinstance(outcome, dict):
                results[instance_id] = outcome.get("resolved", False)
            elif isinstance(outcome, bool):
                results[instance_id] = outcome
        return results

    # Fallback: check individual instance dirs
    for item in eval_dir.iterdir():
        if item.is_dir():
            test_output = item / "test_output.txt"
            if test_output.exists():
                content = test_output.read_text()
                results[item.name] = "PASSED" in content or "passed" in content
    return results


def load_gt_logs(gt_logs_dir: Path) -> dict[str, list[dict]]:
    """Load GT hook logs per instance."""
    logs: dict[str, list[dict]] = {}
    if not gt_logs_dir.exists():
        return logs
    for logfile in gt_logs_dir.glob("*.jsonl"):
        instance_id = logfile.stem
        entries = []
        for line in logfile.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        logs[instance_id] = entries
    return logs


def analyze_gt_utilization(gt_logs: dict[str, list[dict]]) -> dict:
    """Compute GT utilization metrics from hook logs."""
    total = len(gt_logs)
    hook_fired = 0
    test_assertions_shown = 0
    ego_graph_shown = 0
    sibling_shown = 0
    suppressed = 0

    for instance_id, entries in gt_logs.items():
        analyze_entries = [e for e in entries if e.get("endpoint") == "analyze"]
        if analyze_entries:
            hook_fired += 1

        for entry in analyze_entries:
            ta = entry.get("test_assertions", {})
            if ta.get("assertions_extracted", 0) > 0:
                test_assertions_shown += 1
                break

        for entry in analyze_entries:
            eg = entry.get("ego_graph", {})
            if eg.get("total_nodes", 0) >= 2:
                ego_graph_shown += 1
                break

        for entry in analyze_entries:
            sib = entry.get("sibling", {})
            if sib.get("found", False):
                sibling_shown += 1
                break

        for entry in analyze_entries:
            if entry.get("suppressed", False):
                suppressed += 1
                break

    return {
        "total_instances": total,
        "hook_fired": hook_fired,
        "test_assertions_shown": test_assertions_shown,
        "ego_graph_shown": ego_graph_shown,
        "sibling_shown": sibling_shown,
        "suppressed": suppressed,
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze v10 Pro A/B results")
    parser.add_argument("--ab-dir", required=True, help="Path to A/B results directory")
    args = parser.parse_args()

    ab_dir = Path(args.ab_dir)
    if not ab_dir.exists():
        print(f"ERROR: Directory not found: {ab_dir}", file=sys.stderr)
        sys.exit(1)

    # Load eval results
    baseline_results = load_eval_results(ab_dir / "eval_baseline")
    gt_results = load_eval_results(ab_dir / "eval_gt_v10")

    # Load predictions (for counting submitted patches)
    baseline_preds = load_preds(ab_dir / "baseline" / "preds.json")
    gt_preds = load_preds(ab_dir / "gt_v10" / "preds.json")

    # Load GT logs
    gt_logs = load_gt_logs(ab_dir / "gt_v10" / "gt_logs")

    # ─── Resolution Rates ───
    all_ids = sorted(set(baseline_results.keys()) | set(gt_results.keys()))
    common_ids = sorted(set(baseline_results.keys()) & set(gt_results.keys()))

    baseline_resolved = sum(1 for iid in common_ids if baseline_results.get(iid, False))
    gt_resolved = sum(1 for iid in common_ids if gt_results.get(iid, False))

    gained = []  # resolved by GT, not by baseline
    lost = []    # resolved by baseline, not by GT
    both_pass = []
    both_fail = []

    for iid in common_ids:
        b = baseline_results.get(iid, False)
        g = gt_results.get(iid, False)
        if g and not b:
            gained.append(iid)
        elif b and not g:
            lost.append(iid)
        elif b and g:
            both_pass.append(iid)
        else:
            both_fail.append(iid)

    print("=" * 70)
    print("  GroundTruth v10 Pro — A/B Analysis")
    print("=" * 70)
    print()

    print("RESOLUTION:")
    print(f"  Baseline: {baseline_resolved}/{len(common_ids)}")
    print(f"  GT v10:   {gt_resolved}/{len(common_ids)}")
    print(f"  Delta:    {gt_resolved - baseline_resolved:+d}")
    print()

    print("BREAKDOWN:")
    print(f"  Both pass:  {len(both_pass)}")
    print(f"  Both fail:  {len(both_fail)}")
    print(f"  GT gained:  {len(gained)}")
    print(f"  GT lost:    {len(lost)}")
    print()

    if gained:
        print("GAINED (GT resolved, baseline failed):")
        for iid in gained:
            print(f"  + {iid}")
        print()

    if lost:
        print("LOST (baseline resolved, GT failed):")
        for iid in lost:
            print(f"  - {iid}")
        print()

    # ─── GT Utilization ───
    if gt_logs:
        util = analyze_gt_utilization(gt_logs)
        print("GT UTILIZATION:")
        print(f"  Hook fired:            {util['hook_fired']}/{util['total_instances']}")
        print(f"  Test assertions shown: {util['test_assertions_shown']}/{util['total_instances']}")
        print(f"  Ego-graph shown:       {util['ego_graph_shown']}/{util['total_instances']}")
        print(f"  Sibling shown:         {util['sibling_shown']}/{util['total_instances']}")
        print(f"  Output suppressed:     {util['suppressed']}/{util['total_instances']}")
        print()

        # Signal contribution for gained tasks
        if gained:
            print("SIGNAL CONTRIBUTION (gained tasks):")
            for iid in gained:
                entries = gt_logs.get(iid, [])
                analyze_entries = [e for e in entries if e.get("endpoint") == "analyze"]
                signals = []
                for entry in analyze_entries:
                    if entry.get("test_assertions", {}).get("assertions_extracted", 0) > 0:
                        signals.append("tests")
                    if entry.get("ego_graph", {}).get("total_nodes", 0) >= 2:
                        signals.append("ego-graph")
                    if entry.get("sibling", {}).get("found", False):
                        signals.append("sibling")
                print(f"  {iid}: {', '.join(set(signals)) if signals else 'no signals logged'}")
            print()
    else:
        print("GT UTILIZATION: No GT logs found.")
        print()

    # ─── Patch Stats ───
    print("PATCH STATS:")
    baseline_patched = sum(1 for p in baseline_preds.values() if p and p.strip())
    gt_patched = sum(1 for p in gt_preds.values() if p and p.strip())
    print(f"  Baseline patches submitted: {baseline_patched}/{len(baseline_preds)}")
    print(f"  GT v10 patches submitted:   {gt_patched}/{len(gt_preds)}")
    print()

    # Save summary
    summary = {
        "common_tasks": len(common_ids),
        "baseline_resolved": baseline_resolved,
        "gt_resolved": gt_resolved,
        "delta": gt_resolved - baseline_resolved,
        "gained": gained,
        "lost": lost,
        "both_pass": len(both_pass),
        "both_fail": len(both_fail),
    }
    if gt_logs:
        summary["gt_utilization"] = analyze_gt_utilization(gt_logs)

    summary_path = ab_dir / "analysis_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to: {summary_path}")


if __name__ == "__main__":
    main()
