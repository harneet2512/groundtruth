#!/usr/bin/env python3
"""Select a representative 50-task subset of SWE-bench_Lite for A/B testing.

Mirrors the full 300-task distribution by repo. Outputs a file with one instance_id per line.

Usage:
    python3 scripts/swebench/select_50.py --output fifty_tasks.txt
    python3 scripts/swebench/select_50.py --output fifty_tasks.txt --seed 42
"""
import argparse
import random
import sys
from collections import Counter


# Full SWE-bench_Lite distribution (300 tasks, approximate repo counts)
# Target 50-task distribution: proportional sampling per repo
TARGET_DISTRIBUTION = {
    "django__django": 19,       # ~114/300 → 19/50
    "sympy__sympy": 13,         # ~77/300  → 13/50
    "scikit-learn__scikit-learn": 5,  # ~30/300 → 5/50
    "matplotlib__matplotlib": 4, # ~23/300 → 4/50
    "pytest-dev__pytest": 3,    # ~17/300 → 3/50
    "sphinx-doc__sphinx": 3,    # ~16/300 → 3/50
    "astropy__astropy": 1,      # ~6/300  → 1/50
    "requests__requests": 1,    # ~3/300  → 1/50
    "psf__requests": 1,         # ~3/300  → 1/50
}
# Remaining slots filled from largest repos


def get_repo(instance_id: str) -> str:
    """Extract repo name from instance_id like 'django__django-12856'."""
    parts = instance_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else instance_id


def select_tasks(n: int = 50, seed: int = 42) -> list[str]:
    """Select n representative tasks from SWE-bench_Lite."""
    try:
        from datasets import load_dataset
        ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    except ImportError:
        print("ERROR: pip install datasets  (required to load SWE-bench_Lite)")
        sys.exit(1)

    # Group by repo
    by_repo: dict[str, list[str]] = {}
    for row in ds:
        repo = get_repo(row["instance_id"])
        by_repo.setdefault(repo, []).append(row["instance_id"])

    rng = random.Random(seed)

    # Shuffle each repo's tasks
    for repo in by_repo:
        rng.shuffle(by_repo[repo])

    selected = []
    remaining_slots = n

    # First pass: fill target distribution
    for repo, count in sorted(TARGET_DISTRIBUTION.items(), key=lambda x: -x[1]):
        if repo in by_repo:
            take = min(count, len(by_repo[repo]), remaining_slots)
            selected.extend(by_repo[repo][:take])
            by_repo[repo] = by_repo[repo][take:]
            remaining_slots -= take

    # Second pass: fill remaining slots from largest repos
    if remaining_slots > 0:
        all_remaining = []
        for repo, tasks in sorted(by_repo.items(), key=lambda x: -len(x[1])):
            all_remaining.extend(tasks)
        rng.shuffle(all_remaining)
        selected.extend(all_remaining[:remaining_slots])

    # Final shuffle
    rng.shuffle(selected)

    return selected[:n]


def main():
    parser = argparse.ArgumentParser(description="Select representative 50-task subset")
    parser.add_argument("--output", "-o", default="fifty_tasks.txt", help="Output file path")
    parser.add_argument("--count", "-n", type=int, default=50, help="Number of tasks to select")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--show-distribution", action="store_true", help="Print repo distribution")

    args = parser.parse_args()

    selected = select_tasks(args.count, args.seed)

    with open(args.output, "w") as f:
        for task_id in selected:
            f.write(task_id + "\n")

    print(f"Selected {len(selected)} tasks → {args.output}")

    if args.show_distribution:
        dist = Counter(get_repo(t) for t in selected)
        print("\nDistribution:")
        for repo, count in dist.most_common():
            print(f"  {repo}: {count}")


if __name__ == "__main__":
    main()
