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
# Kept as the legacy flat cap so _neighbors() and explicit max_neighbors callers
# reproduce v1.0 behavior byte-for-byte.
_DEFAULT_MAX_NEIGHBORS = 5

# --- Dynamic-budget knobs (provenance-aware breadth) -----------------------
# A FACT edge is structurally verified, so it never misdirects the agent — we can
# afford a generous ceiling. RepoGraph (ICLR 2025) shows the useful unit is the
# 1-hop ego-graph; ~8 verified neighbors stays well inside its tight-context
# regime while no longer truncating fact-rich hubs to an arbitrary 3-5.
_FACT_CEILING = 8
# UNVERIFIED (name_match >= floor) edges are plausible-but-wrong risk. The
# Distracting Effect (arXiv:2505.06914, 2025) / Power of Noise (SIGIR 2024):
# such context drops accuracy 6-11pp and models do not filter it, so the budget
# for guesses must shrink as facts accumulate. unverified_shown = max(0, k - facts):
# a fact-rich function shows ZERO guesses; an isolated one (0 facts) still gets a
# couple of honest hints for the agent's grep to confirm or discard.
_UNVERIFIED_BUDGET_K = 3

# --- Dynamic-hop knobs (sparse-target rescue) ------------------------------
# Default to 1-hop (RepoGraph: 1-hop 29.67% resolved > 2-hop 26.00% — deeper
# traversal is net-negative from token explosion). A SECOND hop fires ONLY when
# the 1-hop set is empty/sparse for a focus function, and ONLY along VERIFIED
# (deterministic) edges — never name_match — so it rescues an isolated/low-reach
# target without the blanket-2-hop blowup or laundering a guess as a fact.
_SECOND_HOP_SPARSE_THRESHOLD = 1  # hop-1 visible count at/below this is "sparse"
_SECOND_HOP_MAX = 3  # hard cap on rescued 2-hop facts per direction


@dataclass(frozen=True)
class Edge:
    """One 1-hop connection of a focus function."""

    name: str
    file: str
    confidence: float
    resolution_method: str
    # 1 = direct 1-hop edge; 2 = rescued via a verified-only second hop (only
    # populated when the 1-hop set was empty/sparse). Defaults to 1 so every
    # existing construction (keyword, four-field) is unchanged.
    hops: int = 1

    @property
    def is_fact(self) -> bool:
        """True only for deterministically-resolved edges (never name_match).

        Normalize the raw ``resolution_method`` (strip + lower) before the
        membership test so this "verified" decider agrees with contract_map's
        callee gate, which normalizes the same way; the deterministic methods are
        already lowercase, so normalization only widens, never narrows, the set.
        """
        return (self.resolution_method or "").strip().lower() in _DETERMINISTIC_METHODS

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


def _apply_dynamic_budget(
    edges: list[Edge],
    *,
    fact_ceiling: int,
    unverified_k: int,
) -> list[Edge]:
    """Provenance-aware breadth: ALL facts up to ``fact_ceiling``, then a
    fact-scaled number of unverified hints.

    ``edges`` must already be visible-only and sorted facts-first / confidence-
    desc (the order ``_neighbors`` produces). Facts are structurally verified and
    never misdirect, so we keep up to a generous ceiling. The unverified budget
    SHRINKS as facts accumulate — ``unverified_shown = max(0, k - fact_count)`` —
    so a fact-rich function shows zero guesses (The Distracting Effect,
    arXiv:2505.06914, 2025) while an isolated one still gets a couple of honest
    hints. Returns facts (capped) followed by the allowed unverified edges.
    """
    # Shrink the unverified budget from the RAW pre-cap fact count, not the
    # post-cap len(facts): the "fact-rich -> zero guesses" intent must hold even
    # if a future config sets unverified_k > fact_ceiling (otherwise capping
    # facts at the ceiling would re-open the guess budget on a fact-rich hub).
    raw_fact_count = sum(1 for e in edges if e.is_fact)
    facts = [e for e in edges if e.is_fact][:fact_ceiling]
    unverified_allowed = max(0, unverified_k - raw_fact_count)
    if unverified_allowed <= 0:
        return facts
    unverified = [e for e in edges if not e.is_fact][:unverified_allowed]
    return facts + unverified


def _second_hop_facts(
    conn: sqlite3.Connection,
    seed_ids: list[int],
    *,
    direction: str,
    has_conf: bool,
    has_method: bool,
    exclude: set[tuple[str, str]],
    limit: int,
) -> list[Edge]:
    """Verified-only 2-hop rescue for sparse 1-hop targets.

    ``seed_ids`` are the node ids of the focus's FACT 1-hop neighbors. We take one
    more hop FROM those seeds, keeping only deterministically-resolved (fact)
    edges — never name_match — and drop anything already shown at hop 1 or the
    focus itself (``exclude``). Returns at most ``limit`` Edges tagged ``hops=2``.

    Why verified-only and gated on sparseness: RepoGraph (ICLR 2025) finds 1-hop
    (29.67%) beats blanket 2-hop (26.00%) because deeper traversal explodes
    tokens; this rescues an isolated/low-reach target (reach≈0) WITHOUT that
    blowup and without laundering a guess (Distracting Effect, 2025) — a 2-hop
    name_match would be a guess about a guess.
    """
    if not seed_ids or limit <= 0:
        return []
    # Re-query 1-hop neighbors of the seeds, then keep facts only. We reuse the
    # frozen _neighbors path (its contract is unchanged) for the SQL + dedup, then
    # filter to facts here so a name_match can never become a 2-hop edge.
    raw = _neighbors(
        conn,
        seed_ids,
        direction=direction,
        has_conf=has_conf,
        has_method=has_method,
        max_neighbors=limit * 4,  # over-fetch; exclusion + fact filter trims it
    )
    out: list[Edge] = []
    for e in raw:
        if not e.is_fact:  # verified-only second hop
            continue
        if (e.name, e.file) in exclude:
            continue
        out.append(
            Edge(
                name=e.name,
                file=e.file,
                confidence=e.confidence,
                resolution_method=e.resolution_method,
                hops=2,
            )
        )
        exclude.add((e.name, e.file))
        if len(out) >= limit:
            break
    return out


def _dynamic_neighbors(
    conn: sqlite3.Connection,
    node_ids: list[int],
    *,
    direction: str,
    has_conf: bool,
    has_method: bool,
    fact_ceiling: int,
    unverified_k: int,
    second_hop: bool,
) -> list[Edge]:
    """1-hop neighbors under the dynamic provenance budget, plus an optional
    verified-only 2-hop rescue when the 1-hop set is empty/sparse.

    Backward-compatible by construction: it composes the unchanged ``_neighbors``
    (over-fetched, then budgeted here) — it does not alter the frozen helper.
    """
    if not node_ids:
        return []
    # Over-fetch the full visible 1-hop set (facts-first, deduped) so the budget
    # can see every fact before deciding how many guesses to allow.
    raw = _neighbors(
        conn,
        node_ids,
        direction=direction,
        has_conf=has_conf,
        has_method=has_method,
        max_neighbors=fact_ceiling + unverified_k + 8,
    )
    edges = _apply_dynamic_budget(
        raw, fact_ceiling=fact_ceiling, unverified_k=unverified_k
    )

    if not second_hop:
        return edges
    # Only rescue when the 1-hop set is empty/sparse (RepoGraph: stay 1-hop by
    # default; expand only the isolated/low-reach targets).
    if len(edges) > _SECOND_HOP_SPARSE_THRESHOLD:
        return edges
    # Seeds for the 2-hop = node ids of the FACT 1-hop neighbors (verified path).
    fact_neighbors = [e for e in edges if e.is_fact]
    seed_ids: list[int] = []
    for e in fact_neighbors:
        seed_ids.extend(_node_ids(conn, e.file, e.name))
    if not seed_ids:
        return edges
    exclude = {(e.name, e.file) for e in edges}
    # The exclude set is built from 1-hop edges only; add the focus function's
    # own (name, file) so a verified self-call (focus -> ... -> focus) can never
    # surface the focus itself as a (2-hop) neighbor.
    for fid in node_ids:
        try:
            row = conn.execute(
                "SELECT name, file_path FROM nodes WHERE id = ?", (fid,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            exclude.add((row[0], row[1] or ""))
    remaining = max(0, fact_ceiling - len(fact_neighbors))
    # remaining == 0 means the fact budget is exhausted -> take NO 2-hop edges.
    # (The old ``if remaining else _SECOND_HOP_MAX`` wrongly treated 0 as falsy
    # and fell back to the full cap.)
    limit = min(_SECOND_HOP_MAX, remaining)
    hop2 = _second_hop_facts(
        conn,
        seed_ids,
        direction=direction,
        has_conf=has_conf,
        has_method=has_method,
        exclude=exclude,
        limit=limit,
    )
    return edges + hop2


def build_function_map(
    graph_db_path: str,
    focus: list[tuple[str, str]],
    *,
    max_neighbors: int = _DEFAULT_MAX_NEIGHBORS,
    dynamic: bool = True,
    fact_ceiling: int = _FACT_CEILING,
    unverified_k: int = _UNVERIFIED_BUDGET_K,
    second_hop: bool = True,
) -> list[FunctionMap]:
    """Build the curation map for each (file_path, function) in ``focus``.

    By default (``dynamic=True``) breadth is PROVENANCE-AWARE rather than a flat
    cap: every FACT edge up to ``fact_ceiling`` (~8) is shown, and the number of
    UNVERIFIED (name_match) hints shrinks as facts accumulate
    (``unverified_shown = max(0, unverified_k - fact_count)``) — a fact-rich
    function shows no guesses, an isolated one gets a couple of honest hints
    (The Distracting Effect, arXiv:2505.06914, 2025). When ``second_hop`` and a
    focus's 1-hop set is empty/sparse, a VERIFIED-only second hop rescues the
    isolated target (RepoGraph ICLR 2025: stay 1-hop except where reach≈0).

    ``max_neighbors`` is honored only on the legacy path (``dynamic=False``),
    where behavior is byte-for-byte v1.0 — kept so existing callers stay
    call-compatible. Pure read; never raises on a bad/missing db (returns []).
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
            if dynamic:
                callers = _dynamic_neighbors(
                    conn, ids, direction="callers", has_conf=has_conf,
                    has_method=has_method, fact_ceiling=fact_ceiling,
                    unverified_k=unverified_k, second_hop=second_hop,
                )
                callees = _dynamic_neighbors(
                    conn, ids, direction="callees", has_conf=has_conf,
                    has_method=has_method, fact_ceiling=fact_ceiling,
                    unverified_k=unverified_k, second_hop=second_hop,
                )
            else:
                # Legacy flat-cap path — unchanged v1.0 behavior.
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
    """Render one edge: ``name (file)`` for a fact, ``+ (unverified)`` otherwise.

    A verified 2-hop rescue edge (``hops == 2``) is tagged ``(2-hop)`` so the
    agent knows it is transitive (a neighbor-of-neighbor), not a direct call —
    honest about distance, never laundering reach as adjacency.
    """
    base = f"{e.name} ({e.file})" if e.file else e.name
    if not e.is_fact:
        return f"{base} (unverified)"
    return f"{base} (2-hop)" if e.hops >= 2 else base


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
