#!/usr/bin/env python3
"""FIX 5 (2026-06-11) — derive the GT_ESC_* escalation band edges from the
frozen 9-trajectory oracle corpus (run 27321848581) instead of the invented
0.4/0.5/0.3/0.7/2.0 placeholders.

Method (deterministic, replayable — gt_gt §15.2 statelessness):
  for each frozen trajectory, stream the Stage-0 sensor
  (gt_oracle_sense.sense over prefixes) and record, at each REVIEW
  transition (source edit followed by >=3 non-edit actions — the live F4
  predicate), the behavioral signals the bands key on:
    - test_coverage_ratio (edited files with test evidence / edited files)
    - B = action_count / step_limit   (budget fraction consumed)
    - R = step_limit - action_count   (remaining steps)
    - V = the agent's own observed edit->test cycle pace (live estimator
      twin: cycle opens at the first source edit after the last observed
      result, closes at the next observed result; median, railed [8,40];
      never-test -> the 25-step default, matching the live fallback)

Edges (n=9 — too small for outcome-stratified quantiles: 0/9 resolved, so
the succeeded/failed split the design asks for does not exist; per the
fallback rule the edges are corpus medians/quartiles, documented as
"n=9, calibration-quality placeholder"):
    ADV_TCOV  = median(tcov @ FIRST review transition)   [advisory: tcov below]
    ADV_B     = p25   (B    @ FIRST review transition)   [advisory: B above]
    URG_TCOV  = p25   (tcov @ FIRST review transition)   [urgent: tcov below]
    URG_B     = median(B    @ LAST  review transition)   [urgent: B above]
    GATE_TCOV = median(tcov @ LAST  review transition)   [gate: tcov below]
    GATE_KV   = median(remaining-at-SUBMISSION / V)      [gate: R < KV*V]
                (the design's stated anchor: the remaining-steps level where
                 the agent typically submitted = end-of-trajectory remaining)
Monotonicity is enforced (URG_TCOV <= ADV_TCOV; ADV_B <= URG_B) and each
tcov edge is floored at a functional epsilon (a 0.0 edge would make the
strict `tcov < edge` predicate unfirable — dead band).

Usage:  python scripts/calibrate_esc_bands.py [--corpus DIR] [--step-limit N]
Writes a JSON receipt next to the corpus report dir and prints the 8-dp
edges to bake into gt_mini_patch.py.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CORPUS = _ROOT / ".claude" / "reports" / "runs" / "oracle_tenpack_27321848581"
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"

# The strict-predicate floor: verify_horizon_band fires on `tcov < edge`, so a
# 0.0-derived edge is a DEAD band. 1/8 = one-of-eight edited files covered —
# the smallest honest "almost nothing verified" grain at the corpus's typical
# edited-file counts.
_TCOV_EPS = 0.125


def _load_sense():
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("gt_oracle_sense_cal", _SENSE_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_oracle_sense_cal"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


def _review_transitions(sense, turns) -> list[int]:
    """Turn indices where the trajectory ENTERS review (the live F4 predicate:
    >=1 source edit, then the 3rd consecutive non-edit action). Streaming twin
    of gt_oracle_sense.sense's phase logic — O(n), no prefix re-sense."""
    idxs: list[int] = []
    seen_edit = False
    streak = 0
    in_review = False
    for ti, t in enumerate(turns):
        kind, fpath = sense._classify(t.command)
        if kind == "post_edit" and fpath:
            seen_edit = True
            streak = 0
            in_review = False
            continue
        if seen_edit:
            streak += 1
            if streak >= 3 and not in_review:
                in_review = True
                idxs.append(ti)
    return idxs


def _estimate_v(sense, turns, default_v: int = 25,
                v_min: int = 8, v_max: int = 40) -> tuple[int, bool]:
    """Live `_estimate_v` twin over a frozen trajectory.
    Returns (V, observed?) — observed=False means the never-test default."""
    spans: list[int] = []
    cycle_start: int | None = None
    action = 0
    for t in turns:
        action += 1
        kind, fpath = sense._classify(t.command)
        if kind == "post_edit" and fpath and cycle_start is None:
            cycle_start = action
        tr = sense._classify_test_run(t.command, t.raw_obs)
        if tr is not None and tr.has_result:
            if cycle_start is not None and action - cycle_start > 0:
                spans.append(action - cycle_start)
            cycle_start = None
    if spans:
        return max(v_min, min(v_max, int(statistics.median(spans)))), True
    return default_v, False


def _p25(vals: list[float]) -> float:
    return statistics.quantiles(vals, n=4)[0] if len(vals) > 1 else vals[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(_DEFAULT_CORPUS))
    ap.add_argument("--step-limit", type=int, default=300,
                    help="GT_STEP_LIMIT of the corpus run (deepswe_full.yml)")
    args = ap.parse_args()

    sense = _load_sense()
    pattern = os.path.join(args.corpus, "*", "jobs", "*", "*", "agent",
                           "mini-swe-agent.trajectory.json")
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"NO trajectories under {args.corpus}", file=sys.stderr)
        return 1

    S = args.step_limit
    per_task: list[dict] = []
    for p in paths:
        task = Path(p).parents[4].name
        turns = sense.load_trajectory(p)
        transitions = _review_transitions(sense, turns)
        if not transitions:
            per_task.append({"task": task, "n_turns": len(turns),
                             "review_transitions": 0})
            continue
        v, v_observed = _estimate_v(sense, turns)
        rec = {"task": task, "n_turns": len(turns),
               "review_transitions": len(transitions),
               "v": v, "v_observed": v_observed,
               # submission anchor: budget left when the trajectory ENDED
               "remaining_at_end": S - len(turns),
               "rv_at_end": float(f"{(S - len(turns)) / v:.8f}")}
        for label, ti in (("first", transitions[0]), ("last", transitions[-1])):
            st = sense.sense(turns[: ti + 1])
            tcov = sense.test_coverage_ratio(
                st.edited_tokens_by_file, st.tested_tokens)
            rec[label] = {
                "turn": ti,
                "action_count": st.action_count,
                "test_coverage": None if tcov is None else float(f"{tcov:.8f}"),
                "budget_fraction": float(f"{st.action_count / S:.8f}"),
                "remaining": S - st.action_count,
                "r_over_v": float(f"{(S - st.action_count) / v:.8f}"),
            }
        per_task.append(rec)

    with_rt = [r for r in per_task if r.get("review_transitions", 0) > 0]
    tcov_first = [r["first"]["test_coverage"] for r in with_rt
                  if r["first"]["test_coverage"] is not None]
    tcov_last = [r["last"]["test_coverage"] for r in with_rt
                 if r["last"]["test_coverage"] is not None]
    b_first = [r["first"]["budget_fraction"] for r in with_rt]
    b_last = [r["last"]["budget_fraction"] for r in with_rt]
    rv_last = [r["last"]["r_over_v"] for r in with_rt]
    rv_end = [r["rv_at_end"] for r in with_rt]

    adv_tcov = max(statistics.median(tcov_first), _TCOV_EPS)
    urg_tcov = max(_p25(tcov_first), _TCOV_EPS)
    urg_tcov = min(urg_tcov, adv_tcov)            # monotone
    adv_b = _p25(b_first)
    urg_b = statistics.median(b_last)
    adv_b = min(adv_b, urg_b)                     # monotone
    gate_tcov = max(statistics.median(tcov_last), _TCOV_EPS)
    gate_kv = statistics.median(rv_end)

    edges = {
        "GT_ESC_ADV_TCOV": float(f"{adv_tcov:.8f}"),
        "GT_ESC_ADV_B": float(f"{adv_b:.8f}"),
        "GT_ESC_URG_TCOV": float(f"{urg_tcov:.8f}"),
        "GT_ESC_URG_B": float(f"{urg_b:.8f}"),
        "GT_ESC_GATE_TCOV": float(f"{gate_tcov:.8f}"),
        "GT_ESC_GATE_KV": float(f"{gate_kv:.8f}"),
    }
    receipt = {
        "schema": "gt.esc_band_calibration.v1",
        "generated": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "corpus": str(args.corpus),
        "step_limit": S,
        "n_tasks": len(per_task),
        "n_with_review_transition": len(with_rt),
        "note": ("n=9, calibration-quality placeholder — 0/9 resolved, so the "
                 "outcome-stratified quantiles of the design are impossible; "
                 "median/quartile fallback per the FIX 5 rule. tcov edges "
                 f"floored at {_TCOV_EPS} (strict `tcov < edge` predicate; a "
                 "0.0 edge is a dead band)."),
        "signals": {
            "tcov_first": tcov_first, "tcov_last": tcov_last,
            "b_first": b_first, "b_last": b_last, "rv_last": rv_last,
            "rv_end": rv_end,
        },
        "edges": edges,
        "per_task": per_task,
    }
    out_dir = _ROOT / ".claude" / "reports" / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"esc_calibration_{receipt['generated']}.json"
    out_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"edges": edges, "receipt": str(out_path),
                      "n_with_review_transition": len(with_rt)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
