"""gt_route_map / gt_api_impact — the service-boundary surface.

Question: "What routes does this repo serve, who calls them, and what does
each handler do next?"

Sources (all producer-emitted, never inferred):

  * ``HANDLES_ROUTE`` edges — function node → the file's anchor node, with
    ``source_line`` pointing at the decorator/registration line. The edge
    stores no path by design; the route path + method are recovered by
    re-reading that one line through the producer's own patterns
    (``_graph_db.parse_route_line``). An unreadable/unparseable line renders
    as the typed name ``"unknown"``.
  * ``API_CALL`` edges — client-call file anchor → route file anchor, with
    ``metadata`` carrying ``route``/``method``/``framework`` verbatim. A route
    known only through API_CALL metadata (a handler the relationship pass did
    not mint a HANDLES_ROUTE for) surfaces with ``discovered_via="api_call"``
    and ``handler=None``.
  * ``middleware`` is always ``[]`` — the producer stores no middleware
    facts, so the field is emitted empty rather than guessed.
  * ``flows`` — the handler's direct outgoing CALLS targets (what the route
    invokes next), capped.

gt_api_impact adds the consumer-key analysis: per consumer node, the count of
DISTINCT routes it calls. A consumer fetching ≥2 routes is a multi-fetch
consumer and carries ``attributionNote`` — its traffic cannot be exclusively
attributed to any single route.
"""

from __future__ import annotations

from typing import Any

from groundtruth.mcp.endpoints import _graph_db
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints.route_map")

_MAX_ROUTES = 50
_MAX_FLOWS = 5
_MAX_CONSUMERS = 25

_UNKNOWN_ROUTE = "unknown"


def _outgoing_calls(conn: Any, node_id: int, limit: int) -> list[dict[str, Any]]:
    """A handler's direct outgoing CALLS targets — the downstream flow."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT n.name, n.file_path, e.confidence "
            "FROM edges e JOIN nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? AND e.type = 'CALLS' "
            "ORDER BY n.name, n.file_path LIMIT ?",
            (node_id, limit),
        ).fetchall()
    except Exception as exc:
        log.debug("flow_read_failed", node=node_id, error=str(exc))
        return []
    return [
        {"symbol": row["name"], "file": row["file_path"], "confidence": row["confidence"]}
        for row in rows
    ]


def _collect_routes(
    conn: Any, root_path: str, *, max_routes: int = _MAX_ROUTES
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Collect route records + raw API_CALL rows.

    Returns (routes, api_edges, truncated). Each route record carries
    ``file_node`` (the file-anchor node id that API_CALL edges target) for
    consumer attribution, and ``handler_node`` for flow computation.
    """
    routes: list[dict[str, Any]] = []

    handles_rows: list[Any] = []
    api_rows: list[Any] = []
    try:
        handles_rows = conn.execute(
            "SELECT e.id, e.source_id, e.target_id, e.source_line, e.source_file, "
            "e.confidence, n.name AS handler_name, n.file_path AS handler_file, "
            "n.start_line AS handler_line "
            "FROM edges e JOIN nodes n ON n.id = e.source_id "
            "WHERE e.type = 'HANDLES_ROUTE' "
            "ORDER BY e.source_file, e.source_line, e.id",
        ).fetchall()
    except Exception as exc:
        log.debug("handles_route_read_failed", error=str(exc))
    try:
        api_rows = conn.execute(
            "SELECT e.id, e.source_id, e.target_id, e.source_line, e.source_file, "
            "e.confidence, e.metadata FROM edges e WHERE e.type = 'API_CALL' "
            "ORDER BY e.source_file, e.source_line, e.id",
        ).fetchall()
    except Exception as exc:
        log.debug("api_call_read_failed", error=str(exc))

    for row in handles_rows:
        # The decorator line holds the route path — re-parse it through the
        # producer's own patterns. Unreadable/absent -> typed "unknown".
        line_text = _graph_db.read_source_line(
            root_path, row["source_file"] or row["handler_file"], row["source_line"]
        )
        parsed = _graph_db.parse_route_line(line_text)
        name, method = parsed if parsed is not None else (_UNKNOWN_ROUTE, None)
        routes.append(
            {
                "name": name,
                "method": method,
                "handler": row["handler_name"],
                "handler_file": row["handler_file"],
                "handler_line": row["handler_line"],
                "middleware": [],
                "confidence": row["confidence"],
                "discovered_via": "handles_route",
                "file_node": row["target_id"],
                "handler_node": row["source_id"],
                "consumers": [],
                "flows": [],
            }
        )

    # Routes the producer never minted a HANDLES_ROUTE for but that API_CALL
    # metadata proves are called (e.g. non-Python handlers): surfaced by the
    # call side alone, handler unknown.
    covered_keys = {(r["file_node"], r["name"]) for r in routes}
    for row in api_rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        route_name = meta.get("route")
        if not route_name:
            continue
        key = (row["target_id"], route_name)
        if key in covered_keys:
            continue
        covered_keys.add(key)
        handler_file = ""
        try:
            frow = conn.execute(
                "SELECT file_path FROM nodes WHERE id = ?", (row["target_id"],)
            ).fetchone()
            handler_file = frow["file_path"] if frow else ""
        except Exception:
            pass
        routes.append(
            {
                "name": route_name,
                "method": meta.get("method") or None,
                "handler": None,
                "handler_file": handler_file,
                "handler_line": None,
                "middleware": [],
                "confidence": None,
                "discovered_via": "api_call",
                "file_node": row["target_id"],
                "handler_node": None,
                "consumers": [],
                "flows": [],
            }
        )

    # Attach consumers. Route-level attribution only when the call's metadata
    # route equals the route's parsed name. When the route name is "unknown"
    # (decorator line unreadable) or the call carries no route key, the file
    # anchor is all the graph proves — attach with attribution "file_level",
    # never silently upgrade a file-granularity hit to a route-granularity one.
    for row in api_rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        meta_route = meta.get("route")
        for route in routes:
            if route["file_node"] != row["target_id"]:
                continue
            route_level = (
                bool(meta_route)
                and route["name"] not in (_UNKNOWN_ROUTE,)
                and meta_route == route["name"]
            )
            if meta_route and route["name"] != _UNKNOWN_ROUTE and meta_route != route["name"]:
                continue  # a different known route in the same file
            route["consumers"].append(
                {
                    "file": row["source_file"],
                    "line": row["source_line"],
                    "route": meta_route,
                    "method": meta.get("method") or None,
                    "confidence": row["confidence"],
                    "attribution": "route_level" if route_level else "file_level",
                }
            )

    truncated = len(routes) > max_routes
    routes = routes[:max_routes]

    for route in routes:
        if route["handler_node"] is not None:
            route["flows"] = _outgoing_calls(conn, route["handler_node"], _MAX_FLOWS)
        route["consumers"] = route["consumers"][:_MAX_CONSUMERS]
        route.pop("file_node", None)
        route.pop("handler_node", None)

    routes.sort(key=lambda r: (r["name"], r["handler_file"]))
    return routes, api_rows, truncated


def run_route_map(
    conn: Any,
    root_path: str,
    *,
    max_routes: int = _MAX_ROUTES,
) -> dict[str, Any]:
    """Sync core for gt_route_map."""
    if conn is None or not _graph_db.has_tables(conn, "nodes", "edges"):
        return {
            "status": "unavailable",
            "reason": "graph_tables_absent",
            "routes": [],
            "truncated": False,
        }
    routes, _api_rows, truncated = _collect_routes(conn, root_path, max_routes=max_routes)
    return {"status": "ok", "routes": routes, "truncated": truncated}


def run_api_impact(
    conn: Any,
    root_path: str,
    *,
    route: str | None = None,
    handler: str | None = None,
    max_routes: int = _MAX_ROUTES,
) -> dict[str, Any]:
    """Sync core for gt_api_impact — routes plus consumer-key analysis."""
    if conn is None or not _graph_db.has_tables(conn, "nodes", "edges"):
        return {
            "status": "unavailable",
            "reason": "graph_tables_absent",
            "routes": [],
            "truncated": False,
        }

    routes, api_rows, truncated = _collect_routes(conn, root_path, max_routes=max_routes)

    # Consumer key analysis: how many DISTINCT routes each consumer node calls.
    # A consumer fetching several routes cannot have its behavior attributed
    # to any single one — the attributionNote says so on the consumer row.
    routes_per_consumer: dict[Any, set[str]] = {}
    for row in api_rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        routes_per_consumer.setdefault(row["source_id"], set())
        if meta.get("route"):
            routes_per_consumer[row["source_id"]].add(meta["route"])
    count_by_file_line = {
        (row["source_file"], row["source_line"]): len(routes_per_consumer.get(row["source_id"], ()))
        for row in api_rows
    }

    # Optional filters: by normalized route name and/or handler symbol.
    if route is not None:
        wanted = _graph_db.normalize_route_path(route)
        routes = [r for r in routes if r["name"] == wanted]
    if handler is not None:
        routes = [r for r in routes if r["handler"] == handler]
    if (route is not None or handler is not None) and not routes:
        return {
            "status": "not_found",
            "route": route,
            "handler": handler,
            "routes": [],
            "truncated": False,
        }

    out_routes: list[dict[str, Any]] = []
    for r in routes:
        consumers: list[dict[str, Any]] = []
        for c in r["consumers"]:
            entry = dict(c)
            n = count_by_file_line.get((c["file"], c["line"]), 0)
            entry["routes_called"] = n
            if n > 1:
                entry["attributionNote"] = (
                    f"multi-fetch consumer: calls {n} distinct routes — "
                    "impact is shared, not exclusive to this route"
                )
            consumers.append(entry)
        affected_files = sorted({c["file"] for c in consumers if c.get("file")})
        out_routes.append(
            {
                **r,
                "consumers": consumers,
                "consumer_count": len(consumers),
                "affected_files": affected_files,
            }
        )

    return {
        "status": "ok",
        "route": route,
        "handler": handler,
        "routes": out_routes,
        "truncated": truncated,
    }


async def handle_gt_route_map(
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
) -> dict[str, Any]:
    """All detected routes with handlers, consumers, and downstream flows."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace("gt_route_map", input_summary="route map") as t:
        conn = _graph_db.store_connection(store)
        result = run_route_map(conn, root_path)
        status = result.get("status", "unavailable")
        t.log_component(
            "route_edges",
            ComponentStatus.USED if status == "ok" else ComponentStatus.SKIPPED,
            output_summary=f"{len(result.get('routes', []))} routes",
            item_count=len(result.get("routes", [])),
        )
        t.respond(
            response_type="route_map",
            verdict=status.upper(),
            output_summary=f"{len(result.get('routes', []))} route(s)",
        )
        return result


async def handle_gt_api_impact(
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    route: str | None = None,
    handler: str | None = None,
) -> dict[str, Any]:
    """Consumer-key impact analysis for API routes."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "gt_api_impact",
        input_summary=f"api impact route={route} handler={handler}",
    ) as t:
        conn = _graph_db.store_connection(store)
        result = run_api_impact(conn, root_path, route=route, handler=handler)
        status = result.get("status", "unavailable")
        t.log_component(
            "api_call_edges",
            ComponentStatus.USED if status == "ok" else ComponentStatus.SKIPPED,
            output_summary=f"{len(result.get('routes', []))} routes",
            item_count=len(result.get("routes", [])),
        )
        t.respond(
            response_type="api_impact",
            verdict=status.upper(),
            output_summary=f"{len(result.get('routes', []))} route(s) analyzed",
        )
        return result
