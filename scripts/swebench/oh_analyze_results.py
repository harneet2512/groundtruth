#!/usr/bin/env python3
"""Analyze OpenHands SWE-bench results: baseline vs GT gt_check.

Reads eval output.jsonl files from both conditions, computes:
- Resolve rates
- Task-level diffs (flips)
- GT tool usage stats (from trajectories)
- Comparison with published and Inspect baselines

Usage:
    python oh_analyze_results.py \
        --baseline ~/results/baseline/output.jsonl \
        --gt ~/results/gt/output.jsonl \
        --output OPENHANDS_GT_RESULTS.md
"""

import argparse
import json
from pathlib import Path


def load_results(path: str) -> dict[str, dict]:
    """Load output.jsonl, return dict keyed by instance_id."""
    results = {}
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            iid = entry.get("instance_id", entry.get("id", ""))
            results[iid] = entry
    return results


def is_resolved(entry: dict) -> bool:
    """Check if a task was resolved (patch passes tests)."""
    # OpenHands eval format: check test_result or resolved field
    if "resolved" in entry:
        return bool(entry["resolved"])
    test_result = entry.get("test_result", {})
    if isinstance(test_result, dict):
        return bool(test_result.get("resolved", False))
    return False


def count_gt_check_usage(results_dir: str) -> dict:
    """Scan trajectories for gt_check usage."""
    results_path = Path(results_dir)
    stats = {"total_calls": 0, "tasks_with_calls": 0, "tasks_total": 0}

    # Look for trajectory/event files
    for traj_file in results_path.rglob("*.json"):
        if "output" in traj_file.name:
            continue
        try:
            content = traj_file.read_text()
            calls = content.count("groundtruth_check") + content.count("gt_tool.py")
            stats["tasks_total"] += 1
            if calls > 0:
                stats["total_calls"] += calls
                stats["tasks_with_calls"] += 1
        except Exception:
            pass

    return stats


def analyze(baseline_path: str, gt_path: str) -> dict:
    """Run full analysis."""
    baseline = load_results(baseline_path)
    gt = load_results(gt_path)

    # Common tasks
    common_ids = set(baseline.keys()) & set(gt.keys())

    baseline_resolved = {iid for iid in common_ids if is_resolved(baseline[iid])}
    gt_resolved = {iid for iid in common_ids if is_resolved(gt[iid])}

    # Flips
    gt_wins = gt_resolved - baseline_resolved  # GT solved, baseline didn't
    gt_losses = baseline_resolved - gt_resolved  # baseline solved, GT didn't
    both_solved = baseline_resolved & gt_resolved
    neither_solved = common_ids - baseline_resolved - gt_resolved

    return {
        "total_common": len(common_ids),
        "baseline_total": len(baseline),
        "gt_total": len(gt),
        "baseline_resolved": len(baseline_resolved),
        "gt_resolved": len(gt_resolved),
        "baseline_rate": len(baseline_resolved) / len(common_ids) * 100 if common_ids else 0,
        "gt_rate": len(gt_resolved) / len(common_ids) * 100 if common_ids else 0,
        "delta": len(gt_resolved) - len(baseline_resolved),
        "delta_pp": (len(gt_resolved) - len(baseline_resolved)) / len(common_ids) * 100 if common_ids else 0,
        "gt_wins": sorted(gt_wins),
        "gt_losses": sorted(gt_losses),
        "both_solved": len(both_solved),
        "neither_solved": len(neither_solved),
    }


def format_report(analysis: dict, gt_stats: dict | None = None) -> str:
    """Format analysis as markdown report."""
    lines = [
        "# OpenHands + GT gt_check — SWE-bench Verified Results",
        "",
        f"**Date:** {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}",
        f"**Model:** Qwen3-Coder-480B via Vertex AI",
        f"**Scaffold:** OpenHands (benchmarks repo)",
        f"**GT tool:** gt_check v2 (live re-indexing, noise suppression, hard blocker format)",
        "",
        "## Primary Results",
        "",
        "| Condition | Resolved | Total | Rate | Delta |",
        "|-----------|----------|-------|------|-------|",
        f"| OpenHands Baseline | {analysis['baseline_resolved']} | {analysis['total_common']} | {analysis['baseline_rate']:.1f}% | — |",
        f"| OpenHands + gt_check v2 | {analysis['gt_resolved']} | {analysis['total_common']} | {analysis['gt_rate']:.1f}% | {analysis['delta']:+d} ({analysis['delta_pp']:+.1f}pp) |",
        f"| Published Qwen3-Coder + OpenHands | 335 | 500 | 67.0% | reference |",
        f"| Our Inspect Baseline (previous) | 288 | 500 | 57.6% | reference |",
        "",
        "## Task-Level Analysis",
        "",
        f"- **GT wins** (GT solved, baseline didn't): {len(analysis['gt_wins'])}",
        f"- **GT losses** (baseline solved, GT didn't): {len(analysis['gt_losses'])}",
        f"- **Both solved:** {analysis['both_solved']}",
        f"- **Neither solved:** {analysis['neither_solved']}",
        f"- **Net flip:** {len(analysis['gt_wins']) - len(analysis['gt_losses']):+d}",
        "",
    ]

    if analysis["gt_wins"]:
        lines.extend([
            "### GT Wins (tasks GT solved that baseline missed)",
            "",
        ])
        for iid in analysis["gt_wins"][:20]:
            lines.append(f"- `{iid}`")
        if len(analysis["gt_wins"]) > 20:
            lines.append(f"- ... and {len(analysis['gt_wins']) - 20} more")
        lines.append("")

    if analysis["gt_losses"]:
        lines.extend([
            "### GT Losses (tasks baseline solved that GT missed)",
            "",
        ])
        for iid in analysis["gt_losses"][:20]:
            lines.append(f"- `{iid}`")
        if len(analysis["gt_losses"]) > 20:
            lines.append(f"- ... and {len(analysis['gt_losses']) - 20} more")
        lines.append("")

    if gt_stats:
        lines.extend([
            "## GT Tool Usage",
            "",
            f"- Total gt_check references in trajectories: {gt_stats['total_calls']}",
            f"- Tasks with gt_check calls: {gt_stats['tasks_with_calls']} / {gt_stats['tasks_total']}",
            f"- Adoption rate: {gt_stats['tasks_with_calls'] / gt_stats['tasks_total'] * 100:.1f}%" if gt_stats['tasks_total'] > 0 else "- Adoption rate: N/A",
            "",
        ])

    lines.extend([
        "## Scaffold Validation",
        "",
        f"Baseline resolve rate: {analysis['baseline_rate']:.1f}%",
        f"Published Qwen3-Coder + OpenHands: 67.0%",
        f"Gap: {analysis['baseline_rate'] - 67.0:+.1f}pp",
        "",
    ])

    if analysis["baseline_rate"] >= 60:
        lines.append("Scaffold is correctly configured (within 7pp of published).")
    elif analysis["baseline_rate"] >= 55:
        lines.append("Scaffold is close but below published. Check model params and iteration limits.")
    else:
        lines.append("WARNING: Scaffold significantly below published. Investigate configuration.")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze OpenHands GT results")
    parser.add_argument("--baseline", required=True, help="Path to baseline output.jsonl")
    parser.add_argument("--gt", required=True, help="Path to GT output.jsonl")
    parser.add_argument("--gt-dir", default=None, help="Path to GT results dir for trajectory analysis")
    parser.add_argument("--output", default="OPENHANDS_GT_RESULTS.md", help="Output markdown file")
    args = parser.parse_args()

    analysis = analyze(args.baseline, args.gt)

    gt_stats = None
    if args.gt_dir:
        gt_stats = count_gt_check_usage(args.gt_dir)

    report = format_report(analysis, gt_stats)

    Path(args.output).write_text(report)
    print(report)
    print(f"\nWritten to: {args.output}")


if __name__ == "__main__":
    main()
