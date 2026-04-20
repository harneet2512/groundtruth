#!/usr/bin/env python3
"""Aggregate repeated 10-task GT runs into decision-grade summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, pvariance


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_task_logs(run_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not run_dir.exists():
        return out
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        log = _load_json(task_dir / "gt_task_log.json")
        if log:
            out.append(log)
    return out


def _arm_metrics(run_dir: Path) -> dict:
    logs = _load_task_logs(run_dir)
    if not logs:
        return {
            "run_dir": str(run_dir),
            "task_count": 0,
            "resolved_count": 0,
            "regressions": 0,
            "gains": 0,
            "material_edit_total": 0,
            "ack_armed_total": 0,
            "steer_delivered_total": 0,
            "ack_engagement_total": 0,
            "behavior_shift_total": 0,
            "infra_contaminated_count": 0,
            "budget_state_present_count": 0,
            "budget_state_present_rate": 0.0,
            "gt_orient_calls_total": 0,
            "arm_coverage": 0.0,
            "delivery_rate": 0.0,
            "engagement_rate": 0.0,
            "behavior_shift_rate": 0.0,
            "ack_follow_rate": 0.0,
            "not_observed_rate": 0.0,
            "ready_for_comparison": False,
        }

    resolved_count = 0
    gains = 0
    regressions = 0
    material_total = 0
    ack_armed_total = 0
    delivered_total = 0
    engagement_total = 0
    follow_total = 0
    not_observed_total = 0
    behavior_windows = 0
    behavior_shifted = 0
    infra_contaminated = 0
    budget_state_present = 0
    gt_orient_calls_total = 0

    for log in logs:
        outcome = log.get("outcome") or {}
        if outcome.get("resolved") is True:
            resolved_count += 1
        if outcome.get("is_gain"):
            gains += 1
        if outcome.get("is_regression"):
            regressions += 1
        material_total += int(log.get("material_edit_count", 0) or 0)
        ack_armed_total += int(log.get("ack_armed_count", 0) or 0)
        delivered_total += int(log.get("steer_delivered_count", 0) or 0)
        engagement_total += int(log.get("ack_engagement_count", 0) or 0)
        follow_total += int(log.get("ack_followed_count", 0) or 0)
        not_observed_total += int(log.get("ack_not_observed_count", 0) or 0)
        behavior = log.get("behavior_shift") or {}
        behavior_windows += int(log.get("behavior_shift_count", 0) or len(behavior.get("windows", [])) or 0)
        behavior_shifted += int(behavior.get("weak_behavior_shift", 0) or 0) + int(behavior.get("clear_behavior_shift", 0) or 0)
        infra_contaminated += int(bool(log.get("infra_contaminated")))
        budget_state_present += int(bool(log.get("budget_state_present")))
        gt_orient_calls_total += int(log.get("gt_orient_calls_per_task", 0) or 0)

    arm_coverage = (ack_armed_total / material_total) if material_total else 0.0
    delivery_rate = (delivered_total / ack_armed_total) if ack_armed_total else 0.0
    engagement_rate = (engagement_total / delivered_total) if delivered_total else 0.0
    behavior_shift_rate = (behavior_shifted / behavior_windows) if behavior_windows else 0.0
    ack_follow_rate = (follow_total / ack_armed_total) if ack_armed_total else 0.0
    not_observed_rate = (not_observed_total / ack_armed_total) if ack_armed_total else 0.0
    budget_state_present_rate = (budget_state_present / len(logs)) if logs else 0.0
    ready = (
        arm_coverage >= 0.6
        and delivery_rate >= 0.9
        and material_total > 0
        and infra_contaminated == 0
    )

    return {
        "run_dir": str(run_dir),
        "task_count": len(logs),
        "resolved_count": resolved_count,
        "regressions": regressions,
        "gains": gains,
        "material_edit_total": material_total,
        "ack_armed_total": ack_armed_total,
        "steer_delivered_total": delivered_total,
        "ack_engagement_total": engagement_total,
        "behavior_shift_total": behavior_windows,
        "behavior_shifted_total": behavior_shifted,
        "infra_contaminated_count": infra_contaminated,
        "budget_state_present_count": budget_state_present,
        "budget_state_present_rate": budget_state_present_rate,
        "gt_orient_calls_total": gt_orient_calls_total,
        "arm_coverage": arm_coverage,
        "delivery_rate": delivery_rate,
        "engagement_rate": engagement_rate,
        "behavior_shift_rate": behavior_shift_rate,
        "ack_follow_rate": ack_follow_rate,
        "not_observed_rate": not_observed_rate,
        "ready_for_comparison": ready,
    }


def _mean_var(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    if len(vals) == 1:
        return vals[0], 0.0
    return mean(vals), pvariance(vals)


def _block(label: str, runs: list[dict]) -> list[str]:
    lines = [f"## {label}", "", "| repeat | resolved | regressions | gains | arm_cov | delivery | engage | shift | infra | bud | ready |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for idx, run in enumerate(runs, start=1):
        lines.append(
            f"| {idx} | {run['resolved_count']} | {run['regressions']} | {run['gains']} | "
            f"{run['arm_coverage']:.0%} | {run['delivery_rate']:.0%} | {run['engagement_rate']:.0%} | "
            f"{run['behavior_shift_rate']:.0%} | {run['infra_contaminated_count']} | {run['budget_state_present_count']} | "
            f"{'yes' if run['ready_for_comparison'] else 'no'} |"
        )
    resolved_vals = [float(r["resolved_count"]) for r in runs]
    reg_vals = [float(r["regressions"]) for r in runs]
    gain_vals = [float(r["gains"]) for r in runs]
    mean_res, var_res = _mean_var(resolved_vals)
    mean_reg, var_reg = _mean_var(reg_vals)
    mean_gain, var_gain = _mean_var(gain_vals)
    lines += [
        "",
        f"- mean resolved: **{mean_res:.2f}**",
        f"- resolved variance: **{var_res:.2f}**",
        f"- mean regressions: **{mean_reg:.2f}**",
        f"- regression variance: **{var_reg:.2f}**",
        f"- mean gains: **{mean_gain:.2f}**",
        f"- gain variance: **{var_gain:.2f}**",
        "",
    ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--nolsp", nargs="+", required=True)
    ap.add_argument("--lsp", nargs="+", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--markdown", required=True)
    args = ap.parse_args()

    baseline = _arm_metrics(Path(args.baseline))
    nolsp = [_arm_metrics(Path(p)) for p in args.nolsp]
    lsp = [_arm_metrics(Path(p)) for p in args.lsp]

    nolsp_res_vals = [float(r["resolved_count"]) for r in nolsp]
    lsp_res_vals = [float(r["resolved_count"]) for r in lsp]
    nolsp_mean, nolsp_var = _mean_var(nolsp_res_vals)
    lsp_mean, lsp_var = _mean_var(lsp_res_vals)
    nolsp_delivery_mean, _ = _mean_var([float(r["delivery_rate"]) for r in nolsp])
    lsp_delivery_mean, _ = _mean_var([float(r["delivery_rate"]) for r in lsp])
    nolsp_infra_mean, _ = _mean_var([float(r["infra_contaminated_count"]) for r in nolsp])
    lsp_infra_mean, _ = _mean_var([float(r["infra_contaminated_count"]) for r in lsp])
    nolsp_arm_cov_mean, _ = _mean_var([float(r["arm_coverage"]) for r in nolsp])
    lsp_arm_cov_mean, _ = _mean_var([float(r["arm_coverage"]) for r in lsp])
    nolsp_ready = sum(1 for r in nolsp if r["ready_for_comparison"])
    lsp_ready = sum(1 for r in lsp if r["ready_for_comparison"])

    decision = "inconclusive"
    if nolsp and lsp:
        nolsp_score = (nolsp_mean, -nolsp_var, nolsp_delivery_mean, -nolsp_infra_mean, nolsp_arm_cov_mean)
        lsp_score = (lsp_mean, -lsp_var, lsp_delivery_mean, -lsp_infra_mean, lsp_arm_cov_mean)
        if nolsp_score > lsp_score:
            decision = "nolsp"
        elif lsp_score > nolsp_score:
            decision = "lsp-hybrid"

    out = {
        "baseline": baseline,
        "nolsp": nolsp,
        "lsp": lsp,
        "decision": decision,
        "summary": {
            "baseline_resolved": baseline["resolved_count"],
            "nolsp_mean_resolved": nolsp_mean,
            "nolsp_variance": nolsp_var,
            "lsp_mean_resolved": lsp_mean,
            "lsp_variance": lsp_var,
            "nolsp_mean_delivery_rate": nolsp_delivery_mean,
            "lsp_mean_delivery_rate": lsp_delivery_mean,
            "nolsp_mean_infra_contaminated": nolsp_infra_mean,
            "lsp_mean_infra_contaminated": lsp_infra_mean,
            "nolsp_mean_arm_coverage": nolsp_arm_cov_mean,
            "lsp_mean_arm_coverage": lsp_arm_cov_mean,
            "nolsp_ready_count": nolsp_ready,
            "lsp_ready_count": lsp_ready,
        },
    }
    Path(args.output).write_text(json.dumps(out, indent=2), encoding="utf-8")

    md = [
        "# GT Repeat Summary",
        "",
        f"- baseline resolved: **{baseline['resolved_count']}**",
        f"- decision: **{decision}**",
        "",
    ]
    md.extend(_block("nolsp repeats", nolsp))
    md.extend(_block("lsp repeats", lsp))
    md += [
        "## Baseline",
        "",
        f"- run: `{baseline['run_dir']}`",
        f"- resolved: **{baseline['resolved_count']}**",
        f"- regressions: **{baseline['regressions']}**",
        f"- gains: **{baseline['gains']}**",
        f"- arm coverage: **{baseline['arm_coverage']:.0%}**",
        f"- delivery: **{baseline['delivery_rate']:.0%}**",
        f"- engagement: **{baseline['engagement_rate']:.0%}**",
        f"- infra contaminated: **{baseline['infra_contaminated_count']}**",
        f"- budget state present: **{baseline['budget_state_present_count']}**",
    ]
    Path(args.markdown).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
