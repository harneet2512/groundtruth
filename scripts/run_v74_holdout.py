"""Run v7.4 ablation, gate checks, and step-3 evaluation on the full holdout.

Three phases (run sequentially or individually via --phase):

  ablation  â€” run A/B0/B1/C/D on 40-bug eval split (bugs 21-60 in holdout_v1.jsonl)
  gates     â€” compute Wilcoxon + bootstrap CI + 6 go/no-go gates vs v7.3 baseline
  step3     â€” run best variant on original 15 tasks (requires --task-manifest)

Run full pipeline:
    python scripts/run_v74_holdout.py --phase all
        [--holdout holdout_v1.jsonl]
        [--baseline results/baseline_holdout.csv]
        [--coefficients results/coefficients_v1.json]
        [--ablation-out results/holdout_ablation.jsonl]
        [--gate-out results/gate_verdict.json]
        [--step3-manifest <path/to/15tasks.jsonl>]
        [--step3-out results/step3_results.csv]
        [--workers 8]

Requires scipy for gate phase.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from groundtruth.pretask.v7_4_brief import run_v74, V74BriefResult
from groundtruth.pretask._deprecated.v7_brief import generate_brief, V7BriefResult

_WRITE_LOCK = threading.Lock()

# Locked K params from feasibility tranche
K_ANCHOR   = 3
K_SEM_TOP  = 10
TAU_ANCHOR = 0.20
MAX_DEPTH  = 3

ABLATIONS = ["A", "B0", "B1", "C", "D"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_jsonl(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _run_v74_bug(bug: dict, ablation: str, weights: dict) -> V74BriefResult:
    return run_v74(
        issue_text=bug.get("issue_body") or bug.get("issue_title") or "",
        repo_root=bug["repo_path"],
        graph_db=bug["graph_db_path"],
        bug_id=bug["bug_id"],
        repo=bug["repo"],
        gold_files=bug["gold_files"],
        ablation=ablation,  # type: ignore[arg-type]
        k_anchor=K_ANCHOR,
        k_sem_top=K_SEM_TOP,
        tau_anchor=TAU_ANCHOR,
        max_depth=MAX_DEPTH,
        weights=weights,
    )


def _weighted_score(components: dict[str, float], weights: dict, ablation: str) -> float:
    if ablation == "A":
        # Dense only: W_LEX zeroed by ablation weights
        return weights.get("W_SEM", 0.0) * components.get("sem", 0.0)
    if ablation in ("B0", "B1"):
        # Graph only: W_SEM and W_LEX zeroed by ablation weights
        return (
            weights.get("W_REACH", 0.0) * components.get("reach", 0.0)
            + weights.get("W_PROX", 0.0) * components.get("anchor_prox", 0.0)
        )
    hub_pen = components.get("hub_pen", 0.0)
    reach_contrib = weights.get("W_REACH", 0.0) * components.get("reach", 0.0) * max(0.0, 1.0 - hub_pen)
    evidence_pre_hub = (
        weights.get("W_SEM", 0.0) * components.get("sem", 0.0)
        + weights.get("W_LEX", 0.0) * components.get("lex", 0.0)
        + reach_contrib
        + weights.get("W_PROX", 0.0) * components.get("anchor_prox", 0.0)
        + weights.get("W_COMMIT", 0.0) * components.get("commit", 0.0)
    )
    w_hub = weights.get("W_HUB", 0.0)
    hub_sub = w_hub * hub_pen if evidence_pre_hub < w_hub else 0.0
    return evidence_pre_hub - hub_sub


def _record_from_ranked(
    bug: dict,
    ablation: str,
    ranked_full: list[dict],
    weights: dict,
    elapsed_ms: int,
) -> dict:
    candidates = [
        r for r in ranked_full
        if ablation != "A" or r.get("components", {}).get("sem", 0.0) > 0.0
    ]
    ranked = sorted(
        candidates,
        key=lambda rec: _weighted_score(rec.get("components", {}), weights, ablation),
        reverse=True,
    )
    gold = set(bug.get("gold_files", []))
    first_gold_rank = None
    for idx, rec in enumerate(ranked, start=1):
        if rec["path"] in gold:
            first_gold_rank = idx
            break
    return {
        "bug_id": bug["bug_id"],
        "repo": bug["repo"],
        "ablation": ablation,
        "MRR_full": round(1.0 / first_gold_rank, 4) if first_gold_rank else 0.0,
        "hit1": int(first_gold_rank == 1),
        "hit3": int(bool(first_gold_rank and first_gold_rank <= 3)),
        "hit5": int(bool(first_gold_rank and first_gold_rank <= 5)),
        "hit10": int(bool(first_gold_rank and first_gold_rank <= 10)),
        "gold_in_focus": int(any(rec["path"] in gold for rec in ranked[:3])),
        "first_gold_rank_full": first_gold_rank,
        "candidate_set_size": len(ranked),
        "elapsed_ms": elapsed_ms,
    }


def _record_from_result(r: V74BriefResult) -> dict:
    return {
        "bug_id": r.bug_id,
        "repo": r.repo,
        "ablation": r.ablation_variant,
        "MRR_full": r.first_gold_rank_full and round(1.0 / r.first_gold_rank_full, 4) or 0.0,
        "hit1": int(bool(r.first_gold_rank_full == 1)),
        "hit3": int(bool(r.first_gold_rank_full and r.first_gold_rank_full <= 3)),
        "hit5": int(bool(r.first_gold_rank_full and r.first_gold_rank_full <= 5)),
        "hit10": int(bool(r.first_gold_rank_full and r.first_gold_rank_full <= 10)),
        "gold_in_focus": int(r.gold_in_focus),
        "first_gold_rank_full": r.first_gold_rank_full,
        "candidate_set_size": r.candidate_set_size,
        "elapsed_ms": r.elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Phase: ablation
# ---------------------------------------------------------------------------

def run_ablation(
    bugs: list[dict],
    weights: dict,
    out_path: Path,
    workers: int,
    resume_ids: set[str],
) -> list[dict]:
    """Run all 5 ablation variants on eval bugs. Returns list of result records."""
    tasks = [
        (bug, abl)
        for abl in ABLATIONS
        for bug in bugs
        if f"{bug['bug_id']}_{abl}" not in resume_ids
    ]
    print(f"Ablation: {len(tasks)} tasks ({len(bugs)} bugs Ã— {len(ABLATIONS)} variants, workers={workers})")

    all_records: list[dict] = []
    done = 0

    def _task(item: tuple[dict, str]) -> dict:
        bug, abl = item
        r = _run_v74_bug(bug, abl, weights)
        return _record_from_result(r)

    with open(out_path, "a") as f:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, item): item for item in tasks}
            for fut in as_completed(futures):
                bug, abl = futures[fut]
                try:
                    rec = fut.result()
                    with _WRITE_LOCK:
                        f.write(json.dumps(rec) + "\n")
                        f.flush()
                    all_records.append(rec)
                    done += 1
                    if done % 20 == 0 or done == len(tasks):
                        print(f"  [{done}/{len(tasks)}] {rec['bug_id']} {abl} MRR={rec['MRR_full']:.4f}")
                except Exception as exc:
                    print(f"  [error] {bug['bug_id']} {abl}: {exc}", file=sys.stderr)

    print(f"Ablation complete. {len(all_records)} records -> {out_path}")
    return all_records


def run_ablation_fast(
    bugs: list[dict],
    weights: dict,
    out_path: Path,
    workers: int,
    resume_ids: set[str],
) -> list[dict]:
    """Optimized ablation: derive A/B1/C/D from one C run, run B0 separately."""
    tasks = [
        bug for bug in bugs
        if any(f"{bug['bug_id']}_{abl}" not in resume_ids for abl in ABLATIONS)
    ]
    print(f"Ablation: {len(tasks)} bug tasks ({len(bugs)} bugs x {len(ABLATIONS)} variants, workers={workers})")

    all_records: list[dict] = []
    done = 0

    def _task(bug: dict) -> list[dict]:
        records: list[dict] = []
        c_result = _run_v74_bug(bug, "C", weights)
        c_weights = {**weights, "W_COMMIT": 0.0}
        for abl in ("A", "B1", "C", "D"):
            if f"{bug['bug_id']}_{abl}" in resume_ids:
                continue
            if abl == "C":
                records.append(_record_from_result(c_result))
            else:
                records.append(_record_from_ranked(
                    bug,
                    abl,
                    c_result.ranked_full,
                    c_weights if abl != "D" else weights,
                    c_result.elapsed_ms,
                ))
        if f"{bug['bug_id']}_B0" not in resume_ids:
            b0_result = _run_v74_bug(bug, "B0", weights)
            records.append(_record_from_result(b0_result))
        return records

    with open(out_path, "a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, bug): bug for bug in tasks}
            for fut in as_completed(futures):
                bug = futures[fut]
                try:
                    recs = fut.result()
                    with _WRITE_LOCK:
                        for rec in recs:
                            f.write(json.dumps(rec) + "\n")
                        f.flush()
                    all_records.extend(recs)
                    done += 1
                    if done % 5 == 0 or done == len(tasks):
                        print(f"  [{done}/{len(tasks)}] {bug['bug_id']} wrote {len(recs)} records")
                except Exception as exc:
                    print(f"  [error] {bug['bug_id']}: {exc}", file=sys.stderr)

    print(f"Ablation complete. {len(all_records)} records -> {out_path}")
    return all_records


# ---------------------------------------------------------------------------
# Phase: gates
# ---------------------------------------------------------------------------

def _bootstrap_ci(deltas: list[float], n_boot: int = 10_000) -> tuple[float, float]:
    import random
    n = len(deltas)
    means = []
    for _ in range(n_boot):
        sample: list[float] = random.choices(deltas, k=n)  # type: ignore[assignment]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[int(0.975 * n_boot)]
    return lo, hi


def _wilcoxon_p(deltas: list[float]) -> float:
    try:
        from scipy.stats import wilcoxon
        _, p = wilcoxon(deltas, alternative="greater")
        return float(p)
    except ImportError:
        print("WARNING: scipy not available; Wilcoxon p set to 1.0", file=sys.stderr)
        return 1.0


def _load_baseline(path: Path) -> dict[str, float]:
    """Return {bug_id: MRR_full} from baseline CSV."""
    result: dict[str, float] = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            result[row["bug_id"]] = float(row["MRR_full"])
    return result


def run_gates(
    ablation_records: list[dict],
    baseline_path: Path,
    out_path: Path,
) -> dict[str, Any]:
    """Compute all 6 gates and write gate_verdict.json."""
    baseline = _load_baseline(baseline_path)

    by_variant: dict[str, dict[str, dict]] = {}
    for rec in ablation_records:
        by_variant.setdefault(rec["ablation"], {})[rec["bug_id"]] = rec

    c_recs = by_variant.get("C", {})

    shared_ids = [bid for bid in c_recs if bid in baseline]
    deltas_vs_baseline = [c_recs[bid]["MRR_full"] - baseline[bid] for bid in shared_ids]

    wilcoxon_p = _wilcoxon_p(deltas_vs_baseline) if deltas_vs_baseline else 1.0
    ci_lo, ci_hi = _bootstrap_ci(deltas_vs_baseline) if deltas_vs_baseline else (0.0, 0.0)
    mean_delta = statistics.mean(deltas_vs_baseline) if deltas_vs_baseline else 0.0

    baseline_hit3: dict[str, int] = {}
    baseline_cand_inner: dict[str, int] = {}
    baseline_hit10: dict[str, int] = {}
    with open(baseline_path) as f:
        for row in csv.DictReader(f):
            baseline_hit3[row["bug_id"]] = int(row.get("gold_in_focus", 0))
            baseline_cand_inner[row["bug_id"]] = int(row.get("candidate_set_size", 0))
            baseline_hit10[row["bug_id"]] = int(row.get("hit10", 0))
    c_hit3_rate = statistics.mean([c_recs[b]["gold_in_focus"] for b in c_recs]) if c_recs else 0.0
    v73_hit3_rate = statistics.mean([baseline_hit3.get(b, 0) for b in c_recs]) if c_recs else 0.0

    c_precision = (
        sum(c_recs[b]["gold_in_focus"] / 3.0 for b in c_recs)
        / max(len(c_recs), 1)
    )
    v73_precision = (
        sum(baseline_hit3.get(b, 0) / 3.0 for b in c_recs)
        / max(len(c_recs), 1)
    )

    c_cand_med = statistics.median([r["candidate_set_size"] for r in c_recs.values()]) if c_recs else 0
    v73_cand_med = statistics.median([baseline_cand_inner.get(b, 0) for b in c_recs]) if c_recs else 1
    bloat_ratio = c_cand_med / max(v73_cand_med, 1)

    old_hit_new_miss_hit3 = sum(
        1 for b in c_recs
        if baseline_hit3.get(b, 0) == 1 and c_recs[b]["gold_in_focus"] == 0
    )
    old_hit_new_miss_hit10 = sum(
        1 for b in c_recs
        if baseline_hit10.get(b, 0) == 1
        and c_recs[b]["hit10"] == 0
    )
    n = max(len(c_recs), 1)

    repo_deltas: dict[str, list[float]] = {}
    for bid in shared_ids:
        _repo = c_recs[bid]["repo"]
        repo_deltas.setdefault(_repo, []).append(c_recs[bid]["MRR_full"] - baseline[bid])
    positive_repos = sum(
        1 for deltas in repo_deltas.values()
        if statistics.mean(deltas) > 0
    )

    gates: dict[str, Any] = {
        "gate1_mrr_wilcoxon": {
            "PASS": wilcoxon_p < 0.05 and ci_lo > 0,
            "wilcoxon_p": round(wilcoxon_p, 4),
            "bootstrap_ci_lo": round(ci_lo, 4),
            "bootstrap_ci_hi": round(ci_hi, 4),
            "mean_delta": round(mean_delta, 4),
            "n_pairs": len(shared_ids),
            "threshold": "p<0.05 AND ci_lo>0",
        },
        "gate2_hit3_nondecreasing": {
            "PASS": c_hit3_rate >= v73_hit3_rate,
            "C_hit3_rate": round(c_hit3_rate, 3),
            "v73_hit3_rate": round(v73_hit3_rate, 3),
            "threshold": "C >= v7.3",
        },
        "gate3_focus_precision_nondecreasing": {
            "PASS": c_precision >= v73_precision,
            "C_precision": round(c_precision, 3),
            "v73_precision": round(v73_precision, 3),
            "threshold": "C >= v7.3",
        },
        "gate4_no_bloat": {
            "PASS": bloat_ratio <= 1.25,
            "C_median_cands": c_cand_med,
            "v73_median_cands": v73_cand_med,
            "bloat_ratio": round(bloat_ratio, 3),
            "threshold": "ratio <= 1.25",
        },
        "gate5_regression_cell": {
            "PASS": (old_hit_new_miss_hit3 / n <= 0.10) and (old_hit_new_miss_hit10 / n <= 0.10),
            "old_hit_new_miss_hit3": old_hit_new_miss_hit3,
            "old_hit_new_miss_hit10": old_hit_new_miss_hit10,
            "hit3_rate": round(old_hit_new_miss_hit3 / n, 3),
            "hit10_rate": round(old_hit_new_miss_hit10 / n, 3),
            "threshold": "both <= 10%",
        },
        "gate6_cross_repo": {
            "PASS": positive_repos >= 2,
            "positive_repos": positive_repos,
            "total_repos": len(repo_deltas),
            "per_repo": {r: round(statistics.mean(d), 4) for r, d in repo_deltas.items()},
            "threshold": ">=2 repos positive",
        },
    }

    all_pass = all(g["PASS"] for g in gates.values())
    verdict = {"PASS": all_pass, "gates": gates, "n_eval_bugs": n}

    # Identify best variant for step 3
    # D beats C if D has higher mean MRR on eval split (no significance test here â€” just pick)
    d_recs = by_variant.get("D", {})
    if d_recs and c_recs:
        d_mrr = statistics.mean([r["MRR_full"] for r in d_recs.values()])
        c_mrr = statistics.mean([r["MRR_full"] for r in c_recs.values()])
        verdict["best_variant"] = "D" if d_mrr > c_mrr else "C"
        verdict["C_mean_MRR"] = round(c_mrr, 4)
        verdict["D_mean_MRR"] = round(d_mrr, 4)
    else:
        verdict["best_variant"] = "C"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(verdict, f, indent=2)

    # Print gate table
    print(f"\n{'='*60}")
    print(f"Gate verdict: {'PASS' if all_pass else 'FAIL'} ({n} eval bugs)")
    print(f"{'='*60}")
    for name, g in gates.items():
        status = "PASS" if g["PASS"] else "FAIL"
        print(f"  {status}  {name}: {g}")
    print(f"\nBest variant for step 3: {verdict['best_variant']}")
    print(f"Written to {out_path}")
    return verdict


# ---------------------------------------------------------------------------
# Phase: step 3
# ---------------------------------------------------------------------------

def _v73_mrr(bug: dict) -> dict:
    issue_text = bug.get("issue_body") or bug.get("issue_title", "")
    result: V7BriefResult = generate_brief(  # type: ignore[assignment]
        issue_text, bug["repo_path"], bug["graph_db_path"], return_telemetry=True
    )
    gold_set = set(bug.get("gold_files") or [])
    candidate_files: list[str] = []
    for cand in result.candidates:
        fp = getattr(cand, "file", None)
        if fp and fp not in candidate_files:
            candidate_files.append(fp)
    for fp in result.cluster_files or []:
        if fp not in candidate_files:
            candidate_files.append(fp)
    first_gold = next((i + 1 for i, fp in enumerate(candidate_files) if fp in gold_set), None)
    return {
        "bug_id": bug["bug_id"],
        "ablation": "v73",
        "MRR_full": round(1.0 / first_gold, 4) if first_gold else 0.0,
        "hit3": int(bool(first_gold and first_gold <= 3)),
        "gold_in_focus": int(bool(first_gold and first_gold <= 3)),
        "first_gold_rank_full": first_gold,
        "candidate_set_size": len(candidate_files),
    }


def run_step3(
    task_manifest_path: Path,
    gate_verdict: dict,
    weights: dict,
    out_path: Path,
    workers: int,
) -> None:
    if not gate_verdict.get("PASS"):
        print("Step 3 skipped: gates did not pass.", file=sys.stderr)
        return

    if not task_manifest_path.exists():
        print(
            f"Step 3 skipped: task manifest not found at {task_manifest_path}\n"
            "Build it by running mine_holdout.py on the 15 SWE-bench task repos.",
            file=sys.stderr,
        )
        return

    bugs = load_jsonl(task_manifest_path)
    best_variant = gate_verdict.get("best_variant", "C")
    print(f"Step 3: running v7.3 + v7.4/{best_variant} on {len(bugs)} tasks (workers={workers})")

    def _task(bug: dict) -> list[dict]:
        rows = []
        r_v73 = _v73_mrr(bug)
        r_v73["task_id"] = bug.get("bug_id", "")
        rows.append(r_v73)
        r_v74 = _run_v74_bug(bug, best_variant, weights)
        mrr = round(1.0 / r_v74.first_gold_rank_full, 4) if r_v74.first_gold_rank_full else 0.0
        rows.append({
            "task_id": bug.get("bug_id", ""),
            "bug_id": r_v74.bug_id,
            "ablation": f"v74_{best_variant}",
            "MRR_full": mrr,
            "hit3": int(bool(r_v74.first_gold_rank_full and r_v74.first_gold_rank_full <= 3)),
            "gold_in_focus": int(r_v74.gold_in_focus),
            "first_gold_rank_full": r_v74.first_gold_rank_full,
            "candidate_set_size": r_v74.candidate_set_size,
        })
        return rows

    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_task, bug): bug["bug_id"] for bug in bugs}
        for fut in as_completed(futures):
            bug_id = futures[fut]
            try:
                rows = fut.result()
                all_rows.extend(rows)
                v73_mrr = next(r["MRR_full"] for r in rows if r["ablation"] == "v73")
                v74_mrr = next(r["MRR_full"] for r in rows if r["ablation"] != "v73")
                delta = v74_mrr - v73_mrr
                print(f"  {bug_id}: v73={v73_mrr:.4f} v74={v74_mrr:.4f} Î”={delta:+.4f}")
            except Exception as exc:
                print(f"  [error] {bug_id}: {exc}", file=sys.stderr)

    if all_rows:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(all_rows[0].keys())
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"Step 3 results -> {out_path}")

        # Direction consistency check
        v74_rows = [r for r in all_rows if r["ablation"] != "v73"]
        v73_rows = [r for r in all_rows if r["ablation"] == "v73"]
        if v74_rows and v73_rows:
            v73_mean = statistics.mean(r["MRR_full"] for r in v73_rows)
            v74_mean = statistics.mean(r["MRR_full"] for r in v74_rows)
            direction_ok = (v74_mean > v73_mean) == (gate_verdict.get("gates", {})
                           .get("gate1_mrr_wilcoxon", {}).get("mean_delta", 0) > 0)
            print(f"\nDirection consistency: v73={v73_mean:.4f} v74={v74_mean:.4f}"
                  f" {'âœ… matches holdout direction' if direction_ok else 'âŒ DIVERGES from holdout'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["ablation", "gates", "step3", "all"],
                        default="all")
    parser.add_argument("--holdout", default="holdout_v1.jsonl")
    parser.add_argument("--baseline", default="results/baseline_holdout.csv")
    parser.add_argument("--coefficients", default="results/coefficients_v1.json")
    parser.add_argument("--ablation-out", default="results/holdout_ablation.jsonl")
    parser.add_argument("--gate-out", default="results/gate_verdict.json")
    parser.add_argument("--step3-manifest", default="",
                        help="JSONL with same schema as holdout_v1.jsonl for the 15 tasks")
    parser.add_argument("--step3-out", default="results/step3_results.csv")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 8))
    parser.add_argument("--calibration-n", type=int, default=20,
                        help="Number of calibration bugs (skip these from eval split)")
    args = parser.parse_args()

    holdout_path = Path(args.holdout)
    if not holdout_path.exists():
        print(f"ERROR: {holdout_path} not found", file=sys.stderr)
        return 1

    all_bugs = load_jsonl(holdout_path)
    eval_bugs = all_bugs[args.calibration_n:]  # bugs 21+ are the eval split
    print(f"Loaded {len(all_bugs)} total bugs; eval split = {len(eval_bugs)} bugs")

    # Load calibrated weights
    weights: dict[str, float] = {}
    coeff_path = Path(args.coefficients)
    if coeff_path.exists():
        with open(coeff_path) as f:
            coeff = json.load(f)
        weights = {k: coeff[k] for k in ("W_SEM", "W_LEX", "W_REACH", "W_PROX", "W_HUB", "W_COMMIT") if k in coeff}
        print(f"Loaded weights: {weights}")
    else:
        print(f"WARNING: {coeff_path} not found â€” using default weights", file=sys.stderr)

    ablation_path = Path(args.ablation_out)
    gate_path = Path(args.gate_out)

    # Resume support: check which (bug_id, ablation) pairs are already done
    resume_ids: set[str] = set()
    if ablation_path.exists():
        for rec in load_jsonl(ablation_path):
            resume_ids.add(f"{rec['bug_id']}_{rec['ablation']}")
        print(f"Resume: {len(resume_ids)} ablation records already done")

    ablation_records: list[dict] = []

    if args.phase in ("ablation", "all"):
        if ablation_path.exists():
            ablation_records = load_jsonl(ablation_path)
        ablation_records.extend(
            run_ablation_fast(eval_bugs, weights, ablation_path, args.workers, resume_ids)
        )
        # Reload to get complete set
        ablation_records = load_jsonl(ablation_path)

    if args.phase in ("gates", "all"):
        if not ablation_records and ablation_path.exists():
            ablation_records = load_jsonl(ablation_path)
        if not Path(args.baseline).exists():
            print(f"ERROR: baseline not found at {args.baseline}", file=sys.stderr)
            return 1
        gate_verdict = run_gates(
            ablation_records, Path(args.baseline), gate_path
        )
    else:
        gate_verdict = {}
        if gate_path.exists():
            with open(gate_path) as f:
                gate_verdict = json.load(f)

    if args.phase in ("step3", "all"):
        manifest_path = Path(args.step3_manifest) if args.step3_manifest else Path("15tasks.jsonl")
        run_step3(
            manifest_path, gate_verdict, weights, Path(args.step3_out), args.workers
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
