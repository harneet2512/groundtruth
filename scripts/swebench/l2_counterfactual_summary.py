#!/usr/bin/env python3
"""Summarize L2 same-state counterfactual pairs — a CHANGE RATE, never an effect size.

The provider boundary emits `gt.counterfactual_pair.v1` rows: for one dosed turn, the action the
model took WITH the staged capsule and the action it took on an otherwise identical call WITHOUT
it. This reads those rows.

WHAT IT REPORTS. `action_change_rate` = P(next action differs | dosed). The state is identical —
same messages, kwargs, provider, history — and the only difference is the capsule, so this is a
proximal causal statement about CHANGE.

WHAT IT REFUSES TO REPORT. EFFECTIVENESS. Whether the GT-arm action was BETTER needs an anchor
this layer does not have and must not invent. `gt_substitution_grader` hard-gates level-5 CAUSAL
behind `assert_paired_for_causal`, and whether an L2 same-turn pair qualifies as `ARMS_PAIRED` is a
DOCTRINAL question deliberately left unsettled here. So: no CAUSAL verdict, no sign, no direction,
an explicit `effectiveness: NOT_ESTABLISHED`, and `arms: same_turn_fixed_history` — this summary
labels its own shape rather than borrowing the paired-run vocabulary it has not earned.

The slide this guards against is "GT changed the action 40% of the time" quietly becoming "GT is
effective". Those are different claims and only one of them is supported by these rows.

A ZERO IS NOT A FINDING. With no pairs the status is NOT_EVALUABLE and no rate is emitted at all —
a run where the probe never fired must never read as a run where GT changed nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = "gt.counterfactual_pair.v1"


def _iter_rows(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], int]:
    """Return (counterfactual rows, malformed line count).

    Malformed lines are COUNTED, not silently skipped: a silent drop is how a denominator
    quietly shrinks and a rate quietly lies.
    """
    rows: list[dict[str, Any]] = []
    malformed = 0
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except (ValueError, TypeError):
                malformed += 1
                continue
            if isinstance(row, dict) and row.get("schema") == _SCHEMA:
                rows.append(row)
    return rows, malformed


def summarize(paths: Iterable[Path]) -> dict[str, Any]:
    rows, malformed = _iter_rows(paths)

    # ONE dosed turn is ONE opportunity. A duplicated row must not inflate the denominator,
    # so pairs are keyed by model_call_id (last write wins, matching journal semantics).
    by_call: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("model_call_id") or "")
        by_call[key] = row

    overhead = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for row in by_call.values():
        tokens = row.get("measurement_overhead_tokens") or {}
        if isinstance(tokens, dict):
            for key in overhead:
                value = tokens.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    overhead[key] += value

    out: dict[str, Any] = {
        "schema": "gt.l2_counterfactual_summary.v1",
        "pairs": len(by_call),
        "malformed_rows": malformed,
        "measurement_overhead_tokens": overhead,
        # This layer cannot establish direction, and says so in the payload rather than
        # leaving a reader to infer it from a bare rate.
        "effectiveness": "NOT_ESTABLISHED",
        "signed": False,
        "arms": "same_turn_fixed_history",
    }

    if not by_call:
        # NOT a 0% change rate. The probe may simply never have fired -- and in-container it
        # is off unless GT_L2_PROBE_RATE is set, so "no rows" is the DEFAULT state, not a
        # result about GT.
        out["status"] = "NOT_EVALUABLE"
        out["reason"] = "no counterfactual pairs recorded"
        return out

    differ = sum(1 for row in by_call.values() if row.get("actions_differ") is True)
    out["status"] = "MEASURED"
    out["actions_differ"] = differ
    out["action_change_rate"] = differ / len(by_call)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize gt.counterfactual_pair.v1 rows into a CHANGE RATE. "
            "Reports no effectiveness verdict and no causal claim."
        )
    )
    parser.add_argument("paths", nargs="+", help="receipt .jsonl file(s)")
    args = parser.parse_args(argv)
    result = summarize([Path(p) for p in args.paths])
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
