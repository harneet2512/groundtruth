"""Analyze SWE-bench results: baseline vs GroundTruth comparison with confidence intervals."""

from __future__ import annotations

import json
import math
from pathlib import Path


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def load_predictions(path: Path) -> list[dict]:
    """Load predictions from JSONL file."""
    predictions = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                predictions.append(json.loads(line))
    return predictions


def load_eval_results(results_dir: Path) -> dict:
    """Load evaluation results from the eval_results directory."""
    eval_file = results_dir / "eval_results" / "evaluation.json"
    if eval_file.exists():
        return json.loads(eval_file.read_text())
    return {}


def analyze(
    baseline_dir: Path,
    groundtruth_dir: Path,
    output_path: Path | None = None,
) -> dict:
    """
    Compare baseline vs GroundTruth SWE-bench results.

    Both dirs should contain predictions.jsonl and eval_results/.
    """
    # Load predictions
    baseline_preds = load_predictions(baseline_dir / "predictions.jsonl")
    gt_preds = load_predictions(groundtruth_dir / "predictions.jsonl")

    # Load eval results
    baseline_eval = load_eval_results(baseline_dir)
    gt_eval = load_eval_results(groundtruth_dir)

    # Load cost reports
    baseline_cost = {}
    gt_cost = {}
    baseline_cost_file = baseline_dir / "cost_report.json"
    gt_cost_file = groundtruth_dir / "cost_report.json"
    if baseline_cost_file.exists():
        baseline_cost = json.loads(baseline_cost_file.read_text())
    if gt_cost_file.exists():
        gt_cost = json.loads(gt_cost_file.read_text())

    # Count resolved
    baseline_resolved = sum(1 for p in baseline_preds if p.get("model_patch", "").strip())
    gt_resolved = sum(1 for p in gt_preds if p.get("model_patch", "").strip())
    total_baseline = len(baseline_preds)
    total_gt = len(gt_preds)

    # If we have real eval results, use those instead
    if baseline_eval:
        baseline_resolved = sum(
            1 for k, v in baseline_eval.items()
            if not k.startswith("__") and (
                (isinstance(v, dict) and v.get("resolved")) or
                (isinstance(v, bool) and v)
            )
        )
    if gt_eval:
        gt_resolved = sum(
            1 for k, v in gt_eval.items()
            if not k.startswith("__") and (
                (isinstance(v, dict) and v.get("resolved")) or
                (isinstance(v, bool) and v)
            )
        )

    # Compute rates and CIs
    baseline_rate = baseline_resolved / max(total_baseline, 1) * 100
    gt_rate = gt_resolved / max(total_gt, 1) * 100
    delta = gt_rate - baseline_rate

    baseline_ci = wilson_ci(baseline_resolved, total_baseline)
    gt_ci = wilson_ci(gt_resolved, total_gt)

    # Per-task comparison
    baseline_by_id = {p["instance_id"]: p for p in baseline_preds}
    gt_by_id = {p["instance_id"]: p for p in gt_preds}
    common_ids = set(baseline_by_id.keys()) & set(gt_by_id.keys())

    gained = []  # GT solved, baseline didn't
    lost = []    # Baseline solved, GT didn't
    both = []    # Both solved
    neither = []  # Neither solved

    for iid in sorted(common_ids):
        b_patch = bool(baseline_by_id[iid].get("model_patch", "").strip())
        g_patch = bool(gt_by_id[iid].get("model_patch", "").strip())
        if g_patch and not b_patch:
            gained.append(iid)
        elif b_patch and not g_patch:
            lost.append(iid)
        elif b_patch and g_patch:
            both.append(iid)
        else:
            neither.append(iid)

    report = {
        "summary": {
            "baseline": {
                "resolved": baseline_resolved,
                "total": total_baseline,
                "pass_rate": round(baseline_rate, 2),
                "ci_95": [round(baseline_ci[0] * 100, 2), round(baseline_ci[1] * 100, 2)],
            },
            "groundtruth": {
                "resolved": gt_resolved,
                "total": total_gt,
                "pass_rate": round(gt_rate, 2),
                "ci_95": [round(gt_ci[0] * 100, 2), round(gt_ci[1] * 100, 2)],
            },
            "delta": round(delta, 2),
            "relative_improvement": round(delta / max(baseline_rate, 0.01) * 100, 1),
        },
        "per_task": {
            "gained": gained,
            "lost": lost,
            "both_solved": len(both),
            "neither_solved": len(neither),
        },
        "cost": {
            "baseline_total": baseline_cost.get("total_cost", "N/A"),
            "groundtruth_total": gt_cost.get("total_cost", "N/A"),
        },
    }

    # Generate markdown report
    md = generate_markdown(report)
    report["markdown"] = md

    # Save
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))

        md_path = output_path.with_suffix(".md")
        md_path.write_text(md)

    return report


def generate_markdown(report: dict) -> str:
    """Generate a markdown comparison report."""
    s = report["summary"]
    b = s["baseline"]
    g = s["groundtruth"]

    lines = [
        "# SWE-bench Results: Baseline vs GroundTruth",
        "",
        "## Summary",
        "",
        "| Metric | Baseline | GroundTruth | Delta |",
        "|--------|----------|-------------|-------|",
        f"| Resolved | {b['resolved']}/{b['total']} | {g['resolved']}/{g['total']} | {'+' if s['delta'] >= 0 else ''}{s['delta']}pp |",
        f"| Pass Rate | {b['pass_rate']}% | {g['pass_rate']}% | {'+' if s['delta'] >= 0 else ''}{s['delta']}pp |",
        f"| 95% CI | [{b['ci_95'][0]}%, {b['ci_95'][1]}%] | [{g['ci_95'][0]}%, {g['ci_95'][1]}%] | |",
        f"| Relative Improvement | — | — | {s['relative_improvement']}% |",
        "",
        "## Per-Task Analysis",
        "",
        f"- **Gained** (GT solved, baseline didn't): {len(report['per_task']['gained'])}",
        f"- **Lost** (baseline solved, GT didn't): {len(report['per_task']['lost'])}",
        f"- **Both solved**: {report['per_task']['both_solved']}",
        f"- **Neither solved**: {report['per_task']['neither_solved']}",
        "",
    ]

    if report["per_task"]["gained"]:
        lines.append("### Tasks Gained by GroundTruth")
        lines.append("")
        for iid in report["per_task"]["gained"]:
            lines.append(f"- `{iid}`")
        lines.append("")

    if report["per_task"]["lost"]:
        lines.append("### Tasks Lost by GroundTruth")
        lines.append("")
        for iid in report["per_task"]["lost"]:
            lines.append(f"- `{iid}`")
        lines.append("")

    c = report["cost"]
    lines.extend([
        "## Cost",
        "",
        f"- Baseline: ${c['baseline_total']}",
        f"- GroundTruth: ${c['groundtruth_total']}",
    ])

    return "\n".join(lines)


def annotate_gt_catches(predictions: list[dict]) -> list[dict]:
    """Annotate predictions with GT validation catch data from gt_report.

    For each prediction that has a gt_report, extract validation_log entries
    and correlate with resolved status.

    Returns annotated list of dicts with GT catch info.
    """
    annotated: list[dict] = []
    for pred in predictions:
        gt_report = pred.get("gt_report")
        if not gt_report:
            continue

        instr = gt_report.get("instrumentation", {})
        val_log = gt_report.get("validation_log", [])

        entry: dict = {
            "instance_id": pred["instance_id"],
            "has_patch": bool(pred.get("model_patch", "").strip()),
            "validations_fired": int(instr.get("validations_fired", 0)),
            "agent_fixed_after_validation": int(instr.get("agent_fixed_after_validation", 0)),
            "validation_timeouts": int(instr.get("validation_timeouts", 0)),
            "catches": [],
        }

        for log_entry in val_log:
            for finding in log_entry.get("findings", []):
                entry["catches"].append({
                    "file": log_entry.get("file_path", ""),
                    "error_type": finding.get("error_type", ""),
                    "symbol": finding.get("symbol", ""),
                    "confidence": finding.get("confidence", 0),
                })

        annotated.append(entry)

    return annotated


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze SWE-bench results")
    parser.add_argument("--baseline", type=Path, required=True, help="Baseline results directory")
    parser.add_argument("--groundtruth", type=Path, required=True, help="GroundTruth results directory")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/swebench/results/analysis.json"))
    args = parser.parse_args()

    report = analyze(args.baseline, args.groundtruth, args.output)

    print(report["markdown"])


if __name__ == "__main__":
    main()
