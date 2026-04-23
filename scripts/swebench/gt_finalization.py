#!/usr/bin/env python3
"""GT finalization helper for readiness gating and repeat comparison.

This script is intentionally conservative:
- it treats the current nolsp repeat artifacts as contaminated reference data
- it can gate a readiness probe on live telemetry
- it can summarize repeated runs once both arms are ready

The tool works with the summary/report files already emitted by the GT harness:
- gt_arm_summary.json
- gt_report.csv
- optional eval outputs such as evaluation.json / report.json / preds.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FROZEN_SUITE_DEFAULT = Path("scripts/swebench/frozen_gt_astropy10.txt")

EVAL_CANDIDATES = (
    "evaluation.json",
    "eval_report.json",
    "output.report.json",
    "report.json",
    "preds.json",
    "predictions.json",
)


@dataclass
class RunMetrics:
    run_dir: Path
    summary: dict[str, Any]
    task_rows: list[dict[str, str]]
    resolution_map: dict[str, bool]
    resolution_source: str | None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _numeric(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _first_numeric(data: dict[str, Any], keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        if key in data:
            return _numeric(data[key], default)
    return default


def _read_suite(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def _coerce_resolution_map(blob: Any) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if isinstance(blob, list):
        for item in blob:
            if not isinstance(item, dict):
                continue
            iid = item.get("instance_id")
            if not iid:
                continue
            if "resolved" in item:
                out[iid] = bool(item["resolved"])
            elif "is_resolved" in item:
                out[iid] = bool(item["is_resolved"])
            else:
                patch = item.get("model_patch") or item.get("patch") or ""
                out[iid] = bool(str(patch).strip())
        return out

    if isinstance(blob, dict):
        if "resolved_ids" in blob or "unresolved_ids" in blob:
            for iid in blob.get("resolved_ids", []):
                out[str(iid)] = True
            for iid in blob.get("unresolved_ids", []):
                out.setdefault(str(iid), False)
            if out:
                return out

        for key, value in blob.items():
            if not isinstance(value, dict):
                continue
            if "resolved" in value:
                out[str(key)] = bool(value["resolved"])
            elif "is_resolved" in value:
                out[str(key)] = bool(value["is_resolved"])
            else:
                patch = value.get("model_patch") or value.get("patch") or ""
                if patch is not None:
                    out[str(key)] = bool(str(patch).strip())
    return out


def _find_resolution_map(run_dir: Path) -> tuple[dict[str, bool], str | None]:
    best: tuple[dict[str, bool], str | None] = ({}, None)
    for candidate_name in EVAL_CANDIDATES:
        for path in sorted(run_dir.rglob(candidate_name)):
            data = _load_json(path)
            res = _coerce_resolution_map(data)
            if res:
                return res, str(path)
            if not best[0] and data:
                best = (res, str(path))

    # Fallback: any preds.json-like artifact can still signal patch presence.
    for candidate_name in ("preds.json", "predictions.json"):
        for path in sorted(run_dir.rglob(candidate_name)):
            data = _load_json(path)
            res = _coerce_resolution_map(data)
            if res:
                return res, str(path)

    return best


def load_run_metrics(run_dir: Path) -> RunMetrics:
    summary = _load_json(run_dir / "gt_arm_summary.json")
    task_rows = _load_rows(run_dir / "gt_report.csv")
    resolution_map, resolution_source = _find_resolution_map(run_dir)
    return RunMetrics(
        run_dir=run_dir,
        summary=summary,
        task_rows=task_rows,
        resolution_map=resolution_map,
        resolution_source=resolution_source,
    )


def readiness_status(run: RunMetrics) -> dict[str, Any]:
    s = run.summary
    rows = run.task_rows

    task_count = int(_first_numeric(s, ("task_count",), len(rows)))
    material = _first_numeric(s, ("avg_material_edit", "material_edit_total", "material_edit_count"), 0.0)
    ack_armed = _first_numeric(s, ("ack_armed_total", "ack_armed_count", "ack_armed"), 0.0)
    steer = _first_numeric(s, ("steer_delivered_total", "steer_delivered_count", "steer_delivered"), 0.0)
    targeted = _first_numeric(s, ("targeted_edit_after_steer_total", "targeted_edit_after_steer_count"), 0.0)
    verification = _first_numeric(s, ("verification_after_steer_total", "verification_after_steer_count"), 0.0)
    engagement = _first_numeric(
        s,
        (
            "verification_after_steer_total",
            "verification_after_steer_count",
            "targeted_edit_after_steer_total",
            "targeted_edit_after_steer_count",
            "ack_engagement_total",
            "ack_engagement_count",
            "ack_engagement",
        ),
        0.0,
    )
    identity_missing = _first_numeric(s, ("identity_missing_total", "identity_missing", "identity_missing_count"), 0.0)
    budget_denied = _first_numeric(s, ("budget_denied_total", "budget_denied_count", "budget_denied"), 0.0)
    run_invalid = _first_numeric(s, ("run_invalid_count",), 0.0)
    infra = _first_numeric(s, ("infra_contaminated_total", "infra_contaminated_count", "infra_contaminated"), 0.0)

    # Readiness is strict: no contamination and a real edit/arm/delivery/engagement chain.
    reasons: list[str] = []
    if task_count == 0:
        reasons.append("empty_run")
    if material <= 0:
        reasons.append("no_material_edit")
    if ack_armed <= 0:
        reasons.append("no_ack_armed")
    if steer <= 0:
        reasons.append("no_steer_delivered")
    if engagement <= 0:
        reasons.append("no_followthrough")
    if identity_missing > 0:
        reasons.append("identity_missing")
    if budget_denied > 0:
        reasons.append("budget_denied")
    if run_invalid > 0:
        reasons.append("run_invalid")
    if infra > 0:
        reasons.append("infra_contaminated")

    ready = not reasons
    return {
        "ready": ready,
        "task_count": task_count,
        "material_edit": material,
        "ack_armed": ack_armed,
        "steer_delivered": steer,
        "targeted_edit_after_steer": targeted,
        "verification_after_steer": verification,
        "ack_engagement": engagement,
        "identity_missing": identity_missing,
        "budget_denied": budget_denied,
        "run_invalid_count": run_invalid,
        "infra_contaminated": infra,
        "fail_reasons": reasons,
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def freeze_state(suite_file: Path | None = None, model: str | None = None) -> dict[str, Any]:
    root = _repo_root()

    def _git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    state = {
        "repo_root": str(root),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git("rev-parse", "HEAD"),
        "status_short": _git("status", "--short"),
        "suite_file": str(suite_file) if suite_file else None,
        "suite_ids": _read_suite(suite_file) if suite_file else [],
        "model": model,
    }
    return state


def _count_rows(rows: list[dict[str, str]], key: str) -> float:
    total = 0.0
    for row in rows:
        total += _numeric(row.get(key, 0))
    return total


def _summary_metric(run: RunMetrics, *keys: str, default: float = 0.0) -> float:
    return _first_numeric(run.summary, tuple(keys), default)


def _material_total(run: RunMetrics) -> float:
    total = _summary_metric(run, "material_edit_total", "material_edit_count", default=0.0)
    if total:
        return total
    avg = _summary_metric(run, "avg_material_edit", default=0.0)
    task_count = int(_first_numeric(run.summary, ("task_count",), len(run.task_rows)))
    return avg * task_count


def _count_total(
    run: RunMetrics,
    total_keys: tuple[str, ...],
    rate_key: str | None = None,
    denominator_keys: tuple[str, ...] = (),
) -> float:
    total = _summary_metric(run, *total_keys, default=0.0)
    if total:
        return total
    if rate_key:
        rate = _summary_metric(run, rate_key, default=0.0)
        if rate:
            denominator = _summary_metric(run, *denominator_keys, default=0.0) if denominator_keys else 0.0
            if denominator:
                return rate * denominator
    return 0.0


def _resolve_count(run: RunMetrics) -> int | None:
    if run.resolution_map:
        return sum(1 for v in run.resolution_map.values() if v)

    # Fallback: patch presence from the GT report if evaluation artifacts are unavailable.
    if not run.task_rows:
        return None
    return sum(1 for row in run.task_rows if _numeric(row.get("has_patch", 0)) > 0)


def _load_baseline_map(path: Path | None) -> dict[str, bool]:
    if not path:
        return {}
    return load_run_metrics(path).resolution_map


def _safe_mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return float(statistics.mean(values))


def _safe_variance(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return 0.0 if values else None
    return float(statistics.variance(values))


def _safe_range(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return max(values) - min(values)


def _format_metric(value: float | None, precision: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{precision}f}"


def _per_task_task_ids(suite_file: Path | None, arm_runs: list[RunMetrics]) -> list[str]:
    if suite_file and suite_file.exists():
        return _read_suite(suite_file)
    task_ids: set[str] = set()
    for run in arm_runs:
        task_ids.update(row.get("instance_id", "") for row in run.task_rows if row.get("instance_id"))
        task_ids.update(run.resolution_map.keys())
    return sorted(task_ids)


def _parse_group(spec: str) -> tuple[str, list[Path]]:
    if "=" not in spec:
        raise ValueError("group spec must look like arm=dir1,dir2,dir3")
    arm, dirs = spec.split("=", 1)
    run_dirs = [Path(p) for p in dirs.split(",") if p]
    return arm.strip(), run_dirs


def _load_groups(specs: list[str]) -> dict[str, list[RunMetrics]]:
    groups: dict[str, list[RunMetrics]] = {}
    for spec in specs:
        arm, run_dirs = _parse_group(spec)
        groups[arm] = [load_run_metrics(p) for p in run_dirs]
    return groups


def compare_report(
    groups: dict[str, list[RunMetrics]],
    baseline_dir: Path | None = None,
    suite_file: Path | None = None,
) -> dict[str, Any]:
    baseline_map = _load_baseline_map(baseline_dir)
    baseline_name = baseline_dir.name if baseline_dir else None

    outcome_rows: list[dict[str, Any]] = []
    mechanism_rows: list[dict[str, Any]] = []
    per_task_freq: dict[str, dict[str, int]] = {}

    for arm, runs in groups.items():
        run_readiness = [readiness_status(run) for run in runs]
        ready_runs = sum(1 for status in run_readiness if status["ready"])
        resolved_counts = [_resolve_count(run) for run in runs]

        gains: list[int] = []
        regressions: list[int] = []
        win_loss: list[int] = []
        if baseline_map:
            for run in runs:
                arm_map = run.resolution_map
                if not arm_map:
                    continue
                common = set(arm_map) & set(baseline_map)
                g = sum(1 for iid in common if arm_map[iid] and not baseline_map[iid])
                r = sum(1 for iid in common if baseline_map[iid] and not arm_map[iid])
                gains.append(g)
                regressions.append(r)
                win_loss.append(g - r)

        outcome_rows.append({
            "arm": arm,
            "repeat_1_resolved": resolved_counts[0] if len(resolved_counts) > 0 else None,
            "repeat_2_resolved": resolved_counts[1] if len(resolved_counts) > 1 else None,
            "repeat_3_resolved": resolved_counts[2] if len(resolved_counts) > 2 else None,
            "mean_resolved": _safe_mean([float(v) for v in resolved_counts if v is not None]),
            "variance": _safe_variance([float(v) for v in resolved_counts if v is not None]),
            "range": _safe_range([float(v) for v in resolved_counts if v is not None]),
            "gains_vs_baseline": sum(gains) if gains else None,
            "regressions_vs_baseline": sum(regressions) if regressions else None,
            "win_loss": sum(win_loss) if win_loss else None,
            "ready_runs": ready_runs,
            "ready": ready_runs == len(runs) and len(runs) > 0,
        })

        total_material = sum(_material_total(run) for run in runs)
        total_ack = sum(_count_total(run, ("ack_armed_total", "ack_armed_count"), "ack_armed_rate", ("ack_denominator",)) for run in runs)
        total_steer = sum(_count_total(run, ("steer_delivered_total", "steer_delivered_count"), "delivery_rate", ("ack_armed_total", "ack_armed_count", "ack_armed")) for run in runs)
        total_engagement = sum(_count_total(run, ("ack_engagement_total", "ack_engagement_count"), "engagement_rate", ("steer_delivered_total", "steer_delivered_count", "steer_delivered")) for run in runs)
        total_follow = sum(_count_total(run, ("ack_followed_total", "ack_followed_count"), "ack_followed_rate", ("ack_denominator",)) for run in runs)
        total_not_observed = sum(_count_total(run, ("ack_not_observed_total", "ack_not_observed_count"), "ack_not_observed_rate", ("ack_denominator",)) for run in runs)
        total_budget_denied = sum(_summary_metric(run, "budget_denied_total", "budget_denied_count", "budget_denied") for run in runs)
        budget_state_present = _safe_mean([_summary_metric(run, "budget_state_present_rate", default=_summary_metric(run, "budget_state_present_count")) for run in runs])
        infra = _safe_mean([_summary_metric(run, "infra_contaminated_rate", default=_summary_metric(run, "infra_contaminated_total")) for run in runs])
        orient_calls = _safe_mean([_summary_metric(run, "avg_gt_orient_calls_per_task", "avg_gt_orient") for run in runs])

        mechanism_rows.append({
            "arm": arm,
            "arm_coverage": (total_ack / total_material) if total_material else None,
            "delivery_rate": (total_steer / total_ack) if total_ack else None,
            "engagement_rate": (total_engagement / total_steer) if total_steer else None,
            "behavior_shift_rate": (total_follow / total_steer) if total_steer else None,
            "ack_follow_rate": (total_follow / total_ack) if total_ack else None,
            "not_observed_rate": (total_not_observed / total_ack) if total_ack else None,
            "gt_orient_calls_per_task": orient_calls,
            "budget_state_present": budget_state_present,
            "infra_contaminated": infra,
            "run_invalid_count": _safe_mean([_summary_metric(run, "run_invalid_count") for run in runs]),
            "ready": ready_runs == len(runs) and len(runs) > 0,
            "ready_runs": ready_runs,
        })

        # Task determinism counts across repeats.
        task_ids = _per_task_task_ids(suite_file, runs)
        for task_id in task_ids:
            seen = per_task_freq.setdefault(task_id, {})
            resolved_repeats = 0
            for run in runs:
                if run.resolution_map.get(task_id):
                    resolved_repeats += 1
            seen[arm] = resolved_repeats

    return {
        "baseline": str(baseline_dir) if baseline_dir else None,
        "baseline_name": baseline_name,
        "outcome_rows": outcome_rows,
        "mechanism_rows": mechanism_rows,
        "per_task_repeats": per_task_freq,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# GT Finalization Report")
    lines.append("")

    lines.append("## Outcome Table")
    lines.append("")
    lines.append("| arm | repeat 1 resolved | repeat 2 resolved | repeat 3 resolved | mean resolved | variance | range | gains vs baseline | regressions vs baseline | win/loss |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in report["outcome_rows"]:
        lines.append(
            f"| {row['arm']} | {_format_metric(row['repeat_1_resolved'], 0)} | {_format_metric(row['repeat_2_resolved'], 0)} | {_format_metric(row['repeat_3_resolved'], 0)} | "
            f"{_format_metric(row['mean_resolved'], 2)} | {_format_metric(row['variance'], 2)} | {_format_metric(row['range'], 2)} | "
            f"{_format_metric(row['gains_vs_baseline'], 0)} | {_format_metric(row['regressions_vs_baseline'], 0)} | {_format_metric(row['win_loss'], 0)} |"
        )

    lines.append("")
    lines.append("## Mechanism Table")
    lines.append("")
    lines.append("| arm | ready | arm coverage | delivery rate | engagement rate | behavior-shift rate | ack-follow rate | not-observed rate | gt_orient_calls_per_task | budget_state_present | infra_contaminated | run_invalid_count |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in report["mechanism_rows"]:
        lines.append(
            f"| {row['arm']} | {str(bool(row.get('ready'))).lower()} | {_format_metric(row['arm_coverage'])} | {_format_metric(row['delivery_rate'])} | {_format_metric(row['engagement_rate'])} | "
            f"{_format_metric(row['behavior_shift_rate'])} | {_format_metric(row['ack_follow_rate'])} | {_format_metric(row['not_observed_rate'])} | "
            f"{_format_metric(row['gt_orient_calls_per_task'])} | {_format_metric(row['budget_state_present'])} | {_format_metric(row['infra_contaminated'])} | "
            f"{_format_metric(row['run_invalid_count'], 0)} |"
        )

    lines.append("")
    lines.append("## Determinism Analysis")
    lines.append("")
    lines.append("| task | repeat hits by arm |")
    lines.append("|---|---|")
    for task_id in sorted(report["per_task_repeats"]):
        hits = report["per_task_repeats"][task_id]
        hit_str = ", ".join(f"{arm}:{count}/3" for arm, count in sorted(hits.items()))
        lines.append(f"| {task_id} | {hit_str} |")
    return "\n".join(lines)


def _handle_readiness(args: argparse.Namespace) -> int:
    run = load_run_metrics(Path(args.summary_dir))
    status = readiness_status(run)
    print(json.dumps(status, indent=2))
    return 0 if status["ready"] else 1


def _handle_freeze_state(args: argparse.Namespace) -> int:
    suite_file = Path(args.suite_file) if args.suite_file else None
    model = args.model if args.model else None
    state = freeze_state(suite_file=suite_file, model=model)
    print(json.dumps(state, indent=2))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(state, indent=2))
    return 0


def _handle_monitor(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir)
    telem = task_dir / "gt_hook_telemetry.jsonl"
    if not telem.exists():
        print(json.dumps({"ready": False, "reason": "missing_gt_hook_telemetry"}, indent=2))
        return 1

    counts: dict[str, int] = {}
    max_cycle = 0
    try:
        for line in telem.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            event = entry.get("event")
            if event:
                counts[event] = counts.get(event, 0) + 1
            cycle = entry.get("cycle")
            if isinstance(cycle, int):
                max_cycle = max(max_cycle, cycle)
    except Exception as exc:
        print(json.dumps({"ready": False, "reason": f"telemetry_read_error:{exc}"}, indent=2))
        return 1

    material = counts.get("material_edit", 0)
    identity_missing = counts.get("identity_missing", 0)
    budget_denied = counts.get("budget_denied", 0)
    fail_fast = (
        max_cycle >= args.max_cycle
        and material == 0
        and (identity_missing > 0 or budget_denied > 0)
    )
    payload = {
        "max_cycle": max_cycle,
        "material_edit": material,
        "identity_missing": identity_missing,
        "budget_denied": budget_denied,
        "fail_fast": fail_fast,
        "reason": "upstream_bootstrap_or_guidance_failure" if fail_fast else "continue",
    }
    print(json.dumps(payload, indent=2))
    return 2 if fail_fast else 0


def _handle_compare(args: argparse.Namespace) -> int:
    groups = _load_groups(args.group)
    baseline_dir = Path(args.baseline_dir) if args.baseline_dir else None
    suite_file = Path(args.suite_file) if args.suite_file else None
    report = compare_report(groups, baseline_dir=baseline_dir, suite_file=suite_file)
    md = render_markdown(report)
    payload = dict(report)
    payload["markdown"] = md
    print(md)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
    if args.md_out:
        Path(args.md_out).write_text(md)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GT finalization helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_readiness = sub.add_parser("readiness", help="Check whether a run is comparison-ready")
    p_readiness.add_argument("--summary-dir", required=True, help="Run directory containing gt_arm_summary.json")
    p_readiness.set_defaults(func=_handle_readiness)

    p_freeze = sub.add_parser("freeze-state", help="Snapshot the frozen repository state")
    p_freeze.add_argument("--suite-file", help="Frozen suite file to record")
    p_freeze.add_argument("--model", help="Locked model name to record")
    p_freeze.add_argument("--out", help="Optional JSON file to write the snapshot to")
    p_freeze.set_defaults(func=_handle_freeze_state)

    p_monitor = sub.add_parser("monitor", help="Fail-fast gate for live readiness probes")
    p_monitor.add_argument("--task-dir", required=True, help="Live task output directory")
    p_monitor.add_argument("--max-cycle", type=int, default=8)
    p_monitor.set_defaults(func=_handle_monitor)

    p_compare = sub.add_parser("compare", help="Compare repeated run groups")
    p_compare.add_argument("--group", action="append", required=True,
                           help="Repeat group in the form arm=dir1,dir2,dir3")
    p_compare.add_argument("--baseline-dir", help="Optional baseline directory for gains/regressions")
    p_compare.add_argument("--suite-file", help="Optional frozen suite file for per-task determinism")
    p_compare.add_argument("--json-out", help="Write JSON report to this path")
    p_compare.add_argument("--md-out", help="Write markdown report to this path")
    p_compare.set_defaults(func=_handle_compare)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
