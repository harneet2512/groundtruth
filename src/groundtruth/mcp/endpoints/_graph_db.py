"""Shared read helpers for the gt_* composite endpoints.

These surfaces read the Go indexer's graph.db schema DIRECTLY — ``nodes``,
``edges`` and the derived sidecar tables (``closure``, ``processes`` /
``process_steps``, ``communities`` / ``community_members``,
``resolution_symbols``, ``assertions``). They are graph.db-only surfaces:
when the store is not backed by that schema, or when a derived table the
producer did not create on this graph is absent, consumers return typed
``unavailable`` / empty results rather than fabricating rows (the
certification-gated abstention convention).

Symbol resolution follows the ``references.py`` / ``impact.py`` convention —
a name that binds to no node is a typed ``not_found``, and a name that binds
to two or more distinct nodes is a typed ``ambiguous`` with its candidate
list, never a silent first-match.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints._graph_db")

# Labels that count as addressable symbols for resolution purposes.
_SYMBOL_LABELS = ("Function", "Method", "Class", "Interface", "Struct", "Enum", "File")

# Caps shared across the composite surfaces — every list is bounded so the
# `truncated` flag is meaningful (never an unbounded dump).
MAX_CANDIDATES = 10


def store_connection(store: Any) -> sqlite3.Connection | None:
    """Return the store's sqlite3 connection, or None when it doesn't expose one."""
    try:
        conn = getattr(store, "connection", None)
    except Exception:
        return None
    return conn if isinstance(conn, sqlite3.Connection) else None


def open_connection(db_path: str) -> sqlite3.Connection | None:
    """Open a graph.db read path for the composite (db_path-based) impls."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def has_tables(conn: sqlite3.Connection, *names: str) -> bool:
    """True iff every named table exists in the connected db."""
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error:
        return False
    present = {r[0] for r in rows}
    return all(n in present for n in names)


def _node_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "symbol": row["name"],
        "qualified_name": row["qualified_name"] if "qualified_name" in keys else None,
        "file": row["file_path"],
        "line": row["start_line"],
        "end_line": row["end_line"] if "end_line" in keys else None,
        "label": row["label"],
        "stable_id": row["stable_id"] if "stable_id" in keys else None,
    }


def resolve_symbol(
    conn: sqlite3.Connection, name: str, *, max_candidates: int = MAX_CANDIDATES
) -> dict[str, Any]:
    """Resolve a symbol name to a single node, with typed failure results.

    Returns one of:
      {"status": "ok",        "node": {...}}
      {"status": "not_found", "symbol": name}
      {"status": "ambiguous", "symbol": name, "candidates": [{...}, ...]}

    A name matches ``nodes.name`` or ``nodes.qualified_name`` (so ``Foo.bar``
    resolves the method). Two or more DISTINCT nodes is a typed ambiguity —
    picking the first row would be the silent-guess failure the endpoint
    conventions exist to prevent.
    """
    try:
        rows = conn.execute(
            "SELECT id, label, name, qualified_name, file_path, start_line, end_line, "
            "stable_id FROM nodes WHERE name = ? OR qualified_name = ? ORDER BY file_path, start_line, id",
            (name, name),
        ).fetchall()
    except sqlite3.Error:
        return {"status": "not_found", "symbol": name}

    seen: set[int] = set()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        nodes.append(_node_dict(row))

    if not nodes:
        return {"status": "not_found", "symbol": name}
    if len(nodes) > 1:
        return {
            "status": "ambiguous",
            "symbol": name,
            "candidates": nodes[:max_candidates],
            "candidates_truncated": len(nodes) > max_candidates,
        }
    return {"status": "ok", "node": nodes[0]}


def stable_ids_for_nodes(conn: sqlite3.Connection, node_ids: list[int]) -> dict[int, str]:
    """Map node ids to stable ids using the producer's two-source join.

    Mirrors ``gt-index/internal/process/process.go`` ``readStableIDs``:
    ``nodes.stable_id`` when stamped, else ``resolution_symbols.stable_id``
    joined on ``native_id = nodes.id``. When ``resolution_symbols`` is absent
    (older graph) the join degrades to the ``nodes.stable_id`` column only —
    never invented identifiers.
    """
    out: dict[int, str] = {}
    if not node_ids:
        return out
    placeholders = ",".join("?" for _ in node_ids)
    has_rs = has_tables(conn, "resolution_symbols")
    if has_rs:
        sql = (
            "SELECT n.id, COALESCE(NULLIF(n.stable_id, ''), rs.stable_id, '') AS sid "
            "FROM nodes n LEFT JOIN resolution_symbols rs "
            "ON CAST(rs.native_id AS INTEGER) = n.id "
            f"WHERE n.id IN ({placeholders})"
        )
    else:
        sql = (
            "SELECT n.id, COALESCE(NULLIF(n.stable_id, ''), '') AS sid "
            f"FROM nodes n WHERE n.id IN ({placeholders})"
        )
    try:
        for row in conn.execute(sql, tuple(node_ids)).fetchall():
            sid = row["sid"] if "sid" in row.keys() else row[1]
            if sid:
                out[int(row["id"])] = str(sid)
    except sqlite3.Error as exc:
        log.debug("stable_id_join_failed", error=str(exc))
    return out


def symbol_names_for_stable_ids(conn: sqlite3.Connection, stable_ids: list[str]) -> dict[str, str]:
    """Map stable ids back to display names (qualified_name preferred).

    Reads ``resolution_symbols`` only; when the table or the row is absent the
    caller keeps the raw stable id — a stable id is itself a real identifier,
    never a fabrication.
    """
    out: dict[str, str] = {}
    if not stable_ids or not has_tables(conn, "resolution_symbols"):
        return out
    placeholders = ",".join("?" for _ in stable_ids)
    try:
        rows = conn.execute(
            f"SELECT stable_id, qualified_name, path FROM resolution_symbols "
            f"WHERE stable_id IN ({placeholders})",
            tuple(stable_ids),
        ).fetchall()
        for row in rows:
            out[str(row["stable_id"])] = str(row["qualified_name"] or row["stable_id"])
    except sqlite3.Error as exc:
        log.debug("stable_id_name_lookup_failed", error=str(exc))
    return out


def read_source_line(root_path: str, file_path: str, line: int | None) -> str:
    """Read a single source line from disk (same contract as endpoints._read_line)."""
    if line is None or not root_path or not file_path:
        return ""
    full = os.path.join(root_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                if i == line:
                    return ln.rstrip()
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Route-decorator re-parse (producer-verbatim patterns)
#
# HANDLES_ROUTE edges deliberately store no route path: the edge records only
# WHICH function handles a route (source_id -> file anchor) and WHERE the
# decorator sits (source_line). The path and method are recovered by
# re-reading that one line — the same text the producer's regexes matched —
# so a route name is either the real decorator literal or the typed string
# "unknown", never a guess.
# ---------------------------------------------------------------------------

_ROUTE_LINE_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    # Python: @app.get("/path") / @router.post("/path") / @app.route("/path")
    (
        re.compile(
            r'^\s*@(?:app|router|api)\.(get|post|put|delete|patch|route)\s*\(\s*["\']([^"\']+)["\']'
        ),
        1,
        2,
    ),
    # Java/Kotlin: @GetMapping("/path") / @RequestMapping(value="/path")
    (
        re.compile(
            r'@(Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'
        ),
        1,
        2,
    ),
    # TS NestJS: @Get("/path") / @Post("/path")
    (re.compile(r'^\s*@(Get|Post|Put|Patch|Delete|Options|Head)\s*\(\s*["\']([^"\']+)["\']'), 1, 2),
    # JS/TS: app.get("/path") / router.post("/path")
    (
        re.compile(r'^\s*(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']'),
        1,
        2,
    ),
    # Go: r.HandleFunc("/path", h) / mux.Handle("/path", h) / r.GET("/path", h)
    (
        re.compile(
            r'^\s*[\w.]+\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE)\s*\(\s*["\']([^"\']+)["\']'
        ),
        1,
        2,
    ),
]

_VERB_MAP = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "options": "OPTIONS",
    "head": "HEAD",
    "getmapping": "GET",
    "postmapping": "POST",
    "putmapping": "PUT",
    "deletemapping": "DELETE",
    "patchmapping": "PATCH",
}


def normalize_route_path(raw: str) -> str:
    """Canonical route key — producer-verbatim normalizePath port.

    Strips scheme+host, query, fragment, and ONLY declared parameter segments
    (``{id}`` / ``:id`` / ``<id>``); concrete literals are kept (P2-10 — a
    literal path matches only the same literal path).
    """
    p = raw
    if "://" in p:
        rest = p.split("://", 1)[1]
        if "/" in rest:
            p = rest[rest.index("/") :]
        else:
            return "/"
    p = p.split("?", 1)[0].split("#", 1)[0]
    cleaned = [seg for seg in p.split("/") if seg and not seg.startswith(("{", ":", "<"))]
    return "/" + "/".join(cleaned) if cleaned else "/"


def parse_route_line(line_text: str) -> tuple[str, str | None] | None:
    """Parse a route decorator/registration line into (normalized_path, method).

    Returns None when no producer pattern matches — the caller renders the
    route name as the typed string ``"unknown"`` rather than guessing.
    """
    if not line_text:
        return None
    for pattern, verb_group, path_group in _ROUTE_LINE_PATTERNS:
        m = pattern.search(line_text)
        if m is None:
            continue
        verb_raw = m.group(verb_group)
        path = normalize_route_path(m.group(path_group))
        if path == "/":
            continue
        verb = _VERB_MAP.get(verb_raw.lower())
        # 'route'/'handle'/'handlefunc'/'requestmapping' carry no fixed verb.
        return (path, verb)
    return None


def api_call_metadata(raw: str | None) -> dict[str, str]:
    """Decode an API_CALL edge's metadata JSON: {route, method, framework, ...}.

    A missing or malformed payload reads as {} — never as a fabricated route.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
