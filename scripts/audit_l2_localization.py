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

def extract_l2_from_text(text):
    if not text: return [], "empty"
    
    if "GT graph built inside the task container" in text:
        return [], "fallback"
    
    if "GT could not deterministically localize this issue" in text:
        return [], "agnostic_fail"
    
    candidates = []
    # Try V7 pattern: "  1. path/to/file.py [reason]"
    for line in text.split("\n"):
        m = re.search(r"^\s*\d+\.\s+(\S+)\s+\[", line)
        if m:
            candidates.append(m.group(1))
            
    if not candidates:
        # Check for bullet points like "- path/to/file.py"
        for line in text.split("\n"):
            m = re.search(r"^\s*-\s+(\S+\.py)\b", line)
            if m:
                candidates.append(m.group(1))
                
    return candidates, "success" if candidates else "parse_fail"

def extract_l2_candidates(record):
    # 1. Try explicit gt_brief keys
    brief = record.get("gt_brief")
    if not brief and "test_result" in record and isinstance(record["test_result"], dict):
        brief = record["test_result"].get("gt_brief")
    
    if brief:
        cands, status = extract_l2_from_text(brief)
        if status != "empty": return cands, status

    # 2. Try instruction field (often has the injected brief)
    instr = record.get("instruction")
    if instr:
        # Look for <gt-task-brief> block
        m = re.search(r"<gt-task-brief>(.*?)</gt-task-brief>", instr, re.DOTALL)
        if m:
            cands, status = extract_l2_from_text(m.group(1))
            if status != "empty": return cands, status
        else:
            # Fallback to scanning the whole instruction
            cands, status = extract_l2_from_text(instr)
            if status != "empty": return cands, status

    # 3. Try gt_plan in record or test_result
    plan = record.get("gt_plan")
    if not plan and "test_result" in record and isinstance(record["test_result"], dict):
        plan = record["test_result"].get("gt_plan")
        
    if plan and isinstance(plan, dict):
        focus = plan.get("agent_focus_files", [])
        cands = []
        for f in focus:
            if isinstance(f, dict) and f.get("file"):
                cands.append(f["file"])
            elif isinstance(f, str):
                cands.append(f)
        if cands: return cands, "success"

    return [], "not_found"

def main():
    print("Loading SWE-bench-Live Lite dataset...")
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
    
    records = {}
    for out_file in OUTPUT_FILES:
        if os.path.exists(out_file):
            print(f"Reading {out_file}...")
            with open(out_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        iid = rec.get("instance_id")
                        if iid:
                            records[iid] = rec
                    except:
                        pass
    
    print(f"Found {len(records)} total unique task records.")
    
    stats = {"success": 0, "fallback": 0, "agnostic_fail": 0, "parse_fail": 0, "not_found": 0}
    hits = 0
    total = 0
    
    for iid in sorted(records.keys()):
        gold = get_gold_files(iid, ds)
        if not gold: continue
        
        total += 1
        rec = records.get(iid, {})
        candidates, status = extract_l2_candidates(rec)
        
        stats[status] = stats.get(status, 0) + 1
        
        norm_gold = [g.replace("\\", "/").lower() for g in gold]
        norm_cand = [c.replace("\\", "/").lower() for c in candidates[:3]]
        
        is_hit = any(g in norm_cand for g in norm_gold)
        if is_hit:
            hits += 1
        
        status_marker = f" [{status.upper()}]" if status != "success" else ""
        hit_marker = "✓" if is_hit else "✗"
        print(f"{hit_marker} {iid}: Gold={gold} | L2={candidates[:3]}{status_marker}")

    if total > 0:
        accuracy = (hits / total) * 100
        real_briefs = stats["success"]
        real_acc = (hits / real_briefs * 100) if real_briefs > 0 else 0
        
        print(f"\n--- L2 Localization Audit Report ---")
        print(f"Total Tasks Analyzed: {total}")
        print(f"  - Real L2 Briefs:  {stats['success']}")
        print(f"  - Fallback Briefs: {stats['fallback']}")
        print(f"  - Agnostic Fails:  {stats['agnostic_fail']}")
        print(f"  - Parse Fails:     {stats['parse_fail']}")
        print(f"  - Not Found:       {stats['not_found']}")
        print(f"\nOverall L2 Accuracy (Top-3): {hits}/{total} ({accuracy:.1f}%)")
        print(f"Precision on Real Briefs: {hits}/{real_briefs} ({real_acc:.1f}%)")
    else:
        print("\nNo matching tasks found.")

if __name__ == "__main__":
    main()
