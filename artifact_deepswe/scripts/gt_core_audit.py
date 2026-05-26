#!/usr/bin/env python3
"""Deep GT core readiness audit for multi-language multi-repo capability.

Tests whether GT's engine handles the 5 capabilities that matter for
arbitrary repos:

1. BOUNDARY DETECTION: Can GT find "there's more here" — sibling methods,
   co-change files, second exception types, other env vars?
2. NOISE SUPPRESSION: Does GT shut up when uncertain? What % of edges
   are conf<0.5 noise that would mislead the agent?
3. RESOLUTION GRANULARITY: Does GT resolve at class.method level (how
   developers think) or bare name level (how grep thinks)?
4. TEST RELAY: Can GT find and relay test assertions — the machine-
   readable spec of correct behavior?
5. CROSS-LANGUAGE PARITY: Do all 5 languages get equal treatment, or
   are some second-class citizens?

Each capability is tested against real graph.db files from holdout repos.
"""
import sqlite3, sys, os, json
from pathlib import Path
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOLDOUT = Path(r"D:\Groundtruth\.tmp_holdout\bugs")
GT_SELF = Path(r"D:\Groundtruth\gt-index\graph.db")

DBS = {
    "hono (TS)":       HOLDOUT / "hono-4876" / "graph.db",
    "crossplane (Go)": HOLDOUT / "crossplane-7332" / "graph.db",
    "marimo (Py)":     HOLDOUT / "marimo-9408" / "graph.db",
    "axum (Rust)":     HOLDOUT / "axum-3722" / "graph.db",
    "gt-self (Go)":    GT_SELF,
}

def audit_db(label, db_path):
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    result = {"label": label}

    total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    if total_edges == 0:
        conn.close()
        return result

    # ── CAPABILITY 1: BOUNDARY DETECTION ──
    # Can GT detect siblings? Requires parent_id linkage.
    nodes_with_parent = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE parent_id IS NOT NULL AND parent_id > 0"
    ).fetchone()[0]
    classes = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE label IN ('Class', 'Struct', 'Interface')"
    ).fetchone()[0]
    methods = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE label = 'Method'"
    ).fetchone()[0]
    functions = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE label = 'Function'"
    ).fetchone()[0]

    # Classes with 2+ methods (sibling detection possible)
    classes_with_siblings = conn.execute("""
        SELECT COUNT(DISTINCT parent_id) FROM (
            SELECT parent_id, COUNT(*) as cnt
            FROM nodes WHERE parent_id > 0
            GROUP BY parent_id HAVING cnt >= 2
        )
    """).fetchone()[0]

    # Average methods per class
    avg_methods = conn.execute("""
        SELECT ROUND(AVG(cnt), 1) FROM (
            SELECT parent_id, COUNT(*) as cnt
            FROM nodes WHERE parent_id > 0 AND label IN ('Method', 'Function')
            GROUP BY parent_id
        )
    """).fetchone()[0] or 0

    result["boundary"] = {
        "nodes_with_parent": nodes_with_parent,
        "classes": classes,
        "methods": methods,
        "functions": functions,
        "classes_with_siblings": classes_with_siblings,
        "avg_methods_per_class": avg_methods,
        "parent_coverage_pct": round(nodes_with_parent * 100 / total_nodes, 1) if total_nodes > 0 else 0,
        "verdict": "READY" if nodes_with_parent > total_nodes * 0.1 else "WEAK",
    }

    # ── CAPABILITY 2: NOISE SUPPRESSION ──
    # What % of edges are noise (conf < 0.5)?
    has_conf = any(c[1] == "confidence" for c in conn.execute("PRAGMA table_info(edges)"))
    if has_conf:
        noise = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence < 0.5").fetchone()[0]
        low_signal = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.5 AND confidence < 0.7").fetchone()[0]
        high_signal = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.7").fetchone()[0]

        # Top noise generators: names with most low-confidence edges
        noise_names = []
        for row in conn.execute("""
            SELECT n2.name, COUNT(*) as cnt
            FROM edges e JOIN nodes n2 ON e.target_id = n2.id
            WHERE e.confidence < 0.5
            GROUP BY n2.name ORDER BY cnt DESC LIMIT 5
        """):
            noise_names.append({"name": row[0], "noise_edges": row[1]})

        result["noise"] = {
            "noise_edges": noise,
            "noise_pct": round(noise * 100 / total_edges, 1),
            "low_signal": low_signal,
            "high_signal": high_signal,
            "high_signal_pct": round(high_signal * 100 / total_edges, 1),
            "top_noise_names": noise_names,
            "verdict": "GOOD" if noise < total_edges * 0.3 else "NOISY",
        }
    else:
        result["noise"] = {"verdict": "NO_CONFIDENCE_DATA"}

    # ── CAPABILITY 3: RESOLUTION GRANULARITY ──
    # Does GT resolve at class.method level or bare name level?
    # Check: how many edges connect to Method nodes (have parent_id)?
    method_targets = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n ON e.target_id = n.id
        WHERE n.label = 'Method' AND n.parent_id > 0
    """).fetchone()[0]

    function_targets = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n ON e.target_id = n.id
        WHERE n.label = 'Function'
    """).fetchone()[0]

    # Ambiguity: how many target names appear in 3+ different files?
    ambiguous_names = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT n.name, COUNT(DISTINCT n.file_path) as file_count
            FROM nodes n WHERE n.label IN ('Function', 'Method')
            GROUP BY n.name HAVING file_count >= 3
        )
    """).fetchone()[0]

    total_unique_names = conn.execute("""
        SELECT COUNT(DISTINCT name) FROM nodes WHERE label IN ('Function', 'Method')
    """).fetchone()[0]

    result["resolution"] = {
        "method_target_edges": method_targets,
        "function_target_edges": function_targets,
        "method_target_pct": round(method_targets * 100 / total_edges, 1) if total_edges > 0 else 0,
        "ambiguous_names": ambiguous_names,
        "total_unique_names": total_unique_names,
        "ambiguity_pct": round(ambiguous_names * 100 / total_unique_names, 1) if total_unique_names > 0 else 0,
        "verdict": "GOOD" if ambiguous_names < total_unique_names * 0.15 else "AMBIGUOUS",
    }

    # ── CAPABILITY 4: TEST RELAY ──
    # Can GT find tests and link them to production code?
    test_nodes = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE is_test = 1"
    ).fetchone()[0]

    test_files = conn.execute(
        "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE is_test = 1"
    ).fetchone()[0]

    # Edges from test nodes to production nodes
    test_to_prod = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.is_test = 1 AND n2.is_test = 0
    """).fetchone()[0]

    # Assertions table check
    has_assertions = False
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='assertions'"):
        has_assertions = True
    assertion_count = 0
    assertions_linked = 0
    if has_assertions:
        assertion_count = conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0]
        try:
            assertions_linked = conn.execute(
                "SELECT COUNT(*) FROM assertions WHERE target_node_id > 0"
            ).fetchone()[0]
        except Exception:
            pass

    result["test_relay"] = {
        "test_nodes": test_nodes,
        "test_files": test_files,
        "test_to_prod_edges": test_to_prod,
        "test_coverage_pct": round(test_to_prod * 100 / total_edges, 1) if total_edges > 0 else 0,
        "has_assertions_table": has_assertions,
        "assertion_count": assertion_count,
        "assertions_linked": assertions_linked,
        "verdict": "READY" if test_to_prod > 0 else "BLIND",
    }

    # ── CAPABILITY 5: CROSS-LANGUAGE PARITY ──
    langs = {}
    for row in conn.execute("SELECT language, COUNT(*) FROM nodes GROUP BY language"):
        langs[row[0]] = row[1]

    edge_by_method = {}
    for row in conn.execute(
        "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
    ):
        edge_by_method[row[0] or "NULL"] = row[1]

    same_file = edge_by_method.get("same_file", 0)
    import_v = edge_by_method.get("import", 0)
    det_pct = round((same_file + import_v) * 100 / total_edges, 1) if total_edges > 0 else 0

    result["parity"] = {
        "languages": langs,
        "edge_methods": edge_by_method,
        "deterministic_pct": det_pct,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
    }

    conn.close()
    return result


def main():
    print("=" * 75)
    print("  GT CORE READINESS AUDIT — Multi-Language Multi-Repo")
    print("=" * 75)

    all_results = {}
    for label, db_path in DBS.items():
        r = audit_db(label, db_path)
        if r is None:
            continue
        all_results[label] = r

        print(f"\n{'='*65}")
        print(f"  {label}")
        print(f"{'='*65}")

        # Boundary
        b = r.get("boundary", {})
        print(f"\n  1. BOUNDARY DETECTION: {b.get('verdict', '?')}")
        print(f"     Classes/Structs: {b.get('classes', 0)}, Methods: {b.get('methods', 0)}, Functions: {b.get('functions', 0)}")
        print(f"     Nodes with parent_id: {b.get('nodes_with_parent', 0)} ({b.get('parent_coverage_pct', 0)}%)")
        print(f"     Classes with 2+ children (sibling detection): {b.get('classes_with_siblings', 0)}")
        print(f"     Avg methods per class: {b.get('avg_methods_per_class', 0)}")

        # Noise
        n = r.get("noise", {})
        print(f"\n  2. NOISE SUPPRESSION: {n.get('verdict', '?')}")
        if n.get("verdict") != "NO_CONFIDENCE_DATA":
            print(f"     High signal (>=0.7): {n.get('high_signal', 0)} ({n.get('high_signal_pct', 0)}%)")
            print(f"     Noise (<0.5): {n.get('noise_edges', 0)} ({n.get('noise_pct', 0)}%)")
            for nn in n.get("top_noise_names", []):
                print(f"       -> {nn['name']}: {nn['noise_edges']} noise edges")

        # Resolution
        res = r.get("resolution", {})
        print(f"\n  3. RESOLUTION GRANULARITY: {res.get('verdict', '?')}")
        print(f"     Method-targeted edges: {res.get('method_target_edges', 0)} ({res.get('method_target_pct', 0)}%)")
        print(f"     Ambiguous names (in 3+ files): {res.get('ambiguous_names', 0)} / {res.get('total_unique_names', 0)} ({res.get('ambiguity_pct', 0)}%)")

        # Test relay
        t = r.get("test_relay", {})
        print(f"\n  4. TEST RELAY: {t.get('verdict', '?')}")
        print(f"     Test nodes: {t.get('test_nodes', 0)} in {t.get('test_files', 0)} files")
        print(f"     Test->prod edges: {t.get('test_to_prod_edges', 0)}")
        print(f"     Assertions: {t.get('assertion_count', 0)} (linked: {t.get('assertions_linked', 0)})")

        # Parity
        p = r.get("parity", {})
        print(f"\n  5. EDGE QUALITY: {p.get('deterministic_pct', 0)}% deterministic")
        print(f"     {p.get('edge_methods', {})}")

    # Cross-language comparison
    print(f"\n\n{'='*75}")
    print("  CROSS-LANGUAGE COMPARISON")
    print(f"{'='*75}")
    print(f"\n  {'Language':<20s} {'Det%':>6s} {'Boundary':>10s} {'Noise':>8s} {'Resolution':>12s} {'Tests':>8s}")
    print(f"  {'-'*64}")
    for label, r in all_results.items():
        det = r.get("parity", {}).get("deterministic_pct", 0)
        bnd = r.get("boundary", {}).get("verdict", "?")
        noi = r.get("noise", {}).get("verdict", "?")
        res = r.get("resolution", {}).get("verdict", "?")
        tst = r.get("test_relay", {}).get("verdict", "?")
        print(f"  {label:<20s} {det:>5.1f}% {bnd:>10s} {noi:>8s} {res:>12s} {tst:>8s}")

    # Overall verdict
    print(f"\n\n{'='*75}")
    print("  OVERALL GT READINESS VERDICT")
    print(f"{'='*75}")

    issues = []
    for label, r in all_results.items():
        if r.get("boundary", {}).get("verdict") == "WEAK":
            issues.append(f"{label}: parent_id coverage too low for sibling detection")
        if r.get("noise", {}).get("verdict") == "NOISY":
            issues.append(f"{label}: >{r['noise']['noise_pct']}% noise edges")
        if r.get("resolution", {}).get("verdict") == "AMBIGUOUS":
            issues.append(f"{label}: {r['resolution']['ambiguity_pct']}% ambiguous names")
        if r.get("test_relay", {}).get("verdict") == "BLIND":
            issues.append(f"{label}: zero test->prod edges")

    if not issues:
        print("\n  VERDICT: READY")
    else:
        print(f"\n  VERDICT: {len(issues)} ISSUES")
        for issue in issues:
            print(f"    - {issue}")

    # Write JSON
    out = Path("D:/Groundtruth/artifact_deepswe/gt_core_audit.json")
    with open(out, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n  Detailed: {out}")


if __name__ == "__main__":
    main()
