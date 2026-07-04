"""G3 Validation: Does graph BFS explode in big repos?

Measures BFS candidate counts at hops 1, 2, 3 from issue anchors,
comparing big repos (dagster 47K nodes, marimo 29K nodes) vs small repos.

Also measures: confidence floor effect, name_match ratio, gold reachability.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from groundtruth.pretask.anchors import extract_issue_anchors

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"

BIG_REPOS = {"dagster-io/dagster", "marimo-team/marimo"}
SMALL_REPOS = {"tokio-rs/axum", "honojs/hono", "crossplane/crossplane"}


def get_edge_stats(db_path):
    """Get edge type distribution and confidence stats."""
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # Resolution method distribution
    methods = {}
    for row in conn.execute("SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method"):
        methods[row[0] or "unknown"] = row[1]

    # Confidence distribution
    has_confidence = False
    try:
        avg_conf = conn.execute("SELECT AVG(confidence) FROM edges WHERE confidence > 0").fetchone()[0]
        high_conf = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.7").fetchone()[0]
        low_conf = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence < 0.5").fetchone()[0]
        has_confidence = avg_conf is not None
    except sqlite3.OperationalError:
        avg_conf = None
        high_conf = 0
        low_conf = 0

    conn.close()

    name_match = methods.get("name_match", 0)
    name_match_ratio = name_match / max(total, 1)

    return {
        "total_edges": total,
        "name_match_ratio": name_match_ratio,
        "methods": methods,
        "has_confidence": has_confidence,
        "avg_confidence": avg_conf,
        "high_conf_count": high_conf,
        "low_conf_count": low_conf,
        "high_conf_ratio": high_conf / max(total, 1),
    }


def bfs_from_anchors(db_path, anchor_nodes, max_depth=3, min_confidence=0.0):
    """BFS from anchor nodes, returning files discovered at each depth."""
    conn = sqlite3.connect(db_path)

    # Map anchor names to node IDs
    if not anchor_nodes:
        conn.close()
        return {1: set(), 2: set(), 3: set()}

    placeholders = ",".join("?" for _ in anchor_nodes)
    seed_ids = set()
    for row in conn.execute(
        f"SELECT id, file_path FROM nodes WHERE name IN ({placeholders})",
        tuple(anchor_nodes)
    ):
        seed_ids.add(row[0])

    if not seed_ids:
        conn.close()
        return {1: set(), 2: set(), 3: set()}

    # BFS
    visited_ids = set(seed_ids)
    frontier = set(seed_ids)
    files_by_depth = {}

    # Build confidence filter clause
    conf_clause = ""
    if min_confidence > 0:
        try:
            conn.execute("SELECT confidence FROM edges LIMIT 1")
            conf_clause = f" AND confidence >= {min_confidence}"
        except sqlite3.OperationalError:
            pass

    for depth in range(1, max_depth + 1):
        if not frontier:
            files_by_depth[depth] = set()
            continue

        placeholders = ",".join("?" for _ in frontier)
        # Get targets from frontier (both directions)
        query = f"""
            SELECT DISTINCT n.file_path
            FROM edges e
            JOIN nodes n ON n.id = e.target_id
            WHERE e.source_id IN ({placeholders}){conf_clause}
            AND e.target_id NOT IN ({','.join('?' for _ in visited_ids)})
        """
        params = list(frontier) + list(visited_ids)

        new_files = set()
        new_ids = set()
        try:
            for row in conn.execute(query, params):
                new_files.add(row[0])
        except sqlite3.OperationalError:
            pass

        # Also get the node IDs for next frontier
        query2 = f"""
            SELECT DISTINCT e.target_id
            FROM edges e
            WHERE e.source_id IN ({','.join('?' for _ in frontier)}){conf_clause}
            AND e.target_id NOT IN ({','.join('?' for _ in visited_ids)})
        """
        try:
            for row in conn.execute(query2, list(frontier) + list(visited_ids)):
                new_ids.add(row[0])
        except sqlite3.OperationalError:
            pass

        files_by_depth[depth] = new_files
        visited_ids.update(new_ids)
        frontier = new_ids

    conn.close()
    return files_by_depth


def measure_graph_noise():
    bugs = []
    with open(HOLDOUT_PATH) as f:
        for line in f:
            bugs.append(json.loads(line))

    print("=" * 70)
    print("G3 VALIDATION: Graph BFS Noise in Big vs Small Repos")
    print("=" * 70)
    print(f"\nDataset: holdout_v1.jsonl ({len(bugs)} bugs, 5 repos)")
    print()

    # Phase 1: Edge statistics per repo
    print("PHASE 1: Edge Type Distribution")
    print("-" * 70)

    repo_edge_stats = {}
    seen_dbs = set()
    for bug in bugs:
        db_path = bug.get("graph_db_path", "")
        repo = bug["repo"]
        if not os.path.exists(db_path) or db_path in seen_dbs:
            continue
        seen_dbs.add(db_path)
        if repo not in repo_edge_stats:
            stats = get_edge_stats(db_path)
            stats["total_nodes"] = bug.get("gt_index_stats", {}).get("nodes", 0)
            stats["total_files"] = bug.get("total_source_files", 0)
            repo_edge_stats[repo] = stats

    print(f"{'Repo':<25} {'Files':>6} {'Nodes':>7} {'Edges':>7} {'NM%':>5} {'HiConf%':>8} {'AvgConf':>8}")
    for repo in sorted(repo_edge_stats):
        s = repo_edge_stats[repo]
        nm_pct = s["name_match_ratio"] * 100
        hi_pct = s["high_conf_ratio"] * 100
        avg_c = f"{s['avg_confidence']:.2f}" if s['avg_confidence'] else "N/A"
        print(f"{repo:<25} {s['total_files']:>6} {s['total_nodes']:>7} {s['total_edges']:>7} {nm_pct:>4.0f}% {hi_pct:>7.0f}% {avg_c:>8}")

    # Phase 2: BFS expansion from anchors
    print(f"\n{'=' * 70}")
    print("PHASE 2: BFS Candidate Count by Depth (from issue anchors)")
    print("-" * 70)

    results_by_repo = defaultdict(list)

    for bug in bugs:
        db_path = bug.get("graph_db_path", "")
        if not os.path.exists(db_path):
            continue

        repo = bug["repo"]
        issue_text = (bug.get("issue_title", "") or "") + "\n" + (bug.get("issue_body", "") or "")
        gold_files = set(bug.get("gold_files", []))

        # Extract anchors
        anchors = extract_issue_anchors(issue_text, db_path)
        anchor_names = anchors.symbols

        if not anchor_names:
            results_by_repo[repo].append({
                "bug_id": bug["bug_id"],
                "anchors": 0,
                "hop1": 0, "hop2": 0, "hop3": 0,
                "hop1_conf07": 0, "hop2_conf07": 0, "hop3_conf07": 0,
                "gold_reachable_hop1": False,
                "gold_reachable_hop3": False,
                "gold_reachable_hop3_conf07": False,
            })
            continue

        # BFS without confidence filter
        files_no_filter = bfs_from_anchors(db_path, anchor_names, max_depth=3, min_confidence=0.0)

        # BFS with confidence >= 0.7
        files_conf07 = bfs_from_anchors(db_path, anchor_names, max_depth=3, min_confidence=0.7)

        # Check gold reachability
        all_files_hop3 = files_no_filter.get(1, set()) | files_no_filter.get(2, set()) | files_no_filter.get(3, set())
        all_files_hop3_conf07 = files_conf07.get(1, set()) | files_conf07.get(2, set()) | files_conf07.get(3, set())

        gold_reach_hop1 = bool(gold_files & files_no_filter.get(1, set()))
        gold_reach_hop3 = bool(gold_files & all_files_hop3)
        gold_reach_hop3_conf07 = bool(gold_files & all_files_hop3_conf07)

        results_by_repo[repo].append({
            "bug_id": bug["bug_id"],
            "anchors": len(anchor_names),
            "hop1": len(files_no_filter.get(1, set())),
            "hop2": len(files_no_filter.get(2, set())),
            "hop3": len(files_no_filter.get(3, set())),
            "hop1_conf07": len(files_conf07.get(1, set())),
            "hop2_conf07": len(files_conf07.get(2, set())),
            "hop3_conf07": len(files_conf07.get(3, set())),
            "gold_reachable_hop1": gold_reach_hop1,
            "gold_reachable_hop3": gold_reach_hop3,
            "gold_reachable_hop3_conf07": gold_reach_hop3_conf07,
        })

    # Print results per repo
    print(f"\n{'Repo':<25} {'N':>3} {'Hop1':>6} {'Hop2':>6} {'Hop3':>6} | {'H1@.7':>6} {'H2@.7':>6} {'H3@.7':>6} | {'GoldR1':>6} {'GoldR3':>6}")
    print("-" * 95)

    for repo in sorted(results_by_repo):
        res = results_by_repo[repo]
        n = len(res)
        avg_h1 = sum(r["hop1"] for r in res) / max(n, 1)
        avg_h2 = sum(r["hop2"] for r in res) / max(n, 1)
        avg_h3 = sum(r["hop3"] for r in res) / max(n, 1)
        avg_h1c = sum(r["hop1_conf07"] for r in res) / max(n, 1)
        avg_h2c = sum(r["hop2_conf07"] for r in res) / max(n, 1)
        avg_h3c = sum(r["hop3_conf07"] for r in res) / max(n, 1)
        gold_r1 = sum(1 for r in res if r["gold_reachable_hop1"]) / max(n, 1)
        gold_r3 = sum(1 for r in res if r["gold_reachable_hop3"]) / max(n, 1)
        print(f"{repo:<25} {n:>3} {avg_h1:>6.1f} {avg_h2:>6.1f} {avg_h3:>6.1f} | {avg_h1c:>6.1f} {avg_h2c:>6.1f} {avg_h3c:>6.1f} | {gold_r1:>5.0%} {gold_r3:>5.0%}")

    # Big vs small comparison
    print(f"\n{'=' * 70}")
    print("BIG vs SMALL REPO COMPARISON")
    print("=" * 70)

    for label, repo_set in [("BIG (dagster+marimo)", BIG_REPOS), ("SMALL (axum+hono+crossplane)", SMALL_REPOS)]:
        subset = []
        for repo in repo_set:
            subset.extend(results_by_repo.get(repo, []))
        n = len(subset)
        if n == 0:
            continue

        avg_h1 = sum(r["hop1"] for r in subset) / n
        avg_h2 = sum(r["hop2"] for r in subset) / n
        avg_h3 = sum(r["hop3"] for r in subset) / n
        avg_h1c = sum(r["hop1_conf07"] for r in subset) / n
        avg_h2c = sum(r["hop2_conf07"] for r in subset) / n
        avg_h3c = sum(r["hop3_conf07"] for r in subset) / n
        gold_r1 = sum(1 for r in subset if r["gold_reachable_hop1"]) / n
        gold_r3 = sum(1 for r in subset if r["gold_reachable_hop3"]) / n
        gold_r3c = sum(1 for r in subset if r["gold_reachable_hop3_conf07"]) / n
        zero_anchors = sum(1 for r in subset if r["anchors"] == 0) / n

        print(f"\n{label} (n={n}):")
        print(f"  BFS candidates (no filter):    hop1={avg_h1:.1f}, hop2={avg_h2:.1f}, hop3={avg_h3:.1f}")
        print(f"  BFS candidates (conf>=0.7):    hop1={avg_h1c:.1f}, hop2={avg_h2c:.1f}, hop3={avg_h3c:.1f}")
        print(f"  Noise reduction from conf>=0.7: hop3 {avg_h3:.0f} -> {avg_h3c:.0f} ({(1-avg_h3c/max(avg_h3,1))*100:.0f}% reduction)")
        print(f"  Gold reachable at hop1:        {gold_r1:.0%}")
        print(f"  Gold reachable at hop3:        {gold_r3:.0%}")
        print(f"  Gold reachable at hop3@0.7:    {gold_r3c:.0%}")
        print(f"  Zero-anchor bugs:              {zero_anchors:.0%}")

    # Validation verdict
    print(f"\n{'=' * 70}")
    print("VALIDATION VERDICT")
    print("=" * 70)

    big_subset = []
    small_subset = []
    for repo in BIG_REPOS:
        big_subset.extend(results_by_repo.get(repo, []))
    for repo in SMALL_REPOS:
        small_subset.extend(results_by_repo.get(repo, []))

    big_h3 = sum(r["hop3"] for r in big_subset) / max(len(big_subset), 1)
    small_h3 = sum(r["hop3"] for r in small_subset) / max(len(small_subset), 1)
    ratio = big_h3 / max(small_h3, 1)

    big_gold = sum(1 for r in big_subset if r["gold_reachable_hop3"]) / max(len(big_subset), 1)
    small_gold = sum(1 for r in small_subset if r["gold_reachable_hop3"]) / max(len(small_subset), 1)

    print(f"  Big-repo hop3 candidates:   {big_h3:.1f}")
    print(f"  Small-repo hop3 candidates: {small_h3:.1f}")
    print(f"  Ratio (big/small):          {ratio:.1f}x")
    print(f"  Big-repo gold reachable:    {big_gold:.0%}")
    print(f"  Small-repo gold reachable:  {small_gold:.0%}")

    if ratio > 10 and big_gold <= small_gold:
        print("\n  G3 VALIDATED: BFS candidates >10x in big repos, gold reachable same/lower")
    elif ratio > 5:
        print(f"\n  G3 PARTIALLY VALIDATED: {ratio:.0f}x expansion in big repos (threshold was 10x)")
    else:
        print(f"\n  G3 WEAKENED/FALSIFIED: only {ratio:.1f}x expansion (not dramatic)")


if __name__ == "__main__":
    measure_graph_noise()
