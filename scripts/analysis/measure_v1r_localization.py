"""Measure V1R brief localization accuracy across all 30 tasks locally.

For each task: checkout base_commit, build graph.db, generate V1R brief,
compare candidates to gold files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

GT_INDEX = str(Path(__file__).resolve().parents[2] / "gt-index" / "gt-index.exe")
TEST_DIR = "D:/tmp/gt_test"

TASKS = {
    "aiogram__aiogram-1594": ("aiogram", "51beb482"),
    "aws-cloudformation__cfn-lint-3789": ("cfn-lint", "74847b14"),
    "aws-cloudformation__cfn-lint-3798": ("cfn-lint", "d5c3da9e"),
    "aws-cloudformation__cfn-lint-3821": ("cfn-lint", "9d83f3fb"),
    "aws-cloudformation__cfn-lint-3854": ("cfn-lint", "6d083eb3"),
    "aws-cloudformation__cfn-lint-3856": ("cfn-lint", "19192ef6"),
    "aws-cloudformation__cfn-lint-3862": ("cfn-lint", "30ecbc1f"),
    "aws-cloudformation__cfn-lint-3866": ("cfn-lint", "30ecbc1f"),
    "aws-cloudformation__cfn-lint-3875": ("cfn-lint", "8454bc9a"),
    "aws-cloudformation__cfn-lint-3890": ("cfn-lint", "700563cd"),
    "aws-cloudformation__cfn-lint-4002": ("cfn-lint", "e6278b45"),
    "aws-cloudformation__cfn-lint-4023": ("cfn-lint", "0bf508f3"),
    "aws-cloudformation__cfn-lint-4032": ("cfn-lint", "7351d0cf"),
    "aws-cloudformation__cfn-lint-3779": ("cfn-lint", "de28d1e4"),
    "aws-cloudformation__cfn-lint-3805": ("cfn-lint", "88efb088"),
    "aws-cloudformation__cfn-lint-4016": ("cfn-lint", "65136702"),
    "beancount__beancount-931": ("beancount", "a0e6f445"),
    "beetbox__beets-5495": ("beets", "fa10dcf1"),
    "beeware__briefcase-2075": ("briefcase", "98b3cb01"),
    "beeware__briefcase-2085": ("briefcase", "40052023"),
    "bridgecrewio__checkov-6893": ("checkov", "7741985f"),
    "bridgecrewio__checkov-6895": ("checkov", "a94c1682"),
    "bridgecrewio__checkov-7002": ("checkov", "8b0f288a"),
    "arviz-devs__arviz-2413": ("arviz", "0fc11178"),
    "delgan__loguru-1306": ("loguru", "3cfd03fb"),
    "kozea__weasyprint-2303": ("WeasyPrint", "d0fcb3c4"),
    "pydata__xarray-9760": ("xarray", "5a9ff0be"),
    "pydata__xarray-9971": ("xarray", "4ccb048d"),
    "pylint-dev__pylint-10044": ("pylint", "2e0c41f6"),
    "pypa__twine-1225": ("twine", "aa3a910c"),
}


def gold_files_from_patch(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].strip()
            if p and p != "/dev/null" and p not in files:
                files.append(p)
    return files


def checkout(repo_dir: str, commit: str) -> bool:
    r = subprocess.run(
        ["git", "checkout", commit, "--force"],
        cwd=repo_dir, capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0


def build_graph(repo_dir: str, db_path: str) -> bool:
    r = subprocess.run(
        [GT_INDEX, f"-root={repo_dir}", f"-output={db_path}"],
        capture_output=True, text=True, timeout=300,
    )
    return r.returncode == 0


def generate_v1r(issue_text: str, repo_dir: str, db_path: str, task_id: str) -> list[str]:
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        result = generate_v1r_brief(
            issue_text=issue_text,
            repo_root=repo_dir,
            graph_db=db_path,
            bug_id=task_id,
        )
        return [f.path for f in result.files]
    except Exception as e:
        print(f"  ERROR generating brief: {e}", file=sys.stderr)
        return []


def hit_at_k(gold: list[str], cands: list[str], k: int) -> bool:
    gold_set = {g.replace("\\", "/") for g in gold}
    for c in cands[:k]:
        cn = c.replace("\\", "/")
        if cn in gold_set or any(cn.endswith("/" + g) or g.endswith("/" + cn) for g in gold_set):
            return True
    return False


def first_gold_rank(gold: list[str], cands: list[str]) -> int | float:
    gold_set = {g.replace("\\", "/") for g in gold}
    for i, c in enumerate(cands, 1):
        cn = c.replace("\\", "/")
        if cn in gold_set or any(cn.endswith("/" + g) or g.endswith("/" + cn) for g in gold_set):
            return i
    return float("inf")


def main():
    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="test")
    issue_map = {}
    gold_map = {}
    for row in ds:
        iid = row["instance_id"]
        if iid in TASKS:
            issue_map[iid] = row["problem_statement"]
            gold_map[iid] = gold_files_from_patch(row["patch"])

    db_cache: dict[str, str] = {}
    results = []

    print("| # | Task | Gold | Cand | H@1 | H@3 | H@5 | FGR |")
    print("|---|------|------|------|-----|-----|-----|-----|")

    for i, (tid, (repo_name, commit)) in enumerate(sorted(TASKS.items()), 1):
        repo_dir = os.path.join(TEST_DIR, repo_name)
        cache_key = f"{repo_name}_{commit}"
        db_path = os.path.join(TEST_DIR, f"{cache_key}.db")

        if cache_key not in db_cache:
            if not os.path.exists(db_path):
                if not checkout(repo_dir, commit):
                    print(f"| {i} | {tid} | ? | 0 | ERR | ERR | ERR | ERR | checkout failed")
                    continue
                if not build_graph(repo_dir, db_path):
                    print(f"| {i} | {tid} | ? | 0 | ERR | ERR | ERR | ERR | index failed")
                    continue
            db_cache[cache_key] = db_path

        gold = gold_map.get(tid, [])
        issue = issue_map.get(tid, "")
        cands = generate_v1r(issue, repo_dir, db_path, tid)

        h1 = hit_at_k(gold, cands, 1)
        h3 = hit_at_k(gold, cands, 3)
        h5 = hit_at_k(gold, cands, 5)
        fgr = first_gold_rank(gold, cands)
        fgr_s = str(fgr) if fgr != float("inf") else "inf"

        results.append({"tid": tid, "h1": h1, "h3": h3, "h5": h5, "fgr": fgr, "cands": cands, "gold": gold})
        print(f"| {i} | {tid} | {len(gold)} | {len(cands)} | {'Y' if h1 else '-'} | {'Y' if h3 else '-'} | {'Y' if h5 else '-'} | {fgr_s} |")

    n = len(results)
    h1_total = sum(r["h1"] for r in results)
    h3_total = sum(r["h3"] for r in results)
    h5_total = sum(r["h5"] for r in results)
    print()
    print(f"V1R hit@1: {h1_total}/{n} ({100*h1_total/n:.1f}%)")
    print(f"V1R hit@3: {h3_total}/{n} ({100*h3_total/n:.1f}%)")
    print(f"V1R hit@5: {h5_total}/{n} ({100*h5_total/n:.1f}%)")
    print()
    print("Comparison: v7 was 3/30 (10%) hit@1, 10/30 (33%) hit@3")


if __name__ == "__main__":
    main()
