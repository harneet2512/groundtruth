#!/usr/bin/env python3
"""Analyze resolved tasks — what made the agent succeed, what would help faster."""
import json
import sys
from datasets import load_dataset

PREDS_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/Lenovo/results/v8_mini_20260328_101151/baseline/preds.json"
EVAL_PATH = sys.argv[2] if len(sys.argv) > 2 else "/home/Lenovo/openai__qwen3-coder.v9_50_bl.json"
TRAJ_DIR = sys.argv[3] if len(sys.argv) > 3 else "/home/Lenovo/results/v8_mini_20260328_101151/baseline"

ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
gt_patches = {d["instance_id"]: d["patch"] for d in ds}
gt_problems = {d["instance_id"]: d["problem_statement"] for d in ds}

bl_preds = json.load(open(PREDS_PATH))
bl_eval = json.load(open(EVAL_PATH))
resolved = sorted(bl_eval.get("resolved_ids", []))

print(f"Analyzing {len(resolved)} RESOLVED tasks")
print("=" * 70)

for iid in resolved:
    gt_patch = gt_patches.get(iid, "")
    pred = bl_preds.get(iid, {})
    agent_patch = pred.get("model_patch", "") if isinstance(pred, dict) else ""

    # Parse GT patch
    gt_files = set()
    gt_added = 0
    gt_removed = 0
    for line in gt_patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                gt_files.add(parts[2].lstrip("a/"))
        if line.startswith("+") and not line.startswith("+++"):
            gt_added += 1
        if line.startswith("-") and not line.startswith("---"):
            gt_removed += 1

    # Count agent turns from trajectory
    turns = 0
    traj_path = f"{TRAJ_DIR}/{iid}/{iid}.traj.json"
    try:
        t = json.load(open(traj_path))
        turns = len(t.get("history", t.get("messages", [])))
    except Exception:
        pass

    # Classify fix complexity
    total_lines = gt_added + gt_removed
    if total_lines <= 3:
        complexity = "TRIVIAL (1-3 lines)"
    elif total_lines <= 10:
        complexity = "SIMPLE (4-10 lines)"
    elif total_lines <= 30:
        complexity = "MODERATE (11-30 lines)"
    else:
        complexity = "COMPLEX (30+ lines)"

    multi_file = len(gt_files) > 1

    print(f"\n--- {iid} ---")
    print(f"  Fix: {sorted(gt_files)} (+{gt_added}/-{gt_removed} = {total_lines} lines) {complexity}")
    print(f"  Multi-file: {multi_file}")
    print(f"  Agent turns: {turns}")

    # What cross-file info would have helped the agent get here FASTER?
    if turns > 80:
        print(f"  >> SLOW: {turns} turns. Agent explored extensively before fixing.")
        print(f"  >> GT could help: show WHERE to edit upfront (localization)")
    elif turns > 40:
        print(f"  >> MODERATE: {turns} turns. Some exploration before fix.")
    else:
        print(f"  >> FAST: {turns} turns. Agent found fix quickly.")

    # Show the GT fix pattern
    hunk_count = 0
    for line in gt_patch.split("\n"):
        if line.startswith("@@"):
            hunk_count += 1
    print(f"  Hunks: {hunk_count}")

# Summary statistics
print("\n" + "=" * 70)
print("RESOLVED TASK SUMMARY:")
print()

trivial = simple = moderate = complex_count = 0
multi = 0
fast = moderate_speed = slow = 0
total_turns = 0

for iid in resolved:
    gt_patch = gt_patches.get(iid, "")
    gt_files = set()
    total_lines = 0
    for line in gt_patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                gt_files.add(parts[2].lstrip("a/"))
        if line.startswith("+") and not line.startswith("+++"):
            total_lines += 1
        if line.startswith("-") and not line.startswith("---"):
            total_lines += 1

    if total_lines <= 3: trivial += 1
    elif total_lines <= 10: simple += 1
    elif total_lines <= 30: moderate += 1
    else: complex_count += 1

    if len(gt_files) > 1: multi += 1

    traj_path = f"{TRAJ_DIR}/{iid}/{iid}.traj.json"
    try:
        t = json.load(open(traj_path))
        turns = len(t.get("history", t.get("messages", [])))
    except Exception:
        turns = 0
    total_turns += turns
    if turns <= 40: fast += 1
    elif turns <= 80: moderate_speed += 1
    else: slow += 1

print(f"Fix complexity:")
print(f"  Trivial (1-3 lines):  {trivial}")
print(f"  Simple (4-10 lines):  {simple}")
print(f"  Moderate (11-30):     {moderate}")
print(f"  Complex (30+):        {complex_count}")
print(f"  Multi-file:           {multi}")
print()
print(f"Agent speed:")
print(f"  Fast (<=40 turns):    {fast}")
print(f"  Moderate (41-80):     {moderate_speed}")
print(f"  Slow (>80 turns):     {slow}")
print(f"  Avg turns:            {total_turns / len(resolved):.0f}")
print()
print("GENERALIZABLE GT SIGNALS (for any codebase):")
print()
print("1. LOCALIZATION BOOST: For slow tasks (>80 turns), showing the target")
print("   file + function upfront would save 30-60 turns of exploration.")
print("   On resolved tasks, agent eventually finds the right file — GT")
print("   could make this instant.")
print()
print("2. FIX PATTERN: For simple fixes (1-10 lines), showing a similar")
print("   pattern in the same codebase (sibling method, git precedent)")
print("   would anchor the agent's fix in real code.")
print()
print("3. TEST EXPECTATION: For all tasks, showing what the failing test")
print("   asserts gives the agent a concrete correctness target.")
print()
print("4. CALLER CONTRACT: For right-file-wrong-fix cases, showing how")
print("   callers use the function's return value prevents interface breaks.")
print()
print("5. MODIFICATION SCOPE: For complex fixes, showing which other files")
print("   need changes (co-change coupling) prevents partial fixes.")
