#!/usr/bin/env python3
"""Baseline-arm dashboard — computes B/C/D/E metrics for the frozen baseline's matching tasks,
from its single multi-instance output.jsonl, so GT-on can be paired (Δ) against it.

A (context quality) is GT-on-only and stays null here (baseline has no GT context).
Reuses pm_dashboard's helpers + groundtruth.metrics.run_metrics (canonical formulas).

Usage: PYTHONPATH=src python scripts/metrics/baseline_dashboard.py \
         <baseline_output.jsonl> <baseline_resolved_json> <dataset_jsonl> <task_ids_file> [out.json]
"""
import json, sys, os, glob, statistics
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_dashboard import (gold_files_from_patch, load_dataset, load_json,
                          phases_and_actions, turns_to_gold_read, RM)

def main():
    bl_path, resolved_path, dataset_path, tasks_path = sys.argv[1:5]
    out_path = sys.argv[5] if len(sys.argv) > 5 else None
    resolved = set(load_json(resolved_path).get("resolved_ids", []))
    ds = load_dataset(dataset_path)
    want = [t.strip() for t in open(tasks_path, encoding="utf-8") if t.strip()]

    insts = {}
    for ln in open(bl_path, encoding="utf-8"):
        if ln.strip():
            try:
                d = json.loads(ln); insts[d.get("instance_id")] = d
            except Exception:
                pass

    rows = []
    for task in want:
        inst = insts.get(task)
        if inst is None:
            rows.append({"task": task, "baseline_trajectory": "MISSING"})
            continue
        hist = inst.get("history", [])
        gold = gold_files_from_patch(ds.get(task, {}).get("patch", ""))
        nav = RM.compute_navigation(inst, gold) if gold else {}
        ph, first_edit, actions = phases_and_actions(hist)
        ttg_read = turns_to_gold_read(hist, gold) if gold else None
        met = inst.get("metrics", {}) if isinstance(inst.get("metrics"), dict) else {}
        cost = met.get("accumulated_cost") or met.get("cost") or 0
        rows.append({
            "task": task, "baseline_trajectory": "present", "n_gold_files": len(gold),
            "resolved": int(task in resolved),
            "turns_to_gold_read": float(ttg_read) if ttg_read else None,
            "turns_to_gold_edit": float(nav.get("edit_to_gold_action")) if nav.get("edit_to_gold_action") else None,
            "gold_edited": int(bool(nav.get("gold_edited"))),
            "actions": float(actions), "first_edit_action": float(first_edit) if first_edit else None,
            "phase_explore": float(ph["explore"]), "phase_understand": float(ph["understand"]),
            "phase_edit": float(ph["edit"]), "phase_verify": float(ph["verify"]),
            "llm_cost_usd": float(cost or 0),
        })

    present = [r for r in rows if r.get("baseline_trajectory") == "present"]
    n = len(present) or 1
    def mean(k):
        v = [r[k] for r in present if r.get(k) is not None]
        return round(statistics.mean(v), 8) if v else None
    agg = {
        "n_tasks": len(present), "n_missing": len(rows) - len(present),
        "resolve_rate": round(sum(r["resolved"] for r in present) / n, 8),
        "mean_turns_to_gold_read": mean("turns_to_gold_read"),
        "mean_turns_to_gold_edit": mean("turns_to_gold_edit"),
        "gold_edited_rate": round(sum(r["gold_edited"] for r in present) / n, 8),
        "mean_actions": mean("actions"), "mean_first_edit_action": mean("first_edit_action"),
        "cost_total_usd": round(sum(r["llm_cost_usd"] for r in present), 8),
        # A-metrics intentionally null (baseline has no GT context)
        "mean_context_precision": None, "mean_context_recall": None,
        "mean_first_gold_rank": None, "hit_at_1_rate": None, "hit_at_3_rate": None,
        "flips": None, "regressions": None, "net_flips": None, "gt_caused_flips": None,
        "cost_per_flip_usd": None, "cost_per_resolved_usd": (round(sum(r["llm_cost_usd"] for r in present)/max(1,sum(r["resolved"] for r in present)),8) if sum(r["resolved"] for r in present) else None),
        "infra_failure_rate": 0.0, "semantic_on_rate": 0.0,
    }
    out = {"aggregate": agg, "per_task": rows}
    if out_path: json.dump(out, open(out_path, "w"), indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    main()
