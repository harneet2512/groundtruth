#!/usr/bin/env python3
"""Deep metrics for GT runs — GT-side + Agent-side + mechanism activation.

Usage:
    python scripts/deep_metrics.py /tmp/v2_full10
    python scripts/deep_metrics.py /tmp/v2_full10 --compare /tmp/v2_all5
    python scripts/deep_metrics.py /tmp/v2_full10 --eval  # include official eval results
"""
import argparse
import json
import os
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _find_outputs(base):
    return sorted(glob.glob(f"{base}/task-*/results/**/output.jsonl", recursive=True))


def _find_layer_events(base):
    return sorted(glob.glob(f"{base}/task-*/gt_debug/gt_layer_events_*.jsonl"))


def _find_eval(base):
    return sorted(glob.glob(f"{base}/task-*/eval_result.json"))


def _find_run_summary(base):
    return sorted(glob.glob(f"{base}/task-*/gt_debug/gt_run_summary_*.json"))


def _find_logs(base):
    return sorted(glob.glob(f"{base}/task-*/gt_debug/full_run_*.log"))


def _tid(path):
    return path.replace("\\", "/").split("task-")[1].split("/")[0]


def _short(tid):
    return tid.split("__")[1][:15] if "__" in tid else tid[:15]


def analyze_task(base, tid):
    """Full analysis for one task."""
    m = {}
    m["task_id"] = tid
    m["short"] = _short(tid)

    # --- Output.jsonl metrics ---
    outputs = [f for f in _find_outputs(base) if tid in f.replace("\\", "/")]
    if not outputs:
        m["has_output"] = False
        return m
    m["has_output"] = True

    with open(outputs[0], encoding="utf-8", errors="replace") as f:
        data = json.loads(f.readline())

    history = data.get("history", [])
    m["history_len"] = len(history)

    # Action count
    action_count = 0
    for e in history:
        a = e.get("action", "")
        if a and a not in ("think", "recall", "message", ""):
            action_count += 1
    m["action_count"] = action_count

    # GT visibility
    gt_visible = 0
    gt_constraint = 0
    gt_recall = 0
    gt_semantic = 0
    gt_scope = 0
    gt_next = 0
    gt_tool_calls = 0
    for e in history:
        c = str(e.get("content", "") or "") + str(e.get("message", "") or "")
        args = e.get("args", {}) if isinstance(e.get("args"), dict) else {}
        cmd = str(args.get("command", ""))

        if "[GT]" in c or "[GT-router-v2" in c or "[GT L5" in c:
            gt_visible += 1
        if "gt-constraint" in c:
            gt_constraint += 1
        if "[RECALL]" in c:
            gt_recall += 1
        if "SEMANTIC WARNING" in c:
            gt_semantic += 1
        if "SCOPE:" in c:
            gt_scope += 1
        if "Next: read" in c:
            gt_next += 1
        if "gt_query" in cmd or "gt_validate" in cmd:
            gt_tool_calls += 1

    m["gt_visible"] = gt_visible
    m["gt_constraint"] = gt_constraint
    m["gt_recall"] = gt_recall
    m["gt_semantic_warning"] = gt_semantic
    m["gt_scope_warning"] = gt_scope
    m["gt_next_suggestions"] = gt_next
    m["gt_tool_calls"] = gt_tool_calls

    # Gold file detection (from localization_metrics if available)
    try:
        from scripts.localization_metrics import compute_task_metrics
        lm = compute_task_metrics(outputs[0], tid)
        m["first_gold_view"] = lm.get("first_gold_view_step")
        m["edit_precision"] = lm.get("edit_file_precision", 0)
        m["late_guidance"] = lm.get("late_guidance_count", 0)
        m["stale_guidance"] = lm.get("stale_guidance_count", 0)
    except Exception:
        m["first_gold_view"] = None
        m["edit_precision"] = None

    # Patch info
    patch = data.get("test_result", {}).get("git_patch", "") or ""
    real_files = [l[6:].strip() for l in patch.split("\n") if l.startswith("+++ b/") and ".openhands" not in l]
    m["has_patch"] = len(real_files) > 0
    m["patch_files"] = real_files[:3]

    # --- Layer events ---
    layer_files = [f for f in _find_layer_events(base) if tid in f.replace("\\", "/")]
    layers = {}
    for lf in layer_files:
        for line in open(lf, encoding="utf-8", errors="replace"):
            try:
                ev = json.loads(line)
                layer = ev.get("layer", "?")
                if layer not in layers:
                    layers[layer] = {"emit": 0, "sup": 0, "reasons": {}}
                if ev.get("emitted"):
                    layers[layer]["emit"] += 1
                else:
                    layers[layer]["sup"] += 1
                    r = ev.get("suppression_reason", "")
                    if r:
                        layers[layer]["reasons"][r] = layers[layer]["reasons"].get(r, 0) + 1
            except Exception:
                pass
    m["layers"] = layers

    # --- Run summary utilization ---
    summaries = [f for f in _find_run_summary(base) if tid in f.replace("\\", "/")]
    util = {}
    for sf in summaries:
        try:
            s = json.load(open(sf, encoding="utf-8"))
            for l, data_l in s.get("per_layer", {}).items():
                util[l] = data_l.get("utilization_score", 0)
        except Exception:
            pass
    m["utilization"] = util

    # --- Eval result ---
    evals = [f for f in _find_eval(base) if tid in f.replace("\\", "/")]
    if evals:
        try:
            ev = json.load(open(evals[0], encoding="utf-8"))
            if "resolved_ids" in ev:
                m["resolved"] = tid in ev.get("resolved_ids", [])
                m["eval_status"] = ev.get("status", "")
            else:
                for k, v in ev.items():
                    if isinstance(v, dict) and "resolved" in v:
                        m["resolved"] = v["resolved"]
                        ts = v.get("tests_status", {})
                        f2p = ts.get("FAIL_TO_PASS", {})
                        p2p = ts.get("PASS_TO_PASS", {})
                        m["f2p_pass"] = len(f2p.get("success", []))
                        m["f2p_total"] = m["f2p_pass"] + len(f2p.get("failure", []))
                        m["p2p_regress"] = len(p2p.get("failure", []))
                        break
        except Exception:
            pass

    # --- Log analysis ---
    logs = [f for f in _find_logs(base) if tid in f.replace("\\", "/")]
    if logs:
        try:
            log_text = open(logs[0], encoding="utf-8", errors="replace").read()
            m["l5_scaffold_fired"] = "No Source Edits" in log_text or "scaffolding_trap" in log_text
            m["l5_no_durable"] = "No Durable Progress" in log_text
            m["behavioral_contract"] = "BEHAVIORAL CONTRACT" in log_text
            m["l6_caller_delta"] = "L6 caller delta" in log_text
        except Exception:
            pass

    return m


def print_report(tasks, label=""):
    if label:
        print(f"\n{'=' * 80}")
        print(f"  {label}")
        print(f"{'=' * 80}")

    # Summary table
    print(f"\n{'Task':<16} {'Res':<5} {'F2P':<6} {'Acts':<5} {'1stG':<5} {'Prec':<5} {'GT#':<4} {'Con':<4} {'Rcl':<4} {'Sem':<4} {'Scp':<4} {'Nxt':<4} {'Tool':<4}")
    print("-" * 85)
    for m in tasks:
        if not m.get("has_output"):
            print(f"{m['short']:<16} NO OUTPUT")
            continue
        res = "YES" if m.get("resolved") else ("EVAL?" if "resolved" not in m else "NO")
        f2p = f"{m.get('f2p_pass', '?')}/{m.get('f2p_total', '?')}" if "f2p_pass" in m else "-"
        fgv = str(m.get("first_gold_view") or "-")
        prec = f"{m.get('edit_precision', 0):.2f}" if m.get("edit_precision") is not None else "-"
        print(f"{m['short']:<16} {res:<5} {f2p:<6} {m['action_count']:<5} {fgv:<5} {prec:<5} "
              f"{m['gt_visible']:<4} {m['gt_constraint']:<4} {m['gt_recall']:<4} "
              f"{m['gt_semantic_warning']:<4} {m['gt_scope_warning']:<4} {m['gt_next_suggestions']:<4} {m['gt_tool_calls']:<4}")

    n = len(tasks)
    resolved = sum(1 for m in tasks if m.get("resolved"))
    evaluable = sum(1 for m in tasks if "resolved" in m)
    avg_acts = sum(m.get("action_count", 0) for m in tasks if m.get("has_output")) / max(n, 1)
    print("-" * 85)
    print(f"{'TOTAL':<16} {resolved}/{evaluable:<4} {'':6} {avg_acts:<5.0f}")

    # Layer utilization
    print(f"\n{'Task':<16} {'L1':<8} {'L3 em/tot':<10} {'L4':<8} {'L5':<8} {'L6':<8} {'Sup reasons'}")
    print("-" * 80)
    for m in tasks:
        if not m.get("has_output"):
            continue
        layers = m.get("layers", {})
        l1 = layers.get("L1", {"emit": 0, "sup": 0})
        l3 = layers.get("L3_router_v2", {"emit": 0, "sup": 0})
        l4 = layers.get("L4", {"emit": 0, "sup": 0})
        l5 = layers.get("L5", {"emit": 0, "sup": 0})
        l5b = layers.get("L5b", {"emit": 0, "sup": 0})
        l6 = layers.get("L6", {"emit": 0, "sup": 0})
        l5_total = l5["emit"] + l5b["emit"]
        top_sup = sorted(l3.get("reasons", {}).items(), key=lambda x: -x[1])[:2]
        sup_str = ", ".join(f"{k}={v}" for k, v in top_sup) if top_sup else "-"
        print(f"{m['short']:<16} {l1['emit']}/{l1['emit'] + l1['sup']:<5} "
              f"{l3['emit']:>2}/{l3['emit'] + l3['sup']:<7} "
              f"{l4['emit']}/{l4['emit'] + l4['sup']:<5} "
              f"{l5_total:<7} "
              f"{l6['emit']}/{l6['emit'] + l6['sup']:<5} "
              f"{sup_str}")

    # Mechanism activation
    print(f"\n{'Task':<16} {'L4rel':<6} {'Contr':<6} {'Const':<6} {'Recall':<7} {'Seman':<6} {'Scope':<6} {'L5scf':<6} {'L6con':<6} {'BhvCt':<6}")
    print("-" * 80)
    for m in tasks:
        if not m.get("has_output"):
            continue
        print(f"{m['short']:<16} "
              f"{'?' :<6} "
              f"{'YES' if m.get('gt_constraint') else 'NO':<6} "
              f"{'YES' if m.get('gt_constraint') else 'NO':<6} "
              f"{'YES' if m.get('gt_recall') else 'NO':<7} "
              f"{'YES' if m.get('gt_semantic_warning') else 'NO':<6} "
              f"{'YES' if m.get('gt_scope_warning') else 'NO':<6} "
              f"{'YES' if m.get('l5_scaffold_fired') else 'NO':<6} "
              f"{'YES' if m.get('l6_caller_delta') else 'NO':<6} "
              f"{'YES' if m.get('behavioral_contract') else 'NO':<6}")


def main():
    parser = argparse.ArgumentParser(description="Deep GT run metrics")
    parser.add_argument("path", help="Artifact directory")
    parser.add_argument("--compare", help="Compare against another artifact dir")
    parser.add_argument("--eval", action="store_true", help="Include eval results")
    args = parser.parse_args()

    outputs = _find_outputs(args.path)
    tids = [_tid(f) for f in outputs]
    tasks = [analyze_task(args.path, tid) for tid in tids]
    print_report(tasks, os.path.basename(args.path))

    if args.compare:
        comp_outputs = _find_outputs(args.compare)
        comp_tids = [_tid(f) for f in comp_outputs]
        comp_tasks = [analyze_task(args.compare, tid) for tid in comp_tids]
        print_report(comp_tasks, f"COMPARE: {os.path.basename(args.compare)}")

        # Delta table
        shared = set(tids) & set(comp_tids)
        if shared:
            print(f"\n{'=' * 60}")
            print(f"  DELTA (new - old)")
            print(f"{'=' * 60}")
            print(f"{'Task':<16} {'dActs':<7} {'dGT#':<6} {'dCon':<6} {'dRcl':<6} {'dSem':<6}")
            print("-" * 50)
            task_map = {t["task_id"]: t for t in tasks}
            comp_map = {t["task_id"]: t for t in comp_tasks}
            for tid in sorted(shared):
                n = task_map.get(tid, {})
                o = comp_map.get(tid, {})
                da = n.get("action_count", 0) - o.get("action_count", 0)
                dg = n.get("gt_visible", 0) - o.get("gt_visible", 0)
                dc = n.get("gt_constraint", 0) - o.get("gt_constraint", 0)
                dr = n.get("gt_recall", 0) - o.get("gt_recall", 0)
                ds = n.get("gt_semantic_warning", 0) - o.get("gt_semantic_warning", 0)
                print(f"{_short(tid):<16} {da:>+5}  {dg:>+4}  {dc:>+4}  {dr:>+4}  {ds:>+4}")


if __name__ == "__main__":
    main()
