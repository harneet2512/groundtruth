"""Measure how much the LEGACY control arm moves between two sealed runs.

The vNext arm re-runs `repeats` times per case and its determinism is checked.
The legacy arm runs ONCE and is checked against nothing, so every paired delta
reported against it silently assumes a control that does not move.

It moves. Measured across three runs of identical code on identical inputs
(runA / runA2 / canary, oss-60):

    candidate_order   identical on  75-97% of shared cases
    projection hash   identical on  25-81%

`candidate_order` is what is SCORED, so the 3-25% that differ are a real noise
floor under every "old vs new" number. The projection hash also covers brief
text and other internals that are never scored, which is why it looks far worse
and why quoting it alone overstates the problem.

Two consequences this script exists to enforce:

  * WITHIN-RUN paired comparisons stay valid - both arms see one graph and one
    embedder state per case, so the drift cancels.
  * CROSS-RUN comparisons of one arm against another run's arm carry the drift.
    Do not report those without the number this script prints.

Usage:
    python scripts/legacy_control_drift.py RUN_DIR RUN_DIR [RUN_DIR ...]

where each RUN_DIR holds the downloaded shard artifacts (``loc-vnext-*/sealed/*.json``).
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from typing import Any


def load_run(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Sealed cases in one run, keyed by case id."""
    sealed: dict[str, dict[str, Any]] = {}
    for path in sorted(run_dir.glob("loc-vnext-*/sealed/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        case = record.get("case") or {}
        case_id = str(case.get("id") or "")
        if case_id and "legacy" in record:
            sealed[case_id] = record
    return sealed


def compare(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Agreement between two runs on the cases they share."""
    shared = sorted(set(left) & set(right))
    if not shared:
        return {"shared": 0}
    order_same = []
    projection_same = []
    drifted: list[dict[str, Any]] = []
    for case_id in shared:
        a, b = left[case_id]["legacy"], right[case_id]["legacy"]
        same_order = list(a.get("candidate_order") or ()) == list(
            b.get("candidate_order") or ()
        )
        order_same.append(same_order)
        projection_same.append(
            a.get("projection_sha256") == b.get("projection_sha256")
        )
        if not same_order:
            drifted.append(
                {
                    "case_id": case_id,
                    "left": list(a.get("candidate_order") or ()),
                    "right": list(b.get("candidate_order") or ()),
                }
            )
    return {
        "shared": len(shared),
        "candidate_order_identical": sum(order_same),
        "projection_identical": sum(projection_same),
        # The SCORED quantity. This is the number that bounds how much of any
        # cross-run delta could be the control arm moving rather than a change.
        "scored_drift_rate": 1.0 - sum(order_same) / len(shared),
        "drifted": drifted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    runs = {path.name: load_run(path) for path in args.run_dirs}
    for name, sealed in runs.items():
        print(f"  {name}: {len(sealed)} sealed cases")

    report: dict[str, Any] = {"runs": {k: len(v) for k, v in runs.items()}, "pairs": {}}
    worst = 0.0
    print()
    for left, right in combinations(runs, 2):
        result = compare(runs[left], runs[right])
        report["pairs"][f"{left} vs {right}"] = result
        if not result["shared"]:
            print(f"{left} vs {right}: no shared cases")
            continue
        n = result["shared"]
        worst = max(worst, result["scored_drift_rate"])
        print(
            f"{left} vs {right}:  n={n:3d}   "
            f"candidate_order identical {result['candidate_order_identical']:3d}/{n} "
            f"({100 * result['candidate_order_identical'] / n:3.0f}%)   "
            f"projection identical {result['projection_identical']:3d}/{n} "
            f"({100 * result['projection_identical'] / n:3.0f}%)"
        )
        for entry in result["drifted"][:5]:
            print(
                f"     drift: {entry['case_id'][:46]:46s} "
                f"{len(entry['left'])} -> {len(entry['right'])} candidates"
            )

    print(
        f"\nWORST SCORED DRIFT: {100 * worst:.0f}% of shared cases. Any CROSS-RUN "
        "delta smaller than this is\nindistinguishable from the control arm moving. "
        "Within-run paired comparisons are unaffected."
    )
    if args.json_out:
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
