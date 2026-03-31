#!/usr/bin/env python3
"""Generate instance shard files for SWE-bench Verified evaluation.

Loads all instance IDs from the dataset, sorts alphabetically, and splits
into two equal shards for parallel evaluation on 2 VMs.

Also creates an instances_all.txt with all IDs for single-VM runs.

Usage:
    python oh_generate_shards.py [--output-dir .]
"""

import argparse
from pathlib import Path

from datasets import load_dataset


def main():
    parser = argparse.ArgumentParser(description="Generate SWE-bench Verified shards")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Directory to write shard files (default: current dir)",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Verified",
        help="HuggingFace dataset name",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="test",
        help="Dataset split",
    )
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ds = load_dataset(args.dataset, split=args.split)
    instance_ids = sorted(ds["instance_id"])

    total = len(instance_ids)
    mid = total // 2

    shard_a = instance_ids[:mid]
    shard_b = instance_ids[mid:]

    (out / "instances_all.txt").write_text("\n".join(instance_ids) + "\n")
    (out / "instances_a.txt").write_text("\n".join(shard_a) + "\n")
    (out / "instances_b.txt").write_text("\n".join(shard_b) + "\n")

    print(f"Total instances: {total}")
    print(f"Shard A: {len(shard_a)} (first: {shard_a[0]}, last: {shard_a[-1]})")
    print(f"Shard B: {len(shard_b)} (first: {shard_b[0]}, last: {shard_b[-1]})")
    print(f"Written to: {out.resolve()}")


if __name__ == "__main__":
    main()
