"""gt_route_map / gt_api_impact — the service-boundary surface.

Question: "What routes does this repo serve, who calls them, and what does
each handler do next?"

Sources (all producer-emitted, never inferred):

  * ``HANDLES_ROUTE`` edges — function node → the file's anchor node, with
    ``source_line`` pointing at the decorator/registration line. Non-Python
    producers persist ``{"route","method",...}`` edge metadata, which is read
    first (``route_source="edge_metadata"``); edges without it (Python
    decorators) fall back to re-reading that one line through the producer's
    own patterns (``_graph_db.parse_route_line``, ``route_source=
    "source_line"``). An unreadable/unparseable line renders as the typed
    name ``"unknown"``.
  * ``API_CALL`` edges — client-call file anchor → route file anchor, with
    ``metadata`` carrying ``route``/``method``/``framework`` verbatim. A route
    known only through API_CALL metadata (a handler the relationship pass did
    not mint a HANDLES_ROUTE for) surfaces with ``discovered_via="api_call"``
    and ``handler=None``.
  * ``middleware`` — ``MIDDLEWARE_ON`` edges (middleware node → the same
    file/app anchor the route edges target), scoped by the registration's
    route prefix and, for Express ``use``, by registration order. Middleware
    wired onto another anchor (Nest module class, Spring configurer) is
    reported under ``unattached_middleware`` rather than guessed onto routes.
  * ``injections`` — ``INJECTS`` bindings on the handler or its enclosing
    class (DI provider, declared type, ambiguity flag).
  * ``flows`` — the handler's direct outgoing CALLS targets (what the route
    invokes next), capped; ``flows_truncated`` / ``consumers_truncated``
    say when a cap dropped rows.

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


def _edge_columns(conn: Any) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    except Exception:
        return set()


def _col(cols: set[str], name: str, default: str = "NULL") -> str:
    return f"e.{name}" if name in cols else f"{default}"


def _outgoing_calls(conn: Any, node_id: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """A handler's direct outgoing CALLS targets — the downstream flow.

    Returns ``(flows, truncated)``: one extra row is read so a cap that
    dropped callees is reported, never hidden."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT n.name, n.file_path, e.confidence "
            "FROM edges e JOIN nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? AND e.type = 'CALLS' "
            "ORDER BY n.name, n.file_path LIMIT ?",
            (node_id, limit + 1),
        ).fetchall()
    except Exception as exc:
        log.debug("flow_read_failed", node=node_id, error=str(exc))
        return [], False
    flows = [
        {"symbol": row["name"], "file": row["file_path"], "confidence": row["confidence"]}
        for row in rows[:limit]
    ]
    return flows, len(rows) > limit


def _route_from_metadata(raw: str | None) -> tuple[str, str | None] | None:
    """(normalized path, method) from HANDLES_ROUTE edge metadata, if any.

    Non-Python producers persist ``{"route","method","framework",...}`` on
    the edge; that is the producer's own reading of the registration and
    does not depend on the current bytes of the source line."""
    meta = _graph_db.api_call_metadata(raw)
    route = meta.get("route")
    if not isinstance(route, str) or not route.strip():
        return None
    method = meta.get("method")
    method = method.strip().upper() if isinstance(method, str) and method.strip() else None
    return _graph_db.normalize_route_path(route.strip()), method


def _middleware_rows(conn: Any, cols: set[str]) -> list[dict[str, Any]]:
    tier, method, meta = (
        _col(cols, "trust_tier", "''"),
        _col(cols, "resolution_method", "''"),
        _col(cols, "metadata"),
    )
    try:
        rows = conn.execute(
            "SELECT e.source_id, e.target_id, e.source_line, e.source_file, e.confidence, "
            f"{tier} AS tier, {method} AS method, {meta} AS metadata, "
            "n.name AS name, n.file_path AS file_path "
            "FROM edges e JOIN nodes n ON n.id = e.source_id "
            "WHERE e.type = 'MIDDLEWARE_ON' ORDER BY e.source_file, e.source_line, e.id"
        ).fetchall()
    except Exception as exc:
        log.debug("middleware_read_failed", error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        scope = meta.get("route")
        mechanism = meta.get("mechanism") or row["method"] or ""
        out.append(
            {
                "target": row["target_id"],
                "name": row["name"],
                "file": row["file_path"],
                "line": row["source_line"],
                "registered_in": row["source_file"],
                "mechanism": mechanism,
                "route_scope": _graph_db.normalize_route_path(scope) if isinstance(scope, str) and scope else None,
                "confidence": row["confidence"],
                "trust_tier": row["tier"] or "",
            }
        )
    return out


def _middleware_applies(mw: dict[str, Any], route: dict[str, Any]) -> bool:
    if mw["target"] != route["file_node"]:
        return False
    scope = mw["route_scope"]
    name = route["name"]
    if scope and scope != "/" and name != _UNKNOWN_ROUTE:
        if not (name == scope or name.startswith(scope.rstrip("/") + "/")):
            return False
    # Express-style ``app.use`` applies to routes registered after it in the
    # same file; a registration line is known only for HANDLES_ROUTE rows.
    reg_line = route.get("registration_line")
    if (
        mw["mechanism"] == "express_use"
        and reg_line
        and mw["registered_in"] == route.get("registration_file")
        and (mw["line"] or 0) > reg_line
    ):
        return False
    return True


def _injections_for(conn: Any, cols: set[str], handler_node: int) -> list[dict[str, Any]]:
    """INJECTS bindings on the handler itself or on its enclosing class."""
    tier, meta = _col(cols, "trust_tier", "''"), _col(cols, "metadata")
    try:
        rows = conn.execute(
            "SELECT n.name AS provider, n.file_path AS provider_file, e.confidence, "
            f"{tier} AS tier, {meta} AS metadata, e.source_id "
            "FROM edges e JOIN nodes n ON n.id = e.target_id "
            "WHERE e.type = 'INJECTS' AND (e.source_id = ? OR e.source_id = "
            "(SELECT parent_id FROM nodes WHERE id = ?)) "
            "ORDER BY n.name, n.file_path",
            (handler_node, handler_node),
        ).fetchall()
    except Exception as exc:
        log.debug("injects_read_failed", node=handler_node, error=str(exc))
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        out.append(
            {
                "provider": row["provider"],
                "provider_file": row["provider_file"],
                "bound_on": "handler" if row["source_id"] == handler_node else "class",
                "mechanism": meta.get("mechanism") or None,
                "declared_type": meta.get("declared_type") or None,
                "ambiguous": bool(meta.get("ambiguous")),
                "confidence": row["confidence"],
                "trust_tier": row["tier"] or "",
            }
        )
    return out


def _collect_routes(
    conn: Any, root_path: str, *, max_routes: int = _MAX_ROUTES
) -> tuple[list[dict[str, Any]], list[Any], bool, list[dict[str, Any]]]:
    """Collect route records + raw API_CALL rows.

    Returns (routes, api_edges, truncated, unattached_middleware). Each
    route record carries
    ``file_node`` (the file-anchor node id that API_CALL edges target) for
    consumer attribution, and ``handler_node`` for flow computation.
    """
    routes: list[dict[str, Any]] = []
    cols = _edge_columns(conn)
    meta_expr = _col(cols, "metadata")

    handles_rows: list[Any] = []
    api_rows: list[Any] = []
    try:
        handles_rows = conn.execute(
            "SELECT e.id, e.source_id, e.target_id, e.source_line, e.source_file, "
            f"e.confidence, {meta_expr} AS metadata, "
            "n.name AS handler_name, n.file_path AS handler_file, "
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
            f"e.confidence, {meta_expr} AS metadata FROM edges e WHERE e.type = 'API_CALL' "
            "ORDER BY e.source_file, e.source_line, e.id",
        ).fetchall()
    except Exception as exc:
        log.debug("api_call_read_failed", error=str(exc))

    for row in handles_rows:
        # Edge metadata first (the producer's own reading); the decorator
        # line re-parse is the fallback for edges that carry none (Python
        # decorator edges). Unreadable/absent -> typed "unknown".
        parsed = _route_from_metadata(row["metadata"])
        route_source = "edge_metadata"
        if parsed is None:
            line_text = _graph_db.read_source_line(
                root_path, row["source_file"] or row["handler_file"], row["source_line"]
            )
            parsed = _graph_db.parse_route_line(line_text)
            route_source = "source_line"
        if parsed is None:
            route_source = "unknown"
        name, method = parsed if parsed is not None else (_UNKNOWN_ROUTE, None)
        routes.append(
            {
                "name": name,
                "method": method,
                "handler": row["handler_name"],
                "handler_file": row["handler_file"],
                "handler_line": row["handler_line"],
                "route_source": route_source,
                "middleware": [],
                "injections": [],
                "confidence": row["confidence"],
                "discovered_via": "handles_route",
                "file_node": row["target_id"],
                "handler_node": row["source_id"],
                "registration_file": row["source_file"],
                "registration_line": row["source_line"],
                "consumers": [],
                "flows": [],
                "flows_truncated": False,
                "consumers_truncated": False,
            }
        )

    # Routes the producer never minted a HANDLES_ROUTE for but that API_CALL
    # metadata proves are called (e.g. inline-arrow handlers): surfaced by the
    # call side alone, handler unknown.
    covered_keys = {(r["file_node"], r["name"]) for r in routes}
    for row in api_rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        raw_route = meta.get("route")
        if not raw_route:
            continue
        route_name = _graph_db.normalize_route_path(str(raw_route))
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
                "route_source": "api_call_metadata",
                "middleware": [],
                "injections": [],
                "confidence": None,
                "discovered_via": "api_call",
                "file_node": row["target_id"],
                "handler_node": None,
                "registration_file": None,
                "registration_line": None,
                "consumers": [],
                "flows": [],
                "flows_truncated": False,
                "consumers_truncated": False,
            }
        )

    # Attach consumers. Route-level attribution only when the call's metadata
    # route equals the route's parsed name. When the route name is "unknown"
    # (decorator line unreadable) or the call carries no route key, the file
    # anchor is all the graph proves — attach with attribution "file_level",
    # never silently upgrade a file-granularity hit to a route-granularity one.
    for row in api_rows:
        meta = _graph_db.api_call_metadata(row["metadata"])
        raw_route = meta.get("route")
        meta_route = _graph_db.normalize_route_path(str(raw_route)) if raw_route else None
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
                    "route": raw_route,
                    "method": meta.get("method") or None,
                    "confidence": row["confidence"],
                    "attribution": "route_level" if route_level else "file_level",
                }
            )

    # Middleware: MIDDLEWARE_ON edges target the same file/app anchor the
    # route edges use; scope by the registration's route prefix and (for
    # Express ``use``) by registration order.
    middleware = _middleware_rows(conn, cols)
    attached: set[int] = set()
    for route in routes:
        for index, mw in enumerate(middleware):
            if _middleware_applies(mw, route):
                attached.add(index)
                route["middleware"].append(
                    {k: v for k, v in mw.items() if k not in ("target", "registered_in")}
                )

    truncated = len(routes) > max_routes
    routes = routes[:max_routes]

    for route in routes:
        if route["handler_node"] is not None:
            route["flows"], route["flows_truncated"] = _outgoing_calls(
                conn, route["handler_node"], _MAX_FLOWS
            )
            route["injections"] = _injections_for(conn, cols, route["handler_node"])
        if len(route["consumers"]) > _MAX_CONSUMERS:
            route["consumers_truncated"] = True
        route["consumers"] = route["consumers"][:_MAX_CONSUMERS]
        for key in ("file_node", "handler_node", "registration_file", "registration_line"):
            route.pop(key, None)

    routes.sort(key=lambda r: (r["name"], r["handler_file"] or ""))
    unattached = [
        {k: v for k, v in mw.items() if k not in ("target",)}
        for index, mw in enumerate(middleware)
        if index not in attached
    ]
    return routes, api_rows, truncated, unattached


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
    routes, _api_rows, truncated, unattached = _collect_routes(
        conn, root_path, max_routes=max_routes
    )
    return {
        "status": "ok",
        "routes": routes,
        "truncated": truncated,
        "unattached_middleware": unattached,
    }


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

    routes, api_rows, truncated, unattached = _collect_routes(
        conn, root_path, max_routes=max_routes
    )

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
        "unattached_middleware": unattached,
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
