#!/usr/bin/env python3
"""Deep trajectory analysis for v2 ablation."""
import json, glob, os

BASE = "/tmp/qwen_fc_ablation/v2_parallel_1777333524"

# Load resolved maps
resolved_map = {}
for pattern in [f"{BASE}/*/*.v2_*.json", f"/home/Lenovo/*.v2_*.json", f"/tmp/SWE-agent/*.v2_*.json"]:
    for f in glob.glob(pattern):
        arm = os.path.basename(f).split(".")[0]
        d = json.load(open(f))
        resolved_map[arm] = set(d.get("resolved_ids", []))

tasks = [
    "astropy__astropy-12907", "astropy__astropy-13033", "astropy__astropy-13236",
    "astropy__astropy-13398", "astropy__astropy-13453", "astropy__astropy-13579",
    "astropy__astropy-13977", "astropy__astropy-14096", "astropy__astropy-14182",
    "astropy__astropy-14309",
]

print("=" * 90)
print("V2 ABLATION — DEEP TRAJECTORY ANALYSIS")
print("=" * 90)

# Collect per-arm, per-task data
arm_data = {}
for arm in ["A", "B", "C", "D", "E"]:
    adir = f"{BASE}/{arm}"
    if not os.path.isdir(adir):
        continue
    task_details = {}
    for tf in sorted(glob.glob(f"{adir}/astropy*/*.traj")):
        t = json.load(open(tf))
        iid = os.path.basename(os.path.dirname(tf))
        trajectory = t.get("trajectory", [])
        info = t.get("info", {})
        patch = info.get("submission", "") or ""
        steps = len(trajectory)

        fc_retries = 0
        gt_evidence_steps = 0
        gt_evidence_texts = []
        obs_sizes = []
        for i, step in enumerate(trajectory):
            obs = step.get("observation", "") or ""
            obs_sizes.append(len(obs))
            if "FunctionCallingFormatError" in obs or "did not use any tool calls" in obs:
                fc_retries += 1
            state = step.get("state", {})
            if isinstance(state, dict):
                ev = state.get("gt_evidence", "")
                if ev:
                    gt_evidence_steps += 1
                    gt_evidence_texts.append((i, ev[:150]))

        # Repeated actions
        actions = [s.get("action", "") for s in trajectory]
        repeated = sum(1 for i in range(1, len(actions)) if actions[i] and actions[i] == actions[i-1])

        task_details[iid] = {
            "steps": steps,
            "patch": bool(patch.strip()),
            "patch_bytes": len(patch),
            "resolved": iid in resolved_map.get(arm, set()),
            "fc_retries": fc_retries,
            "gt_evidence_steps": gt_evidence_steps,
            "gt_evidence_texts": gt_evidence_texts,
            "avg_obs": sum(obs_sizes) / max(len(obs_sizes), 1),
            "max_obs": max(obs_sizes) if obs_sizes else 0,
            "repeated_actions": repeated,
            "exit_status": info.get("exit_status", "unknown"),
        }
    arm_data[arm] = task_details

# Per-arm summary
print("\n## Per-Arm Summary\n")
print(f"{'Arm':<6} {'Res':>4} {'Patch':>6} {'AvgStp':>7} {'MaxStp':>7} {'FC':>4} {'GTev':>5} {'AvgObs':>8} {'Repeat':>7}")
print("-" * 60)
for arm in ["A", "B", "C", "D", "E"]:
    td = arm_data.get(arm, {})
    res = sum(1 for v in td.values() if v["resolved"])
    pat = sum(1 for v in td.values() if v["patch"])
    steps = [v["steps"] for v in td.values()]
    fc = sum(v["fc_retries"] for v in td.values())
    gt = sum(v["gt_evidence_steps"] for v in td.values())
    obs = sum(v["avg_obs"] for v in td.values()) / max(len(td), 1)
    rep = sum(v["repeated_actions"] for v in td.values())
    print(f"{arm:<6} {res:>4} {pat:>6} {sum(steps)/max(len(steps),1):>7.1f} {max(steps) if steps else 0:>7} {fc:>4} {gt:>5} {obs:>8.0f} {rep:>7}")

# Task-level matrix
print("\n## Task-Level Matrix\n")
print(f"{'Task':<10} {'A':>14} {'B':>14} {'C':>14} {'D':>14} {'E':>14}")
print("-" * 82)
for task in tasks:
    short = task.split("-")[-1]
    row = f"{short:<10}"
    for arm in ["A", "B", "C", "D", "E"]:
        d = arm_data.get(arm, {}).get(task, {})
        if not d:
            row += f" {'?':>14}"
            continue
        tag = "RESOLVED" if d["resolved"] else ("patch" if d["patch"] else "no_patch")
        s = d["steps"]
        ev = d["gt_evidence_steps"]
        cell = f"{tag}({s})"
        if ev > 0:
            cell += f"+{ev}ev"
        row += f" {cell:>14}"
    print(row)

# Evidence delivery analysis
print("\n## Evidence Delivery Analysis\n")
for arm in ["B", "C", "D", "E"]:
    td = arm_data.get(arm, {})
    total_ev = sum(v["gt_evidence_steps"] for v in td.values())
    tasks_with_ev = sum(1 for v in td.values() if v["gt_evidence_steps"] > 0)
    print(f"{arm}: {total_ev} evidence injections across {tasks_with_ev}/10 tasks")
    for task in tasks:
        d = td.get(task, {})
        if d and d["gt_evidence_texts"]:
            for step_i, text in d["gt_evidence_texts"][:3]:
                print(f"  {task.split('-')[-1]} step {step_i}: {text}")

# Scaffold safety — step count comparison
print("\n## Scaffold Safety — Step Count Deltas\n")
a_steps = {t: arm_data["A"][t]["steps"] for t in tasks if t in arm_data.get("A", {})}
for arm in ["B", "C", "D", "E"]:
    td = arm_data.get(arm, {})
    deltas = []
    for task in tasks:
        if task in td and task in a_steps:
            deltas.append(td[task]["steps"] - a_steps[task])
    avg_delta = sum(deltas) / max(len(deltas), 1)
    max_delta = max(deltas) if deltas else 0
    min_delta = min(deltas) if deltas else 0
    print(f"  {arm} vs A: avg={avg_delta:+.1f} min={min_delta:+d} max={max_delta:+d}")

# Flip analysis — which tasks change between arms
print("\n## Task Flips (resolved status differs from A)\n")
for arm in ["B", "C", "D", "E"]:
    td = arm_data.get(arm, {})
    gains = []
    losses = []
    for task in tasks:
        a_res = arm_data.get("A", {}).get(task, {}).get("resolved", False)
        b_res = td.get(task, {}).get("resolved", False)
        if b_res and not a_res:
            gains.append(task.split("-")[-1])
        elif a_res and not b_res:
            losses.append(task.split("-")[-1])
    print(f"  {arm}: gains={gains or 'none'} losses={losses or 'none'}")

# 14182 analysis — the task that flips
print("\n## Task 14182 (flips between arms)\n")
for arm in ["A", "B", "C", "D", "E"]:
    d = arm_data.get(arm, {}).get("astropy__astropy-14182", {})
    if d:
        ev_str = f" evidence={d['gt_evidence_steps']}" if d["gt_evidence_steps"] > 0 else ""
        print(f"  {arm}: steps={d['steps']} patch={'YES' if d['patch'] else 'no'}({d['patch_bytes']}b) resolved={d['resolved']} fc={d['fc_retries']}{ev_str}")

# 14096 analysis — gained by D and E
print("\n## Task 14096 (gained by D and E)\n")
for arm in ["A", "B", "C", "D", "E"]:
    d = arm_data.get(arm, {}).get("astropy__astropy-14096", {})
    if d:
        ev_str = f" evidence={d['gt_evidence_steps']}" if d["gt_evidence_steps"] > 0 else ""
        print(f"  {arm}: steps={d['steps']} patch={'YES' if d['patch'] else 'no'}({d['patch_bytes']}b) resolved={d['resolved']} fc={d['fc_retries']}{ev_str}")

# 13236 analysis — gained by C
print("\n## Task 13236 (gained by C only)\n")
for arm in ["A", "B", "C", "D", "E"]:
    d = arm_data.get(arm, {}).get("astropy__astropy-13236", {})
    if d:
        ev_str = f" evidence={d['gt_evidence_steps']}" if d["gt_evidence_steps"] > 0 else ""
        print(f"  {arm}: steps={d['steps']} patch={'YES' if d['patch'] else 'no'}({d['patch_bytes']}b) resolved={d['resolved']} fc={d['fc_retries']}{ev_str}")
