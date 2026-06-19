#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.behavioral.utils import collect_file_mentions, find_brief_text, first_actions, iter_task_dirs, load_trajectory, overlap_ratio, steps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trajectory-dir", required=True)
    ap.add_argument("--output", default="tests/behavioral/baselines.json")
    # RC-17 (F-011): seed the RNG so the L3 scrambled-overlap baseline is
    # reproducible across runs. 42 is the historical default; override
    # for stress-testing the calibration's sensitivity to the seed.
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for random.shuffle (default 42; RC-17/F-011 fix).",
    )
    args = ap.parse_args()

    random.seed(args.seed)

    root = Path(args.trajectory_dir)
    tasks = iter_task_dirs(root)
    l1_task = {}
    l3_scrambled = []
    task_ids = []

    for td in tasks:
        tr = load_trajectory(td)
        st = steps(tr)
        if not st:
            continue
        task_ids.append(td.name)
        brief = find_brief_text(td, tr)
        bf = collect_file_mentions(brief)
        a = "\n".join(first_actions(st, 3))
        l1_task[td.name] = 1.0 if any(f in a for f in bf) else 0.0

        ev_files = sorted((td / "gt_evidence").glob("edit_*.json")) if (td / "gt_evidence").exists() else []
        evidence = ev_files[-1].read_text(encoding="utf-8", errors="replace") if ev_files else ""
        if evidence:
            words = evidence.split()
            random.shuffle(words)
            scrambled = " ".join(words)
            l3_scrambled.append(overlap_ratio(scrambled, a))

    l1_mean = (sum(l1_task.values()) / len(l1_task)) if l1_task else 0.0
    l1_threshold = max(0.01, l1_mean * 2.0)

    if l3_scrambled:
        m = sum(l3_scrambled) / len(l3_scrambled)
        var = sum((x - m) ** 2 for x in l3_scrambled) / len(l3_scrambled)
        std = var ** 0.5
    else:
        m = 0.0
        std = 0.0
    l3_threshold = m + 2 * std
    metric_status = "broken" if l3_threshold < 0.05 else "ok"

    payload = {
        "corpus": {
            "run_ids": [root.name],
            "task_ids": task_ids,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "l1": {
            "per_task_random_baseline": l1_task,
            "mean_baseline": l1_mean,
            "threshold_multiplier": 2.0,
            "effective_threshold": l1_threshold,
        },
        "l3": {
            "scrambled_baseline_mean": m,
            "scrambled_baseline_std": std,
            "threshold": l3_threshold,
            "metric_status": metric_status,
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    if metric_status == "broken":
        print("L3 metric marked broken: threshold < 0.05")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
