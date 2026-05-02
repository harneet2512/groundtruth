"""Evaluate v7.4 scorer on the feasibility tranche.

Usage:
    python scripts/eval_tranche.py [--tranche holdout_feasibility.jsonl]
                                   [--ablation C]
                                   [--k-anchor 5] [--k-sem-top 20]
                                   [--tau-anchor 0.30] [--max-depth 3]
                                   [--out results/tranche_eval.jsonl]
                                   [--sweep]

With --sweep: runs variant A and C across the full K-sensitivity grid and
writes results/k_sensitivity.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from groundtruth.pretask.v7_4_brief import run_v74, V74BriefResult, Ablation, DEFAULT_FOCUS_SIZE

ABLATIONS = ["A", "B0", "B1", "C", "D"]

# K-sensitivity grid (Step 1b of feasibility tranche)
K_SWEEP_GRID = {
    "K_ANCHOR": [3, 5, 8],
    "K_SEM_TOP": [10, 20, 40],
    "TAU_ANCHOR": [0.20, 0.30, 0.40],
    "max_depth": [2, 3],
}


def load_tranche(path: str) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def run_one(
    bug: dict,
    ablation: Ablation = "C",
    k_anchor: int = 5,
    k_sem_top: int = 20,
    tau_anchor: float = 0.30,
    max_depth: int = 3,
) -> V74BriefResult:
    return run_v74(
        issue_text=bug["issue_body"],
        repo_root=bug["repo_path"],
        graph_db=bug["graph_db_path"],
        bug_id=bug["bug_id"],
        repo=bug["repo"],
        gold_files=bug["gold_files"],
        ablation=ablation,
        k_anchor=k_anchor,
        k_sem_top=k_sem_top,
        tau_anchor=tau_anchor,
        max_depth=max_depth,
    )


def compute_metrics(results: list[V74BriefResult]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    mrr = []
    hit1 = hit3 = hit5 = hit10 = 0
    cov1 = cov3 = covered_any = 0
    first_gold_ranks_full = []
    first_gold_ranks_focus = []
    candidate_sizes = []
    gold_in_focus_count = 0

    for r in results:
        candidate_sizes.append(r.candidate_set_size)

        # MRR_full: reciprocal rank of first gold in full ranking
        if r.first_gold_rank_full is not None:
            mrr.append(1.0 / r.first_gold_rank_full)
            first_gold_ranks_full.append(r.first_gold_rank_full)
            if r.first_gold_rank_full <= 1:
                hit1 += 1
            if r.first_gold_rank_full <= 3:
                hit3 += 1
            if r.first_gold_rank_full <= 5:
                hit5 += 1
            if r.first_gold_rank_full <= 10:
                hit10 += 1
        else:
            mrr.append(0.0)

        # Focus metrics (top-3 cap)
        if r.gold_in_focus:
            gold_in_focus_count += 1
            covered_any += 1

        if r.first_gold_rank_focus is not None:
            first_gold_ranks_focus.append(r.first_gold_rank_focus)

        # Precision / coverage for focus set
        focus_set = set(r.focus_set)
        gold_set = set(r.gold_files)
        if focus_set:
            prec = len(focus_set & gold_set) / len(focus_set)
            cov1 += prec  # using as precision accumulator
        if gold_set:
            cov3 += len(focus_set & gold_set) / len(gold_set)

    return {
        "n": n,
        "MRR_full": round(sum(mrr) / n, 4),
        "hit@1_full": hit1,
        "hit@3_full": hit3,
        "hit@5_full": hit5,
        "hit@10_full": hit10,
        "hit@1_full_rate": round(hit1 / n, 3),
        "hit@3_full_rate": round(hit3 / n, 3),
        "hit@5_full_rate": round(hit5 / n, 3),
        "hit@10_full_rate": round(hit10 / n, 3),
        "gold_in_focus": gold_in_focus_count,
        "gold_in_focus_rate": round(gold_in_focus_count / n, 3),
        "focus_precision": round(cov1 / n, 3),
        "focus_coverage": round(cov3 / n, 3),
        "median_first_gold_rank_full": (
            round(statistics.median(first_gold_ranks_full), 1) if first_gold_ranks_full else None
        ),
        "median_first_gold_rank_focus": (
            round(statistics.median(first_gold_ranks_focus), 1) if first_gold_ranks_focus else None
        ),
        "median_candidate_set_size": round(statistics.median(candidate_sizes), 1),
    }


def print_metrics(label: str, metrics: dict[str, Any]) -> None:
    print(f"\n{'='*60}")
    print(f"Variant {label} — n={metrics.get('n', 0)}")
    print(f"  MRR_full                = {metrics.get('MRR_full', 0):.4f}")
    print(f"  hit@1/3/5/10 (full)     = {metrics.get('hit@1_full', 0)}/{metrics.get('hit@3_full', 0)}/{metrics.get('hit@5_full', 0)}/{metrics.get('hit@10_full', 0)}")
    print(f"  gold_in_focus (top-3)   = {metrics.get('gold_in_focus', 0)} ({metrics.get('gold_in_focus_rate', 0):.3f})")
    print(f"  focus_precision         = {metrics.get('focus_precision', 0):.3f}")
    print(f"  focus_coverage          = {metrics.get('focus_coverage', 0):.3f}")
    print(f"  median_candidate_size   = {metrics.get('median_candidate_set_size', 0)}")
    print(f"  median_first_gold_rank  = full={metrics.get('median_first_gold_rank_full')} focus={metrics.get('median_first_gold_rank_focus')}")


def sweep_K(bugs: list[dict], out_csv: str) -> None:
    """Run K-sensitivity sweep for variants A and C across the full grid."""
    from itertools import product

    rows = []
    grid = [(ka, ks, ta, md)
            for ka in K_SWEEP_GRID["K_ANCHOR"]
            for ks in K_SWEEP_GRID["K_SEM_TOP"]
            for ta in K_SWEEP_GRID["TAU_ANCHOR"]
            for md in K_SWEEP_GRID["max_depth"]]

    total = len(grid) * 2  # A + C
    done = 0
    for variant in ["A", "C"]:
        for ka, ks, ta, md in grid:
            results = []
            for bug in bugs:
                try:
                    r = run_one(bug, ablation=variant, k_anchor=ka, k_sem_top=ks,
                                tau_anchor=ta, max_depth=md)  # type: ignore[arg-type]
                    results.append(r)
                except Exception as e:
                    print(f"  [warn] {bug['bug_id']} {variant} failed: {e}", file=sys.stderr)

            m = compute_metrics(results)
            row = {
                "variant": variant,
                "K_ANCHOR": ka,
                "K_SEM_TOP": ks,
                "TAU_ANCHOR": ta,
                "max_depth": md,
                **m,
            }
            rows.append(row)
            done += 1
            print(f"  [{done}/{total}] {variant} ka={ka} ks={ks} ta={ta} md={md} MRR={m.get('MRR_full', 0):.4f} gold_focus={m.get('gold_in_focus_rate', 0):.3f}")

    if rows:
        fieldnames = list(rows[0].keys())
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"\nSaved to {out_csv}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tranche", default=".tmp_tranche/holdout_feasibility.jsonl")
    parser.add_argument("--ablation", default="C",
                        choices=["A", "B0", "B1", "C", "D", "all"])
    parser.add_argument("--k-anchor", type=int, default=5)
    parser.add_argument("--k-sem-top", type=int, default=20)
    parser.add_argument("--tau-anchor", type=float, default=0.30)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--out", default="")
    parser.add_argument("--sweep", action="store_true",
                        help="Run K-sensitivity sweep (variants A + C only)")
    parser.add_argument("--sweep-out", default="results/k_sensitivity.csv")
    args = parser.parse_args()

    bugs = load_tranche(args.tranche)
    print(f"Loaded {len(bugs)} bugs from {args.tranche}")

    if args.sweep:
        Path(args.sweep_out).parent.mkdir(parents=True, exist_ok=True)
        print(f"\nRunning K-sensitivity sweep ({len(bugs)} bugs × 2 variants × {3*3*3*2} grid points)")
        sweep_K(bugs, args.sweep_out)
        return 0

    variants_to_run: list[str] = ABLATIONS if args.ablation == "all" else [args.ablation]

    all_out: list[dict] = []
    for variant in variants_to_run:
        results = []
        for bug in bugs:
            try:
                r = run_one(
                    bug,
                    ablation=variant,  # type: ignore[arg-type]
                    k_anchor=args.k_anchor,
                    k_sem_top=args.k_sem_top,
                    tau_anchor=args.tau_anchor,
                    max_depth=args.max_depth,
                )
                results.append(r)
                if args.out:
                    all_out.append({**r.__dict__})
            except Exception as e:
                import traceback
                print(f"  [error] {bug['bug_id']} {variant}: {e}", file=sys.stderr)
                traceback.print_exc()

        metrics = compute_metrics(results)
        print_metrics(variant, metrics)

    if args.out and all_out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            for rec in all_out:
                # ranked_full can be huge — trim to top 50 for storage
                if "ranked_full" in rec and len(rec["ranked_full"]) > 50:
                    rec["ranked_full"] = rec["ranked_full"][:50]
                f.write(json.dumps(rec) + "\n")
        print(f"\nSaved {len(all_out)} records to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
