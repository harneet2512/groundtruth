#!/usr/bin/env python3
"""Graph quality metrics — computable on any indexed repo's graph.db.

Usage:
    python scripts/graph_quality_metrics.py path/to/graph.db
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def compute_metrics(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    m: dict = {}

    c.execute("SELECT COUNT(*) FROM edges")
    m["total_edges"] = c.fetchone()[0]

    if m["total_edges"] == 0:
        conn.close()
        return m

    c.execute("SELECT COUNT(*) FROM nodes")
    m["total_nodes"] = c.fetchone()[0]

    # Edge type distribution
    c.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY COUNT(*) DESC")
    m["edges_by_type"] = dict(c.fetchall())

    # Resolution method distribution
    c.execute("SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC")
    m["edges_by_resolution_method"] = dict(c.fetchall())

    # Language distribution
    c.execute("SELECT language, COUNT(*) FROM nodes GROUP BY language ORDER BY COUNT(*) DESC")
    m["nodes_by_language"] = dict(c.fetchall())

    # Trust tier (if available)
    c.execute("PRAGMA table_info(edges)")
    columns = [row[1] for row in c.fetchall()]
    has_trust_tier = "trust_tier" in columns
    has_candidate_count = "candidate_count" in columns

    if has_trust_tier:
        c.execute("SELECT trust_tier, COUNT(*) FROM edges GROUP BY trust_tier ORDER BY COUNT(*) DESC")
        m["edges_by_trust_tier"] = dict(c.fetchall())

    if has_candidate_count:
        c.execute("SELECT candidate_count, COUNT(*) FROM edges WHERE candidate_count IS NOT NULL GROUP BY candidate_count ORDER BY candidate_count")
        m["edges_by_candidate_count"] = dict(c.fetchall())

    # Confidence distribution
    c.execute("""
    SELECT
      CASE
        WHEN confidence >= 0.9 THEN 'certified_ge_0.9'
        WHEN confidence >= 0.5 THEN 'candidate_0.5_0.89'
        WHEN confidence >= 0.2 THEN 'speculative_0.2_0.49'
        ELSE 'noise_lt_0.2'
      END as bucket,
      COUNT(*)
    FROM edges
    GROUP BY bucket
    ORDER BY confidence DESC
    """)
    m["edges_by_confidence_bucket"] = dict(c.fetchall())

    # Certified edge ratio
    c.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.9")
    certified = c.fetchone()[0]
    m["certified_edge_count"] = certified
    m["certified_edge_ratio"] = round(certified / m["total_edges"], 4)

    # Speculative edge ratio
    c.execute("SELECT COUNT(*) FROM edges WHERE confidence < 0.5")
    speculative = c.fetchone()[0]
    m["speculative_edge_count"] = speculative
    m["speculative_edge_ratio"] = round(speculative / m["total_edges"], 4)

    # Location coverage
    c.execute("SELECT COUNT(*) FROM edges WHERE source_line IS NOT NULL AND source_line > 0")
    m["edges_with_source_line"] = c.fetchone()[0]
    m["location_backed_edge_ratio"] = round(m["edges_with_source_line"] / m["total_edges"], 4)

    # Name-match sub-distribution
    c.execute("""
    SELECT confidence, COUNT(*) FROM edges
    WHERE resolution_method = 'name_match'
    GROUP BY confidence ORDER BY confidence DESC
    """)
    m["name_match_by_confidence"] = dict(c.fetchall())

    # Import coverage (cross-file)
    c.execute("SELECT COUNT(*) FROM edges WHERE resolution_method != 'same_file'")
    cross_file = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM edges WHERE resolution_method = 'import'")
    import_count = c.fetchone()[0]
    m["cross_file_edge_count"] = cross_file
    m["import_resolved_count"] = import_count
    m["import_coverage_ratio"] = round(import_count / max(cross_file, 1), 4)

    # File connectivity at different thresholds
    for threshold in [0.0, 0.5, 0.7, 0.9]:
        c.execute("""
        SELECT COUNT(DISTINCT n1.file_path || '|' || n2.file_path)
        FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.file_path IS NOT NULL AND n2.file_path IS NOT NULL
          AND n1.file_path != n2.file_path
          AND COALESCE(e.confidence, 0.5) >= ?
        """, (threshold,))
        m[f"file_pairs_conf_ge_{threshold}"] = c.fetchone()[0]

    # Same-package rate by confidence (precision proxy)
    for conf_val in [0.2, 0.4, 0.6, 0.9]:
        c.execute("""
        SELECT
            SUM(CASE
                WHEN SUBSTR(e.source_file, 1, INSTR(e.source_file, '/') - 1) =
                     SUBSTR(n.file_path, 1, INSTR(n.file_path, '/') - 1)
                THEN 1 ELSE 0 END) as same_pkg,
            COUNT(*) as total
        FROM edges e
        JOIN nodes n ON n.id = e.target_id
        WHERE e.resolution_method = 'name_match'
          AND e.confidence = ?
          AND e.source_file IS NOT NULL
          AND e.source_file LIKE '%/%'
        """, (conf_val,))
        row = c.fetchone()
        if row and row[1] and row[1] > 0:
            m[f"same_package_rate_conf_{conf_val}"] = round((row[0] or 0) / row[1], 4)

    # Function name ambiguity
    c.execute("SELECT COUNT(DISTINCT name) FROM nodes WHERE label IN ('Function', 'Method')")
    m["unique_function_names"] = c.fetchone()[0]
    c.execute("""
    SELECT COUNT(DISTINCT name) FROM (
        SELECT name FROM nodes WHERE label IN ('Function', 'Method')
        GROUP BY name HAVING COUNT(*) >= 6
    )
    """)
    m["ambiguous_names_6plus"] = c.fetchone()[0]
    c.execute("""
    SELECT COUNT(DISTINCT name) FROM (
        SELECT name FROM nodes WHERE label IN ('Function', 'Method')
        GROUP BY name HAVING COUNT(*) = 1
    )
    """)
    m["unique_singleton_names"] = c.fetchone()[0]

    conn.close()
    return m


def print_report(m: dict, db_path: str) -> None:
    print(f"=== Graph Quality Report: {db_path} ===\n")

    print(f"Nodes: {m.get('total_nodes', 0)}")
    print(f"Edges: {m['total_edges']}")
    print()

    print("--- Edge Type Distribution ---")
    for t, c in m.get("edges_by_type", {}).items():
        print(f"  {t}: {c} ({100*c/m['total_edges']:.1f}%)")

    print("\n--- Resolution Method ---")
    for method, c in m.get("edges_by_resolution_method", {}).items():
        print(f"  {method}: {c} ({100*c/m['total_edges']:.1f}%)")

    print("\n--- Confidence Buckets ---")
    for bucket, c in m.get("edges_by_confidence_bucket", {}).items():
        print(f"  {bucket}: {c} ({100*c/m['total_edges']:.1f}%)")

    if "edges_by_trust_tier" in m:
        print("\n--- Trust Tier ---")
        for tier, c in m["edges_by_trust_tier"].items():
            print(f"  {tier}: {c} ({100*c/m['total_edges']:.1f}%)")

    print("\n--- Key Ratios ---")
    print(f"  certified_edge_ratio: {m.get('certified_edge_ratio', 'N/A')}")
    print(f"  speculative_edge_ratio: {m.get('speculative_edge_ratio', 'N/A')}")
    print(f"  location_backed_edge_ratio: {m.get('location_backed_edge_ratio', 'N/A')}")
    print(f"  import_coverage_ratio: {m.get('import_coverage_ratio', 'N/A')}")

    print("\n--- Name-Match Sub-Distribution ---")
    for conf, c in m.get("name_match_by_confidence", {}).items():
        print(f"  confidence={conf}: {c}")

    print("\n--- File Connectivity at Thresholds ---")
    for t in [0.0, 0.5, 0.7, 0.9]:
        key = f"file_pairs_conf_ge_{t}"
        if key in m:
            print(f"  conf >= {t}: {m[key]} connected file pairs")

    print("\n--- Same-Package Rate (Precision Proxy) ---")
    for conf_val in [0.2, 0.4, 0.6, 0.9]:
        key = f"same_package_rate_conf_{conf_val}"
        if key in m:
            print(f"  conf={conf_val}: {m[key]*100:.0f}% same-package")

    print("\n--- Name Ambiguity ---")
    print(f"  Unique function names: {m.get('unique_function_names', 'N/A')}")
    print(f"  Singleton names (1 def): {m.get('unique_singleton_names', 'N/A')}")
    print(f"  Ambiguous names (6+ defs): {m.get('ambiguous_names_6plus', 'N/A')}")

    # Language distribution
    print("\n--- Language ---")
    for lang, c in m.get("nodes_by_language", {}).items():
        print(f"  {lang}: {c} nodes")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/graph_quality_metrics.py <path/to/graph.db>")
        sys.exit(1)

    db_path = sys.argv[1]
    if not Path(db_path).exists():
        print(f"ERROR: {db_path} not found")
        sys.exit(1)

    metrics = compute_metrics(db_path)
    print_report(metrics, db_path)


if __name__ == "__main__":
    main()
