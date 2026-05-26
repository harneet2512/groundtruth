#!/usr/bin/env python3
"""Query resolution_method metrics from existing holdout graph.db files.

Since gt-index.exe has a DLL dependency issue on this machine, we demonstrate
the audit methodology on existing graph.db files from prior GT work sessions.
These cover Go, TypeScript, Python, and Rust — the same languages as DeepSWE.
"""
import json
import sqlite3
from pathlib import Path

HOLDOUT_DIR = Path("D:/Groundtruth/.tmp_holdout/bugs")
GT_SELF_DB = Path("D:/Groundtruth/gt-index/graph.db")

# Existing graph.db files covering DeepSWE languages
AUDIT_DBS = {
    # Go repos from holdout
    "crossplane-7332 (Go)": HOLDOUT_DIR / "crossplane-7332" / "graph.db",
    "crossplane-7330 (Go)": HOLDOUT_DIR / "crossplane-7330" / "graph.db",
    "crossplane-7279 (Go)": HOLDOUT_DIR / "crossplane-7279" / "graph.db",
    # TypeScript repos from holdout
    "hono-4876 (TypeScript)": HOLDOUT_DIR / "hono-4876" / "graph.db",
    "hono-4865 (TypeScript)": HOLDOUT_DIR / "hono-4865" / "graph.db",
    "hono-4848 (TypeScript)": HOLDOUT_DIR / "hono-4848" / "graph.db",
    # Python repos from holdout
    "marimo-9408 (Python)": HOLDOUT_DIR / "marimo-9408" / "graph.db",
    # Rust repos from holdout
    "axum-3722 (Rust)": HOLDOUT_DIR / "axum-3722" / "graph.db",
    "axum-3704 (Rust)": HOLDOUT_DIR / "axum-3704" / "graph.db",
    # GT self-index (mixed Go/Python)
    "gt-self (Go+Python)": GT_SELF_DB,
}


def query_db(label: str, db_path: Path) -> dict | None:
    if not db_path.exists():
        print(f"  SKIP {label}: {db_path} not found")
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        if total_edges == 0:
            return {"label": label, "nodes": total_nodes, "edges": 0, "note": "empty graph"}

        # Resolution method distribution
        res_dist = {}
        for row in conn.execute(
            "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
        ):
            res_dist[row[0] or "NULL"] = row[1]

        det_methods = {"same_file", "import"}
        det_count = sum(v for k, v in res_dist.items() if k in det_methods)
        det_pct = round(100 * det_count / total_edges, 1)

        # Language distribution
        langs = {}
        for row in conn.execute("SELECT language, COUNT(*) FROM nodes GROUP BY language ORDER BY COUNT(*) DESC"):
            langs[row[0]] = row[1]

        # Confidence check
        has_conf = any(col[1] == "confidence" for col in conn.execute("PRAGMA table_info(edges)"))
        conf_stats = {}
        if has_conf:
            row = conn.execute("""
                SELECT ROUND(AVG(confidence), 3),
                       SUM(CASE WHEN confidence >= 0.9 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN confidence >= 0.5 AND confidence < 0.9 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END)
                FROM edges
            """).fetchone()
            conf_stats = {
                "avg_confidence": row[0],
                "high_conf": row[1],
                "mid_conf": row[2],
                "low_conf": row[3],
            }

        return {
            "label": label,
            "db_size_kb": round(db_path.stat().st_size / 1024),
            "nodes": total_nodes,
            "edges": total_edges,
            "resolution_method": res_dist,
            "deterministic_count": det_count,
            "speculative_count": res_dist.get("name_match", 0),
            "deterministic_pct": det_pct,
            "languages": langs,
            "has_confidence": has_conf,
            **conf_stats,
        }
    finally:
        conn.close()


def main():
    results = []
    for label, db_path in AUDIT_DBS.items():
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"  {db_path}")
        r = query_db(label, db_path)
        if r:
            results.append(r)
            print(f"  Nodes: {r['nodes']:,}  Edges: {r['edges']:,}  DB: {r.get('db_size_kb', '?')}KB")
            print(f"  Deterministic: {r['deterministic_pct']}% ({r['deterministic_count']}/{r['edges']})")
            print(f"  Resolution: {r['resolution_method']}")
            if r.get("has_confidence"):
                print(f"  Confidence: avg={r.get('avg_confidence')} high={r.get('high_conf')} mid={r.get('mid_conf')} low={r.get('low_conf')}")
            print(f"  Languages: {r['languages']}")

    # Summary table
    print(f"\n\n{'='*80}")
    print("SUMMARY TABLE")
    print(f"{'='*80}")
    print(f"{'Label':<30s} {'Nodes':>7s} {'Edges':>7s} {'Det%':>6s} {'same_file':>10s} {'import':>8s} {'name_match':>11s}")
    print("-" * 80)
    for r in results:
        rm = r["resolution_method"]
        print(f"{r['label']:<30s} {r['nodes']:>7,d} {r['edges']:>7,d} {r['deterministic_pct']:>5.1f}% {rm.get('same_file', 0):>10,d} {rm.get('import', 0):>8,d} {rm.get('name_match', 0):>11,d}")

    # By language
    print(f"\n\nBY LANGUAGE:")
    lang_groups = {"Go": [], "TypeScript": [], "Python": [], "Rust": []}
    for r in results:
        for lang in lang_groups:
            if lang.lower() in r["label"].lower() or lang in r["label"]:
                lang_groups[lang].append(r)
    for lang, rs in lang_groups.items():
        if rs:
            avg_det = sum(r["deterministic_pct"] for r in rs) / len(rs)
            print(f"  {lang}: {len(rs)} repos, avg deterministic = {avg_det:.1f}%")

    out_path = Path("D:/Groundtruth/artifact_deepswe/audit_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults: {out_path}")


if __name__ == "__main__":
    main()
