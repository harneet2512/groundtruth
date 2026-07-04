"""Benchmark/report aggregation for full-form GT telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from groundtruth.runtime.hook_truth import normalize_hook_truth
from groundtruth.runtime.telemetry import read_blocks


def build_benchmark_report(run_dir: str) -> dict[str, Any]:
    root = Path(run_dir)
    task_dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    rows = [_task_row(task) for task in sorted(task_dirs)]
    if not rows and root.exists():
        rows = [_task_row(root)]

    total = len(rows)
    resolved = sum(1 for row in rows if row["resolved"])
    source_touch = sum(1 for row in rows if row["source_files_touched"])
    root_scaffold = sum(1 for row in rows if row["root_scaffold_files_added"])
    empty_patch = sum(1 for row in rows if row["empty_patch"])
    contract = sum(1 for row in rows if row["contract_extracted"])
    cochange = sum(1 for row in rows if row["cochange_cluster"])
    participated = sum(1 for row in rows if row["gt_participated"])
    warning_count = sum(row["warning_count"] for row in rows)
    runtime_warning_count = sum(row["runtime_warning_count"] for row in rows)
    replan_count = sum(row["replan_count"] for row in rows)
    cluster_rates = [row["cluster_touch_rate"] for row in rows if row["cluster_touch_rate"] is not None]
    focus_overlap_rates = [row["brief_edit_overlap"] for row in rows if row["brief_edit_overlap"] is not None]
    focus_precision_rates = [row["focus_edit_precision"] for row in rows if row["focus_edit_precision"] is not None]
    focus_hit_at_1 = sum(1 for row in rows if row["focus_hit_at_1"])
    focus_hit_at_3 = sum(1 for row in rows if row["focus_hit_at_3"])
    usable_delivery = sum(1 for row in rows if row["usable_delivery_ok"])
    transport_delivery = sum(1 for row in rows if row["transport_delivered"])
    hook_logged = sum(1 for row in rows if row["hook_logged"])
    hook_visible = sum(1 for row in rows if row["hook_visible_to_agent"])
    hook_blocked = sum(1 for row in rows if row["hook_blocked"])
    final_audit = sum(1 for row in rows if row["final_audit_only"])

    return {
        "task_count": total,
        "resolved_count": resolved,
        "transport_delivery_rate": _rate(transport_delivery, total),
        "usable_delivery_rate": _rate(usable_delivery, total),
        "adherence_rate": _rate(focus_hit_at_3, total),
        "outcome_rate": _rate(resolved, total),
        "hook_logged_rate": _rate(hook_logged, total),
        "hook_visible_to_agent_rate": _rate(hook_visible, total),
        "hook_blocked_rate": _rate(hook_blocked, total),
        "final_audit_only_rate": _rate(final_audit, total),
        "empty_patch_rate": _rate(empty_patch, total),
        "root_scaffold_rate": _rate(root_scaffold, total),
        "source_touch_rate": _rate(source_touch, total),
        "brief_edit_overlap": round(sum(focus_overlap_rates) / len(focus_overlap_rates), 4) if focus_overlap_rates else 0.0,
        "focus_hit_at_1_rate": _rate(focus_hit_at_1, total),
        "focus_hit_at_3_rate": _rate(focus_hit_at_3, total),
        "focus_edit_precision": round(sum(focus_precision_rates) / len(focus_precision_rates), 4) if focus_precision_rates else 0.0,
        "cluster_touch_rate": round(sum(cluster_rates) / len(cluster_rates), 4) if cluster_rates else 0.0,
        "contract_extraction_rate": _rate(contract, total),
        "cochange_cluster_rate": _rate(cochange, total),
        "warning_count": warning_count,
        "runtime_warning_count": runtime_warning_count,
        "replan_count": replan_count,
        "gt_participated_rate": _rate(participated, total),
        "tasks": rows,
    }


def write_benchmark_report(run_dir: str, *, output_json: str | None = None, output_md: str | None = None) -> dict[str, Any]:
    report = build_benchmark_report(run_dir)
    root = Path(run_dir)
    json_path = Path(output_json) if output_json else root / "gt_full_form_report.json"
    md_path = Path(output_md) if output_md else root / "gt_full_form_report.md"
    try:
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(format_benchmark_report(report), encoding="utf-8")
    except OSError:
        pass
    return report


def format_benchmark_report(report: dict[str, Any]) -> str:
    lines = [
        "# GT Full-Form Report",
        "",
        f"- tasks: {report['task_count']}",
        f"- resolved: {report['resolved_count']}",
        f"- transport delivery rate: {report['transport_delivery_rate']:.2%}",
        f"- usable delivery rate: {report['usable_delivery_rate']:.2%}",
        f"- adherence rate: {report['adherence_rate']:.2%}",
        f"- outcome rate: {report['outcome_rate']:.2%}",
        f"- hook logged rate: {report['hook_logged_rate']:.2%}",
        f"- hook visible-to-agent rate: {report['hook_visible_to_agent_rate']:.2%}",
        f"- hook blocked rate: {report['hook_blocked_rate']:.2%}",
        f"- final audit-only rate: {report['final_audit_only_rate']:.2%}",
        f"- brief edit overlap: {report['brief_edit_overlap']:.2%}",
        f"- focus hit@1: {report['focus_hit_at_1_rate']:.2%}",
        f"- focus hit@3: {report['focus_hit_at_3_rate']:.2%}",
        f"- focus edit precision: {report['focus_edit_precision']:.2%}",
        f"- empty patch rate: {report['empty_patch_rate']:.2%}",
        f"- root scaffold rate: {report['root_scaffold_rate']:.2%}",
        f"- source-touch rate: {report['source_touch_rate']:.2%}",
        f"- cluster-touch rate (diagnostic): {report['cluster_touch_rate']:.2%}",
        f"- contract extraction rate: {report['contract_extraction_rate']:.2%}",
        f"- co-change cluster rate: {report['cochange_cluster_rate']:.2%}",
        f"- warnings: {report['warning_count']}",
        f"- runtime warnings: {report['runtime_warning_count']}",
        f"- replans: {report['replan_count']}",
        f"- GT participated rate: {report['gt_participated_rate']:.2%}",
        "",
    ]
    return "\n".join(lines)


def _task_row(task_dir: Path) -> dict[str, Any]:
    task_id = task_dir.name
    telemetry = _read_all_runtime(task_dir)
    plan = _latest_json(task_dir, "*_v7_plan.json")
    brief = _latest_jsonl(task_dir, "*_v7_brief.jsonl")
    patch = _latest_block(telemetry, "gt_patch_shape")
    runtime = _latest_block(telemetry, "gt_runtime")
    delivery = _latest_block(telemetry, "gt_usable_delivery")
    replan = [rec for rec in telemetry if rec.get("block") == "gt_replan"]

    hook_records = _read_hook_records(task_dir)
    hook_patch = _latest_hook_patch_shape(hook_records)
    if not patch and hook_patch:
        patch = hook_patch
    hook_runtime = _latest_hook_runtime(hook_records)
    if not runtime and hook_runtime:
        runtime = hook_runtime
    hook_truth = _latest_hook_truth(hook_records)

    resolved = _resolved(task_dir)
    return {
        "task_id": task_id,
        "resolved": resolved,
        "gt_participated": bool(telemetry or plan or brief or hook_records),
        "source_files_touched": patch.get("source_files_touched", []),
        "test_files_touched": patch.get("test_files_touched", []),
        "root_scaffold_files_added": patch.get("root_scaffold_files_added", []),
        "empty_patch": bool(patch.get("empty_patch", False)),
        "agent_focus_files_touched": patch.get("agent_focus_files_touched", []),
        "edited_ranked_focus_files": patch.get("edited_ranked_focus_files", []),
        "brief_edit_overlap": patch.get("brief_edit_overlap"),
        "focus_hit_at_1": bool(patch.get("focus_hit_at_1", False)),
        "focus_hit_at_3": bool(patch.get("focus_hit_at_3", False)),
        "focus_edit_precision": patch.get("focus_edit_precision"),
        "cluster_touch_rate": patch.get("cluster_touch_rate"),
        "transport_delivered": bool(delivery.get("transport_delivered", False)),
        "usable_delivery_ok": bool(delivery.get("usable_delivery_ok", False)),
        "usable_delivery_failure_reasons": delivery.get("failure_reasons", []),
        "warning_count": len(patch.get("warnings", [])),
        "runtime_warning_count": len(runtime.get("runtime_warnings", [])),
        "hook_logged": hook_truth["hook_logged"],
        "hook_visible_to_agent": hook_truth["hook_visible_to_agent"],
        "hook_blocked": hook_truth["hook_blocked"],
        "final_audit_only": hook_truth["final_audit_only"],
        "replan_count": sum(1 for record in replan if _replan_triggered(record)),
        "contract_extracted": bool(_dig(brief, ["module_7_contract", "contract_lines"])),
        "cochange_cluster": bool(_dig(brief, ["module_7_cochange", "cluster_files"])),
    }


def _read_all_runtime(task_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in task_dir.rglob("gt_runtime_telemetry.jsonl"):
        records.extend(read_blocks(path))
    return records


def _read_hook_records(task_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in task_dir.rglob("gt_hook_telemetry.jsonl"):
        records.extend(read_blocks(path))
    for path in task_dir.rglob("gt_hook_log.jsonl"):
        records.extend(read_blocks(path))
    return records


def _latest_block(records: list[dict[str, Any]], block: str) -> dict[str, Any]:
    for record in reversed(records):
        if record.get("block") == block and isinstance(record.get(block), dict):
            return record[block]
    return {}


def _latest_hook_patch_shape(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in reversed(records):
        patch = record.get("gt_patch_shape")
        if isinstance(patch, dict):
            return patch
    return {}


def _latest_hook_runtime(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in reversed(records):
        runtime = record.get("gt_runtime")
        if isinstance(runtime, dict):
            return runtime
    return {}


def _latest_hook_truth(records: list[dict[str, Any]]) -> dict[str, bool]:
    for record in reversed(records):
        truth = normalize_hook_truth(record)
        if truth["hook_logged"]:
            return truth
    return {
        "hook_logged": False,
        "hook_visible_to_agent": False,
        "hook_blocked": False,
        "final_audit_only": False,
    }


def _replan_triggered(record: dict[str, Any]) -> bool:
    payload = record.get("gt_replan")
    return isinstance(payload, dict) and bool(payload.get("should_replan"))


def _latest_json(task_dir: Path, pattern: str) -> dict[str, Any]:
    matches = sorted(task_dir.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in matches:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _latest_jsonl(task_dir: Path, pattern: str) -> dict[str, Any]:
    matches = sorted(task_dir.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in matches:
        records = read_blocks(path)
        if records:
            return records[-1]
    return {}


def _resolved(task_dir: Path) -> bool:
    for pattern in ("eval_report.json", "eval_reports/*.json", "preds.json", "predictions.jsonl"):
        for path in task_dir.rglob(pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if '"resolved": true' in text or '"is_resolved": true' in text:
                return True
    return False


def _dig(data: dict[str, Any], path: list[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0
