"""Mine feasibility-tranche bugs for v7.4 evaluation.

For each selected (repo, PR, issue) triple:
  1. Fetch PR + issue metadata via gh CLI
  2. Clone repo at parent-of-fix commit (reuse if already cloned)
  3. Run gt-index to produce graph.db
  4. Record metadata to holdout_feasibility.jsonl

Run:
    python scripts/mine_feasibility_tranche.py [--out holdout_feasibility.jsonl]

Requires: gh CLI authenticated, gt-index.exe in path or GT_INDEX_BIN env,
          git, python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

BUGS = [
    # (repo, pr_number, issue_number, language)
    # --- axum (Rust) ---
    ("tokio-rs/axum", 3645, 3644, "rust"),
    ("tokio-rs/axum", 3664, 3646, "rust"),
    ("tokio-rs/axum", 3611, 3216, "rust"),
    # --- crossplane (Go) ---
    ("crossplane/crossplane", 7208, 7207, "go"),
    ("crossplane/crossplane", 7241, 7240, "go"),
    # --- hono (TypeScript) ---
    ("honojs/hono", 4894, 4892, "typescript"),
    ("honojs/hono", 4807, 4806, "typescript"),
    ("honojs/hono", 4770, 4769, "typescript"),
    # --- dagster (Python) ---
    ("dagster-io/dagster", 33605, 33584, "python"),
    ("dagster-io/dagster", 33514, 33511, "python"),
    ("dagster-io/dagster", 33480, 32925, "python"),
    # --- marimo (Python) ---
    ("marimo-team/marimo", 9276, 9274, "python"),
    ("marimo-team/marimo", 9228, 9226, "python"),
    ("marimo-team/marimo", 9072, 9004, "python"),
]

# Source-only file extensions per language (used to filter gold set)
_SOURCE_EXTS = {
    "rust": {".rs"},
    "go": {".go"},
    "typescript": {".ts"},
    "python": {".py"},
}
_ALL_SOURCE_EXTS = {".rs", ".go", ".ts", ".tsx", ".js", ".py"}

_TEST_PATTERNS = re.compile(r"test|spec|fixture|mock", re.I)
_CI_INFRA_DIRS = re.compile(
    r"^\.buildkite/|^\.github/|^\.circleci/|^\.travis|^ci/|^\.gitlab",
    re.I,
)


def _is_source_file(path: str, lang: str) -> bool:
    p = Path(path)
    if p.suffix not in _SOURCE_EXTS.get(lang, _ALL_SOURCE_EXTS):
        return False
    # Exclude test/spec files — the gold set is fix-site files
    if _TEST_PATTERNS.search(path):
        return False
    # Exclude CI/infra config dirs — not application fix sites
    if _CI_INFRA_DIRS.search(path):
        return False
    return True


def _run(cmd: list[str], check: bool = True, capture: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=capture, text=True, check=check,
        encoding="utf-8", errors="replace", **kw
    )


def _gh_json(args: list[str], fields: list[str]) -> dict:
    r = _run(["gh"] + args + ["--json", ",".join(fields)])
    return json.loads(r.stdout)  # type: ignore[return-value]


def fetch_pr(repo: str, pr_number: int) -> dict:
    return _gh_json(
        ["pr", "view", str(pr_number), "--repo", repo],
        ["number", "title", "mergeCommit", "baseRefOid", "files", "body"],
    )


def fetch_issue(repo: str, issue_number: int) -> dict:
    return _gh_json(
        ["issue", "view", str(issue_number), "--repo", repo],
        ["number", "title", "body", "state"],
    )


def ensure_clone(repo: str, work_dir: Path) -> Path:
    """Clone repo to work_dir/<repo_name> if not already present."""
    repo_name = repo.split("/")[1]
    repo_path = work_dir / repo_name
    if repo_path.exists() and (repo_path / ".git").exists():
        print(f"  [clone] reusing {repo_path}")
    else:
        print(f"  [clone] cloning {repo} -> {repo_path}")
        _run(["git", "clone", f"https://github.com/{repo}", str(repo_path)], capture=False)
    return repo_path


def checkout_commit(repo_path: Path, commit: str) -> None:
    _run(["git", "-C", str(repo_path), "checkout", commit], capture=False)


def count_source_files(repo_path: Path, lang: str) -> int:
    """Count on-disk source files; avoids git ls-files which over-counts in sparse checkouts."""
    exts = _SOURCE_EXTS.get(lang, _ALL_SOURCE_EXTS)
    total = 0
    for _root, dirs, files in os.walk(repo_path):
        # Skip hidden directories (like .git, .buildkite, .github)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            fp = Path(fname)
            if fp.suffix in exts and not _TEST_PATTERNS.search(fname):
                total += 1
    return total


def run_gt_index(repo_path: Path, output_db: Path, gt_index_bin: str) -> dict:
    """Run gt-index and return stats from the JSON line at the end."""
    t0 = time.time()
    r = _run([gt_index_bin, "-root", str(repo_path), "-output", str(output_db)])
    elapsed = time.time() - t0
    # Last line of stdout is JSON stats
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    stats = {}
    for line in reversed(lines):
        try:
            stats = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    stats["elapsed_s"] = round(elapsed, 2)
    return stats


def get_indexed_file_count(db_path: Path, lang: str) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    ext_map = {"rust": "rust", "go": "go", "typescript": "typescript", "python": "python"}
    lang_str = ext_map.get(lang, lang)
    c.execute("SELECT COUNT(DISTINCT file_path) FROM nodes WHERE language = ?", (lang_str,))
    count = c.fetchone()[0]
    conn.close()
    return count


def extract_gold_files_from_patch(repo_path: Path, parent: str, merge: str, lang: str) -> list[str]:
    """Get files changed between parent and merge commit (source files only)."""
    r = _run(
        ["git", "-C", str(repo_path), "diff", "--name-only", parent, merge],
        check=False,
    )
    if r.returncode != 0:
        return []
    files = [f.strip() for f in r.stdout.splitlines() if f.strip()]
    return [f for f in files if _is_source_file(f, lang)]


@dataclass
class BugRecord:
    bug_id: str
    repo: str
    language: str
    pr_number: int
    issue_number: int
    parent_commit: str
    merge_commit: str
    issue_title: str
    issue_body: str
    gold_files: list[str]
    graph_db_path: str
    repo_path: str
    file_coverage: float
    total_source_files: int
    indexed_files: int
    gt_index_stats: dict
    git_rev: str = ""  # GT mining script git rev


def mine_bug(
    repo: str,
    pr_number: int,
    issue_number: int,
    lang: str,
    work_dir: Path,
    gt_index_bin: str,
    existing_ids: set[str],
) -> Optional[BugRecord]:
    bug_id = f"{repo.split('/')[1]}-{pr_number}"
    if bug_id in existing_ids:
        print(f"  [skip] {bug_id} already in output")
        return None

    print(f"\n{'='*60}")
    print(f"Mining {bug_id} ({repo} PR#{pr_number} issue#{issue_number})")

    # 1. Fetch PR + issue metadata
    print("  [gh] fetching PR metadata...")
    pr = fetch_pr(repo, pr_number)
    parent_commit = pr["baseRefOid"]
    merge_commit = pr["mergeCommit"]["oid"] if pr.get("mergeCommit") else ""

    print("  [gh] fetching issue metadata...")
    issue = fetch_issue(repo, issue_number)

    # 2. Clone / reuse
    repo_path = ensure_clone(repo, work_dir)

    # 3. Checkout parent
    print(f"  [git] checking out parent {parent_commit[:12]}...")
    checkout_commit(repo_path, parent_commit)

    # 4. Gold files from diff (need merge commit)
    gold_files: list[str] = []
    if merge_commit:
        print(f"  [git] extracting gold files from diff {parent_commit[:8]}..{merge_commit[:8]}...")
        # Fetch the merge commit if not present
        _run(
            ["git", "-C", str(repo_path), "fetch", "origin", merge_commit],
            check=False,
        )
        gold_files = extract_gold_files_from_patch(repo_path, parent_commit, merge_commit, lang)

    if not gold_files:
        # Fallback: use PR files field (source only)
        gold_files = [
            f["path"] for f in pr.get("files", []) if _is_source_file(f["path"], lang)
        ]

    if not gold_files:
        print(f"  [warn] no source gold files found, skipping {bug_id}")
        return None

    print(f"  [gold] {gold_files}")

    # 5. Run gt-index
    bug_dir = work_dir / "bugs" / bug_id
    bug_dir.mkdir(parents=True, exist_ok=True)
    db_path = bug_dir / "graph.db"

    if db_path.exists():
        print(f"  [index] reusing {db_path}")
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute("SELECT value FROM metadata WHERE key='files'")
        row = c.fetchone()
        conn.close()
        gt_stats = {"files": int(row[0]) if row else 0}
    else:
        print(f"  [index] running gt-index -> {db_path}...")
        gt_stats = run_gt_index(repo_path, db_path, gt_index_bin)
        print(f"  [index] {gt_stats}")

    # 6. Coverage check
    total_src = count_source_files(repo_path, lang)
    indexed = get_indexed_file_count(db_path, lang)
    coverage = indexed / max(total_src, 1)
    print(f"  [coverage] {indexed}/{total_src} = {coverage:.3f}")

    if coverage < 0.80:
        print(f"  [warn] coverage {coverage:.3f} < 0.80 threshold!")

    return BugRecord(
        bug_id=bug_id,
        repo=repo,
        language=lang,
        pr_number=pr_number,
        issue_number=issue_number,
        parent_commit=parent_commit,
        merge_commit=merge_commit,
        issue_title=issue["title"],
        issue_body=issue["body"],
        gold_files=gold_files,
        graph_db_path=str(db_path.absolute()),
        repo_path=str(repo_path.absolute()),
        file_coverage=round(coverage, 4),
        total_source_files=total_src,
        indexed_files=indexed,
        gt_index_stats=gt_stats,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="holdout_feasibility.jsonl")
    parser.add_argument("--work-dir", default="D:/Groundtruth/.tmp_tranche")
    parser.add_argument("--gt-index-bin", default="")
    parser.add_argument("--bug", default="", help="Mine only this bug_id (for debugging)")
    args = parser.parse_args()

    gt_bin = args.gt_index_bin
    if not gt_bin:
        gt_bin = os.environ.get("GT_INDEX_BIN", "D:/Groundtruth/gt-index/gt-index.exe")
    if not Path(gt_bin).exists():
        print(f"ERROR: gt-index binary not found at {gt_bin}", file=sys.stderr)
        return 1

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    out_path = Path(args.out)
    existing_ids: set[str] = set()
    records: list[BugRecord] = []

    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    existing_ids.add(rec["bug_id"])
                    print(f"  [existing] {rec['bug_id']}")

    # Get mining script git rev
    git_rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True
    ).stdout.strip()

    for repo, pr_num, issue_num, lang in BUGS:
        bug_id = f"{repo.split('/')[1]}-{pr_num}"
        if args.bug and args.bug != bug_id:
            continue
        try:
            rec = mine_bug(repo, pr_num, issue_num, lang, work_dir,
                           gt_bin, existing_ids)
            if rec:
                rec.git_rev = git_rev
                records.append(rec)
                # Append immediately (crash-safe)
                with open(out_path, "a") as f:
                    f.write(json.dumps(asdict(rec)) + "\n")
                print(f"  [saved] {bug_id} -> {out_path}")
        except Exception as e:
            print(f"  [error] {bug_id}: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Done. Wrote {len(records)} new records to {out_path}")
    total = len(existing_ids) + len(records)
    print(f"Total bugs in file: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
