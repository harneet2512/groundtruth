"""Curation-map smoke metrics — does the curation map make the agent FASTER?

THE hypothesis (curation-speed, not Hit@1): the v22 brief's <gt-graph-map> gets
the agent to the right area in fewer turns and with less wandering, freeing
budget to write the fix. Faster first; flips downstream.

Everything is read from the AGENT's output.jsonl history — NOT telemetry.
"Fired != delivered": we confirm the map was actually IN the agent's context,
then measure behavior, per CLAUDE.md.

Arms (change one variable — isolate the map):
  A0 = baseline (no GT)
  A1 = GT brief WITHOUT <gt-graph-map>  (localization seed only; GT_CURATION_MAP=0)
  A2 = GT brief WITH <gt-graph-map>
Primary comparison is A2 vs A1 (isolates the map). A1/A2 vs A0 = GT overall.

Metrics (per task, per arm):
  Delivery:   map_delivered (bool), map_fact_lines, map_unverified_lines
  Speed (lower better w/ map):
              turns_to_first_source_edit, files_wandered_before_first_edit,
              time_to_first_gold  (reused from compute_localization_metrics)
  Budget:     total_actions  (must NOT increase — no latency tax)
  Outcome:    resolved (report.json); flips (A0 FAIL -> arm PASS), regressions (A0 PASS -> arm FAIL)

Stats: paired Wilcoxon signed-rank + bootstrap CI on per-task deltas over the
SHARED task set (per feedback_paired_test). Falsifier, not certifier — this
catches harm/regression and gives a directional signal; it does NOT certify
generalization (that needs cross-repo mechanism ablation).

Usage:
  python curation_smoke_metrics.py --arm A0:runs/a0 --arm A1:runs/a1 --arm A2:runs/a2 \\
      [--baseline A0] [--dataset princeton-nlp/SWE-bench_Lite --split test]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compute_localization_metrics as clm  # reuse the committed primitives

_VIEW_ACTIONS = {"read", "view", "open", "cat"}
_TEST_PATH_HINTS = ("test_", "/tests/", "tests/", "_test.", "conftest")
_MAP_TAG = "<gt-graph-map>"


def _is_test_path(p: str) -> bool:
    pl = p.lower()
    return any(h in pl for h in _TEST_PATH_HINTS)


def detect_curation_map(rec: dict[str, Any]) -> tuple[bool, int, int]:
    """Did the <gt-graph-map> actually reach the agent's context, and with what
    content? Scans the full record text (the brief lands in the first-turn
    instruction and/or history observations). 'fired != delivered'.

    Returns (delivered, fact_lines, unverified_lines). A FACT line is a map
    edge with no '(unverified)' marker; unverified lines carry it.
    """
    blob = json.dumps(rec, ensure_ascii=False)
    if _MAP_TAG not in blob:
        return (False, 0, 0)
    # Count fact vs unverified edge tokens inside the map. Each rendered edge is
    # "name (file)" optionally "(unverified)". Count the markers conservatively.
    seg = blob.split(_MAP_TAG, 1)[1].split("</gt-graph-map>", 1)[0]
    unverified = seg.count("(unverified)")
    # "calls:"/"called by:" lines carry comma-separated edges; approximate fact
    # count as total " (" file-anchor occurrences minus unverified ones.
    anchors = seg.count(" (")
    facts = max(0, anchors - unverified)
    return (True, facts, unverified)


def view_steps(rec: dict[str, Any]) -> list[tuple[int, str]]:
    """(step_idx, normalized_path) for read/view-class actions."""
    out: list[tuple[int, str]] = []
    history = rec.get("history") or rec.get("test_result", {}).get("history") or []
    if not isinstance(history, list):
        return out
    for idx, ev in enumerate(history):
        if not isinstance(ev, dict):
            continue
        atype = (ev.get("action") or "").lower()
        args = ev.get("args") or {}
        if atype not in _VIEW_ACTIONS and isinstance(args, dict):
            atype = (args.get("action") or "").lower()
        if atype not in _VIEW_ACTIONS:
            continue
        path = ""
        if isinstance(args, dict):
            path = args.get("path") or args.get("file") or args.get("file_path") or ""
        if not path:
            path = ev.get("path") or ev.get("file") or ""
        if path:
            out.append((idx, str(path).replace("\\", "/").lstrip("./")))
    return out


def first_source_edit_idx(steps: list[tuple[int, str, str]]) -> int | None:
    """Step idx of the first edit to a NON-test source file."""
    for idx, _atype, path in steps:
        if not _is_test_path(path):
            return idx
    return None


def files_wandered(views: list[tuple[int, str]], first_edit_idx: int | None) -> int:
    """Unique non-test files VIEWED before the first source edit (a wandering
    proxy — fewer is better when the map curates the area)."""
    cutoff = first_edit_idx if first_edit_idx is not None else 10**9
    return len({p for i, p in views if i < cutoff and not _is_test_path(p)})


def total_actions(rec: dict[str, Any]) -> int:
    history = rec.get("history") or rec.get("test_result", {}).get("history") or []
    return len(history) if isinstance(history, list) else 0


def load_resolved_ids(run_dir: str) -> set[str]:
    """Resolved instance ids from a SWE-bench report.json (tolerant of shapes)."""
    for name in ("report.json", "eval_report.json", "results.json"):
        p = os.path.join(run_dir, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("resolved_ids", "resolved", "resolved_instances"):
            v = d.get(key) if isinstance(d, dict) else None
            if isinstance(v, list):
                return {str(x) for x in v}
        if isinstance(d, dict):  # per-instance {id: {"resolved": bool}}
            got = {k for k, val in d.items() if isinstance(val, dict) and val.get("resolved")}
            if got:
                return got
    return set()


def per_task_metrics(rec: dict[str, Any], gold: set[str], resolved: bool) -> dict[str, Any]:
    steps = clm.history_edit_steps(rec)
    views = view_steps(rec)
    delivered, facts, unver = detect_curation_map(rec)
    fse = first_source_edit_idx(steps)
    return {
        "map_delivered": delivered,
        "map_fact_lines": facts,
        "map_unverified_lines": unver,
        "turns_to_first_source_edit": fse,
        "files_wandered": files_wandered(views, fse),
        "time_to_first_gold": clm.time_to_first_gold(steps, gold),
        "total_actions": total_actions(rec),
        "resolved": resolved,
        "failure_class": clm.classify_failure(rec),
    }


def _wilcoxon_and_ci(deltas: list[float]) -> dict[str, Any]:
    """Paired signed-rank p-value + 95% bootstrap CI on the mean delta.
    deltas = per-task (arm - baseline); negative = arm is faster/fewer."""
    vals = [d for d in deltas if d is not None]
    n = len(vals)
    out: dict[str, Any] = {"n": n, "mean_delta": None, "ci95": None, "wilcoxon_p": None}
    if n == 0:
        return out
    out["mean_delta"] = round(sum(vals) / n, 4)
    try:
        import numpy as np
        rng = np.random.default_rng(12345)  # fixed seed: deterministic CI
        arr = np.array(vals, dtype=float)
        boots = [float(rng.choice(arr, size=n, replace=True).mean()) for _ in range(2000)]
        out["ci95"] = [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]
    except Exception:
        out["ci95"] = "numpy_unavailable"
    try:
        from scipy.stats import wilcoxon
        nz = [v for v in vals if v != 0]
        if nz:
            out["wilcoxon_p"] = round(float(wilcoxon(nz).pvalue), 5)
    except Exception:
        out["wilcoxon_p"] = "scipy_unavailable"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Curation-map smoke metrics")
    ap.add_argument("--arm", action="append", required=True, help="NAME:run_dir (output.jsonl + report.json)")
    ap.add_argument("--baseline", default="A0", help="arm name used as flip/regression baseline")
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--split", default="test")
    args = ap.parse_args()

    arms: dict[str, str] = {}
    for spec in args.arm:
        name, _, d = spec.partition(":")
        arms[name] = d

    # load per-arm output.jsonl + resolved + gold
    arm_recs: dict[str, dict[str, dict]] = {}
    arm_resolved: dict[str, set[str]] = {}
    all_ids: set[str] = set()
    for name, d in arms.items():
        oj = os.path.join(d, "output.jsonl")
        recs = clm.load_output_jsonl(oj) if os.path.exists(oj) else {}
        arm_recs[name] = recs
        arm_resolved[name] = load_resolved_ids(d)
        all_ids |= set(recs.keys())

    gold = clm.load_gold_patches(args.dataset, args.split, all_ids)
    gold_sets = {tid: clm.gold_files(gold.get(tid, "")) for tid in all_ids}

    # shared task set = ids present in every arm (paired)
    shared = set.intersection(*[set(r.keys()) for r in arm_recs.values()]) if arm_recs else set()

    per_arm: dict[str, dict[str, dict]] = {}
    for name, recs in arm_recs.items():
        per_arm[name] = {}
        for tid, rec in recs.items():
            per_arm[name][tid] = per_task_metrics(rec, gold_sets.get(tid, set()), tid in arm_resolved[name])

    def agg(name: str, key: str) -> dict[str, Any]:
        vals = [per_arm[name][t][key] for t in shared if isinstance(per_arm[name][t].get(key), (int, float))]
        return clm.aggregate([float(v) for v in vals])

    report: dict[str, Any] = {"shared_n": len(shared), "arms": list(arms), "baseline": args.baseline, "per_arm": {}, "paired": {}}
    for name in arms:
        delivered = sum(1 for t in shared if per_arm[name][t]["map_delivered"])
        report["per_arm"][name] = {
            "map_delivered_rate": round(delivered / len(shared), 3) if shared else 0.0,
            "resolved": sum(1 for t in shared if per_arm[name][t]["resolved"]),
            "turns_to_first_source_edit": agg(name, "turns_to_first_source_edit"),
            "files_wandered": agg(name, "files_wandered"),
            "time_to_first_gold": agg(name, "time_to_first_gold"),
            "total_actions": agg(name, "total_actions"),
        }

    # paired deltas vs every other arm (focus: A2 vs A1, then vs baseline)
    speed_keys = ["turns_to_first_source_edit", "files_wandered", "time_to_first_gold", "total_actions"]
    base = args.baseline
    for name in arms:
        if name == base:
            continue
        pair: dict[str, Any] = {}
        for key in speed_keys:
            deltas = []
            for t in shared:
                a, b = per_arm[name][t].get(key), per_arm[base][t].get(key)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    deltas.append(float(a) - float(b))
            pair[key] = _wilcoxon_and_ci(deltas)
        # flips / regressions vs baseline
        flips = [t for t in shared if per_arm[name][t]["resolved"] and not per_arm[base][t]["resolved"]]
        regr = [t for t in shared if not per_arm[name][t]["resolved"] and per_arm[base][t]["resolved"]]
        pair["flips"] = flips
        pair["regressions"] = regr
        report["paired"][f"{name}_vs_{base}"] = pair

    # "went as planned" verdict (only meaningful when A1 & A2 present)
    verdict: list[str] = []
    if "A2" in arms and "A1" in arms:
        a2 = report["per_arm"]["A2"]
        p = report["paired"].get("A2_vs_A1", {})
        verdict.append(f"map_delivered_rate(A2)={a2['map_delivered_rate']} (want high — proves fired==delivered)")
        for k in ("turns_to_first_source_edit", "files_wandered"):
            md = p.get(k, {}).get("mean_delta")
            ci = p.get(k, {}).get("ci95")
            verdict.append(f"{k}: A2-A1 mean_delta={md} ci95={ci} (want NEGATIVE = faster/less wandering)")
        ta = p.get("total_actions", {}).get("mean_delta")
        verdict.append(f"total_actions: A2-A1 mean_delta={ta} (want ~0 / not positive — no latency tax)")
    report["verdict"] = verdict

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
