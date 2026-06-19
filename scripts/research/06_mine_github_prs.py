"""Mine bug-fix PRs from large non-benchmark GitHub repos.

Collects issue text + gold files (changed source files) for G1/G3 cross-validation.
Uses `gh pr list` + `gh pr view` (faster than search API for massive repos).

Target repos (confirmed NOT in any SWE-bench split):
  kubernetes/kubernetes (Go, ~20K files)
  pytorch/pytorch (Python/C++, ~15K files)
  microsoft/TypeScript (TypeScript, ~3K files)
  rust-lang/rust (Rust, ~30K files)
  golang/go (Go, ~10K files)

Output: research/big_repo_routing/mined_prs.jsonl
"""

import json
import re
import subprocess
import time
from pathlib import Path

OUTPUT_PATH = Path(__file__).resolve().parents[2] / "research" / "big_repo_routing" / "mined_prs.jsonl"

SWEBENCH_REPOS = {
    "django/django", "sympy/sympy", "scikit-learn/scikit-learn",
    "matplotlib/matplotlib", "pallets/requests", "pallets/flask",
    "sphinx-doc/sphinx", "astropy/astropy", "pylint-dev/pylint",
    "pydata/xarray", "pytest-dev/pytest", "mwaskom/seaborn",
    "pandas-dev/pandas", "psf/requests", "pallets/click",
    "pypa/setuptools",
}

TARGET_REPOS = [
    {"repo": "kubernetes/kubernetes", "language": "Go"},
    {"repo": "pytorch/pytorch", "language": "Python"},
    {"repo": "microsoft/TypeScript", "language": "TypeScript"},
    {"repo": "rust-lang/rust", "language": "Rust"},
    {"repo": "golang/go", "language": "Go"},
]

SOURCE_EXTENSIONS = {
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb",
}

TEST_INDICATORS = {"test", "spec", "_test", "test_", "tests/", "testing/"}


def run_gh(args, timeout=60):
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8"
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_source_file(path):
    return any(path.endswith(ext) for ext in SOURCE_EXTENSIONS)


def is_test_file(path):
    path_lower = path.lower()
    return any(ind in path_lower for ind in TEST_INDICATORS)


def get_candidate_prs(repo, limit=80):
    """Get merged PRs with 'fix' or 'bug' in title."""
    out = run_gh([
        "pr", "list", "--repo", repo,
        "--state", "merged",
        "--search", "fix bug in:title",
        "--limit", str(limit),
        "--json", "number,title,body,additions,deletions"
    ], timeout=90)

    if not out:
        return []
    return json.loads(out)


def get_pr_files(repo, pr_number):
    """Get changed files via gh api."""
    out = run_gh([
        "api", f"repos/{repo}/pulls/{pr_number}/files",
        "--paginate", "--jq", ".[].filename"
    ], timeout=30)

    if not out:
        return []
    return out.strip().split("\n")


def extract_issue_ref(body):
    """Extract issue number from PR body."""
    if not body:
        return None
    patterns = [
        r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)',
        r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+https://github\.com/[^/]+/[^/]+/issues/(\d+)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, body, re.IGNORECASE)
        if matches:
            return int(matches[0])
    return None


def get_issue_text(repo, issue_num):
    """Fetch issue title + body."""
    out = run_gh([
        "api", f"repos/{repo}/issues/{issue_num}",
        "--jq", '{title: .title, body: .body, is_pr: (has("pull_request"))}'
    ], timeout=30)

    if not out:
        return None, None
    data = json.loads(out)
    if data.get("is_pr"):
        return None, None
    return data.get("title", ""), data.get("body", "")


def mine_repo(repo_info):
    repo = repo_info["repo"]
    language = repo_info["language"]

    print(f"\n{'='*60}")
    print(f"Mining {repo} ({language})")
    print(f"{'='*60}")

    assert repo not in SWEBENCH_REPOS, f"SAFETY: {repo} is in SWE-bench!"

    candidates = get_candidate_prs(repo)
    print(f"  Found {len(candidates)} candidate PRs")

    mined = []
    skipped = {"too_large": 0, "no_source": 0, "no_issue_text": 0, "api_error": 0}

    for i, pr in enumerate(candidates):
        if len(mined) >= 25:
            break

        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        pr_body = pr.get("body", "") or ""
        additions = pr.get("additions", 0)
        deletions = pr.get("deletions", 0)

        if (additions + deletions) > 500:
            skipped["too_large"] += 1
            continue

        print(f"  [{i+1}] PR #{pr_number}: {pr_title[:55]}...", end="", flush=True)
        time.sleep(0.3)

        files = get_pr_files(repo, pr_number)
        if not files or files == [""]:
            print(" SKIP (no files)")
            skipped["api_error"] += 1
            continue

        if len(files) > 50:
            print(" SKIP (>50 files)")
            skipped["too_large"] += 1
            continue

        source_files = [f for f in files if is_source_file(f) and not is_test_file(f)]
        test_files = [f for f in files if is_test_file(f)]

        if not source_files:
            print(" SKIP (no src)")
            skipped["no_source"] += 1
            continue

        # Try to get linked issue text
        issue_num = extract_issue_ref(pr_body)
        issue_title = None
        issue_body = None

        if issue_num:
            issue_title, issue_body = get_issue_text(repo, issue_num)
            time.sleep(0.3)

        # Fall back to PR title+body as issue text
        final_title = issue_title or pr_title
        final_body = issue_body or pr_body

        if not final_body or len(final_body.strip()) < 20:
            print(" SKIP (no text)")
            skipped["no_issue_text"] += 1
            continue

        record = {
            "repo": repo,
            "language": language,
            "pr_number": pr_number,
            "pr_title": pr_title,
            "issue_number": issue_num,
            "issue_title": final_title,
            "issue_body": final_body,
            "gold_files": source_files,
            "test_files_changed": test_files,
            "total_files_changed": len(files),
            "additions": additions,
            "deletions": deletions,
            "text_source": "issue" if issue_body else "pr_body",
        }

        mined.append(record)
        src = "issue" if issue_body else "pr"
        print(f" OK ({len(source_files)} src, {src})")

    print(f"\n  Mined: {len(mined)}, Skipped: {skipped}")
    return mined


def main():
    print("GitHub PR Miner for G1/G3 Cross-Validation")
    print("=" * 60)
    print(f"Output: {OUTPUT_PATH}")

    for t in TARGET_REPOS:
        assert t["repo"] not in SWEBENCH_REPOS, f"SAFETY VIOLATION: {t['repo']} in SWE-bench"
    print("Safety check: all target repos confirmed NOT in SWE-bench\n")

    all_mined = []
    for repo_info in TARGET_REPOS:
        try:
            mined = mine_repo(repo_info)
            all_mined.extend(mined)
        except Exception as e:
            print(f"  ERROR mining {repo_info['repo']}: {e}")
            import traceback
            traceback.print_exc()
            continue

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for record in all_mined:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\n{'='*60}")
    print(f"MINING COMPLETE")
    print(f"{'='*60}")
    print(f"Total mined: {len(all_mined)}")

    from collections import Counter
    repo_counts = Counter(r["repo"] for r in all_mined)
    lang_counts = Counter(r["language"] for r in all_mined)
    has_issue = sum(1 for r in all_mined if r["issue_number"])
    avg_gold = sum(len(r["gold_files"]) for r in all_mined) / max(len(all_mined), 1)
    issue_src = Counter(r["text_source"] for r in all_mined)

    print(f"\nPer repo:")
    for repo, count in repo_counts.most_common():
        print(f"  {repo}: {count}")
    print(f"\nPer language:")
    for lang, count in lang_counts.most_common():
        print(f"  {lang}: {count}")
    print(f"\nText source: {dict(issue_src)}")
    print(f"With linked issue: {has_issue}/{len(all_mined)}")
    print(f"Avg gold files per PR: {avg_gold:.1f}")
    print(f"\nSaved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
