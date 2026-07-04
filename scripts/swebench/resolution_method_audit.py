#!/usr/bin/env python3
"""Audit graph resolution-method risk from targeted DeepSWE proof artifacts.

This is a post-run analyzer: it does not run agents, tests, or benchmarks. Feed it
one or more trial artifact directories or graph.db paths and it emits a JSON report
for G2 localization hardening.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# B3: import the SAME canonical fact-set the product uses (curation_map) so the audit's
# notion of "trusted" cannot drift from the consumer's. The old literal was missing
# verified_unique / lsp / lsp_verified, so it UNDER-counted trust on every LSP-promoted
# graph — the better the §3 enrichment worked, the worse the audit said the graph was.
# Mirrors scripts/metrics/foundational_gates.py (same import + fallback).
try:
    from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS as _CANON

    TRUSTED_METHODS = frozenset(_CANON)
    _TRUSTED_SOURCE = "curation_map.DETERMINISTIC_RESOLUTION_METHODS"
except Exception:  # pragma: no cover - fallback only when src not importable
    TRUSTED_METHODS = frozenset(
        {
            "same_file", "import", "import_type", "type_flow", "verified_unique",
            "inherited", "return_type", "lsp", "lsp_verified",
        }
    )
    _TRUSTED_SOURCE = "fallback_literal(curation_map import failed)"
UNTRUSTED_METHOD_PREFIXES = ("name_match",)
RECEIVER_TYPE_METHODS = {"type_flow", "import_type", "return_type", "inherited"}
RECEIVER_UNPROVEN_METHODS = {"impl_method", "unique_method"}


def _scalar(cur: sqlite3.Cursor, sql: str, args: tuple[Any, ...] = ()) -> int:
    row = cur.execute(sql, args).fetchone()
    return int(row[0] or 0) if row else 0


def _dist(cur: sqlite3.Cursor, sql: str) -> dict[str, int]:
    return {str(k or ""): int(v or 0) for k, v in cur.execute(sql).fetchall()}


def _columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}


def _find_graphs(inputs: list[str]) -> list[Path]:
    graphs: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_file() and p.name == "graph.db":
            graphs.append(p)
            continue
        if p.is_dir():
            graphs.extend(sorted(p.rglob("graph.db")))
    return sorted(dict.fromkeys(g.resolve() for g in graphs))


def audit_graph(path: Path, *, sample_limit: int) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cols = _columns(cur, "edges")
    has_conf = "confidence" in cols
    has_trust = "trust_tier" in cols
    has_cc = "candidate_count" in cols

    total_calls = _scalar(cur, "SELECT COUNT(*) FROM edges WHERE type='CALLS'")
    by_method = _dist(
        cur,
        "SELECT COALESCE(resolution_method, ''), COUNT(*) "
        "FROM edges WHERE type='CALLS' GROUP BY COALESCE(resolution_method, '') "
        "ORDER BY COUNT(*) DESC",
    )
    trusted = sum(v for k, v in by_method.items() if k in TRUSTED_METHODS)
    untrusted = sum(
        v for k, v in by_method.items()
        if any(k.startswith(prefix) for prefix in UNTRUSTED_METHOD_PREFIXES)
    )
    receiver_type_proven = sum(v for k, v in by_method.items() if k in RECEIVER_TYPE_METHODS)
    receiver_unproven = sum(v for k, v in by_method.items() if k in RECEIVER_UNPROVEN_METHODS)
    lsp_promoted = sum(v for k, v in by_method.items() if k.startswith("lsp"))
    ambiguous = 0
    if has_cc:
        ambiguous = _scalar(
            cur,
            "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND COALESCE(candidate_count, 0) > 1",
        )

    low_conf_trusted = 0
    if has_conf:
        placeholders = ",".join("?" for _ in TRUSTED_METHODS)
        low_conf_trusted = _scalar(
            cur,
            "SELECT COUNT(*) FROM edges WHERE type='CALLS' "
            f"AND resolution_method IN ({placeholders}) AND COALESCE(confidence, 0) < 0.7",
            tuple(sorted(TRUSTED_METHODS)),
        )

    sample_cols = ["id", "type", "resolution_method", "source_id", "target_id"]
    for c in ("confidence", "trust_tier", "candidate_count", "source_file", "source_line"):
        if c in cols:
            sample_cols.append(c)
    sample_sql = (
        "SELECT " + ", ".join(sample_cols) + " FROM edges WHERE type='CALLS' AND ("
        "COALESCE(resolution_method, '') LIKE 'name_match%'"
    )
    params: list[Any] = []
    if has_cc:
        sample_sql += " OR COALESCE(candidate_count, 0) > 1"
    if has_conf:
        sample_sql += " OR COALESCE(confidence, 0) < 0.7"
    sample_sql += ") ORDER BY id LIMIT ?"
    params.append(sample_limit)
    samples = [dict(row) for row in cur.execute(sample_sql, tuple(params)).fetchall()]
    lsp_residuals = _lsp_residuals(cur, cols, sample_limit=sample_limit)
    trusted_file_rank = _trusted_file_rank(cur, sample_limit=sample_limit)
    scip = _scip_coverage(cur)

    report = {
        "graph_db": str(path),
        "call_edges": total_calls,
        "resolution_method_distribution": by_method,
        "scip_coverage": scip,
        "trusted_call_edges": trusted,
        "untrusted_name_match_call_edges": untrusted,
        "receiver_type_proven_call_edges": receiver_type_proven,
        "receiver_unproven_call_edges": receiver_unproven,
        "lsp_promoted_call_edges": lsp_promoted,
        "ambiguous_candidate_call_edges": ambiguous,
        "low_confidence_trusted_call_edges": low_conf_trusted,
        "trusted_ratio": (trusted / total_calls) if total_calls else None,
        "risk_samples": samples,
        "demand_lsp_residuals": lsp_residuals,
        "trusted_graph_rank": trusted_file_rank,
    }
    if has_trust:
        report["trust_tier_distribution"] = _dist(
            cur,
            "SELECT COALESCE(trust_tier, ''), COUNT(*) FROM edges WHERE type='CALLS' "
            "GROUP BY COALESCE(trust_tier, '') ORDER BY COUNT(*) DESC",
        )
    con.close()
    return report


def _scip_coverage(cur: sqlite3.Cursor) -> dict[str, Any]:
    cols = _columns(cur, "nodes")
    scip_cols = sorted(c for c in cols if "scip" in c.lower() or c.lower() in {"symbol", "symbol_id"})
    meta_count = 0
    try:
        meta_count = _scalar(
            cur,
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND lower(name) LIKE '%scip%'",
        )
    except sqlite3.Error:
        meta_count = 0
    return {
        "node_columns": scip_cols,
        "scip_table_count": meta_count,
        "present": bool(scip_cols or meta_count),
    }


def _lsp_residuals(cur: sqlite3.Cursor, cols: set[str], *, sample_limit: int) -> list[dict[str, Any]]:
    sample_cols = ["e.id", "e.resolution_method", "e.source_id", "e.target_id"]
    if "confidence" in cols:
        sample_cols.append("e.confidence")
    if "candidate_count" in cols:
        sample_cols.append("e.candidate_count")
    for col in ("source_file", "source_line"):
        if col in cols:
            sample_cols.append(f"e.{col}")
    sql = (
        "SELECT " + ", ".join(sample_cols) + ", "
        "COALESCE(ns.file_path, '') AS caller_file, COALESCE(nt.file_path, '') AS target_file, "
        "COALESCE(ns.name, '') AS caller_name, COALESCE(nt.name, '') AS target_name "
        "FROM edges e "
        "LEFT JOIN nodes ns ON e.source_id = ns.id "
        "LEFT JOIN nodes nt ON e.target_id = nt.id "
        "WHERE e.type='CALLS' AND COALESCE(e.resolution_method, '') LIKE 'name_match%' "
        "ORDER BY COALESCE(e.candidate_count, 0) DESC, e.id LIMIT ?"
    )
    try:
        return [dict(row) for row in cur.execute(sql, (sample_limit,)).fetchall()]
    except sqlite3.Error:
        return []


def _trusted_file_rank(cur: sqlite3.Cursor, *, sample_limit: int) -> list[dict[str, Any]]:
    methods = ",".join("?" for _ in TRUSTED_METHODS)
    sql = (
        "SELECT COALESCE(nt.file_path, '') AS file_path, COUNT(*) AS trusted_in_edges "
        "FROM edges e JOIN nodes nt ON e.target_id = nt.id "
        "WHERE e.type='CALLS' AND e.resolution_method IN (" + methods + ") "
        "GROUP BY COALESCE(nt.file_path, '') "
        "ORDER BY trusted_in_edges DESC, file_path ASC LIMIT ?"
    )
    try:
        rows = cur.execute(sql, tuple(sorted(TRUSTED_METHODS)) + (sample_limit,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", help="artifact dirs or graph.db paths")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args(argv)
    graphs = _find_graphs(args.inputs)
    payload = {
        "schema": "gt.resolution_method_audit.v1",
        "graph_count": len(graphs),
        # B3: record where the trusted set came from (canonical import vs fallback) so a
        # reader can tell the audit is aligned to the product fact-set, not drifting.
        "trusted_methods": sorted(TRUSTED_METHODS),
        "trusted_methods_source": _TRUSTED_SOURCE,
        "graphs": [audit_graph(g, sample_limit=args.sample_limit) for g in graphs],
    }
    json.dump(payload, fp=sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if graphs else 2


if __name__ == "__main__":
    raise SystemExit(main())
