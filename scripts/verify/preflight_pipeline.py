"""Preflight pipeline verification — runs BEFORE the agent starts.

Tests every GT layer with the actual graph.db built for this task.
If any check fails, the failure is a GT infrastructure bug, not a
task failure. Run after gt-index + LSP enrichment, before agent launch.

Usage:
    python scripts/verify/preflight_pipeline.py --db /tmp/gt_prebuilt.db --root /tmp/testbed_src

Exit code 0 = all checks pass. Non-zero = broken layer (see output).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def check_graph_exists(db: str) -> tuple[bool, str]:
    if not os.path.exists(db):
        return False, f"graph.db not found at {db}"
    conn = sqlite3.connect(db)
    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()
    if nodes == 0:
        return False, f"graph.db has 0 nodes (indexer failed)"
    if edges == 0:
        return False, f"graph.db has 0 edges (resolver failed)"
    return True, f"nodes={nodes} edges={edges}"


def check_schema_version(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "project_meta" not in tables:
            return False, "project_meta table missing"
        row = conn.execute(
            "SELECT value FROM project_meta WHERE key='schema_version'"
        ).fetchone()
        if not row:
            return False, "schema_version not stamped (L3b will crash)"
        return True, f"schema_version={row[0]}"
    finally:
        conn.close()


def check_fts5(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "nodes_fts" in tables:
            count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            return True, f"nodes_fts exists ({count} entries, Go-built)"
        # Try Python-side creation using shared DDL constants
        try:
            from groundtruth.pretask.graph_localizer import _FTS5_CREATE, _FTS5_POPULATE
            conn2 = sqlite3.connect(db)
            conn2.execute(_FTS5_CREATE)
            conn2.execute(_FTS5_POPULATE)
            conn2.commit()
            count = conn2.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            conn2.close()
            return True, f"nodes_fts created Python-side ({count} entries)"
        except sqlite3.Error as e:
            return False, f"FTS5 unavailable: {e}"
    finally:
        conn.close()


def check_edge_quality(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        if "confidence" not in cols:
            return False, "no confidence column (old schema)"
        total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE confidence >= 0.9"
        ).fetchone()[0]
        pct = (verified / total * 100) if total > 0 else 0
        if "resolution_method" in cols:
            methods = conn.execute(
                "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC LIMIT 5"
            ).fetchall()
            method_str = ", ".join(f"{m}:{c}" for m, c in methods)
        else:
            method_str = "no resolution_method column"
        return True, f"verified(>=0.9)={verified}/{total} ({pct:.0f}%) methods=[{method_str}]"
    finally:
        conn.close()


def check_assertions(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "assertions" not in tables:
            return True, "no assertions table (OK for non-test repos)"
        count = conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM assertions WHERE target_node_id > 0"
        ).fetchone()[0]
        return True, f"assertions={count} linked_to_target={linked}"
    finally:
        conn.close()


def check_lsp_enrichment(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "return_type" not in cols:
            return True, "no return_type column (old schema, LSP enrichment N/A)"
        enriched = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL AND return_type != ''"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE label IN ('Function','Method')"
        ).fetchone()[0]
        return True, f"return_type populated: {enriched}/{total} functions"
    finally:
        conn.close()


def check_brief_generation(db: str, root: str) -> tuple[bool, str]:
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        result = generate_v1r_brief(
            issue_text="test issue with some keywords like function error bug",
            repo_root=root,
            graph_db=db,
        )
        n_files = len(result.files)
        tok = result.token_estimate
        if n_files == 0:
            return False, "brief generated 0 candidates"
        return True, f"brief OK: {n_files} files, {tok} tokens"
    except Exception as e:
        return False, f"brief generation failed: {e}"


def check_l3b_delivery(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        # Pick a non-test file with functions
        row = conn.execute(
            "SELECT file_path FROM nodes WHERE is_test = 0 AND label IN ('Function','Method') "
            "GROUP BY file_path ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False, "no non-test functions in graph"
        test_file = row[0]
        conn.close()

        from groundtruth.hooks.post_view import graph_navigation
        lines, _ = graph_navigation(test_file, db, limit=3)
        if not lines:
            return True, f"graph_navigation returned empty for {test_file} (may be OK)"
        has_evidence = any(
            m in "\n".join(lines)
            for m in ["[CONTRACT]", "[SIGNATURE]", "Called by:", "Calls into:", "[TEST]"]
        )
        return True, f"L3b OK: {len(lines)} lines, evidence={has_evidence}, file={test_file}"
    except Exception as e:
        return False, f"L3b failed: {e}"


def main():
    parser = argparse.ArgumentParser(description="Preflight pipeline verification")
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    checks = [
        ("graph_exists", lambda: check_graph_exists(args.db)),
        ("schema_version", lambda: check_schema_version(args.db)),
        ("fts5", lambda: check_fts5(args.db)),
        ("edge_quality", lambda: check_edge_quality(args.db)),
        ("assertions", lambda: check_assertions(args.db)),
        ("lsp_enrichment", lambda: check_lsp_enrichment(args.db)),
        ("brief_generation", lambda: check_brief_generation(args.db, args.root)),
        ("l3b_delivery", lambda: check_l3b_delivery(args.db)),
    ]

    results = {}
    all_pass = True
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"EXCEPTION: {e}"
        results[name] = {"pass": ok, "detail": detail}
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        if not args.json:
            print(f"  [{status}] {name}: {detail}")

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print()
        if all_pass:
            print("PREFLIGHT: ALL CHECKS PASS")
        else:
            failed = [k for k, v in results.items() if not v["pass"]]
            print(f"PREFLIGHT: {len(failed)} FAILURES: {', '.join(failed)}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
