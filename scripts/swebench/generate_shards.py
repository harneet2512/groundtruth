#!/usr/bin/env python3
"""Generate deterministic repo-sorted shard files for 2-VM SWE-bench run.

Shard A: django + astropy + pylint + pallets (~129 tasks, faster per-task)
Shard B: sympy + matplotlib + sklearn + pytest + sphinx + psf + pydata + mwaskom (~171 tasks)

Tasks within each shard are sorted by repo for optimal Docker cache reuse.

Usage:
    python3 scripts/swebench/generate_shards.py --output-dir /tmp
"""
import argparse
import sys


SHARD_A_REPOS = {"django", "astropy", "pylint-dev", "pallets"}
# Everything else goes to shard B


def main():
    parser = argparse.ArgumentParser(description="Generate repo-sorted shards")
    parser.add_argument("--output-dir", default="/tmp", help="Output directory")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    args = parser.parse_args()

    try:
        from datasets import load_dataset
        ds = load_dataset(args.dataset, split="test")
    except ImportError:
        print("ERROR: pip install datasets")
        sys.exit(1)

    shard_a = []
    shard_b = []

    for row in ds:
        iid = row["instance_id"]
        repo = iid.split("__")[0]
        if repo in SHARD_A_REPOS:
            shard_a.append(iid)
        else:
            shard_b.append(iid)

    # Sort by repo then instance_id for contiguous repo batches
    shard_a.sort(key=lambda x: (x.split("__")[0], x))
    shard_b.sort(key=lambda x: (x.split("__")[0], x))

    a_path = f"{args.output_dir}/shard_a.txt"
    b_path = f"{args.output_dir}/shard_b.txt"

    with open(a_path, "w") as f:
        f.writelines(i + "\n" for i in shard_a)
    with open(b_path, "w") as f:
        f.writelines(i + "\n" for i in shard_b)

    print(f"Shard A: {len(shard_a)} tasks -> {a_path}")
    print(f"Shard B: {len(shard_b)} tasks -> {b_path}")

    # Show distribution
    from collections import Counter
    for name, shard in [("Shard A", shard_a), ("Shard B", shard_b)]:
        dist = Counter(x.split("__")[0] for x in shard)
        repos = ", ".join(f"{r}({c})" for r, c in dist.most_common())
        print(f"  {name}: {repos}")


if __name__ == "__main__":
    main()
