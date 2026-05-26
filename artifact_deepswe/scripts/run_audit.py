#!/usr/bin/env python3
"""Run gt-index on 9 audit repos and collect metrics.

Clones each repo at the exact DeepSWE commit, runs gt-index, and queries
resolution_method distribution from the resulting graph.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

GT_INDEX = Path(os.environ.get(
    "GT_INDEX_BIN",
    str(Path(__file__).resolve().parents[2] / "gt-index" / "gt-index.exe"),
))

AUDIT_DIR = Path(__file__).resolve().parent.parent / "audit_repos"

AUDIT_REPOS = [
    # Go
    {"instance_id": "etree-xml-diff-patch", "repo_url": "https://github.com/beevik/etree", "commit": "4032e04c8f2e2f35e43ce5d772fcef14a5df4d74", "language": "go"},
    {"instance_id": "expr-try-catch-errors", "repo_url": "https://github.com/expr-lang/expr", "commit": "851b241a301f7c74646e65e4009c69cf290993a8", "language": "go"},
    {"instance_id": "task-task-graph-export", "repo_url": "https://github.com/go-task/task", "commit": "54bdcba369357b47e19066b57badfb216a4c8d95", "language": "go"},
    # TypeScript
    {"instance_id": "arktype-json-schema-refs-dependencies", "repo_url": "https://github.com/arktypeio/arktype", "commit": "04355e8b26d1ad5264ef62314a2bc46c4de58ed8", "language": "typescript"},
    {"instance_id": "kysely-window-grouping-helpers", "repo_url": "https://github.com/kysely-org/kysely", "commit": "91cf3733b2a419f5b17dff118cedb7052ab5300d", "language": "typescript"},
    {"instance_id": "ts-pattern-match-each", "repo_url": "https://github.com/gvergnaud/ts-pattern", "commit": "f66fc061fde4f764b113ededa09be63dae564159", "language": "typescript"},
    # Python
    {"instance_id": "dateutil-rfc5545-timezone-interop", "repo_url": "https://github.com/dateutil/dateutil", "commit": "c981f9c7aa91b83cc9bd33a09ecee9e751b06e8d", "language": "python"},
    {"instance_id": "kombu-single-active-consumer-priority", "repo_url": "https://github.com/celery/kombu", "commit": "3c5c1bd86376ee73d52a4cc770bdaeab15bbc2f3", "language": "python"},
    {"instance_id": "sqlite-utils-safe-import-checkpoints", "repo_url": "https://github.com/simonw/sqlite-utils", "commit": "8d74ffc93292c604d5827e2b44fffedca0c28c19", "language": "python"},
]


def clone_repo(repo: dict) -> Path:
    repo_dir = AUDIT_DIR / repo["instance_id"]
    if repo_dir.exists():
        print(f"  [skip] Already cloned: {repo_dir}")
        return repo_dir

    print(f"  Cloning {repo['repo_url']}...")
    subprocess.run(
        ["git", "clone", "--quiet", repo["repo_url"], str(repo_dir)],
        check=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", repo["commit"]],
        cwd=str(repo_dir),
        check=True,
        timeout=30,
    )
    return repo_dir


def run_gt_index(repo_dir: Path) -> tuple[int, float, int]:
    """Returns (exit_code, wall_seconds, db_size_bytes)."""
    db_path = repo_dir / "graph.db"
    if db_path.exists():
        db_path.unlink()

    start = time.monotonic()
    result = subprocess.run(
        [str(GT_INDEX), f"-root={repo_dir}", f"-output={db_path}"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    elapsed = time.monotonic() - start

    db_size = db_path.stat().st_size if db_path.exists() else 0
    if result.returncode != 0:
        print(f"  [ERROR] gt-index failed (exit {result.returncode})")
        print(f"  stderr: {result.stderr[:500]}")

    return result.returncode, elapsed, db_size


def query_metrics(db_path: Path) -> dict:
    """Query resolution_method distribution and node/edge counts."""
    if not db_path.exists():
        return {"error": "no graph.db"}

    conn = sqlite3.connect(str(db_path))
    try:
        total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        resolution_dist = {}
        for row in conn.execute(
            "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
        ):
            resolution_dist[row[0] or "NULL"] = row[1]

        # Check if confidence column exists
        has_confidence = False
        for col in conn.execute("PRAGMA table_info(edges)"):
            if col[1] == "confidence":
                has_confidence = True
                break

        confidence_stats = {}
        if has_confidence:
            for row in conn.execute("""
                SELECT
                    ROUND(AVG(confidence), 3) as avg_conf,
                    ROUND(MIN(confidence), 3) as min_conf,
                    ROUND(MAX(confidence), 3) as max_conf,
                    SUM(CASE WHEN confidence >= 0.7 THEN 1 ELSE 0 END) as high_conf,
                    SUM(CASE WHEN confidence >= 0.5 AND confidence < 0.7 THEN 1 ELSE 0 END) as mid_conf,
                    SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END) as low_conf
                FROM edges
            """):
                confidence_stats = {
                    "avg": row[0], "min": row[1], "max": row[2],
                    "high_conf_count": row[3], "mid_conf_count": row[4], "low_conf_count": row[5],
                }

        # Deterministic breakdown
        det_methods = {"same_file", "import"}
        det_count = sum(v for k, v in resolution_dist.items() if k in det_methods)
        spec_count = resolution_dist.get("name_match", 0)
        det_pct = round(100 * det_count / total_edges, 1) if total_edges > 0 else 0

        # Language distribution
        lang_dist = {}
        for row in conn.execute("SELECT language, COUNT(*) FROM nodes GROUP BY language ORDER BY COUNT(*) DESC"):
            lang_dist[row[0]] = row[1]

        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "resolution_method": resolution_dist,
            "deterministic_edges": det_count,
            "speculative_edges": spec_count,
            "deterministic_pct": det_pct,
            "confidence": confidence_stats,
            "languages": lang_dist,
        }
    finally:
        conn.close()


def main() -> None:
    print(f"GT Index binary: {GT_INDEX}")
    if not GT_INDEX.exists():
        print(f"ERROR: gt-index not found at {GT_INDEX}", file=sys.stderr)
        sys.exit(1)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for repo in AUDIT_REPOS:
        print(f"\n{'='*60}")
        print(f"Repo: {repo['instance_id']} ({repo['language']})")
        print(f"URL:  {repo['repo_url']}")
        print(f"{'='*60}")

        try:
            repo_dir = clone_repo(repo)
        except Exception as e:
            print(f"  [ERROR] Clone failed: {e}")
            results.append({**repo, "error": f"clone_failed: {e}"})
            continue

        exit_code, elapsed, db_size = run_gt_index(repo_dir)
        metrics = query_metrics(repo_dir / "graph.db") if exit_code == 0 else {"error": f"exit_code={exit_code}"}

        result = {
            **repo,
            "index_exit_code": exit_code,
            "index_time_sec": round(elapsed, 1),
            "db_size_bytes": db_size,
            **metrics,
        }
        results.append(result)

        print(f"  Time: {elapsed:.1f}s | DB: {db_size:,} bytes | Nodes: {metrics.get('total_nodes', '?')} | Edges: {metrics.get('total_edges', '?')}")
        if "deterministic_pct" in metrics:
            print(f"  Deterministic: {metrics['deterministic_pct']}% ({metrics['deterministic_edges']}/{metrics['total_edges']})")
            print(f"  Resolution: {metrics.get('resolution_method', {})}")

    # Write results
    out_path = Path(__file__).resolve().parent.parent / "audit_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
