#!/usr/bin/env python3
"""Fetch SWE-bench Verified instance IDs from HuggingFace and write the canonical JSON.

One-time setup script. Requires `pip install datasets`.

Usage:
    python scripts/swebench/fetch_verified_ids.py
"""
from __future__ import annotations

import json
from pathlib import Path

_OUT = Path(__file__).resolve().parent.parent.parent / "benchmarks" / "swebench_verified_500_ids.json"


def main() -> None:
    from datasets import load_dataset

    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    ids = sorted({str(row["instance_id"]) for row in ds})  # type: ignore[index]

    repos: dict[str, int] = {}
    for iid in ids:
        repo = iid.rsplit("-", 1)[0].replace("__", "/")
        repos[repo] = repos.get(repo, 0) + 1

    payload = {
        "version": 1,
        "source": "princeton-nlp/SWE-bench_Verified",
        "split": "test",
        "n_total_in_split": len(ids),
        "selected_count": len(ids),
        "stratification": dict(sorted(repos.items())),
        "instance_ids": ids,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(ids)} instance IDs to {_OUT}")
    print(f"Repos: {len(repos)}")
    for repo, count in sorted(repos.items(), key=lambda x: -x[1])[:15]:
        print(f"  {repo}: {count}")


if __name__ == "__main__":
    main()
