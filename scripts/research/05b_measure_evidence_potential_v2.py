"""G7 Validation v2: Evidence potential WITHOUT relying on tests.

Corrects v1 by including contract evidence (signature, return_type) as
always-available signal. A function with typed signature has contract evidence
even with 0 callers and 0 tests.

Measures what GT can ALWAYS provide vs what's conditional on tests.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"

BIG_REPOS = {"dagster-io/dagster", "marimo-team/marimo"}
SMALL_REPOS = {"tokio-rs/axum", "honojs/hono", "crossplane/crossplane"}


def get_gold_function_evidence(db_path, gold_file):
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    try:
        nodes = conn.execute(
            "SELECT id, name, label, signature, return_type FROM nodes WHERE file_path = ?",
            (gold_file,)
        ).fetchall()

        if not nodes:
            basename = Path(gold_file).name
            nodes = conn.execute(
                "SELECT id, name, label, signature, return_type FROM nodes WHERE file_path LIKE ?",
                (f"%{basename}",)
            ).fetchall()

        if not nodes:
            return {"status": "not_in_graph", "functions": 0,
                    "callers_all": 0, "callers_hi_conf": 0, "callees": 0,
                    "siblings": 0, "tests": 0,
                    "has_signature": False, "has_return_type": False,
                    "signature_count": 0, "return_type_count": 0}

        node_ids = [n[0] for n in nodes]
        func_count = len(nodes)
        placeholders = ",".join("?" for _ in node_ids)

        # Contract evidence: how many functions have typed signatures?
        sig_count = sum(1 for n in nodes if n[3] and n[3].strip() and n[3].strip() != "()")
        ret_count = sum(1 for n in nodes if n[4] and n[4].strip())

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
            callers_hi = callers_all

        # Callees
        callees = conn.execute(
            f"SELECT COUNT(DISTINCT target_id) FROM edges WHERE source_id IN ({placeholders})",
            node_ids
        ).fetchone()[0]

        # Siblings
        siblings = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE file_path = ? AND id NOT IN ({})".format(placeholders),
            (gold_file, *node_ids)
        ).fetchone()[0]

        # Test callers (bonus only)
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
            "has_signature": sig_count > 0,
            "has_return_type": ret_count > 0,
            "signature_count": sig_count,
            "return_type_count": ret_count,
        }
    except sqlite3.Error as e:
        return {"status": "error", "error": str(e), "functions": 0,
                "callers_all": 0, "callers_hi_conf": 0, "callees": 0,
                "siblings": 0, "tests": 0,
                "has_signature": False, "has_return_type": False,
                "signature_count": 0, "return_type_count": 0}
    finally:
        conn.close()


def classify_potential_v1(ev):
    """Original G7 classification (test-dependent)."""
    if ev is None or ev.get("status") != "in_graph":
        return "ABSENT"
    callers = ev.get("callers_hi_conf", ev.get("callers_all", 0))
    tests = ev.get("tests", 0)
    siblings = ev.get("siblings", 0)
    if callers >= 3 and (tests > 0 or siblings >= 2):
        return "RICH"
    elif callers >= 1 or tests > 0:
        return "MODERATE"
    else:
        return "POOR"


def classify_potential_v2(ev):
    """Corrected classification: callers + contracts + siblings (no test dependency)."""
    if ev is None or ev.get("status") != "in_graph":
        return "ABSENT"

    callers = ev.get("callers_hi_conf", ev.get("callers_all", 0))
    siblings = ev.get("siblings", 0)
    has_sig = ev.get("has_signature", False)
    has_ret = ev.get("has_return_type", False)
    has_contract = has_sig or has_ret

    # RICH: multiple callers + structural context (siblings or contract)
    if callers >= 3 and (siblings >= 2 or has_contract):
        return "RICH"
    # MODERATE: has any structural evidence
    elif callers >= 1 or (has_contract and siblings >= 1):
        return "MODERATE"
    # POOR: truly isolated, no callers, no contract, no sibling context
    else:
        return "POOR"


def measure():
    bugs = []
    with open(HOLDOUT_PATH) as f:
        for line in f:
            bugs.append(json.loads(line))

    print("=" * 70)
    print("G7 v2: Evidence Potential (non-test-dependent)")
    print("=" * 70)
    print(f"Dataset: holdout_v1.jsonl ({len(bugs)} bugs, 5 repos)")
    print()

    results = []
    for bug in bugs:
        db_path = bug.get("graph_db_path", "")
        gold_files = bug.get("gold_files", [])
        repo = bug["repo"]

        gold_potentials = []
        for gf in gold_files:
            ev = get_gold_function_evidence(db_path, gf)
            v1_cls = classify_potential_v1(ev)
            v2_cls = classify_potential_v2(ev)
            gold_potentials.append({
                "file": gf,
                "evidence": ev,
                "v1": v1_cls,
                "v2": v2_cls,
            })

        # Overall: worst across gold files
        v1_classes = [gp["v1"] for gp in gold_potentials]
        v2_classes = [gp["v2"] for gp in gold_potentials]

        def worst(classes):
            if "POOR" in classes or "ABSENT" in classes:
                return "POOR"
            elif "MODERATE" in classes:
                return "MODERATE"
            return "RICH"

        results.append({
            "bug_id": bug["bug_id"],
            "repo": repo,
            "gold_count": len(gold_files),
            "potentials": gold_potentials,
            "v1_overall": worst(v1_classes),
            "v2_overall": worst(v2_classes),
            "max_callers": max((gp["evidence"].get("callers_hi_conf", 0)
                              for gp in gold_potentials
                              if gp["evidence"] and gp["evidence"].get("status") == "in_graph"),
                             default=0),
            "has_contract": any(gp["evidence"] and
                               (gp["evidence"].get("has_signature", False) or
                                gp["evidence"].get("has_return_type", False))
                              for gp in gold_potentials),
            "has_tests": any(gp["evidence"] and gp["evidence"].get("tests", 0) > 0
                           for gp in gold_potentials),
        })

    # Side-by-side comparison
    print("CLASSIFICATION COMPARISON: v1 (test-dependent) vs v2 (non-test)")
    print("-" * 70)
    total = len(results)

    v1_counts = Counter(r["v1_overall"] for r in results)
    v2_counts = Counter(r["v2_overall"] for r in results)

    print(f"{'Class':<10} {'v1 (old)':>12} {'v2 (new)':>12} {'Delta':>10}")
    for cls in ["RICH", "MODERATE", "POOR"]:
        v1n = v1_counts[cls]
        v2n = v2_counts[cls]
        delta = v2n - v1n
        sign = "+" if delta > 0 else ""
        print(f"  {cls:<10} {v1n:>3}/{total} ({v1n/total:.0%})  {v2n:>3}/{total} ({v2n/total:.0%})  {sign}{delta:>3}")

    # What moved from POOR to MODERATE/RICH?
    upgraded = [r for r in results if r["v1_overall"] == "POOR" and r["v2_overall"] != "POOR"]
    still_poor = [r for r in results if r["v2_overall"] == "POOR"]

    print(f"\nBugs upgraded from POOR (v1) to MODERATE/RICH (v2): {len(upgraded)}")
    for r in upgraded:
        reason = []
        if r["has_contract"]:
            reason.append("has_contract")
        print(f"  {r['bug_id']} ({r['repo']}): v1={r['v1_overall']} -> v2={r['v2_overall']} [{', '.join(reason)}]")

    print(f"\nBugs STILL POOR in v2: {len(still_poor)}/{total} ({len(still_poor)/total:.0%})")
    for r in still_poor[:10]:
        print(f"  {r['bug_id']} ({r['repo']}): callers={r['max_callers']}, contract={r['has_contract']}")
        for gp in r["potentials"]:
            if gp["v2"] in ("POOR", "ABSENT"):
                ev = gp["evidence"]
                if ev:
                    sig = ev.get("has_signature", False)
                    ret = ev.get("has_return_type", False)
                    sib = ev.get("siblings", 0)
                    print(f"    {gp['file']}: sig={sig}, ret={ret}, siblings={sib}, status={ev.get('status','?')}")

    # Per repo
    print(f"\n{'=' * 70}")
    print("PER-REPO COMPARISON")
    print("=" * 70)
    print(f"{'Repo':<25} {'v1 POOR':>8} {'v2 POOR':>8} {'Contract':>9} {'Callers>0':>10}")
    print("-" * 70)

    for repo in sorted(set(r["repo"] for r in results)):
        subset = [r for r in results if r["repo"] == repo]
        n = len(subset)
        v1_poor = sum(1 for r in subset if r["v1_overall"] == "POOR")
        v2_poor = sum(1 for r in subset if r["v2_overall"] == "POOR")
        has_contract = sum(1 for r in subset if r["has_contract"])
        has_callers = sum(1 for r in subset if r["max_callers"] > 0)
        print(f"{repo:<25} {v1_poor:>3}/{n} ({v1_poor/n:.0%})  {v2_poor:>3}/{n} ({v2_poor/n:.0%})  {has_contract:>4}/{n} ({has_contract/n:.0%}) {has_callers:>5}/{n} ({has_callers/n:.0%})")

    # Big vs small
    print(f"\n{'=' * 70}")
    print("BIG vs SMALL REPO (v2 classification)")
    print("=" * 70)

    for label, repo_set in [("BIG (dagster+marimo)", BIG_REPOS), ("SMALL (axum+hono+crossplane)", SMALL_REPOS)]:
        subset = [r for r in results if r["repo"] in repo_set]
        n = len(subset)
        if n == 0:
            continue
        rich = sum(1 for r in subset if r["v2_overall"] == "RICH") / n
        mod = sum(1 for r in subset if r["v2_overall"] == "MODERATE") / n
        poor = sum(1 for r in subset if r["v2_overall"] == "POOR") / n
        has_contract = sum(1 for r in subset if r["has_contract"]) / n
        has_callers = sum(1 for r in subset if r["max_callers"] > 0) / n

        print(f"\n{label} (n={n}):")
        print(f"  RICH:       {rich:.0%}")
        print(f"  MODERATE:   {mod:.0%}")
        print(f"  POOR:       {poor:.0%}")
        print(f"  Has contract: {has_contract:.0%}")
        print(f"  Has callers:  {has_callers:.0%}")

    # Evidence type availability (always-available vs test-dependent)
    print(f"\n{'=' * 70}")
    print("EVIDENCE AVAILABILITY BY TYPE (always-available vs bonus)")
    print("=" * 70)

    always_callers = sum(1 for r in results if r["max_callers"] > 0)
    always_contract = sum(1 for r in results if r["has_contract"])
    bonus_tests = sum(1 for r in results if r["has_tests"])

    print(f"  Callers (always-available):   {always_callers}/{total} ({always_callers/total:.0%})")
    print(f"  Contract (always-available):  {always_contract}/{total} ({always_contract/total:.0%})")
    print(f"  Tests (bonus):               {bonus_tests}/{total} ({bonus_tests/total:.0%})")
    print()
    print(f"  Bugs with ANY always-available evidence: {sum(1 for r in results if r['max_callers'] > 0 or r['has_contract'])}/{total}")
    print(f"  Bugs with ONLY test evidence (no callers, no contract): {sum(1 for r in results if r['max_callers'] == 0 and not r['has_contract'] and r['has_tests'])}/{total}")

    # Validation verdict
    v2_poor_rate = v2_counts.get("POOR", 0) / total
    v1_poor_rate = v1_counts.get("POOR", 0) / total

    print(f"\n{'=' * 70}")
    print("VALIDATION VERDICT")
    print("=" * 70)
    print(f"  v1 POOR rate (test-dependent):     {v1_poor_rate:.0%}")
    print(f"  v2 POOR rate (non-test):           {v2_poor_rate:.0%}")
    print(f"  Difference:                        {(v1_poor_rate - v2_poor_rate):.0%} fewer POOR")
    print()

    if v2_poor_rate > 0.25:
        print(f"  G7 STILL VALIDATED at {v2_poor_rate:.0%} even without test dependency.")
        print(f"  GT should still suppress for truly isolated functions.")
    elif v2_poor_rate > 0.10:
        print(f"  G7 WEAKENED: {v2_poor_rate:.0%} POOR (was {v1_poor_rate:.0%} with test dependency)")
        print(f"  Contract evidence rescues some cases. Silence threshold needs recalibration.")
    else:
        print(f"  G7 POSSIBLY FALSIFIED: only {v2_poor_rate:.0%} truly POOR when contracts counted")
        print(f"  Most functions have SOME non-test evidence. Silence less urgent than thought.")


if __name__ == "__main__":
    measure()
