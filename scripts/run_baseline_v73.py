"""Run v7.3 brief on all bugs in holdout_v1.jsonl → baseline_holdout.csv.

Each bug is processed in parallel (one thread per bug). Results are written
to CSV incrementally (crash-safe via append + dedup).

Run:
    python scripts/run_baseline_v73.py
        [--input holdout_v1.jsonl]
        [--out results/baseline_holdout.csv]
        [--workers 8]
        [--resume]  # skip bug_ids already in output CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from groundtruth.pretask.v7_brief import generate_brief, V7BriefResult

_WRITE_LOCK = threading.Lock()

FIELDNAMES = [
    "bug_id", "repo", "ablation",
    "MRR_full", "hit1", "hit3", "hit5", "hit10",
    "gold_in_focus", "first_gold_rank_full", "candidate_set_size",
]


def _run_one(bug: dict) -> dict:
    issue_text = (bug.get("issue_body") or bug.get("issue_title") or "")
    repo_root = bug["repo_path"]
    graph_db = bug["graph_db_path"]
    gold_set = set(bug.get("gold_files") or [])

    result: V7BriefResult = generate_brief(  # type: ignore[assignment]
        issue_text, repo_root, graph_db, return_telemetry=True
    )

    # Extract ordered candidate file list from v7.3 result
    candidate_files: list[str] = []
    for cand in result.candidates:
        fp = getattr(cand, "file", None)
        if fp and fp not in candidate_files:
            candidate_files.append(fp)

    # Also include cluster_files that aren't already in candidates
    for fp in result.cluster_files or []:
        if fp not in candidate_files:
            candidate_files.append(fp)

    n = len(candidate_files)
    first_gold: int | None = None
    for i, fp in enumerate(candidate_files, start=1):
        if fp in gold_set:
            first_gold = i
            break

    mrr = (1.0 / first_gold) if first_gold else 0.0
    return {
        "bug_id": bug["bug_id"],
        "repo": bug["repo"],
        "ablation": "v73",
        "MRR_full": round(mrr, 4),
        "hit1": int(first_gold == 1) if first_gold else 0,
        "hit3": int(bool(first_gold and first_gold <= 3)),
        "hit5": int(bool(first_gold and first_gold <= 5)),
        "hit10": int(bool(first_gold and first_gold <= 10)),
        "gold_in_focus": int(bool(first_gold and first_gold <= 3)),
        "first_gold_rank_full": first_gold,
        "candidate_set_size": n,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="holdout_v1.jsonl")
    parser.add_argument("--out", default="results/baseline_holdout.csv")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 8))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: {in_path} not found", file=sys.stderr)
        return 1

    bugs: list[dict] = []
    with open(in_path) as f:
        for line in f:
            line = line.strip()
            if line:
                bugs.append(json.loads(line))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    done_ids: set[str] = set()
    if args.resume and out_path.exists():
        with open(out_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                done_ids.add(row["bug_id"])
        print(f"Resume: skipping {len(done_ids)} already-done bugs")

    write_header = not out_path.exists() or not done_ids
    pending = [b for b in bugs if b["bug_id"] not in done_ids]
    print(f"Running v7.3 baseline on {len(pending)} bugs (workers={args.workers})")

    errors = 0
    done = 0
    with open(out_path, "a", newline="") as csv_f:
        writer = csv.DictWriter(csv_f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_run_one, bug): bug["bug_id"] for bug in pending}
            for fut in as_completed(futures):
                bug_id = futures[fut]
                try:
                    row = fut.result()
                    with _WRITE_LOCK:
                        writer.writerow(row)
                        csv_f.flush()
                    done += 1
                    print(
                        f"  [{done}/{len(pending)}] {bug_id}"
                        f" MRR={row['MRR_full']:.4f}"
                        f" hit3={row['hit3']} cands={row['candidate_set_size']}"
                    )
                except Exception as exc:
                    errors += 1
                    print(f"  [error] {bug_id}: {exc}", file=sys.stderr)

    print(f"\nDone. {done} rows written to {out_path} ({errors} errors)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
