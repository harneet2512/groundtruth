"""gt_closure — the precomputed transitive-reach sidecar.

Question: "What transitively calls this symbol, and what does it transitively
call?" — answered by the ``closure`` table the producer publishes (C7/RF-4),
not by a live traversal.

A ``closure`` row ``(source_id, target_id, depth, min_confidence)`` means
``source_id`` reaches ``target_id`` in ``depth`` verified-CALLS hops with a
weakest-edge confidence of ``min_confidence``. For a symbol:

  * callers  = rows with ``target_id`` = the symbol (sources reach it)
  * callees  = rows with ``source_id`` = the symbol (it reaches them)

Each direction reports the shortest depth per neighbour. The table is
bounded at build time (depth<=3, min_confidence>=0.5, verified edges only) —
those producer bounds are stated in the response, not re-applied.

Abstention: a pre-C7 graph has no ``closure`` table → typed ``unavailable``.
A stale closure (post-incremental partial DROP, detected by the same
two-signal probe ImportGraph uses) is still reported but flagged
``stale: true`` — the rows are real, just provably incomplete.
"""

from __future__ import annotations

from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.mcp.endpoints import _graph_db
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints.closure")

_MAX_ROWS = 50
# Producer-side bounds (gt-index/internal/closure): the table is built at
# MaxDepth=3 over edges at MinEdgeConfidence — stated so a reader knows the
# window this answer covers.
_PRODUCER_MAX_DEPTH = 3
_PRODUCER_MIN_CONFIDENCE = 0.5


def _closure_stale(conn: Any, store: Any) -> bool | None:
    """Reuse ImportGraph's two-signal staleness probe. None = undeterminable."""
    if store is None:
        return None
    try:
        return not ImportGraph(store)._closure_is_fresh(conn)
    except Exception:
        return None


def _closure_side(
    conn: Any, node_id: int, direction: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """One direction of the closure: neighbours at their shortest depth.

    ``direction`` is "callers" (target_id = node) or "callees"
    (source_id = node). The inner select takes MIN(depth) per neighbour —
    the producer may store a row per (source,target,depth) triple, so the
    reported depth is the shortest one and its min_confidence the strongest
    at that depth.
    """
    if direction == "callers":
        # rows whose target is this symbol: sources that reach it
        side_col, anchor_col = "source_id", "target_id"
    else:
        # rows whose source is this symbol: targets it reaches
        side_col, anchor_col = "target_id", "source_id"
    sql = f"""
        SELECT c.{side_col} AS nid, c.depth AS depth, c.min_confidence AS mc
        FROM closure c
        JOIN (
            SELECT {side_col} AS k, MIN(depth) AS md
            FROM closure WHERE {anchor_col} = ? GROUP BY {side_col}
        ) m ON m.k = c.{side_col} AND m.md = c.depth
        WHERE c.{anchor_col} = ?
        ORDER BY depth, nid
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (node_id, node_id, limit + 1)).fetchall()
    except Exception as exc:
        log.debug("closure_read_failed", direction=direction, error=str(exc))
        return [], False
    truncated = len(rows) > limit
    rows = rows[:limit]

    out: list[dict[str, Any]] = []
    for row in rows:
        info: dict[str, Any] = {
            "id": row["nid"],
            "symbol": f"node#{row['nid']}",
            "file": "",
            "line": None,
        }
        try:
            nrow = conn.execute(
                "SELECT id, label, name, qualified_name, file_path, start_line, end_line, "
                "stable_id FROM nodes WHERE id = ?",
                (row["nid"],),
            ).fetchone()
            if nrow is not None:
                info = _graph_db._node_dict(nrow)
        except Exception:
            pass
        out.append(
            {
                "symbol": info["symbol"],
                "qualified_name": info.get("qualified_name"),
                "file": info["file"],
                "line": info["line"],
                "depth": row["depth"],
                "min_confidence": row["mc"],
            }
        )
    return out, truncated


def run_closure(
    conn: Any,
    symbol: str,
    *,
    store: Any = None,
    max_rows: int = _MAX_ROWS,
) -> dict[str, Any]:
    """Sync core for gt_closure."""
    empty: dict[str, Any] = {
        "status": "unavailable",
        "symbol": symbol,
        "callers": [],
        "callees": [],
        "truncated": False,
    }
    if conn is None or not _graph_db.has_tables(conn, "nodes", "edges"):
        return {**empty, "reason": "graph_tables_absent"}

    res = _graph_db.resolve_symbol(conn, symbol)
    if res["status"] != "ok":
        out: dict[str, Any] = {
            "status": res["status"],
            "symbol": symbol,
            "callers": [],
            "callees": [],
            "truncated": False,
        }
        if res["status"] == "ambiguous":
            out["candidates"] = res.get("candidates", [])
        return out
    node = res["node"]

    if not _graph_db.has_tables(conn, "closure"):
        return {
            **empty,
            "reason": "closure_table_absent",
            "resolved": node,
        }

    stale = _closure_stale(conn, store)
    callers, callers_trunc = _closure_side(conn, node["id"], "callers", max_rows)
    callees, callees_trunc = _closure_side(conn, node["id"], "callees", max_rows)

    return {
        "status": "ok",
        "symbol": node,
        "callers": callers,
        "callees": callees,
        "stale": stale,
        "bounds": {
            "max_depth": _PRODUCER_MAX_DEPTH,
            "min_confidence": _PRODUCER_MIN_CONFIDENCE,
        },
        "truncated": callers_trunc or callees_trunc,
    }


async def handle_gt_closure(
    symbol: str,
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    max_rows: int = _MAX_ROWS,
) -> dict[str, Any]:
    """Transitive callers/callees from the precomputed closure table."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace("gt_closure", symbol=symbol, input_summary=f"closure of {symbol}") as t:
        conn = _graph_db.store_connection(store)
        result = run_closure(conn, symbol, store=store, max_rows=max_rows)
        status = result.get("status", "unavailable")
        t.log_component(
            "closure_table",
            ComponentStatus.USED if status == "ok" else ComponentStatus.SKIPPED,
            output_summary=(
                f"{len(result.get('callers', []))} callers, "
                f"{len(result.get('callees', []))} callees"
            ),
            item_count=len(result.get("callers", [])) + len(result.get("callees", [])),
        )
        t.respond(
            response_type="closure",
            verdict=status.upper(),
            output_summary=f"{symbol}: {status}",
        )
        return result
