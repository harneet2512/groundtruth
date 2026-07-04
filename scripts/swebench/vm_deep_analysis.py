#!/usr/bin/env python3
"""Deep trajectory analysis for Qwen FC ablation."""
import json, glob, os, sys

RUN_A = "/tmp/qwen_fc_ablation/run_1777313070/runs"
# D and E are in a different run dir
RUN_DE = None
for d in sorted(glob.glob("/tmp/qwen_fc_ablation/run_*/runs/D")):
    RUN_DE = os.path.dirname(d)
    break

arm_dirs = {
    "A": f"{RUN_A}/A",
    "B": f"{RUN_A}/B",
    "C": f"{RUN_A}/C",
    "D": f"{RUN_DE}/D" if RUN_DE else None,
    "E": f"{RUN_DE}/E" if RUN_DE else None,
}

resolved_map = {}
for f in glob.glob("/home/Lenovo/*.ablation_*.json"):
    arm = os.path.basename(f).split(".")[0]
    d = json.load(open(f))
    resolved_map[arm] = set(d.get("resolved_ids", []))

tasks = [
    "astropy__astropy-12907", "astropy__astropy-13033", "astropy__astropy-13236",
    "astropy__astropy-13398", "astropy__astropy-13453", "astropy__astropy-13579",
    "astropy__astropy-13977", "astropy__astropy-14096", "astropy__astropy-14182",
    "astropy__astropy-14309",
]

print("=" * 80)
print("QWEN FC ABLATION — DEEP TRAJECTORY ANALYSIS")
print("=" * 80)

# Per-arm summary
print("\n## Per-Arm Summary\n")
print(f"{'Arm':<20} {'Resolved':>8} {'Patched':>8} {'Avg Steps':>10} {'Max Steps':>10} {'FC Retries':>10} {'Avg Obs':>10}")
print("-" * 80)

arm_data = {}
for arm in ["A", "B", "C", "D", "E"]:
    adir = arm_dirs.get(arm)
    if not adir or not os.path.isdir(adir):
        print(f"{arm:<20} {'MISSING':>8}")
        continue

    trajs = sorted(glob.glob(f"{adir}/astropy*/*.traj"))
    steps_list = []
    patch_count = 0
    fc_retries_total = 0
    obs_sizes = []
    task_details = {}

    for tf in trajs:
        t = json.load(open(tf))
        iid = os.path.basename(os.path.dirname(tf))
        trajectory = t.get("trajectory", [])
        info = t.get("info", {})
        patch = info.get("submission", "") or ""
        steps = len(trajectory)
        has_patch = bool(patch.strip())

        fc_retries = 0
        xml_count = 0
        gt_evidence_count = 0
        for step in trajectory:
            obs = step.get("observation", "") or ""
            if "FunctionCallingFormatError" in obs or "did not use any tool calls" in obs:
                fc_retries += 1
            if "<gt-intervention" in obs or "<gt-evidence>" in obs:
                xml_count += 1
            if "gt_evidence" in obs:
                gt_evidence_count += 1
            obs_sizes.append(len(obs))

        steps_list.append(steps)
        if has_patch:
            patch_count += 1
        fc_retries_total += fc_retries

        resolved = iid in resolved_map.get(arm, set())
        task_details[iid] = {
            "steps": steps, "patch": has_patch, "patch_bytes": len(patch),
            "resolved": resolved, "fc_retries": fc_retries,
            "xml_count": xml_count, "gt_evidence": gt_evidence_count,
        }

    avg_steps = sum(steps_list) / max(len(steps_list), 1)
    max_steps = max(steps_list) if steps_list else 0
    avg_obs = sum(obs_sizes) / max(len(obs_sizes), 1)
    resolved_count = len(resolved_map.get(arm, set()))

    arm_data[arm] = task_details
    print(f"{arm:<20} {resolved_count:>8} {patch_count:>8} {avg_steps:>10.1f} {max_steps:>10} {fc_retries_total:>10} {avg_obs:>10.0f}")

# Task-level matrix
print("\n## Task-Level Matrix\n")
print(f"{'Task':<30}", end="")
for arm in ["A", "B", "C", "D", "E"]:
    print(f" {arm:>12}", end="")
print()
print("-" * 90)

for task in tasks:
    short = task.split("-")[-1]
    print(f"{short:<30}", end="")
    for arm in ["A", "B", "C", "D", "E"]:
        details = arm_data.get(arm, {}).get(task, {})
        if not details:
            print(f" {'?':>12}", end="")
            continue
        r = "RESOLVED" if details["resolved"] else ("patch" if details["patch"] else "no_patch")
        s = details["steps"]
        print(f" {r}({s})", end="")
        # Pad to 12 chars
        printed = len(f" {r}({s})")
        if printed < 13:
            print(" " * (13 - printed), end="")
    print()

# Scaffold safety analysis
print("\n## Scaffold Safety Analysis\n")

for arm_pair, desc in [("A", "B"), ("B", "C"), ("C", "D"), ("C", "E")]:
    a_data = arm_data.get(arm_pair, {})
    b_data = arm_data.get(desc, {})
    if not a_data or not b_data:
        continue

    step_diffs = []
    fc_diffs = []
    behavior_changes = []
    for task in tasks:
        a = a_data.get(task, {})
        b = b_data.get(task, {})
        if a and b:
            step_diffs.append(b.get("steps", 0) - a.get("steps", 0))
            fc_diffs.append(b.get("fc_retries", 0) - a.get("fc_retries", 0))
            if a.get("resolved") != b.get("resolved"):
                who = desc if b.get("resolved") else arm_pair
                behavior_changes.append(f"{task.split('-')[-1]}: {who} resolves")

    avg_step_delta = sum(step_diffs) / max(len(step_diffs), 1)
    avg_fc_delta = sum(fc_diffs) / max(len(fc_diffs), 1)

    print(f"### {arm_pair} vs {desc}")
    print(f"  Avg step delta: {avg_step_delta:+.1f}")
    print(f"  Avg FC retry delta: {avg_fc_delta:+.1f}")
    if behavior_changes:
        for bc in behavior_changes:
            print(f"  Resolution change: {bc}")
    else:
        print(f"  No resolution changes")
    print()

# D's +1 analysis
print("## D's +1 Task: 14096\n")
for arm in ["A", "B", "C", "D", "E"]:
    d = arm_data.get(arm, {}).get("astropy__astropy-14096", {})
    if d:
        print(f"  {arm}: steps={d['steps']} patch={'YES' if d['patch'] else 'no'}({d['patch_bytes']}b) resolved={d['resolved']} fc_retries={d['fc_retries']} gt_evidence={d['gt_evidence']}")
