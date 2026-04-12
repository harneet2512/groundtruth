"""Eval Acceptance Gate — determines if a feature meets acceptance criteria.

Exit code 0 = accepted, exit code 1 = rejected.

Usage:
    python scripts/eval_acceptance.py --gt-results gt_results.json --baseline baseline_results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="GT Eval Acceptance Gate")
    parser.add_argument("--gt-results", required=True, help="GT-enabled results JSON")
    parser.add_argument("--baseline", required=True, help="Baseline results JSON")
    parser.add_argument("--min-uplift", type=float, default=0.05, help="Min absolute uplift (default 5%%)")
    parser.add_argument("--min-models", type=int, default=2, help="Min model baselines passing")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Import here to avoid circular issues
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from benchmarks.eval_matrix.variance_check import run_variance_matrix

    gt_data = json.loads(Path(args.gt_results).read_text())
    bl_data = json.loads(Path(args.baseline).read_text())

    results = run_variance_matrix(gt_data, bl_data, args.min_uplift)

    if not results:
        print("ERROR: No comparable results found")
        sys.exit(1)

    # Check acceptance: ≥min_models must show significant uplift
    passing_models = set()
    failing = []

    for r in results:
        if args.verbose:
            status = "PASS" if r.significant and r.exceeds_threshold else "FAIL"
            print(
                f"  {r.benchmark}/{r.model}: "
                f"uplift={r.uplift:+.1%}, t={r.t_statistic:.2f}, "
                f"sig={r.significant}, exceeds={r.exceeds_threshold} [{status}]"
            )

        if r.significant and r.exceeds_threshold:
            passing_models.add(r.model)
        else:
            failing.append(f"{r.benchmark}/{r.model}")

    n_passing = len(passing_models)

    print(f"\nAcceptance check: {n_passing}/{args.min_models} models passing")

    if n_passing >= args.min_models:
        print("RESULT: ACCEPTED — uplift exceeds variance on sufficient model baselines")
        sys.exit(0)
    else:
        print(f"RESULT: REJECTED — only {n_passing} models pass (need {args.min_models})")
        if failing:
            print(f"  Failing: {', '.join(failing[:5])}")
        sys.exit(1)


if __name__ == "__main__":
    main()
