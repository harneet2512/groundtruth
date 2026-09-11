"""gt_trace — directed path between two symbols over the call graph.

Question: "Is there a path from this symbol to that one, and through what?"
When: when the agent needs to know whether/how two symbols connect.

Traversal: bounded BFS from ``from_symbol`` forward along ``edges`` of type
``CALLS`` (and ``HAS_METHOD`` — an edge type the producer does not mint today;
containment is recovered from ``nodes.parent_id`` instead, which carries the
same class→method relation, labelled ``HAS_METHOD`` at confidence 1.0, with
the inverse method→class hop labelled ``MEMBER_OF``). Both containment
directions are real stored data — nothing is inferred.

Resolution: ``from`` and ``to`` resolve independently through the shared
typed resolver — ``not_found`` / ``ambiguous`` (with candidates) are returned
per endpoint, never a silent first-match.

Output shape:
    {"status": "ok"|"no_path"|"not_found"|"ambiguous"|"unavailable",
     "path": [{"symbol", "file", "line", "relation", "confidence"}, ...],
     "truncated": bool}
"""

from __future__ import annotations

from collections import deque
from typing import Any

from groundtruth.mcp.endpoints import _graph_db
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints.trace_path")

_MAX_DEPTH = 6
_MAX_EXPANSIONS = 500

# Edge types traversed forward. HAS_METHOD is listed even though the producer
# emits none today: if it ever lands in `edges`, the surface picks it up with
# no code change. The live containment hop comes from nodes.parent_id below.
_EDGE_TYPES = ("CALLS", "HAS_METHOD")


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "path": [],
        "truncated": False,
    }


def _resolution_failure(endpoint: str, res: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": res["status"],
        "endpoint": endpoint,
        "symbol": res["symbol"],
        "path": [],
        "truncated": False,
    }
    if res["status"] == "ambiguous":
        out["candidates"] = res.get("candidates", [])
        out["candidates_truncated"] = res.get("candidates_truncated", False)
    return out


def _neighbors(conn: Any, node_id: int) -> list[tuple[int, str, float]]:
    """Forward adjacency for one node: (target_id, relation, confidence).

    Edge hops carry the stored edge confidence. Containment hops from
    ``nodes.parent_id`` carry 1.0 — the column is a structural fact, not a
    probabilistic resolution.
    """
    out: list[tuple[int, str, float]] = []
    try:
        rows = conn.execute(
            "SELECT target_id, type, COALESCE(confidence, 0.0) AS conf "
            "FROM edges WHERE source_id = ? AND type IN ('CALLS','HAS_METHOD') "
            "ORDER BY target_id, id",
            (node_id,),
        ).fetchall()
        for row in rows:
            if row["target_id"] is not None and row["target_id"] != node_id:
                out.append((int(row["target_id"]), str(row["type"]), float(row["conf"])))
    except Exception as exc:
        log.debug("trace_edges_read_failed", node=node_id, error=str(exc))

    try:
        # class -> method containment (the producer's parent_id relation).
        for row in conn.execute(
            "SELECT id FROM nodes WHERE parent_id = ? ORDER BY start_line, id",
            (node_id,),
        ).fetchall():
            out.append((int(row["id"]), "HAS_METHOD", 1.0))
        # method -> owning class (inverse containment).
        for row in conn.execute(
            "SELECT parent_id FROM nodes WHERE id = ? AND parent_id IS NOT NULL",
            (node_id,),
        ).fetchall():
            if row["parent_id"]:
                out.append((int(row["parent_id"]), "MEMBER_OF", 1.0))
    except Exception as exc:
        log.debug("trace_containment_read_failed", node=node_id, error=str(exc))
    return out


def run_trace(
    conn: Any,
    from_symbol: str,
    to_symbol: str,
    *,
    max_depth: int = _MAX_DEPTH,
    max_expansions: int = _MAX_EXPANSIONS,
) -> dict[str, Any]:
    """Sync core: directed BFS path between two resolved nodes."""
    if conn is None or not _graph_db.has_tables(conn, "nodes", "edges"):
        return _unavailable("graph_tables_absent")

    from_res = _graph_db.resolve_symbol(conn, from_symbol)
    if from_res["status"] != "ok":
        return _resolution_failure("from", from_res)
    to_res = _graph_db.resolve_symbol(conn, to_symbol)
    if to_res["status"] != "ok":
        return _resolution_failure("to", to_res)

    src = from_res["node"]
    dst = to_res["node"]

    def _step(
        node: dict[str, Any], relation: str | None, confidence: float | None
    ) -> dict[str, Any]:
        return {
            "symbol": node["symbol"],
            "file": node["file"],
            "line": node["line"],
            "relation": relation,
            "confidence": confidence,
        }

    if src["id"] == dst["id"]:
        return {
            "status": "ok",
            "from": src,
            "to": dst,
            "path": [_step(src, None, None)],
            "hops": 0,
            "truncated": False,
        }

    # Bounded BFS: prev[node] = (predecessor, relation, confidence).
    prev: dict[int, tuple[int, str, float]] = {}
    depth_of: dict[int, int] = {src["id"]: 0}
    visited = {src["id"]}
    frontier: deque[int] = deque([src["id"]])
    truncated = False
    expansions = 0
    node_cache: dict[int, dict[str, Any]] = {src["id"]: src, dst["id"]: dst}

    def _node(node_id: int) -> dict[str, Any]:
        if node_id in node_cache:
            return node_cache[node_id]
        row = conn.execute(
            "SELECT id, label, name, qualified_name, file_path, start_line, end_line, "
            "stable_id FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        info = (
            _graph_db._node_dict(row)
            if row is not None
            else {
                "id": node_id,
                "symbol": f"node#{node_id}",
                "qualified_name": None,
                "file": "",
                "line": None,
                "end_line": None,
                "label": "unknown",
                "stable_id": None,
            }
        )
        node_cache[node_id] = info
        return info

    found = False
    depth_limited = False
    while frontier and not found:
        if expansions >= max_expansions:
            truncated = True
            break
        cur = frontier.popleft()
        cur_depth = depth_of[cur]
        if cur_depth >= max_depth:
            # Bound cut exploration here: if this node still has unvisited
            # neighbours, a "no_path" verdict is bound-limited, not proven —
            # flag it truncated rather than claiming exhaustion.
            if any(n not in visited for n, _r, _c in _neighbors(conn, cur)):
                depth_limited = True
            continue
        expansions += 1
        for nxt, relation, conf in _neighbors(conn, cur):
            if nxt in visited:
                continue
            visited.add(nxt)
            prev[nxt] = (cur, relation, conf)
            depth_of[nxt] = cur_depth + 1
            if nxt == dst["id"]:
                found = True
                break
            frontier.append(nxt)

    if not found:
        return {
            "status": "no_path",
            "from": src,
            "to": dst,
            "path": [],
            "explored": len(visited),
            "truncated": truncated or depth_limited,
        }

    # Reconstruct path back to front.
    ids = [dst["id"]]
    while ids[-1] != src["id"]:
        ids.append(prev[ids[-1]][0])
    ids.reverse()

    path = [_step(src, None, None)]
    for i in range(1, len(ids)):
        _, relation, conf = prev[ids[i]]
        path.append(_step(_node(ids[i]), relation, conf))

    return {
        "status": "ok",
        "from": src,
        "to": dst,
        "path": path,
        "hops": len(path) - 1,
        "truncated": False,
    }


async def handle_gt_trace(
    from_symbol: str,
    to_symbol: str,
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    max_depth: int = _MAX_DEPTH,
    max_expansions: int = _MAX_EXPANSIONS,
) -> dict[str, Any]:
    """Directed path between two symbols over the call graph."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "gt_trace",
        symbol=from_symbol,
        input_summary=f"path {from_symbol} -> {to_symbol}",
    ) as t:
        conn = _graph_db.store_connection(store)
        result = run_trace(
            conn,
            from_symbol,
            to_symbol,
            max_depth=max_depth,
            max_expansions=max_expansions,
        )
        status = result.get("status", "unavailable")
        t.log_component(
            "call_graph_bfs",
            ComponentStatus.USED if status == "ok" else ComponentStatus.SKIPPED,
            output_summary=f"{status}: {result.get('hops', 0)} hops",
            item_count=len(result.get("path", [])),
        )
        t.respond(
            response_type="trace_path",
            verdict=status.upper(),
            output_summary=(f"{from_symbol} -> {to_symbol}: {status}"),
        )
        return result
