"""Post-search graph enrichment: append symbol/flow context to grep output.

Closes the coverage gap between bootstrap and post_view: an agent grepping
for a concept gets callers, callees, and flow membership for the symbols
its pattern matches — before it has opened any file. This is the push-side
equivalent of GitNexus's grep augmentation; same trigger, richer payload
(flow membership + provenance tiers, not just neighbor names).

Read-only against graph.db: a miss returns an empty string, never an error.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

from .processes import DetectedProcess, build_process_index

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# grep patterns under this length after normalization are too vague to enrich.
MIN_PATTERN_LENGTH = 3


_REGEX_ESCAPE_RE = re.compile(r"\\.")


def extract_search_terms(pattern: str) -> list[str]:
    """Identifiers in a grep pattern worth looking up in the symbol index."""
    terms: list[str] = []
    stripped = _REGEX_ESCAPE_RE.sub(" ", pattern or "")
    for tok in _IDENTIFIER_RE.findall(stripped):
        if len(tok) >= MIN_PATTERN_LENGTH and tok not in terms:
            terms.append(tok)
    return terms


def _match_symbols(
    connection: sqlite3.Connection,
    terms: Iterable[str],
    *,
    limit: int,
) -> list[sqlite3.Row]:
    """FTS symbol lookup. Falls back to LIKE if FTS is absent or errors."""
    rows: list[sqlite3.Row] = []
    seen: set[int] = set()
    for term in terms:
        try:
            hits = connection.execute(
                "SELECT n.id, n.name, n.label, n.file_path, n.start_line"
                " FROM nodes_fts f JOIN nodes n ON n.id = f.rowid"
                " WHERE nodes_fts MATCH ?"
                " ORDER BY rank LIMIT ?",
                (term, limit),
            ).fetchall()
        except sqlite3.Error:
            hits = connection.execute(
                "SELECT id, name, label, file_path, start_line FROM nodes"
                " WHERE name LIKE ? LIMIT ?",
                (f"%{term}%", limit),
            ).fetchall()
        for row in hits:
            if row[0] not in seen:
                seen.add(row[0])
                rows.append(row)
    return rows


def _neighbors(
    connection: sqlite3.Connection,
    node_id: int,
    direction: str,
    *,
    limit: int,
) -> list[tuple[str, str, str]]:
    """CALLS neighbors: (name, file:line, trust_tier). Certified first."""
    if direction == "callers":
        sql = (
            "SELECT n.name, n.file_path, n.start_line, e.trust_tier"
            " FROM edges e JOIN nodes n ON n.id = e.source_id"
            " WHERE e.target_id = ? AND e.type='CALLS'"
        )
    else:
        sql = (
            "SELECT n.name, n.file_path, n.start_line, e.trust_tier"
            " FROM edges e JOIN nodes n ON n.id = e.target_id"
            " WHERE e.source_id = ? AND e.type='CALLS'"
        )
    rows = connection.execute(sql, (node_id,)).fetchall()
    rows.sort(key=lambda r: (0 if (r[3] or "").upper() == "CERTIFIED" else 1, r[0]))
    return [(r[0], f"{r[1]}:{r[2]}", r[3] or "") for r in rows[:limit]]


def build_search_context(
    graph_db: str | Path,
    pattern: str,
    *,
    processes: Iterable[DetectedProcess] = (),
    max_symbols: int = 5,
    per_symbol_neighbors: int = 3,
    max_flow_refs: int = 2,
) -> str:
    """Render a <gt-search-context> block for a grep pattern.

    Returns "" when the pattern has no indexable terms or nothing matches —
    callers must treat empty as "attach nothing".
    """
    terms = extract_search_terms(pattern)
    if not terms:
        return ""
    process_index = build_process_index(processes)
    # ``with sqlite3.connect(...)`` is a TRANSACTION context, not a close — the
    # handle would leak until GC and hold the db file open (fatal for tempdir
    # cleanup on Windows). Explicit try/finally close; read-only queries only.
    connection = None
    try:
        connection = sqlite3.connect(str(graph_db))
        symbols = _match_symbols(connection, terms, limit=max_symbols)
        if not symbols:
            return ""
        lines: list[str] = []
        for node_id, name, label, file_path, line in symbols[:max_symbols]:
            loc = f"{file_path}:{line}"
            callers = _neighbors(connection, node_id, "callers", limit=per_symbol_neighbors)
            callees = _neighbors(connection, node_id, "callees", limit=per_symbol_neighbors)
            parts = [f"{name} ({label}, {loc})"]
            if callers:
                parts.append("called by: " + ", ".join(f"{n} ({l})" for n, l, _ in callers))
            if callees:
                parts.append("calls: " + ", ".join(f"{n} ({l})" for n, l, _ in callees))
            flows = process_index.get(node_id, ())[:max_flow_refs]
            if flows:
                parts.append("in flow: " + "; ".join(p.label for p in flows))
            lines.append("  " + "\n    ".join(parts))
    except sqlite3.Error:
        return ""
    finally:
        if connection is not None:
            connection.close()
    if not lines:
        return ""
    return "<gt-search-context>\n" + "\n".join(lines) + "\n</gt-search-context>"


__all__ = [
    "MIN_PATTERN_LENGTH",
    "build_search_context",
    "extract_search_terms",
]
