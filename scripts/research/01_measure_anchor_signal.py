"""G1 Validation: Does issue text predict gold files?

Measures how often issue-mentioned identifiers overlap with gold files
across 60 non-benchmark bugs in holdout_v1.jsonl.

Metrics:
- anchor_hit_rate: bugs where >=1 gold file contains an issue-mentioned identifier
- anchor_precision: of files matching issue identifiers, what fraction are gold?
- anchor_noise: how many non-gold files also match?
- big vs small repo comparison

Uses existing src/groundtruth/pretask/anchors.py for extraction.
Gold files used ONLY for offline correlation measurement.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from groundtruth.pretask.anchors import extract_issue_anchors

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"

BIG_REPOS = {"dagster-io/dagster", "marimo-team/marimo"}
SMALL_REPOS = {"tokio-rs/axum", "honojs/hono", "crossplane/crossplane"}


@dataclass
class BugResult:
    bug_id: str
    repo: str
    language: str
    total_source_files: int
    anchors_extracted: int
    anchors_in_graph: int
    paths_mentioned: int
    gold_files: list
    gold_files_hit_by_anchor: list
    non_gold_files_hit: int
    random_baseline_hit_prob: float


def measure_anchor_signal():
    bugs = []
    with open(HOLDOUT_PATH) as f:
        for line in f:
            bugs.append(json.loads(line))

    results = []

    for bug in bugs:
        bug_id = bug["bug_id"]
        repo = bug["repo"]
        issue_text = (bug.get("issue_title", "") or "") + "\n" + (bug.get("issue_body", "") or "")
        gold_files = bug.get("gold_files", [])
        graph_db_path = bug.get("graph_db_path", "")
        total_files = bug.get("total_source_files", 0)

        # Extract anchors (with graph cross-check if db exists)
        db_path = graph_db_path if os.path.exists(graph_db_path) else None
        anchors = extract_issue_anchors(issue_text, db_path)

        # Match anchors against gold file paths
        gold_basenames = {Path(gf).stem for gf in gold_files}
        gold_path_parts = set()
        for gf in gold_files:
            for part in Path(gf).parts:
                if len(part) >= 3:
                    gold_path_parts.add(part.replace(".py", "").replace(".ts", "").replace(".go", "").replace(".rs", ""))

        # Check: do any extracted anchors match gold file names or path components?
        anchor_set = anchors.symbols | anchors.paths | anchors.test_names
        raw_anchors = anchors.symbols_raw

        gold_hit = []
        for gf in gold_files:
            gf_stem = Path(gf).stem
            gf_parts = {p.replace(".py","").replace(".ts","").replace(".go","").replace(".rs","")
                       for p in Path(gf).parts if len(p) >= 3}
            # Check if any anchor matches gold file stem or path parts
            hit = False
            for anc in anchor_set | raw_anchors:
                anc_lower = anc.lower().replace("-", "_")
                if anc_lower == gf_stem.lower().replace("-", "_"):
                    hit = True
                    break
                if anc_lower in {p.lower().replace("-", "_") for p in gf_parts}:
                    hit = True
                    break
                # Check path mentions
                if gf in anchors.paths or any(gf.endswith(p) for p in anchors.paths):
                    hit = True
                    break
            if hit:
                gold_hit.append(gf)

        # Count non-gold files matching anchors (noise measurement)
        non_gold_hit = 0
        if db_path:
            try:
                conn = sqlite3.connect(db_path)
                # Get all unique file paths from graph
                all_files = {r[0] for r in conn.execute("SELECT DISTINCT file_path FROM nodes").fetchall()}
                conn.close()

                gold_set = set(gold_files)
                for fpath in all_files:
                    if fpath in gold_set:
                        continue
                    f_stem = Path(fpath).stem.lower().replace("-", "_")
                    for anc in anchor_set | raw_anchors:
                        if anc.lower().replace("-", "_") == f_stem:
                            non_gold_hit += 1
                            break
            except sqlite3.Error:
                pass

        # Random baseline: P(random file is gold) = len(gold_files) / total_files
        random_prob = len(gold_files) / max(total_files, 1)

        results.append(BugResult(
            bug_id=bug_id,
            repo=repo,
            language=bug.get("language", ""),
            total_source_files=total_files,
            anchors_extracted=len(raw_anchors),
            anchors_in_graph=len(anchors.symbols),
            paths_mentioned=len(anchors.paths),
            gold_files=gold_files,
            gold_files_hit_by_anchor=gold_hit,
            non_gold_files_hit=non_gold_hit,
            random_baseline_hit_prob=random_prob,
        ))

    # Compute aggregate metrics
    print("=" * 70)
    print("G1 VALIDATION: Issue Anchor -> Gold File Signal")
    print("=" * 70)
    print(f"\nDataset: holdout_v1.jsonl ({len(results)} bugs, 5 repos)")
    print(f"Safety: Non-benchmark repos only (axum, hono, crossplane, dagster, marimo)")
    print()

    # Per-repo results
    repos = sorted(set(r.repo for r in results))
    print(f"{'Repo':<25} {'N':>3} {'Hit%':>6} {'Prec':>6} {'Noise':>6} {'Anchors':>8} {'InGraph':>8}")
    print("-" * 70)

    for repo in repos:
        repo_results = [r for r in results if r.repo == repo]
        n = len(repo_results)
        hits = sum(1 for r in repo_results if r.gold_files_hit_by_anchor)
        hit_rate = hits / n if n else 0

        # Precision: of all anchor matches, what fraction are gold?
        total_gold_hit = sum(len(r.gold_files_hit_by_anchor) for r in repo_results)
        total_non_gold_hit = sum(r.non_gold_files_hit for r in repo_results)
        precision = total_gold_hit / max(total_gold_hit + total_non_gold_hit, 1)

        avg_anchors = sum(r.anchors_extracted for r in repo_results) / n
        avg_in_graph = sum(r.anchors_in_graph for r in repo_results) / n
        avg_noise = total_non_gold_hit / n

        print(f"{repo:<25} {n:>3} {hit_rate:>5.1%} {precision:>5.2f} {avg_noise:>6.1f} {avg_anchors:>8.1f} {avg_in_graph:>8.1f}")

    # Big vs small
    print("\n" + "=" * 70)
    print("BIG REPO vs SMALL REPO COMPARISON")
    print("=" * 70)

    for label, repo_set in [("BIG (dagster+marimo)", BIG_REPOS), ("SMALL (axum+hono+crossplane)", SMALL_REPOS)]:
        subset = [r for r in results if r.repo in repo_set]
        n = len(subset)
        hits = sum(1 for r in subset if r.gold_files_hit_by_anchor)
        hit_rate = hits / n if n else 0
        avg_noise = sum(r.non_gold_files_hit for r in subset) / max(n, 1)
        avg_random = sum(r.random_baseline_hit_prob for r in subset) / max(n, 1)
        print(f"\n{label} (n={n}):")
        print(f"  anchor_hit_rate:   {hit_rate:.1%} ({hits}/{n} bugs have >=1 gold file matching an anchor)")
        print(f"  avg_noise:         {avg_noise:.1f} non-gold files also match anchors")
        print(f"  random_baseline:   {avg_random:.4f} (expected hit if selecting files at random)")
        if avg_random > 0:
            print(f"  signal/random:     {hit_rate/avg_random:.1f}x above random chance")

    # Overall
    print("\n" + "=" * 70)
    print("OVERALL")
    print("=" * 70)
    n = len(results)
    hits = sum(1 for r in results if r.gold_files_hit_by_anchor)
    hit_rate = hits / n
    avg_random = sum(r.random_baseline_hit_prob for r in results) / n
    print(f"  anchor_hit_rate:   {hit_rate:.1%} ({hits}/{n})")
    print(f"  random_baseline:   {avg_random:.4f}")
    print(f"  signal/random:     {hit_rate/avg_random:.1f}x" if avg_random > 0 else "  signal/random:     inf")

    # Validation verdict
    print("\n" + "=" * 70)
    print("VALIDATION VERDICT")
    print("=" * 70)
    repos_with_hits = sum(1 for repo in repos
                         if sum(1 for r in results if r.repo == repo and r.gold_files_hit_by_anchor) > 0)
    if hit_rate > 0.40 and repos_with_hits >= 3:
        print("  G1 VALIDATED: anchor_hit_rate > 40% across >=3 repos")
    elif hit_rate < 0.25:
        print("  G1 FALSIFIED: anchor_hit_rate < 25%")
    else:
        print(f"  G1 INCONCLUSIVE: hit_rate={hit_rate:.1%}, repos_with_hits={repos_with_hits}")
        print("  Need supplementary data from mined PRs to strengthen/weaken")

    # Per-bug detail for inspection
    print("\n" + "=" * 70)
    print("PER-BUG DETAIL (first 10)")
    print("=" * 70)
    for r in results[:10]:
        hit_mark = "HIT" if r.gold_files_hit_by_anchor else "MISS"
        print(f"\n  [{hit_mark}] {r.bug_id} ({r.repo}, {r.language})")
        print(f"    Gold files: {r.gold_files}")
        print(f"    Anchors extracted: {r.anchors_extracted}, in graph: {r.anchors_in_graph}")
        print(f"    Gold matched by anchor: {r.gold_files_hit_by_anchor}")
        print(f"    Non-gold noise: {r.non_gold_files_hit} files")


if __name__ == "__main__":
    measure_anchor_signal()
