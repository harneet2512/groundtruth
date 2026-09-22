"""Pre-submit diff impact: map a unified diff to downstream callers + flows.

Fires when the agent produces a diff (git diff / apply). For each symbol
whose body intersects a changed hunk, walk CALLS edges upstream to list
callers by distance band — the same "d=1 WILL BREAK" discipline as
GitNexus's impact tool — plus which detected flows traverse the change.

Read-only against graph.db: an unparseable diff or missing symbol returns a
clean, empty result — the gate degrades to silence, never to an error.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .processes import DetectedProcess, build_process_index

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


@dataclass(frozen=True, slots=True)
class ChangedSymbol:
    node_id: int
    name: str
    file_path: str
    start_line: int


@dataclass(frozen=True, slots=True)
class CallerDetail:
    """One upstream caller, keyed by node identity (never by bare name).

    ``trust_tier``/``confidence``/``resolution_method`` are those of the
    strongest CALLS edge that reached the caller at its hop distance."""

    node_id: int
    name: str
    qualified_name: str
    file_path: str
    line: int
    trust_tier: str
    confidence: float
    resolution_method: str

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.line}"


@dataclass(slots=True)
class DiffImpactResult:
    changed_files: list[str] = field(default_factory=list)
    changed_symbols: list[ChangedSymbol] = field(default_factory=list)
    # depth -> [(name, file:line)], one entry per distinct caller node
    callers_by_depth: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    # depth -> identity-keyed callers with edge provenance
    caller_details_by_depth: dict[int, list[CallerDetail]] = field(default_factory=dict)
    affected_flows: list[DetectedProcess] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changed_symbols


def parse_diff_files_and_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """file -> [(hunk_start, hunk_len)] from unified diff '+' ranges."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current: str | None = None
    for line in diff_text.splitlines():
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        h = _HUNK_RE.match(line)
        if h and current:
            start = int(h.group(1))
            length = int(h.group(2) or 1)
            out[current].append((start, length))
    return dict(out)


def _symbols_in_hunks(
    connection: sqlite3.Connection,
    file_path: str,
    hunks: list[tuple[int, int]],
) -> list[ChangedSymbol]:
    symbols: list[ChangedSymbol] = []
    rows = connection.execute(
        "SELECT id, name, file_path, start_line, end_line FROM nodes"
        " WHERE file_path = ? AND label IN ('Function','Method')",
        (file_path,),
    ).fetchall()
    for node_id, name, fp, start, end in rows:
        start, end = int(start or 0), int(end or 0)
        for hunk_start, hunk_len in hunks:
            hunk_end = hunk_start + max(hunk_len - 1, 0)
            if start <= hunk_end and end >= hunk_start:
                symbols.append(
                    ChangedSymbol(
                        node_id=int(node_id), name=str(name),
                        file_path=str(fp), start_line=start,
                    )
                )
                break
    return symbols


_TIER_RANK = {"CERTIFIED": 0, "CANDIDATE": 1, "SPECULATIVE": 2}


def _upstream_callers(
    connection: sqlite3.Connection,
    seed_ids: Iterable[int],
    *,
    max_depth: int,
) -> dict[int, list[CallerDetail]]:
    """Level-synchronous BFS upstream over CALLS edges, banded by hop distance.

    Callers are keyed by node id: two different functions that share a name
    (``run`` in two files) are two callers.  Keying by bare name collapsed
    them into one and understated the blast radius.  Each caller keeps the
    strongest edge (tier, then confidence) that reached it at its distance.
    """
    node_cols = {str(r[1]) for r in connection.execute("PRAGMA table_info(nodes)")}
    edge_cols = {str(r[1]) for r in connection.execute("PRAGMA table_info(edges)")}
    qualified = "COALESCE(n.qualified_name,'')" if "qualified_name" in node_cols else "''"
    tier = "COALESCE(e.trust_tier,'')" if "trust_tier" in edge_cols else "''"
    conf = "COALESCE(e.confidence,0.0)" if "confidence" in edge_cols else "0.0"
    method = "COALESCE(e.resolution_method,'')" if "resolution_method" in edge_cols else "''"
    sql = (
        f"SELECT e.source_id, n.name, {qualified}, n.file_path,"
        f" COALESCE(n.start_line,0), {tier}, {conf}, {method}"
        " FROM edges e JOIN nodes n ON n.id = e.source_id"
        " WHERE e.target_id = ? AND e.type='CALLS'"
    )
    bands: dict[int, list[CallerDetail]] = {}
    visited = set(seed_ids)
    frontier = sorted(visited)
    for depth in range(1, max_depth + 1):
        best: dict[int, CallerDetail] = {}
        for node_id in frontier:
            for src, name, qname, fp, line, etier, econf, emethod in connection.execute(
                sql, (node_id,)
            ):
                src = int(src)
                if src in visited:
                    continue
                detail = CallerDetail(
                    node_id=src,
                    name=str(name or ""),
                    qualified_name=str(qname or ""),
                    file_path=str(fp or ""),
                    line=int(line or 0),
                    trust_tier=str(etier or "").upper(),
                    confidence=float(econf or 0.0),
                    resolution_method=str(emethod or ""),
                )
                prior = best.get(src)
                if prior is None or (
                    _TIER_RANK.get(detail.trust_tier, 3), -detail.confidence
                ) < (_TIER_RANK.get(prior.trust_tier, 3), -prior.confidence):
                    best[src] = detail
        if not best:
            break
        visited.update(best)
        bands[depth] = sorted(
            best.values(), key=lambda d: (d.name, d.file_path, d.line, d.node_id)
        )
        frontier = sorted(best)
    return bands


def impact_for_ranges(
    graph_db: str | Path,
    ranges: dict[str, list[tuple[int, int]]],
    *,
    processes: Iterable[DetectedProcess] = (),
    max_depth: int = 3,
    max_flows: int = 5,
) -> DiffImpactResult:
    """Map explicit per-file ``(start, length)`` line ranges — expressed in
    the graph's own line numbering — to changed symbols, callers and flows."""
    result = DiffImpactResult()
    if not ranges:
        return result
    result.changed_files = sorted(ranges)
    process_index = build_process_index(processes)
    try:
        with sqlite3.connect(str(graph_db)) as connection:
            for fp in sorted(ranges):
                result.changed_symbols.extend(
                    _symbols_in_hunks(connection, fp, ranges[fp])
                )
            if not result.changed_symbols:
                return result
            details = _upstream_callers(
                connection, (s.node_id for s in result.changed_symbols),
                max_depth=max_depth,
            )
    except sqlite3.Error:
        return result
    result.caller_details_by_depth = details
    result.callers_by_depth = {
        d: [(c.name, c.location) for c in rows] for d, rows in details.items()
    }
    affected: list[DetectedProcess] = []
    seen_procs: set[str] = set()
    for sym in result.changed_symbols:
        for proc in process_index.get(sym.node_id, ()):
            if proc.process_id not in seen_procs:
                seen_procs.add(proc.process_id)
                affected.append(proc)
    result.affected_flows = affected[:max_flows]
    return result


def diff_impact(
    graph_db: str | Path,
    diff_text: str,
    *,
    processes: Iterable[DetectedProcess] = (),
    max_depth: int = 3,
    max_flows: int = 5,
) -> DiffImpactResult:
    """Map a unified diff to the callers and flows it can break.

    Hunks are read on the post-image (``+``) side, i.e. this assumes the
    graph indexes the post-edit tree.  Callers holding a pre-edit graph and
    both file versions should compute pre-image ranges and use
    :func:`impact_for_ranges` (``patch_impact`` does)."""
    files = parse_diff_files_and_hunks(diff_text or "")
    return impact_for_ranges(
        graph_db, files, processes=processes, max_depth=max_depth, max_flows=max_flows
    )


def render_impact_block(result: DiffImpactResult) -> str:
    """Render the pre-submit impact block; '' when nothing is affected."""
    if result.empty:
        return ""
    lines = ["Changed symbols:"]
    for s in result.changed_symbols[:10]:
        lines.append(f"  {s.name} ({s.file_path}:{s.start_line})")
    for depth, callers in result.callers_by_depth.items():
        sev = "WILL BREAK" if depth == 1 else f"at depth {depth}"
        lines.append(f"  Callers {sev}:")
        for name, loc in callers[:8]:
            lines.append(f"    {name} ({loc})")
    if result.affected_flows:
        lines.append("  Affected flows:")
        for p in result.affected_flows:
            lines.append(f"    {p.label} ({p.step_count} steps)")
    return "<gt-impact>\n" + "\n".join(lines) + "\n</gt-impact>"


__all__ = [
    "CallerDetail",
    "ChangedSymbol",
    "DiffImpactResult",
    "diff_impact",
    "impact_for_ranges",
    "parse_diff_files_and_hunks",
    "render_impact_block",
]
