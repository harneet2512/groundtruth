#!/usr/bin/env python3
"""Delivery-layer preflight — fail FAST on C6/C7 runtime fuckups (RUNTIME_FUCKUPS.md).

Runs against a graph.db produced by gt-index (+ offline promotion). Catches the
silent-failure surfaces the GHA adversary flagged, in seconds, BEFORE a 90-min
eval — so we don't discover a broken delivery layer after the full run.

Checks (opt in per workflow stage):
  --schema            nodes/edges tables + resolution_method/confidence columns.
  --closure           C7: the `closure` table EXISTS (Go closure pass compiled +
                      added the table). Catches a gt-index that built WITHOUT the
                      closure pass / a Go compile regression.
  --closure-populated C7: closure has rows IFF there are verified CALLS edges.
                      Catches a closure pass that ran but produced nothing (or
                      crashed non-fatally). On a repo with no calls, empty is OK.
  --require-lsp       C6: at least one resolution_method='lsp' edge exists.
                      Catches the '|| echo WARN'-swallowed un-promoted db (pyright
                      missing / resolve timed out → looks benign in logs, ships
                      un-promoted). Use AFTER the promotion step on a repo that has
                      cross-file edges.
  --lsp-report        C6: print name_match vs lsp edge counts (promotion yield) —
                      informational, never fails.

Exit 0 = all requested checks pass; exit 1 = a check failed (with a clear reason).
LLM-free, stdlib only.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

_VERIFIED_METHODS = (
    "same_file", "import", "verified_unique", "type_flow", "import_type", "lsp_verified", "lsp",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return 0


def run(db: str, args: argparse.Namespace) -> list[str]:
    """Return a list of failure messages (empty = all pass)."""
    fails: list[str] = []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        ecols = _cols(conn, "edges")

        if args.schema:
            if not _table_exists(conn, "nodes"):
                fails.append("schema: 'nodes' table missing")
            if not _table_exists(conn, "edges"):
                fails.append("schema: 'edges' table missing")
            for c in ("resolution_method", "confidence"):
                if c not in ecols:
                    fails.append(f"schema: edges.{c} column missing")

        # how many verified CALLS edges exist (the closure's input universe)
        verified_clause = (
            "(confidence >= 0.5 OR resolution_method IN ({}))".format(
                ",".join(f"'{m}'" for m in _VERIFIED_METHODS)
            )
            if "confidence" in ecols and "resolution_method" in ecols
            else "1"
        )
        verified_calls = _count(
            conn, f"SELECT COUNT(*) FROM edges WHERE type='CALLS' AND {verified_clause}"
        )

        if args.closure or args.closure_populated:
            if not _table_exists(conn, "closure"):
                fails.append(
                    "C7: 'closure' table missing — gt-index built WITHOUT the closure "
                    "pass (Go compile regression or pass removed). See RF-4."
                )
            elif args.closure_populated:
                crows = _count(conn, "SELECT COUNT(*) FROM closure")
                if verified_calls > 0 and crows == 0:
                    fails.append(
                        f"C7: 'closure' table EMPTY but {verified_calls} verified CALLS "
                        "edges exist — closure pass ran but produced nothing (crashed "
                        "non-fatally?). See RF-4."
                    )

        if args.require_lsp or args.lsp_report:
            lsp = _count(conn, "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'") \
                if "resolution_method" in ecols else 0
            nm = _count(conn, "SELECT COUNT(*) FROM edges WHERE resolution_method='name_match'") \
                if "resolution_method" in ecols else 0
            if args.lsp_report:
                print(f"[C6 yield] lsp={lsp}  name_match={nm}  verified_calls={verified_calls}")
            if args.require_lsp and lsp == 0:
                fails.append(
                    "C6: ZERO resolution_method='lsp' edges — offline promotion did NOT "
                    "happen (pyright missing / resolve timed out and was '|| WARN'-swallowed; "
                    "the db ships un-promoted while logs look benign). See RF-1/RF-2."
                )
    finally:
        conn.close()
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="Delivery-layer (C6/C7) preflight")
    ap.add_argument("db", help="path to graph.db")
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--closure", action="store_true", help="closure table must exist (C7)")
    ap.add_argument("--closure-populated", dest="closure_populated", action="store_true",
                    help="closure must have rows if verified CALLS edges exist (C7)")
    ap.add_argument("--require-lsp", dest="require_lsp", action="store_true",
                    help="at least one lsp-promoted edge must exist (C6)")
    ap.add_argument("--lsp-report", dest="lsp_report", action="store_true",
                    help="print promotion yield (informational)")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.db):
        print(f"FAIL: graph.db not found: {args.db}", file=sys.stderr)
        return 1

    fails = run(args.db, args)
    if fails:
        print("=== DELIVERY-LAYER PREFLIGHT: FAIL ===", file=sys.stderr)
        for f in fails:
            print(f"  FAIL: {f}", file=sys.stderr)
        return 1
    print("=== DELIVERY-LAYER PREFLIGHT: PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
