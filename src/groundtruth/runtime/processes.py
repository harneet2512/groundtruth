"""Repository-level execution-flow ("process") detection over graph.db.

Detects named end-to-end flows by walking CALLS edges forward from entry
points (symbols with no internal callers). This is the repo-level analog of
``RepositoryContextEngine._execution_views``: that machinery produces
anchor-scoped views for the *current* context; this module enumerates the
flow library of the whole repository at index/query time so flows can be
searched, ranked, and attached without an anchor.

Provenance discipline (house rule, differs from GitNexus): every step keeps
its edge trust tier. A flow is rendered as a *lower bound* when it contains
non-CERTIFIED hops — matching the existing "Execution (lower bound; ...)"
render contract — never presented as fully certified when it is not.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Edge trust tiers as stored in edges.trust_tier by the indexer.
TIER_RANK = {"CERTIFIED": 0, "CANDIDATE": 1, "SPECULATIVE": 2, "": 3}

_ENTRY_SYMBOL_NAMES = {"main", "Main", "init", "cli", "run"}
_ENTRY_LABELS = {"Function", "Method"}


@dataclass(frozen=True, slots=True)
class ProcessNode:
    """One graph node participating in a flow step."""

    node_id: int
    name: str
    label: str
    file_path: str
    start_line: int
    signature: str
    is_test: bool

    @property
    def rendered(self) -> str:
        loc = f":{self.start_line}" if self.start_line else ""
        return f"{self.name} ({self.file_path}{loc})"


@dataclass(frozen=True, slots=True)
class ProcessStepEdge:
    """A CALLS hop with its provenance tier."""

    source_id: int
    target_id: int
    trust_tier: str
    confidence: float
    resolution_method: str


@dataclass(frozen=True, slots=True)
class DetectedProcess:
    """One entry-to-terminal execution trace."""

    process_id: str
    label: str
    entry: ProcessNode
    terminal: ProcessNode
    nodes: tuple[ProcessNode, ...]
    edges: tuple[ProcessStepEdge, ...]
    entry_kind: str  # "declared_main" | "exported" | "no_callers"
    witnessed: bool = False  # persisted by the Go process layer (test-assertion backed)
    witness_test: str = ""  # stable_id of the test that witnessed the flow

    @property
    def step_count(self) -> int:
        return len(self.edges)

    @property
    def certified_ratio(self) -> float:
        if not self.edges:
            return 0.0
        certified = sum(1 for e in self.edges if e.trust_tier == "CERTIFIED")
        return certified / len(self.edges)

    @property
    def rendered(self) -> str:
        chain = " -> ".join(n.name for n in self.nodes)
        mark = "" if self.certified_ratio >= 1.0 else " (lower bound)"
        return f"{self.label}: {chain}{mark}"


@dataclass(slots=True)
class ProcessTruncationStats:
    """What the detection ceilings dropped, so a partial answer cannot
    present itself as a complete one."""

    entry_candidates_dropped: int = 0
    walks_cut_by_budget: int = 0
    traces_depth_capped: int = 0
    callees_dropped: int = 0
    processes_dropped: int = 0

    @property
    def truncated(self) -> bool:
        return any(
            (
                self.entry_candidates_dropped,
                self.walks_cut_by_budget,
                self.traces_depth_capped,
                self.callees_dropped,
                self.processes_dropped,
            )
        )


@dataclass(slots=True)
class ProcessDetectionResult:
    processes: list[DetectedProcess]
    stats: ProcessTruncationStats = field(default_factory=ProcessTruncationStats)
    node_count: int = 0
    edge_count: int = 0


def _load_graph(
    connection: sqlite3.Connection,
) -> tuple[dict[int, ProcessNode], dict[int, list[tuple[ProcessNode, ProcessStepEdge]]], int]:
    """Load nodes and forward CALLS adjacency from graph.db."""
    nodes: dict[int, ProcessNode] = {}
    for row in connection.execute(
        "SELECT id, name, COALESCE(label,''), COALESCE(file_path,''),"
        " COALESCE(start_line,0), COALESCE(signature,''),"
        " COALESCE(is_test,0) FROM nodes"
    ):
        node_id, name, label, file_path, start_line, signature, is_test = row
        nodes[int(node_id)] = ProcessNode(
            node_id=int(node_id),
            name=str(name or ""),
            label=str(label or ""),
            file_path=str(file_path or ""),
            start_line=int(start_line or 0),
            signature=str(signature or ""),
            is_test=bool(is_test),
        )

    adjacency: dict[int, list[tuple[ProcessNode, ProcessStepEdge]]] = defaultdict(list)
    edge_count = 0
    edge_cols = {r[1] for r in connection.execute("PRAGMA table_info(edges)")}
    method_expr = "COALESCE(resolution_method,'')" if "resolution_method" in edge_cols else "''"
    tier_expr = "COALESCE(trust_tier,'')" if "trust_tier" in edge_cols else "''"
    for row in connection.execute(
        f"SELECT source_id, target_id, {tier_expr},"
        f" COALESCE(confidence,0.0), {method_expr}"
        " FROM edges WHERE type='CALLS'"
    ):
        source_id, target_id, tier, confidence, method = row
        source_id, target_id = int(source_id), int(target_id)
        if source_id not in nodes or target_id not in nodes:
            continue
        edge = ProcessStepEdge(
            source_id=source_id,
            target_id=target_id,
            trust_tier=str(tier or "").upper(),
            confidence=float(confidence or 0.0),
            resolution_method=str(method or ""),
        )
        adjacency[source_id].append((nodes[target_id], edge))
        edge_count += 1

    for rows in adjacency.values():
        rows.sort(
            key=lambda item: (
                TIER_RANK.get(item[1].trust_tier, 3),
                -item[1].confidence,
                item[0].file_path,
                item[0].name,
            )
        )
    return nodes, adjacency, edge_count


def _stable_id_to_nodes(
    connection: sqlite3.Connection,
) -> dict[str, ProcessNode]:
    """Map stable ids to ProcessNodes via the producer's two-source join.

    Mirrors ``gt-index/internal/process`` ``readStableIDs``: ``nodes.stable_id``
    when stamped, else ``resolution_symbols.stable_id`` joined on
    ``native_id = nodes.id``. Both halves are optional; absent pieces degrade
    to whichever source exists — never invented identifiers.
    """
    node_cols = {r[1] for r in connection.execute("PRAGMA table_info(nodes)")}
    has_rs = bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolution_symbols'"
        ).fetchone()
    )
    n_sid = "n.stable_id" if "stable_id" in node_cols else "''"
    if has_rs:
        sql = (
            "SELECT n.id, n.name, COALESCE(n.label,''), COALESCE(n.file_path,''),"
            " COALESCE(n.start_line,0), COALESCE(n.signature,''),"
            " COALESCE(n.is_test,0),"
            f" COALESCE(NULLIF({n_sid},''), rs.stable_id, '') AS sid"
            " FROM nodes n LEFT JOIN resolution_symbols rs"
            " ON CAST(rs.native_id AS INTEGER) = n.id"
        )
    else:
        sql = (
            "SELECT n.id, n.name, COALESCE(n.label,''), COALESCE(n.file_path,''),"
            " COALESCE(n.start_line,0), COALESCE(n.signature,''),"
            " COALESCE(n.is_test,0),"
            f" COALESCE(NULLIF({n_sid},''), '') AS sid FROM nodes n"
        )
    out: dict[str, ProcessNode] = {}
    for row in connection.execute(sql):
        node_id, name, label, file_path, start_line, signature, is_test, sid = row
        if not sid:
            continue
        out[str(sid)] = ProcessNode(
            node_id=int(node_id),
            name=str(name or ""),
            label=str(label or ""),
            file_path=str(file_path or ""),
            start_line=int(start_line or 0),
            signature=str(signature or ""),
            is_test=bool(is_test),
        )
    return out


def _load_persisted_processes(
    connection: sqlite3.Connection,
    adjacency: dict[int, list[tuple[ProcessNode, ProcessStepEdge]]],
    *,
    max_processes: int,
) -> list[DetectedProcess]:
    """Load test-witnessed flows persisted by the Go process layer.

    The producer's ``processes``/``process_steps`` tables carry flows that were
    witnessed by a resolved assertion — stronger provenance than the heuristic
    walk. Steps are stable ids; per-hop edge provenance is re-joined from the
    CALLS adjacency (a hop with no matching CALLS edge is marked tierless so it
    can never claim a certification it does not have).
    """
    tables = {
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('processes','process_steps')"
        )
    }
    if tables != {"processes", "process_steps"}:
        return []
    proc_rows = connection.execute(
        "SELECT id, entry_stable_id, terminal_stable_id, witness_assertion_id,"
        " test_stable_id, kind, depth, trust_floor FROM processes ORDER BY id"
    ).fetchall()
    if not proc_rows:
        return []

    sid_map = _stable_id_to_nodes(connection)
    edge_lookup: dict[tuple[int, int], ProcessStepEdge] = {}
    for source_id, rows in adjacency.items():
        for target, edge in rows:
            edge_lookup.setdefault((source_id, target.node_id), edge)
    incoming: dict[int, int] = defaultdict(int)
    for source_id, target_id in edge_lookup:
        incoming[target_id] += 1

    steps_by_proc: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        "SELECT process_id, stable_id FROM process_steps ORDER BY process_id, ordinal"
    ):
        steps_by_proc[str(row[0])].append(str(row[1]))

    out: list[DetectedProcess] = []
    for row in proc_rows[:max_processes]:
        process_id = str(row[0])
        step_sids = steps_by_proc.get(process_id) or []
        step_nodes = [sid_map[sid] for sid in step_sids if sid in sid_map]
        if len(step_nodes) < 2:
            continue
        edges: list[ProcessStepEdge] = []
        for prev, cur in zip(step_nodes, step_nodes[1:]):
            edges.append(
                edge_lookup.get(
                    (prev.node_id, cur.node_id),
                    ProcessStepEdge(
                        source_id=prev.node_id,
                        target_id=cur.node_id,
                        trust_tier="",
                        confidence=0.0,
                        resolution_method="persisted_no_calls_edge",
                    ),
                )
            )
        entry = step_nodes[0]
        out.append(
            DetectedProcess(
                process_id=process_id,
                label=f"{entry.name} -> {step_nodes[-1].name}",
                entry=entry,
                terminal=step_nodes[-1],
                nodes=tuple(step_nodes),
                edges=tuple(edges),
                entry_kind=_entry_kind(entry, incoming.get(entry.node_id, 0), False),
                witnessed=True,
                witness_test=str(row[4] or ""),
            )
        )
    return out


def _entry_kind(node: ProcessNode, in_degree: int, exported: bool) -> str:
    if node.name in _ENTRY_SYMBOL_NAMES:
        return "declared_main"
    if in_degree == 0:
        return "no_callers"
    return "exported"


def find_entry_points(
    nodes: dict[int, ProcessNode],
    adjacency: dict[int, list[tuple[ProcessNode, ProcessStepEdge]]],
    *,
    max_candidates: int = 200,
) -> tuple[list[tuple[ProcessNode, str]], int]:
    """Rank flow entry points: no internal callers first, tests excluded.

    Returns (ranked entries, candidates_dropped).
    """
    incoming: dict[int, int] = defaultdict(int)
    for source_id, rows in adjacency.items():
        for target, _edge in rows:
            incoming[target.node_id] += 1

    exported_ids: set[int] = set()
    candidates: list[tuple[int, ProcessNode, str]] = []
    for node in nodes.values():
        if node.is_test or node.label not in _ENTRY_LABELS:
            continue
        deg = incoming.get(node.node_id, 0)
        if deg == 0:
            kind = _entry_kind(node, deg, node.node_id in exported_ids)
            # Score: prefer declared mains, then no-caller roots; longer
            # reachable subtree proxy = own out-degree.
            rank = (
                0 if kind == "declared_main" else 1,
                -len(adjacency.get(node.node_id, ())),
                node.file_path,
                node.name,
            )
            candidates.append((0 if kind == "declared_main" else 1, node, kind))

    candidates.sort(key=lambda item: (item[0], item[1].file_path, item[1].name))
    kept = [(node, kind) for _rank, node, kind in candidates[:max_candidates]]
    dropped = max(0, len(candidates) - max_candidates)
    return kept, dropped


def detect_processes(
    graph_db: str | Path,
    *,
    max_depth: int = 10,
    max_branching: int = 4,
    max_processes: int = 75,
    min_steps: int = 3,
    max_entry_point_candidates: int = 200,
) -> ProcessDetectionResult:
    """Detect named entry-to-terminal execution flows in graph.db.

    Deterministic: identical graph input produces identical process output.
    """
    stats = ProcessTruncationStats()
    # ``with sqlite3.connect(...)`` is a TRANSACTION context (commit/rollback),
    # not a close — the handle would leak until GC and hold the db file open
    # (fatal for tempdir cleanup on Windows). Explicit try/finally close.
    connection = sqlite3.connect(str(graph_db))
    try:
        nodes, adjacency, edge_count = _load_graph(connection)
        persisted = _load_persisted_processes(connection, adjacency, max_processes=max_processes)
    finally:
        connection.close()
    persisted_keys = {tuple(n.node_id for n in p.nodes) for p in persisted}

    entries, dropped = find_entry_points(
        nodes, adjacency, max_candidates=max_entry_point_candidates
    )
    stats.entry_candidates_dropped = dropped

    traces: list[tuple[tuple[ProcessNode, ...], tuple[ProcessStepEdge, ...], str]] = []
    for entry, kind in entries:
        if len(traces) >= max_processes * 8:
            stats.walks_cut_by_budget += 1
            continue
        # DFS forward from the entry point. Stack items:
        # (current node, node path so far, edge path so far, visited ids)
        stack: list[
            tuple[ProcessNode, tuple[ProcessNode, ...], tuple[ProcessStepEdge, ...], frozenset[int]]
        ] = [(entry, (entry,), (), frozenset({entry.node_id}))]
        while stack:
            current, node_path, edge_path, visited = stack.pop()
            rows = adjacency.get(current.node_id, ())
            followed = 0
            terminal = True
            for target, edge in rows:
                if followed >= max_branching:
                    stats.callees_dropped += 1
                    continue
                if target.node_id in visited:
                    continue  # cycle: do not follow, do not record
                terminal = False
                followed += 1
                next_nodes = (*node_path, target)
                next_edges = (*edge_path, edge)
                if len(next_edges) >= max_depth:
                    stats.traces_depth_capped += 1
                    traces.append((next_nodes, next_edges, kind))
                else:
                    stack.append((target, next_nodes, next_edges, visited | {target.node_id}))
            if terminal and edge_path:
                traces.append((node_path, edge_path, kind))
            elif not rows and edge_path:
                traces.append((node_path, edge_path, kind))

    # Dedupe: drop strict prefixes and exact duplicate traces.
    def trace_key(nodes_path: tuple[ProcessNode, ...]) -> tuple[int, ...]:
        return tuple(n.node_id for n in nodes_path)

    maximal: dict[
        tuple[int, ...], tuple[tuple[ProcessNode, ...], tuple[ProcessStepEdge, ...], str]
    ] = {}
    for node_path, edge_path, kind in traces:
        key = trace_key(node_path)
        existing = maximal.get(key)
        if existing is None:
            maximal[key] = (node_path, edge_path, kind)
    keys = sorted(maximal.keys())
    prefix_dropped: set[tuple[int, ...]] = set()
    key_set = set(keys)
    for key in keys:
        for i in range(1, len(key)):
            prefix = key[:i]
            if prefix in key_set:
                prefix_dropped.add(prefix)
    deduped = [maximal[key] for key in keys if key not in prefix_dropped]

    processes: list[DetectedProcess] = []
    for node_path, edge_path, kind in deduped:
        if len(edge_path) < min_steps:
            continue
        # A witnessed (persisted) trace of the same path outranks the
        # heuristic reconstruction — same nodes, stronger provenance.
        if tuple(n.node_id for n in node_path) in persisted_keys:
            continue
        label = f"{node_path[0].name} -> {node_path[-1].name}"
        process_id = "proc_" + "_".join(str(n.node_id) for n in node_path[:6])
        processes.append(
            DetectedProcess(
                process_id=process_id,
                label=label,
                entry=node_path[0],
                terminal=node_path[-1],
                nodes=node_path,
                edges=edge_path,
                entry_kind=kind,
            )
        )

    processes = persisted + processes
    processes.sort(
        key=lambda p: (
            0 if p.witnessed else 1,
            0 if p.entry_kind == "declared_main" else 1,
            -p.certified_ratio,
            -p.step_count,
            p.process_id,
        )
    )
    # Diversity selection: a DFS forest produces many traces sharing long
    # prefixes. Prefer flows that cover new nodes, so the library spans the
    # repository rather than enumerating variants of one trunk.
    selected: list[DetectedProcess] = []
    covered: set[int] = set()
    pool = processes
    for novelty_min in (0.3, 0.0):  # strict pass, then fill remainder
        for p in pool:
            if len(selected) >= max_processes:
                break
            if p in selected:
                continue
            ids = {n.node_id for n in p.nodes}
            new_ratio = len(ids - covered) / len(ids) if ids else 0.0
            if new_ratio >= novelty_min or novelty_min == 0.0:
                if new_ratio >= novelty_min or len(selected) < max_processes:
                    selected.append(p)
                    covered |= ids
        if len(selected) >= max_processes:
            break
    stats.processes_dropped = len(processes) - len(selected)
    processes = selected

    return ProcessDetectionResult(
        processes=processes,
        stats=stats,
        node_count=len(nodes),
        edge_count=edge_count,
    )


def build_process_index(
    processes: Iterable[DetectedProcess],
) -> dict[int, list[DetectedProcess]]:
    """node_id -> flows containing it, for membership lookups on hot paths."""
    index: dict[int, list[DetectedProcess]] = defaultdict(list)
    for proc in processes:
        for node in proc.nodes:
            index[node.node_id].append(proc)
    return dict(index)


def render_process_block(
    processes: Iterable[DetectedProcess],
    *,
    max_processes: int = 5,
    max_symbols_per_process: int = 8,
) -> str:
    """Render flows as a bounded, provenance-honest text block."""
    lines: list[str] = []
    for i, proc in enumerate(list(processes)[:max_processes]):
        lines.append(
            f"{i + 1}. {proc.label} ({proc.step_count} steps, "
            f"{len(proc.nodes)} symbols, certified {proc.certified_ratio:.0%})"
        )
        for node in proc.nodes[:max_symbols_per_process]:
            lines.append(f"   {node.rendered}")
        if len(proc.nodes) > max_symbols_per_process:
            lines.append(f"   ... and {len(proc.nodes) - max_symbols_per_process} more")
        lines.append("")
    return "\n".join(lines).strip()


__all__ = [
    "DetectedProcess",
    "ProcessDetectionResult",
    "ProcessNode",
    "ProcessStepEdge",
    "ProcessTruncationStats",
    "build_process_index",
    "detect_processes",
    "find_entry_points",
    "render_process_block",
]
