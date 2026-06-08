"""Deterministic 1-hop curation map: callers/callees per focus function.

This is the curation surface the agent's own grep loop cannot cheaply build:
for each focus function, the verified callers (who depends on it) and callees
(what it calls), so the agent orients in fewer turns and keeps its budget for
writing the fix. The value is the graph MAP, not a ranked file list.

Correct-or-quiet (the agreement-guard in mechanism form): an edge is rendered
as a FACT only when its ``resolution_method`` is one the Go resolver assigns by
STRUCTURAL resolution — the unified ``DETERMINISTIC_RESOLUTION_METHODS`` set
(same_file / import / import_type / type_flow / verified_unique / impl_method /
inherited / unique_method / return_type / lsp / lsp_verified).
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
import re as _re
import sqlite3
from dataclasses import dataclass, field

# SINGLE SOURCE OF TRUTH for the FACT (deterministically-resolved) CALLS-edge
# resolution_method set. Every consumer (this module's Edge.is_fact, the live
# brief's caller-gate + [VERIFIED] tag in v1r_brief.py, contract_map's callee
# gate, the localizer's verified-witness in graph_localizer.py, and the post_edit
# hooks' categorical filter) imports THIS constant so they agree edge-for-edge.
#
# An edge belongs here ONLY when the Go resolver resolved its target
# STRUCTURALLY (compiler-grade / LSP-grade), never by bare name. Each entry is a
# real ``resolution_method`` string the resolver writes for a CALLS edge:
#   same_file       Strategy 1.0   (resolver.go)  — callee defined in same file
#   import          Strategy 1.25  (resolver.go)  — import-verified target
#   import_type     Strategy 1.93  (resolver.go:~866 region) — import-scoped type
#   type_flow       Strategy 1.95/1.96 (resolver.go:866/1005/1084) — assignment-/type-flow
#   verified_unique Strategy 1.9   (resolver.go)  — globally unique by name
#   impl_method     Strategy 1.94  (resolver.go:959) — single/few-implementor class
#   inherited       Strategy 1.75  (resolver.go:684) — CHA self/super lookup
#   unique_method   Strategy 1.98  (resolver.go:1175) — method name unique to one class
#   return_type     Strategy 1.97  (resolver.go:1141) — return-type bridging
#   lsp / lsp_verified  offline LSP promotion pass (closure.go verifiedMethods)
#
# These are exactly the structurally-resolved CALLS methods. The Go closure's own
# verified set (gt-index/internal/closure/closure.go:59) admits the first five +
# lsp/lsp_verified by NAME, and admits impl_method/inherited/return_type/unique_method
# by the confidence>=0.5 floor (resolver assigns them 0.85-1.0). The Python
# consumers gate on the METHOD NAME alone (no confidence-floor fallback), so those
# four MUST be listed explicitly here or genuinely-resolved edges get demoted to
# (unverified) — the audited 15% (738/4874 on a real graph) FACT loss.
#
# EXCLUDED ON PURPOSE:
#   param_type  — it is an EvidenceType, NOT a resolution_method (resolver.go:870
#                 sets EvidenceType="param_type" while the SAME edge's Method is
#                 "type_flow", which is already in the set). Listing it would be a
#                 category error; those edges are already covered via type_flow.
#   inheritance — it is an EXTENDS-relationship method (relationships.go:148/190/
#                 266/317 emit Type="EXTENDS", resolution_method="inheritance"),
#                 NOT a CALLS edge. The curation map / caller gate trace CALLS
#                 edges; an EXTENDS method must never enter the CALLS fact-set.
#
# name_match is NEVER here: it is a name GUESS (N same-named candidates), the
# maximally-harmful plausible-but-wrong context (The Distracting Effect,
# arXiv:2505.06914, 2025). Widening this set only widens facts; it can never let
# a name_match edge classify as a fact.
DETERMINISTIC_RESOLUTION_METHODS: frozenset[str] = frozenset(
    {
        "same_file",
        "import",
        "import_type",
        "type_flow",
        "verified_unique",
        "impl_method",
        "inherited",
        "unique_method",
        "return_type",
        "lsp",
        "lsp_verified",
    }
)

# Back-compat alias: existing imports (v1r_brief, contract_map, graph_localizer,
# Edge.is_fact below) reference ``_DETERMINISTIC_METHODS``. Point it at the shared
# constant so every consumer uses the unified fact-set with no import churn.
_DETERMINISTIC_METHODS: frozenset[str] = DETERMINISTIC_RESOLUTION_METHODS

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


def normalize_file_path(file_path: str) -> str:
    """Canonical repo-relative form of a graph/focus file path.

    SHARED normalizer — the witness twin (v1r_brief._resolved_witnesses_for_file)
    and the curation map MUST agree on path shape or the two symmetric surfaces
    disagree (one delivers a map, the other silently abstains) on the SAME file
    when the graph stored a `./`-prefixed or backslash (Windows-indexed) variant.
    Identical to the witness path's inline normalization
    (``file_path.replace("\\\\", "/").lstrip("./").lstrip("/")``). Structural; no
    task/file-specific logic.
    """
    if not file_path:
        return ""
    return file_path.replace("\\", "/").lstrip("./").lstrip("/")


# Stdlib modules whose attribute calls (``os.walk(``) the indexer can name-match
# to a same-named PROJECT symbol and stamp with a DETERMINISTIC resolution_method,
# laundering a false fact. The shared secondary defense below drops those. Kept in
# sync with v1r_brief._STDLIB_MODULES (the witness twin imports THIS set).
_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
        "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
        "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
        "functools", "operator", "collections", "subprocess", "contextlib",
    }
)

_STDLIB_SHADOW_RE = _re.compile(r"([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(")


def is_stdlib_shadow(code: str, target_name: str) -> bool:
    """True when ``code`` calls ``<stdlib_module>.<target_name>(`` — a stdlib
    attribute call the indexer name-matched to a project function of the same name
    (the proven ``os.walk`` -> project ``walk`` false caller).

    SHARED secondary defense against an indexer that records such an edge with a
    DETERMINISTIC ``resolution_method`` (so the provenance gate alone would trust
    it). The witness twin (v1r_brief) and the <gt-graph-map> (_neighbors) MUST
    apply the SAME guard or they render the same laundered edge differently — one
    drops it, the other shows it bare as a fact. Repo- and language-agnostic.
    """
    if not code or not target_name:
        return False
    for m in _STDLIB_SHADOW_RE.finditer(code):
        head = m.group(1).split(".")[0]
        if m.group(2) == target_name and head in _STDLIB_MODULES:
            return True
    return False


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

    PATH NORMALIZATION (parity with the witness twin): the focus path and the
    stored ``nodes.file_path`` can differ in separator (Windows-indexed `\\`) or a
    `./` / leading-`/` prefix. An exact `file_path = ?` match then returns [] and
    the WHOLE map silently abstains on a file that DOES have edges. We normalize
    both sides identically (``normalize_file_path``) and match by SUFFIX LIKE —
    exactly what ``v1r_brief._resolved_witnesses_for_file`` does
    (``nt.file_path LIKE '%' || norm_fp``) — so the two symmetric surfaces deliver
    on the same file. Structural; no task/file-specific logic.
    """
    norm_fp = normalize_file_path(file_path)
    if not norm_fp:
        return []
    try:
        rows = conn.execute(
            "SELECT id FROM nodes "
            "WHERE REPLACE(file_path, '\\', '/') LIKE ? AND name = ? "
            "AND label IN ('Function','Method')",
            (f"%{norm_fp}", name),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _read_code_line(repo_root: str, rel_file: str, line: int) -> str:
    """The stripped source text at ``rel_file:line`` under ``repo_root``.

    Mirrors v1r_brief._resolved_witnesses_for_file._code_at — the shadow guard
    needs the literal call site (``os.walk(``) which is NOT stored on the edge.
    Empty string on any miss (no root / bad line / unreadable) -> guard no-ops.
    """
    if not repo_root or not rel_file or not line or line <= 0:
        return ""
    try:
        with open(
            os.path.join(repo_root, rel_file), encoding="utf-8", errors="ignore"
        ) as fh:
            lines = fh.readlines()
        if 0 < line <= len(lines):
            return lines[line - 1].strip()
    except OSError:
        pass
    return ""


def _neighbors(
    conn: sqlite3.Connection,
    node_ids: list[int],
    *,
    direction: str,
    has_conf: bool,
    has_method: bool,
    max_neighbors: int,
    repo_root: str = "",
) -> list[Edge]:
    """1-hop CALLS neighbors of ``node_ids``.

    direction='callers' -> incoming edges (target_id IN ids), neighbor = source node.
    direction='callees' -> outgoing edges (source_id IN ids), neighbor = target node.

    Facts first, then by confidence desc; deduped by (name, file); capped.

    ``repo_root`` (optional): when set, the stdlib-shadow secondary defense
    (``is_stdlib_shadow``) is applied — the SAME guard the witness twin
    (v1r_brief._resolved_witnesses_for_file) uses — so a stdlib attribute call
    (``os.walk(``) the indexer name-matched to a same-named PROJECT symbol and
    stamped with a DETERMINISTIC ``resolution_method`` is DROPPED instead of
    rendered bare as a fact in <gt-graph-map>. Without ``repo_root`` (call sites
    unreadable) the guard no-ops — the provenance gate already filtered the row,
    and we never read a half-truth as proof. Repo-/language-agnostic.
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
    # The TARGET node (always e.target_id) carries the called-symbol name the
    # stdlib-shadow guard checks (``stdlibmod.<target_name>(``); the SOURCE file +
    # line locate the call site. Both are pulled so the guard can run; harmless
    # when repo_root is unset.
    sql = (
        f"SELECT DISTINCT n.name, n.file_path, {conf_sel}, {method_sel}, "
        f"ntgt.name, e.source_file, e.source_line "
        f"FROM edges e JOIN nodes n ON {join_col} = n.id "
        f"JOIN nodes ntgt ON e.target_id = ntgt.id "
        # SWAP-INVARIANT (run16 leak): never surface a test node as a caller/callee — the
        # <gt-graph-map> "called by:" leaked 6 test_plot_hdi* functions. is_test nodes are excluded.
        f"WHERE {match_col} IN ({placeholders}) AND e.type = 'CALLS' AND n.is_test = 0"
    )
    # FACTS-ONLY parity with the witness path: the symmetric resolved caller/callee queries in
    # v1r_brief already gate resolution_method ∈ DETERMINISTIC. Without the same gate here the
    # <gt-graph-map> "called by:"/"calls:" admitted name_match phantom edges (e.g. dynamic-dispatch
    # names that are not real defs) and _fmt_edge rendered them indistinguishably from facts. Gate
    # only when the method column exists; without it the conf=0.0 sentinel already suppresses them.
    if has_method:
        _det_in = ",".join("'" + str(m).lower() + "'" for m in sorted(DETERMINISTIC_RESOLUTION_METHODS))
        sql += f" AND LOWER(TRIM(e.resolution_method)) IN ({_det_in})"
    try:
        rows = conn.execute(sql, node_ids).fetchall()
    except sqlite3.Error:
        return []

    # Finding 1: a focus name can resolve to multiple node ids (_node_ids unions
    # overloads / same-name methods), and DISTINCT keeps one row per distinct
    # tuple. The SAME neighbor can therefore appear twice — once via a
    # deterministic edge (a FACT) and once via name_match — differing only in
    # resolution_method/confidence. Build candidate Edges first, then sort them
    # fact-first / confidence-desc / name BEFORE the (name,file) dedup, so the
    # best-provenance row wins deterministically. A name_match row can no longer
    # win the dedup and silently downgrade a real fact.
    candidates: list[Edge] = []
    for name, fpath, conf, method, target_name, src_file, src_line in rows:
        if not name:
            continue
        # STDLIB-SHADOW secondary defense (parity with the witness twin): when the
        # call site reads ``<stdlib>.<target>(``, the edge is a name-match to a
        # same-named project symbol stamped as a fact — drop it regardless of
        # recorded provenance. No-ops when repo_root/code is unavailable.
        if repo_root:
            code = _read_code_line(repo_root, src_file or "", int(src_line) if src_line else 0)
            if code and is_stdlib_shadow(code, target_name or ""):
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


def _verified_neighbor_count(
    conn: sqlite3.Connection,
    node_ids: list[int],
    *,
    direction: str,
    has_method: bool,
) -> int:
    """TRUE count of DISTINCT verified (fact) 1-hop neighbors — never truncated.

    A COUNT must never be subject to a presentation cap (the ``max_neighbors`` /
    over-fetch window): a hub with 30 verified callers reported as 5 understates
    blast radius 6x in the drift block. This runs a dedicated
    ``COUNT(DISTINCT name, file)`` with the SAME deterministic-method + is_test=0
    gate as ``_neighbors`` but NO row cap, so the count is the real fact count.

    Used by (a) the dynamic budget, to shrink the unverified-guess allowance
    against the real fact count rather than the capped over-fetch window
    (otherwise a >19-neighbor hub under-counts facts and leaks guesses), and by
    (b) ``contract_map._verified_caller_count`` for the drift "{n} verified
    callers depend on this" framing. ``name_match`` is never counted. Returns 0
    on any error / legacy DB without resolution_method (cannot judge provenance ->
    no fact). Pure read; never raises.
    """
    if not node_ids or not has_method:
        return 0
    placeholders = ",".join("?" for _ in node_ids)
    if direction == "callers":
        match_col, join_col = "e.target_id", "e.source_id"
    else:
        match_col, join_col = "e.source_id", "e.target_id"
    _det_in = ",".join(
        "'" + str(m).lower() + "'" for m in sorted(DETERMINISTIC_RESOLUTION_METHODS)
    )
    sql = (
        f"SELECT COUNT(DISTINCT n.name || '\\x00' || n.file_path) "
        f"FROM edges e JOIN nodes n ON {join_col} = n.id "
        f"WHERE {match_col} IN ({placeholders}) AND e.type = 'CALLS' "
        f"AND n.is_test = 0 AND n.name IS NOT NULL "
        f"AND LOWER(TRIM(e.resolution_method)) IN ({_det_in})"
    )
    try:
        row = conn.execute(sql, node_ids).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _apply_dynamic_budget(
    edges: list[Edge],
    *,
    fact_ceiling: int,
    unverified_k: int,
    true_fact_count: int | None = None,
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

    ``true_fact_count`` (when supplied) is the UNCAPPED fact count from a
    dedicated ``COUNT`` — used instead of counting ``edges`` (which the caller may
    have over-fetched under a row cap). On a mega-hub with more verified neighbors
    than the over-fetch window, counting ``edges`` under-counts facts and would
    re-open the guess budget on a fact-rich hub; the uncapped count closes that.
    """
    # Shrink the unverified budget from the RAW pre-cap fact count, not the
    # post-cap len(facts): the "fact-rich -> zero guesses" intent must hold even
    # if a future config sets unverified_k > fact_ceiling (otherwise capping
    # facts at the ceiling would re-open the guess budget on a fact-rich hub).
    # Prefer the uncapped COUNT when given (closes the >over-fetch-window hub gap).
    windowed_fact_count = sum(1 for e in edges if e.is_fact)
    raw_fact_count = (
        true_fact_count if true_fact_count is not None
        else windowed_fact_count
    )
    # Defensive: the true count can only be >= what we see in the window; never
    # let a bad/smaller override re-open the guess budget.
    raw_fact_count = max(raw_fact_count, windowed_fact_count)
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
    repo_root: str = "",
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
    #
    # TRUNCATE-AFTER-EXCLUDE: _neighbors caps rows BEFORE we can apply ``exclude``.
    # On a well-connected seed whose top neighbors heavily overlap the focus's own
    # 1-hop set, the capped window can be entirely excluded -> 0 rescued edges even
    # though valid 2-hop facts exist beyond the window. Over-fetch by the exclude
    # size so the cap never bites before exclusion runs.
    over_fetch = limit * 4 + len(exclude)
    raw = _neighbors(
        conn,
        seed_ids,
        direction=direction,
        has_conf=has_conf,
        has_method=has_method,
        max_neighbors=over_fetch,
        repo_root=repo_root,
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
    repo_root: str = "",
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
        repo_root=repo_root,
    )
    # TRUE (uncapped) fact count so the guess budget shrinks against the REAL
    # number of verified neighbors, not the over-fetch window. On a mega-hub with
    # more facts than the window, counting only ``raw`` would under-count and leak
    # guesses onto a fact-rich hub.
    true_facts = _verified_neighbor_count(
        conn, node_ids, direction=direction, has_method=has_method
    )
    edges = _apply_dynamic_budget(
        raw, fact_ceiling=fact_ceiling, unverified_k=unverified_k,
        true_fact_count=true_facts,
    )

    if not second_hop:
        return edges
    # Only rescue when the VERIFIED reach is empty/sparse (RepoGraph: stay 1-hop by
    # default; expand only the isolated/low-reach targets). Gate on the FACT count,
    # NOT total visible: a target with 0 facts but a couple of name_match guesses
    # is exactly the isolated case the rescue exists for — counting the guesses
    # toward "not sparse" would suppress the verified rescue on precisely those
    # targets (self-defeating). reach is measured by VERIFIED edges, not guesses.
    fact_neighbors = [e for e in edges if e.is_fact]
    if len(fact_neighbors) > _SECOND_HOP_SPARSE_THRESHOLD:
        return edges
    # Seeds for the 2-hop = node ids of the FACT 1-hop neighbors (verified path).
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
        repo_root=repo_root,
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
    repo_root: str = "",
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

    ``repo_root`` (optional): when set, the stdlib-shadow secondary defense is
    applied to every neighbor (parity with the witness twin) so a stdlib attribute
    call name-matched to a same-named project symbol is dropped instead of rendered
    bare in <gt-graph-map>. Unset -> guard no-ops (the provenance gate already ran).

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
                    repo_root=repo_root,
                )
                callees = _dynamic_neighbors(
                    conn, ids, direction="callees", has_conf=has_conf,
                    has_method=has_method, fact_ceiling=fact_ceiling,
                    unverified_k=unverified_k, second_hop=second_hop,
                    repo_root=repo_root,
                )
            else:
                # Legacy flat-cap path — unchanged v1.0 behavior.
                callers = _neighbors(
                    conn, ids, direction="callers", has_conf=has_conf,
                    has_method=has_method, max_neighbors=max_neighbors,
                    repo_root=repo_root,
                )
                callees = _neighbors(
                    conn, ids, direction="callees", has_conf=has_conf,
                    has_method=has_method, max_neighbors=max_neighbors,
                    repo_root=repo_root,
                )
            out.append(
                FunctionMap(file=fpath, function=fname, callers=callers, callees=callees)
            )
        return out
    finally:
        conn.close()


def verified_caller_count(graph_db_path: str, file_path: str, name: str) -> int:
    """TRUE count of VERIFIED (fact) callers of ``(file_path, name)`` — uncapped.

    Item #14: ``contract_map._verified_caller_count`` previously derived this from
    ``build_function_map(..., dynamic=False)`` and counted ``e.is_fact`` over the
    returned callers — which ``_neighbors`` had ALREADY truncated at
    ``max_neighbors`` (default 5). A function with 30 verified callers reported 5,
    understating the drift block's "{n} verified callers depend on this" 6x. A
    COUNT must never be subject to a presentation cap. This runs the dedicated
    uncapped ``COUNT(DISTINCT)`` with the deterministic-method gate instead.

    name_match callers are NEVER counted (a guessed caller must not inflate the
    consequence). Returns 0 on bad/missing db or legacy DB without provenance.
    Pure read; never raises.
    """
    if not file_path or not name or not os.path.exists(graph_db_path):
        return 0
    conn = _open_ro(graph_db_path)
    if conn is None:
        return 0
    try:
        _, has_method = _has_columns(conn)
        ids = _node_ids(conn, file_path, name)
        if not ids:
            return 0
        return _verified_neighbor_count(
            conn, ids, direction="callers", has_method=has_method
        )
    finally:
        conn.close()


def _fmt_edge(e: Edge) -> str:
    """Render one edge as agent-friendly text. No internal jargon.

    Correct-or-quiet honesty marker (the module docstring's promise, in code):
    a FACT edge (deterministically resolved — ``Edge.is_fact``) renders bare; a
    VISIBLE-but-unverified edge (a name_match / unknown-provenance edge that only
    cleared the confidence floor, or any edge on a legacy DB with no
    ``resolution_method`` column so provenance is unknown) is tagged
    ``(unverified)`` so the agent's grep stays the filter and a guess is never
    shown indistinguishably from a structurally-resolved fact (The Distracting
    Effect, arXiv:2505.06914, 2025). A 2-hop edge is additionally tagged so the
    agent knows it's transitive (2-hop edges are verified-only, hence always facts).
    """
    base = f"{e.name} ({e.file})" if e.file else e.name
    if e.hops >= 2:
        base = f"{base} (2-hop)"
    if not e.is_fact:
        base = f"{base} (unverified)"
    return base


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
