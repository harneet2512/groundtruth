"""Measure L1 brief localization accuracy before and after Phase A changes.

Usage (on VM with graph.db available):
    python scripts/analysis/measure_l1_localization.py \
        --repo-root /path/to/cloned/repo \
        --graph-db /path/to/graph.db \
        --task-ids task1,task2,...

Usage (offline with existing output.jsonl):
    python scripts/analysis/measure_l1_localization.py \
        --output-dir results/extracted/home/ubuntu/results/compress_t0/...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_gold_files_from_patch(patch: str) -> list[str]:
    """Extract modified file paths from a unified diff patch."""
    files = []
    for line in patch.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and path not in files:
                files.append(path)
    return files


def parse_brief_candidates(instruction: str) -> list[str]:
    """Extract ranked candidate files from <gt-task-brief> block."""
    m = re.search(r"<gt-task-brief>(.*?)</gt-task-brief>", instruction, re.DOTALL)
    if not m:
        return []
    block = m.group(1)
    candidates = []
    for line in block.strip().splitlines():
        line = line.strip()
        ranked = re.match(r"^\d+\.\s+(\S+\.(?:py|go|js|ts|rs|java|rb|php|yaml|yml|rst|md|toml|json))\b", line)
        if ranked:
            candidates.append(ranked.group(1))
    return candidates


def compute_hits(gold_files: list[str], candidates: list[str]) -> dict:
    """Compute hit@k metrics."""
    gold_set = {f.replace("\\", "/") for f in gold_files}
    cand_norm = [c.replace("\\", "/") for c in candidates]

    first_gold_rank = float("inf")
    for i, cand in enumerate(cand_norm, 1):
        if cand in gold_set:
            first_gold_rank = i
            break

    return {
        "hit_at_1": any(c in gold_set for c in cand_norm[:1]),
        "hit_at_3": any(c in gold_set for c in cand_norm[:3]),
        "hit_at_5": any(c in gold_set for c in cand_norm[:5]),
        "first_gold_rank": first_gold_rank,
        "gold_coverage": sum(1 for c in cand_norm if c in gold_set) / max(len(gold_set), 1),
    }


def load_gold_from_dataset(task_ids: list[str]) -> dict[str, list[str]]:
    """Load gold files for specified tasks from SWE-bench-Live dataset."""
    from datasets import load_dataset

    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="test")
    gold_map = {}
    for row in ds:
        iid = row["instance_id"]
        if iid in task_ids:
            gold_map[iid] = parse_gold_files_from_patch(row["patch"])
    return gold_map


def load_briefs_from_output(output_dir: str, task_ids: list[str]) -> dict[str, list[str]]:
    """Load brief candidates from output.jsonl files."""
    briefs = {}
    for jsonl_path in Path(output_dir).rglob("output.jsonl"):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                iid = d.get("instance_id", "")
                if iid in task_ids:
                    candidates = parse_brief_candidates(d.get("instruction", ""))
                    briefs[iid] = candidates
    return briefs


def main():
    parser = argparse.ArgumentParser(description="Measure L1 localization accuracy")
    parser.add_argument("--output-dir", help="Directory with output.jsonl files")
    parser.add_argument("--task-ids", help="Comma-separated task IDs (default: all 30)")
    args = parser.parse_args()

    default_ids = [
        "aiogram__aiogram-1594",
        "aws-cloudformation__cfn-lint-3789", "aws-cloudformation__cfn-lint-3798",
        "aws-cloudformation__cfn-lint-3821", "aws-cloudformation__cfn-lint-3854",
        "aws-cloudformation__cfn-lint-3856", "aws-cloudformation__cfn-lint-3862",
        "aws-cloudformation__cfn-lint-3866", "aws-cloudformation__cfn-lint-3875",
        "aws-cloudformation__cfn-lint-3890", "aws-cloudformation__cfn-lint-4002",
        "aws-cloudformation__cfn-lint-4023", "aws-cloudformation__cfn-lint-4032",
        "beancount__beancount-931", "beetbox__beets-5495",
        "beeware__briefcase-2075", "beeware__briefcase-2085",
        "bridgecrewio__checkov-6893", "bridgecrewio__checkov-6895",
        "bridgecrewio__checkov-7002",
        "arviz-devs__arviz-2413", "aws-cloudformation__cfn-lint-3779",
        "aws-cloudformation__cfn-lint-3805", "aws-cloudformation__cfn-lint-4016",
        "delgan__loguru-1306", "kozea__weasyprint-2303",
        "pydata__xarray-9760", "pydata__xarray-9971",
        "pylint-dev__pylint-10044", "pypa__twine-1225",
    ]
    task_ids = args.task_ids.split(",") if args.task_ids else default_ids

    gold_map = load_gold_from_dataset(task_ids)
    if not gold_map:
        print("ERROR: No gold patches found for the specified task IDs", file=sys.stderr)
        sys.exit(1)

    if args.output_dir:
        briefs = load_briefs_from_output(args.output_dir, task_ids)
    else:
        briefs = {}

    print("| # | Task | Gold | Cand | H@1 | H@3 | FGR |")
    print("|---|------|------|------|-----|-----|-----|")

    totals = {"hit_at_1": 0, "hit_at_3": 0, "hit_at_5": 0, "count": 0}
    for i, tid in enumerate(sorted(gold_map.keys()), 1):
        gold = gold_map[tid]
        cand = briefs.get(tid, [])
        if not cand:
            print(f"| {i} | {tid} | {len(gold)} | 0 | - | - | - |")
            totals["count"] += 1
            continue
        hits = compute_hits(gold, cand)
        totals["hit_at_1"] += hits["hit_at_1"]
        totals["hit_at_3"] += hits["hit_at_3"]
        totals["hit_at_5"] += hits["hit_at_5"]
        totals["count"] += 1
        fgr = hits["first_gold_rank"] if hits["first_gold_rank"] != float("inf") else "inf"
        print(
            f"| {i} | {tid} | {len(gold)} | {len(cand)} | "
            f"{'Y' if hits['hit_at_1'] else '-'} | "
            f"{'Y' if hits['hit_at_3'] else '-'} | {fgr} |"
        )

    n = totals["count"]
    print()
    print(f"hit@1: {totals['hit_at_1']}/{n} ({100*totals['hit_at_1']/n:.1f}%)")
    print(f"hit@3: {totals['hit_at_3']}/{n} ({100*totals['hit_at_3']/n:.1f}%)")
    print(f"hit@5: {totals['hit_at_5']}/{n} ({100*totals['hit_at_5']/n:.1f}%)")


if __name__ == "__main__":
    main()
