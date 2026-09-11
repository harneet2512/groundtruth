"""gt_community — the already-computed community decomposition.

Question: "What are the cohesive regions of this codebase?"

Reads the producer's ``communities`` / ``community_members`` tables verbatim:

  * ``label`` — the community's name (LLM-enriched label, or the heuristic
    label when enrichment did not run).
  * ``cohesion`` — measured edge-internal ratio. The producer deliberately
    stores SQL NULL when cohesion is unmeasurable (e.g. zero internal weight,
    NaN) — this surface preserves that NULL as ``null``/``None`` and reports
    ``cohesion_reason``; a missing value is never rendered as 0.0.
  * ``member_count`` / ``community_members`` — members joined per community,
    ordered and capped deterministically.

Abstention: absent ``communities`` table → typed ``unavailable`` (the
community pass is opt-in / may not have run on this graph).
"""

from __future__ import annotations

import json
from typing import Any

from groundtruth.mcp.endpoints import _graph_db
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints.community")

_MAX_COMMUNITIES = 20
_TOP_MEMBERS = 5


def _top_members(conn: Any, community_id: str, limit: int) -> list[str]:
    """Deterministic member preview — producer orders members ASC."""
    if not _graph_db.has_tables(conn, "community_members"):
        return []
    try:
        rows = conn.execute(
            "SELECT member FROM community_members WHERE community_id = ? ORDER BY member LIMIT ?",
            (community_id, limit),
        ).fetchall()
    except Exception as exc:
        log.debug("community_members_read_failed", community=community_id, error=str(exc))
        return []
    return [row["member"] for row in rows]


def run_community(
    conn: Any,
    *,
    name: str | None = None,
    member: str | None = None,
    max_communities: int = _MAX_COMMUNITIES,
    top_members: int = _TOP_MEMBERS,
) -> dict[str, Any]:
    """Sync core for gt_community."""
    if conn is None or not _graph_db.has_tables(conn, "communities"):
        return {
            "status": "unavailable",
            "reason": ("graph_tables_absent" if conn is None else "communities_table_absent"),
            "communities": [],
            "truncated": False,
        }

    if member is not None and not _graph_db.has_tables(conn, "community_members"):
        # A member filter that cannot be applied must not silently widen to
        # "all communities" — that would fabricate membership the graph
        # cannot prove.
        return {
            "status": "unavailable",
            "reason": "community_members_table_absent",
            "communities": [],
            "truncated": False,
        }

    sql = "SELECT * FROM communities"
    params: list[Any] = []
    clauses: list[str] = []
    if name is not None:
        clauses.append("(label = ? OR heuristic_label = ? OR label LIKE ?)")
        params.extend([name, name, f"%{name}%"])
    if member is not None:
        clauses.append("id IN (SELECT community_id FROM community_members WHERE member = ?)")
        params.append(member)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY member_count DESC, id ASC LIMIT ?"
    params.append(max_communities + 1)

    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as exc:
        log.debug("communities_read_failed", error=str(exc))
        return {
            "status": "unavailable",
            "reason": "communities_read_failed",
            "communities": [],
            "truncated": False,
        }

    truncated = len(rows) > max_communities
    rows = rows[:max_communities]

    communities: list[dict[str, Any]] = []
    for row in rows:
        try:
            keywords = json.loads(row["keywords"]) if row["keywords"] else []
            if not isinstance(keywords, list):
                keywords = []
        except (json.JSONDecodeError, TypeError):
            keywords = []
        communities.append(
            {
                "id": row["id"],
                "name": row["label"] or row["heuristic_label"],
                "heuristic_label": row["heuristic_label"],
                "cohesion": row["cohesion"],  # NULL stays None — never 0.0
                "cohesion_reason": row["cohesion_reason"],
                "member_count": row["member_count"],
                "top_members": _top_members(conn, row["id"], top_members),
                "keywords": keywords,
                "description": row["description"],
            }
        )

    return {
        "status": "ok",
        "communities": communities,
        "truncated": truncated,
    }


async def handle_gt_community(
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    name: str | None = None,
    member: str | None = None,
    limit: int = _MAX_COMMUNITIES,
) -> dict[str, Any]:
    """The codebase's community decomposition (cohesive regions + members)."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace("gt_community", input_summary="community decomposition") as t:
        conn = _graph_db.store_connection(store)
        result = run_community(conn, name=name, member=member, max_communities=limit)
        status = result.get("status", "unavailable")
        t.log_component(
            "communities_table",
            ComponentStatus.USED if status == "ok" else ComponentStatus.SKIPPED,
            output_summary=f"{len(result.get('communities', []))} communities",
            item_count=len(result.get("communities", [])),
        )
        t.respond(
            response_type="community",
            verdict=status.upper(),
            output_summary=f"{len(result.get('communities', []))} communit(y/ies)",
        )
        return result
