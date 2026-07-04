#!/usr/bin/env python3
"""Analyze identifier extraction coverage on SWE-bench Verified.

Runs extract_identifiers_from_issue on all 500 tasks locally.
Reports how many return empty (= guaranteed zero GT delta).
Identifies regex gaps and failure patterns.

Usage:
    python scripts/swebench/analyze_briefing_gap.py
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

# Import extract_identifiers_from_issue from gt_intel.py
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "benchmarks" / "swebench"))
from gt_intel import extract_identifiers_from_issue, _NOISE_WORDS

try:
    from datasets import load_dataset
except ImportError:
    print("ERROR: pip install datasets", file=sys.stderr)
    sys.exit(1)


def extract_traceback_functions(text: str) -> list[str]:
    """Extract function names from Python tracebacks (not covered by current regexes)."""
    # Pattern: File "...", line X, in function_name
    return re.findall(r'File ".+?", line \d+, in (\w+)', text)


def analyze_issue(issue_text: str) -> dict:
    """Analyze a single issue text for identifier extraction."""
    identifiers = extract_identifiers_from_issue(issue_text)
    traceback_funcs = extract_traceback_functions(issue_text)

    # Check what the current regexes miss
    has_backtick = bool(re.findall(r'`([a-zA-Z_][\w.]*)`', issue_text))
    has_camelcase = bool(re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', issue_text))
    has_filepath = bool(re.findall(r'[\w/]+\.(?:py|go|js|ts|rs|java)\b', issue_text))
    has_snakecase = bool(re.findall(r'\b([a-z]+_[a-z_]+)\b', issue_text))
    has_error_class = bool(re.findall(r'\b(\w+(?:Error|Exception))\b', issue_text))
    has_traceback = "Traceback" in issue_text or 'File "' in issue_text

    # Check for single-word identifiers filtered as noise
    all_backtick = re.findall(r'`(\w+)`', issue_text)
    filtered_by_noise = [w for w in all_backtick if w in _NOISE_WORDS]

    return {
        "identifiers": identifiers,
        "count": len(identifiers),
        "empty": len(identifiers) == 0,
        "traceback_funcs": traceback_funcs,
        "traceback_funcs_not_in_identifiers": [
            f for f in traceback_funcs
            if f not in identifiers and f not in _NOISE_WORDS and len(f) >= 3
        ],
        "has_backtick": has_backtick,
        "has_camelcase": has_camelcase,
        "has_filepath": has_filepath,
        "has_snakecase": has_snakecase,
        "has_error_class": has_error_class,
        "has_traceback": has_traceback,
        "filtered_by_noise": filtered_by_noise,
    }


def main():
    print("Loading SWE-bench Verified...")
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    print(f"Loaded {len(ds)} tasks\n")

    results = []
    empty_tasks = []
    traceback_recovery = []

    for row in ds:
        instance_id = row["instance_id"]
        issue_text = row["problem_statement"]
        analysis = analyze_issue(issue_text)
        analysis["instance_id"] = instance_id
        results.append(analysis)

        if analysis["empty"]:
            empty_tasks.append(analysis)
        if analysis["traceback_funcs_not_in_identifiers"]:
            traceback_recovery.append(analysis)

    # Summary
    total = len(results)
    empty_count = len(empty_tasks)
    non_empty = total - empty_count

    print("=" * 70)
    print("  IDENTIFIER EXTRACTION COVERAGE")
    print("=" * 70)
    print(f"  Total tasks:          {total}")
    print(f"  Non-empty extraction: {non_empty} ({100*non_empty/total:.1f}%)")
    print(f"  Empty extraction:     {empty_count} ({100*empty_count/total:.1f}%)")
    print()

    # Distribution of identifier counts
    count_dist = Counter(r["count"] for r in results)
    print("  Identifier count distribution:")
    for n in sorted(count_dist.keys())[:15]:
        bar = "#" * min(count_dist[n], 50)
        print(f"    {n:3d}: {count_dist[n]:4d} {bar}")
    print()

    # Pattern coverage
    pattern_counts = {
        "backtick": sum(1 for r in results if r["has_backtick"]),
        "camelcase": sum(1 for r in results if r["has_camelcase"]),
        "filepath": sum(1 for r in results if r["has_filepath"]),
        "snakecase": sum(1 for r in results if r["has_snakecase"]),
        "error_class": sum(1 for r in results if r["has_error_class"]),
        "traceback": sum(1 for r in results if r["has_traceback"]),
    }
    print("  Pattern coverage (tasks with at least one match):")
    for pattern, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"    {pattern:15s}: {count:4d} ({100*count/total:.1f}%)")
    print()

    # Empty tasks analysis
    if empty_tasks:
        print("-" * 70)
        print(f"  EMPTY EXTRACTION TASKS ({empty_count})")
        print("-" * 70)
        for t in empty_tasks[:20]:
            # Show first 100 chars of issue
            preview = t.get("instance_id", "?")
            print(f"    {preview}")
        if empty_count > 20:
            print(f"    ... and {empty_count - 20} more")
        print()

        # Why empty? Check if tracebacks could help
        empty_with_tb = sum(1 for t in empty_tasks if t["has_traceback"])
        print(f"  Empty tasks with tracebacks: {empty_with_tb}")
        print()

    # Traceback recovery opportunity
    if traceback_recovery:
        print("-" * 70)
        print(f"  TRACEBACK RECOVERY OPPORTUNITY ({len(traceback_recovery)} tasks)")
        print("-" * 70)
        print("  These tasks have function names in tracebacks not captured by current regexes:")
        for t in traceback_recovery[:10]:
            funcs = t["traceback_funcs_not_in_identifiers"][:3]
            print(f"    {t['instance_id']:50s} -> {', '.join(funcs)}")
        print()
        total_recoverable = sum(len(t["traceback_funcs_not_in_identifiers"]) for t in traceback_recovery)
        print(f"  Total recoverable functions: {total_recoverable}")
        print()

    # Noise word filtering impact
    all_filtered = []
    for r in results:
        all_filtered.extend(r["filtered_by_noise"])
    if all_filtered:
        noise_dist = Counter(all_filtered).most_common(15)
        print("-" * 70)
        print("  NOISE WORD FILTERING (backtick-quoted words filtered as noise)")
        print("-" * 70)
        for word, count in noise_dist:
            print(f"    {word:20s}: filtered {count} times")
        print()

    # Repo distribution of empty tasks
    if empty_tasks:
        repo_dist = Counter(t["instance_id"].rsplit("-", 1)[0] for t in empty_tasks)
        print("-" * 70)
        print("  EMPTY TASKS BY REPO")
        print("-" * 70)
        for repo, count in repo_dist.most_common():
            print(f"    {repo:40s}: {count}")
        print()

    # Recommendations
    print("=" * 70)
    print("  RECOMMENDATIONS")
    print("=" * 70)
    tb_count = sum(1 for t in traceback_recovery if t["empty"])
    print(f"  1. Add traceback parsing: File '...', line X, in func_name")
    print(f"     Would recover functions from {len(traceback_recovery)} tasks")
    print(f"     Would fix {tb_count} currently-empty tasks")
    print()

    # Check for single-hump CamelCase (current regex needs 2+ humps)
    single_hump = 0
    for r in results:
        if r["empty"]:
            issue = ds[results.index(r)]["problem_statement"]
            if re.findall(r'\b([A-Z][a-z]{2,})\b', issue):
                single_hump += 1
    print(f"  2. Expand CamelCase to single-hump (e.g., 'Model', 'View')")
    print(f"     {single_hump} empty tasks have single-hump CamelCase words")
    print()


if __name__ == "__main__":
    main()
