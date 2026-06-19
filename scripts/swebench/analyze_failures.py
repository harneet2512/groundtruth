#!/usr/bin/env python3
"""Analyze why the baseline failed on each task — what info would flip it?"""
import json
import os
import sys
from datasets import load_dataset

# Paths
PREDS_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/Lenovo/results/v8_mini_20260328_101151/baseline/preds.json"
EVAL_PATH = sys.argv[2] if len(sys.argv) > 2 else "/home/Lenovo/openai__qwen3-coder.v9_50_bl.json"

# Load dataset for ground truth
ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
gt_patches = {}
gt_problems = {}
for d in ds:
    gt_patches[d["instance_id"]] = d["patch"]
    gt_problems[d["instance_id"]] = d["problem_statement"]

# Load predictions and eval
bl_preds = json.load(open(PREDS_PATH))
bl_eval = json.load(open(EVAL_PATH))
unresolved = set(bl_eval.get("unresolved_ids", []))
resolved = set(bl_eval.get("resolved_ids", []))

print(f"Baseline: {len(resolved)} resolved, {len(unresolved)} failed")
print()

# Classify failures
no_patch = []
wrong_file = []
right_file_wrong_fix = []

for iid in sorted(unresolved):
    gt_patch = gt_patches.get(iid, "")
    problem = gt_problems.get(iid, "")

    pred = bl_preds.get(iid, {})
    agent_patch = pred.get("model_patch", "") if isinstance(pred, dict) else ""

    # Parse GT patch files
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

    # Parse agent patch files
    agent_files = set()
    agent_added = 0
    for line in agent_patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                agent_files.add(parts[2].lstrip("a/"))
        if line.startswith("+") and not line.startswith("+++"):
            agent_added += 1

    right_file = bool(gt_files & agent_files)
    has_patch = len(agent_patch.strip()) > 20

    # Classify
    if not has_patch:
        no_patch.append(iid)
        category = "NO_PATCH"
    elif not right_file:
        wrong_file.append(iid)
        category = "WRONG_FILE"
    else:
        right_file_wrong_fix.append(iid)
        category = "RIGHT_FILE_WRONG_FIX"

    print(f"--- {iid} [{category}] ---")
    print(f"  GT fix: {sorted(gt_files)} (+{gt_added}/-{gt_removed} lines)")
    print(f"  Agent:  {sorted(agent_files)} (+{agent_added} lines)")

    # Show what the GT patch actually does (first 3 hunks)
    hunk_count = 0
    in_hunk = False
    hunk_lines = []
    for line in gt_patch.split("\n"):
        if line.startswith("@@"):
            if hunk_lines and hunk_count <= 3:
                for hl in hunk_lines[:5]:
                    print(f"    {hl}")
                if len(hunk_lines) > 5:
                    print(f"    ... ({len(hunk_lines)-5} more lines)")
            hunk_lines = [line]
            hunk_count += 1
            in_hunk = True
        elif in_hunk:
            if line.startswith(("diff", "---", "+++")):
                in_hunk = False
            else:
                hunk_lines.append(line)
    # Last hunk
    if hunk_lines and hunk_count <= 3:
        for hl in hunk_lines[:5]:
            print(f"    {hl}")

    # What would GT need to provide to flip this?
    if category == "WRONG_FILE":
        print(f"  >> GT COULD HELP: Agent edited {sorted(agent_files)} but needed {sorted(gt_files)}")
        print(f"  >> Need: localization to {sorted(gt_files)}")
    elif category == "RIGHT_FILE_WRONG_FIX":
        print(f"  >> Agent found the right file but wrote the wrong fix")
        print(f"  >> Need: behavioral constraint from callers/tests to guide the fix")
    elif category == "NO_PATCH":
        print(f"  >> Agent couldn't produce any patch")
        print(f"  >> Need: localization + understanding of the codebase")
    print()

# Summary
print("=" * 70)
print("FAILURE CLASSIFICATION:")
print(f"  No patch produced:      {len(no_patch)}")
print(f"  Wrong file edited:      {len(wrong_file)}")
print(f"  Right file, wrong fix:  {len(right_file_wrong_fix)}")
print()
print("WHAT GT NEEDS TO PROVIDE TO FLIP TASKS:")
print(f"  Localization (wrong file or no patch): {len(no_patch) + len(wrong_file)} tasks")
print(f"  Better fix guidance (right file):      {len(right_file_wrong_fix)} tasks")
print()
print("WRONG FILE details:")
for iid in wrong_file:
    gt_files = set()
    for line in gt_patches.get(iid, "").split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                gt_files.add(parts[2].lstrip("a/"))
    pred = bl_preds.get(iid, {})
    agent_patch = pred.get("model_patch", "") if isinstance(pred, dict) else ""
    agent_files = set()
    for line in agent_patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                agent_files.add(parts[2].lstrip("a/"))
    print(f"  {iid}: agent={sorted(agent_files)} needed={sorted(gt_files)}")

print()
print("RIGHT FILE WRONG FIX details:")
for iid in right_file_wrong_fix:
    gt_files = set()
    for line in gt_patches.get(iid, "").split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                gt_files.add(parts[2].lstrip("a/"))
    print(f"  {iid}: files={sorted(gt_files)}")
