"""Symbol-anchored multi-hop graph-witness localizer (L1 core).

THE DIAGNOSED FAILURE this module exists to close (real beets-5495 run,
gt_run_summary): the L1 ranker selected candidate files by LEXICAL keyword
overlap -> l1_candidate_files=['beets/util/pipeline.py','beets/library.py']
with the gold file beets/importer.py NOT a candidate, those candidates had 0
call/import/test edges, l1_confidence_score=0.0, yet a "Highest-confidence
candidate" line still rendered. The lexical path never TRAVERSED graph.db, so it
missed importer.py even though importer.py::set_fields has a CALLS edge to
dbcore/db.py::set_parse — the exact symbol pair the issue names.

This module fixes that by anchoring on issue SYMBOLS (not file blobs) and
walking graph.db edges from those symbol nodes, recording a structural WITNESS
for every candidate file it surfaces.

RESEARCH BASIS (deterministic parts only — no embeddings / no GNN / no LLM):
  * KGCompass 2025: 89.7% of localizable bugs carry NO explicit file/line hint
    and are recoverable ONLY via multi-hop traversal over a code graph from
    issue-anchored entities. => seed on symbols, BFS the graph.
  * SWERank 2025 (retrieve -> rerank): down-rank "hard negatives" — files that
    are lexically similar to the issue but structurally UNWITNESSED (our
    pipeline.py / library.py). => a witnessed candidate MUST outrank a
    witness-less lexical-only one.
  * RepoGraph (ICLR 2025): a k=1 ego-graph is the strongest single hop; FILTER
    stdlib / third-party edges so the walk stays repo-internal. => default 1-hop,
    optional 2nd hop; stdlib-shadow guard on edges.
  * BLUiR (ASE 2013): structured field-level lexical anchoring on
    function/class/identifier names beats flat-blob BM25. => the lexical
    component of the rerank scores issue-term ∩ symbol/path identifiers, not a
    document blob.
  * CoSIL 2025: a pruner that drops unrelated directions + top-K narrowing keeps
    precision high. => top-K cap on witnessed candidates.

We deliberately do NOT adopt SWERank's neural reranker / GREPO's GNN — those
violate GroundTruth's LLM-free, deterministic-only core contract.

Everything here is pure sqlite + regex over graph.db. No model, no network.
"""
from __future__ import annotations

import os
import re as _re
import sqlite3
from dataclasses import dataclass, field

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _NAME_MATCH_FLOOR,
    _has_columns,
    _open_ro,
)
from groundtruth.confidence import dynamic_cutoff, is_seed_pollutant

# _STDLIB_HEADS deleted (Step 2): it was DEAD — the code's own comment noted the
# `nbr_name in _STDLIB_HEADS` guard never fired (the shadow token is the attribute,
# not the module head).

# Stdlib-shadow ATTRIBUTE guard (TEMPORARY, Python-only band-aid). The indexer
# name-matches a stdlib attribute call (os.walk / json.loads) to a same-named
# PROJECT function, fabricating a spurious name_match edge. This conservative list of
# attribute tokens (almost never a project edit target) drops those shadows from
# WITNESS DISPLAY — applied to name_match (unverified) edges ONLY; verified edges are
# never filtered.
#
# Frontier-correct fix (DEFERRED to Step 6 / Go indexer): IMPORT-SCOPE resolution —
# accept a name_match edge as project-internal only if the caller file imports the
# module that defines that name (RepoGraph ICLR 2025 documents WHY name-match
# over-connects; Aider's defines∩references membership predicate). That generalizes
# to every language with an import extractor; a literal stdlib-attr list does not.
# A membership test alone cannot catch this case because the project DOES define a
# same-named symbol. Kept here as a no-op-on-non-Python safety net (os/walk/loads
# never collide in Go/Rust/JS) until the indexer resolves qualifiers — correct-or-
# quiet (it only SUPPRESSES a known-spurious unverified edge), not poison.
_STDLIB_ATTRS: frozenset[str] = frozenset(
    {
        "walk", "loads", "dumps", "utcnow", "getlogger", "basicconfig",
        "deepcopy", "namedtuple", "defaultdict", "fromtimestamp",
    }
)

# ---------------------------------------------------------------------------
# Composite rerank weights (the Hybrid pillar: >=3 independent signals).
#
# A witnessed candidate's score is:
#   score = W_WITNESS * witness_strength      # structural (graph)  -- PRIMARY
#         + W_LEX     * structured_lexical    # field-level lexical (BLUiR)
#         + W_DEGREE  * degree_prior          # caller-frequency / centrality
#
# Rationale for the ordering W_WITNESS > W_LEX > W_DEGREE:
#   * W_WITNESS dominates because the whole point (SWERank hard-negative
#     principle + KGCompass) is that a structural edge from an issue-named symbol
#     is stronger evidence of the edit target than keyword overlap, which is
#     exactly what mislocalized beets-5495 (pipeline.py won on lexical alone).
#   * W_LEX is a real but secondary signal (BLUiR): a file whose own
#     symbol/path identifiers intersect the issue terms is more likely relevant,
#     but only as a tie-breaker among witnessed files, never enough to beat a
#     verified-edge witness on its own.
#   * W_DEGREE is the weakest (RepoGraph hub caution): high in-degree is a hub
#     prior, useful only to break ties, and it is hub-penalized so a pure hub
#     never wins on degree alone.
# These are NOT calibrated magic constants in the benchmark sense — they encode
# the cited research ordering. The CONFIDENCE GATE downstream is data-derived
# (per-task median gap), so the absolute scale of these weights is not load-
# bearing; only their ORDER is.
W_WITNESS = 0.60
W_LEX = 0.30
# Degree prior: weak centrality tie-breaker, hub-capped by tanh. NOTE: a hub-PENALTY
# variant (degree as `- W_HUB*deg_norm`) was tested on the v15.2 holdout and REVERTED
# — it regressed python hit@1 (8->6) while only helping rust (net wash), because some
# real edit targets are themselves high-degree (crossplane gold deg 250 > the hub
# beating it at 201). The data FALSIFIED the RepoGraph hub-penalty hypothesis for this
# localization metric, so the original small positive prior is kept (measure-first).
W_DEGREE = 0.10
# Generated / codegen files are NEVER hand-edited fix targets -> heavy demote (kept as
# a last-resort, not hard-dropped). Cross-ecosystem markers, not benchmark-shaped. This
# SURVIVED measurement: run_function.pb.go (a protobuf) no longer out-ranks the gold.
W_GEN = 1.0
# Subject bonus: a file that DEFINES the issue's SUBJECT symbol (the broken
# function, named earliest in the issue) is the likely EDIT TARGET. This must
# dominate the raw centrality (degree) prior — otherwise a high-in-degree CALLEE
# (db.py::set_parse) out-ranks the CALLER the issue is actually about
# (importer.py::set_fields), which is the RepoGraph/SWERank hub-bias failure.
# Set above W_DEGREE so the subject always beats a pure centrality tie, but below
# W_LEX/W_WITNESS so it never overturns a stronger structural/lexical signal.
W_SUBJECT = 0.15

# Witness strength by edge provenance (correct-or-quiet): a deterministic edge is
# a strong structural fact; a name_match edge is a weak, unverified hint that can
# still surface a candidate but must not masquerade as a verified witness.
_WITNESS_VERIFIED = 1.0
_WITNESS_NAMEMATCH = 0.45  # below the 0.5 mid-tier line on purpose
# Hop decay is applied inline in Witness.strength() as 1/(1+hop).

# Hub guard for the degree prior — tanh saturates so a 500-caller hub doesn't
# linearly dominate a 5-caller specific module. Matches hub_penalty.HUB_SCALE.
_HUB_SCALE = 50.0

_MIN_ANCHOR_LEN = 3

# Composite rerank weights (Phase 1: FTS5 + path decay added).
# The formula is:
#   Score(f) = W_BM25 * BM25_norm + W_PATH_DECAY * PathDecay_norm
#            + W_WITNESS * witness_norm + W_SUBJECT * subject_norm
#            + W_LEX * lex_norm + W_DEGREE * deg_norm - W_GEN * gen_flag
#
# BM25 and PathDecay are NEW signals that ADD to the existing witness/lex/degree
# scoring. They do not replace any existing signal — backward compatible.
W_BM25 = 0.35
W_PATH_DECAY = 0.30

def _is_generic_symbol(sym: str) -> bool:
    """DUNDER-SHAPE language invariant ONLY — used for WITNESS DISPLAY choice (prefer
    an informative 'set_fields calls set_parse' edge over a generic '__init__ called
    by _setup'). The former literal set (setUp/tearDown/setUpClass/__call__/__eq__...)
    was poison: those are unittest/Python conventions, NOT language invariants, and
    fail the moment the repo is pytest-style / Go / JS. Frontier precedent (Aider
    repomap.py `if ident.startswith('_'): mul *= 0.1`) penalizes by name SHAPE, not a
    list. DATA-DERIVED genericness (homonym/hub) lives in is_seed_pollutant (used for
    the DEFINES trust gate below); a fuller symbol_specificity ordering of the display
    needs a conn threaded into render_witness — deferred follow-up."""
    s = (sym or "").strip()
    return s.startswith("__") and s.endswith("__")


_GENERATED_MARKERS: tuple[str, ...] = (
    "zz_generated", ".pb.go", ".pb.gw.go", "_pb2.py", "_pb2_grpc.py",
    ".generated.", "/generated/", "_generated.go", ".g.dart", ".freezed.dart",
)


def _is_generated(fp: str) -> bool:
    """True for machine-generated files (protobuf, grpc, codegen) that are never
    hand-edited fix targets. Cross-ecosystem marker list, language-agnostic — it
    keeps a generated hub (run_function.pb.go, deg 201) from out-ranking the real
    edit target. Correct-or-quiet: a heavy score penalty, not a hard drop."""
    f = (fp or "").lower()
    return any(m in f for m in _GENERATED_MARKERS)


def _fts5_candidates(
    conn: sqlite3.Connection,
    issue_tokens: set[str],
    limit: int = 50,
) -> list[tuple[int, str, str, float]]:
    """BM25 retrieval over function names/signatures/paths via FTS5.

    Returns (node_id, name, file_path, bm25_score) tuples.
    Matches grep's recall but ranks by relevance using SQLite's built-in BM25.

    Research: BLUiR (ASE 2013) — structured field-level lexical anchoring on
    function/class/identifier names beats flat-blob BM25. FTS5 over the nodes
    table is exactly that: structured per-symbol indexing, not whole-file text.

    Graceful fallback: returns [] when nodes_fts table doesn't exist (old
    graph.db without FTS5, incremental-only builds). The caller merges FTS5
    candidates with name-match seeds; an empty return means name-match-only.
    """
    if not issue_tokens:
        return []

    # Check if nodes_fts exists. If not (Go-SQLite lacked FTS5), create it
    # with a writable conn AND use that same conn for queries (the read-only
    # conn has a stale WAL snapshot and won't see the new table).
    _fts_conn = conn  # default: use the caller's read-only conn
    _fts_conn_owned = False  # True if we opened our own conn (must close it)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "nodes_fts" not in tables:
            _db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            if not _db_path:
                return []
            try:
                _fts_conn = sqlite3.connect(_db_path)
                _fts_conn_owned = True
                _fts_conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                        name, qualified_name, signature, file_path,
                        content='nodes', content_rowid='id'
                    )
                """)
                _fts_conn.execute("""
                    INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path)
                    SELECT id, name, COALESCE(qualified_name, ''), COALESCE(signature, ''), file_path
                    FROM nodes
                """)
                _fts_conn.commit()
            except sqlite3.Error:
                if _fts_conn_owned:
                    try:
                        _fts_conn.close()
                    except Exception:
                        pass
                return []
    except sqlite3.Error:
        return []

    # Build FTS5 MATCH query: join tokens with OR for broad recall.
    # Filter tokens: skip very short (< 3 chars) and escape FTS5 special chars.
    safe_tokens = []
    for t in sorted(issue_tokens, key=lambda x: -len(x)):
        # FTS5 special chars: *, ^, ", (, ), :, +, -, NOT, AND, OR, NEAR
        # Wrap each token in double quotes to treat as literal phrase.
        cleaned = t.replace('"', '')
        if len(cleaned) >= 3 and all(c.isalnum() or c == '_' for c in cleaned):
            safe_tokens.append(f'"{cleaned}"')
        if len(safe_tokens) >= 20:
            break

    if not safe_tokens:
        return []

    match_expr = " OR ".join(safe_tokens)

    try:
        rows = _fts_conn.execute(
            """SELECT rowid, name, file_path,
                      bm25(nodes_fts, 1.0, 2.0, 0.5, 0.5) as score
               FROM nodes_fts
               WHERE nodes_fts MATCH ?
               ORDER BY score
               LIMIT ?""",
            (match_expr, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if _fts_conn_owned:
            try:
                _fts_conn.close()
            except Exception:
                pass

    results: list[tuple[int, str, str, float]] = []
    for row in rows:
        if row and row[0] is not None:
            score = -float(row[3]) if row[3] is not None else 0.0
            results.append((int(row[0]), str(row[1]), _normalize(str(row[2])), score))
    return results


def _path_decay_scores(
    conn: sqlite3.Connection,
    seed_node_ids: list[int],
    has_conf: bool,
    max_hop: int = 3,
    beta: float = 0.85,
    min_edge_conf: float = 0.5,
) -> dict[str, float]:
    """KGCompass-style path decay scoring over the call graph.

    Walk call graph from seeds using Dijkstra-style BFS. Edge weight =
    1/confidence, so high-confidence edges (verified imports at 1.0) are
    cheap paths and speculative name_match edges (0.4) are expensive.

    Path cost L(f) = sum(1/confidence) along the shortest path from any seed.
    Score S(f) = beta^L(f). Verified import edges yield short paths with
    minimal decay; speculative name_match edges yield long paths with heavy
    decay — exactly the correct-or-quiet property.

    Research: KGCompass (2025) — confidence-weighted path traversal for
    entity retrieval in knowledge graphs. RepoGraph (ICLR 2025) — k-hop
    ego-graph with diminishing returns beyond k=2 for dense graphs.

    Returns {file_path: decay_score} for all reachable files within max_hop.
    """
    import heapq

    if not seed_node_ids:
        return {}

    # Priority queue: (cost, node_id, hop_count)
    pq: list[tuple[float, int, int]] = [(0.0, nid, 0) for nid in seed_node_ids]
    heapq.heapify(pq)
    # Best cost to reach each node.
    best_cost: dict[int, float] = {nid: 0.0 for nid in seed_node_ids}
    # File path for each visited node.
    node_file: dict[int, str] = {}

    # Pre-fetch seed file paths.
    for i in range(0, len(seed_node_ids), 400):
        chunk = seed_node_ids[i:i + 400]
        ph = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT id, file_path FROM nodes WHERE id IN ({ph})",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0] is not None and r[1]:
                node_file[int(r[0])] = _normalize(str(r[1]))

    conf_sel = "e.confidence" if has_conf else "1.0"
    conf_where = f"AND e.confidence >= {min_edge_conf}" if has_conf else ""

    while pq:
        cost, nid, hops = heapq.heappop(pq)

        # Skip if we already found a cheaper path to this node.
        if cost > best_cost.get(nid, float('inf')):
            continue

        if hops >= max_hop:
            continue

        # Expand neighbors in both directions (out-edges and in-edges).
        for match_col, join_col in [("e.source_id", "e.target_id"),
                                     ("e.target_id", "e.source_id")]:
            try:
                rows = conn.execute(
                    f"""SELECT {join_col} AS nbr_id, n.file_path, {conf_sel}
                        FROM edges e
                        JOIN nodes n ON {join_col} = n.id
                        WHERE {match_col} = ?
                          AND e.type IN ('CALLS', 'IMPORTS')
                          AND n.is_test = 0
                          {conf_where}
                        LIMIT 100""",
                    (nid,),
                ).fetchall()
            except sqlite3.Error:
                continue

            for nbr_id, nbr_file, conf in rows:
                if nbr_id is None or nbr_file is None:
                    continue
                nbr_id = int(nbr_id)
                nbr_file = _normalize(str(nbr_file))
                try:
                    conf_f = float(conf) if conf is not None else 1.0
                except (TypeError, ValueError):
                    conf_f = 1.0
                if conf_f <= 0:
                    conf_f = 0.1  # avoid division by zero

                edge_cost = 1.0 / conf_f
                new_cost = cost + edge_cost

                if new_cost < best_cost.get(nbr_id, float('inf')):
                    best_cost[nbr_id] = new_cost
                    node_file[nbr_id] = nbr_file
                    heapq.heappush(pq, (new_cost, nbr_id, hops + 1))

    # Aggregate to file level: take the minimum cost (best path) to each file.
    file_cost: dict[str, float] = {}
    for nid, cost in best_cost.items():
        fp = node_file.get(nid)
        if fp:
            if fp not in file_cost or cost < file_cost[fp]:
                file_cost[fp] = cost

    # Convert cost to decay score: S(f) = beta^cost.
    return {fp: beta ** cost for fp, cost in file_cost.items()}


@dataclass(frozen=True)
class Witness:
    """The structural reason a file is a localization candidate.

    anchor: the issue symbol that seeded this witness (e.g. ``set_parse``).
    edge_type: 'CALLS' | 'IMPORTS' (the edge that connects the candidate file's
        symbol to / from the anchor symbol).
    direction: 'calls_anchor' (candidate symbol CALLS the anchor) or
        'called_by_anchor' (anchor CALLS the candidate symbol).
    verified: True iff the edge's resolution_method is deterministic.
    confidence: the edge confidence (0..1).
    hop: graph hop distance from the seed symbol's file (0 = seed file itself).
    src_symbol / dst_symbol: the two endpoints, so the renderer can state the
        fact ``set_fields -> set_parse`` without re-querying.
    """

    file_path: str
    anchor: str
    edge_type: str
    direction: str
    verified: bool
    confidence: float
    hop: int
    src_symbol: str
    dst_symbol: str

    def strength(self) -> float:
        base = _WITNESS_VERIFIED if self.verified else _WITNESS_NAMEMATCH
        # Scale a verified witness by its own confidence too (an import edge at
        # 1.0 beats a low-confidence verified edge). hop decay: 1/(1+hop).
        conf = self.confidence if self.confidence > 0 else (1.0 if self.verified else 0.5)
        return base * conf * (1.0 / (1.0 + self.hop))


@dataclass(frozen=True)
class Candidate:
    file_path: str
    score: float
    witnesses: list[Witness]
    lex_hits: int  # # of issue terms intersecting this file's symbol/path identifiers
    degree: int
    confidence: float  # best-witness strength, 0..1 (drives the render gate)

    @property
    def has_verified_witness(self) -> bool:
        return any(w.verified for w in self.witnesses)

    def render_witness(self) -> str:
        """Human-facing one-liner for the most INFORMATIVE witness, or '' if none.

        Prefers a real edge witness (CALLS/IMPORTS connecting this file's symbol
        to a DIFFERENT issue-anchored symbol) over a self-DEFINES witness, since
        "set_fields calls set_parse [CALLS]" tells the agent the structural fact
        it needs, whereas "set_fields defines set_fields" is uninformative. Among
        edge witnesses, the strongest (verified, lowest-hop) wins. Falls back to
        the DEFINES witness only when no edge witness exists (the file merely
        defines the anchor and nothing connects it onward).
        """
        if not self.witnesses:
            return ""
        edge_wits = [
            w for w in self.witnesses if w.direction != "defines_anchor"
            and w.src_symbol != w.dst_symbol
        ]
        if edge_wits:
            # Prefer a MEANINGFUL edge (neither endpoint a generic constructor/
            # dunder) over a generic one — all hop-0 verified edges tie on strength,
            # so without this the display picks an arbitrary "__init__ called by X"
            # and hides the real "set_fields calls set_parse" (live beets-5495 bug).
            def _display_key(x: Witness) -> tuple[int, float]:
                generic = _is_generic_symbol(x.src_symbol) or _is_generic_symbol(x.dst_symbol)
                return (1 if generic else 0, -x.strength())

            w = min(edge_wits, key=_display_key)
            rel = "calls" if w.direction == "calls_anchor" else "called by"
            tag = "" if w.verified else " (unverified)"
            if w.hop >= 2:
                # Anchor-drift guard: at hop>=2 the edge endpoints (src/dst) can be
                # INTERMEDIATE symbols the issue never names. Render the issue
                # anchor provenance instead of two off-anchor symbols, and mark it
                # an N-hop path so it does not read as a confident DIRECT claim.
                # RepoGraph (ICLR 2025): the k=1 ego-graph is the strongest hop; a
                # >=2-hop line must not masquerade as a 1-hop fact.
                far = w.src_symbol if w.direction == "calls_anchor" else w.dst_symbol
                return (
                    f"{w.anchor} -> ... -> {far} "
                    f"[{w.edge_type}, {w.hop}-hop{tag}]"
                )
            return f"{w.src_symbol} {rel} {w.dst_symbol} [{w.edge_type}{tag}]"
        # Only a self-DEFINES witness: state that the file defines the issue symbol.
        w = max(self.witnesses, key=lambda x: x.strength())
        tag = "" if w.verified else " (unverified)"
        return f"defines {w.anchor} (issue symbol){'' if w.verified else tag}"


@dataclass(frozen=True)
class LocalizerResult:
    candidates: list[Candidate]
    anchor_symbols: list[str]
    confidence: float            # best candidate confidence (0 when no anchor hit)
    confident: bool              # passes the per-task data-derived gate
    gate_reason: str             # why confident / not (telemetry)
    scope_chains: list[ScopeChain] = field(default_factory=list)
    graph_stats: dict = field(default_factory=dict)


def _normalize(fp: str) -> str:
    return fp.replace("\\", "/").lstrip("./").lstrip("/")


def _issue_terms(issue_text: str) -> set[str]:
    return {
        w.lower()
        for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text or "")
        if len(w) >= _MIN_ANCHOR_LEN
    }


def _seed_node_rows(
    conn: sqlite3.Connection, anchors: set[str]
) -> list[tuple[int, str, str]]:
    """(node_id, name, file_path) for every Function/Method/Class node whose name
    is an issue anchor. These are the BFS seeds (KGCompass entity seeding)."""
    if not anchors:
        return []
    out: list[tuple[int, str, str]] = []
    anchors_l = list(anchors)
    # Chunk to stay under SQLite's variable limit on huge anchor sets.
    for i in range(0, len(anchors_l), 400):
        chunk = anchors_l[i : i + 400]
        ph = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT id, name, file_path FROM nodes "
                f"WHERE name IN ({ph}) AND is_test = 0 "
                f"AND label IN ('Function','Method','Class','Interface')",
                tuple(chunk),
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0] is not None and r[2]:
                out.append((int(r[0]), str(r[1]), _normalize(str(r[2]))))
    return out


def _grep_to_seeds(
    issue_tokens: set[str],
    repo_root: str,
    conn: sqlite3.Connection,
    max_seeds: int = 20,
) -> list[tuple[int, str, str]]:
    """Grep-recall seeding: subsume grep so GT can never have worse recall.

    Runs ripgrep (or fallback grep) over the repo for issue tokens, maps hit
    file:line pairs to the enclosing graph node (the function/method/class
    containing that line), and returns those nodes as additional BFS seeds.

    This is mechanism B from the recall analysis: use grep for recall, graph
    for rank. GT seeds only on name-matched Function/Method/Class/Interface
    nodes today (_seed_node_rows), missing files whose code CONTAINS issue
    tokens in string literals, attributes, variable names, or function bodies.
    Grep finds those. This function bridges the gap.

    Research: SWERank (2025) retrieve→rerank — the retrieve must have at
    least grep-grade recall; the rerank adds structural depth.
    """
    import subprocess

    if not repo_root or not issue_tokens:
        return []

    # Pick distinctive tokens (skip very short or very common words)
    tokens = sorted(
        (t for t in issue_tokens if len(t) >= 4 and t not in {
            "that", "this", "with", "from", "have", "been", "will",
            "when", "what", "which", "were", "they", "their", "does",
            "should", "would", "could", "about", "some", "other",
            "into", "more", "than", "each", "also", "after", "before",
        }),
        key=lambda t: -len(t),
    )[:10]

    if not tokens:
        return []

    # Run ripgrep for each token, collect file:line hits
    hit_files: dict[str, set[int]] = {}
    for token in tokens:
        try:
            result = subprocess.run(
                ["rg", "-n", "--no-heading", "-l", "-i", token, repo_root],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    fp = line.strip()
                    if fp:
                        rel = os.path.relpath(fp, repo_root).replace("\\", "/")
                        hit_files.setdefault(rel, set()).add(0)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # rg not available — fallback to Python grep
            try:
                for dirpath, _, filenames in os.walk(repo_root):
                    for fname in filenames:
                        if not any(fname.endswith(ext) for ext in (
                            ".py", ".go", ".rs", ".ts", ".js", ".java", ".rb",
                            ".c", ".cpp", ".h", ".cs",
                        )):
                            continue
                        fpath = os.path.join(dirpath, fname)
                        try:
                            with open(fpath, encoding="utf-8", errors="ignore") as fh:
                                content = fh.read(500_000)
                            if token.lower() in content.lower():
                                rel = os.path.relpath(fpath, repo_root).replace("\\", "/")
                                hit_files.setdefault(rel, set()).add(0)
                        except OSError:
                            continue
            except Exception:
                pass
            break  # if rg fails, one Python pass is enough

    if not hit_files:
        return []

    # Score files by number of distinct tokens they contain
    file_scores: list[tuple[str, int]] = []
    for fp, lines in hit_files.items():
        # Count how many distinct issue tokens hit this file
        try:
            fpath = os.path.join(repo_root, fp)
            content = open(fpath, encoding="utf-8", errors="ignore").read(500_000).lower()
            hits = sum(1 for t in tokens if t.lower() in content)
            file_scores.append((fp, hits))
        except OSError:
            file_scores.append((fp, 1))

    file_scores.sort(key=lambda x: -x[1])
    top_files = [fp for fp, _ in file_scores[:max_seeds]]

    # Map hit files to enclosing graph nodes
    seeds: list[tuple[int, str, str]] = []
    seen_ids: set[int] = set()
    for fp in top_files:
        norm = _normalize(fp)
        try:
            rows = conn.execute(
                "SELECT id, name, file_path FROM nodes "
                "WHERE file_path = ? AND is_test = 0 "
                "AND label IN ('Function','Method','Class','Interface') "
                "LIMIT 5",
                (norm,),
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0] is not None and r[0] not in seen_ids:
                seen_ids.add(int(r[0]))
                seeds.append((int(r[0]), str(r[1]), _normalize(str(r[2]))))
    return seeds


def _file_degrees(conn: sqlite3.Connection, files: set[str]) -> dict[str, int]:
    """In-degree (number of incoming CALLS) per file — the centrality prior."""
    if not files:
        return {}
    deg: dict[str, int] = {}
    files_l = list(files)
    for i in range(0, len(files_l), 400):
        chunk = files_l[i : i + 400]
        ph = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT n.file_path, COUNT(e.id) FROM nodes n "
                f"JOIN edges e ON e.target_id = n.id "
                f"WHERE n.file_path IN ({ph}) GROUP BY n.file_path",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0]:
                deg[_normalize(str(r[0]))] = int(r[1] or 0)
    return deg


def _is_verified(method: str) -> bool:
    return (method or "").strip().lower() in _DETERMINISTIC_METHODS


def _graph_stats(conn: sqlite3.Connection, has_conf: bool) -> dict:
    """Per-graph density + confidence distribution for dynamic BFS calibration.

    Returns dict with:
      node_count, edge_count, avg_degree, conf_p50, conf_p90,
      high_conf_frac (fraction of edges with confidence >= 0.5).

    Research: RepoGraph (ICLR 2025) — graph density determines effective BFS
    radius; KGCompass (2025) — hop calibration by entity density.
    """
    stats: dict = {"node_count": 0, "edge_count": 0, "avg_degree": 0.0,
                   "conf_p50": 1.0, "conf_p90": 1.0, "high_conf_frac": 1.0}
    try:
        stats["node_count"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_test = 0"
        ).fetchone()[0] or 0
        stats["edge_count"] = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE type IN ('CALLS','IMPORTS')"
        ).fetchone()[0] or 0
        if stats["node_count"] > 0:
            stats["avg_degree"] = stats["edge_count"] / stats["node_count"]
        if has_conf and stats["edge_count"] > 0:
            # SQL aggregates instead of materializing all confidences into Python.
            row = conn.execute(
                "SELECT COUNT(*), "
                "       SUM(CASE WHEN confidence >= 0.5 THEN 1 ELSE 0 END) "
                "FROM edges WHERE type IN ('CALLS','IMPORTS') AND confidence IS NOT NULL"
            ).fetchone()
            total_conf = row[0] or 0
            high_count = row[1] or 0
            if total_conf > 0:
                stats["high_conf_frac"] = high_count / total_conf
            # Approximate percentiles via NTILE without full sort.
            p50_row = conn.execute(
                "SELECT confidence FROM edges "
                "WHERE type IN ('CALLS','IMPORTS') AND confidence IS NOT NULL "
                "ORDER BY confidence LIMIT 1 OFFSET ?",
                (total_conf // 2,),
            ).fetchone()
            p90_row = conn.execute(
                "SELECT confidence FROM edges "
                "WHERE type IN ('CALLS','IMPORTS') AND confidence IS NOT NULL "
                "ORDER BY confidence LIMIT 1 OFFSET ?",
                (int(total_conf * 0.9),),
            ).fetchone()
            if p50_row:
                stats["conf_p50"] = p50_row[0]
            if p90_row:
                stats["conf_p90"] = p90_row[0]
    except sqlite3.Error:
        pass
    return stats


def _dynamic_max_hop(stats: dict) -> int:
    """Adapt BFS depth to graph density — dynamic, not hardcoded.

    Sparse graphs (avg_degree < 3): 3 hops — need deeper traversal to reach
    anything useful. Verified edges dominate → low false-positive risk.
    Medium graphs (3-10): 2 hops — standard.
    Dense graphs (avg_degree > 10): 2 hops but with tighter confidence floor
    (handled by _dynamic_conf_floor). Going deeper in a dense graph explodes
    the candidate set without adding signal.

    RepoGraph (ICLR 2025): k=1 ego-graph is strongest single hop; diminishing
    returns beyond k=2 for dense graphs. KGCompass (2025): sparse entity
    graphs need deeper traversal (3+ hops) for 89.7% recovery.
    """
    deg = stats.get("avg_degree", 0.0)
    if deg < 3.0:
        return 3
    return 2


def _dynamic_conf_floor(stats: dict) -> float:
    """Adapt confidence admission floor to the graph's confidence distribution.

    High-quality graphs (conf_p50 >= 0.8): floor at 0.6 — most edges are
    reliable, a higher floor keeps only the best.
    Mixed graphs (conf_p50 0.3-0.8): floor at 0.5 — standard.
    Low-quality graphs (conf_p50 < 0.3): floor stays at 0.5 — going below 0.5
    admits speculative name_match edges, which creates noise. Better to find
    fewer candidates than to flood with false positives.
    Correct-or-quiet: the floor NEVER drops below 0.5 (the _NAME_MATCH_FLOOR
    from curation_map). Noise is worse than silence.
    """
    p50 = stats.get("conf_p50", 1.0)
    if p50 >= 0.8:
        return 0.6
    return _NAME_MATCH_FLOOR  # 0.5 — the categorical minimum


@dataclass(frozen=True)
class ScopeChain:
    """A connected subgraph of files that should be edited together.

    files: ordered list of file paths in the chain (source → target direction).
    edges: list of (src_file, dst_file, edge_type, symbol_pair) connecting them.
    confidence: minimum edge confidence in the chain (weakest link).
    description: human-readable one-liner describing the chain.

    Research: co-change analysis (Zimmermann+ ICSE 2004) — files that change
    together in commits form edit scope chains. This is the GRAPH version: files
    connected by call/import edges from anchor symbols form a structural scope.
    Addresses the 32% INCOMPLETE_SCOPE failures: agent finds 1 file but the fix
    needs 2-8 connected files.
    """
    files: list[str]
    edges: list[tuple[str, str, str, str]]
    confidence: float
    description: str


def _build_scope_chains(
    candidates: list["Candidate"],
    conn: sqlite3.Connection,
    has_conf: bool,
    max_chains: int = 3,
) -> list[ScopeChain]:
    """Extract scope chains from verified candidates — connected file subgraphs.

    For every pair of top candidates, check if they share a direct CALLS/IMPORTS
    edge. If so, group them into a chain. This surfaces "this fix spans A → B → C"
    for the agent, addressing incomplete-scope failures.

    Only uses high-confidence edges (verified/import) — a scope chain backed by
    speculative name_match would misdirect worse than no chain.
    """
    if len(candidates) < 2:
        return []

    top_files = [c.file_path for c in candidates[:8]]
    if not top_files:
        return []

    conf_sel = "e.confidence" if has_conf else "1.0"
    try:
        # Get all edges between top candidate files
        ph = ",".join("?" for _ in top_files)
        rows = conn.execute(
            f"""
            SELECT DISTINCT ns.file_path, nt.file_path, e.type,
                   ns.name, nt.name, {conf_sel}, e.resolution_method
            FROM edges e
            JOIN nodes ns ON e.source_id = ns.id
            JOIN nodes nt ON e.target_id = nt.id
            WHERE ns.file_path IN ({ph}) AND nt.file_path IN ({ph})
              AND ns.file_path != nt.file_path
              AND e.type IN ('CALLS','IMPORTS')
            """,
            tuple(top_files) + tuple(top_files),
        ).fetchall()
    except sqlite3.Error:
        return []

    if not rows:
        return []

    # Build adjacency from verified edges only
    adj: dict[str, list[tuple[str, str, str, float]]] = {}
    for src_fp, dst_fp, etype, src_name, dst_name, conf, method in rows:
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        verified = _is_verified(method)
        if not verified and conf_f < _NAME_MATCH_FLOOR:
            continue
        sym_pair = f"{src_name} → {dst_name}"
        adj.setdefault(src_fp, []).append((dst_fp, etype, sym_pair, conf_f))

    # BFS from each top file to find connected components
    chains: list[ScopeChain] = []
    visited_files: set[str] = set()

    for start_file in top_files:
        if start_file in visited_files:
            continue
        chain_files = [start_file]
        chain_edges: list[tuple[str, str, str, str]] = []
        chain_conf = 1.0
        queue = [start_file]
        visited_files.add(start_file)

        while queue:
            current = queue.pop(0)
            for dst, etype, sym_pair, conf_f in adj.get(current, []):
                if dst not in visited_files and dst in top_files:
                    visited_files.add(dst)
                    chain_files.append(dst)
                    chain_edges.append((current, dst, etype, sym_pair))
                    chain_conf = min(chain_conf, conf_f)
                    queue.append(dst)

        if len(chain_files) >= 2:
            desc_parts = []
            for src, dst, etype, sym in chain_edges:
                src_base = os.path.basename(src)
                dst_base = os.path.basename(dst)
                desc_parts.append(f"{src_base} → {dst_base} ({sym})")
            chains.append(ScopeChain(
                files=chain_files,
                edges=chain_edges,
                confidence=chain_conf,
                description="; ".join(desc_parts),
            ))

    chains.sort(key=lambda c: (-len(c.files), -c.confidence))
    return chains[:max_chains]


def localize(
    issue_text: str,
    graph_db: str,
    *,
    issue_anchors: IssueAnchors | None = None,
    max_hop: int = 2,
    top_k: int = 8,
    repo_root: str = "",
) -> LocalizerResult:
    """RETRIEVE (grep-grade recall) -> TRAVERSE (graph depth) -> RERANK -> GATE.

    Seeding is TWO-STAGE: (1) exact symbol-name match (the original path),
    then (2) grep-to-seed — run grep for issue tokens, map hit files to
    enclosing graph nodes, add those as BFS seeds. Stage 2 gives GT at least
    grep's recall, then the graph rerank adds depth grep cannot.

    BFS depth and confidence floor are DYNAMIC — adapted per-graph based on
    density and confidence distribution (_dynamic_max_hop, _dynamic_conf_floor).
    """
    import math

    if not graph_db or not os.path.exists(graph_db):
        return LocalizerResult([], [], 0.0, False, "no_graph_db")

    if issue_anchors is None:
        try:
            issue_anchors = extract_issue_anchors(issue_text, graph_db)
        except Exception:
            issue_anchors = IssueAnchors()

    anchors = {a for a in issue_anchors.symbols if len(a) >= _MIN_ANCHOR_LEN}
    if not anchors:
        return LocalizerResult([], [], 0.0, False, "no_anchor_hit")

    conn = _open_ro(graph_db)
    if conn is None:
        try:
            conn = sqlite3.connect(graph_db)
        except sqlite3.Error:
            return LocalizerResult([], [], 0.0, False, "graph_open_failed")

    try:
        has_conf, has_method = _has_columns(conn)
        # trust_tier column (schema v15.2+): when present, a SUPPRESSED edge is
        # HARD-EXCLUDED at admission per the categorical filter (CLAUDE.md edge
        # rule + Pillar 4 "confidence-gated AT THE FILTER LEVEL"). Detected locally
        # because the shared _has_columns only reports (confidence, method).
        try:
            _edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        except sqlite3.Error:
            _edge_cols = set()
        has_trust_tier = "trust_tier" in _edge_cols

        # DYNAMIC BFS CALIBRATION: adapt hop depth and confidence floor to
        # THIS graph's density and quality (Pillar 1: dynamic, not hardcoded).
        _stats = _graph_stats(conn, has_conf)
        _dyn_hop = min(max_hop, _dynamic_max_hop(_stats))
        _dyn_conf = _dynamic_conf_floor(_stats)

        seeds = _seed_node_rows(conn, anchors)

        # GREP-TO-SEED: dynamically gated by seed QUALITY, not count.
        # Three signals compose the gate (hybrid, ≥3 signals):
        #   1. Diversity: how many distinct files are seeds from?
        #   2. Coverage: what fraction of issue tokens are covered by seeds?
        #   3. Confidence: do any seeds have verified-edge backing?
        # The gate produces a quality score [0,1]. Grep runs when quality
        # is below the per-task MEDIAN of what "good" seeding looks like —
        # i.e., dynamically, not against a hardcoded floor.
        # Grep is additive (can never remove seeds), so even at high quality
        # it's safe — it just adds less. The composite scoring downstream
        # handles the rest.
        _grep_seed_used = False
        terms = _issue_terms(issue_text)
        _seed_files = set(fp for _, _, fp in seeds)
        _seed_diversity = len(_seed_files)
        _n_terms = max(len(terms), 1)
        _covered_terms = {s[1].lower() for s in seeds} & {t.lower() for t in terms}
        _anchor_coverage = len(_covered_terms) / _n_terms
        # Diversity score: tanh normalizes so 5+ files → ~1.0, 1 file → ~0.2
        import math
        _diversity_score = math.tanh(_seed_diversity / 3.0)
        # Confidence score: fraction of seeds with a verified edge backing
        _verified_seed_files = set()
        if has_method:
            for _, sname, sfp in seeds:
                try:
                    _v = conn.execute(
                        "SELECT COUNT(*) FROM edges e JOIN nodes n ON n.id = e.source_id "
                        "WHERE n.file_path = ? AND e.resolution_method IN ('import','same_file','lsp') "
                        "UNION ALL "
                        "SELECT COUNT(*) FROM edges e JOIN nodes n ON n.id = e.target_id "
                        "WHERE n.file_path = ? AND e.resolution_method IN ('import','same_file','lsp') "
                        "LIMIT 1",
                        (sfp, sfp),
                    ).fetchone()
                    if _v and _v[0] > 0:
                        _verified_seed_files.add(sfp)
                except sqlite3.Error:
                    pass
        _conf_score = len(_verified_seed_files) / max(_seed_diversity, 1)
        # Composite seed quality: 3 signals, equal weight
        _seed_quality = (_diversity_score + _anchor_coverage + _conf_score) / 3.0
        # Gate: grep adds value when quality < 0.5 (below the midpoint).
        # When quality ≥ 0.5, grep still runs but with a reduced seed limit
        # (fewer candidates, less noise). Truly zero-gate would always run
        # at full capacity, which wastes time on well-seeded tasks.
        _grep_limit = 20 if _seed_quality < 0.5 else 8
        if repo_root:
            try:
                grep_seeds = _grep_to_seeds(terms, repo_root, conn, max_seeds=_grep_limit)
                if grep_seeds:
                    existing_ids = {s[0] for s in seeds}
                    for gs in grep_seeds:
                        if gs[0] not in existing_ids:
                            seeds.append(gs)
                            existing_ids.add(gs[0])
                    _grep_seed_used = True
            except Exception:
                pass

        # FTS5-TO-SEED (mechanism C): BM25 retrieval over the nodes_fts
        # virtual table. Matches grep's recall by searching function names,
        # signatures, qualified names, and file paths — but ranks by
        # relevance. FTS5 candidates are MERGED with name-match + grep seeds.
        # Graceful fallback: returns [] when nodes_fts doesn't exist.
        #
        # Research: BLUiR (ASE 2013) — structured field-level lexical
        # anchoring beats flat-blob BM25. FTS5 over nodes is structured.
        _fts5_score_by_file: dict[str, float] = {}
        _fts5_seed_used = False
        try:
            fts5_hits = _fts5_candidates(conn, terms, limit=50)
            if fts5_hits:
                existing_ids = {s[0] for s in seeds}
                for nid, name, fp, bm25_score in fts5_hits:
                    # Track BM25 score per file (best across nodes in file).
                    if fp not in _fts5_score_by_file or bm25_score > _fts5_score_by_file[fp]:
                        _fts5_score_by_file[fp] = bm25_score
                    # Add as BFS seed if not already present.
                    if nid not in existing_ids:
                        seeds.append((nid, name, fp))
                        existing_ids.add(nid)
                _fts5_seed_used = True
        except Exception:
            pass

        if not seeds:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            return LocalizerResult([], list(anchors), 0.0, False, "no_anchor_hit")

        # Seed files themselves are hop-0 candidates: the issue named a symbol that
        # lives there. Witness = self-anchor (the named symbol is defined here).
        witnesses_by_file: dict[str, list[Witness]] = {}

        # Anchor SUBJECT position: where each anchor symbol first appears in the
        # issue text. The reporter typically names the BROKEN function as the
        # subject (earliest mention) — e.g. "set_fields does not parse" puts
        # set_fields before set_parse. This is a deterministic, generalized
        # tiebreaker between two co-witnessed seed files (importer.py defines
        # set_fields, db.py defines set_parse — both verified-witnessed; the one
        # whose anchor is the issue SUBJECT wins). Lower position = earlier =
        # stronger subject. Files with no defined anchor get a large sentinel.
        _it_lower = (issue_text or "").lower()
        _anchor_pos: dict[str, int] = {}
        for a in anchors:
            idx = _it_lower.find(a.lower())
            _anchor_pos[a] = idx if idx >= 0 else 10**9

        seed_ids = [s[0] for s in seeds]
        seed_name_by_id = {s[0]: s[1] for s in seeds}

        for _, name, fp in seeds:
            # A DEFINES seed is a NAME MATCH: "the issue token equals a symbol
            # defined in this file". For a DISTINCTIVE symbol (set_fields,
            # aware_now, _to_geo) that is strong localization evidence -> verified
            # fact. For a GENERIC symbol (__format__, __init__, a dunder) it is
            # LAUNDERING: __format__ is defined in many files, so a same-name file
            # (loguru _recattrs.py) must NOT be stamped [VERIFIED] and tie the gold
            # on the verified-first sort. Non-generic stays a verified fact;
            # generic drops to name_match-grade so has_verified_witness / the
            # confidence gate / the [VERIFIED] tier cannot launder it.
            # (audit: defines-witness-stamped-verified; .claude/CLAUDE.md Pillar 3.)
            # Demote a HOMONYM definition out of [VERIFIED] (data-derived, repo P95
            # def-count — is_seed_pollutant), not a hardcoded generic list. __format__
            # in many files, or a project `Config` defined in 20 files, must NOT be
            # stamped a verified DEFINES fact and tie the gold on the verified-first
            # sort; a UNIQUELY-defined domain symbol stays verified even when highly
            # called (a unique definition is unambiguous — in-degree is NOT a demotion
            # signal here; Step-2 finding #1). Aider `len(defines[ident])>5: mul*=0.1`
            # generalized to per-repo P95; never PROMOTE on uniqueness.
            _def_verified = not is_seed_pollutant(name, conn)
            witnesses_by_file.setdefault(fp, []).append(
                Witness(
                    file_path=fp, anchor=name, edge_type="DEFINES",
                    direction="defines_anchor", verified=_def_verified,
                    confidence=1.0 if _def_verified else 0.45,
                    hop=0, src_symbol=name, dst_symbol=name,
                )
            )

        # ---- TRAVERSE: 1..max_hop BFS over CALLS/IMPORTS, both directions ----
        # Frontier of node-ids; each hop pulls neighbors and records a witness on
        # the NEIGHBOR's file. We follow neighbor node-ids to extend the BFS, but
        # the witness is always anchored to the original seed symbol semantics.
        frontier_ids = list(seed_ids)
        # Map a frontier node-id -> the seed anchor name it descends from, so a
        # 2-hop witness still cites the ISSUE anchor, not an intermediate symbol.
        anchor_of_id: dict[int, str] = {nid: seed_name_by_id[nid] for nid in seed_ids}
        # Map node-id -> the symbol name at that node (for src/dst rendering).
        name_of_id: dict[int, str] = dict(seed_name_by_id)
        visited_ids: set[int] = set(seed_ids)

        for hop in range(1, _dyn_hop + 1):
            if not frontier_ids:
                break
            # OUT edges (frontier symbol CALLS/IMPORTS neighbor) and IN edges
            # (neighbor CALLS/IMPORTS frontier symbol). We need neighbor node-ids
            # to continue BFS, so re-query with ids selected.
            next_ids: list[int] = []
            for direction, edge_dir in (("out", "called_by_anchor"), ("in", "calls_anchor")):
                if direction == "out":
                    match_col, join_col = "e.source_id", "e.target_id"
                else:
                    match_col, join_col = "e.target_id", "e.source_id"
                conf_sel = "e.confidence" if has_conf else "1.0"
                method_sel = "e.resolution_method" if has_method else "''"
                tier_sel = "e.trust_tier" if has_trust_tier else "''"
                for i in range(0, len(frontier_ids), 300):
                    chunk = frontier_ids[i : i + 300]
                    ph = ",".join("?" for _ in chunk)
                    sql = (
                        f"SELECT {match_col} AS frontier_id, {join_col} AS nbr_id, "
                        f"n.name, n.file_path, e.type, {conf_sel}, {method_sel}, {tier_sel} "
                        f"FROM edges e JOIN nodes n ON {join_col} = n.id "
                        f"WHERE {match_col} IN ({ph}) "
                        f"AND e.type IN ('CALLS','IMPORTS') AND n.is_test = 0"
                    )
                    try:
                        rows = conn.execute(sql, chunk).fetchall()
                    except sqlite3.Error:
                        continue
                    for fr_id, nbr_id, nbr_name, nbr_file, etype, conf, method, tier in rows:
                        if nbr_id is None or nbr_file is None:
                            continue
                        nbr_id = int(nbr_id)
                        nbr_file = _normalize(str(nbr_file))
                        nbr_name = str(nbr_name or "")
                        try:
                            conf_f = float(conf) if conf is not None else 0.0
                        except (TypeError, ValueError):
                            conf_f = 0.0
                        verified = _is_verified(method)

                        # ---- CATEGORICAL ADMISSION FILTER (single source of truth)
                        # Reuse curation_map's rule (curation_map.py:113): admit IFF
                        # the edge is a FACT (deterministic resolution_method) OR
                        # confidence >= _NAME_MATCH_FLOOR (0.5). A trust_tier =
                        # 'SUPPRESSED' edge is HARD-EXCLUDED. This drops 5+-candidate
                        # / conf<0.5 name_match noise AT ADMISSION so it can never
                        # surface a junk candidate file as an (unverified) witness.
                        # CLAUDE.md edge-filter rule + Pillar 4 (.claude/CLAUDE.md:24
                        # "confidence-gated AT THE FILTER LEVEL"). Without this the BFS
                        # rolled its own verified/name_match split and laundered
                        # suppressed edges into the candidate list.
                        if str(tier or "").strip().upper() == "SUPPRESSED":
                            continue
                        if not verified and conf_f < _dyn_conf:
                            continue

                        # Stdlib-shadow guard (RepoGraph stdlib filter): drop an
                        # edge whose neighbor name is a stdlib attribute call of an
                        # anchor (os.walk -> project walk). We approximate by
                        # dropping a neighbor whose bare name collides with a
                        # stdlib head's typical attribute AND is name_match.
                        frontier_anchor = anchor_of_id.get(int(fr_id), "")
                        src_name = name_of_id.get(int(fr_id), frontier_anchor)
                        if not verified and nbr_name and nbr_name in _STDLIB_ATTRS:
                            continue

                        if edge_dir == "calls_anchor":
                            src_sym, dst_sym = nbr_name, src_name
                        else:
                            src_sym, dst_sym = src_name, nbr_name

                        witnesses_by_file.setdefault(nbr_file, []).append(
                            Witness(
                                file_path=nbr_file,
                                anchor=frontier_anchor or src_name,
                                edge_type=str(etype or "CALLS"),
                                direction=edge_dir,
                                verified=verified,
                                confidence=conf_f,
                                hop=hop,
                                src_symbol=src_sym,
                                dst_symbol=dst_sym,
                            )
                        )
                        if nbr_id not in visited_ids:
                            visited_ids.add(nbr_id)
                            anchor_of_id[nbr_id] = frontier_anchor or src_name
                            name_of_id[nbr_id] = nbr_name
                            next_ids.append(nbr_id)
            frontier_ids = next_ids

        if not witnesses_by_file:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            return LocalizerResult([], list(anchors), 0.0, False, "no_witness",
                                   graph_stats=_stats)

        # ---- PATH DECAY SCORING (KGCompass-style) ----
        # Dijkstra-style BFS from ALL seed nodes. Edge weight = 1/confidence,
        # so verified import edges (1.0) are cheap and speculative name_match
        # edges (0.4) are expensive. Score = beta^cost. This adds a CONTINUOUS
        # decay signal on top of the discrete hop count in witnesses.
        _path_decay_by_file: dict[str, float] = {}
        try:
            _path_decay_by_file = _path_decay_scores(
                conn, seed_ids, has_conf,
                max_hop=_dyn_hop, beta=0.85, min_edge_conf=_dyn_conf,
            )
        except Exception:
            pass

        # ---- RERANK: composite (BM25 + path_decay + witness + lexical + degree) ----
        all_files = set(witnesses_by_file.keys())
        degrees = _file_degrees(conn, all_files)
        _has_conf_for_chains = has_conf
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Per-file SUBJECT position: the earliest issue-text position of any anchor
    # this file DEFINES (hop-0). A file defining the subject function (set_fields,
    # named first) gets a lower position than one defining the object (set_parse).
    _subject_pos_by_file: dict[str, int] = {}
    for fp, wits in witnesses_by_file.items():
        best = 10**9
        for w in wits:
            if w.direction == "defines_anchor":
                best = min(best, _anchor_pos.get(w.anchor, 10**9))
        _subject_pos_by_file[fp] = best

    # Rank the DEFINING files by subject position so the earliest-mentioned-anchor
    # file gets the full subject bonus and later ones decay. Files that define no
    # anchor (pure graph neighbors) get 0. Deterministic, generalized.
    _defining_files = sorted(
        (fp for fp, p in _subject_pos_by_file.items() if p < 10**9),
        key=lambda fp: (_subject_pos_by_file[fp], fp),
    )
    _subject_bonus_by_file: dict[str, float] = {
        fp: 1.0 / (1.0 + rank) for rank, fp in enumerate(_defining_files)
    }

    # Normalize BM25 scores to [0, 1] over the candidate set for composite.
    _bm25_vals = [v for v in _fts5_score_by_file.values() if v > 0]
    _bm25_max = max(_bm25_vals) if _bm25_vals else 1.0

    # Normalize path decay scores to [0, 1] over the candidate set.
    _decay_vals = [v for v in _path_decay_by_file.values() if v > 0]
    _decay_max = max(_decay_vals) if _decay_vals else 1.0

    candidates: list[Candidate] = []
    _cand_subject_pos: dict[str, int] = {}
    for fp, wits in witnesses_by_file.items():
        best_strength = max((w.strength() for w in wits), default=0.0)
        # Structured lexical (BLUiR): issue terms intersecting the file's own
        # path/symbol identifiers (basename stem + every witness symbol name).
        stem = os.path.splitext(os.path.basename(fp))[0].lower()
        symset = {stem}
        for w in wits:
            symset.add(w.src_symbol.lower())
            symset.add(w.dst_symbol.lower())
        lex_hits = sum(1 for t in terms if any(t == s or t in s or s in t for s in symset if len(s) > 2))
        lex_norm = min(1.0, lex_hits / 5.0)
        deg = degrees.get(fp, 0)
        deg_norm = math.tanh(deg / _HUB_SCALE)
        subject_bonus = _subject_bonus_by_file.get(fp, 0.0)

        # NEW composite signals: BM25 retrieval score + path decay.
        bm25_raw = _fts5_score_by_file.get(fp, 0.0)
        bm25_norm = (bm25_raw / _bm25_max) if _bm25_max > 0 else 0.0
        decay_raw = _path_decay_by_file.get(fp, 0.0)
        decay_norm = (decay_raw / _decay_max) if _decay_max > 0 else 0.0

        _raw_score = (
            W_BM25 * bm25_norm
            + W_PATH_DECAY * decay_norm
            + W_WITNESS * best_strength
            + W_LEX * lex_norm
            + W_SUBJECT * subject_bonus
            + W_DEGREE * deg_norm
            - W_GEN * (1.0 if _is_generated(fp) else 0.0)
        )
        # Normalize so the positive weights sum to 1.0 — prevents score
        # inflation from compressing dynamic_cutoff gaps.
        _weight_sum = W_BM25 + W_PATH_DECAY + W_WITNESS + W_LEX + W_SUBJECT + W_DEGREE
        score = _raw_score / _weight_sum if _weight_sum > 0 else _raw_score
        candidates.append(
            Candidate(
                file_path=fp,
                score=round(score, 6),
                witnesses=sorted(wits, key=lambda w: -w.strength()),
                lex_hits=lex_hits,
                degree=deg,
                confidence=round(best_strength, 6),
            )
        )
        _cand_subject_pos[fp] = _subject_pos_by_file.get(fp, 10**9)

    # SWERank hard-negative ordering: among candidates, a verified-witness file
    # MUST outrank a name_match-only or witness-less one. Then break ties by:
    #   score desc -> SUBJECT position asc (the issue-subject file wins between
    #   two co-witnessed seeds) -> path asc (deterministic).
    candidates.sort(
        key=lambda c: (
            0 if c.has_verified_witness else 1,
            -c.score,
            _cand_subject_pos.get(c.file_path, 10**9),
            c.file_path,
        )
    )
    candidates = candidates[:top_k]

    # ---- SCOPE CHAINS (structural edit-scope from graph edges) ----
    # Opens its own connection — the BFS conn is already closed above.
    _chains: list[ScopeChain] = []
    try:
        _sc_conn = sqlite3.connect(graph_db)
        try:
            _chains = _build_scope_chains(candidates, _sc_conn, _has_conf_for_chains)
        finally:
            _sc_conn.close()
    except Exception:
        pass

    # ---- CONFIDENCE GATE (data-derived, per-task) ----
    best = candidates[0]
    if not best.has_verified_witness:
        return LocalizerResult(
            candidates, list(anchors), best.confidence, False, "top_unverified",
            scope_chains=_chains, graph_stats=_stats,
        )

    verified = [c for c in candidates if c.has_verified_witness]
    scores = sorted((c.score for c in candidates), reverse=True)

    if len(candidates) == 1:
        confident, gate_reason = True, "single_verified_candidate"
    elif len(verified) >= 2 and all(c.score > 0 for c in verified):
        confident, gate_reason = True, f"verified_cluster(n={len(verified)})"
    else:
        _dt = dynamic_cutoff(list(scores))
        confident = bool(_dt.tiers) and _dt.tiers[0] == "high"
        _top_tier = _dt.tiers[0] if _dt.tiers else "none"
        gate_reason = f"top_tier={_top_tier} median={_dt.median:.3f}"

    return LocalizerResult(
        candidates, list(anchors), best.confidence, confident, gate_reason,
        scope_chains=_chains, graph_stats=_stats,
    )
