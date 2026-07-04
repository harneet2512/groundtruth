"""G7 Validation: How often does GT have nothing useful to say?

For each bug in holdout_v1, queries graph.db for the gold function's
callers, siblings, tests, and imports. Classifies evidence potential as
RICH, MODERATE, or POOR. A POOR classification means GT would inject
noise/placeholder if it tried to provide evidence.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"

BIG_REPOS = {"dagster-io/dagster", "marimo-team/marimo"}
SMALL_REPOS = {"tokio-rs/axum", "honojs/hono", "crossplane/crossplane"}


def get_gold_function_evidence(db_path, gold_file):
    """Query graph.db for what GT could say about a gold file's functions."""
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    try:
        # Find nodes in the gold file
        nodes = conn.execute(
            "SELECT id, name, label, signature FROM nodes WHERE file_path = ?",
            (gold_file,)
        ).fetchall()

        if not nodes:
            # Try with various path normalizations
            for row in conn.execute("SELECT DISTINCT file_path FROM nodes LIMIT 5"):
                pass  # just checking schema
            # Try basename match
            basename = Path(gold_file).name
            nodes = conn.execute(
                "SELECT id, name, label, signature FROM nodes WHERE file_path LIKE ?",
                (f"%{basename}",)
            ).fetchall()

        if not nodes:
            return {"status": "not_in_graph", "callers": 0, "callees": 0,
                    "siblings": 0, "tests": 0, "functions": 0}

        node_ids = [n[0] for n in nodes]
        func_count = len(nodes)

        # Count callers (incoming edges to these nodes)
        placeholders = ",".join("?" for _ in node_ids)

        # Callers at any confidence
        callers_all = conn.execute(
            f"SELECT COUNT(DISTINCT source_id) FROM edges WHERE target_id IN ({placeholders})",
            node_ids
        ).fetchone()[0]

        # Callers at high confidence
        try:
            callers_hi = conn.execute(
                f"SELECT COUNT(DISTINCT source_id) FROM edges WHERE target_id IN ({placeholders}) AND confidence >= 0.7",
                node_ids
            ).fetchone()[0]
        except sqlite3.OperationalError:
            callers_hi = callers_all  # no confidence column

        # Callees (outgoing edges)
        callees = conn.execute(
            f"SELECT COUNT(DISTINCT target_id) FROM edges WHERE source_id IN ({placeholders})",
            node_ids
        ).fetchone()[0]

        # Siblings (other functions in same file)
        siblings = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE file_path = ? AND id NOT IN ({})".format(placeholders),
            (gold_file, *node_ids)
        ).fetchone()[0]

        # Test functions that reference these nodes
        # Look for edges from test files to these nodes
        test_callers = conn.execute(
            f"""SELECT COUNT(DISTINCT n.id) FROM edges e
                JOIN nodes n ON n.id = e.source_id
                WHERE e.target_id IN ({placeholders})
                AND n.file_path LIKE '%test%'""",
            node_ids
        ).fetchone()[0]

        return {
            "status": "in_graph",
            "functions": func_count,
            "callers_all": callers_all,
            "callers_hi_conf": callers_hi,
            "callees": callees,
            "siblings": siblings,
            "tests": test_callers,
        }
    except sqlite3.Error as e:
        return {"status": "error", "error": str(e), "callers": 0, "callees": 0,
                "siblings": 0, "tests": 0, "functions": 0}
    finally:
        conn.close()


def classify_potential(evidence):
    """Classify evidence potential: RICH, MODERATE, POOR."""
    if evidence is None or evidence.get("status") != "in_graph":
        return "ABSENT"

    callers = evidence.get("callers_hi_conf", evidence.get("callers_all", 0))
    tests = evidence.get("tests", 0)
    siblings = evidence.get("siblings", 0)

    if callers >= 3 and (tests > 0 or siblings >= 2):
        return "RICH"
    elif callers >= 1 or tests > 0:
        return "MODERATE"
    else:
        return "POOR"


def measure_evidence_potential():
    bugs = []
    with open(HOLDOUT_PATH) as f:
        for line in f:
            bugs.append(json.loads(line))

    print("=" * 70)
    print("G7 VALIDATION: Evidence Potential / Silence Opportunity")
    print("=" * 70)
    print(f"\nDataset: holdout_v1.jsonl ({len(bugs)} bugs, 5 repos)")
    print()

    results = []
    for bug in bugs:
        db_path = bug.get("graph_db_path", "")
        gold_files = bug.get("gold_files", [])
        repo = bug["repo"]

        # For each gold file, check what GT could say
        gold_potentials = []
        for gf in gold_files:
            ev = get_gold_function_evidence(db_path, gf)
            classification = classify_potential(ev)
            gold_potentials.append({
                "file": gf,
                "evidence": ev,
                "classification": classification,
            })

        # Overall classification: worst potential across gold files
        # (if GT has nothing for ANY gold file the agent edits, it fails on that edit)
        classifications = [gp["classification"] for gp in gold_potentials]
        if "POOR" in classifications or "ABSENT" in classifications:
            overall = "POOR"
        elif "MODERATE" in classifications:
            overall = "MODERATE"
        else:
            overall = "RICH"

        results.append({
            "bug_id": bug["bug_id"],
            "repo": repo,
            "gold_count": len(gold_files),
            "potentials": gold_potentials,
            "overall": overall,
            "max_callers": max((gp["evidence"].get("callers_hi_conf", 0)
                              for gp in gold_potentials
                              if gp["evidence"] and gp["evidence"].get("status") == "in_graph"),
                             default=0),
            "has_tests": any(gp["evidence"] and gp["evidence"].get("tests", 0) > 0
                           for gp in gold_potentials),
        })

    # Aggregate
    print("EVIDENCE POTENTIAL DISTRIBUTION")
    print("-" * 70)

    from collections import Counter
    overall_counts = Counter(r["overall"] for r in results)
    total = len(results)

    for cls in ["RICH", "MODERATE", "POOR"]:
        n = overall_counts[cls]
        print(f"  {cls:<10}: {n:>3}/{total} ({n/total:.0%})")

    # Per repo
    print(f"\n{'Repo':<25} {'RICH':>6} {'MOD':>6} {'POOR':>6} {'HasTest':>8} {'AvgCall':>8}")
    print("-" * 70)

    for repo in sorted(set(r["repo"] for r in results)):
        subset = [r for r in results if r["repo"] == repo]
        n = len(subset)
        rich = sum(1 for r in subset if r["overall"] == "RICH")
        mod = sum(1 for r in subset if r["overall"] == "MODERATE")
        poor = sum(1 for r in subset if r["overall"] == "POOR")
        has_test = sum(1 for r in subset if r["has_tests"])
        avg_callers = sum(r["max_callers"] for r in subset) / n
        print(f"{repo:<25} {rich:>5} {mod:>6} {poor:>6} {has_test:>7} {avg_callers:>8.1f}")

    # Big vs small
    print(f"\n{'=' * 70}")
    print("BIG vs SMALL REPO COMPARISON")
    print("=" * 70)

    for label, repo_set in [("BIG (dagster+marimo)", BIG_REPOS), ("SMALL (axum+hono+crossplane)", SMALL_REPOS)]:
        subset = [r for r in results if r["repo"] in repo_set]
        n = len(subset)
        rich = sum(1 for r in subset if r["overall"] == "RICH") / n
        mod = sum(1 for r in subset if r["overall"] == "MODERATE") / n
        poor = sum(1 for r in subset if r["overall"] == "POOR") / n
        has_test = sum(1 for r in subset if r["has_tests"]) / n
        avg_callers = sum(r["max_callers"] for r in subset) / n

        print(f"\n{label} (n={n}):")
        print(f"  RICH:      {rich:.0%}")
        print(f"  MODERATE:  {mod:.0%}")
        print(f"  POOR:      {poor:.0%}")
        print(f"  Has test callers: {has_test:.0%}")
        print(f"  Avg max callers (hi-conf): {avg_callers:.1f}")

    # Silence opportunity
    silence_rate = overall_counts.get("POOR", 0) / total
    print(f"\n{'=' * 70}")
    print("SILENCE OPPORTUNITY ANALYSIS")
    print("=" * 70)
    print(f"  Bugs where GT has POOR evidence potential: {overall_counts.get('POOR', 0)}/{total} ({silence_rate:.0%})")
    print(f"  These are cases where GT should stay silent rather than inject noise.")
    print()

    # Detail the POOR cases
    poor_cases = [r for r in results if r["overall"] == "POOR"]
    print(f"  POOR cases detail ({len(poor_cases)} bugs):")
    for r in poor_cases[:10]:
        print(f"    {r['bug_id']} ({r['repo']}): max_callers={r['max_callers']}, has_tests={r['has_tests']}")
        for gp in r["potentials"]:
            if gp["classification"] in ("POOR", "ABSENT"):
                ev = gp["evidence"]
                status = ev.get("status", "?") if ev else "None"
                print(f"      {gp['file']}: {gp['classification']} (status={status})")

    # Validation verdict
    print(f"\n{'=' * 70}")
    print("VALIDATION VERDICT")
    print("=" * 70)

    if silence_rate > 0.25:
        print(f"  G7 VALIDATED: {silence_rate:.0%} of bugs have POOR evidence potential (>25% threshold)")
        print(f"  GT should suppress injection for these cases.")
    elif silence_rate < 0.10:
        print(f"  G7 FALSIFIED: only {silence_rate:.0%} have POOR potential (<10%)")
        print(f"  GT almost always has something useful from graph.")
    else:
        print(f"  G7 INCONCLUSIVE: {silence_rate:.0%} (between 10-25%)")


if __name__ == "__main__":
    measure_evidence_potential()
