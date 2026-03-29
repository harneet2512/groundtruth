#!/usr/bin/env python3
"""GT v13 post-run analysis — per-task attribution with admissibility breakdown.

v13 additions over v12:
  - Admissibility metrics: edges_same_file, edges_import, edges_name_match_rejected
  - Precision gate: name_match_in_output must ALWAYS be False
  - Coverage metrics: tasks_suppressed_by_min_2_gate
  - Import resolution verification: edges_import > 0 per task

Usage:
    python3 analyze_v13.py \
        --baseline-results /path/to/eval_bl/results.json \
        --gt-results /path/to/eval_gt/results.json \
        --gt-logs /path/to/gt_logs/ \
        --gt-trajs /path/to/gt_run/ \
        [--baseline-trajs /path/to/bl_run/]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_results(path: str) -> dict[str, bool]:
    data = json.load(open(path))
    if isinstance(data, dict):
        return {k: v.get("passed", False) if isinstance(v, dict) else bool(v)
                for k, v in data.items()}
    elif isinstance(data, list):
        return {r["instance_id"]: r.get("passed", False) for r in data}
    return {}


def load_evidence_logs(gt_logs_dir: str) -> dict[str, list[dict]]:
    logs: dict[str, list[dict]] = {}
    log_dir = Path(gt_logs_dir)
    if not log_dir.exists():
        return logs
    for f in log_dir.glob("*.evidence.jsonl"):
        instance_id = f.stem.replace(".evidence", "")
        entries = []
        for line in f.read_text().strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if entries:
            logs[instance_id] = entries
    return logs


def load_traj_info(traj_dir: str) -> dict[str, dict]:
    infos: dict[str, dict] = {}
    traj_path = Path(traj_dir)
    if not traj_path.exists():
        return infos
    for d in traj_path.iterdir():
        if d.is_dir():
            traj_file = d / f"{d.name}.traj.json"
            if traj_file.exists():
                try:
                    data = json.loads(traj_file.read_text())
                    info = data.get("info", {}) if isinstance(data, dict) else {}
                    infos[d.name] = info
                except (json.JSONDecodeError, KeyError):
                    pass
    return infos


def analyze_flip(
    instance_id: str,
    direction: str,
    evidence_logs: list[dict],
    traj_info: dict,
) -> dict:
    analysis: dict = {
        "instance_id": instance_id,
        "direction": direction,
        "briefing_shown": traj_info.get("briefing_shown", False),
        "briefing_lines": traj_info.get("briefing_lines", 0),
        "evidence_events": len(evidence_logs),
        "families_shown": [],
        "name_match_in_evidence": False,
        "attribution": "unknown",
        # v13 admissibility
        "edges_same_file": 0,
        "edges_import": 0,
        "edges_name_match_rejected": 0,
        "output_gate_passed": False,
    }

    all_families: set[str] = set()
    any_suppressed = False

    for ev in evidence_logs:
        for fam in ev.get("post_edit_families_shown", []):
            all_families.add(fam)
        if ev.get("post_edit_suppressed"):
            any_suppressed = True
        # v13 admissibility stats
        adm = ev.get("v13_admissibility", {})
        analysis["edges_same_file"] += adm.get("edges_same_file", 0)
        analysis["edges_import"] += adm.get("edges_import", 0)
        analysis["edges_name_match_rejected"] += adm.get("edges_name_match_rejected", 0)
        if adm.get("output_gate_passed"):
            analysis["output_gate_passed"] = True
        if adm.get("name_match_in_output"):
            analysis["name_match_in_evidence"] = True  # SHOULD NEVER HAPPEN in v13

    analysis["families_shown"] = sorted(all_families)
    analysis["any_suppressed"] = any_suppressed

    # Attribution
    if direction == "GAINED":
        if analysis["briefing_shown"] and not evidence_logs:
            analysis["attribution"] = "briefing_only"
        elif analysis["briefing_shown"] and evidence_logs:
            analysis["attribution"] = "briefing_and_evidence"
        elif evidence_logs:
            if "PRECEDENT" in all_families:
                analysis["attribution"] = "evidence_with_precedent"
            else:
                analysis["attribution"] = "evidence_only"
        else:
            analysis["attribution"] = "unknown_no_gt_output"

    elif direction == "LOST":
        if analysis["name_match_in_evidence"]:
            analysis["attribution"] = "GATE_BROKEN_name_match_leaked"  # CRITICAL BUG
        elif analysis["briefing_shown"] and not evidence_logs:
            analysis["attribution"] = "briefing_misleading"
        elif evidence_logs and any_suppressed:
            analysis["attribution"] = "evidence_suppressed_correctly_but_still_lost"
        elif evidence_logs:
            analysis["attribution"] = "evidence_distraction"
        else:
            analysis["attribution"] = "unknown_no_gt_output"

    return analysis


def main():
    parser = argparse.ArgumentParser(description="GT v13 post-run analysis")
    parser.add_argument("--baseline-results", required=True)
    parser.add_argument("--gt-results", required=True)
    parser.add_argument("--gt-logs", default="")
    parser.add_argument("--gt-trajs", default="")
    parser.add_argument("--baseline-trajs", default="")
    args = parser.parse_args()

    bl_results = load_results(args.baseline_results)
    gt_results = load_results(args.gt_results)
    evidence_logs = load_evidence_logs(args.gt_logs) if args.gt_logs else {}
    traj_infos = load_traj_info(args.gt_trajs) if args.gt_trajs else {}

    common = set(bl_results.keys()) & set(gt_results.keys())
    if not common:
        print("ERROR: No common tasks between baseline and GT results")
        sys.exit(1)

    gained, lost, both_pass, both_fail = [], [], [], []
    for iid in sorted(common):
        bl, gt = bl_results[iid], gt_results[iid]
        if not bl and gt:
            gained.append(iid)
        elif bl and not gt:
            lost.append(iid)
        elif bl and gt:
            both_pass.append(iid)
        else:
            both_fail.append(iid)

    bl_total = sum(1 for v in bl_results.values() if v)
    gt_total = sum(1 for iid in common if gt_results.get(iid))
    delta = gt_total - bl_total

    # === SUMMARY ===
    print("=" * 70)
    print("GT v13 ANALYSIS REPORT")
    print("=" * 70)
    print(f"\nTasks evaluated: {len(common)}")
    print(f"Baseline passed: {bl_total}")
    print(f"GT passed:       {gt_total}")
    print(f"Delta:           {delta:+d}")
    print(f"\nGained (BL fail → GT pass): {len(gained)}")
    print(f"Lost   (BL pass → GT fail): {len(lost)}")
    print(f"Both pass:                   {len(both_pass)}")
    print(f"Both fail:                   {len(both_fail)}")

    # === EVIDENCE PRECISION (v13 core metric) ===
    print("\n" + "=" * 70)
    print("EVIDENCE PRECISION (v13)")
    print("=" * 70)

    flip_analyses = []

    for iid in gained:
        ev = evidence_logs.get(iid, [])
        ti = traj_infos.get(iid, {})
        flip_analyses.append(analyze_flip(iid, "GAINED", ev, ti))

    for iid in lost:
        ev = evidence_logs.get(iid, [])
        ti = traj_infos.get(iid, {})
        flip_analyses.append(analyze_flip(iid, "LOST", ev, ti))

    gained_analyses = [a for a in flip_analyses if a["direction"] == "GAINED"]
    lost_analyses = [a for a in flip_analyses if a["direction"] == "LOST"]

    gt_attributed_gains = sum(1 for a in gained_analyses
                              if a["attribution"] not in ("unknown_no_gt_output",))
    gt_attributed_losses = sum(1 for a in lost_analyses
                               if a["attribution"] not in ("unknown_no_gt_output",))
    precision = (gt_attributed_gains / max(gt_attributed_gains + gt_attributed_losses, 1)) * 100

    print(f"\n  GT-attributed gains:  {gt_attributed_gains}")
    print(f"  GT-attributed losses: {gt_attributed_losses}")
    print(f"  Precision:            {precision:.0f}% (TARGET: 100%)")

    gate_broken = any(a.get("name_match_in_evidence") for a in flip_analyses)
    print(f"\n  name_match in ANY output: {'YES — GATE BROKEN' if gate_broken else 'NO ✓'}")

    # === ADMISSIBILITY (v13) ===
    print("\n" + "=" * 70)
    print("ADMISSIBILITY BREAKDOWN (v13)")
    print("=" * 70)

    total_same_file = 0
    total_import = 0
    total_name_match_rejected = 0
    tasks_with_import_edges = 0
    tasks_suppressed_by_gate = 0

    for iid in common:
        task_import = 0
        task_gate_passed = False
        for ev in evidence_logs.get(iid, []):
            adm = ev.get("v13_admissibility", {})
            total_same_file += adm.get("edges_same_file", 0)
            total_import += adm.get("edges_import", 0)
            total_name_match_rejected += adm.get("edges_name_match_rejected", 0)
            task_import += adm.get("edges_import", 0)
            if adm.get("output_gate_passed"):
                task_gate_passed = True
        if task_import > 0:
            tasks_with_import_edges += 1
        if evidence_logs.get(iid) and not task_gate_passed:
            tasks_suppressed_by_gate += 1

    print(f"\n  edges_same_file:          {total_same_file}")
    print(f"  edges_import:             {total_import}")
    print(f"  edges_name_match_rejected: {total_name_match_rejected}")
    print(f"  tasks_with_import_edges:  {tasks_with_import_edges}/{len(common)}")
    print(f"  tasks_suppressed_by_gate: {tasks_suppressed_by_gate}")

    # === PER-TASK ATTRIBUTION ===
    print("\n" + "=" * 70)
    print("PER-TASK ATTRIBUTION")
    print("=" * 70)

    if gained:
        print(f"\n--- GAINED ({len(gained)} tasks) ---")
        for a in gained_analyses:
            print(f"\n  {a['instance_id']}")
            print(f"    Briefing: {a['briefing_shown']} | Evidence events: {a['evidence_events']}")
            print(f"    Families: {', '.join(a['families_shown']) or 'none'}")
            print(f"    Import edges: {a['edges_import']} | Same-file: {a['edges_same_file']}")
            print(f"    Attribution: {a['attribution']}")

    if lost:
        print(f"\n--- LOST ({len(lost)} tasks) ---")
        for a in lost_analyses:
            print(f"\n  {a['instance_id']}")
            print(f"    Briefing: {a['briefing_shown']} | Evidence events: {a['evidence_events']}")
            print(f"    Families: {', '.join(a['families_shown']) or 'none'}")
            print(f"    name_match leaked: {a['name_match_in_evidence']}")
            print(f"    Attribution: {a['attribution']}")

    # === COVERAGE ===
    print("\n" + "=" * 70)
    print("COVERAGE")
    print("=" * 70)

    tasks_with_evidence = sum(1 for iid in common if evidence_logs.get(iid))
    tasks_with_briefing = sum(1 for iid in common
                              if traj_infos.get(iid, {}).get("briefing_shown"))
    tasks_with_gt_output = sum(1 for iid in common
                               if evidence_logs.get(iid) or
                               traj_infos.get(iid, {}).get("briefing_shown"))

    print(f"\n  Tasks with GT output:     {tasks_with_gt_output}/{len(common)} ({100*tasks_with_gt_output/max(len(common),1):.0f}%) — TARGET: ≥40%")
    print(f"  Tasks with evidence:      {tasks_with_evidence}/{len(common)}")
    print(f"  Tasks with briefing:      {tasks_with_briefing}/{len(common)} — TARGET: ≥20%")

    family_counts: dict[str, int] = {}
    for iid in common:
        for ev in evidence_logs.get(iid, []):
            for fam in ev.get("post_edit_families_shown", []):
                family_counts[fam] = family_counts.get(fam, 0) + 1
    if family_counts:
        print("\n  Evidence families shown:")
        for fam, count in sorted(family_counts.items(), key=lambda x: -x[1]):
            print(f"    {fam}: {count}")

    # === DECISION GATE ===
    print("\n" + "=" * 70)
    print("DECISION GATE")
    print("=" * 70)

    if gate_broken:
        print("\n  STOP: name_match leaked through admissibility gate. Fix before continuing.")
    elif delta >= 3:
        print(f"\n  Delta = {delta:+d} >= +3 → PROCEED to full 731-task run")
    elif delta >= 1:
        print(f"\n  Delta = {delta:+d} (1-2) → TUNE and re-iterate on Flash")
    else:
        print(f"\n  Delta = {delta:+d} <= 0 → ANALYZE logs, check edge resolution")

    # Save structured output
    output = {
        "version": "v13",
        "summary": {
            "tasks_evaluated": len(common),
            "baseline_passed": bl_total,
            "gt_passed": gt_total,
            "delta": delta,
            "gained": len(gained),
            "lost": len(lost),
        },
        "precision": {
            "gt_attributed_gains": gt_attributed_gains,
            "gt_attributed_losses": gt_attributed_losses,
            "precision_pct": precision,
            "gate_broken": gate_broken,
        },
        "admissibility": {
            "edges_same_file": total_same_file,
            "edges_import": total_import,
            "edges_name_match_rejected": total_name_match_rejected,
            "tasks_with_import_edges": tasks_with_import_edges,
            "tasks_suppressed_by_gate": tasks_suppressed_by_gate,
        },
        "coverage": {
            "tasks_with_gt_output": tasks_with_gt_output,
            "tasks_with_evidence": tasks_with_evidence,
            "tasks_with_briefing": tasks_with_briefing,
            "family_counts": family_counts,
        },
        "gained_tasks": gained,
        "lost_tasks": lost,
        "flip_analyses": flip_analyses,
    }

    output_path = Path(args.gt_results).parent / "v13_analysis.json"
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Full analysis saved to: {output_path}")


if __name__ == "__main__":
    main()
