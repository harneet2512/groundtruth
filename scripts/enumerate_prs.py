"""Enumerate candidate PRs for v7.4 holdout mining.

Queries each of the 5 holdout repos for merged PRs since DATE_FLOOR,
filters to those with ≥1 non-test, non-CI source gold file, and emits
holdout_prs.jsonl.

Run:
    python scripts/enumerate_prs.py [--out holdout_prs.jsonl] [--target-per-repo 12]

Requires: gh CLI authenticated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mine_feasibility_tranche import _is_source_file, _gh_json

REPOS = [
    ("tokio-rs/axum", "rust"),
    ("crossplane/crossplane", "go"),
    ("honojs/hono", "typescript"),
    ("dagster-io/dagster", "python"),
    ("marimo-team/marimo", "python"),
]

# PR numbers already mined in the feasibility tranche — exclude from holdout.
TRANCHE_PRS: dict[str, set[int]] = {
    "tokio-rs/axum": {3645, 3664, 3611},
    "crossplane/crossplane": {7208, 7241},
    "honojs/hono": {4894, 4807, 4770},
    "dagster-io/dagster": {33605, 33514, 33480},
    "marimo-team/marimo": {9276, 9228, 9072},
}

DATE_FLOOR = "2025-09-01"

_CLOSES_RE = re.compile(
    r"(?:closes?|fixes?|resolves?)\s+#(\d+)", re.I
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _extract_issue_number(body: str, pr_number: int) -> int | None:
    for pat in (_CLOSES_RE, _ISSUE_REF_RE):
        m = pat.search(body or "")
        if m:
            n = int(m.group(1))
            if n != pr_number:
                return n
    return None


def _enumerate_repo(repo: str, lang: str, target: int) -> list[dict]:
    print(f"  [{repo}] querying merged PRs since {DATE_FLOOR}...")
    excluded = TRANCHE_PRS.get(repo, set())

    prs = _gh_json(
        [
            "pr", "list",
            "--repo", repo,
            "--state", "merged",
            "--limit", "300",
            "--search", f"merged:>={DATE_FLOOR}",
        ],
        ["number", "mergeCommit", "files", "title", "body"],
    )

    eligible: list[dict] = []
    for pr in prs:
        pr_num = int(pr["number"])
        if pr_num in excluded:
            continue
        gold = [
            f["path"]
            for f in (pr.get("files") or [])
            if _is_source_file(f["path"], lang)
        ]
        if not gold:
            continue
        body = pr.get("body") or ""
        issue_num = _extract_issue_number(body, pr_num)
        eligible.append({
            "repo": repo,
            "pr_number": pr_num,
            "issue_number": issue_num,
            "language": lang,
            "pr_title": pr.get("title", ""),
            "gold_file_count": len(gold),
        })
        if len(eligible) >= target:
            break

    print(f"  [{repo}] {len(eligible)} eligible PRs")
    return eligible


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="holdout_prs.jsonl")
    parser.add_argument("--target-per-repo", type=int, default=12)
    args = parser.parse_args()

    all_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_enumerate_repo, repo, lang, args.target_per_repo): repo
            for repo, lang in REPOS
        }
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                all_records.extend(fut.result())
            except Exception as exc:
                print(f"  [error] {repo}: {exc}", file=sys.stderr)

    out_path = Path(args.out)
    with open(out_path, "w") as f:
        for rec in all_records:
            f.write(json.dumps(rec) + "\n")

    print(f"\nWrote {len(all_records)} PR records -> {out_path}")
    by_repo: dict[str, int] = {}
    for rec in all_records:
        by_repo[rec["repo"]] = by_repo.get(rec["repo"], 0) + 1
    for repo, count in sorted(by_repo.items()):
        flag = "WARN" if count < 8 else "OK  "
        print(f"  [{flag}] {repo}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
