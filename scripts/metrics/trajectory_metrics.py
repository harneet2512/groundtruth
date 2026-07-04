#!/usr/bin/env python3
"""Comprehensive trajectory-efficiency metrics — GT-on vs baseline, paired.

Research-grounded (TRAJEVAL arXiv 2603.24631; the 16,758-trajectory search/read/edit-stage study;
trajectory-reduction arXiv 2509.23586; SWE-agent phases). Decomposes the OH trajectory into
SEARCH / READ / EDIT / VERIFY stages with precision+recall each, plus navigation, wandering,
redundancy/loops, recovery, and action-efficiency. Correct OH parsing:
  action=='read'  -> file read (args.path)
  action=='edit'  -> file edit (args.path)
  action=='run'   -> bash: classify command (grep/find/ls=search, pytest=verify, cat/head=read,
                     sed -i/>/tee/apply_patch=edit, else run_other) + extract file paths
  action=='think'/'task_tracking'/None/'recall'/'finish'/'system' -> not a productive code action

Usage: PYTHONPATH=src python scripts/metrics/trajectory_metrics.py
"""
import json, re, os, sys, glob, statistics
sys.path.insert(0, "src"); sys.path.insert(0, "scripts/metrics")
from pm_dashboard import gold_files_from_patch, RM

SRC_EXT = r"\.(?:py|go|js|ts|java|rs|rb|c|cpp|h|tsx|jsx|cc|hpp)"

def _norm(p):
    p = (p or "").replace("\\", "/")
    p = re.sub(r"^/workspace/[^/]+/", "", p)
    return p.lstrip("/")

def _paths(s):
    return [_norm(m) for m in re.findall(r"[\w./-]+" + SRC_EXT, s or "")]

def _is_gold(path, gold_norm):
    if not path:
        return False
    return any(RM._path_suffix_match(path, g) or RM._path_suffix_match(g, path) for g in gold_norm)

def classify(e):
    """-> (kind, files:list). kind in search/read/edit/verify/run_other/think/scaffold/none."""
    a = e.get("action")
    args = e.get("args", {}) if isinstance(e.get("args"), dict) else {}
    if a == "read":
        return "read", [_norm(args.get("path", ""))]
    if a == "edit":
        return "edit", [_norm(args.get("path", ""))]
    if a == "run":
        cmd = args.get("command", "") or ""
        low = cmd.lower()
        files = _paths(cmd)
        if "pytest" in low or "tox" in low or re.search(r"python -m unittest|nosetests|\bunittest\b", low):
            return "verify", files
        if re.search(r"\b(grep|rg|ag|ack|find|ls|glob|locate|fd)\b", low):
            return "search", files
        if re.search(r"sed -i|>>|[^>]>[^>]|\btee\b|apply_patch|str_replace|\bpatch\b|cat\s*<<|tee\b", cmd):
            return "edit", files
        if re.search(r"\b(cat|head|tail|less|more)\b", low) or "sed -n" in low:
            return "read", files
        return "run_other", files
    if a == "think":
        return "think", []
    if a == "task_tracking":
        return "scaffold", []
    return "none", []

def traj_metrics(history, gold):
    gn = [_norm(g) for g in gold]
    ngold = len(gn) or 1
    reads = []          # ordered (file, is_gold)
    edits = []
    searches = 0
    verifies = []       # action index
    run_other = 0
    think = 0
    scaffold = 0
    actions = 0         # productive code actions (search+read+edit+verify+run_other)
    cmds = []           # for repeat/loop detection (run commands)
    first_gold_read = first_gold_edit = first_edit = first_verify = None
    reads_before_gold = searches_before_gold = 0
    seen_gold_read = False

    for e in history:
        kind, files = classify(e)
        if kind in ("think",):
            think += 1; continue
        if kind == "scaffold":
            scaffold += 1; continue
        if kind == "none":
            continue
        actions += 1
        if e.get("action") == "run":
            cmds.append((e.get("args", {}) or {}).get("command", ""))
        if kind == "search":
            searches += 1
            if not seen_gold_read:
                searches_before_gold += 1
        elif kind == "verify":
            if first_verify is None:
                first_verify = actions
            verifies.append(actions)
        elif kind == "read":
            for f in files:
                if not f:
                    continue
                ig = _is_gold(f, gn)
                reads.append((f, ig))
                if not seen_gold_read:
                    reads_before_gold += 1
                if ig and first_gold_read is None:
                    first_gold_read = actions; seen_gold_read = True
        elif kind == "edit":
            for f in files:
                if not f:
                    continue
                ig = _is_gold(f, gn)
                edits.append((f, ig))
                if first_edit is None:
                    first_edit = actions
                if ig and first_gold_edit is None:
                    first_gold_edit = actions
        elif kind == "run_other":
            run_other += 1

    read_files = [f for f, _ in reads]
    uniq_reads = set(read_files)
    gold_reads = [f for f, ig in reads if ig]
    uniq_gold_read = set(gold_reads)
    edit_files = [f for f, _ in edits]
    uniq_edits = set(edit_files)
    uniq_gold_edit = set(f for f, ig in edits if ig)
    # redundancy / loops
    from collections import Counter
    cmd_counts = Counter(c for c in cmds if c.strip())
    repeated = sum(v - 1 for v in cmd_counts.values() if v > 1)
    max_repeat = max(cmd_counts.values()) if cmd_counts else 0
    read_counts = Counter(read_files)
    redundant_reads = sum(v - 1 for v in read_counts.values() if v > 1)

    productive = len(reads) + len(edits) + searches + len(verifies)
    return {
        # trajectory length / efficiency
        "total_actions": float(actions),
        "action_efficiency": round(productive / actions, 6) if actions else None,
        "scaffold_actions": float(scaffold),
        "think_actions": float(think),
        "run_other": float(run_other),
        # SEARCH stage
        "searches": float(searches),
        "searches_before_gold": float(searches_before_gold),
        # READ stage  (precision = gold reads/total reads ; recall = uniq gold read/total gold)
        "reads": float(len(reads)),
        "unique_files_read": float(len(uniq_reads)),
        "read_precision": round(len(gold_reads) / len(reads), 6) if reads else None,
        "read_recall": round(len(uniq_gold_read) / ngold, 6),
        "nongold_read_ratio": round((len(reads) - len(gold_reads)) / len(reads), 6) if reads else None,
        "redundant_reads": float(redundant_reads),
        "reads_before_gold": float(reads_before_gold),
        # EDIT stage  (precision = gold edits/total edits ; recall = uniq gold edited/total gold)
        "edits": float(len(edits)),
        "files_edited": float(len(uniq_edits)),
        "edit_precision": round(len([1 for _, ig in edits if ig]) / len(edits), 6) if edits else None,
        "edit_recall": round(len(uniq_gold_edit) / ngold, 6),
        "blast_radius": round(len(uniq_edits) / ngold, 6),
        # VELOCITY
        "turns_to_gold_read": float(first_gold_read) if first_gold_read else None,
        "turns_to_gold_edit": float(first_gold_edit) if first_gold_edit else None,
        "first_edit_action": float(first_edit) if first_edit else None,
        # NAVIGATION / WANDERING
        "unique_files_touched": float(len(uniq_reads | uniq_edits)),
        "nav_actions": float(len(reads) + searches),
        "nav_dominance": round((len(reads) + searches) / actions, 6) if actions else None,
        # REDUNDANCY / LOOPS (TRAJEVAL behavioral)
        "repeated_commands": float(repeated),
        "max_command_repeat": float(max_repeat),
        "looped_stuck": 1.0 if max_repeat >= 3 else 0.0,
        # VERIFY
        "verify_actions": float(len(verifies)),
        "first_verify_action": float(first_verify) if first_verify else None,
        "verified_after_edit": 1.0 if (first_verify and first_edit and first_verify > first_edit) else 0.0,
    }

def main():
    DS = {}
    for ln in open(".tmp_run10d/gt-dataset/dataset.jsonl", encoding="utf-8"):
        if ln.strip():
            d = json.loads(ln); DS[d["instance_id"]] = d
    BL = {}
    bp = ".claude/reports/full300_baseline_ohdeepseek_20260531/artifacts/predictions-ohdeepseek-baseline-full300-20260531/output.jsonl"
    for ln in open(bp, encoding="utf-8"):
        if ln.strip():
            try:
                o = json.loads(ln); BL[o.get("instance_id")] = o
            except Exception:
                pass
    rows = []
    for task in [t.strip() for t in open(".tmp_random10_gha.txt") if t.strip()]:
        gj = f".tmp_trajd/{task}.jsonl"
        if not os.path.exists(gj) or task not in BL:
            continue
        gold = gold_files_from_patch(DS.get(task, {}).get("patch", ""))
        if not gold:
            continue
        g = traj_metrics([json.loads(l) for l in open(gj, encoding="utf-8") if l.strip()][0].get("history", []), gold)
        b = traj_metrics(BL[task].get("history", []), gold)
        rows.append((task.split("__")[-1], g, b))

    keys = list(rows[0][1].keys())
    lower_better = {"total_actions", "scaffold_actions", "think_actions", "run_other", "searches",
                    "searches_before_gold", "reads", "unique_files_read", "nongold_read_ratio",
                    "redundant_reads", "reads_before_gold", "files_edited", "blast_radius",
                    "turns_to_gold_read", "turns_to_gold_edit", "first_edit_action",
                    "unique_files_touched", "nav_actions", "nav_dominance", "repeated_commands",
                    "max_command_repeat", "looped_stuck", "edits"}
    print(f"COMPREHENSIVE TRAJECTORY METRICS — GT-on (clean) vs baseline, paired n={len(rows)}\n")
    print(f"{'metric':<24}{'GT-on':>9}{'base':>9}{'Δ':>9}  verdict")
    print("-" * 66)
    for k in keys:
        gv = [r[1][k] for r in rows if r[1][k] is not None]
        bv = [r[2][k] for r in rows if r[2][k] is not None]
        ds = [r[1][k] - r[2][k] for r in rows if r[1][k] is not None and r[2][k] is not None]
        if not gv or not bv:
            continue
        gm, bm = statistics.mean(gv), statistics.mean(bv)
        dm = statistics.mean(ds) if ds else 0.0
        lb = k in lower_better
        if abs(dm) < 1e-9:
            verd = "flat"
        else:
            better = (dm < 0) if lb else (dm > 0)
            wins = sum(1 for d in ds if ((d < 0) == lb) and d != 0)
            verd = f"{'GT' if better else 'base'} ({wins}/{len(ds)})"
        print(f"{k:<24}{gm:>9.3f}{bm:>9.3f}{dm:>+9.3f}  {verd}")
    json.dump([{"task": t, "gt": g, "base": b} for t, g, b in rows],
              open("scripts/metrics/trajectory_metrics.json", "w"), indent=2, default=str)
    print("\nsaved -> scripts/metrics/trajectory_metrics.json")

if __name__ == "__main__":
    main()
