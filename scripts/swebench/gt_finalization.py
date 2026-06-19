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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _first_truthy(data: dict[str, Any], keys: tuple[str, ...], default: bool = False) -> bool:
    for key in keys:
        if key in data:
            return _truthy(data[key])
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
    engagement = _first_numeric(s, ("ack_engagement_total", "ack_engagement_count", "ack_engagement"), 0.0)
    identity_missing = _first_numeric(s, ("identity_missing_total", "identity_missing", "identity_missing_count"), 0.0)
    budget_denied = _first_numeric(s, ("budget_denied_total", "budget_denied_count", "budget_denied"), 0.0)
    run_invalid = _first_numeric(s, ("run_invalid_count",), 0.0)
    infra = _first_numeric(s, ("infra_contaminated_total", "infra_contaminated_count", "infra_contaminated"), 0.0)
    lsp_signals_present = any(
        key in s
        for key in (
            "lsp_enabled",
            "lsp_ready",
            "lsp_fallback_count",
            "lsp_promotion_count",
            "hybrid_active_before_first_edit",
            "lsp_ready_ts",
            "lsp_ready_source",
        )
    )
    lsp_enabled = _first_truthy(s, ("lsp_enabled", "GT_LSP_ENABLED", "hybrid_enabled"), False)
    lsp_ready = _first_truthy(s, ("lsp_ready", "GT_LSP_READY", "lsp_ready_present"), False)
    lsp_fallback_count = _first_numeric(s, ("lsp_fallback_count", "lsp_fallbacks", "fallback_count"), 0.0)
    lsp_promotion_count = _first_numeric(s, ("lsp_promotion_count", "lsp_promotions", "promotions"), 0.0)
    hybrid_active_before_first_edit = _first_truthy(
        s,
        ("hybrid_active_before_first_edit", "lsp_ready_before_first_edit"),
        False,
    )

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
        reasons.append("no_ack_engagement")
    if identity_missing > 0:
        reasons.append("identity_missing")
    if budget_denied > 0:
        reasons.append("budget_denied")
    if run_invalid > 0:
        reasons.append("run_invalid")
    if infra > 0:
        reasons.append("infra_contaminated")
    if lsp_signals_present and lsp_enabled:
        if not lsp_ready:
            reasons.append("lsp_not_ready")
        if lsp_fallback_count > 0 and lsp_promotion_count <= 0:
            reasons.append("lsp_degraded")
        if not hybrid_active_before_first_edit and lsp_promotion_count > 0:
            reasons.append("hybrid_started_late")

    ready = not reasons
    return {
        "ready": ready,
        "task_count": task_count,
        "material_edit": material,
        "ack_armed": ack_armed,
        "steer_delivered": steer,
        "ack_engagement": engagement,
        "identity_missing": identity_missing,
        "budget_denied": budget_denied,
        "run_invalid_count": run_invalid,
        "infra_contaminated": infra,
        "lsp_enabled": lsp_enabled,
        "lsp_ready": lsp_ready,
        "lsp_fallback_count": lsp_fallback_count,
        "lsp_promotion_count": lsp_promotion_count,
        "hybrid_active_before_first_edit": hybrid_active_before_first_edit,
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


_TELEMETRY_CANDIDATES = ("gt_hook_telemetry.jsonl", ".gt/gt_hook_telemetry.jsonl")
_TIMESTAMP_KEYS = ("ts", "timestamp", "time", "emitted_at", "event_time")


def _find_telemetry(task_dir: Path) -> Path | None:
    for name in _TELEMETRY_CANDIDATES:
        path = task_dir / name
        if path.exists():
            return path
    return None


def _parse_timestamp(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # numeric string
        try:
            return float(s)
        except ValueError:
            pass
        # ISO 8601 with trailing Z
        try:
            if s.endswith("Z"):
                dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _latest_telemetry_ts(path: Path) -> tuple[float | None, int, int]:
    """Return (latest_ts_epoch, line_count, max_cycle) from telemetry file."""
    latest: float | None = None
    lines = 0
    max_cycle = 0
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            lines += 1
            try:
                entry = json.loads(line)
            except Exception:
                continue
            cycle = entry.get("cycle")
            if isinstance(cycle, int):
                max_cycle = max(max_cycle, cycle)
            for key in _TIMESTAMP_KEYS:
                if key in entry:
                    ts = _parse_timestamp(entry[key])
                    if ts is not None:
                        if latest is None or ts > latest:
                            latest = ts
                        break
    except FileNotFoundError:
        return None, 0, 0
    # Fallback: file mtime as a last-resort freshness signal
    if latest is None:
        try:
            latest = path.stat().st_mtime
        except OSError:
            latest = None
    return latest, lines, max_cycle


def _runlog_mtime(task_dir: Path) -> float | None:
    """Return mtime of run.log — alternative liveness signal for long reasoning
    loops where the agent is making model calls but no GT-hook events fire."""
    rl = task_dir / "run.log"
    if rl.exists():
        try:
            return rl.stat().st_mtime
        except OSError:
            return None
    return None


def watchdog_status(task_dir: Path, stall_minutes: int) -> dict[str, Any]:
    telem = _find_telemetry(task_dir)
    now = time.time()
    if telem is None:
        return {
            "task_dir": str(task_dir),
            "triggered": False,
            "reason": "telemetry_missing",
            "now_epoch": now,
            "stall_minutes": stall_minutes,
        }
    latest, lines, max_cycle = _latest_telemetry_ts(telem)
    # Use the freshest of (telemetry last_ts, run.log mtime). run.log updates
    # on every model response even when GT-hook cycle events don't fire, so it
    # catches long-reasoning tasks that would otherwise be false-killed.
    runlog_ts = _runlog_mtime(task_dir)
    if runlog_ts is not None and (latest is None or runlog_ts > latest):
        latest = runlog_ts
    if latest is None:
        return {
            "task_dir": str(task_dir),
            "telemetry": str(telem),
            "triggered": False,
            "reason": "no_timestamps_yet",
            "line_count": lines,
            "max_cycle": max_cycle,
            "now_epoch": now,
            "stall_minutes": stall_minutes,
        }
    gap_seconds = max(0.0, now - latest)
    gap_minutes = gap_seconds / 60.0
    triggered = gap_minutes >= stall_minutes
    return {
        "task_dir": str(task_dir),
        "telemetry": str(telem),
        "triggered": triggered,
        "reason": "stall_exceeded" if triggered else "progressing",
        "last_ts_epoch": latest,
        "gap_seconds": gap_seconds,
        "gap_minutes": gap_minutes,
        "line_count": lines,
        "max_cycle": max_cycle,
        "now_epoch": now,
        "stall_minutes": stall_minutes,
    }


def _handle_watchdog(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir)
    status = watchdog_status(task_dir, args.stall_minutes)
    print(json.dumps(status, indent=2))
    if status["triggered"]:
        trigger_path = task_dir / "watchdog_trigger.json"
        try:
            trigger_path.parent.mkdir(parents=True, exist_ok=True)
            trigger_path.write_text(json.dumps(status, indent=2))
        except OSError as exc:
            # surface the failure but still signal the trigger
            print(f"warning: could not write {trigger_path}: {exc}")
        return 2
    return 0


_CLASSIFICATIONS = ("OFFICIAL_REPEAT", "DIAGNOSTIC_ONLY", "READINESS_SMOKE", "FAST_DIAG", "PENDING")


def classify_run(run_dir: Path, declared: str | None = None) -> dict[str, Any]:
    summary_path = run_dir / "gt_arm_summary.json"
    watchdog_path = run_dir / "watchdog_trigger.json"
    killed_path = run_dir / "killed_tasks.jsonl"
    run_metrics = load_run_metrics(run_dir)
    readiness = readiness_status(run_metrics)

    watchdog_triggered = watchdog_path.exists()
    killed_count = 0
    if killed_path.exists():
        for line in killed_path.read_text().splitlines():
            if line.strip():
                killed_count += 1

    summary_present = summary_path.exists()
    run_invalid = readiness.get("run_invalid_count", 0) or 0
    infra = readiness.get("infra_contaminated", 0) or 0
    identity_missing = readiness.get("identity_missing", 0) or 0

    blocking_reasons: list[str] = []
    if not summary_present:
        blocking_reasons.append("missing_gt_arm_summary")
    if watchdog_triggered:
        blocking_reasons.append("watchdog_triggered")
    if killed_count > 0:
        blocking_reasons.append(f"killed_tasks={killed_count}")
    if run_invalid > 0:
        blocking_reasons.append("run_invalid")
    if infra > 0:
        blocking_reasons.append("infra_contaminated")
    if identity_missing > 0:
        blocking_reasons.append("identity_missing")
    if not readiness.get("ready"):
        blocking_reasons.extend(f"readiness:{r}" for r in readiness.get("fail_reasons", []))

    if declared:
        declared_upper = declared.strip().upper()
        if declared_upper not in _CLASSIFICATIONS:
            raise ValueError(f"declared classification must be one of {_CLASSIFICATIONS}")
    else:
        declared_upper = None

    if declared_upper == "PENDING":
        classification = "PENDING"
    elif declared_upper in ("READINESS_SMOKE", "FAST_DIAG"):
        # Caller is labeling this run non-official by intent; we honor that regardless of blockers.
        classification = declared_upper
    elif blocking_reasons:
        classification = "DIAGNOSTIC_ONLY"
    else:
        classification = declared_upper or "OFFICIAL_REPEAT"

    return {
        "classification": classification,
        "declared": declared_upper,
        "run_dir": str(run_dir),
        "summary_present": summary_present,
        "readiness": readiness,
        "watchdog_triggered": watchdog_triggered,
        "killed_task_count": killed_count,
        "blocking_reasons": blocking_reasons,
        "classified_at_epoch": time.time(),
    }


def _handle_classify(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    result = classify_run(run_dir, declared=args.declared)
    print(json.dumps(result, indent=2))
    out_path = run_dir / "run_classification.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))
    except OSError as exc:
        print(f"warning: could not write {out_path}: {exc}")
        return 1
    return 0 if result["classification"] in ("OFFICIAL_REPEAT", "READINESS_SMOKE", "FAST_DIAG", "PENDING") else 2


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

    p_watchdog = sub.add_parser(
        "watchdog",
        help="Time-based watchdog: exits 2 and writes watchdog_trigger.json if no step progress within --stall-minutes.",
    )
    p_watchdog.add_argument("--task-dir", required=True,
                            help="Live task output directory containing gt_hook_telemetry.jsonl")
    p_watchdog.add_argument("--stall-minutes", type=int, default=15,
                            help="Minutes of telemetry silence that count as a stall (default: 15)")
    p_watchdog.set_defaults(func=_handle_watchdog)

    p_classify = sub.add_parser(
        "classify",
        help="Stamp a run with OFFICIAL_REPEAT | DIAGNOSTIC_ONLY | READINESS_SMOKE | FAST_DIAG | PENDING.",
    )
    p_classify.add_argument("--run-dir", required=True,
                            help="Run directory containing gt_arm_summary.json")
    p_classify.add_argument("--declared",
                            help="Optional declared classification (e.g. READINESS_SMOKE for Lane B probes).")
    p_classify.set_defaults(func=_handle_classify)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
