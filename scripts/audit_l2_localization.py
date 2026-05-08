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

# We'll look for telemetry files in these locations
TELEMETRY_DIRS = [
    "/tmp/gt_logs",
    "logs/gt",
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

def extract_l2_candidates_from_record(record):
    """Extract L2 candidates from a TelemetryRecord-like dictionary."""
    candidates = []
    
    # 1. Try Module 6 Hybrid Fused Candidates (The definitive L2 output)
    m6 = record.get("module_6_hybrid", {})
    fused = m6.get("fused_candidates", [])
    if fused:
        for c in fused:
            if isinstance(c, dict) and c.get("file"):
                candidates.append(c["file"])
            elif isinstance(c, str):
                candidates.append(c)
                
    # 2. Try GT Plan (v7 specific)
    if not candidates:
        plan = record.get("gt_plan", {})
        focus = plan.get("agent_focus_files", [])
        for f in focus:
            if isinstance(f, dict) and f.get("file"):
                candidates.append(f["file"])

    return candidates

def main():
    print("Loading SWE-bench-Live Lite dataset...")
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    
    # Map from instance_id to telemetry records
    all_telemetry = {}
    
    # Scan for telemetry files
    for d in TELEMETRY_DIRS:
        if os.path.exists(d):
            for f in Path(d).glob("*.jsonl"):
                with open(f, "r", encoding="utf-8") as file:
                    for line in file:
                        try:
                            rec = json.loads(line)
                            iid = rec.get("task_id")
                            if iid in ALL_TASKS:
                                all_telemetry[iid] = rec
                        except:
                            pass

    print(f"Found telemetry for {len(all_telemetry)} tasks.")
    
    hits = 0
    total = 0
    
    for iid in ALL_TASKS:
        gold = get_gold_files(iid, ds)
        if not gold:
            continue
            
        total += 1
        record = all_telemetry.get(iid)
        if not record:
            print(f"MISSING {iid}: No telemetry found.")
            continue
            
        candidates = extract_l2_candidates_from_record(record)
        norm_gold = [g.replace("\\", "/") for g in gold]
        norm_cand = [c.replace("\\", "/") for c in candidates[:3]]
        
        is_hit = any(g in norm_cand for g in norm_gold)
        if is_hit:
            hits += 1
            
        status = "✓" if is_hit else "✗"
        print(f"{status} {iid}: Gold={gold} | L2={candidates[:3]}")

    if total > 0:
        accuracy = (hits / total) * 100
        print(f"\nL2 Localization Accuracy (Top-3): {hits}/{total} ({accuracy:.1f}%)")
    else:
        print("\nNo tasks analyzed.")

if __name__ == "__main__":
    main()
