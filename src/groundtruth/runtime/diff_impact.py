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
from collections import defaultdict, deque
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


@dataclass(slots=True)
class DiffImpactResult:
    changed_files: list[str] = field(default_factory=list)
    changed_symbols: list[ChangedSymbol] = field(default_factory=list)
    # depth -> set of (name, file:line)
    callers_by_depth: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
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


def _upstream_callers(
    connection: sqlite3.Connection,
    seed_ids: Iterable[int],
    *,
    max_depth: int,
) -> dict[int, list[tuple[str, str]]]:
    """BFS upstream over CALLS edges, banded by hop distance."""
    bands: dict[int, dict[str, tuple[str, str]]] = defaultdict(dict)
    seeds = list(seed_ids)
    visited = set(seeds)
    frontier = deque((nid, 0) for nid in seeds)
    while frontier:
        node_id, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for src, name, fp, line in connection.execute(
            "SELECT e.source_id, n.name, n.file_path, n.start_line"
            " FROM edges e JOIN nodes n ON n.id = e.source_id"
            " WHERE e.target_id = ? AND e.type='CALLS'",
            (node_id,),
        ):
            loc = f"{fp}:{line}"
            if src not in visited:
                visited.add(src)
                bands[depth + 1][str(name)] = (str(name), loc)
                frontier.append((src, depth + 1))
    return {
        d: sorted(b.values(), key=lambda t: t[0]) for d, b in sorted(bands.items())
    }


def diff_impact(
    graph_db: str | Path,
    diff_text: str,
    *,
    processes: Iterable[DetectedProcess] = (),
    max_depth: int = 3,
    max_flows: int = 5,
) -> DiffImpactResult:
    """Map a unified diff to the callers and flows it can break."""
    result = DiffImpactResult()
    files = parse_diff_files_and_hunks(diff_text or "")
    if not files:
        return result
    result.changed_files = sorted(files)
    process_index = build_process_index(processes)
    try:
        with sqlite3.connect(str(graph_db)) as connection:
            for fp, hunks in files.items():
                result.changed_symbols.extend(
                    _symbols_in_hunks(connection, fp, hunks)
                )
            if not result.changed_symbols:
                return result
            result.callers_by_depth = _upstream_callers(
                connection, (s.node_id for s in result.changed_symbols),
                max_depth=max_depth,
            )
    except sqlite3.Error:
        return result
    affected: list[DetectedProcess] = []
    seen_procs: set[str] = set()
    for sym in result.changed_symbols:
        for proc in process_index.get(sym.node_id, ()):
            if proc.process_id not in seen_procs:
                seen_procs.add(proc.process_id)
                affected.append(proc)
    result.affected_flows = affected[:max_flows]
    return result


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
    "ChangedSymbol",
    "DiffImpactResult",
    "diff_impact",
    "parse_diff_files_and_hunks",
    "render_impact_block",
]
