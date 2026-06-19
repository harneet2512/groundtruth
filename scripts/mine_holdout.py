"""Mine 60-bug holdout dataset for v7.4 full evaluation.

Reads holdout_prs.jsonl, groups by repo, and runs one worker per repo in
parallel (5 total). Within each repo group, bugs are mined sequentially
because git checkout is stateful.

Output is appended to holdout_v1.jsonl incrementally (crash-safe).

Run:
    python scripts/mine_holdout.py
        [--prs holdout_prs.jsonl]
        [--out holdout_v1.jsonl]
        [--work-dir D:/Groundtruth/.tmp_holdout]
        [--gt-index-bin <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mine_feasibility_tranche import mine_bug

# Bugs already in feasibility tranche — always skip.
_TRANCHE_IDS = {
    "axum-3645", "axum-3664", "axum-3611",
    "crossplane-7208", "crossplane-7241",
    "hono-4894", "hono-4807", "hono-4770",
    "dagster-33605", "dagster-33514", "dagster-33480",
    "marimo-9276", "marimo-9228", "marimo-9072",
}

_FILE_LOCK = threading.Lock()


def _mine_repo_group(
    group: list[dict],
    work_dir: Path,
    gt_bin: str,
    out_path: Path,
    existing_ids: set[str],
) -> list[str]:
    """Mine one repo's bugs sequentially; return list of newly mined bug_ids."""
    git_rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()

    mined: list[str] = []
    for rec in group:
        repo = rec["repo"]
        pr_num = rec["pr_number"]
        issue_num = rec.get("issue_number") or 0
        lang = rec["language"]
        bug_id = f"{repo.split('/')[1]}-{pr_num}"

        skip_set = existing_ids | _TRANCHE_IDS
        if bug_id in skip_set:
            print(f"  [skip] {bug_id}")
            continue

        try:
            result = mine_bug(
                repo=repo,
                pr_number=pr_num,
                issue_number=issue_num,
                lang=lang,
                work_dir=work_dir,
                gt_index_bin=gt_bin,
                existing_ids=skip_set,
            )
            if result:
                result.git_rev = git_rev
                row = json.dumps(asdict(result))
                with _FILE_LOCK:
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(row + "\n")
                    existing_ids.add(bug_id)
                mined.append(bug_id)
                print(f"  [saved] {bug_id} -> {out_path.name}")
        except Exception as exc:
            import traceback
            print(f"  [error] {bug_id}: {exc}", file=sys.stderr)
            traceback.print_exc()

    return mined


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prs", default="holdout_prs.jsonl")
    parser.add_argument("--out", default="holdout_v1.jsonl")
    parser.add_argument("--work-dir", default="D:/Groundtruth/.tmp_holdout")
    parser.add_argument("--gt-index-bin", default="")
    args = parser.parse_args()

    gt_bin = args.gt_index_bin or os.environ.get(
        "GT_INDEX_BIN", "D:/Groundtruth/gt-index/gt-index.exe"
    )
    if not Path(gt_bin).exists():
        print(f"ERROR: gt-index not found at {gt_bin}", file=sys.stderr)
        return 1

    prs_path = Path(args.prs)
    if not prs_path.exists():
        print(f"ERROR: {prs_path} not found — run enumerate_prs.py first", file=sys.stderr)
        return 1

    prs: list[dict] = []
    with open(prs_path) as f:
        for line in f:
            line = line.strip()
            if line:
                prs.append(json.loads(line))

    out_path = Path(args.out)
    existing_ids: set[str] = set()
    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    existing_ids.add(json.loads(line)["bug_id"])

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Symlink the already-cloned repos from the feasibility tranche work dir so
    # gt-index doesn't need to re-clone them.
    tranche_dir = Path("D:/Groundtruth/.tmp_tranche")
    for repo_dir in tranche_dir.glob("*/"):
        if (repo_dir / ".git").exists():
            link = work_dir / repo_dir.name
            if not link.exists():
                try:
                    link.symlink_to(repo_dir.resolve())
                    print(f"  [link] {link.name} -> {repo_dir}")
                except Exception:
                    pass  # Windows may need admin for symlinks — fall back to re-clone

    print(f"Loaded {len(existing_ids)} already-mined bugs; {len(prs)} PRs to consider")

    # Group by repo
    groups: dict[str, list[dict]] = {}
    for rec in prs:
        groups.setdefault(rec["repo"], []).append(rec)

    total_mined: list[str] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _mine_repo_group,
                group, work_dir, gt_bin, out_path, existing_ids,
            ): repo
            for repo, group in groups.items()
        }
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                mined = fut.result()
                total_mined.extend(mined)
                print(f"  [{repo}] mined {len(mined)} bugs")
            except Exception as exc:
                print(f"  [error] {repo}: {exc}", file=sys.stderr)

    print(f"\nDone. Mined {len(total_mined)} new bugs -> {out_path}")
    # Final count in file
    final_count = sum(1 for _ in open(out_path) if _.strip())
    print(f"Total in {out_path.name}: {final_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
