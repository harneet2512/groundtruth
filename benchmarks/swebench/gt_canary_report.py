#!/usr/bin/env python3
"""GT canary per-task reporter.

Walks a canary outdir and emits, per task:
  gt_report.csv         — PRIMARY, one row per (arm, instance_id)
  gt_arm_summary.json   — DERIVED aggregates over rows

Per-row gates (CANARY_VERIFY):
  MUST identity_ok                 (run_id, arm, instance_id all present)
  MUST within_call_budget          (cycle <= max_steps)
  SHOULD gt_orient_count >= 1      (briefing delivery)
  SHOULD micro_emit_count >= 1 or material_edit_count == 0
  SHOULD (hybrid only) lsp_promotion_count >= 1 on edited tasks

A row with any MUST failure is marked run_invalid=1.
The script exits non-zero if any row is run_invalid.

Usage:
  python3 gt_canary_report.py --outdir /tmp/smoke_A1 --arm A1 --run-id <id> \
                              [--hybrid] [--max-steps 100]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROW_FIELDS = [
    "run_id", "arm", "instance_id", "cycle",
    "gt_orient_count", "gt_lookup_count", "gt_impact_count", "gt_check_count",
    "material_edit_count", "micro_emit_count", "micro_suppress_count",
    "verify_emit_count", "verify_suppress_count",
    "ack_followed_count", "ack_ignored_count", "ack_not_observed_count",
    "budget_denied_count", "submit_observed_count", "pre_edit_briefing_count",
    "lsp_promotion_count",
    "patch_bytes", "has_patch",
    "gt_budget_ok", "gt_budget_fail_reasons",
    "within_call_budget", "identity_ok", "budget_state_present",
    "must_ok", "should_ok", "run_invalid",
    "fail_reasons",
]

GT_TOOL_LIMITS = {
    "gt_orient_count": 1,
    "gt_lookup_count": 2,
    "gt_impact_count": 2,
    "gt_check_count": 3,
}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _load_budget_state(task_dir: Path) -> dict:
    """Load harvested runtime budget state for the task if present."""
    for cand in (task_dir / "gt_budget.state.json", task_dir / "budget_state.json"):
        j = _load_json(cand)
        if isinstance(j, dict):
            return j
    return {}


def _count_events(jsonl: Path) -> dict:
    out: dict = {}
    try:
        for line in jsonl.read_text().splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            e = j.get("event", "")
            out[e] = out.get(e, 0) + 1
    except Exception:
        pass
    return out


def _find_task_dirs(outdir: Path) -> list[Path]:
    return sorted([p for p in outdir.iterdir() if p.is_dir() and p.name != "gt"])


def _find_patch_bytes(task_dir: Path) -> int:
    """Look at preds.json in the task dir or a sibling; return patch byte length."""
    preds = task_dir / "preds.json"
    if not preds.exists():
        # Try any preds.json in subdirs
        for p in task_dir.rglob("preds.json"):
            preds = p
            break
    if not preds.exists():
        return 0
    try:
        j = json.loads(preds.read_text())
        if isinstance(j, dict):
            # dict keyed by instance_id
            for v in j.values():
                if isinstance(v, dict):
                    patch = v.get("model_patch") or v.get("patch") or ""
                    if patch:
                        return len(patch)
        elif isinstance(j, list):
            for v in j:
                if isinstance(v, dict):
                    patch = v.get("model_patch") or v.get("patch") or ""
                    if patch:
                        return len(patch)
    except Exception:
        return 0
    return 0


def build_row(outdir: Path, task_dir: Path, arm: str, run_id: str,
              max_steps: int, hybrid: bool) -> dict:
    iid = task_dir.name
    summary_path = task_dir / "gt_per_task_summary.json"
    telem_path = task_dir / "gt_hook_telemetry.jsonl"

    summary = _load_json(summary_path) or {}
    events = _count_events(telem_path) if telem_path.exists() else {}
    budget_state = _load_budget_state(task_dir)
    budget_state_present = bool(budget_state)
    traj_counts = _count_tool_calls_from_trajectory(task_dir)

    # Map CSV column names to trajectory short-names for override.
    _TRAJ_KEY = {
        "gt_orient_count": "orient",
        "gt_lookup_count": "lookup",
        "gt_impact_count": "impact",
        "gt_check_count": "check",
    }
    _BUDGET_KEY = {
        "gt_orient_count": "orient",
        "gt_lookup_count": "lookup",
        "gt_impact_count": "impact",
        "gt_check_count": "check",
    }

    def g(key: str, default=0):
        # Runtime budget state is authoritative for allowed gt_* tool calls.
        # Trajectory-derived counts remain a fallback/attempt proxy only when
        # the harvested budget state is unavailable.
        if budget_state_present and key in _BUDGET_KEY:
            bucket = budget_state.get(_BUDGET_KEY[key], {})
            if isinstance(bucket, dict):
                try:
                    return int(bucket.get("count", 0))
                except Exception:
                    return 0
        if traj_counts is not None and key in _TRAJ_KEY:
            return traj_counts[_TRAJ_KEY[key]]
        if summary.get(key) is not None:
            return summary[key]
        # Derive from events if summary missing.
        if key == "gt_orient_count":
            return events.get("checkpoint_startup", 0)
        if key == "gt_check_count":
            return events.get("verify_emitted", 0)
        if key == "material_edit_count":
            return events.get("material_edit", 0)
        if key == "micro_emit_count":
            return events.get("micro_emitted", 0)
        if key == "micro_suppress_count":
            return events.get("micro_suppressed", 0)
        if key == "verify_emit_count":
            return events.get("verify_emitted", 0)
        if key == "verify_suppress_count":
            return events.get("verify_suppressed", 0)
        if key == "ack_followed_count":
            return events.get("ack_followed", 0)
        if key == "ack_ignored_count":
            return events.get("ack_ignored", 0)
        if key == "ack_not_observed_count":
            return events.get("ack_not_observed", 0)
        if key == "lsp_promotion_count":
            return events.get("lsp_promotion", 0)
        if key == "budget_denied_count":
            return events.get("budget_denied", 0)
        if key == "submit_observed_count":
            return events.get("submit_observed", 0)
        if key == "pre_edit_briefing_count":
            return events.get("pre_edit_briefing", 0)
        if key == "orient_redirected_count":
            return events.get("orient_redirected", 0)
        if key == "submit_blocked_count":
            return events.get("submit_gate_blocked", 0)
        if key == "submit_bypassed_count":
            return events.get("submit_gate_bypassed", 0)
        if key == "stuck_loop_fired_count":
            return events.get("stuck_loop", 0)
        return default

    patch_bytes = _find_patch_bytes(task_dir)
    cycle = summary.get("cycle", 0)
    identity_ok = bool(summary.get("identity_ok"))
    # If the hook never wrote a summary, identity cannot be confirmed.
    if not summary_path.exists():
        identity_ok = False
    within_budget = bool(summary.get("within_call_budget", cycle <= max_steps))

    row = {
        "run_id": summary.get("run_id") or run_id,
        "arm": summary.get("arm") or arm,
        "instance_id": iid,
        "cycle": cycle,
        "gt_orient_count": g("gt_orient_count"),
        "gt_lookup_count": g("gt_lookup_count"),
        "gt_impact_count": g("gt_impact_count"),
        "gt_check_count": g("gt_check_count"),
        "material_edit_count": g("material_edit_count"),
        "micro_emit_count": g("micro_emit_count"),
        "micro_suppress_count": g("micro_suppress_count"),
        "verify_emit_count": g("verify_emit_count"),
        "verify_suppress_count": g("verify_suppress_count"),
        "ack_followed_count": g("ack_followed_count"),
        "ack_ignored_count": g("ack_ignored_count"),
        "ack_not_observed_count": g("ack_not_observed_count"),
        "budget_denied_count": g("budget_denied_count"),
        "submit_observed_count": g("submit_observed_count"),
        "pre_edit_briefing_count": g("pre_edit_briefing_count"),
        "orient_redirected_count": g("orient_redirected_count"),
        "submit_blocked_count": g("submit_blocked_count"),
        "submit_bypassed_count": g("submit_bypassed_count"),
        "stuck_loop_fired_count": g("stuck_loop_fired_count"),
        "lsp_promotion_count": g("lsp_promotion_count"),
        "patch_bytes": patch_bytes,
        "has_patch": 1 if patch_bytes > 0 else 0,
        "gt_budget_ok": 1,
        "gt_budget_fail_reasons": "",
        "within_call_budget": 1 if within_budget else 0,
        "identity_ok": 1 if identity_ok else 0,
        "budget_state_present": 1 if budget_state_present else 0,
    }

    fails: list[str] = []
    if not identity_ok:
        fails.append("identity_missing")
    if not within_budget:
        fails.append("over_call_budget")
    if not budget_state_present:
        fails.append("budget_state_missing")

    tool_budget_fails: list[str] = []
    for key, limit in GT_TOOL_LIMITS.items():
        count = int(row.get(key, 0) or 0)
        if count > limit:
            tool_budget_fails.append(f"{key}:{count}>{limit}")
    if tool_budget_fails:
        row["gt_budget_ok"] = 0
        row["gt_budget_fail_reasons"] = ";".join(tool_budget_fails)
        fails.extend(f"gt_budget_{msg}" for msg in tool_budget_fails)

    must_ok = not fails

    should_fails: list[str] = []
    if row["gt_orient_count"] < 1:
        should_fails.append("no_orient")
    if row["material_edit_count"] > 0 and row["micro_emit_count"] < 1:
        should_fails.append("no_micro_on_edits")
    if hybrid and row["material_edit_count"] > 0 and row["lsp_promotion_count"] < 1:
        should_fails.append("no_lsp_promotion_hybrid")
    should_ok = not should_fails

    row["must_ok"] = 1 if must_ok else 0
    row["should_ok"] = 1 if should_ok else 0
    row["run_invalid"] = 0 if must_ok else 1
    row["fail_reasons"] = ";".join(fails + should_fails) or ""
    return row


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ROW_FIELDS})


def arm_summary(rows: list[dict]) -> dict:
    if not rows:
        return {"task_count": 0}
    n = len(rows)
    sum_i = lambda k: sum(int(r.get(k, 0) or 0) for r in rows)
    max_i = lambda k: max((int(r.get(k, 0) or 0) for r in rows), default=0)
    # v12 rollups — emphasizing per-task extremes (orient_max catches the 150x
    # thrash case that averages hide) and the new behavior-control event types.
    budget_denied_by_tool: dict = {}
    for r in rows:
        # Per-task denied_by_tool was not tracked pre-v12; count from summary
        # structure when possible.
        pass
    ack_denom = sum_i("ack_followed_count") + sum_i("ack_ignored_count") + sum_i("ack_not_observed_count")
    ack_followed_total = sum_i("ack_followed_count")
    return {
        "task_count": n,
        "run_invalid_count": sum_i("run_invalid"),
        "must_ok_rate": sum_i("must_ok") / n,
        "should_ok_rate": sum_i("should_ok") / n,
        "has_patch_rate": sum_i("has_patch") / n,
        "avg_gt_orient": sum_i("gt_orient_count") / n,
        "avg_gt_lookup": sum_i("gt_lookup_count") / n,
        "avg_gt_impact": sum_i("gt_impact_count") / n,
        "avg_gt_check": sum_i("gt_check_count") / n,
        "orient_max": max_i("gt_orient_count"),
        "lookup_max": max_i("gt_lookup_count"),
        "impact_max": max_i("gt_impact_count"),
        "check_max": max_i("gt_check_count"),
        "avg_material_edit": sum_i("material_edit_count") / n,
        "avg_micro_emit": sum_i("micro_emit_count") / n,
        "avg_verify_emit": sum_i("verify_emit_count") / n,
        "ack_followed_total": ack_followed_total,
        "ack_ignored_total": sum_i("ack_ignored_count"),
        "ack_not_observed_total": sum_i("ack_not_observed_count"),
        "ack_denominator": ack_denom,
        "ack_followed_rate": (ack_followed_total / ack_denom) if ack_denom else 0.0,
        "budget_denied_total": sum_i("budget_denied_count"),
        "submit_observed_total": sum_i("submit_observed_count"),
        "orient_redirected_total": sum_i("orient_redirected_count"),
        "submit_blocked_total": sum_i("submit_blocked_count"),
        "submit_bypassed_total": sum_i("submit_bypassed_count"),
        "stuck_loop_fired_total": sum_i("stuck_loop_fired_count"),
        "lsp_promotion_total": sum_i("lsp_promotion_count"),
        "gt_budget_violations": sum(1 for r in rows if int(r.get("gt_budget_ok", 1)) == 0),
    }


def _load_briefing_meta(task_dir: Path) -> dict:
    """Load briefing meta JSON sidecar if present."""
    for cand in (task_dir / "gt_briefing_meta.json", task_dir / "briefing_meta.json"):
        j = _load_json(cand)
        if j:
            return j
    return {}


def _load_index_sentinel(task_dir: Path) -> dict:
    """Load /tmp/gt_graph.db.ready sentinel harvested into the task dir."""
    for cand in (task_dir / "gt_graph.db.ready", task_dir / "gt_index_ready.json"):
        j = _load_json(cand)
        if j:
            return j
    return {}


def _outcome_from_dirs(task_dir: Path, baseline_outdir: Path | None) -> dict:
    """Best-effort outcome reconstruction from preds/resolve artifacts."""
    resolved = None
    for cand in (task_dir / "resolved.json", task_dir / "outcome.json"):
        j = _load_json(cand)
        if isinstance(j, dict) and "resolved" in j:
            resolved = bool(j["resolved"])
            break
    baseline_resolved = None
    if baseline_outdir is not None:
        bt = baseline_outdir / task_dir.name
        if bt.is_dir():
            for cand in (bt / "resolved.json", bt / "outcome.json"):
                j = _load_json(cand)
                if isinstance(j, dict) and "resolved" in j:
                    baseline_resolved = bool(j["resolved"])
                    break
    return {
        "resolved": resolved,
        "baseline_resolved": baseline_resolved,
        "is_gain": bool(resolved and baseline_resolved is False),
        "is_regression": bool(resolved is False and baseline_resolved),
        "is_neutral": (resolved == baseline_resolved) if baseline_resolved is not None else None,
    }


def _trajectory_has_gt_check(task_dir: Path) -> bool:
    """Scan the SWE-agent trajectory for the <gt-check> tag emitted by the
    PreSubmit hook. This is a fallback for when the hook fires but the
    wrapper counter isn't incremented (gt_check is invoked with no argument
    and the CLI early-exits on Usage).
    """
    for traj in task_dir.rglob("*.traj"):
        try:
            txt = traj.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<gt-check>" in txt or "GT_CHECK_PRESUBMIT" in txt:
            return True
    return False


def _extract_actions(obj) -> list[str]:
    """Recursively extract action/command strings from a trajectory record.

    Recurses only into dicts and lists (not strings) to avoid double-counting.
    """
    actions: list[str] = []
    if isinstance(obj, dict):
        for key in ("action", "command"):
            val = obj.get(key)
            if isinstance(val, str):
                actions.append(val.strip())
        for v in obj.values():
            if isinstance(v, (dict, list)):
                actions.extend(_extract_actions(v))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                actions.extend(_extract_actions(item))
    return actions


def _count_tool_calls_from_trajectory(task_dir: Path) -> dict[str, int] | None:
    """Count gt_* tool invocations from SWE-agent trajectory files.

    Returns None if no trajectory file is found (caller falls back to event
    counts). Returns a dict keyed by tool short-name (orient, lookup, impact,
    check) with the number of action strings that begin with gt_<tool>.

    Trajectory files are authoritative for tool invocations: the hook's
    per-task summary never increments lookup/impact counters, so event-based
    counts undercount real usage by design.
    """
    counts = {"orient": 0, "lookup": 0, "impact": 0, "check": 0}
    found = False
    for traj in task_dir.rglob("*.traj*"):
        if traj.is_dir():
            continue
        found = True
        try:
            data = json.loads(traj.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        records = data.get("trajectory") if isinstance(data, dict) else None
        if not isinstance(records, list):
            records = [data]
        for rec in records:
            for action in _extract_actions(rec):
                for tool in counts:
                    if action.startswith(f"gt_{tool}"):
                        counts[tool] += 1
                        break
    return counts if found else None


def _utilization(index_sentinel: dict, briefing_meta: dict, row: dict,
                 task_dir: Path | None = None) -> dict:
    """Plan §3A utilization block.

    briefing_utilized: briefing_meta with non-zero tokens, OR gt_orient was
    called while the index was ready (wrapper emitted but meta not
    harvested, e.g. container exited before scraper swept).

    check_utilized: gt_check telemetry counter >= 1, OR the trajectory
    contains the <gt-check> tag (PreSubmit hook fired).
    """
    idx_ok = (index_sentinel or {}).get("status") == "success"
    brief_tok = int(briefing_meta.get("token_count", 0) or 0) if briefing_meta else 0
    orient_count = int(row.get("gt_orient_count", 0) or 0)
    brief_ok = (brief_tok > 0) or (orient_count >= 1 and idx_ok)
    check_count = int(row.get("gt_check_count", 0) or 0)
    check_ok = check_count >= 1 or (task_dir is not None and _trajectory_has_gt_check(task_dir))
    score_num = int(idx_ok) + int(brief_ok) + int(check_ok)
    return {
        "index_utilized": bool(idx_ok),
        "briefing_utilized": bool(brief_ok),
        "check_utilized": bool(check_ok),
        "full_utilization": score_num == 3,
        "utilization_score": f"{score_num}/3",
    }


def _failure_diagnosis(util: dict, index_sentinel: dict, briefing_meta: dict) -> dict | None:
    """Plan §3D — only emitted when utilization_score < 3/3."""
    if util["full_utilization"]:
        return None
    if not util["index_utilized"]:
        status = (index_sentinel or {}).get("status")
        return {
            "failed_phase": "index",
            "failure_category": "timeout" if status is None else "gt_bug",
            "recommended_action": (
                "Check /tmp/gt_index.log for indexer traceback; if sentinel absent, the 120s wait barrier timed out."
            ),
        }
    if not util["briefing_utilized"]:
        if briefing_meta.get("identifier_count", 0) == 0:
            return {
                "failed_phase": "briefing",
                "failure_category": "no_matches",
                "recommended_action": "No identifiers extracted from issue text — expected; exclude from numerator.",
            }
        return {
            "failed_phase": "briefing",
            "failure_category": "gate_over_filter",
            "recommended_action": "Inspect briefing.admissibility_gate counters; tune HAS_VALUE / CONCISE if rejection concentrated.",
        }
    return {
        "failed_phase": "check",
        "failure_category": "gt_bug",
        "recommended_action": "PreSubmit hook did not fire — check /root/tools/review_on_submit_m/bin/submit patch.",
    }


def emit_task_log(task_dir: Path, row: dict, baseline_outdir: Path | None) -> dict:
    """Plan §3A — one JSON object per task at gt_task_log.json."""
    briefing_meta = _load_briefing_meta(task_dir)
    index_sentinel = _load_index_sentinel(task_dir)
    util = _utilization(index_sentinel, briefing_meta, row, task_dir=task_dir)
    traj_counts = _count_tool_calls_from_trajectory(task_dir)
    # Event-based counts derived directly from telemetry (unaffected by
    # trajectory override in build_row) so divergence stays visible.
    telem_path = task_dir / "gt_hook_telemetry.jsonl"
    events = _count_events(telem_path) if telem_path.exists() else {}
    event_counts = dict(events)
    budget_state = _load_budget_state(task_dir)
    if "checkpoint_startup" not in event_counts and "startup" not in event_counts:
        event_counts["checkpoint_startup"] = 0
    log = {
        "task_id": task_dir.name,
        "run_id": row.get("run_id"),
        "arm": row.get("arm"),
        "index": {
            "status": index_sentinel.get("status", "unknown"),
            "node_count": index_sentinel.get("nodes"),
            "edge_count": index_sentinel.get("edges"),
            "deterministic_edges": (index_sentinel.get("same_file", 0) or 0)
                                   + (index_sentinel.get("import", 0) or 0),
            "name_match_edges": index_sentinel.get("name_match", 0),
        },
        "briefing": {
            "fired": util["briefing_utilized"],
            "meta_present": bool(briefing_meta),
            "token_count": briefing_meta.get("token_count"),
            "line_count": briefing_meta.get("line_count"),
            "symbol_count": briefing_meta.get("symbol_count"),
            "within_token_budget": briefing_meta.get("within_token_budget"),
        },
        "tool_calls": {
            "trajectory_counts": traj_counts,
            "event_counts": event_counts,
            "source_of_record": "trajectory" if traj_counts is not None else "events",
        },
        "budget": {
            "state_present": bool(budget_state),
            "state": budget_state,
        },
        "tool_calls_summary": {
            "orient": row.get("gt_orient_count", 0),
            "lookup": row.get("gt_lookup_count", 0),
            "impact": row.get("gt_impact_count", 0),
            "check": row.get("gt_check_count", 0),
        },
        "event_counts": event_counts,
        "checkpoint_startup_count": events.get("checkpoint_startup", 0),
        "material_edit_count": events.get("material_edit", 0),
        "micro_emit_count": events.get("micro_emitted", 0),
        "micro_suppress_count": events.get("micro_suppressed", 0),
        "verify_emit_count": events.get("verify_emitted", 0),
        "verify_suppress_count": events.get("verify_suppressed", 0),
        "ack_followed_count": events.get("ack_followed", 0),
        "ack_ignored_count": events.get("ack_ignored", 0),
        "ack_not_observed_count": events.get("ack_not_observed", 0),
        "budget_denied_count": events.get("budget_denied", 0),
        "submit_observed_count": events.get("submit_observed", 0),
        "submit_gate_blocked_count": events.get("submit_gate_blocked", 0),
        "submit_gate_bypassed_count": events.get("submit_gate_bypassed", 0),
        "pre_edit_briefing_count": events.get("pre_edit_briefing", 0),
        "lsp_promotion_count": events.get("lsp_promotion", 0),
        "gt_check": {
            "fired": util["check_utilized"],
            "invocations": row.get("gt_check_count", 0),
        },
        "outcome": _outcome_from_dirs(task_dir, baseline_outdir),
        "utilization": util,
    }
    diag = _failure_diagnosis(util, index_sentinel, briefing_meta)
    if diag:
        log["failure_diagnosis"] = diag
    (task_dir / "gt_task_log.json").write_text(json.dumps(log, indent=2))
    return log


def emit_smoke_summary(outdir: Path, logs: list[dict]) -> None:
    """Plan §3B — cross-task summary markdown + JSON."""
    if not logs:
        return
    util_matrix = []
    for log in logs:
        u = log["utilization"]
        util_matrix.append({
            "task_id": log["task_id"],
            "index": "✅" if u["index_utilized"] else "❌",
            "briefing": "✅" if u["briefing_utilized"] else "❌",
            "check": "✅" if u["check_utilized"] else "❌",
        })
    total_det = sum((log["index"].get("deterministic_edges") or 0) for log in logs)
    total_nm = sum((log["index"].get("name_match_edges") or 0) for log in logs)
    det_pct = (total_det / (total_det + total_nm)) if (total_det + total_nm) else 0.0

    summary = {
        "task_count": len(logs),
        "full_utilization_rate": sum(1 for l in logs if l["utilization"]["full_utilization"]) / len(logs),
        "deterministic_edge_pct": det_pct,
        "gains": sum(1 for l in logs if l["outcome"]["is_gain"]),
        "regressions": sum(1 for l in logs if l["outcome"]["is_regression"]),
        "utilization_matrix": util_matrix,
    }
    (outdir / "gt_smoke_summary.json").write_text(json.dumps(summary, indent=2))

    md_lines = [
        "# GT Smoke Summary",
        "",
        f"- tasks: **{summary['task_count']}**",
        f"- full utilization: **{summary['full_utilization_rate']:.0%}**",
        f"- deterministic edges: **{det_pct:.0%}** (target ≥ 60%)",
        f"- gains vs baseline: **{summary['gains']}**",
        f"- regressions vs baseline: **{summary['regressions']}**",
        "",
        "| task_id | index | briefing | check |",
        "|---|:-:|:-:|:-:|",
    ]
    for row in util_matrix:
        md_lines.append(f"| {row['task_id']} | {row['index']} | {row['briefing']} | {row['check']} |")
    (outdir / "gt_smoke_summary.md").write_text("\n".join(md_lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--hybrid", action="store_true",
                    help="Enforce SHOULD gate: lsp_promotion_count>=1 on edited tasks.")
    ap.add_argument("--max-steps", type=int, default=100)
    ap.add_argument("--emit-task-logs", action="store_true",
                    help="Plan §3A: emit gt_task_log.json in each task dir.")
    ap.add_argument("--emit-smoke-summary", action="store_true",
                    help="Plan §3B: emit gt_smoke_summary.{md,json} at outdir root.")
    ap.add_argument("--baseline-outdir", default="",
                    help="Baseline arm outdir; used to diff outcomes for gain/regression labelling.")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    if not outdir.is_dir():
        print("ERROR: outdir does not exist: %s" % outdir, file=sys.stderr)
        return 2

    task_dirs = _find_task_dirs(outdir)
    if not task_dirs:
        print("ERROR: no task subdirs under %s" % outdir, file=sys.stderr)
        return 2

    rows = [build_row(outdir, td, args.arm, args.run_id,
                      args.max_steps, args.hybrid) for td in task_dirs]

    csv_path = outdir / "gt_report.csv"
    json_path = outdir / "gt_arm_summary.json"
    write_csv(rows, csv_path)
    summary = arm_summary(rows)
    summary["arm"] = args.arm
    summary["run_id"] = args.run_id
    json_path.write_text(json.dumps(summary, indent=2))

    baseline_outdir = Path(args.baseline_outdir) if args.baseline_outdir else None
    task_logs: list[dict] = []
    if args.emit_task_logs:
        for td, r in zip(task_dirs, rows):
            task_logs.append(emit_task_log(td, r, baseline_outdir))
    if args.emit_smoke_summary:
        if not task_logs:
            task_logs = [emit_task_log(td, r, baseline_outdir)
                         for td, r in zip(task_dirs, rows)]
        emit_smoke_summary(outdir, task_logs)

    # Print a human-readable per-row digest to stdout.
    print("# %s (run_id=%s) — %d tasks" % (args.arm, args.run_id, len(rows)))
    print("instance_id".ljust(30) + "cycle  orient lookup impact check  micro verify  ack_f  lsp_p  patch  OK")
    for r in rows:
        mark = "PASS" if r["run_invalid"] == 0 and r["should_ok"] == 1 else ("MUST_FAIL" if r["run_invalid"] else "SHOULD_FAIL")
        print(
            "%s%5d  %5d %5d %5d %5d  %5d %5d  %5d %5d %6d  %s%s" % (
                r["instance_id"].ljust(30),
                r["cycle"], r["gt_orient_count"], r["gt_lookup_count"],
                r["gt_impact_count"], r["gt_check_count"],
                r["micro_emit_count"], r["verify_emit_count"],
                r["ack_followed_count"], r["lsp_promotion_count"],
                r["patch_bytes"], mark,
                ("  " + r["fail_reasons"]) if r["fail_reasons"] else "",
            )
        )
    print()
    print("must_ok_rate=%.2f should_ok_rate=%.2f has_patch_rate=%.2f" % (
        summary["must_ok_rate"], summary["should_ok_rate"], summary["has_patch_rate"]))
    print("written: %s" % csv_path)
    print("written: %s" % json_path)

    return 0 if summary["run_invalid_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
