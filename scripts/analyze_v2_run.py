#!/usr/bin/env python3
"""Analyze GT v1.0.4 v2 run -- per-task function output deep dive."""

import json
import re
import glob
import os
import sys

BASE_GT = "/home/Lenovo/groundtruth/output/v104_sweagent_gt_v2"
BASE_BL = "/home/Lenovo/groundtruth/output/v104_sweagent_baseline_v2"

ALL_TASKS = ["12907", "13033", "13236", "13398", "13453",
             "13579", "13977", "14096", "14182", "14309"]

GT_RESOLVED = {"12907", "13236", "13453", "13579", "14096", "14309"}
BL_RESOLVED = {"12907", "13453", "13579", "14309"}
V1_RESOLVED = {"12907", "13453", "13579", "14096", "14309"}


def analyze_task(short):
    task = f"astropy__astropy-{short}"
    base = f"{BASE_GT}/{task}"
    status = "RESOLVED" if short in GT_RESOLVED else "UNRESOLVED"
    bl_status = "RESOLVED" if short in BL_RESOLVED else "UNRESOLVED"
    v1_status = "RESOLVED" if short in V1_RESOLVED else "UNRESOLVED"

    delta = ""
    if short in GT_RESOLVED and short not in V1_RESOLVED:
        delta = " ** NEW GT FLIP **"
    elif short not in GT_RESOLVED and short in V1_RESOLVED:
        delta = " ** GT REGRESSION **"

    print(f"\n{'=' * 70}")
    print(f"TASK: {short}  |  v1 GT: {v1_status}  |  v2 GT: {status}  |  v2 BL: {bl_status}{delta}")
    print(f"{'=' * 70}")

    # 1. State retrievals
    debug_path = f"{base}/{task}.debug.log"
    debug_log = open(debug_path).read() if os.path.exists(debug_path) else ""
    states = re.findall(r"Retrieved state from environment: (.*)", debug_log)
    gt_states = [s for s in states if "gt_evidence" in s]

    print(f"\n  _state_gt EXECUTION:")
    print(f"    Total state retrievals: {len(states)}")
    print(f"    Deliveries with gt_evidence: {len(gt_states)}")
    print(f"    Delivery rate: {len(gt_states)}/{len(states)} = {100*len(gt_states)/max(len(states),1):.0f}%")

    # 2. Evidence content from info.log
    info_path = f"{base}/{task}.info.log"
    info_log = open(info_path).read() if os.path.exists(info_path) else ""
    ev_blocks = re.findall(r"<gt-evidence>(.*?)</gt-evidence>", info_log, re.DOTALL)

    families = set()
    evidence_lines = []
    for block in ev_blocks:
        for line in block.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            evidence_lines.append(line)
            if "CAUTION:" in line:
                families.add("IMPACT")
            if "sibling" in line.lower():
                families.add("SIBLING")
            if "commit:" in line:
                families.add("PRECEDENT")
            if "DO NOT change" in line:
                families.add("CALLER")
            if "MUST PRESERVE" in line:
                families.add("OBLIGATION")
            if "STRUCTURAL ERROR" in line:
                families.add("NEGATIVE")
            if "VERIFY" in line:
                families.add("TEST")
            if "No high-confidence" in line:
                families.add("EMPTY")

    print(f"\n  EVIDENCE SHOWN TO MODEL:")
    print(f"    Blocks shown: {len(ev_blocks)}")
    print(f"    Families: {sorted(families) if families else '(none)'}")
    if evidence_lines:
        for el in evidence_lines[:4]:
            print(f"    > {el[:120]}")

    # 3. First evidence block quality
    first_structural = "none"
    if ev_blocks:
        first = ev_blocks[0].strip()
        if any(kw in first for kw in ["MUST PRESERVE", "STRUCTURAL ERROR", "DO NOT change", "VERIFY"]):
            first_structural = "STRUCTURAL"
        elif any(kw in first for kw in ["CAUTION:", "callers in"]):
            first_structural = "IMPACT (generic)"
        elif "commit:" in first or "MATCH PATTERN: commit" in first:
            first_structural = "PRECEDENT"
        elif "sibling" in first.lower():
            first_structural = "SIBLING"
        elif "No high-confidence" in first:
            first_structural = "EMPTY"
        else:
            first_structural = "OTHER"
    print(f"    First block type: {first_structural}")

    # 4. Trajectory
    traj_path = f"{base}/{task}.traj"
    if os.path.exists(traj_path):
        traj = json.load(open(traj_path))
        steps = traj.get("trajectory", [])
        edits = sum(1 for s in steps if "str_replace" in str(s.get("action", "")))
        gt_checks = sum(1 for s in steps if "gt_check" in str(s.get("action", "")))
        print(f"\n  TRAJECTORY:")
        print(f"    Steps: {len(steps)}")
        print(f"    Edits: {edits}")
        print(f"    gt_check calls: {gt_checks}")

    # 5. Patch
    pred_path = f"{base}/{task}.pred"
    if os.path.exists(pred_path):
        try:
            inner = json.loads(open(pred_path).read())
            patch = inner.get("model_patch", "")
            lines = patch.split("\n")
            files_changed = [l for l in lines if l.startswith("diff --git")]
            additions = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
            print(f"\n  PATCH:")
            print(f"    Files changed: {len(files_changed)}")
            print(f"    +{additions} -{deletions}")
            for fc in files_changed[:3]:
                fname = fc.split(" b/")[-1] if " b/" in fc else fc
                print(f"    {fname}")
        except Exception:
            pass


def main():
    for short in ALL_TASKS:
        try:
            analyze_task(short)
        except Exception as e:
            print(f"\n  ERROR analyzing {short}: {e}")

    print(f"\n{'=' * 70}")
    print("AGGREGATE SUMMARY")
    print(f"{'=' * 70}")
    print(f"v1: GT 5/10, BL 5/10, delta=0")
    print(f"v2: GT {len(GT_RESOLVED)}/10, BL {len(BL_RESOLVED)}/9, delta=+{len(GT_RESOLVED)-len(BL_RESOLVED)}")
    print(f"GT gains vs v1: {sorted(GT_RESOLVED - V1_RESOLVED)}")
    print(f"GT losses vs v1: {sorted(V1_RESOLVED - GT_RESOLVED)}")
    print(f"GT-only resolves (not in BL): {sorted(GT_RESOLVED - BL_RESOLVED)}")


if __name__ == "__main__":
    main()
