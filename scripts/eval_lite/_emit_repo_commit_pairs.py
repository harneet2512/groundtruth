"""Emit one `repo_safe<TAB>base_commit` line per unique (repo, commit) pair
in SWE-Bench Lite. Used by index_per_commit.sh.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    cache = os.environ.get("DATASET_CACHE", "/data1tb/swe_lite_dataset")
    dataset = os.environ.get("DATASET", "princeton-nlp/SWE-bench_Lite")
    split = os.environ.get("SPLIT", "test")
    try:
        if os.path.isdir(cache):
            from datasets import load_from_disk  # type: ignore[import-untyped]

            ds = load_from_disk(cache)
        else:
            from datasets import load_dataset  # type: ignore[import-untyped]

            ds = load_dataset(dataset, split=split)  # type: ignore[arg-type]
    except Exception as e:
        print(f"emit_repo_commit_pairs: dataset load failed: {e}", file=sys.stderr)
        return 1

    seen: set[tuple[str, str]] = set()
    for row in ds:  # type: ignore[union-attr]
        repo = row.get("repo")  # type: ignore[union-attr]
        commit = row.get("base_commit")  # type: ignore[union-attr]
        if not repo or not commit:
            continue
        repo_safe = repo.replace("/", "__")
        key = (repo_safe, commit)
        if key in seen:
            continue
        seen.add(key)
        print(f"{repo_safe}\t{commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
