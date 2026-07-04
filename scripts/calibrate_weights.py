"""Grid-search weights on calibration subset → coefficients_v1.json.

Reads the first N bugs from holdout_v1.jsonl (calibration split), runs
variant C across weight combos in parallel, picks the combo maximising
mean MRR_full, and writes coefficients_v1.json + calibration_grid.csv.

Grid: W_SEM×W_REACH×W_PROX×W_HUB×W_LEX = 3×3×3×3×4 = 324 combos.
W_LEX range [0.1, 0.15, 0.2, 0.25] covers the typical BM25 weight zone
found optimal on code-search tasks in BEIR benchmark papers.

Run:
    python scripts/calibrate_weights.py
        [--input holdout_v1.jsonl]
        [--first-n 20]
        [--out-json results/coefficients_v1.json]
        [--out-csv  results/calibration_grid.csv]
        [--workers 8]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import product
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from groundtruth.pretask.v7_4_brief import run_v74

# Weight grid (§2d of plan)
W_SEM_GRID   = [0.4, 0.5, 0.6]
W_REACH_GRID = [0.3, 0.4, 0.5]
W_PROX_GRID  = [0.05, 0.1, 0.2]
W_HUB_GRID   = [0.0, 0.05, 0.10]
# W_LEX grid: range from BEIR benchmark hybrid retrieval literature (BM25 weight 20-38%)
W_LEX_GRID   = [0.1, 0.15, 0.2, 0.25]

# Locked K params from feasibility tranche
K_ANCHOR   = 3
K_SEM_TOP  = 10
TAU_ANCHOR = 0.20
MAX_DEPTH  = 2


def _precompute_bug(bug: dict) -> dict[str, Any]:
    """Run v7.4 once and keep per-candidate components for cheap reweighting."""
    r = run_v74(
        issue_text=bug.get("issue_body") or bug.get("issue_title") or "",
        repo_root=bug["repo_path"],
        graph_db=bug["graph_db_path"],
        bug_id=bug["bug_id"],
        repo=bug["repo"],
        gold_files=bug["gold_files"],
        ablation="C",
        k_anchor=K_ANCHOR,
        k_sem_top=K_SEM_TOP,
        tau_anchor=TAU_ANCHOR,
        max_depth=MAX_DEPTH,
        weights={"W_SEM": 1.0, "W_LEX": 1.0, "W_REACH": 1.0, "W_PROX": 1.0, "W_HUB": 1.0, "W_COMMIT": 0.0},
    )
    return {
        "bug_id": r.bug_id,
        "gold_files": set(r.gold_files),
        "candidate_set_size": r.candidate_set_size,
        "ranked_full": r.ranked_full,
    }


def _score_components(comps: dict[str, float], weights: dict[str, float]) -> float:
    hub_pen = comps.get("hub_pen", 0.0)
    reach_contrib = weights["W_REACH"] * comps.get("reach", 0.0) * max(0.0, 1.0 - hub_pen)
    evidence_pre_hub = (
        weights["W_SEM"] * comps.get("sem", 0.0)
        + weights.get("W_LEX", 0.0) * comps.get("lex", 0.0)
        + reach_contrib
        + weights["W_PROX"] * comps.get("anchor_prox", 0.0)
        + weights.get("W_COMMIT", 0.0) * comps.get("commit", 0.0)
    )
    w_hub = weights["W_HUB"]
    hub_sub = w_hub * hub_pen if evidence_pre_hub < w_hub else 0.0
    return evidence_pre_hub - hub_sub


def _rank_for_weights(precomputed: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    gold = precomputed["gold_files"]
    ranked = sorted(
        precomputed["ranked_full"],
        key=lambda rec: _score_components(rec.get("components", {}), weights),
        reverse=True,
    )
    first_gold_rank = None
    for idx, rec in enumerate(ranked, start=1):
        if rec["path"] in gold:
            first_gold_rank = idx
            break
    return {
        "MRR_full": (1.0 / first_gold_rank) if first_gold_rank else 0.0,
        "hit1": int(first_gold_rank == 1),
        "hit3": int(bool(first_gold_rank and first_gold_rank <= 3)),
        "hit5": int(bool(first_gold_rank and first_gold_rank <= 5)),
        "hit10": int(bool(first_gold_rank and first_gold_rank <= 10)),
        "gold_in_focus": int(any(rec["path"] in gold for rec in ranked[:3])),
        "first_gold_rank_full": first_gold_rank,
        "candidate_set_size": precomputed["candidate_set_size"],
    }


def _mean_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "MRR_full": 0.0,
            "hit1": 0.0,
            "hit3": 0.0,
            "hit5": 0.0,
            "hit10": 0.0,
            "gold_in_focus": 0.0,
            "candidate_set_size": 0.0,
        }
    keys = ["MRR_full", "hit1", "hit3", "hit5", "hit10", "gold_in_focus", "candidate_set_size"]
    return {k: sum(float(r.get(k, 0.0)) for r in records) / len(records) for k in keys}


def _run_combo(precomputed: list[dict[str, Any]], weights: dict[str, float]) -> dict[str, Any]:
    records = [_rank_for_weights(bug, weights) for bug in precomputed]
    m = _mean_metrics(records)
    return {
        "W_SEM":   weights["W_SEM"],
        "W_LEX":   weights.get("W_LEX", 0.0),
        "W_REACH": weights["W_REACH"],
        "W_PROX":  weights["W_PROX"],
        "W_HUB":   weights["W_HUB"],
        "W_COMMIT": 0.0,
        **m,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="holdout_v1.jsonl")
    parser.add_argument("--first-n", type=int, default=20)
    parser.add_argument("--out-json", default="results/coefficients_v1.json")
    parser.add_argument("--out-csv",  default="results/calibration_grid.csv")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 8))
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: {in_path} not found", file=sys.stderr)
        return 1

    all_bugs: list[dict] = []
    with open(in_path) as f:
        for line in f:
            line = line.strip()
            if line:
                all_bugs.append(json.loads(line))

    cal_bugs = all_bugs[: args.first_n]
    if len(cal_bugs) < args.first_n:
        print(
            f"WARNING: only {len(cal_bugs)} bugs available, wanted {args.first_n}",
            file=sys.stderr,
        )
    n_combos = len(W_SEM_GRID) * len(W_REACH_GRID) * len(W_PROX_GRID) * len(W_HUB_GRID) * len(W_LEX_GRID)
    print(
        f"Calibrating on {len(cal_bugs)} bugs across {n_combos} weight combos"
        f" (workers={args.workers})"
    )

    combos = [
        {"W_SEM": ws, "W_LEX": wl, "W_REACH": wr, "W_PROX": wp, "W_HUB": wh, "W_COMMIT": 0.0}
        for ws, wr, wp, wh, wl in product(W_SEM_GRID, W_REACH_GRID, W_PROX_GRID, W_HUB_GRID, W_LEX_GRID)
    ]

    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        print(f"Precomputing v7.4 components for {len(cal_bugs)} bugs")
        pre_futures = {pool.submit(_precompute_bug, bug): bug for bug in cal_bugs}
        precomputed: list[dict[str, Any]] = []
        for fut in as_completed(pre_futures):
            bug = pre_futures[fut]
            try:
                rec = fut.result()
                precomputed.append(rec)
                print(f"  [precompute] {rec['bug_id']} candidates={rec['candidate_set_size']}")
            except Exception as exc:
                print(f"  [warn] {bug['bug_id']}: {exc}", file=sys.stderr)

        if not precomputed:
            print("ERROR: no precomputed bugs", file=sys.stderr)
            return 1

        futures = {
            pool.submit(_run_combo, precomputed, combo): combo for combo in combos
        }
        for fut in as_completed(futures):
            combo = futures[fut]
            try:
                row = fut.result()
                rows.append(row)
                done += 1
                if done % 50 == 0 or done == n_combos:
                    print(
                        f"  [{done}/{n_combos}] W_SEM={combo['W_SEM']:.2f} W_LEX={combo['W_LEX']:.2f}"
                        f" W_REACH={combo['W_REACH']:.2f} W_PROX={combo['W_PROX']:.2f}"
                        f" W_HUB={combo['W_HUB']:.2f} MRR={row.get('MRR_full', 0):.4f}"
                    )
            except Exception as exc:
                print(f"  [error] {combo}: {exc}", file=sys.stderr)

    if not rows:
        print("ERROR: no results", file=sys.stderr)
        return 1

    best = max(rows, key=lambda r: r.get("MRR_full", 0.0))
    print(f"\nBest combo: W_SEM={best['W_SEM']} W_LEX={best.get('W_LEX', 0.0)}"
          f" W_REACH={best['W_REACH']} W_PROX={best['W_PROX']} W_HUB={best['W_HUB']}"
          f" -> MRR={best.get('MRR_full', 0):.4f}")

    coeff = {
        "W_SEM":   best["W_SEM"],
        "W_LEX":   best.get("W_LEX", 0.0),
        "W_REACH": best["W_REACH"],
        "W_PROX":  best["W_PROX"],
        "W_HUB":   best["W_HUB"],
        "W_COMMIT": 0.0,
        "calibration_MRR":    best.get("MRR_full", 0),
        "n_calibration_bugs": len(cal_bugs),
    }
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(coeff, f, indent=2)
    print(f"Saved coefficients -> {out_json}")

    out_csv = Path(args.out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: r.get("MRR_full", 0.0), reverse=True)
    fieldnames = list(rows_sorted[0].keys())
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_sorted)
    print(f"Saved grid -> {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
