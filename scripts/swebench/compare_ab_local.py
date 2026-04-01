#!/usr/bin/env python3
"""Compare baseline vs GT hooked results from eval_new directory."""
import json
import glob
import sys
import os

EVAL_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/eval_new")

bl_path = os.path.join(EVAL_DIR, "baseline", "preds.json")
gt_path = os.path.join(EVAL_DIR, "gt_hooked", "preds.json")

bl = json.load(open(bl_path)) if os.path.exists(bl_path) else {}
gt = json.load(open(gt_path)) if os.path.exists(gt_path) else {}

print("=" * 60)
print("BASELINE vs GT HOOKED")
print("=" * 60)
print(f"Baseline tasks: {len(bl)}")
print(f"GT tasks: {len(gt)}")

common = sorted(set(bl.keys()) & set(gt.keys()))
print(f"Common: {common}")
print()

for tid in common:
    bl_patch = bl[tid].get("model_patch", "") or ""
    gt_patch = gt[tid].get("model_patch", "") or ""
    print(f"--- {tid} ---")
    print(f"  Baseline patch: {len(bl_patch)} chars")
    print(f"  GT patch:       {len(gt_patch)} chars")
    print(f"  Same? {bl_patch == gt_patch}")

    traj_path = os.path.join(EVAL_DIR, "gt_hooked", tid, f"{tid}.traj.json")
    if os.path.exists(traj_path):
        with open(traj_path) as f:
            traj = json.load(f)
        info = traj.get("info", {})
        msgs = traj.get("messages", [])
        print(f"  briefing_lines: {info.get('briefing_lines', 0)}")
        print(f"  hook_injected: {info.get('hook_injected')}")

        gt_lines = []
        for m in msgs:
            c = m.get("content", "")
            if not isinstance(c, str):
                continue
            for line in c.split("\n"):
                line = line.strip()
                if any(k in line for k in [
                    "REMINDER", "CODEBASE CONTEXT", "FIX HERE",
                    "DO NOT", "CAUTION", "PRESERVE", "gt-evidence",
                    "[VERIFIED]", "[WARNING]",
                ]):
                    gt_lines.append(line[:130])
        print(f"  GT evidence: {len(gt_lines)} lines")
        for line in gt_lines:
            print(f"    | {line}")
    print()

print("=" * 60)
print("PATCHES (first 400 chars)")
print("=" * 60)
for tid in common:
    print(f"\n  {tid} BASELINE:")
    print("  " + (bl[tid].get("model_patch", "") or "(empty)")[:400])
    print(f"\n  {tid} GT:")
    print("  " + (gt[tid].get("model_patch", "") or "(empty)")[:400])
