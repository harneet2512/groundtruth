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
from dataclasses import dataclass

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _has_columns,
    _open_ro,
)

# Stdlib module heads whose attribute calls (os.walk, json.load, ...) the indexer
# name-matches to a same-named PROJECT function. Reused conceptually from
# v1r_brief._STDLIB_MODULES; kept local so this module has no import cycle back
# into v1r_brief. Repo- and language-agnostic.
_STDLIB_HEADS: frozenset[str] = frozenset(
    {
        "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
        "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
        "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
        "functools", "operator", "collections", "subprocess", "contextlib",
        "abc", "enum", "asyncio", "threading", "queue", "struct", "socket",
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
W_DEGREE = 0.10
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
            w = max(edge_wits, key=lambda x: x.strength())
            rel = "calls" if w.direction == "calls_anchor" else "called by"
            tag = "" if w.verified else " (unverified)"
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


def localize(
    issue_text: str,
    graph_db: str,
    *,
    issue_anchors: IssueAnchors | None = None,
    max_hop: int = 2,
    top_k: int = 8,
) -> LocalizerResult:
    """ANCHOR -> TRAVERSE -> RERANK -> CONFIDENCE-GATE.

    Returns a LocalizerResult. When no issue symbol resolves to a graph node
    (no anchor hit), or graph_db is unreadable, returns an EMPTY, non-confident
    result so the caller emits the honest grep-fallback (correct-or-quiet).
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

        seeds = _seed_node_rows(conn, anchors)
        if not seeds:
            return LocalizerResult([], list(anchors), 0.0, False, "no_anchor_hit")

        # Seed files themselves are hop-0 candidates: the issue named a symbol that
        # lives there. Witness = self-anchor (the named symbol is defined here).
        terms = _issue_terms(issue_text)
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
            witnesses_by_file.setdefault(fp, []).append(
                Witness(
                    file_path=fp, anchor=name, edge_type="DEFINES",
                    direction="defines_anchor", verified=True, confidence=1.0,
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

        for hop in range(1, max_hop + 1):
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
                for i in range(0, len(frontier_ids), 300):
                    chunk = frontier_ids[i : i + 300]
                    ph = ",".join("?" for _ in chunk)
                    sql = (
                        f"SELECT {match_col} AS frontier_id, {join_col} AS nbr_id, "
                        f"n.name, n.file_path, e.type, {conf_sel}, {method_sel} "
                        f"FROM edges e JOIN nodes n ON {join_col} = n.id "
                        f"WHERE {match_col} IN ({ph}) "
                        f"AND e.type IN ('CALLS','IMPORTS') AND n.is_test = 0"
                    )
                    try:
                        rows = conn.execute(sql, chunk).fetchall()
                    except sqlite3.Error:
                        continue
                    for fr_id, nbr_id, nbr_name, nbr_file, etype, conf, method in rows:
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

                        # Stdlib-shadow guard (RepoGraph stdlib filter): drop an
                        # edge whose neighbor name is a stdlib attribute call of an
                        # anchor (os.walk -> project walk). We approximate by
                        # dropping a neighbor whose bare name collides with a
                        # stdlib head's typical attribute AND is name_match.
                        frontier_anchor = anchor_of_id.get(int(fr_id), "")
                        src_name = name_of_id.get(int(fr_id), frontier_anchor)
                        if not verified and nbr_name and nbr_name in _STDLIB_HEADS:
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
            return LocalizerResult([], list(anchors), 0.0, False, "no_witness")

        # ---- RERANK: composite (witness + lexical + degree) ----
        all_files = set(witnesses_by_file.keys())
        degrees = _file_degrees(conn, all_files)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
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

        score = (
            W_WITNESS * best_strength
            + W_LEX * lex_norm
            + W_SUBJECT * subject_bonus
            + W_DEGREE * deg_norm
        )
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

    # ---- CONFIDENCE GATE (data-derived, per-task) ----
    # Precondition (correct-or-quiet): the top candidate MUST carry a VERIFIED
    # witness. A name_match-only top is never confident — that is exactly the
    # beets-5495 harm (a 0.0-confidence lexical guess rendered as the answer).
    best = candidates[0]
    if not best.has_verified_witness:
        return LocalizerResult(
            candidates, list(anchors), best.confidence, False, "top_unverified"
        )

    verified = [c for c in candidates if c.has_verified_witness]
    scores = sorted((c.score for c in candidates), reverse=True)

    if len(candidates) == 1:
        confident, gate_reason = True, "single_verified_candidate"
    elif len(verified) >= 2 and all(c.score > 0 for c in verified):
        # MULTIPLE files carry a verified structural witness (e.g. importer.py
        # defines set_fields, db.py defines set_parse, and they CALL each other).
        # The whole verified cluster is high-confidence — the issue genuinely
        # touches both. Do NOT demand a score gap here: the gap-vs-median test is
        # for separating a single strong candidate from weak lexical noise, not
        # for splitting two equally-grounded verified seeds. We are confident in
        # the localization; the SUBJECT tiebreak already ordered them.
        confident, gate_reason = True, f"verified_cluster(n={len(verified)})"
    else:
        # Exactly one verified file among several witness-less/name_match ones:
        # require the top to clear the per-task MEDIAN by a relative, scale-free
        # margin (data-derived, no hardcoded absolute — the Dynamic pillar).
        median = scores[len(scores) // 2]
        gap = (best.score - median) / best.score if best.score > 0 else 0.0
        confident = gap >= 0.25
        gate_reason = f"gap_vs_median={gap:.2f}" + ("" if confident else " (below 0.25)")

    return LocalizerResult(
        candidates, list(anchors), best.confidence, confident, gate_reason
    )
