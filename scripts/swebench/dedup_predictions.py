#!/usr/bin/env python3
"""Deduplicate a SWE-bench predictions JSONL file.

Keeps the first occurrence of each instance_id, discards duplicates.

Usage:
    python3 scripts/swebench/dedup_predictions.py <predictions.jsonl>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def dedup(path: Path) -> None:
    seen: set[str] = set()
    unique: list[str] = []
    total = 0

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            record = json.loads(line)
            instance_id = record["instance_id"]
            if instance_id not in seen:
                seen.add(instance_id)
                unique.append(line)

    dupes = total - len(unique)
    print(f"Total lines: {total}")
    print(f"Unique instance_ids: {len(unique)}")
    print(f"Duplicates removed: {dupes}")

    if dupes == 0:
        print("No duplicates found. File unchanged.")
        return

    # Write backup
    backup = path.with_suffix(".jsonl.bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Backup saved to: {backup}")

    # Overwrite with deduplicated data
    with open(path, "w", encoding="utf-8") as f:
        for line in unique:
            f.write(line + "\n")

    print(f"Wrote {len(unique)} unique predictions to: {path}")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Error: {path} does not exist")
        sys.exit(1)

    dedup(path)


if __name__ == "__main__":
    main()
