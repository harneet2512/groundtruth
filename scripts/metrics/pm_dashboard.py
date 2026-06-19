#!/usr/bin/env python3
"""GT PM dashboard — the metrics a Claude Code PM reviews (A-G).

REUSES the project's canonical formulas (groundtruth.metrics.run_metrics: compute_localization,
compute_navigation, _extract_brief_ranked_files, _action_reaches_gold) — NOT reinvented — and adds
the RAG/ContextBench context-quality family (context_precision/recall) + unit economics (cost/flip),
because GT is a context-retrieval product.

  A Context quality:   context_precision, context_recall, first_gold_rank, focus_precision, focus_coverage
  B Localization velocity: turns_to_gold_read, turns_to_gold_edit(=edit_to_gold_action), gold_edited
  C Outcome+attribution:  resolved, flip, regression, net; gt_caused (filled by verifier agents)
  D Efficiency:           actions, EXPLORE/UNDERSTAND/EDIT/VERIFY phases, first_edit_action
  E Unit economics:       llm_cost, cost_per_resolved, cost_per_flip, gt_overhead_pct
  F Reliability:          agent_started, infra_failure, wall_clock, semantic_on
  G Quality:              has_patch, over_edit proxy (edited files count)

Usage: PYTHONPATH=src python scripts/metrics/pm_dashboard.py <run_dir> <baseline_json> <dataset_jsonl> [out.json]
Legitimacy: gold patch used ONLY for post-hoc metric analysis, never in product logic.
"""
import json, sys, os, glob, re, statistics

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
try:
    from groundtruth.metrics import run_metrics as RM
except Exception as e:  # pragma: no cover
    print(f"FATAL: cannot import run_metrics ({e}) — run with PYTHONPATH=src", file=sys.stderr)
    raise

def load_json(p):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return {}

def first_obj(p):
    try:
        for ln in open(p, encoding="utf-8"):
            if ln.strip(): return json.loads(ln)
    except Exception: pass
    return {}

def gold_files_from_patch(patch: str):
    files = set()
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch or "", re.M):
        files.add(m.group(2))
    for m in re.finditer(r"^\+\+\+ b/(.+?)$", patch or "", re.M):
        if m.group(1) != "/dev/null": files.add(m.group(1))
    return sorted(files)

def load_dataset(path):
    idx = {}
    try:
        for ln in open(path, encoding="utf-8"):
            if ln.strip():
                d = json.loads(ln); idx[d.get("instance_id")] = d
    except Exception: pass
    return idx

EXPLORE = ("ls", "find", "grep", "glob", "search", "tree")
VERIFY = ("pytest", "tox", "unittest", "run_tests", "python -m pytest")

def phases_and_actions(history):
    ph = {"explore": 0, "understand": 0, "edit": 0, "verify": 0, "other": 0}
    first_edit = None; actions = 0
    for e in history:
        act = (e.get("action") or "")
        if act in ("think", "recall", "message", "", "system"): continue
        actions += 1
        blob = (str(e.get("args", {})) + " " + str(e.get("command", ""))).lower()
        if act in ("edit", "write") or "str_replace" in blob:
            ph["edit"] += 1
            if first_edit is None: first_edit = actions
        elif any(v in blob for v in VERIFY): ph["verify"] += 1
        elif any(x in blob for x in EXPLORE): ph["explore"] += 1
        elif act in ("read", "view") or "cat " in blob or "open(" in blob: ph["understand"] += 1
        else: ph["other"] += 1
    return ph, first_edit, actions

def turns_to_gold_read(history, gold):
    """First action index (counting non-think actions) whose target is a gold file (any read/view/cat)."""
    counted = 0
    for e in history:
        act = (e.get("action") or "")
        if act in ("think", "recall", "message", "", "system"): continue
        counted += 1
        try:
            if RM._action_reaches_gold(e, gold): return counted
        except Exception:
            blob = str(e.get("args", {})) + " " + str(e.get("command", ""))
            if any(RM._path_suffix_match(RM._norm_path(t), RM._norm_path(g)) for g in gold for t in re.findall(r"[\w./-]+\.\w+", blob)):
                return counted
    return None

def main():
    run_dir, baseline_path, dataset_path = sys.argv[1], sys.argv[2], sys.argv[3]
    out_path = sys.argv[4] if len(sys.argv) > 4 else None
    baseline = set(load_json(baseline_path).get("resolved_ids", []))
    ds = load_dataset(dataset_path)

    rows = []
    for td in sorted(glob.glob(os.path.join(run_dir, "task-*"))):
        task = os.path.basename(td).replace("task-", "")
        oj = next(iter(glob.glob(os.path.join(td, "results", "**", "output.jsonl"), recursive=True)), "")
        inst = first_obj(oj) if oj else {}
        hist = inst.get("history", [])
        dm = load_json(next(iter(glob.glob(os.path.join(td, "gt_debug", "gt_deep_metrics_*.json"))), ""))
        ev = load_json(next(iter(glob.glob(os.path.join(td, "eval_result.json"))), ""))
        gold = gold_files_from_patch(ds.get(task, {}).get("patch", ""))
        eff = dm.get("efficiency", {}) if isinstance(dm.get("efficiency"), dict) else {}

        loc = RM.compute_localization(inst, gold) if gold else {}
        nav = RM.compute_navigation(inst, gold) if gold else {}
        ranked = []
        try: ranked = RM._extract_brief_ranked_files(inst)
        except Exception: ranked = []
        gold_n = [RM._norm_path(g) for g in gold]
        hit = [rf for rf in ranked if any(RM._path_suffix_match(RM._norm_path(rf), g) for g in gold_n)]
        ctx_p = (len(hit) / len(ranked)) if ranked else None
        ctx_r = (len(set(g for g in gold_n if any(RM._path_suffix_match(RM._norm_path(rf), g) for rf in ranked))) / len(gold_n)) if gold_n else None
        ph, first_edit, actions = phases_and_actions(hist)
        ttg_read = turns_to_gold_read(hist, gold) if gold else None
        resolved = bool(ev.get("resolved_instances", 0)) if "resolved_instances" in ev else (task in (ev.get("resolved_ids") or []))
        base = task in baseline
        rows.append({
            "task": task, "n_gold_files": len(gold),
            # A context quality
            "context_precision": round(ctx_p, 8) if ctx_p is not None else None,
            "context_recall": round(ctx_r, 8) if ctx_r is not None else None,
            "first_gold_rank": loc.get("first_gold_rank"),
            "focus_precision": loc.get("focus_precision"),
            "focus_coverage": loc.get("focus_coverage"),
            # B localization velocity
            "turns_to_gold_read": float(ttg_read) if ttg_read else None,
            "turns_to_gold_edit": float(nav.get("edit_to_gold_action")) if nav.get("edit_to_gold_action") else None,
            "gold_edited": int(bool(nav.get("gold_edited"))),
            # C outcome + attribution
            "resolved": int(resolved), "baseline_pass": int(base),
            "flip": int(resolved and not base), "regression": int((not resolved) and base),
            "gt_caused": None, "regression_root_cause": None, "baseline_actions": None,
            # D efficiency
            "actions": float(actions), "first_edit_action": float(first_edit) if first_edit else None,
            "phase_explore": float(ph["explore"]), "phase_understand": float(ph["understand"]),
            "phase_edit": float(ph["edit"]), "phase_verify": float(ph["verify"]),
            # E economics
            "llm_cost_usd": float(eff.get("llm_cost_usd", 0)),
            "llm_tokens_total": float(eff.get("llm_tokens_total", 0)),
            "gt_overhead_pct": float(eff.get("gt_injection_overhead_pct", 0)),
            # F reliability
            "agent_started": int(bool(dm.get("agent_started"))),
            "semantic_enabled": int(bool(dm.get("semantic_enabled"))),
            "verified_edge_ratio": float(dm.get("verified_edge_ratio", 0)),
        })

    n = len(rows) or 1
    res = [r for r in rows if r["resolved"]]; flips = [r for r in rows if r["flip"]]
    cost = sum(r["llm_cost_usd"] for r in rows)
    def mean(key, src=rows):
        v = [r[key] for r in src if r.get(key) is not None]
        return round(statistics.mean(v), 8) if v else None
    agg = {
        "n_tasks": len(rows),
        # A
        "mean_context_precision": mean("context_precision"), "mean_context_recall": mean("context_recall"),
        "mean_first_gold_rank": mean("first_gold_rank"), "mean_focus_precision": mean("focus_precision"),
        "hit_at_1_rate": round(sum(1 for r in rows if r["first_gold_rank"] == 1) / n, 8),
        "hit_at_3_rate": round(sum(1 for r in rows if r["first_gold_rank"] and r["first_gold_rank"] <= 3) / n, 8),
        # B
        "mean_turns_to_gold_read": mean("turns_to_gold_read"), "mean_turns_to_gold_edit": mean("turns_to_gold_edit"),
        "gold_edited_rate": round(sum(r["gold_edited"] for r in rows) / n, 8),
        # C
        "resolve_rate": round(len(res) / n, 8), "flips": len(flips),
        "regressions": sum(r["regression"] for r in rows), "net_flips": len(flips) - sum(r["regression"] for r in rows),
        "gt_caused_flips": "pending_verifier_agents",
        # D
        "mean_actions": mean("actions"), "mean_first_edit_action": mean("first_edit_action"),
        # E
        "cost_total_usd": round(cost, 8), "cost_per_resolved_usd": round(cost / len(res), 8) if res else None,
        "cost_per_flip_usd": round(cost / len(flips), 8) if flips else None,
        # F
        "agent_started_rate": round(sum(r["agent_started"] for r in rows) / n, 8),
        "infra_failure_rate": round(sum(1 for r in rows if not r["agent_started"]) / n, 8),
        "semantic_on_rate": round(sum(r["semantic_enabled"] for r in rows) / n, 8),
    }
    out = {"aggregate": agg, "per_task": rows}
    if out_path: json.dump(out, open(out_path, "w"), indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    main()
