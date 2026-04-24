#!/usr/bin/env python3
"""Inspect no-GT zero-edit tasks: first model actions + termination reason."""
import json, glob, os, sys

root = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt_qwen_nogt"

for td in sorted(glob.glob(root + "/astropy__astropy-*")):
    iid = os.path.basename(td)
    short = iid.split("-")[-1]

    # Exit status
    exit_status = "unknown"
    for ef in (td + "/run_batch_exit_statuses.yaml",):
        if os.path.exists(ef):
            with open(ef) as f:
                c = f.read()
            for tag in ("submitted", "exit_format", "exit_error", "exit_cost"):
                if tag in c:
                    exit_status = tag
                    break

    # Patch
    has_patch = False
    for pp in glob.glob(td + "/preds.json") + glob.glob(td + "/*/preds.json"):
        pd = json.load(open(pp))
        for v in pd.values():
            if v.get("model_patch"):
                has_patch = True
        break

    # Trajectory
    traj_path = None
    for p in glob.glob(td + "/*/*.traj") + glob.glob(td + "/*.traj"):
        traj_path = p
        break

    first_actions = []
    n_steps = 0
    if traj_path and os.path.exists(traj_path):
        traj = json.load(open(traj_path))
        history = traj.get("history", traj.get("trajectory", []))
        n_steps = len(history)
        for step in history:
            role = step.get("role", "")
            if role == "assistant":
                content = str(step.get("content", step.get("action", "")))
                first_actions.append(content)
                if len(first_actions) >= 2:
                    break

    print(f"--- {short} ---")
    print(f"  exit={exit_status} patch={has_patch} steps={n_steps}")

    for i, a in enumerate(first_actions):
        has_fence = "```" in a
        has_submit = "submit" in a.lower() and len(a.strip()) < 50
        has_thinking = "<think" in a.lower()
        truncated = a[:300].replace("\n", " | ")
        print(f"  turn_{i+1}: has_fence={has_fence} submit={has_submit} thinking={has_thinking}")
        print(f"    [{truncated}]")

    if not first_actions:
        print("  NO TRAJECTORY FOUND")
    print()
