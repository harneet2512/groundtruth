#!/usr/bin/env python3
"""Pre-flight checks before live DeepSWE proof (P0-01). Does not run agents."""
from __future__ import annotations

import json
import os
import sys


def validate() -> list[str]:
    problems: list[str] = []
    if not os.environ.get("GT_SUBSTRATE_DIGEST"):
        problems.append("GT_SUBSTRATE_DIGEST unset — rebuild/pin substrate before proof")
    if os.environ.get("GT_PROOF_MODE") != "1":
        problems.append("GT_PROOF_MODE!=1")
    if os.environ.get("GT_ORACLE_ROUTE") == "0":
        problems.append("GT_ORACLE_ROUTE=0 forbidden in proof")
    cal = os.path.join(
        os.path.dirname(__file__), "..", "..", ".claude", "calibration", "horizon_v1.json"
    )
    if not os.path.isfile(cal):
        problems.append(f"missing calibration corpus: {cal}")
    else:
        try:
            data = json.load(open(cal, encoding="utf-8"))
            if not data.get("thresholds"):
                problems.append("horizon calibration file has no thresholds")
        except (OSError, ValueError) as exc:
            problems.append(f"horizon calibration unreadable: {exc}")
    return problems


def main() -> int:
    problems = validate()
    if problems:
        for p in problems:
            print(f"NOT_READY: {p}", file=sys.stderr)
        return 1
    print("READY: proof preconditions satisfied (substrate digest + proof flags + calibration)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
