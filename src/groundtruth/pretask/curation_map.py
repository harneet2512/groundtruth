"""Deterministic 1-hop curation map: callers/callees per focus function.

This is the curation surface the agent's own grep loop cannot cheaply build:
for each focus function, the verified callers (who depends on it) and callees
(what it calls), so the agent orients in fewer turns and keeps its budget for
writing the fix. The value is the graph MAP, not a ranked file list.

Correct-or-quiet (the agreement-guard in mechanism form): an edge is rendered
as a FACT only when its ``resolution_method`` is deterministic
(same_file / import / verified_unique / type_flow / import_type / lsp_verified).
A ``name_match`` edge is NEVER a fact — no matter how many lexical/structural
signals agree with it — because plausible-but-wrong context is the maximally
harmful output. name_match edges below a confidence floor are SUPPRESSED;
above it they are shown marked ``(unverified)`` so the agent's grep stays the
filter. When a focus function has no confident connection, the map says so
rather than guessing.

Research basis:
- RepoGraph (ICLR 2025): 1-hop ego-graph (29.67% resolved) beats 2-hop
  (26.00%) — deeper traversal is net-negative from token explosion. Cap at 1-hop.
- LocAgent (ACL 2025): the useful edges are semantic dependency edges
  (invoke/import), not bare containment; restricting to containment drops
  function Acc 71.53 -> 66.42.
- The Distracting Effect (arXiv:2505.06914, 2025) / Power of Noise (SIGIR 2024):
  plausible-but-wrong context drops accuracy 6-11pp and models do not filter it
  -> never render a name_match edge as a fact.
- Geifman & El-Yaniv (NeurIPS 2017): selective prediction — abstention is a
  first-class output, not a failure.

LLM-free, $0, pure SQL over a read-only graph.db.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

# resolution_method values that make an edge a FACT (compiler/LSP/structurally
# verified). Mirrors the categorical edge filter used by the L3/L3b hooks.
_DETERMINISTIC_METHODS: frozenset[str] = frozenset(
    {
        "same_file",
        "import",
        "verified_unique",
        "type_flow",
        "import_type",
        "lsp_verified",
        "lsp",
    }
)

# name_match (or unknown-provenance) edges below this confidence are suppressed
# entirely; at/above it they render marked (unverified). Matches gt_intel
# MIN_CONFIDENCE.
_NAME_MATCH_FLOOR = 0.5

# 1-hop neighbor cap per direction. RepoGraph: tight 1-hop beats wide dumps.
_DEFAULT_MAX_NEIGHBORS = 5


@dataclass(frozen=True)
class Edge:
    """One 1-hop connection of a focus function."""

    name: str
    file: str
    confidence: float
    resolution_method: str

    @property
    def is_fact(self) -> bool:
        """True only for deterministically-resolved edges (never name_match)."""
        return self.resolution_method in _DETERMINISTIC_METHODS

    @property
    def visible(self) -> bool:
        """A fact, or a name_match/unknown edge that cleared the floor."""
        return self.is_fact or self.confidence >= _NAME_MATCH_FLOOR


@dataclass(frozen=True)
class FunctionMap:
    """The 1-hop curation map for a single focus function."""

    file: str
    function: str
    callers: list[Edge] = field(default_factory=list)  # incoming CALLS
    callees: list[Edge] = field(default_factory=list)  # outgoing CALLS

    @property
    def has_fact(self) -> bool:
        return any(e.is_fact for e in self.callers) or any(e.is_fact for e in self.callees)

    @property
    def has_visible(self) -> bool:
        return any(e.visible for e in self.callers) or any(e.visible for e in self.callees)


def _open_ro(graph_db_path: str) -> sqlite3.Connection | None:
    """Open a read-only, query-only connection with the speed pragmas the
    speed-research flagged as missing on the read path. Returns None on failure.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{graph_db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA query_only = 1")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA mmap_size = 268435456")  # 256MB; near-memory warm reads
        conn.execute("PRAGMA cache_size = -8000")  # 8MB page cache
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn
    except sqlite3.Error:
        # Finding 4: a PRAGMA can raise after connect() succeeded; close the
        # half-open handle before bailing so we never leak a connection.
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        return None


def _has_columns(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Return (has_confidence, has_resolution_method) for the edges table."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    except sqlite3.Error:
        return (False, False)
    return ("confidence" in cols, "resolution_method" in cols)


def _node_ids(conn: sqlite3.Connection, file_path: str, name: str) -> list[int]:
    """All Function/Method node ids matching (file_path, name).

    A name can occur more than once in a file (overloads, methods on different
    classes); we union over all of them so the map is complete.
    """
    try:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE file_path = ? AND name = ? "
            "AND label IN ('Function','Method')",
            (file_path, name),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _neighbors(
    conn: sqlite3.Connection,
    node_ids: list[int],
    *,
    direction: str,
    has_conf: bool,
    has_method: bool,
    max_neighbors: int,
) -> list[Edge]:
    """1-hop CALLS neighbors of ``node_ids``.

    direction='callers' -> incoming edges (target_id IN ids), neighbor = source node.
    direction='callees' -> outgoing edges (source_id IN ids), neighbor = target node.

    Facts first, then by confidence desc; deduped by (name, file); capped.
    """
    if not node_ids:
        return []
    placeholders = ",".join("?" for _ in node_ids)
    # Finding 5: without a confidence column, do NOT synthesize a floor-clearing
    # value. A below-floor sentinel keeps name_match/unknown edges suppressed
    # (correct-or-quiet) while deterministic-method edges stay visible via is_fact
    # (Edge.visible short-circuits on is_fact, which ignores confidence).
    conf_sel = "e.confidence" if has_conf else "0.0"
    method_sel = "e.resolution_method" if has_method else "''"
    if direction == "callers":
        match_col, join_col = "e.target_id", "e.source_id"
    else:
        match_col, join_col = "e.source_id", "e.target_id"
    sql = (
        f"SELECT DISTINCT n.name, n.file_path, {conf_sel}, {method_sel} "
        f"FROM edges e JOIN nodes n ON {join_col} = n.id "
        f"WHERE {match_col} IN ({placeholders}) AND e.type = 'CALLS'"
    )
    try:
        rows = conn.execute(sql, node_ids).fetchall()
    except sqlite3.Error:
        return []

    # Finding 1: a focus name can resolve to multiple node ids (_node_ids unions
    # overloads / same-name methods), and DISTINCT keeps one row per distinct
    # 4-tuple. The SAME neighbor can therefore appear twice — once via a
    # deterministic edge (a FACT) and once via name_match — differing only in
    # resolution_method/confidence. Build candidate Edges first, then sort them
    # fact-first / confidence-desc / name BEFORE the (name,file) dedup, so the
    # best-provenance row wins deterministically. A name_match row can no longer
    # win the dedup and silently downgrade a real fact.
    candidates: list[Edge] = []
    for name, fpath, conf, method in rows:
        if not name:
            continue
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        candidates.append(
            Edge(
                name=name,
                file=fpath or "",
                confidence=conf_f,
                resolution_method=(method or ""),
            )
        )
    # Best provenance first so the dedup keeps the fact over a name_match row.
    candidates.sort(key=lambda e: (not e.is_fact, -e.confidence, e.name))

    seen: set[tuple[str, str]] = set()
    edges: list[Edge] = []
    for e in candidates:
        key = (e.name, e.file)
        if key in seen:
            continue
        seen.add(key)
        edges.append(e)
    # Drop edges that are neither facts nor floor-clearing — never show them.
    edges = [e for e in edges if e.visible]
    # Facts first, then confidence desc, then name for stable order.
    edges.sort(key=lambda e: (not e.is_fact, -e.confidence, e.name))
    return edges[:max_neighbors]


def build_function_map(
    graph_db_path: str,
    focus: list[tuple[str, str]],
    *,
    max_neighbors: int = _DEFAULT_MAX_NEIGHBORS,
) -> list[FunctionMap]:
    """Build the 1-hop curation map for each (file_path, function) in ``focus``.

    Pure read. Returns a FunctionMap per focus item (callers/callees may be
    empty). Never raises on a bad/missing db — returns [] instead.
    """
    if not focus or not os.path.exists(graph_db_path):
        return []
    conn = _open_ro(graph_db_path)
    if conn is None:
        return []
    try:
        has_conf, has_method = _has_columns(conn)
        out: list[FunctionMap] = []
        for fpath, fname in focus:
            if not fpath or not fname:
                continue
            ids = _node_ids(conn, fpath, fname)
            callers = _neighbors(
                conn, ids, direction="callers", has_conf=has_conf,
                has_method=has_method, max_neighbors=max_neighbors,
            )
            callees = _neighbors(
                conn, ids, direction="callees", has_conf=has_conf,
                has_method=has_method, max_neighbors=max_neighbors,
            )
            out.append(
                FunctionMap(file=fpath, function=fname, callers=callers, callees=callees)
            )
        return out
    finally:
        conn.close()


def _fmt_edge(e: Edge) -> str:
    """Render one edge: ``name (file)`` for a fact, ``+ (unverified)`` otherwise."""
    base = f"{e.name} ({e.file})" if e.file else e.name
    return base if e.is_fact else f"{base} (unverified)"


def render_map(maps: list[FunctionMap]) -> str:
    """Render the curation map as a compact, prose-free block.

    Emits only functions that have at least one visible connection. Returns an
    empty string when nothing is confident enough to show — the caller then
    emits the honest grep-fallback instead of guessing.
    """
    blocks: list[str] = []
    for fm in maps:
        if not fm.has_visible:
            continue
        lines = [f"{fm.file} :: {fm.function}"]
        if fm.callees:
            lines.append("  calls: " + ", ".join(_fmt_edge(e) for e in fm.callees))
        if fm.callers:
            lines.append("  called by: " + ", ".join(_fmt_edge(e) for e in fm.callers))
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "<gt-graph-map>\n" + "\n".join(blocks) + "\n</gt-graph-map>"


def any_signal(maps: list[FunctionMap]) -> bool:
    """True if any focus function has a visible connection (fact or floor-clearing)."""
    return any(fm.has_visible for fm in maps)
