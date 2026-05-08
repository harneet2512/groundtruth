#!/usr/bin/env python3
"""Compare gt_telemetry utilization between two output.jsonl runs."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def _index(path: Path) -> dict[str, dict[str, Any]]:
    m: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        iid = str(rec.get("instance_id") or "")
        if not iid:
            continue
        tel = rec.get("gt_telemetry")
        if isinstance(tel, dict):
            m[iid] = tel
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="GT telemetry regression diff")
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.05,
        help="Flag per-layer drop larger than this fraction (default 0.05 = 5%%)",
    )
    args = ap.parse_args()
    if not args.before.is_file() or not args.after.is_file():
        print("FATAL: need two jsonl files", file=sys.stderr)
        return 2

    a = _index(args.before)
    b = _index(args.after)
    common = sorted(set(a) & set(b))
    if not common:
        print("No overlapping instance_ids with gt_telemetry.")
        return 0

    flagged = 0
    for iid in common:
        ua = a[iid].get("utilization") or {}
        ub = b[iid].get("utilization") or {}
        if not isinstance(ua, dict) or not isinstance(ub, dict):
            continue
        for layer in sorted(set(ua) | set(ub)):
            try:
                va = float(ua.get(layer, 0) or 0)
                vb = float(ub.get(layer, 0) or 0)
            except (TypeError, ValueError):
                continue
            drop = va - vb
            if drop > args.threshold:
                flagged += 1
                print(
                    f"REGRESSION {iid} {layer}: {va:.3f} -> {vb:.3f} (Δ {-drop:.3f})",
                    flush=True,
                )

    ov_a = statistics.mean([float(x.get("overall_utilization", 0) or 0) for x in a.values()])
    ov_b = statistics.mean([float(x.get("overall_utilization", 0) or 0) for x in b.values()])
    print(f"mean_overall before={ov_a:.4f} after={ov_b:.4f} regression_rows={flagged}")
    return 1 if flagged else 0

if __name__ == "__main__":
    raise SystemExit(main())
