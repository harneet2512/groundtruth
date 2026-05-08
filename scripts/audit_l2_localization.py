#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from datasets import load_dataset

# Phase 4 Tasks from DEFICIENT_DEEP.md
TASKS_T0 = [
    "aiogram__aiogram-1594", "aws-cloudformation__cfn-lint-3789", "aws-cloudformation__cfn-lint-3798",
    "aws-cloudformation__cfn-lint-3821", "aws-cloudformation__cfn-lint-3854", "aws-cloudformation__cfn-lint-3856",
    "aws-cloudformation__cfn-lint-3862", "aws-cloudformation__cfn-lint-3866", "aws-cloudformation__cfn-lint-3875",
    "aws-cloudformation__cfn-lint-3890", "aws-cloudformation__cfn-lint-4002", "aws-cloudformation__cfn-lint-4023",
    "aws-cloudformation__cfn-lint-4032", "beancount__beancount-931", "beetbox__beets-5495",
    "beeware__briefcase-2075", "beeware__briefcase-2085", "bridgecrewio__checkov-6893",
    "bridgecrewio__checkov-6895", "bridgecrewio__checkov-7002"
]
TASKS_V1 = [
    "arviz-devs__arviz-2413", "aws-cloudformation__cfn-lint-3779", "aws-cloudformation__cfn-lint-3805",
    "aws-cloudformation__cfn-lint-4016", "delgan__loguru-1306", "kozea__weasyprint-2303",
    "pydata__xarray-9760", "pydata__xarray-9971", "pylint-dev__pylint-10044", "pypa__twine-1225"
]
ALL_TASKS = TASKS_T0 + TASKS_V1

OUTPUT_FILES = [
    "benchmarks/openhands/cal20_live_lite/output.jsonl",
    ".tmp_oh_smoke_output.jsonl"
]

def get_gold_files(instance_id, dataset):
    for row in dataset:
        if row["instance_id"] == instance_id:
            patch = row.get("patch", "")
            files = set()
            for line in patch.split("\n"):
                if line.startswith("--- a/"):
                    files.add(line[6:].strip())
            return [f for f in files if f and "/test" not in f.lower() and "test_" not in f.lower()]
    return []

def extract_l2_candidates(record):
    # Try to find candidates in gt_brief first
    brief = record.get("gt_brief", "") or ""
    if not brief and "test_result" in record:
         # Some records might wrap it differently
         brief = record["test_result"].get("gt_brief", "") or ""
    
    # V7 Brief format: "  1. path/to/file.py [reason]"
    candidates = []
    for line in brief.split("\n"):
        m = re.search(r"^\s*\d+\.\s+(\S+)\s+\[", line)
        if m:
            candidates.append(m.group(1))
    
    # If not in brief, check telemetry or plan
    if not candidates:
        tel = record.get("gt_telemetry", {})
        if isinstance(tel, str):
            try: tel = json.loads(tel)
            except: tel = {}
        
        plan = record.get("gt_plan", {})
        if not plan and "test_result" in record:
             plan = record["test_result"].get("gt_plan", {})
             
        if plan:
            candidates = [c.get("file") for c in plan.get("agent_focus_files", [])]

    return [c for c in candidates if c]

def main():
    print("Loading SWE-bench-Live Lite dataset...")
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    
    records = {}
    for out_file in OUTPUT_FILES:
        if os.path.exists(out_file):
            with open(out_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        iid = rec.get("instance_id")
                        if iid:
                            records[iid] = rec
                    except:
                        pass
    
    print(f"Found {len(records)} total records in output files.")
    
    hits = 0
    total = 0
    results = []
    
    # Sort IDs for stable output
    for iid in sorted(records.keys()):
        gold = get_gold_files(iid, ds)
        if not gold:
            # Try to see if it's in the dataset at all but maybe not Lite?
            # For now, if not in Lite, skip
            continue
        
        rec = records.get(iid, {})
        candidates = extract_l2_candidates(rec)
        
        # Check if at least one gold file is in top-3 candidates
        # Normalize paths for comparison
        norm_gold = [g.replace("\\", "/") for g in gold]
        norm_cand = [c.replace("\\", "/") for c in candidates[:3]]
        
        is_hit = any(g in norm_cand for g in norm_gold)
        if is_hit:
            hits += 1
        total += 1
        
        results.append({
            "instance_id": iid,
            "gold": gold,
            "l2_candidates": candidates[:3],
            "hit": is_hit
        })
        
        status = "✓" if is_hit else "✗"
        print(f"{status} {iid}: Gold={gold} | L2={candidates[:3]}")

    if total > 0:
        accuracy = (hits / total) * 100
        print(f"\nL2 Localization Accuracy (Top-3): {hits}/{total} ({accuracy:.1f}%)")
    else:
        print("\nNo matching tasks found in dataset.")

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
