"""Generate the reproducible 20-task subset of SWE-bench-Live Lite for the baseline calibration.

Writes two artifacts:
  benchmarks/swebench/cal20_live_lite.txt            -- one instance_id per line
  benchmarks/swebench/cal20_live_lite.manifest.json  -- seed, pool hash, selection, timestamp

Run once. Never regenerate mid-calibration.

Usage:
  python scripts/make_cal20_manifest.py [--dataset NAME] [--split SPLIT] [--seed N] [--out-dir DIR]

Default dataset/split are the canonical SWE-bench-Live Lite pointers. If the canonical HF
path differs, override with --dataset. The preflight (scripts/vertex_preflight.py) verifies
the dataset identity before this script is trusted.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import random
import sys
from pathlib import Path


def _load_instance_ids(dataset: str, split: str) -> list[str]:
    try:
        from datasets import load_dataset
    except ImportError as e:
        sys.stderr.write(
            "ERROR: `datasets` package not installed. `pip install datasets` in the run venv.\n"
        )
        raise SystemExit(2) from e
    ds = load_dataset(dataset, split=split)
    ids = sorted({row["instance_id"] for row in ds})  # type: ignore
    if not ids:
        raise SystemExit(f"ERROR: loaded 0 instance_ids from {dataset}:{split}")
    return ids


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default="princeton-nlp/SWE-Bench_Lite")
    p.add_argument("--split", default="test")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--n", type=int, default=20)
    p.add_argument(
        "--out-dir",
        default="benchmarks/swebench",
        help="Directory to write cal20_live_lite.{txt,manifest.json}",
    )
    p.add_argument(
        "--out-txt",
        default=None,
        help="Explicit path for the .txt id list (overrides --out-dir default filename)",
    )
    p.add_argument(
        "--out-manifest",
        default=None,
        help="Explicit path for the .manifest.json (overrides --out-dir default filename)",
    )
    args = p.parse_args()

    pool = _load_instance_ids(args.dataset, args.split)
    pool_hash = hashlib.sha256("\n".join(pool).encode()).hexdigest()[:16]

    if args.n > len(pool):
        raise SystemExit(
            f"ERROR: requested n={args.n} > pool size {len(pool)} ({args.dataset}:{args.split})"
        )

    rng = random.Random(args.seed)
    sample = sorted(rng.sample(pool, args.n))
    if len(set(sample)) != args.n:
        raise SystemExit("ERROR: duplicates in sample; this should be impossible")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    txt_path = Path(args.out_txt) if args.out_txt else out_dir / "cal20_live_lite.txt"
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    with txt_path.open("w", encoding="utf-8") as f:
        for iid in sample:
            f.write(iid + "\n")

    manifest = {
        "dataset": args.dataset,
        "split": args.split,
        "seed": args.seed,
        "n": args.n,
        "total_pool_size": len(pool),
        "pool_hash": pool_hash,
        "selected": sample,
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
    }
    manifest_path = (
        Path(args.out_manifest) if args.out_manifest else out_dir / "cal20_live_lite.manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"wrote {txt_path} ({args.n} ids)")
    print(f"wrote {manifest_path}")
    print(f"pool={len(pool)} pool_hash={pool_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
