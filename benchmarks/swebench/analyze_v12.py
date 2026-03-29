#!/usr/bin/env python3
"""GT v12 post-run analysis — per-task attribution of what made GT win or lose.

Compares baseline vs GT eval results, correlates with evidence logs to determine
exactly which GT output (briefing or post-edit evidence) caused each flip.

Usage:
    python3 analyze_v12.py \
        --baseline-results /path/to/eval_bl/results.json \
        --gt-results /path/to/eval_gt/results.json \
        --gt-logs /path/to/gt_logs/ \
        --gt-trajs /path/to/gt_run/ \
        [--baseline-trajs /path/to/bl_run/]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_results(path: str) -> dict[str, bool]:
    """Load eval results as {instance_id: passed}."""
    data = json.load(open(path))
    if isinstance(data, dict):
        # Format: {"instance_id": {"passed": true/false, ...}}
        return {k: v.get("passed", False) if isinstance(v, dict) else bool(v)
                for k, v in data.items()}
    elif isinstance(data, list):
        # Format: [{"instance_id": "...", "passed": true}, ...]
        return {r["instance_id"]: r.get("passed", False) for r in data}
    return {}


def load_evidence_logs(gt_logs_dir: str) -> dict[str, list[dict]]:
    """Load per-task evidence logs from JSONL files."""
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


def load_hook_logs(gt_logs_dir: str) -> dict[str, list[dict]]:
    """Load per-task hook logs (v10/v11 format)."""
    logs: dict[str, list[dict]] = {}
    log_dir = Path(gt_logs_dir)
    if not log_dir.exists():
        return logs

    for f in log_dir.glob("*.jsonl"):
        if ".evidence." in f.name:
            continue  # skip evidence logs
        instance_id = f.stem
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
    """Load trajectory metadata (extra_info with briefing_shown, etc.)."""
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
    direction: str,  # "GAINED" or "LOST"
    evidence_logs: list[dict],
    traj_info: dict,
) -> dict:
    """Analyze a single flip — what GT output correlates with the direction."""
    analysis: dict = {
        "instance_id": instance_id,
        "direction": direction,
        "briefing_shown": traj_info.get("briefing_shown", False),
        "briefing_lines": traj_info.get("briefing_lines", 0),
        "evidence_events": len(evidence_logs),
        "families_shown": [],
        "resolution_methods": {},
        "name_match_in_evidence": False,
        "attribution": "unknown",
    }

    # Aggregate evidence info
    all_families: set[str] = set()
    all_resolutions: dict[str, int] = {}
    any_suppressed = False

    for ev in evidence_logs:
        for fam in ev.get("post_edit_families_shown", []):
            all_families.add(fam)
        for method, count in ev.get("resolution_methods_in_evidence", {}).items():
            all_resolutions[method] = all_resolutions.get(method, 0) + count
        if ev.get("post_edit_suppressed"):
            any_suppressed = True

    analysis["families_shown"] = sorted(all_families)
    analysis["resolution_methods"] = all_resolutions
    analysis["name_match_in_evidence"] = all_resolutions.get("name_match", 0) > 0
    analysis["any_suppressed"] = any_suppressed

    # Attribution heuristics
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
            analysis["attribution"] = "name_match_false_positive"
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
    parser = argparse.ArgumentParser(description="GT v12 post-run analysis")
    parser.add_argument("--baseline-results", required=True, help="Baseline eval results JSON")
    parser.add_argument("--gt-results", required=True, help="GT eval results JSON")
    parser.add_argument("--gt-logs", default="", help="Directory with GT evidence logs")
    parser.add_argument("--gt-trajs", default="", help="Directory with GT trajectory files")
    parser.add_argument("--baseline-trajs", default="", help="Directory with baseline trajectories")
    args = parser.parse_args()

    # Load results
    bl_results = load_results(args.baseline_results)
    gt_results = load_results(args.gt_results)

    # Load evidence + trajectory data
    evidence_logs = load_evidence_logs(args.gt_logs) if args.gt_logs else {}
    traj_infos = load_traj_info(args.gt_trajs) if args.gt_trajs else {}

    # Find common tasks
    common = set(bl_results.keys()) & set(gt_results.keys())
    if not common:
        print("ERROR: No common tasks between baseline and GT results")
        sys.exit(1)

    # Compute flips
    gained = []  # baseline FAIL → GT PASS
    lost = []    # baseline PASS → GT FAIL
    both_pass = []
    both_fail = []

    for iid in sorted(common):
        bl = bl_results[iid]
        gt = gt_results[iid]
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
    print("GT v12 ANALYSIS REPORT")
    print("=" * 70)
    print(f"\nTasks evaluated: {len(common)}")
    print(f"Baseline passed: {bl_total}")
    print(f"GT passed:       {gt_total}")
    print(f"Delta:           {delta:+d}")
    print(f"\nGained (BL fail → GT pass): {len(gained)}")
    print(f"Lost   (BL pass → GT fail): {len(lost)}")
    print(f"Both pass:                   {len(both_pass)}")
    print(f"Both fail:                   {len(both_fail)}")

    # === WHAT MADE GT WIN/LOSE ===
    print("\n" + "=" * 70)
    print("WHAT MADE GT WIN/LOSE — Per-Task Attribution")
    print("=" * 70)

    flip_analyses = []

    if gained:
        print(f"\n--- GAINED ({len(gained)} tasks) ---")
        for iid in gained:
            ev = evidence_logs.get(iid, [])
            ti = traj_infos.get(iid, {})
            analysis = analyze_flip(iid, "GAINED", ev, ti)
            flip_analyses.append(analysis)

            print(f"\n  {iid}")
            print(f"    Briefing shown: {analysis['briefing_shown']}")
            print(f"    Evidence events: {analysis['evidence_events']}")
            print(f"    Families: {', '.join(analysis['families_shown']) or 'none'}")
            print(f"    Resolution methods: {analysis['resolution_methods']}")
            print(f"    Attribution: {analysis['attribution']}")

    if lost:
        print(f"\n--- LOST ({len(lost)} tasks) ---")
        for iid in lost:
            ev = evidence_logs.get(iid, [])
            ti = traj_infos.get(iid, {})
            analysis = analyze_flip(iid, "LOST", ev, ti)
            flip_analyses.append(analysis)

            print(f"\n  {iid}")
            print(f"    Briefing shown: {analysis['briefing_shown']}")
            print(f"    Evidence events: {analysis['evidence_events']}")
            print(f"    Families: {', '.join(analysis['families_shown']) or 'none'}")
            print(f"    Name-match in evidence: {analysis['name_match_in_evidence']}")
            print(f"    Attribution: {analysis['attribution']}")

    # === SUMMARY COUNTERS ===
    print("\n" + "=" * 70)
    print("SUMMARY COUNTERS")
    print("=" * 70)

    gained_analyses = [a for a in flip_analyses if a["direction"] == "GAINED"]
    lost_analyses = [a for a in flip_analyses if a["direction"] == "LOST"]

    briefing_helped = sum(1 for a in gained_analyses if "briefing" in a["attribution"])
    evidence_helped = sum(1 for a in gained_analyses if "evidence" in a["attribution"])
    precedent_helped = sum(1 for a in gained_analyses if "precedent" in a["attribution"])
    evidence_hurt = sum(1 for a in lost_analyses if a["name_match_in_evidence"])
    briefing_hurt = sum(1 for a in lost_analyses if "briefing" in a["attribution"])

    print(f"\n  briefing_helped_count:   {briefing_helped}")
    print(f"  evidence_helped_count:   {evidence_helped}")
    print(f"  precedent_helped_count:  {precedent_helped}")
    print(f"  evidence_hurt_count:     {evidence_hurt} (name_match false positives)")
    print(f"  briefing_hurt_count:     {briefing_hurt}")

    # === EVIDENCE COVERAGE ===
    print("\n" + "=" * 70)
    print("EVIDENCE COVERAGE")
    print("=" * 70)

    tasks_with_evidence = sum(1 for iid in common if evidence_logs.get(iid))
    tasks_with_briefing = sum(1 for iid in common if traj_infos.get(iid, {}).get("briefing_shown"))

    print(f"\n  Tasks with evidence logs:    {tasks_with_evidence}/{len(common)}")
    print(f"  Tasks with briefing shown:   {tasks_with_briefing}/{len(common)}")

    # Family distribution across all evidence
    family_counts: dict[str, int] = {}
    resolution_totals: dict[str, int] = {}
    for iid in common:
        for ev in evidence_logs.get(iid, []):
            for fam in ev.get("post_edit_families_shown", []):
                family_counts[fam] = family_counts.get(fam, 0) + 1
            for method, count in ev.get("resolution_methods_in_evidence", {}).items():
                resolution_totals[method] = resolution_totals.get(method, 0) + count

    if family_counts:
        print("\n  Evidence families shown:")
        for fam, count in sorted(family_counts.items(), key=lambda x: -x[1]):
            print(f"    {fam}: {count}")

    if resolution_totals:
        print("\n  Resolution methods in evidence:")
        for method, count in sorted(resolution_totals.items(), key=lambda x: -x[1]):
            print(f"    {method}: {count}")

    # === DECISION GATE ===
    print("\n" + "=" * 70)
    print("DECISION GATE")
    print("=" * 70)

    if delta >= 3:
        print(f"\n  Delta = {delta:+d} >= +3 → PROCEED to full 731-task run")
    elif delta >= 1:
        print(f"\n  Delta = {delta:+d} (1-2) → TUNE and re-iterate on Flash")
    else:
        print(f"\n  Delta = {delta:+d} <= 0 → ANALYZE logs, check fallback chain")

    # Save structured output
    output = {
        "summary": {
            "tasks_evaluated": len(common),
            "baseline_passed": bl_total,
            "gt_passed": gt_total,
            "delta": delta,
            "gained": len(gained),
            "lost": len(lost),
        },
        "gained_tasks": gained,
        "lost_tasks": lost,
        "flip_analyses": flip_analyses,
        "counters": {
            "briefing_helped": briefing_helped,
            "evidence_helped": evidence_helped,
            "precedent_helped": precedent_helped,
            "evidence_hurt": evidence_hurt,
            "briefing_hurt": briefing_hurt,
        },
        "coverage": {
            "tasks_with_evidence": tasks_with_evidence,
            "tasks_with_briefing": tasks_with_briefing,
            "family_counts": family_counts,
            "resolution_totals": resolution_totals,
        },
    }

    # Write to file next to GT results
    output_path = Path(args.gt_results).parent / "v12_analysis.json"
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Full analysis saved to: {output_path}")


if __name__ == "__main__":
    main()
