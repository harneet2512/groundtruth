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
    "lsp_promotion_count",
    "patch_bytes", "has_patch",
    "gt_budget_ok", "gt_budget_fail_reasons",
    "within_call_budget", "identity_ok",
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

    def g(key: str, default=0):
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
        "lsp_promotion_count": g("lsp_promotion_count"),
        "patch_bytes": patch_bytes,
        "has_patch": 1 if patch_bytes > 0 else 0,
        "gt_budget_ok": 1,
        "gt_budget_fail_reasons": "",
        "within_call_budget": 1 if within_budget else 0,
        "identity_ok": 1 if identity_ok else 0,
    }

    fails: list[str] = []
    if not identity_ok:
        fails.append("identity_missing")
    if not within_budget:
        fails.append("over_call_budget")

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
        "avg_material_edit": sum_i("material_edit_count") / n,
        "avg_micro_emit": sum_i("micro_emit_count") / n,
        "avg_verify_emit": sum_i("verify_emit_count") / n,
        "ack_followed_total": sum_i("ack_followed_count"),
        "ack_ignored_total": sum_i("ack_ignored_count"),
        "ack_not_observed_total": sum_i("ack_not_observed_count"),
        "lsp_promotion_total": sum_i("lsp_promotion_count"),
        "gt_budget_violations": sum(1 for r in rows if int(r.get("gt_budget_ok", 1)) == 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--hybrid", action="store_true",
                    help="Enforce SHOULD gate: lsp_promotion_count>=1 on edited tasks.")
    ap.add_argument("--max-steps", type=int, default=100)
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
