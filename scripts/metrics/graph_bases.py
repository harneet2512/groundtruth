"""Canonical graph-bases taxonomy for graph.db certificates.

Graph bases are the codebase-intelligence substrate GT stores before any
name-matching or model reasoning: symbols, structural edges, hierarchy,
data/dependency flow, contracts, search, and reachability. Keep this file as the
single source of truth so gates and reports do not drift or double-count edge
families under different names.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GraphBase:
    name: str
    kind: str
    sql: str
    required: bool = False


GRAPH_BASES: tuple[GraphBase, ...] = (
    GraphBase("symbols", "nodes", "SELECT COUNT(*) FROM nodes", True),
    GraphBase("calls", "edge", "SELECT COUNT(*) FROM edges WHERE type='CALLS'", True),
    GraphBase("contains", "edge", "SELECT COUNT(*) FROM edges WHERE type='CONTAINS'"),
    GraphBase("imports", "edge", "SELECT COUNT(*) FROM edges WHERE type='IMPORTS'"),
    GraphBase("inheritance", "edge", "SELECT COUNT(*) FROM edges WHERE type IN ('EXTENDS','IMPLEMENTS')"),
    GraphBase("composition", "edge", "SELECT COUNT(*) FROM edges WHERE type='COMPOSES'"),
    GraphBase("re_exports", "edge", "SELECT COUNT(*) FROM edges WHERE type='RE_EXPORTS'"),
    GraphBase("routes", "edge", "SELECT COUNT(*) FROM edges WHERE type IN ('HANDLES_ROUTE','API_CALL')"),
    GraphBase("reads", "edge", "SELECT COUNT(*) FROM edges WHERE type='READS'"),
    GraphBase("writes", "edge", "SELECT COUNT(*) FROM edges WHERE type='WRITES'"),
    GraphBase("raises", "edge", "SELECT COUNT(*) FROM edges WHERE type='RAISES'"),
    GraphBase("data_flow", "edge_or_annotation",
              "SELECT (SELECT COUNT(*) FROM edges WHERE type='DATA_FLOW') + "
              "(SELECT COUNT(*) FROM edges WHERE instr(COALESCE(metadata,''), 'dataflow=') > 0)"),
    GraphBase("uses", "annotation",
              "SELECT COUNT(*) FROM edges WHERE instr(COALESCE(metadata,''), 'usage=') > 0"),
    GraphBase("precedes", "edge", "SELECT COUNT(*) FROM edges WHERE type='PRECEDES'"),
    GraphBase("co_serializes", "edge", "SELECT COUNT(*) FROM edges WHERE type='CO_SERIALIZES'"),
    GraphBase("typed_params", "property", "SELECT COUNT(*) FROM properties WHERE kind='param'"),
    GraphBase("return_shape", "property", "SELECT COUNT(*) FROM properties WHERE kind='return_shape'"),
    GraphBase("class_fields", "property", "SELECT COUNT(*) FROM properties WHERE kind='class_field'"),
    GraphBase("field_reads_raw", "property", "SELECT COUNT(*) FROM properties WHERE kind='field_read'"),
    GraphBase("field_writes_raw", "property", "SELECT COUNT(*) FROM properties WHERE kind='side_effect'"),
    GraphBase("exception_flow_raw", "property",
              "SELECT COUNT(*) FROM properties WHERE kind IN ('exception_type','exception_flow')"),
    GraphBase("data_flow_raw", "property", "SELECT COUNT(*) FROM properties WHERE kind='data_flow'"),
    GraphBase("call_order_raw", "property", "SELECT COUNT(*) FROM properties WHERE kind='call_order'"),
    GraphBase("assertions", "table", "SELECT COUNT(*) FROM assertions"),
    GraphBase("cochanges", "table", "SELECT COUNT(*) FROM cochanges"),
    GraphBase("closure", "table", "SELECT COUNT(*) FROM closure"),
    GraphBase("fts5", "search", "SELECT COUNT(*) FROM nodes_fts"),
)


def _scalar(con: sqlite3.Connection, sql: str) -> int:
    count, _, _ = _scalar_eval(con, sql)
    return count


def _scalar_eval(con: sqlite3.Connection, sql: str) -> tuple[int, bool, str]:
    try:
        row = con.execute(sql).fetchone()
        return (int(row[0]) if row and row[0] is not None else 0, True, "")
    except Exception as exc:
        return 0, False, str(exc)


def _dist(con: sqlite3.Connection, sql: str) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in con.execute(sql).fetchall()}
    except Exception:
        return {}


def graph_bases_contract(con: sqlite3.Connection) -> dict[str, Any]:
    """Return the deduplicated [graph bases] inventory for an open graph.db."""
    bases: dict[str, dict[str, Any]] = {}
    for base in GRAPH_BASES:
        count, evaluatable, error = _scalar_eval(con, base.sql)
        bases[base.name] = {
            "kind": base.kind,
            "count": count,
            "required": base.required,
            "evaluatable": evaluatable,
        }
        if error:
            bases[base.name]["error"] = error

    edge_type_counts = _dist(con, "SELECT type, COUNT(*) FROM edges GROUP BY type")
    property_kind_counts = _dist(con, "SELECT kind, COUNT(*) FROM properties GROUP BY kind")
    language_counts = _dist(con, "SELECT language, COUNT(*) FROM nodes GROUP BY language")
    fk_violations = 0
    try:
        fk_violations = len(con.execute("PRAGMA foreign_key_check").fetchall())
    except Exception:
        fk_violations = -1

    names = [base.name for base in GRAPH_BASES]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    evaluable_missing = [
        name for name, row in bases.items()
        if not bool(row["evaluatable"])
    ]
    required_missing = [
        name for name, row in bases.items()
        if row["required"] and int(row["count"]) <= 0
    ]
    depth_names = (
        "imports", "inheritance", "composition", "re_exports", "routes",
        "reads", "writes", "raises", "data_flow", "uses", "precedes",
        "co_serializes",
    )
    depth_present = [name for name in depth_names if int(bases[name]["count"]) > 0]

    return {
        "label": "[graph bases]",
        "bases": bases,
        "edge_type_counts": edge_type_counts,
        "property_kind_counts": property_kind_counts,
        "language_counts": language_counts,
        "foreign_key_violations": fk_violations,
        "base_total_count": len(GRAPH_BASES),
        "base_evaluable_count": len(GRAPH_BASES) - len(evaluable_missing),
        "base_evaluable_pct": round(
            100.0 * (len(GRAPH_BASES) - len(evaluable_missing)) / max(len(GRAPH_BASES), 1),
            8,
        ),
        "evaluable_missing": evaluable_missing,
        "duplicate_names": duplicate_names,
        "required_missing": required_missing,
        "depth_present": depth_present,
        "depth_present_count": len(depth_present),
        "depth_total_count": len(depth_names),
    }
