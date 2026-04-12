#!/usr/bin/env python3
"""GT Shell Tools — thin CLI for gt_orient/gt_lookup/gt_impact/gt_check.

Runs INSIDE the Docker container. Queries graph.db directly via sqlite3.
No external deps needed (pure stdlib + sqlite3).

Usage:
    python3 /tmp/gt_tools.py orient
    python3 /tmp/gt_tools.py lookup <symbol>
    python3 /tmp/gt_tools.py impact <symbol>
    python3 /tmp/gt_tools.py check <file_path>
"""
import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("GT_DB", "/tmp/graph.db")
ROOT = os.environ.get("GT_ROOT", "/testbed")
MAX_TOKENS = 200  # ~800 chars output cap


def _conn():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: graph.db not found at {DB_PATH}")
        sys.exit(1)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def orient(focus=None):
    """Codebase structure: top dirs, hot symbols, file count."""
    c = _conn()
    out = {}

    # File count
    files = c.execute("SELECT COUNT(DISTINCT file_path) as cnt FROM nodes").fetchone()
    out["files"] = files["cnt"]

    # Top directories
    rows = c.execute("""
        SELECT SUBSTR(file_path, 1, INSTR(file_path||'/', '/')) as dir,
               COUNT(*) as cnt
        FROM nodes GROUP BY dir ORDER BY cnt DESC LIMIT 8
    """).fetchall()
    out["top_dirs"] = [{"dir": r["dir"], "symbols": r["cnt"]} for r in rows]

    # Hot symbols (most referenced)
    rows = c.execute("""
        SELECT n.name, n.file_path, n.label, COUNT(e.id) as refs
        FROM nodes n JOIN edges e ON e.target_id = n.id
        WHERE e.type = 'CALLS'
        GROUP BY n.id ORDER BY refs DESC LIMIT 5
    """).fetchall()
    out["hot_symbols"] = [
        {"name": r["name"], "file": r["file_path"], "refs": r["refs"]}
        for r in rows
    ]

    print(json.dumps(out, indent=2)[:800])


def lookup(symbol):
    """Symbol definition + callers + test references."""
    c = _conn()
    out = {}

    # Find symbol
    rows = c.execute(
        "SELECT * FROM nodes WHERE name = ? LIMIT 3", (symbol,)
    ).fetchall()
    if not rows:
        print(f"Symbol '{symbol}' not found in graph.db")
        return
    if len(rows) > 1:
        out["ambiguous"] = [{"name": r["name"], "file": r["file_path"]} for r in rows]

    node = rows[0]
    out["name"] = node["name"]
    out["file"] = node["file_path"]
    out["line"] = node["start_line"]
    out["signature"] = (node["signature"] or "")[:100]
    out["return_type"] = node["return_type"]

    # Callers (top 5, import/same_file only)
    callers = c.execute("""
        SELECT n.name, n.file_path, e.source_line, e.resolution_method
        FROM edges e JOIN nodes n ON e.source_id = n.id
        WHERE e.target_id = ? AND e.type = 'CALLS'
        AND (e.confidence >= 0.5 OR e.confidence IS NULL)
        ORDER BY e.confidence DESC LIMIT 5
    """, (node["id"],)).fetchall()
    out["callers"] = [
        {"name": r["name"], "file": r["file_path"], "line": r["source_line"]}
        for r in callers
    ]

    # Tests
    tests = c.execute("""
        SELECT n.name, n.file_path FROM nodes n
        JOIN edges e ON e.source_id = n.id
        WHERE e.target_id = ? AND n.is_test = 1
        LIMIT 3
    """, (node["id"],)).fetchall()
    out["tests"] = [{"name": r["name"], "file": r["file_path"]} for r in tests]

    print(json.dumps(out, indent=2)[:800])


def impact(symbol):
    """Pre-edit: callers at risk, obligations, safe vs unsafe."""
    c = _conn()
    out = {}

    rows = c.execute(
        "SELECT * FROM nodes WHERE name = ? LIMIT 1", (symbol,)
    ).fetchall()
    if not rows:
        print(f"Symbol '{symbol}' not found")
        return

    node = rows[0]
    nid = node["id"]
    out["symbol"] = node["name"]
    out["file"] = node["file_path"]

    # Caller count
    caller_count = c.execute(
        "SELECT COUNT(*) as cnt FROM edges WHERE target_id = ? AND type = 'CALLS'",
        (nid,)
    ).fetchone()["cnt"]
    out["caller_count"] = caller_count

    # Callers at risk (cross-file only)
    callers = c.execute("""
        SELECT n.name, n.file_path
        FROM edges e JOIN nodes n ON e.source_id = n.id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND n.file_path != ?
        AND (e.confidence >= 0.5 OR e.confidence IS NULL)
        LIMIT 5
    """, (nid, node["file_path"])).fetchall()
    out["cross_file_callers"] = [
        {"name": r["name"], "file": r["file_path"]} for r in callers
    ]

    # Signature obligation
    if node["signature"]:
        out["signature_obligation"] = (node["signature"] or "")[:80]

    # Exception properties
    exc_props = c.execute(
        "SELECT value FROM properties WHERE node_id = ? AND kind = 'exception_type'",
        (nid,)
    ).fetchall()
    if exc_props:
        out["raises"] = [r["value"] for r in exc_props]

    risk = "HIGH" if caller_count >= 5 else ("MEDIUM" if caller_count >= 2 else "LOW")
    out["risk"] = risk

    # Tier based on average caller edge confidence
    avg_conf = c.execute("""
        SELECT AVG(COALESCE(e.confidence, 0.5)) as avg_conf
        FROM edges e WHERE e.target_id = ? AND e.type = 'CALLS'
    """, (nid,)).fetchone()["avg_conf"] or 0.0
    out["tier"] = "verified" if avg_conf >= 0.8 else ("likely" if avg_conf >= 0.5 else "possible")

    print(json.dumps(out, indent=2)[:800])


def check(file_path):
    """Post-edit: detect arity changes, removed symbols, stale references."""
    c = _conn()
    issues = []

    # Get symbols that WERE in this file according to graph.db
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(ROOT, file_path)
    db_path = file_path

    # Try exact match first, then suffix
    old_nodes = c.execute(
        "SELECT * FROM nodes WHERE file_path = ? AND label IN ('Function','Method')",
        (db_path,)
    ).fetchall()
    if not old_nodes:
        old_nodes = c.execute(
            "SELECT * FROM nodes WHERE file_path LIKE ? AND label IN ('Function','Method')",
            (f"%{file_path}",)
        ).fetchall()

    if not old_nodes:
        print(json.dumps({"status": "clean", "note": "no symbols tracked for this file"}))
        return

    # Read current file
    if not os.path.exists(abs_path):
        print(json.dumps({"status": "error", "note": f"file not found: {abs_path}"}))
        return

    with open(abs_path, "r", errors="replace") as f:
        current_content = f.read()

    # Check each old symbol
    for node in old_nodes:
        name = node["name"]

        # Check if symbol still exists in file
        if f"def {name}" not in current_content and f"class {name}" not in current_content:
            # Symbol removed — check if it has callers
            callers = c.execute(
                "SELECT COUNT(*) as cnt FROM edges WHERE target_id = ? AND type = 'CALLS'"
                " AND (confidence >= 0.5 OR confidence IS NULL)",
                (node["id"],)
            ).fetchone()["cnt"]
            if callers > 0:
                issues.append({
                    "type": "STALE",
                    "symbol": name,
                    "detail": f"removed but {callers} caller(s) still reference it",
                    "severity": "high" if callers >= 3 else "medium"
                })

    if issues:
        print(json.dumps({"status": "blockers", "issues": issues}, indent=2)[:800])
    else:
        print(json.dumps({"status": "clean", "symbols_checked": len(old_nodes)}))


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gt_tools.py orient|lookup|impact|check [args]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "orient":
        orient(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "lookup":
        if len(sys.argv) < 3:
            print("Usage: python3 gt_tools.py lookup <symbol>")
            sys.exit(1)
        lookup(sys.argv[2])
    elif cmd == "impact":
        if len(sys.argv) < 3:
            print("Usage: python3 gt_tools.py impact <symbol>")
            sys.exit(1)
        impact(sys.argv[2])
    elif cmd == "check":
        if len(sys.argv) < 3:
            print("Usage: python3 gt_tools.py check <file_path>")
            sys.exit(1)
        check(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
