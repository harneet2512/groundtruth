#!/usr/bin/env python3
"""Diagnostic: why do extracted identifiers fail to resolve in graph.db?

Usage:
  python diagnose_graph_gaps.py --db graph.db --identifiers "foo,bar,Baz"
  python diagnose_graph_gaps.py --db graph.db --issue issue.txt
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Import from gt_intel (same directory)
sys.path.insert(0, os.path.dirname(__file__))
from gt_intel import extract_identifiers_from_issue  # type: ignore[import-untyped]


def diagnose(conn: sqlite3.Connection, identifiers: list[str]) -> dict[str, list[str]]:
    """Check each identifier against the graph and classify resolution."""
    results: dict[str, list[str]] = {
        "exact": [],
        "icase": [],
        "qname": [],
        "suffix": [],
        "miss": [],
    }

    cur = conn.cursor()
    for ident in identifiers:
        # 1. Exact match on name
        row = cur.execute(
            "SELECT id, name, file_path, start_line FROM nodes WHERE name = ? LIMIT 1",
            (ident,),
        ).fetchone()
        if row:
            results["exact"].append(f"{ident} -> {row[2]}:{row[3]}")
            continue

        # 2. Case-insensitive match
        row = cur.execute(
            "SELECT id, name, file_path, start_line FROM nodes WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (ident,),
        ).fetchone()
        if row:
            results["icase"].append(f"{ident} -> found as {row[1]} (case mismatch)")
            continue

        # 3. Suffix match on qualified_name
        row = cur.execute(
            "SELECT id, qualified_name, file_path, start_line FROM nodes WHERE qualified_name LIKE ? LIMIT 1",
            (f"%{ident}",),
        ).fetchone()
        if row:
            results["qname"].append(f"{ident} -> found as {row[1]} (qualified name)")
            continue

        # 4. Suffix match on name (partial)
        row = cur.execute(
            "SELECT id, name, file_path, start_line FROM nodes WHERE name LIKE ? LIMIT 1",
            (f"%{ident}%",),
        ).fetchone()
        if row:
            results["suffix"].append(f"{ident} -> partial match {row[1]} in {row[2]}")
            continue

        # 5. File path match (if identifier looks like a path)
        if "/" in ident or ident.endswith(".py"):
            row = cur.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE ? LIMIT 1",
                (f"%{ident}%",),
            ).fetchone()
            if row:
                results["suffix"].append(f"{ident} -> file path match {row[0]}")
                continue

        results["miss"].append(ident)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose graph lookup gaps")
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--identifiers", default="", help="Comma-separated identifiers")
    parser.add_argument("--issue", default="", help="Path to issue text file")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} not found", file=sys.stderr)
        sys.exit(1)

    if args.issue:
        with open(args.issue) as f:
            identifiers = extract_identifiers_from_issue(f.read())
    elif args.identifiers:
        identifiers = [s.strip() for s in args.identifiers.split(",") if s.strip()]
    else:
        print("ERROR: provide --identifiers or --issue", file=sys.stderr)
        sys.exit(1)

    print(f"Extracted {len(identifiers)} identifiers: {identifiers[:10]}...")

    conn = sqlite3.connect(args.db)
    results = diagnose(conn, identifiers)
    conn.close()

    total = len(identifiers)
    resolved = total - len(results["miss"])

    print(f"\n--- Resolution Report ({resolved}/{total} resolved) ---")
    for category, items in results.items():
        tag = category.upper()
        if items:
            print(f"\n[{tag}] ({len(items)}):")
            for item in items:
                print(f"  {item}")

    print(f"\nSummary: exact={len(results['exact'])}, icase={len(results['icase'])}, "
          f"qname={len(results['qname'])}, suffix={len(results['suffix'])}, "
          f"miss={len(results['miss'])}")


if __name__ == "__main__":
    main()
