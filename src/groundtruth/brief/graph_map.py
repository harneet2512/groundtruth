"""Graph-map brief generator — core module.

Produces a graph neighborhood map for L1 injection. Not a ranked file list.
Each entry includes: file, callers, callees, contracts, tests, risks, next move.

Shared between OH adapter and MCP product face.
Uses graph.db for structural data. No LLM. Deterministic.
"""
from __future__ import annotations

import os
import sqlite3

from dataclasses import dataclass, field
from typing import Any


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class GraphMapEntry:
    """One file in the graph map with its structural neighborhood."""
    file: str
    score: float = 0.0
    functions: list[dict[str, str]] = field(default_factory=list)
    callers: list[dict[str, str]] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_move: str = ""


@dataclass
class GraphMapBrief:
    """Complete graph-map brief for a task."""
    entries: list[GraphMapEntry] = field(default_factory=list)
    interpretation: str = ""
    suggested_first_move: str = ""

    def render(self, max_chars: int = 2000) -> str:
        parts = ["<gt-task-brief>"]
        if self.interpretation:
            parts.append(f"## Task: {self.interpretation}")
        parts.append("")
        for i, e in enumerate(self.entries, 1):
            parts.append(f"{i}. {e.file}")
            if e.functions:
                funcs = ", ".join(f"{f['name']}({f.get('sig','')})" for f in e.functions[:3])
                parts.append(f"   Functions: {funcs}")
            if e.callers:
                caller_strs = []
                for c in e.callers[:3]:
                    if c.get("func"):
                        # High confidence: show function name (verified)
                        caller_strs.append(f"{c['func']}() in {os.path.basename(c['file'])}")
                    else:
                        # Low confidence fallback: file path only
                        caller_strs.append(f"{c['file']}:{c.get('line','')}")
                parts.append(f"   Called by: {' | '.join(caller_strs)}")
            if e.callees:
                parts.append(f"   Calls: {', '.join(e.callees[:5])}")
            if e.contracts:
                parts.append(f"   Contract: {'; '.join(e.contracts[:2])}")
            if e.tests:
                parts.append(f"   Tests: {', '.join(e.tests[:3])}")
            if e.risks:
                parts.append(f"   Risk: {'; '.join(e.risks[:2])}")
            parts.append("")
        if self.suggested_first_move:
            parts.append(f"Start: {self.suggested_first_move}")
        parts.append("</gt-task-brief>")
        rendered = "\n".join(parts)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars - 20] + "\n</gt-task-brief>"
        return rendered


def build_graph_map(
    ranked_files: list[dict[str, Any]],
    graph_db_path: str,
    repo_root: str = "",
    *,
    max_entries: int = 5,
    max_callers: int = 3,
    max_callees: int = 5,
) -> GraphMapBrief:
    """Build a graph-map brief from ranked files and graph.db.

    Args:
        ranked_files: list of {"file": path, "score": float}
        graph_db_path: path to graph.db
        repo_root: optional repo root for reading source
        max_entries: max files in the map
        max_callers: max callers per file
        max_callees: max callees per file
    """
    if not os.path.exists(graph_db_path):
        return GraphMapBrief()

    conn = sqlite3.connect(graph_db_path)
    entries: list[GraphMapEntry] = []

    for rf in ranked_files[:max_entries]:
        fpath = rf.get("file", "")
        entry = GraphMapEntry(file=fpath, score=rf.get("score", 0.0))

        _esc_fpath = "%" + _escape_like(fpath.lstrip("/"))
        try:
            funcs = conn.execute(
                "SELECT name, signature FROM nodes "
                "WHERE file_path LIKE ? ESCAPE '\\' AND label IN ('Function','Method') AND is_test = 0 "
                "ORDER BY start_line LIMIT 5",
                (_esc_fpath,),
            ).fetchall()
            entry.functions = [{"name": n, "sig": s or ""} for n, s in funcs]
        except Exception:
            pass

        try:
            # Confidence-tiered caller rendering:
            # ≥0.9: show function names (graph earned trust)
            # 0.7-0.9: show file paths only (no structural claims)
            # <0.7: silence (filtered out)
            hi_callers = conn.execute(
                "SELECT DISTINCT nsrc.file_path, nsrc.name, e.source_line, e.confidence "
                "FROM nodes nt "
                "JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' "
                "  AND COALESCE(e.confidence, 0.5) >= 0.9 "
                "JOIN nodes nsrc ON e.source_id = nsrc.id "
                "WHERE nt.file_path LIKE ? ESCAPE '\\' AND nsrc.file_path != nt.file_path "
                "LIMIT ?",
                (_esc_fpath, max_callers),
            ).fetchall()
            if hi_callers:
                entry.callers = [
                    {"file": c[0], "func": c[1], "line": str(c[2] or "")}
                    for c in hi_callers
                ]
            else:
                # Fallback: file paths only at 0.7 threshold
                lo_callers = conn.execute(
                    "SELECT DISTINCT nsrc.file_path, e.source_line "
                    "FROM nodes nt "
                    "JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' "
                    "  AND COALESCE(e.confidence, 0.5) >= 0.7 "
                    "JOIN nodes nsrc ON e.source_id = nsrc.id "
                    "WHERE nt.file_path LIKE ? ESCAPE '\\' AND nsrc.file_path != nt.file_path "
                    "LIMIT ?",
                    (_esc_fpath, max_callers),
                ).fetchall()
                entry.callers = [{"file": c[0], "line": str(c[1] or "")} for c in lo_callers]
        except Exception:
            pass

        try:
            # Bug 10 fix: raise callee confidence to 0.7 to filter name_match
            # false positives (e.g. conftest.py -> _colorama.py)
            callees = conn.execute(
                "SELECT DISTINCT nt.file_path "
                "FROM nodes ns "
                "JOIN edges e ON e.source_id = ns.id AND e.type = 'CALLS' "
                "  AND COALESCE(e.confidence, 0.5) >= 0.7 "
                "JOIN nodes nt ON e.target_id = nt.id "
                "WHERE ns.file_path LIKE ? ESCAPE '\\' AND nt.file_path != ns.file_path "
                "LIMIT ?",
                (_esc_fpath, max_callees),
            ).fetchall()
            entry.callees = [c[0] for c in callees]
        except Exception:
            pass

        try:
            sigs = conn.execute(
                "SELECT name, signature, return_type FROM nodes "
                "WHERE file_path LIKE ? ESCAPE '\\' AND label IN ('Function','Method') "
                "AND signature IS NOT NULL AND signature != '' "
                "LIMIT 3",
                (_esc_fpath,),
            ).fetchall()
            entry.contracts = [
                f"{n}({s}) -> {r}" if r else f"{n}({s})"
                for n, s, r in sigs if s
            ]
        except Exception:
            pass

        if entry.callers:
            entry.risks.append(f"{len(entry.callers)}+ callers — changes here propagate")

        entries.append(entry)

    conn.close()

    brief = GraphMapBrief(entries=entries)
    if entries:
        brief.suggested_first_move = f"Read {entries[0].file} first"

    return brief
