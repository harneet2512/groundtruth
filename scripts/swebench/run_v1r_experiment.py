#!/usr/bin/env python3
"""V1R architecture experiment launcher.

Runs 4 behavior arms on the same task set via run_mini_gt_hooked.py:
  BL           — baseline, no GT (brief=none, hook=none)
  V1           — current v1 PRETASK (brief=enhanced, hook=full)
  V1R-map      — refined v1R brief only (brief=v1r, hook=none)
  V1R-map+hook — refined v1R brief + lean hook (brief=v1r, hook=lean)

Each arm runs as a separate invocation with env vars controlling GT behavior.
Results are written to separate output directories for post-hoc comparison.

Usage:
    python run_v1r_experiment.py --arm BL --model openai/qwen3-coder \\
        --subset SWE-bench-Live/SWE-bench-Live --split lite --slice 0:15 -w 2
    python run_v1r_experiment.py --arm V1R-map --model openai/qwen3-coder \\
        --subset SWE-bench-Live/SWE-bench-Live --split lite --slice 0:15 -w 2

Environment variables set per arm:
    GT_BRIEF_MODE   — "none" | "enhanced" | "v1r"
    GT_HOOK_MODE    — "none" | "full" | "lean"
    GT_ARM_NAME     — arm label for logging
"""
from __future__ import annotations

import argparse
import os
import sys

ARMS = {
    "BL": {"GT_BRIEF_MODE": "none", "GT_HOOK_MODE": "none"},
    "V1": {"GT_BRIEF_MODE": "enhanced", "GT_HOOK_MODE": "full"},
    "V1R-map": {"GT_BRIEF_MODE": "v1r", "GT_HOOK_MODE": "none"},
    "V1R-map+hook": {"GT_BRIEF_MODE": "v1r", "GT_HOOK_MODE": "lean"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="V1R experiment launcher")
    parser.add_argument("--arm", required=True, choices=list(ARMS.keys()),
                        help="Experiment arm to run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print env vars and command without executing")
    args, remaining = parser.parse_known_args()

    arm_env = ARMS[args.arm]
    for key, val in arm_env.items():
        os.environ[key] = val
    os.environ["GT_ARM_NAME"] = args.arm

    print(f"=== V1R Experiment: arm={args.arm} ===")
    print(f"  GT_BRIEF_MODE = {arm_env['GT_BRIEF_MODE']}")
    print(f"  GT_HOOK_MODE  = {arm_env['GT_HOOK_MODE']}")
    print(f"  GT_ARM_NAME   = {args.arm}")
    print(f"  Remaining args: {remaining}")
    print(flush=True)

    if args.dry_run:
        print("DRY RUN — would invoke run_mini_gt_hooked.py with above env")
        return 0

    sys.argv = [sys.argv[0]] + remaining
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

    from benchmarks.swebench.run_mini_gt_hooked import app
    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
