"""Phase 1: Validate the Abstention Envelope formula.

Extends measure_v1r_localization.py: same infra (checkout, build graph.db,
load SWE-bench-Live dataset), but computes envelope confidence metrics and
checks whether they separate brief-correct from brief-wrong.

Two SEPARATE analyses (never collapse these):
  1A: Confidence vs Gold Inclusion — does high confidence predict retrieval accuracy?
  1B: Confidence vs Resolved Outcome — informational only, confounded by model/difficulty.

Usage:
  python scripts/analysis/phase1_envelope_validation.py --output D:/tmp/envelope_analysis.json

Requires: same setup as measure_v1r_localization.py (repos in D:/tmp/gt_test,
gt-index.exe built, HuggingFace datasets installed).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# --- Infrastructure (from measure_v1r_localization.py) ---

GT_INDEX = str(Path(__file__).resolve().parents[2] / "gt-index" / "gt-index.exe")
TEST_DIR = "D:/tmp/gt_test"

TASKS: dict[str, tuple[str, str]] = {
    "aiogram__aiogram-1594": ("aiogram", "51beb482"),
    "aws-cloudformation__cfn-lint-3789": ("cfn-lint", "74847b14"),
    "aws-cloudformation__cfn-lint-3798": ("cfn-lint", "d5c3da9e"),
    "aws-cloudformation__cfn-lint-3821": ("cfn-lint", "9d83f3fb"),
    "aws-cloudformation__cfn-lint-3854": ("cfn-lint", "6d083eb3"),
    "aws-cloudformation__cfn-lint-3856": ("cfn-lint", "19192ef6"),
    "aws-cloudformation__cfn-lint-3862": ("cfn-lint", "30ecbc1f"),
    "aws-cloudformation__cfn-lint-3866": ("cfn-lint", "30ecbc1f"),
    "aws-cloudformation__cfn-lint-3875": ("cfn-lint", "8454bc9a"),
    "aws-cloudformation__cfn-lint-3890": ("cfn-lint", "700563cd"),
    "aws-cloudformation__cfn-lint-4002": ("cfn-lint", "e6278b45"),
    "aws-cloudformation__cfn-lint-4023": ("cfn-lint", "0bf508f3"),
    "aws-cloudformation__cfn-lint-4032": ("cfn-lint", "7351d0cf"),
    "aws-cloudformation__cfn-lint-3779": ("cfn-lint", "de28d1e4"),
    "aws-cloudformation__cfn-lint-3805": ("cfn-lint", "88efb088"),
    "aws-cloudformation__cfn-lint-4016": ("cfn-lint", "65136702"),
    "beancount__beancount-931": ("beancount", "a0e6f445"),
    "beetbox__beets-5495": ("beets", "fa10dcf1"),
    "beeware__briefcase-2075": ("briefcase", "98b3cb01"),
    "beeware__briefcase-2085": ("briefcase", "40052023"),
    "bridgecrewio__checkov-6893": ("checkov", "7741985f"),
    "bridgecrewio__checkov-6895": ("checkov", "a94c1682"),
    "bridgecrewio__checkov-7002": ("checkov", "8b0f288a"),
    "arviz-devs__arviz-2413": ("arviz", "0fc11178"),
    "delgan__loguru-1306": ("loguru", "3cfd03fb"),
    "kozea__weasyprint-2303": ("WeasyPrint", "d0fcb3c4"),
    "pydata__xarray-9760": ("xarray", "5a9ff0be"),
    "pydata__xarray-9971": ("xarray", "4ccb048d"),
    "pylint-dev__pylint-10044": ("pylint", "2e0c41f6"),
    "pypa__twine-1225": ("twine", "aa3a910c"),
}

# Resolved tasks from our eval run (no timeout, 2026-05-10)
RESOLVED = {
    "aws-cloudformation__cfn-lint-3798",
    "aws-cloudformation__cfn-lint-3854",
    "aws-cloudformation__cfn-lint-3856",
    "aws-cloudformation__cfn-lint-3890",
    "aws-cloudformation__cfn-lint-4002",
    "aws-cloudformation__cfn-lint-4032",
    "beeware__briefcase-2075",
    "bridgecrewio__checkov-6895",
    "bridgecrewio__checkov-7002",
}

# Baseline resolved (without GT)
BASELINE_RESOLVED = {
    "beancount__beancount-931",
    "kozea__weasyprint-2303",
    "pypa__twine-1225",
    "aws-cloudformation__cfn-lint-3821",
}


def gold_files_from_patch(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].strip()
            if p and p != "/dev/null" and p not in files:
                files.append(p)
    return files


def checkout(repo_dir: str, commit: str) -> bool:
    r = subprocess.run(
        ["git", "checkout", commit, "--force"],
        cwd=repo_dir, capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0


def build_graph(repo_dir: str, db_path: str) -> bool:
    r = subprocess.run(
        [GT_INDEX, f"-root={repo_dir}", f"-output={db_path}"],
        capture_output=True, text=True, timeout=300,
    )
    return r.returncode == 0


# --- Envelope Confidence Computation ---

@dataclass
class EnvelopeResult:
    task_id: str
    agreement: float
    separation: float
    redundancy: float
    confidence: float
    gold_in_top5: bool
    gold_in_top3: bool
    resolved: bool
    top5_paths: list[str]
    edges_per_file: float
    p90_in_degree: float
    total_edges: int
    total_files: int
    gold_files: list[str]
    first_gold_rank: int | None


def compute_agreement(ranked_full: list[dict], k: int = 5) -> float:
    """How much do different signals agree on the top-K candidates?

    1.0 = all signals point to the same files (unanimous agreement)
    0.0 = each signal nominates a completely different file (max disagreement)

    Method: For each of the top-K files, compute the coefficient of variation
    (std/mean) of its component scores. Average across top-K.
    High agreement = each file scores high on MULTIPLE signals.
    Low agreement = files score high on only ONE signal each.
    """
    top_k = ranked_full[:k]
    if not top_k:
        return 0.0

    signal_keys = ["lex", "reach", "anchor_prox"]  # skip sem (often 0), hub_pen (negative)

    agreements = []
    for entry in top_k:
        components = entry.get("components", {})
        scores = [components.get(s, 0.0) for s in signal_keys]
        nonzero = [s for s in scores if s > 0.01]
        if len(nonzero) >= 2:
            # Multiple signals contributed — high agreement for this candidate
            mean = sum(nonzero) / len(nonzero)
            variance = sum((s - mean) ** 2 for s in nonzero) / len(nonzero)
            cv = math.sqrt(variance) / mean if mean > 0 else 1.0
            # cv close to 0 = signals balanced = high agreement
            # cv > 1 = one signal dominates = low agreement
            agreements.append(max(0.0, 1.0 - cv))
        elif len(nonzero) == 1:
            # Only one signal fired — low agreement
            agreements.append(0.3)
        else:
            agreements.append(0.0)

    return sum(agreements) / len(agreements) if agreements else 0.0


def compute_separation(ranked_full: list[dict], k: int = 5) -> float:
    """Is there a clear winner, or is the distribution flat?

    1.0 = rank-1 dominates (large gap between 1st and K-th)
    0.0 = all scores are the same (flat distribution, pure noise)
    """
    if len(ranked_full) < 2:
        return 1.0  # trivial case

    scores = [entry.get("score", 0.0) for entry in ranked_full[:max(k + 1, len(ranked_full))]]
    if not scores or scores[0] == 0:
        return 0.0

    k_actual = min(k, len(scores) - 1)
    gap = (scores[0] - scores[k_actual]) / scores[0]
    return max(0.0, min(1.0, gap))


def compute_redundancy(ranked_full: list[dict], k: int = 5) -> float:
    """Did candidates enter via multiple independent paths?

    1.0 = all top-K entered via both semantic_seed and graph_rescue
    0.0 = all entered via only one path
    """
    top_k = ranked_full[:k]
    if not top_k:
        return 0.0

    multi_path = sum(
        1 for entry in top_k
        if entry.get("entered_via", "") == "both"
    )
    return multi_path / len(top_k)


def compute_envelope(ranked_full: list[dict], k: int = 5) -> tuple[float, float, float, float]:
    """Compute the full envelope confidence scalar."""
    agreement = compute_agreement(ranked_full, k)
    separation = compute_separation(ranked_full, k)
    redundancy = compute_redundancy(ranked_full, k)

    # Composite: multiplicative (all three must be present for high confidence)
    confidence = agreement * separation * max(0.1, redundancy)  # floor redundancy at 0.1 to not zero-out

    return agreement, separation, redundancy, confidence


def compute_graph_metrics(graph_db: str) -> tuple[float, float, int, int]:
    """Compute repo-relative graph metrics: edges_per_file, p90_in_degree."""
    conn = sqlite3.connect(graph_db)

    total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    total_files = conn.execute(
        "SELECT COUNT(DISTINCT file_path) FROM nodes"
    ).fetchone()[0]

    edges_per_file = total_edges / max(1, total_files)

    # In-degree per file
    in_degrees = [r[0] for r in conn.execute("""
        SELECT COUNT(e.id) as deg
        FROM nodes n
        JOIN edges e ON e.target_id = n.id
        GROUP BY n.file_path
        ORDER BY deg
    """).fetchall()]

    conn.close()

    if in_degrees:
        p90_idx = int(len(in_degrees) * 0.9)
        p90_in_degree = float(in_degrees[min(p90_idx, len(in_degrees) - 1)])
    else:
        p90_in_degree = 0.0

    return edges_per_file, p90_in_degree, total_edges, total_files


def check_gold_in_topk(ranked_full: list[dict], gold_files: list[str], k: int) -> tuple[bool, int | None]:
    """Check if any gold file appears in top-K candidates."""
    top_k_paths = [entry.get("path", "") for entry in ranked_full[:k]]

    for rank, path in enumerate(top_k_paths, 1):
        for gold in gold_files:
            if path.endswith(gold) or gold.endswith(path) or path == gold:
                return True, rank

    # Check beyond top-K for first_gold_rank
    for rank, entry in enumerate(ranked_full, 1):
        path = entry.get("path", "")
        for gold in gold_files:
            if path.endswith(gold) or gold.endswith(path) or path == gold:
                return (rank <= k), rank

    return False, None


def run_v74_for_task(
    task_id: str,
    graph_db: str,
    repo_root: str,
    issue_text: str,
    gold_files: list[str],
) -> EnvelopeResult | None:
    """Run V7.4 scorer and compute envelope metrics for one task."""
    try:
        from groundtruth.pretask.v7_4_brief import run_v74
    except ImportError:
        print(f"ERROR: Cannot import v7_4_brief. Ensure src/ is on path.")
        return None

    try:
        v74 = run_v74(
            issue_text,
            repo_root,
            graph_db,
            bug_id=task_id,
            repo=task_id.rsplit("-", 1)[0],
            gold_files=gold_files,
            ablation="C",
            k_anchor=3,
            k_sem_top=10,
            tau_anchor=0.20,
            max_depth=3,
            min_confidence=0.5,
            focus_size=5,
        )
    except Exception as e:
        print(f"ERROR running v74 for {task_id}: {e}")
        return None

    ranked_full = v74.ranked_full if v74.ranked_full else []

    agreement, separation, redundancy, confidence = compute_envelope(ranked_full, k=5)
    gold_in_top5, first_gold_rank = check_gold_in_topk(ranked_full, gold_files, k=5)
    gold_in_top3, _ = check_gold_in_topk(ranked_full, gold_files, k=3)

    edges_per_file, p90_in_degree, total_edges, total_files = compute_graph_metrics(graph_db)

    top5_paths = [entry.get("path", "") for entry in ranked_full[:5]]

    return EnvelopeResult(
        task_id=task_id,
        agreement=round(agreement, 4),
        separation=round(separation, 4),
        redundancy=round(redundancy, 4),
        confidence=round(confidence, 4),
        gold_in_top5=gold_in_top5,
        gold_in_top3=gold_in_top3,
        resolved=task_id in RESOLVED,
        top5_paths=top5_paths,
        edges_per_file=round(edges_per_file, 2),
        p90_in_degree=p90_in_degree,
        total_edges=total_edges,
        total_files=total_files,
        gold_files=gold_files,
        first_gold_rank=first_gold_rank,
    )


def print_analysis(results: list[EnvelopeResult]) -> None:
    """Print the Phase 1A/1B analysis tables."""
    print("\n" + "=" * 90)
    print("PHASE 1A: Confidence vs Gold Inclusion (Retrieval Quality)")
    print("=" * 90)
    print(f"{'Task':<45} {'Conf':>6} {'Agree':>6} {'Sep':>5} {'Red':>5} {'Gold@5':>6} {'GoldRk':>6}")
    print("-" * 90)

    for r in sorted(results, key=lambda x: x.confidence, reverse=True):
        gold_str = "YES" if r.gold_in_top5 else "no"
        rank_str = str(r.first_gold_rank) if r.first_gold_rank else "-"
        print(f"{r.task_id:<45} {r.confidence:>6.3f} {r.agreement:>6.3f} {r.separation:>5.3f} {r.redundancy:>5.2f} {gold_str:>6} {rank_str:>6}")

    # Separation analysis
    gold_yes = [r for r in results if r.gold_in_top5]
    gold_no = [r for r in results if not r.gold_in_top5]

    if gold_yes:
        avg_conf_yes = sum(r.confidence for r in gold_yes) / len(gold_yes)
        print(f"\nMean confidence (gold IN top5):  {avg_conf_yes:.4f} (n={len(gold_yes)})")
    if gold_no:
        avg_conf_no = sum(r.confidence for r in gold_no) / len(gold_no)
        print(f"Mean confidence (gold NOT top5): {avg_conf_no:.4f} (n={len(gold_no)})")

    if gold_yes and gold_no:
        avg_yes = sum(r.confidence for r in gold_yes) / len(gold_yes)
        avg_no = sum(r.confidence for r in gold_no) / len(gold_no)
        effect = avg_yes - avg_no
        print(f"Separation (effect size):        {effect:+.4f}")
        if effect > 0.05:
            print("→ POSITIVE: envelope separates correct from incorrect briefs")
        elif effect > 0:
            print("→ WEAK: some separation but not clean")
        else:
            print("→ NEGATIVE: envelope does NOT separate. Formula needs revision.")

    print("\n" + "=" * 90)
    print("PHASE 1B: Confidence vs Resolved (End-to-End, Confounded)")
    print("=" * 90)

    resolved = [r for r in results if r.resolved]
    not_resolved = [r for r in results if not r.resolved]

    if resolved:
        avg_conf_res = sum(r.confidence for r in resolved) / len(resolved)
        print(f"Mean confidence (RESOLVED):      {avg_conf_res:.4f} (n={len(resolved)})")
    if not_resolved:
        avg_conf_nres = sum(r.confidence for r in not_resolved) / len(not_resolved)
        print(f"Mean confidence (NOT RESOLVED):  {avg_conf_nres:.4f} (n={len(not_resolved)})")

    print("\n" + "=" * 90)
    print("PHASE 1C: Repo-Relative Metrics")
    print("=" * 90)
    print(f"{'Task':<45} {'Edges/File':>10} {'p90 Deg':>8} {'TotEdges':>9} {'TotFiles':>9}")
    print("-" * 90)
    for r in sorted(results, key=lambda x: x.edges_per_file, reverse=True):
        print(f"{r.task_id:<45} {r.edges_per_file:>10.2f} {r.p90_in_degree:>8.0f} {r.total_edges:>9} {r.total_files:>9}")

    # Key question: do regressions (beancount, weasyprint, twine) have low confidence?
    regression_tasks = {"beancount__beancount-931", "kozea__weasyprint-2303", "pypa__twine-1225"}
    flip_tasks = RESOLVED

    print("\n" + "=" * 90)
    print("KEY QUESTION: Do regressions cluster at LOW confidence?")
    print("=" * 90)

    regressions = [r for r in results if r.task_id in regression_tasks]
    flips = [r for r in results if r.task_id in flip_tasks]

    if regressions:
        print(f"\nRegressions (n={len(regressions)}):")
        for r in regressions:
            print(f"  {r.task_id}: confidence={r.confidence:.4f}, gold@5={r.gold_in_top5}, edges/file={r.edges_per_file:.1f}")
    if flips:
        print(f"\nFlips (n={len(flips)}):")
        for r in flips:
            print(f"  {r.task_id}: confidence={r.confidence:.4f}, gold@5={r.gold_in_top5}, edges/file={r.edges_per_file:.1f}")

    if regressions and flips:
        reg_conf = sum(r.confidence for r in regressions) / len(regressions)
        flip_conf = sum(r.confidence for r in flips) / len(flips)
        print(f"\nMean confidence — regressions: {reg_conf:.4f}, flips: {flip_conf:.4f}")
        if reg_conf < flip_conf:
            print("→ HYPOTHESIS SUPPORTED: regressions have lower confidence than flips")
        else:
            print("→ HYPOTHESIS NOT SUPPORTED: formula does not distinguish regressions from flips")


def main():
    parser = argparse.ArgumentParser(description="Phase 1: Validate Abstention Envelope")
    parser.add_argument("--output", default="D:/tmp/envelope_analysis.json", help="Output JSON path")
    parser.add_argument("--tasks", nargs="*", help="Specific task IDs (default: all 30)")
    args = parser.parse_args()

    # Load SWE-bench-Live dataset for issue text + gold files
    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="test")
    issue_map: dict[str, str] = {}
    gold_map: dict[str, list[str]] = {}
    for row in ds:
        iid = row["instance_id"]
        if iid in TASKS:
            issue_map[iid] = row["problem_statement"]
            gold_map[iid] = gold_files_from_patch(row["patch"])

    tasks_to_run = args.tasks or list(TASKS.keys())
    db_cache: dict[str, str] = {}
    results: list[EnvelopeResult] = []

    print(f"Running Phase 1 envelope validation on {len(tasks_to_run)} tasks...")
    print()

    for tid in tasks_to_run:
        if tid not in TASKS:
            print(f"SKIP {tid}: not in task list")
            continue

        repo_name, commit = TASKS[tid]
        repo_dir = os.path.join(TEST_DIR, repo_name)
        cache_key = f"{repo_name}_{commit}"
        db_path = os.path.join(TEST_DIR, f"{cache_key}.db")

        # Build graph.db if not cached
        if cache_key not in db_cache:
            if not os.path.exists(db_path):
                if not os.path.isdir(repo_dir):
                    print(f"SKIP {tid}: repo dir not found at {repo_dir}")
                    continue
                if not checkout(repo_dir, commit):
                    print(f"SKIP {tid}: checkout failed")
                    continue
                if not build_graph(repo_dir, db_path):
                    print(f"SKIP {tid}: index build failed")
                    continue
            db_cache[cache_key] = db_path

        issue_text = issue_map.get(tid, "")
        gold_files = gold_map.get(tid, [])

        if not issue_text:
            print(f"SKIP {tid}: no issue text in dataset")
            continue

        result = run_v74_for_task(tid, db_path, repo_dir, issue_text, gold_files)
        if result:
            results.append(result)
            gold_str = "YES" if result.gold_in_top5 else "no"
            print(f"  {tid}: conf={result.confidence:.3f} gold@5={gold_str} edges/file={result.edges_per_file:.1f}")

    if results:
        print_analysis(results)

        output_data = {
            "metadata": {
                "n_tasks": len(results),
                "n_resolved": sum(1 for r in results if r.resolved),
                "n_gold_in_top5": sum(1 for r in results if r.gold_in_top5),
                "n_baseline_resolved": sum(1 for r in results if r.task_id in BASELINE_RESOLVED),
            },
            "results": [asdict(r) for r in results],
        }

        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nResults written to: {args.output}")
    else:
        print("ERROR: No results produced. Check repo dirs and gt-index binary.")


if __name__ == "__main__":
    main()
