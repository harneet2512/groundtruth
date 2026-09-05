"""V1R brief — map-only, inject-once, stay-silent.

Generates a minimal pre-task brief: ranked files + functions + test mappings.
No prose, no constraints, no behavioral nudges.

Uses v7.4 hybrid retrieval (sem + lex + reach + anchor_prox - hub_pen) to
rank candidates, then queries graph.db for top functions and test coverage.
"""

from __future__ import annotations

import os
import re as _re
import sqlite3
import subprocess
import hashlib
import json
from dataclasses import dataclass, field, replace

# Single source of truth for the categorical correct-or-quiet rule lives in
# curation_map: an edge is a caller FACT only when its resolution_method is
# deterministic (compiler/LSP/structurally verified); a name_match edge is NEVER
# a fact, no matter its confidence. Reuse those constants so v1r's caller
# evidence and the <gt-graph-map> obey one identical rule.
from groundtruth.pretask.curation_map import (
    DETERMINISTIC_RESOLUTION_METHODS,
    _DETERMINISTIC_METHODS,
    _NAME_MATCH_FLOOR,
    _has_columns,
    _is_cross_language_pair,
    _nodes_have_language,
)
from groundtruth.pretask.v7_4_brief import V74BriefResult, _w_sem_floor, run_v74
from groundtruth.pretask.contract_map import (
    _callee_sig_args,
    _sanitize_signature,
    contract_line,
    edit_target_callee_contracts,
)

# Symbol-anchored multi-hop graph-witness localizer (the L1 core). This is the
# deterministic graph TRAVERSAL that the old lexical-only candidate path lacked:
# it anchors on issue SYMBOLS, walks graph.db CALLS/IMPORTS from those nodes, and
# returns candidates WITH a structural witness so a witnessed file outranks a
# lexically-similar-but-unwitnessed hard negative (the beets-5495 failure).
from groundtruth.pretask.graph_localizer import (
    LocalizerResult,
    _normalize as _gl_normalize,
    localize,
)


MAX_FILES = 5
MAX_FUNCTIONS_PER_FILE = 3
MAX_BRIEF_TOKENS = 600
EDGE_CONFIDENCE_FLOOR = 0.7

# D1 (CLAUDE.md Core Product Contract: "compact, high-precision"): a single body
# DETAIL line (Contract / Spec / Callers / Calls / Chain / function list) must
# never blow the whole token budget. The store caps a raw signature at 1000 chars
# and a scope-chain "Chain:" body can run to several thousand — a per-line cap is
# the structural enforcement that the file-dropping cap loop cannot provide (it
# only drops WHOLE files and stops at len==1). ~320 chars ≈ 80 tokens keeps a
# multi-clause contract readable while making 5 entries × ~6 lines fit the 600-tok
# rail. Language-agnostic (operates on rendered text, not syntax); the leading
# "   Label: " stays intact, only the trailing detail is elided with "…".
_MAX_BODY_LINE_CHARS = 320


def _clip_body_line(line: str, limit: int = _MAX_BODY_LINE_CHARS) -> str:
    """Cap a single rendered body line to ``limit`` chars, preserving its
    leading indent + "Label:" prefix and eliding the trailing detail with "…".

    Correct-or-quiet: this only ELIDES already-rendered detail (never invents),
    and a line already within budget is returned byte-identical. Rank-neutral:
    it touches presentation only, never which files/functions are selected."""
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


_schema_cache: dict[str, bool] = {}


def _has_confidence(graph_db: str) -> bool:
    if graph_db in _schema_cache:
        return _schema_cache[graph_db]
    try:
        conn = sqlite3.connect(graph_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        conn.close()
        result = "confidence" in cols
    except Exception:
        result = False
    _schema_cache[graph_db] = result
    return result


# Cache of (has_confidence, has_resolution_method) per db so the no-confidence
# categorical-gate branch (BUG-1 fix) probes the schema once.
_method_schema_cache: dict[str, bool] = {}


def _has_resolution_method(graph_db: str) -> bool:
    if graph_db in _method_schema_cache:
        return _method_schema_cache[graph_db]
    try:
        conn = sqlite3.connect(graph_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        conn.close()
        result = "resolution_method" in cols
    except Exception:
        result = False
    _method_schema_cache[graph_db] = result
    return result


# Categorical method gate, built ONCE from the canonical FACT set so a
# no-confidence-column DB still suppresses name_match. Imported, never hardcoded.
_DET_METHOD_INLIST = ",".join(
    "'" + str(m).lower() + "'" for m in sorted(DETERMINISTIC_RESOLUTION_METHODS)
)


def _edge_conf_clause(graph_db: str, alias: str = "e") -> str:
    """Edge-confidence gate as a categorical (dynamic + hybrid + confidence-gated)
    clause, reusing the SAME primitive L3/L3b use (``post_edit._edge_filter_for_db``)
    in place of the flat numeric ``EDGE_CONFIDENCE_FLOOR`` gate.

    ADDITIVE / correct-or-quiet:
    - no ``confidence`` column at all  -> ``""`` (unchanged no-gate behavior),
    - post-merge schema (trust_tier/candidate_count/resolution_method) -> categorical
      3-signal clause (resolution_method strong-set OR unique name_match OR
      CERTIFIED/CANDIDATE tier, never SUPPRESSED),
    - older schema -> numeric ``confidence >= EDGE_CONFIDENCE_FLOOR`` fallback (the
      constant is RETAINED, not deleted, so old-schema behavior is byte-identical).

    Research: PyCG ICSE 2021 (structural resolution methods are the trustworthy
    signal), Anthropic "Writing Effective Tools" 2025 (filter hard upstream),
    Squeez arXiv 2604.04979 2026 (aggressive pre-display filtering).

    BUG-1 (no-confidence-column DB): returning ``""`` here meant NO gate at all,
    so the ``Calls:`` line + neighbor-expansion rendered every name_match target
    as a fact on any DB lacking a ``confidence`` column. Fail-closed: when
    ``confidence`` is absent but ``resolution_method`` exists, fall back to the
    SAME categorical method gate curation_map._neighbors uses (resolution_method
    ∈ DETERMINISTIC_RESOLUTION_METHODS). Only when NEITHER column exists do we
    return ``""`` (last-resort no-gate; the caller marks/suppresses unverified).
    """
    if not _has_confidence(graph_db):
        # No confidence column: gate categorically on resolution_method when present
        # (mirrors curation_map._neighbors ~line 518). name_match is NEVER in the
        # FACT set, so this strips every name_match target from the joined surface.
        if _has_resolution_method(graph_db):
            return f"AND LOWER(TRIM({alias}.resolution_method)) IN ({_DET_METHOD_INLIST})"
        # Neither column exists -> cannot judge provenance. Last-resort no-gate;
        # the categorical FACT cannot be asserted, so consumers must treat the
        # joined rows as unverified (correct-or-quiet at the render layer).
        return ""
    if _has_resolution_method(graph_db):
        # B-3: agree edge-for-edge with the Go closure's admission rule
        # (closure.isVerifiedEdge: method ∈ set AND confidence >= MinEdgeConfidence=0.7).
        # Method-only admitted ambiguity-demoted deterministic edges (import/same_file/lsp
        # @0.6/CANDIDATE) as bare FACTS — the wrong-file "Calls:" lead — diverging from the
        # closure. The AND conjunct keeps genuine facts (conf 1.0/0.9) and drops the 0.6
        # ambiguous picks to the (unverified) render path. EDGE_CONFIDENCE_FLOOR == 0.7.
        return (
            f"AND LOWER(TRIM({alias}.resolution_method)) IN ({_DET_METHOD_INLIST}) "
            f"AND {alias}.confidence >= {EDGE_CONFIDENCE_FLOOR}"
        )
    return f"AND {alias}.confidence >= {EDGE_CONFIDENCE_FLOOR}"


def _file_is_namematch_only(graph_db: str, file_path: str) -> bool:
    """True iff ``file_path`` is touched by edges but NONE are verified — i.e. the
    file's connectivity rests ENTIRELY on name_match (or unknown-provenance) edges.

    This is positive evidence that the file's high rank is a lexical/name_match
    guess, not a structural fact. Used to SUPPRESS the single-candidate
    "Highest-confidence candidate" line on exactly the beets ev1 failure mode
    (pipeline.py was confidently named but had only name_match backing), while NOT
    over-suppressing the common case: when no graph_db / no resolution_method
    column is available we cannot PROVE weakness, so we do not suppress (the
    [VERIFIED] tier + score gap still gate the line). A file with at least one
    verified edge, or with no edges at all (node-local / isolated), returns False.

    Correct-or-quiet applied to the SUPPRESSION decision: only suppress on proven
    weakness, never on absence of evidence.
    """
    if not graph_db or not file_path:
        return False
    try:
        conn = sqlite3.connect(graph_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
            if "resolution_method" not in cols:
                return False  # cannot judge provenance -> do not claim weakness
            det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
            # Total distinct edges incident to a node defined in this file.
            # Use UNION (not OR) to avoid double-counting edges where both
            # endpoints are defined in the same file.
            total = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT e.id FROM edges e
                    JOIN nodes n ON n.id = e.source_id
                    WHERE n.file_path = ?
                    UNION
                    SELECT e.id FROM edges e
                    JOIN nodes n ON n.id = e.target_id
                    WHERE n.file_path = ?
                )
                """,
                (file_path, file_path),
            ).fetchone()[0]
            if not total:
                return False  # no edges at all -> isolated, not "name_match-ranked"
            verified = conn.execute(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT e.id FROM edges e
                    JOIN nodes n ON n.id = e.source_id
                    WHERE n.file_path = ?
                      AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
                    UNION
                    SELECT e.id FROM edges e
                    JOIN nodes n ON n.id = e.target_id
                    WHERE n.file_path = ?
                      AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
                )
                """,
                (file_path, file_path),
            ).fetchone()[0]
            return verified == 0
        finally:
            conn.close()
    except Exception:
        return False  # error -> cannot prove weakness -> do not suppress


@dataclass(frozen=True)
class FileEntry:
    path: str
    score: float
    functions: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    co_changes: list[str] = field(default_factory=list)
    contract: str = ""
    # Deterministic CONTRACT pillar: signature/raises/guards/return-shape of the
    # edit-target function (contract_map). Always-available — fires even on isolated
    # functions; the interface facts the agent must preserve. Empirically these
    # property kinds are in every task db but were delivered nowhere. (2026-05-29)
    contract_props: str = ""
    pattern: str = ""
    spec: str = ""
    # Raw function names (not signatures) for issue-text matching.
    # `functions` stores signatures (`def foo(...) -> T:`) which never match
    # substring against issue text. `function_names` stores bare names.
    function_names: list[str] = field(default_factory=list)
    # Graph-traversal localizer witness (graph_localizer.py): the structural
    # reason this file is a candidate, e.g. "set_fields calls set_parse [CALLS]".
    # Empty when the file entered via lexical/semantic only (witness-less). A
    # verified witness is what lets this file outrank a lexical hard-negative.
    witness: str = ""
    # True iff the witness rests on a DETERMINISTIC edge (verified fact), not a
    # name_match. Drives the [VERIFIED] tier + the confident-line render gate.
    witness_verified: bool = False
    # Best-witness strength 0..1 from the localizer — the per-candidate
    # confidence surfaced to gt_run_summary l1_confidence_score.
    localizer_confidence: float = 0.0
    # v7.4 anchor proximity = min(1.0, n_issue_anchors_within_1_hop / 3.0). An
    # EDGE-INDEPENDENT issue-SUBJECT signal: the file is a direct call-graph
    # neighbour of >=1 symbol named in the issue. Plumbed from the v74 record so
    # _entry_confidence_tier can keep an anchor-matched file (e.g. matplotlib
    # lines.py, anchor_prox=1.0 but witness-less and whose freshly-added gold
    # functions set_xy1/set_xy2 are absent from the ref-count-ranked
    # function_names) out of the [INFO] drop. Without this the one signal that
    # correctly identified gold died at the FileEntry boundary (BUG-3).
    anchor_prox: float = 0.0
    # Candidate-local issue relevance from graph_localizer. This is distinct
    # from witness_verified/edge truth and is the authority for confidence tags.
    relevance_grade: str = ""


def _candidate_acquisition_sources(
    graph_db: str,
    repo_root: str,
    file_path: str,
    resolution_methods: set[str] | frozenset[str],
) -> dict[str, dict[str, object]]:
    """Return candidate-local acquisition lineage without changing delivery.

    Only positive, independently checkable source contributions are emitted.
    Missing legacy columns, an unreadable checkout, or an unresolved partition
    all abstain. A single-repo no-op is emitted only when the indexed candidate
    bytes equal the active checkout bytes. Repeatability is intentionally not
    synthesized here: a single generation cannot prove ``determinism``.
    """
    methods = {
        str(method).strip().lower()
        for method in resolution_methods
        if isinstance(method, str) and str(method).strip()
    }
    deterministic = {str(m).strip().lower() for m in DETERMINISTIC_RESOLUTION_METHODS}
    verified = methods & deterministic
    sources: dict[str, dict[str, object]] = {}
    if verified:
        sources["resolution_honesty"] = {
            "kind": "resolution_methods",
            "methods": sorted(verified),
            "all_verified": methods <= deterministic,
        }
        type_methods = verified & {"type_flow", "import_type"}
        if type_methods:
            sources["type_intelligence"] = {
                "kind": "type_resolution",
                "methods": sorted(type_methods),
            }
        lsp_methods = verified & {"lsp", "lsp_verified"}
        if lsp_methods:
            sources["LSP"] = {
                "kind": "lsp_resolution",
                "methods": sorted(lsp_methods),
            }

    if not graph_db or not file_path:
        return sources
    normalized_path = file_path.replace("\\", "/").lstrip("./").lstrip("/")
    try:
        conn = sqlite3.connect(graph_db)
        try:
            hash_cols = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(file_hashes)").fetchall()
            }
            hash_col = (
                "content_hash"
                if "content_hash" in hash_cols
                else ("hash" if "hash" in hash_cols else "")
            )
            candidate_revision_current = False
            if hash_col:
                row = conn.execute(
                    f"SELECT {hash_col} FROM file_hashes WHERE file_path = ? LIMIT 1",
                    (normalized_path,),
                ).fetchone()
                indexed = str(row[0]).strip().lower() if row and row[0] else ""
                try:
                    with open(os.path.join(repo_root, normalized_path), "rb") as source_file:
                        observed = hashlib.sha256(source_file.read()).hexdigest()
                except OSError:
                    observed = ""
                if _re.fullmatch(r"[0-9a-f]{64}", indexed) and indexed == observed:
                    candidate_revision_current = True
                    sources["freshness_basis"] = {
                        "kind": "content_revision",
                        "indexed_sha256": indexed,
                        "observed_sha256": observed,
                    }

            from groundtruth.index.repo_scope import for_read

            scope = for_read(conn, repo_root)
            if not scope.is_multi_repo and scope.resolved and candidate_revision_current:
                sources["repo_scope"] = {
                    "kind": "repo_partition",
                    "is_multi_repo": False,
                    "resolved": True,
                    "scope_mode": "single_repo_noop",
                    "active_repo_id": None,
                    "candidate_repo_id": None,
                    "candidate_path": normalized_path,
                }
            elif scope.is_multi_repo and scope.resolved and scope.active_repo_id is not None:
                node_cols = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
                }
                if "repo_id" in node_cols:
                    repo_rows = conn.execute(
                        "SELECT DISTINCT repo_id FROM nodes WHERE file_path = ? AND repo_id IS NOT NULL",
                        (normalized_path,),
                    ).fetchall()
                    repo_ids = {int(row[0]) for row in repo_rows if row and row[0] is not None}
                    if repo_ids == {scope.active_repo_id}:
                        sources["repo_scope"] = {
                            "kind": "repo_partition",
                            "is_multi_repo": True,
                            "resolved": True,
                            "active_repo_id": scope.active_repo_id,
                            "candidate_repo_id": scope.active_repo_id,
                        }
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return sources


@dataclass(frozen=True)
class V1RBriefResult:
    files: list[FileEntry]
    brief_text: str
    token_estimate: int
    v74_result: V74BriefResult | None = None
    # --- L1 signal-provenance counts (observability, NOT ranking) ---
    # These let a fail-closed preflight / deep-metrics gate PROVE the brief's
    # localization rests on REAL multi-signal evidence (graph edges + structural +
    # semantic + FTS5) and not a degraded lexical-only / hollow run. A candidate
    # counts toward a signal iff that signal contributed a NONZERO score to it.
    # Defaults keep every existing caller byte-compatible. (instr 2026-06-04)
    # SCOPE (C15, 2026-07-27 — read this before citing any of the four): these four are
    # DELIVERY claims. They count over the RENDERED candidate set (``.files``), joined to
    # the final bytes, and they pair with ``rendered_candidate_count`` /``sem_components``
    # as the numerator/denominator/distribution triple that gate 3b and the absorption
    # contract read. Their unqualified names invite the ACQUISITION reading; for that
    # question read ``acquired_*`` below, NEVER these.
    graph_edge_count: int = 0  # DELIVERED candidates backed by >=1 real graph edge
    semantic_signal_count: int = 0  # DELIVERED candidates with a nonzero semantic/ONNX score
    structural_signal_count: int = (
        0  # DELIVERED candidates with a nonzero structural/graph-reach score
    )
    fts5_signal_count: int = 0  # DELIVERED candidates scored by / entering via FTS5/BM25
    confidence_tier: str = "low"  # HIGH/MEDIUM/LOW from _localization_header
    # --- ACQUISITION counts (C15) — what the LEGS FOUND, independent of delivery ---
    # Counted over the RANKED set (``top_records``), so the brief reduction cannot zero
    # them. WHY THIS FAMILY EXISTS: under GT_BRIEF_MINIMAL + GT_LOC_RESLOT the reducer
    # deletes every localization block, so the DELIVERED set is empty BY CONSTRUCTION and
    # the four fields above all read 0. On run 30297116212 that zero was read as "the
    # acquisition subsystem is dark" and written into the architecture state-of-record —
    # while the SAME run's embedder certificate reported 112 semantic candidates and the
    # production localizer, driven against that run's own graph, produced 50. It was a
    # broken gauge, not a broken subsystem. Both facts are wanted; they must not share a
    # name. These answer "what did the legs find"; the four above answer "what reached
    # the model".
    acquired_graph_edge_count: int = 0
    acquired_semantic_signal_count: int = 0
    acquired_structural_signal_count: int = 0
    acquired_fts5_signal_count: int = 0
    # Per-ranked-candidate acquisition witnesses, populated before any delivery
    # reduction. This is deliberately narrower than ``localization_proof``: it
    # carries only the four acquisition-leg witnesses and makes no rendered-block,
    # contribution-attestation, or receipt claim.
    acquisition_proof: list[dict[str, object]] = field(default_factory=list)
    # --- DELIVERY counts, explicitly named. ``None`` == NOT_EVALUABLE ---
    # Same values as the four legacy fields above, under names that cannot be misread.
    # NOT_EVALUABLE (None) — never 0 — when the minimal/re-slot reduction is what emptied
    # the delivered set: "0 reached the model via the step-0 brief" is then a statement
    # about the re-slot, not about delivery quality, and a 0 there is the same lie under a
    # better name. A genuine 0 (candidates delivered, none carrying the signal) stays 0.
    delivered_graph_edge_count: int | None = None
    delivered_semantic_signal_count: int | None = None
    delivered_structural_signal_count: int | None = None
    delivered_fts5_signal_count: int | None = None
    delivered_candidate_count: int | None = None
    # --- Embedder-CONSUMPTION metrics (instr 2026-06-07, FIELD-NAME CONTRACT) ---
    # Let a fail-closed precheck distinguish "embedder PRESENT" from "embedder
    # CONSUMED": a present-but-unconsumed embedder has effective_w_sem > 0 yet
    # semantic_signal_count == 0 / all-zero sem_components. Measured over the
    # RENDERED candidates (.files), so it reflects exactly what the agent saw.
    effective_w_sem: float = 0.0  # W_SEM actually applied after all zeroing branches (from run_v74)
    rendered_candidate_count: int = 0  # number of rendered/delivered candidates (== len(files))
    k_sem_top: int = 0  # the relative sem-component cap actually used (from run_v74)
    sem_components: list[float] = field(
        default_factory=list
    )  # components['sem'] over rendered candidates
    # Per-rendered-candidate proof payload persisted to substrate ``brief_result.json``.
    # Observability only: lets Stage-1 audits explain why each delivered file ranked
    # where it did without changing the brief text or ranking behavior.
    localization_proof: list[dict[str, object]] = field(default_factory=list)
    # B-30: labels of the brief blocks the DOSE-rail enforcer suppressed to keep the
    # brief within ``max_brief_tokens`` (e.g. "graph-map", "scope-chain", "truncated").
    # Empty on the common under-budget path; observability only, no ranking effect.
    budget_suppressed: list[str] = field(default_factory=list)
    # B-6: per-brief-block delivery receipts — a sidecar map giving each fact-bearing
    # block (localization / obligations / contract / scope / companion) a STABLE,
    # DISTINCT identity so the consumption grader can attribute which BLOCK the agent
    # consumed. Each entry: ``{block_id, fact_class, label, char_span:[start,end],
    # content_hash}``. HOST-SIDE METADATA — NEVER rendered into ``brief_text`` (the
    # brief bytes are byte-identical whether this is populated or not). Empty unless
    # ``GT_BLOCK_RECEIPTS`` explicitly enables them, or ``GT_INSEAM_METRICS``
    # enables them when the dedicated flag is absent. No ranking effect.
    block_receipts: list[dict] = field(default_factory=list)
    # Typed CAP terminal decisions generated while assembling this exact brief.
    # Sidecar only; never rendered into brief_text.
    control_participation: list[dict] = field(default_factory=list)
    # Cluster-2b: the producer's build-time obligations record — the EXACT issue source
    # identity (issue_sha256 + revision) the task-start obligations block was extracted
    # from, plus the extracted obligations digest + count, bound to the delivered
    # obligations block's candidate_id + seal. HOST-SIDE METADATA — never rendered into
    # brief_text (byte-identical whether populated or not). Empty when there is no
    # delivered obligations block or no persisted extraction record (fail-closed: the
    # obligations attestation then stays honestly UNMEASURED). Consumed by the
    # canonical task-start evidence adapter.
    obligations_record: dict = field(default_factory=dict)
    # B-31 (Brief-F9): which token counter produced ``token_estimate`` /governed the
    # DOSE rail — ``"gte-modernbert-bpe"`` (the baked HF BPE vocabulary) or
    # ``"char4-estimate"`` (the char/4 fallback when no tokenizer.json is configured).
    # Observability only (no ranking / brief-byte effect). NOTE: even the "real"
    # counter is gte-modernbert's BPE — a PROXY for the sampled model's tokenizer, not
    # the model's own; treat the count as consistent-and-honest, not exact-per-model.
    tokenizer_used: str = ""


def _provenance_order_clause(
    code_syms: list[str],
    nl_terms: list[str],
    *,
    nl_hoist: bool,
) -> tuple[str, list]:
    """Build the PROVENANCE-AWARE ``ORDER BY`` for the per-file function ranker.

    The lexical-false-positive blend (2026-06-17). Two anchor provenances rank
    differently:

    * ``code_syms`` — names the reporter marked as CODE (backtick/fence,
      ``IssueAnchors.code_symbols``). HIGH confidence: a match HOISTS to the
      front (tier 0), so a low-ref edit target (``set_fields``, ref=0) survives
      the LIMIT — the case the anchor-first sort was built for.
    * ``nl_terms`` — the raw NL word bag. When ``nl_hoist`` is False (the caller
      separated provenance, so these are KNOWN to be prose-only) a name that
      coincidentally matches a word (``start``, ``template``, ``check``) must NOT
      out-rank a structurally-central function — it is only a TIEBREAK *under*
      ``ref_count DESC``. When ``nl_hoist`` is True (LEGACY callers that pass no
      ``code_symbols`` channel, so provenance is unknown) NL terms keep the old
      absolute hoist — back-compat for direct callers and the ``set_fields``
      single-channel guarantee.

    Returns ``(order_by_sql, params)``. The caller binds ``file_path`` FIRST,
    then these params, then any trailing LIMIT param.

    Research: Reformulate-Retrieve-Localize (arXiv:2512.07022, 2025) —
    distinguish code mentions from prose; raw keywords are noisy queries.
    """
    parts: list[str] = []
    params: list = []
    # Code-symbol hoist (always tier 0). In legacy mode the NL hoist joins it at
    # the SAME tier so a single-channel issue-term match still floats to the front.
    hoist_terms = list(code_syms)
    if nl_hoist:
        hoist_terms = sorted(set(hoist_terms) | set(nl_terms))
    if hoist_terms:
        _ph = ",".join("?" * len(hoist_terms))
        parts.append(f"CASE WHEN LOWER(n.name) IN ({_ph}) THEN 0 ELSE 1 END")
        params.extend(hoist_terms)
    # Degree dominates everything below the hoist tier.
    parts.append("ref_count DESC")
    if not nl_hoist and nl_terms:
        # Provenance-known mode: prose-word match is a tiebreak UNDER degree only.
        _ph = ",".join("?" * len(nl_terms))
        parts.append(f"CASE WHEN LOWER(n.name) IN ({_ph}) THEN 0 ELSE 1 END")
        params.extend(nl_terms)
    parts.append("n.name")
    return ", ".join(parts), params


def _top_functions(
    graph_db: str,
    file_path: str,
    limit: int = MAX_FUNCTIONS_PER_FILE,
    issue_terms: set[str] | None = None,
    code_symbols: set[str] | None = None,
) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        # BUG-3: a freshly-added gold function has 0 callers and (often) a name that
        # is not a verbatim issue token, so a pure ``ref_count DESC`` order + LIMIT
        # cuts it before it can surface — Contract/Spec/(funcs) then describe the
        # WRONG (most-central) function. The PROVENANCE-AWARE order (lexical-false-
        # positive blend, 2026-06-17) hoists a CODE-SYMBOL-anchored function (e.g.
        # set_fields, ref=0) ahead of the ref-count cap so it survives, while a bare
        # NL-WORD match (start/template/check) is only a tiebreak UNDER degree — so a
        # ref=1 word-coincidence never beats a ref=32 central function. No-op when no
        # terms are passed (existing positional callers unaffected).
        # nl_hoist: LEGACY callers pass no code_symbols channel (None) -> provenance
        # is unknown, so an issue_terms match keeps the old absolute hoist (the
        # set_fields single-channel guarantee). A caller that DID separate provenance
        # passes code_symbols (even an empty set) -> NL match is demoted to a tiebreak.
        _nl_hoist = code_symbols is None
        _syms = sorted({t.lower() for t in (code_symbols or set()) if t and len(t) > 2})
        _terms = sorted({t.lower() for t in (issue_terms or set()) if t and len(t) > 2})
        if _syms or _terms:
            _order, _oparams = _provenance_order_clause(_syms, _terms, nl_hoist=_nl_hoist)
            rows = conn.execute(
                f"""
                SELECT n.name, n.signature, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' {conf_clause}
                WHERE n.file_path = ?
                  AND n.label IN ('Function', 'Method', 'Class', 'ImplBlock')
                  AND n.is_test = 0
                GROUP BY n.id
                ORDER BY {_order}
                LIMIT ?
                """,
                (file_path, *_oparams, max(limit * 8, 24)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT n.name, n.signature, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' {conf_clause}
                WHERE n.file_path = ?
                  AND n.label IN ('Function', 'Method', 'Class', 'ImplBlock')
                  AND n.is_test = 0
                GROUP BY n.id
                ORDER BY ref_count DESC, n.name
                LIMIT ?
                """,
                (file_path, max(limit * 8, 24)),
            ).fetchall()
        conn.close()
        # Dedup title-line text (signature, else name) preserving rank order, so
        # byte-identical same-named overloads (e.g. three identical
        # "def __format__(self, spec):") collapse to one and the freed slots show
        # distinct functions. Cap AFTER dedup.
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            # D2 (gt_new App D, CLAUDE.md "compact, high-precision"): a SIGNATURE
            # contract is the typed param list + return — never prose. Strip any
            # inline docstring (Annotated[T, Doc("""…""")] / Field(description=…),
            # Javadoc, Rust doc-comment) and cap length via sanitize_signature
            # BEFORE this title becomes the brief's "(funcs)" line. Without it the
            # raw n.signature (capped at 1000 chars by the store) renders a
            # multi-hundred-char docstring wall as entry #1 (FastAPI jsonable_encoder
            # = 1223 chars). General for ANY prose-in-signature codebase, not FastAPI;
            # a short normal signature passes through unchanged.
            title = _sanitize_signature(row[1]) if row[1] else row[0]
            if title in seen:
                continue
            seen.add(title)
            out.append(title)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _top_function_names(
    graph_db: str,
    file_path: str,
    limit: int = MAX_FUNCTIONS_PER_FILE,
    issue_terms: set[str] | None = None,
    code_symbols: set[str] | None = None,
) -> list[str]:
    """Return raw function NAMES (not signatures) for contract lookup.

    Ranking is PROVENANCE-AWARE (lexical-false-positive blend, 2026-06-17), via
    ``_provenance_order_clause``:

    * ``code_symbols`` (backtick/fence provenance, ``IssueAnchors.code_symbols``)
      — a match HOISTS to the front so a low-ref edit target (e.g. ``set_fields``,
      ref=0) SURVIVES the LIMIT, exactly the case the anchor-first sort was built
      for (SWERank ICLR 2025: issue-named entities are the edit target).
    * ``issue_terms`` (raw NL word bag) — a coincidental prose-word match
      (``start``, ``template``, ``check``) is only a TIEBREAK UNDER ``ref_count
      DESC``; it never out-ranks a structurally-central function. Without this
      split, ``span.rs::start`` (ref=1) beat ``new`` (ref=32) — the lexical
      false-positive that led the brief on rust/py/js.
    """
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        # nl_hoist: see _top_functions — None code_symbols channel = legacy absolute
        # hoist (set_fields single-channel guarantee); a provided set = NL is a tiebreak.
        _nl_hoist = code_symbols is None
        _syms = sorted({t.lower() for t in (code_symbols or set()) if t and len(t) > 2})
        _terms = sorted({t.lower() for t in (issue_terms or set()) if t and len(t) > 2})
        if _syms or _terms:
            _order, _oparams = _provenance_order_clause(_syms, _terms, nl_hoist=_nl_hoist)
            rows = conn.execute(
                f"""
                SELECT n.name, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' {conf_clause}
                WHERE n.file_path = ? AND n.label IN ('Function', 'Method', 'Class', 'ImplBlock') AND n.is_test = 0
                GROUP BY n.id
                ORDER BY {_order}
                LIMIT 20
                """,
                (file_path, *_oparams),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT n.name, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' {conf_clause}
                WHERE n.file_path = ? AND n.label IN ('Function', 'Method', 'Class', 'ImplBlock') AND n.is_test = 0
                GROUP BY n.id
                ORDER BY ref_count DESC, n.name
                LIMIT 20
                """,
                (file_path,),
            ).fetchall()
        conn.close()
    except Exception:
        return []

    if not rows:
        return []

    # #60: the SQL CASE above already sorts issue-matched names to the FRONT (THEN 0)
    # using the SINGLE filtered term set `_terms` (len > 2). A second Python partition
    # here used `terms_lower = {t.lower() for t in issue_terms}` — the UNFILTERED set —
    # so a 1-2 char term re-promoted a function the SQL had (correctly) not matched,
    # producing a rank that contradicts the SQL order. The rows are already in the
    # authoritative order; return them directly so ONE filtered ranker decides the
    # order. Generalized (no per-repo logic), correct-or-quiet.
    return [row[0] for row in rows[:limit]]


def _is_test_path(path: str) -> bool:
    """De-dup'd (2026-06-15) to the single canonical predicate
    ``delivery.path_policy.is_test_or_demo``: a TEST **or** DEMO/non-source path is never
    surfaced to the agent. The brief copies previously caught only the TEST half and
    missed DEMO dirs (docs_src/examples), which leaked docs_src/ tutorial files as
    candidate edit targets (fastapi witness). Dir-segment match, never substring.

    Class-A collapse (2026-06-17): the orphan module-level ``_TEST_DIR_SEGMENTS``
    frozenset that used to sit above this function was a DEAD duplicate of
    ``path_policy._TEST_DIR_SEGMENTS`` (had even drifted to carry an extra
    ``test-utils`` segment) — nothing referenced it once this wrapper delegated to
    the canonical predicate. Deleted so ONE segment literal exists in the repo."""
    return _is_test_or_demo(path)


def _issue_relevant_neighbors(
    graph_db: str,
    file_path: str,
    repo_root: str,
    issue_terms: set[str],
    limit: int = 3,
) -> list[str]:
    """Graph neighbors scored by issue relevance, not edge count.

    Queries both callees and callers, then ranks them by how many issue
    keywords appear in their file content.  The agent sees the connections
    most relevant to the current issue — dynamic, not static.
    """
    if not issue_terms:
        return _static_callees(graph_db, file_path, limit)
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        # FIX 2 (2026-06-11, gt_gt §16.5 issue C): same cross-language
        # disqualifier as _static_callees — this UNION (callees + CALLERS) is
        # the surface that promoted the vendored-JS caller of a Python file
        # (aiomonitor tailwind.js, brief entry #2, both runs). Legacy schema
        # (no nodes.language) stays permissive.
        has_lang = _nodes_have_language(conn)
        src_lang_sel = "nsrc.language" if has_lang else "''"
        tgt_lang_sel = "nt.language" if has_lang else "''"
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path, {src_lang_sel}, {tgt_lang_sel}
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ? AND nt.file_path != ? AND nt.is_test = 0
            UNION
            SELECT DISTINCT nsrc.file_path, {src_lang_sel}, {tgt_lang_sel}
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' {conf_clause}
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
            """,
            (file_path, file_path, file_path, file_path),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    scored: list[tuple[str, int]] = []
    seen_neighbors: set[str] = set()
    for neighbor, src_lang, tgt_lang in rows:
        if _is_cross_language_pair(src_lang, tgt_lang):
            continue
        # E1 (Fable 2026-07-05): route the Calls: neighbor through the Class-A path chokepoint.
        # is_test=0 (SQL) alone can leak a vendored/demo path (same-language) or a frozen-graph
        # test node whose is_test flag was never set. is_deliverable = the ONE predicate every
        # other delivery surface uses (no surface re-implements "is this non-source").
        if not _is_deliverable(neighbor):
            continue
        if neighbor in seen_neighbors:
            continue
        seen_neighbors.add(neighbor)
        fpath = os.path.join(repo_root, neighbor)
        try:
            text = open(fpath, encoding="utf-8", errors="ignore").read(200_000).lower()
            hits = sum(1 for t in issue_terms if t in text)
            scored.append((neighbor, hits))
        except OSError:
            scored.append((neighbor, 0))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [f for f, s in scored[:limit] if s > 0] or [f for f, _ in scored[:limit]]


def _static_callees(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        # FIX 2 (2026-06-11, gt_gt §16.5 issue C): this RANKING surface fed the
        # brief's file candidates from CALLS edges WITHOUT the cross-language
        # disqualifier (the fact-filter protects FACT ROWS only) — vendored
        # tailwind.js reached brief entry #2 on a Python repo. Over-fetch, drop
        # cross-language pairs, cap. Legacy schema (no language) -> permissive.
        has_lang = _nodes_have_language(conn)
        src_lang_sel = "nsrc.language" if has_lang else "''"
        tgt_lang_sel = "nt.language" if has_lang else "''"
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path, {src_lang_sel}, {tgt_lang_sel}
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
              AND nt.is_test = 0
            LIMIT ?
            """,
            (file_path, file_path, limit * 4),
        ).fetchall()
        conn.close()
        out: list[str] = []
        for fpath, src_lang, tgt_lang in rows:
            if _is_cross_language_pair(src_lang, tgt_lang):
                continue
            # E1 (Fable 2026-07-05): Class-A path chokepoint — is_test=0 (SQL) misses a
            # same-language vendored/demo path or a frozen-graph test node.
            if not _is_deliverable(fpath):
                continue
            if fpath not in out:
                out.append(fpath)
        return out[:limit]
    except Exception:
        return []


# Retained for backward-compat / external references. The caller gate below no
# longer keys off these thresholds — provenance (resolution_method), not a bare
# confidence cutoff, decides whether a caller is a fact.
CALLER_CONFIDENCE_HI = 0.9
CALLER_CONFIDENCE_LO = 0.7
MAX_CALLERS_PER_FUNC = 2


# ---------------------------------------------------------------------------
# DELIVERY FACT-FILTER — SINGLE-SOURCED in groundtruth.delivery (B1, 2026-06-13).
# The SAME path_policy + name_policy modules are imported by
# artifact_deepswe/gt_mini_patch.py, so the brief's DELIVERY surface and the
# agent-time hook apply IDENTICAL exclusion decisions on identical inputs.
# Two classifiers, FACT-FILTERING ONLY (no ranking/anchor/fusion effect):
#   (a) vendored/minified/generated PATHS (path_policy) — extends the localizer's
#       `_is_generated` W_GEN demote (ranking) to the brief's DELIVERY surface;
#   (b) builtin/dunder-shadow + stdlib-shadow NAMES (name_policy) — a bare builtin
#       call resolves verified_unique when one project symbol shadows the name;
#       the resolver's T2 builtin drop (gt_gt §2.3, index-time) covers QUALIFIED
#       calls only and substrate graphs are frozen, so the consumer fact surface
#       is the operative guard. Correct-or-quiet: exclusion suppresses, never invents.
# ---------------------------------------------------------------------------
from groundtruth.delivery.path_policy import (  # noqa: E402
    is_vendored_path as _is_vendored_path,
    is_minified_file as _is_minified_file,
    is_test_or_demo as _is_test_or_demo,
    is_test_tooling as _is_test_tooling,
    is_deliverable as _is_deliverable,
    test_tooling_roots as _test_tooling_roots,
)
from groundtruth.delivery.name_policy import (  # noqa: E402
    is_builtin_shadow_name as _is_builtin_shadow_name,
    is_stdlib_shadow as _is_stdlib_shadow,
)


def _caller_contract_for_file(
    graph_db: str,
    file_path: str,
    repo_root: str,
    func_names: list[str],
) -> str:
    """Categorical, correct-or-quiet caller evidence for the brief.

    A cross-file caller is rendered as a confident FACT (``name() in file:line
    `code```) ONLY when its edge ``resolution_method`` is deterministic
    (same_file / import / verified_unique / type_flow / import_type /
    lsp_verified / lsp). A ``name_match`` edge is NEVER a fact — even a
    single-candidate name_match scores 0.9, and the old ``confidence >= 0.9``
    gate laundered it as a confident caller (PROVEN harm on beancount-931: stdlib
    ``os.walk`` rendered as a caller of beancount ``account.walk``).

    name_match / unknown-provenance edges below ``_NAME_MATCH_FLOOR`` are
    suppressed; at/above it they render as ``file:line (unverified)`` — a bare
    location hint with NO function-name relationship claim — so the agent's grep
    stays the filter. Facts always win: unverified hints are emitted only when no
    fact exists, never mixed in alongside verified callers.
    """
    if not func_names:
        return ""

    try:
        conn = sqlite3.connect(graph_db)
    except Exception:
        return ""

    fact_parts: list[str] = []
    unverified_parts: list[str] = []
    try:
        # Column probe inside the try so conn is always closed (no leak if the
        # PRAGMA raises). Reuse curation_map._has_columns — single source of truth.
        has_conf, has_method = _has_columns(conn)
        conf_sel = "e.confidence" if has_conf else "0.0"
        method_sel = "e.resolution_method" if has_method else "''"
        # Cross-language disqualifier (ported from the mini delivery): pull
        # both endpoint languages when the column exists; legacy graphs
        # (no nodes.language) stay PERMISSIVE — '' -> family None -> no judgement.
        has_lang = _nodes_have_language(conn)
        src_lang_sel = "nsrc.language" if has_lang else "''"
        tgt_lang_sel = "nt.language" if has_lang else "''"
        # Facts-first ordering: deterministic-provenance edges sort before
        # name_match, so the over-fetch LIMIT can never cut a real fact off behind
        # a run of higher-confidence name_match rows.
        _det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
        _norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
        for fname in func_names[:2]:
            # 2026-06-10 fact-filter: never claim callers for a builtin/dunder-
            # shadow name (the `isinstance` launder — callers call the BUILTIN).
            if _is_builtin_shadow_name(fname):
                continue
            # No confidence gate in SQL — fetch cross-file callers and classify by
            # provenance in Python. Over-fetch so non-fact rows don't crowd out
            # the deterministic ones before the per-func cap.
            rows = conn.execute(
                f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, {conf_sel}, {method_sel},
                       {src_lang_sel}, {tgt_lang_sel}
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND nt.file_path LIKE ?
                  AND nsrc.file_path != nt.file_path
                  AND nsrc.is_test = 0
                  AND e.source_line > 0
                ORDER BY CASE WHEN {method_sel} IN ('{_det_sql}') THEN 0 ELSE 1 END,
                         {conf_sel} DESC, e.source_line
                LIMIT ?
                """,
                (fname, f"%{_norm_fp}", MAX_CALLERS_PER_FUNC * 4),
            ).fetchall()

            for caller_file, source_line, caller_name, conf, method, src_lang, tgt_lang in rows:
                # 2026-06-10 fact-filter: a vendored/minified/generated caller
                # is never a fact NOR an unverified location hint.
                if _is_vendored_path(caller_file or ""):
                    continue
                # 2026-06-17 demo-filter: a caller located in a docs/examples/
                # demo path is NOISE, not a real dependent — it tells the agent to
                # inspect a tutorial, not a function that USES the edit target. The
                # scope-chain (c15d306f) + localization entries already drop these
                # via the SAME canonical predicate; the brief Callers pillar missed
                # it (textual-richlog leaked `compose() in docs/examples/...`).
                # `_is_vendored_path` does NOT catch examples/ or docs/ — only
                # `_is_test_or_demo` does. Correct-or-quiet: keep real source callers.
                if _is_test_or_demo(caller_file or ""):
                    continue
                # Cross-language disqualifier (mini-delivery port, boa [57]): a
                # CALLS edge whose endpoint files are in DIFFERENT language
                # families cannot be a real source-level call, whatever its
                # recorded resolution_method/confidence — drop it before fact
                # OR unverified-hint classification. Unknown language -> keep.
                if _is_cross_language_pair(src_lang, tgt_lang):
                    continue
                try:
                    conf_f = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    conf_f = 0.0

                # Read the caller's source line once — used for both the
                # stdlib-shadow guard and the fact snippet.
                code = ""
                try:
                    with open(
                        os.path.join(repo_root, caller_file),
                        encoding="utf-8",
                        errors="ignore",
                    ) as fh:
                        _lines = fh.readlines()
                    if 0 < source_line <= len(_lines):
                        code = _lines[source_line - 1].strip()
                except OSError:
                    code = ""

                # Stdlib-shadow guard: a "caller" that is really calling a stdlib
                # function of the same name (os.walk -> project walk) is a false
                # caller regardless of the edge's recorded provenance. Drop it.
                if _is_stdlib_shadow(code, fname):
                    continue

                # Normalize provenance (strip/lower) so 'Import' / 'import ' from
                # an inconsistent indexer still classify as the canonical method.
                # A whitelisted METHOD is necessary but NOT sufficient: a tier whose
                # premise was not re-proven is capped to conf 0.6 (the -file/L6 restore
                # demote in incremental.go) but KEEPS its method as provenance, and a
                # genuinely-uncertain whitelisted edge (e.g. among-files import pick) is
                # also minted at 0.6. Neither is a FACT — EDGE_CONFIDENCE_FLOOR (0.7)
                # "keeps genuine facts (1.0/0.9) and drops the 0.6" (the SAME conjunct the
                # caller-query helper applies). Gate on it here too, else a method-only
                # is_fact launders the capped restore back to CERTIFIED. Old schema (no
                # confidence column) stays permissive — there is no conf to judge.
                is_fact = (method or "").strip().lower() in _DETERMINISTIC_METHODS and (
                    not has_conf or conf_f >= EDGE_CONFIDENCE_FLOOR
                )
                if is_fact:
                    snippet = code if len(code) <= 80 else code[:77] + "..."
                    rendered = (
                        f"{caller_name}() in {caller_file}:{source_line} `{snippet}`"
                        if snippet
                        else f"{caller_name}() in {caller_file}:{source_line}"
                    )
                    if rendered not in fact_parts:
                        fact_parts.append(rendered)
                elif conf_f >= _NAME_MATCH_FLOOR or not has_conf:
                    # name_match / unknown above floor -> location hint only, marked
                    # unverified, with NO caller-name claim (don't launder a guess).
                    # `not has_conf`: on an old schema with no confidence column we
                    # cannot gate by the floor, so render the bare location hint
                    # (matches the documented unverified path) rather than dropping
                    # every caller — the pre-rewrite behavior, kept correct-or-quiet.
                    # Honesty marker (curation_map._fmt_edge discipline, bug #9;
                    # docstring contract above): an unverified hint must never
                    # render indistinguishably from a structurally-resolved fact.
                    hint = f"{caller_file}:{source_line} (unverified)"
                    if hint not in unverified_parts:
                        unverified_parts.append(hint)
                # below floor and not a fact -> suppressed (correct-or-quiet)

                if len(fact_parts) >= 3:
                    break
            if len(fact_parts) >= 3:
                break
    finally:
        conn.close()

    if fact_parts:
        return " | ".join(fact_parts[:3])
    if unverified_parts:
        return " | ".join(unverified_parts[:2])
    return ""


def _resolved_witnesses_for_file(
    graph_db: str,
    file_path: str,
    repo_root: str,
    max_each: int = 2,
) -> list[dict]:
    """Deterministic-provenance caller AND callee witnesses for ``file_path``.

    This is the STRUCTURED twin of ``_caller_contract_for_file``: it surfaces the
    RESOLVED call-edge FACTS already in graph.db so a candidate carries a concrete
    call-edge witness at iter-0 (fixes the audited ``l1_candidates_with_call_edge_count
    = 0`` / ``l1_primary_witness_file = 'N/A — no confirming edge'`` — the resolution
    was on disk but never surfaced as a confirming edge in the L1 brief).

    A witness is emitted ONLY when its edge ``resolution_method`` is in
    ``DETERMINISTIC_RESOLUTION_METHODS`` (the unified categorical fact-set, shared
    with curation_map / post_edit). ``name_match`` is NEVER a witness here — even a
    single-candidate name_match scores 0.9 and is still a name GUESS. The same
    ``_is_stdlib_shadow`` guard the brief's caller line applies is applied here, so a
    DETERMINISTIC-tagged edge that is really a stdlib attribute call name-matched to a
    same-named project symbol (``os.walk`` -> project ``walk``) is dropped despite its
    recorded provenance (wire.md RUN VERDICT: the provenance gate alone trusts that
    false fact; the stdlib guard is the secondary defense).

    Returns a list of dicts ``{relation: 'CALLS', direction: 'caller'|'callee',
    file_path, line, symbol, target, code}`` — caller witnesses first (a caller is
    the stronger localization confirmation: it proves the candidate's symbol is a
    REAL, USED target). Correct-or-quiet: empty list on any error / no DB / no
    deterministic edge. Pure read; no ranking effect (BRIEFING.md §3 row 4 / §4 —
    surface facts that already rank, never change reach/weights).
    """
    if not graph_db or not file_path:
        return []
    conn = None
    try:
        conn = sqlite3.connect(graph_db)
        has_conf, has_method = _has_columns(conn)
        if not has_method:
            return []  # cannot judge provenance -> emit nothing (never launder)
        _det_sql = "','".join(sorted(DETERMINISTIC_RESOLUTION_METHODS))
        # E3 (2026-07-05): a whitelisted METHOD is necessary but NOT sufficient — the
        # -file/L6 restore demote (incremental.go) caps an unre-proven deterministic edge
        # to conf 0.6 while KEEPING its method as provenance, and an among-files import
        # pick is minted at 0.6. Neither is a FACT. Gate on EDGE_CONFIDENCE_FLOOR (0.7),
        # the SAME conjunct is_fact()/_resolution_fact_clause apply, so a demoted edge
        # cannot launder back into a [WITNESS]. Old schema (no confidence col) stays
        # permissive — there is no conf to judge.
        _conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if has_conf else ""
        _norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
        # Cross-language disqualifier (mini-delivery port): endpoint languages,
        # permissive on legacy graphs without nodes.language ('' -> no judgement).
        has_lang = _nodes_have_language(conn)
        _src_lang_sel = "nsrc.language" if has_lang else "''"
        _tgt_lang_sel = "nt.language" if has_lang else "''"
        out: list[dict] = []

        def _code_at(rel_file: str, line: int) -> str:
            if not rel_file or not line or line <= 0:
                return ""
            try:
                with open(
                    os.path.join(repo_root, rel_file), encoding="utf-8", errors="ignore"
                ) as fh:
                    _lines = fh.readlines()
                if 0 < line <= len(_lines):
                    return _lines[line - 1].strip()
            except OSError:
                pass
            return ""

        # CALLERS: cross-file functions that CALL a symbol defined in this file
        # (DETERMINISTIC edges only). The target symbol (nt.name) is required so the
        # stdlib-shadow guard can be applied per (code, target_name).
        caller_sql = f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nt.name,
                   {_src_lang_sel}, {_tgt_lang_sel}
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE {{file_predicate}}
              AND nsrc.file_path != nt.file_path
              AND nsrc.is_test = 0
              AND e.source_line > 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{_det_sql}')
              {_conf_clause}
            ORDER BY e.source_line
            LIMIT ?
            """
        caller_rows = conn.execute(
            caller_sql.format(file_predicate="(nt.file_path = ? OR nt.file_path = ?)"),
            (_norm_fp, "./" + _norm_fp, max_each * 4),
        ).fetchall()
        if not caller_rows:
            caller_rows = conn.execute(
                caller_sql.format(file_predicate="nt.file_path LIKE ?"),
                ("%/" + _norm_fp, max_each * 4),
            ).fetchall()
        for caller_file, line, caller_name, target_name, _slang, _tlang in caller_rows:
            # 2026-06-10 fact-filter: vendored/minified caller files and
            # builtin/dunder-shadow targets are never [WITNESS] facts.
            if _is_vendored_path(caller_file or ""):
                continue
            # 2026-06-17 demo-filter: a caller in a docs/examples/ demo path is
            # never a resolved-caller WITNESS — it points the L1 `resolved caller:`
            # annotation at a tutorial file, not a real dependent (textual-richlog
            # leaked `resolved caller: compose() in docs/examples/...`). Same
            # canonical predicate the localization/scope surfaces use; `_is_vendored_path`
            # misses examples/ + docs/, so this is the load-bearing drop.
            if _is_test_or_demo(caller_file or ""):
                continue
            if _is_builtin_shadow_name(target_name or ""):
                continue
            # Cross-language disqualifier (mini-delivery port, boa [57]): an
            # edge across language families is never a [WITNESS] fact.
            if _is_cross_language_pair(_slang, _tlang):
                continue
            code = _code_at(caller_file, line)
            if _is_stdlib_shadow(code, target_name or ""):
                continue  # false caller: stdlib attr call name-matched to project symbol
            out.append(
                {
                    "relation": "CALLS",
                    "direction": "caller",
                    "file_path": caller_file,
                    "line": int(line) if line else 0,
                    "symbol": caller_name or "",
                    "target": target_name or "",
                    "code": code,
                }
            )
            if sum(1 for w in out if w["direction"] == "caller") >= max_each:
                break

        # CALLEES: cross-file symbols this file CALLS into (DETERMINISTIC edges only).
        callee_sql = f"""
            SELECT nt.file_path, e.source_line, nt.name, nsrc.name, nt.start_line,
                   {_src_lang_sel}, {_tgt_lang_sel}
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
            JOIN nodes nt ON e.target_id = nt.id
            WHERE {{file_predicate}}
              AND nt.file_path != nsrc.file_path
              AND nt.is_test = 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{_det_sql}')
              {_conf_clause}
            ORDER BY e.source_line
            LIMIT ?
            """
        callee_rows = conn.execute(
            callee_sql.format(file_predicate="(nsrc.file_path = ? OR nsrc.file_path = ?)"),
            (_norm_fp, "./" + _norm_fp, max_each * 4),
        ).fetchall()
        if not callee_rows:
            callee_rows = conn.execute(
                callee_sql.format(file_predicate="nsrc.file_path LIKE ?"),
                ("%/" + _norm_fp, max_each * 4),
            ).fetchall()
        for (
            callee_file,
            source_line,
            callee_name,
            src_name,
            def_line,
            _slang,
            _tlang,
        ) in callee_rows:
            # `source_line` is the CALL SITE in THIS candidate file — use it ONLY for the
            # stdlib-shadow check on the call (`os.walk(` must be read at the call site).
            # The RENDERED location is the callee's DEFINITION line in callee_file
            # (nt.start_line); pairing callee_file with the caller's source_line printed
            # "X in <calleefile>:<callerline>" (wrong file:line). #36: the emitted `code`
            # field travels with (callee_file, def_line), so it must be read THERE too —
            # the call-site line belongs to a DIFFERENT file than file_path:line, a latent
            # wrong-fact. Read code at the callee's definition so every field of the record
            # references the same callee location (symmetric with the caller branch).
            # 2026-06-10 fact-filter: vendored/minified callee files and
            # builtin/dunder-shadow callee names are never [WITNESS] facts.
            if _is_vendored_path(callee_file or ""):
                continue
            # 2026-06-17 demo-filter (symmetry with the caller branch): a callee
            # defined in a docs/examples/ demo path is never a resolved-call
            # WITNESS — `resolved call: -> x() in docs/examples/...` is the same
            # tutorial-misdirection noise. `_is_vendored_path` misses examples/+docs/.
            if _is_test_or_demo(callee_file or ""):
                continue
            if _is_builtin_shadow_name(callee_name or ""):
                continue
            # Cross-language disqualifier (mini-delivery port): an edge across
            # language families is never a [CALLEE] witness fact.
            if _is_cross_language_pair(_slang, _tlang):
                continue
            _call_code = _code_at(file_path, source_line)
            if _is_stdlib_shadow(_call_code, callee_name or ""):
                continue
            out.append(
                {
                    "relation": "CALLS",
                    "direction": "callee",
                    "file_path": callee_file,
                    "line": int(def_line) if def_line else 0,
                    "symbol": callee_name or "",
                    "target": src_name or "",
                    "code": _code_at(callee_file, def_line) if def_line else "",
                }
            )
            if sum(1 for w in out if w["direction"] == "callee") >= max_each:
                break
        return out
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _norm_fp(file_path: str) -> str:
    """Normalize a path to the form gt-index stores in nodes.file_path:
    repo-relative, forward slashes, no leading ``./`` or ``/``. Kept byte-parity
    with the mini port (gt_mini_patch._norm_fp).

    NB: strip the ``./`` PREFIX, not a char-SET (Fable finding 2). ``str.lstrip("./")``
    removes any leading run of {'.','/'} → ``.github/x`` becomes ``github/x`` and the
    sibling query's ``file_path = ?`` matches nothing for every dot-directory file."""
    p = (file_path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _compact_sig(sig: str) -> str:
    """Compact a stored signature to the pattern shape for the sibling line:
    sanitize LSP markdown, strip a leading Python ``def``/``async def`` (other
    languages keep their native ``func``/``fn``/method head), bound the length so
    several siblings fit one line. Correct-or-quiet: empty in -> empty out. Kept
    byte-parity with the mini port (gt_mini_patch._compact_sig)."""
    s = _sanitize_signature((sig or "").strip())
    if not s:
        return ""
    s = _re.sub(r"^\s*(async\s+)?def\s+", "", s).rstrip(":").strip()
    return (s[:88] + "...") if len(s) > 88 else s


def _sibling_context(graph_db: str, file_path: str, func_names: list[str]) -> str:
    """Sibling functions at the same scope — parallel patterns to follow.

    Each sibling carries its compact SIGNATURE (receiver/params/return) — the
    pattern a new or edited member must MATCH — not just the bare name, so the
    agent writes a member consistent with its siblings. Builtin/dunder-shadow
    names are filtered (a shadowed name is not a sibling pattern). ``Function``/
    ``Method`` ONLY — a class/impl-block is not a parallel-function pattern.
    Correct-or-quiet: a sibling with no clean signature falls back to its bare
    name; empty in / no siblings / error -> empty out. Pure SQL over
    ``nodes.signature``. Kept BYTE-PARITY with the mini port
    (``gt_mini_patch._sibling_context``); the deep-parity harness guards drift.
    """
    if not func_names:
        return ""
    try:
        conn = sqlite3.connect(graph_db)
        rows = conn.execute(
            "SELECT DISTINCT n.name, n.signature FROM nodes n "
            "WHERE n.file_path = ? "
            "AND n.label IN ('Function','Method','Class','ImplBlock') AND n.is_test = 0 "
            "AND n.name NOT IN ({}) ORDER BY n.start_line LIMIT 8".format(
                ",".join("?" * len(func_names))
            ),
            (_norm_fp(file_path), *func_names),
        ).fetchall()
        conn.close()
        out: list[str] = []
        seen: set[str] = set()
        for name, sig in rows:
            if (
                not name
                or len(name) <= 2
                or name.startswith("_")
                or _is_builtin_shadow_name(name)
                or name in seen
            ):
                continue
            seen.add(name)
            csig = _compact_sig(sig)
            out.append(csig if csig else name)
            if len(out) >= 4:
                break
        return ", ".join(out) if out else ""
    except Exception:
        return ""


def _function_spec(
    graph_db: str,
    file_path: str,
    func_name: str,
    repo_root: str,
) -> str:
    """Pre-edit specification: shows parallel patterns within a function.

    This surfaces the COMPLETE set of cases the function handles BEFORE the
    agent edits it. Prevents incomplete fixes (handling case A but missing B).
    Fires regardless of graph connectivity — purely syntactic.
    """
    try:
        conn = sqlite3.connect(graph_db)
        row = conn.execute(
            "SELECT start_line, end_line FROM nodes WHERE file_path = ? AND name = ? "
            "AND label IN ('Function', 'Method', 'Class', 'ImplBlock') LIMIT 1",
            (file_path, func_name),
        ).fetchone()
        conn.close()
        if not row or not row[0] or not row[1]:
            return ""
    except Exception:
        return ""

    full_path = os.path.join(repo_root, file_path)
    try:
        with open(full_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

    start = max(0, row[0] - 1)
    end = min(len(all_lines), row[1])
    func_lines = all_lines[start:end]

    from groundtruth.hooks.post_edit import _make_template

    templates: dict[str, list[str]] = {}
    for line in func_lines:
        stripped = line.strip()
        if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
            continue
        tmpl = _make_template(stripped)
        if tmpl not in templates:
            templates[tmpl] = []
        templates[tmpl].append(stripped)

    groups = [(t, lines) for t, lines in templates.items() if len(lines) >= 2 and len(lines) <= 8]
    if not groups:
        return ""

    groups.sort(key=lambda x: -len(x[1]))
    best = groups[0]
    cases = [ln if len(ln) <= 50 else ln[:47] + "..." for ln in best[1][:4]]
    return f"handles: {' | '.join(cases)}"


def _last_change(file_path: str, repo_root: str) -> str:
    """Get the last git commit message for this file — shows how the file evolves."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--", file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            msg = result.stdout.strip()
            if len(msg) > 70:
                msg = msg[:67] + "..."
            return msg
    except Exception:
        pass
    return ""


def _co_change_files(file_path: str, repo_root: str, limit: int = 3) -> list[str]:
    """Find files that historically co-change with this file (git-based).

    Research: HAFixAgent (arXiv 2025) +56.6% from git history in repair loop.
    ESEM 2024: co-change + structural deps significantly improves impact prediction.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-20", "--", file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    co_counts: dict[str, int] = {}
    current_commit_files: list[str] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            for f in current_commit_files:
                if f != file_path and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml")):
                    co_counts[f] = co_counts.get(f, 0) + 1
            current_commit_files = []
        else:
            current_commit_files.append(line)

    if current_commit_files:
        for f in current_commit_files:
            if f != file_path and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml")):
                co_counts[f] = co_counts.get(f, 0) + 1

    ranked = sorted(co_counts.items(), key=lambda x: (-x[1], x[0]))
    # Dynamic threshold: >= 1 when sparse data, >= 2 when dense
    # Research: "Lost in the Noise" — single co-change may be noise on dense repos
    counts = sorted(co_counts.values())
    median = counts[len(counts) // 2] if counts else 0
    min_count = 1 if median <= 1 else 2
    return [f for f, count in ranked[:limit] if count >= min_count]


def _co_change_from_table(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    """Co-change files from the indexer's `cochanges` table (mined at index time
    with a count>=3 floor) — replaces the per-file `git log` shell-out: faster, and
    works in detached worktrees where git history is unavailable. The threshold is
    already applied at index time, so no "noise floor" knob here. Empty when the
    table is absent/unpopulated (caller then falls back to the git miner)."""
    if not graph_db or not os.path.exists(graph_db):
        return []
    # B7: strip the "./" PREFIX only — .lstrip("./") would eat the leading dot of a
    # dot-directory ('.github/x.py' -> 'github/x.py'), never matching the table.
    _n = file_path.replace("\\", "/")
    _n = _n[2:] if _n.startswith("./") else _n
    _norm = _n.lstrip("/")
    conn = None
    try:
        conn = sqlite3.connect(graph_db)
        # B6: exclude doc/config co-changes IN SQL (before LIMIT). Docs/CHANGELOG/
        # CI-yaml have the highest co-change counts; filtering them in Python AFTER
        # LIMIT 3 let them fill the top-3 and starved real source co-changes to [].
        rows = conn.execute(
            "WITH cc AS ("
            "  SELECT CASE WHEN file_a = ? THEN file_b ELSE file_a END AS other, count "
            "  FROM cochanges WHERE file_a = ? OR file_b = ?"
            ") "
            "SELECT other FROM cc "
            "WHERE other <> ? AND other NOT LIKE '%.md' AND other NOT LIKE '%.rst' "
            "  AND other NOT LIKE '%.txt' AND other NOT LIKE '%.yml' AND other NOT LIKE '%.yaml' "
            "ORDER BY count DESC, other ASC LIMIT ?",
            (_norm, _norm, _norm, _norm, max(limit * 5, 30)),
        ).fetchall()
        # BUG-A / Class-A residual (2026-06-17): the SQL above excludes doc/config
        # by EXTENSION but never the test/demo/vendored DIRS (whole-segment, not an
        # extension) — a test co-change leaked into <gt-cochange>. Route through the
        # canonical deliverable chokepoint (is_deliverable = not test_or_demo/vendored),
        # fetching extra above so the filter does not starve real source co-changes
        # below the limit (the B6 lesson). This is the cochange path-emitter the
        # Class-A generalization missed.
        return [
            r[0]
            for r in rows
            if r[0] and not _is_test_or_demo(r[0]) and not _is_vendored_path(r[0])
        ][:limit]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _estimate_tokens(text: str) -> int:
    """Char/4 token ESTIMATE (B-31 fallback). This is an APPROXIMATION, not a
    real token count — used only when the baked BPE tokenizer is unavailable.
    Prefer ``_count_tokens`` (which uses the real tokenizer when present)."""
    return len(text) // 4 + 1


# B-31: real BPE token count via the baked HF ``tokenizers`` vocabulary. The DOSE
# rail and the reported token count must reflect the MODEL's tokens, not a
# char/4 heuristic (which under/over-counts materially on code, punctuation, and
# Unicode). Deterministic + offline: the tokenizer.json is loaded ONCE from disk
# (no network, no download). Falls back to the char/4 ESTIMATE only when the lib
# or the baked file is unavailable — and that path is honestly an estimate.
_TOKENIZER_CACHE: dict[str, object] = {}


def _resolve_tokenizer_path() -> str:
    """Baked gte tokenizer.json path, or "" when none is configured. Consults ONLY
    explicit env config (``GT_TOKENIZER_JSON`` override, then
    ``GT_MODELS_ROOT/gte-modernbert-base/tokenizer.json``) so behavior is
    deterministic and never depends on an ambient install."""
    p = os.environ.get("GT_TOKENIZER_JSON")
    if p:
        return p
    mr = os.environ.get("GT_MODELS_ROOT")
    if mr:
        return os.path.join(mr, "gte-modernbert-base", "tokenizer.json")
    return ""


def _get_tokenizer():
    """Load (and path-cache) the baked ``tokenizers.Tokenizer``, or None. Keyed by
    the RESOLVED path so a test/process that changes the env is never served a
    stale tokenizer from a different path (hermetic across env changes)."""
    path = _resolve_tokenizer_path()
    if not path or not os.path.isfile(path):
        return None
    if path in _TOKENIZER_CACHE:
        return _TOKENIZER_CACHE[path]
    tok = None
    try:
        from tokenizers import Tokenizer  # baked HF lib

        tok = Tokenizer.from_file(path)
    except Exception:
        tok = None
    _TOKENIZER_CACHE[path] = tok
    return tok


def _count_tokens(text: str) -> int:
    """Real BPE token count when the baked tokenizer is available; otherwise the
    char/4 ESTIMATE. Deterministic + LLM-free + offline.

    B-31/Brief-F9 caveat: the "real" tokenizer is gte-modernbert's BPE — a PROXY for
    the sampled model's own tokenizer, not the model's exact vocabulary. Use
    :func:`_tokenizer_kind` to record which counter actually ran (``tokenizer_used``
    on the result); the char/4 fallback is honest but coarser."""
    tok = _get_tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text).ids)
        except Exception:
            pass
    return _estimate_tokens(text)


def _tokenizer_kind() -> str:
    """Marker for WHICH token counter :func:`_count_tokens` uses right now, so a
    caller (and the deep-metrics audit) can tell the real-BPE path from the silent
    char/4 fallback. ``"gte-modernbert-bpe"`` when the baked HF tokenizer.json is
    configured + loadable (a PROXY for the sampled model's tokenizer), else
    ``"char4-estimate"``. Pure + deterministic; mirrors _count_tokens's own branch."""
    return "gte-modernbert-bpe" if _get_tokenizer() is not None else "char4-estimate"


# ITEM-5 (2026-07-13): the plain-checklist header the GT_BRIEF_NATIVE obligations FORM arm emits
# in place of the <gt-obligations> tag. SINGLE source so _is_brief_boundary / _segment_brief_blocks
# / brief_minimal_certificate recognize the native block (keep it obligations-priority + minimal-safe).
_OBLIGATION_NATIVE_HEADER = "Requirements to satisfy (from the issue):"


def _brief_native_on() -> bool:
    """GT_BRIEF_NATIVE — the obligations-section FORM arm. Default OFF -> byte-identical (the
    ``<gt-obligations>`` tag block). When ON the SAME obligation rows render as a plain requirements
    checklist (``- [ ] <obligation>`` under a one-line plain header, NO ``<gt-*>`` tag). REBAKE-
    RELEVANT: read at brief-GENERATION, dormant + byte-identical on a pre-baked / off run. Retires
    ONLY the obligations FRAME; GT_BRIEF_MINIMAL retires graph/contract narration and
    retains calibrated MEDIUM/LOW localization contention when singularizing it would
    overstate confidence."""
    import os as _os

    return (_os.environ.get("GT_BRIEF_NATIVE") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _ss_ack_form_on() -> bool:
    """GT_SS_ACK_FORM — the SS-5 acknowledgment-FORM arm. Default OFF -> byte-identical. When ON
    (AND ``_brief_native_on()``), the plain checklist additionally applies the requirement-extractor
    discipline: only IMPERATIVE requirement items survive (repro fragments / pleas are dropped) and
    the ``[kind]`` classifier prefix is stripped, so the step-0 obligations read as a native
    requirements checklist. Layering it ON TOP of GT_BRIEF_NATIVE keeps GT_BRIEF_NATIVE-alone
    byte-identical to today's plain-checklist arm. Strict ``== "1"``-family parse."""
    import os as _os

    return (_os.environ.get("GT_SS_ACK_FORM") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


# The ONLY path-bearing orientation note: the matched top-1 candidate line built in the
# confident-line emitter ("Highest-confidence candidate (graph + issue signals): <path>").
# It NAMES A FILE, so under GT_LOC_RESLOT it is a task_start which-file steer and is retired
# from the minimal brief; the fileless steers below name no path and are the legitimate
# minimal orientation. ONE source for this prefix, reused as the first element below.
_PATH_BEARING_ORIENTATION_PREFIX = "Highest-confidence candidate"

# The stable line-prefixes of a step-0 orientation NOTE — the path-bearing matched candidate
# line plus the two FILELESS honest steers ("Note: GT could not anchor …" for a matched brief
# with no verified edge, "Note: GT found no indexed file …" for the no-match/new-file brief).
# ONE source of truth so the THREE consumers stay in lock-step: _is_brief_boundary (scan-stop),
# _segment_brief_blocks (the ``orientation-note`` label), and the token-rail orientation-note
# drop pass. A new steer prefix added here is recognized by ALL THREE at once — never added in
# one site but silently missed at the others (the exact split that let the no-match steer fall
# to ``misc`` → non-substantive → dropped by the minimal reducer).
_ORIENTATION_NOTE_PREFIXES: tuple[str, ...] = (
    _PATH_BEARING_ORIENTATION_PREFIX,
    "Note: GT could not anchor",
    "Note: GT found no indexed file",
)


def _is_brief_boundary(line: str) -> bool:
    """True if ``line`` starts a structural brief block or a scaffold tag. A scan
    that consumes a 'section until the next blank line' must STOP here so it never
    swallows the following block or the ``</gt-task-brief>`` close tag — blocks are
    not always blank-separated (a trailing steer note abuts the close tag)."""
    s = line.strip()
    if s in (
        "<gt-task-brief>",
        "</gt-task-brief>",
        "<gt-obligations>",
        "</gt-obligations>",
        "<gt-graph-map>",
        "</gt-graph-map>",
    ):
        return True
    if (
        s == _OBLIGATION_NATIVE_HEADER
    ):  # ITEM-5: the GT_BRIEF_NATIVE obligations header is a boundary
        return True
    if s.startswith("<gt-localization") or s.startswith("</gt-localization"):
        return True
    if _re.match(r"^\d+\.\s", line):
        return True
    return any(
        line.startswith(p)
        for p in (
            "Expected behavior:",
            "EDIT-TARGET CONTRACTS",
            "Other candidates",
            "Related files to inspect",
            "Likely multi-file scope",
            "Scope chain",
            *_ORIENTATION_NOTE_PREFIXES,
        )
    )


def _segment_brief_blocks(text: str) -> list[dict]:
    """Segment a rendered brief into priority-tagged blocks for the B-30 rail's
    priority-ordered rebuild. Each block is ``{priority, label, text, order}``;
    ``priority < 0`` is protected scaffold (always kept). Lower priority number =
    higher value = kept first. Deterministic; pure text segmentation."""
    lines = text.split("\n")
    n = len(lines)
    blocks: list[dict] = []

    def _add(pri: int, label: str, seg: list[str]) -> None:
        blocks.append(
            {"priority": pri, "label": label, "text": "\n".join(seg), "order": len(blocks)}
        )

    def _until_close(start: int, close_tag: str) -> int:
        j = start + 1
        while j < n and lines[j].strip() != close_tag:
            j += 1
        return min(j, n - 1)

    def _until_unindented(start: int) -> int:
        j = start + 1
        while j < n and (lines[j].startswith(" ") or lines[j].startswith("\t")):
            j += 1
        return j

    def _until_blank(start: int) -> int:
        j = start + 1
        while j < n and lines[j].strip() != "" and not _is_brief_boundary(lines[j]):
            j += 1
        return j

    _file_seen = 0
    i = 0
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if s.startswith("<gt-localization"):
            # the localization steer is a first-class structural fact (which file to
            # edit) — kept high (contract tier), but BELOW the active obligation so a
            # tight budget preserves the behavioral spec over the confidence header.
            e = _until_close(i, "</gt-localization>")
            _add(2, "localization-header", lines[i : e + 1])
            i = e + 1
            continue
        if s in ("<gt-task-brief>", "</gt-task-brief>"):
            _add(-1, "scaffold", [ln])
            i += 1
            continue
        if s == "<gt-graph-map>":
            e = _until_close(i, "</gt-graph-map>")
            _add(6, "graph-map", lines[i : e + 1])
            i = e + 1
            continue
        if s == "<gt-obligations>":
            e = _until_close(i, "</gt-obligations>")
            _add(1, "obligations", lines[i : e + 1])
            i = e + 1
            continue
        if s == _OBLIGATION_NATIVE_HEADER:
            # ITEM-5: the GT_BRIEF_NATIVE obligations block (plain header + `- [ ] …` rows, no tag).
            # Label it ``obligations`` (priority 1) so the token rail protects it EXACTLY like the
            # tagged block and the minimal reducer keeps it — the header + its checklist rows run to
            # the next blank / boundary.
            e = _until_blank(i)
            _add(1, "obligations", lines[i:e])
            i = e
            continue
        if ln.startswith("Expected behavior:"):
            # softer issue-spec echo — kept in the contract/spec tier, strictly
            # BELOW the parsed <gt-obligations> block (the active obligation).
            _add(2, "expected-behavior", [ln])
            i += 1
            continue
        if ln.startswith("EDIT-TARGET CONTRACTS"):
            e = _until_unindented(i)
            _add(2, "edit-target-contracts", lines[i:e])
            i = e
            continue
        if _re.match(r"^\d+\.\s", ln):
            # All file entries share one priority band and are kept as a RANK-ORDERED
            # PREFIX by the enforcer (never file #2 without file #1) — document order
            # == localizer rank, so processing by order preserves the ranking.
            e = _until_unindented(i)
            _file_seen += 1
            _add(3, f"file-entry-{_file_seen}", lines[i:e])
            i = e
            continue
        if (
            ln.startswith("Other candidates")
            or ln.startswith("Related files")
            or ln.startswith("Likely multi-file")
            or ln.startswith("Scope chain")
        ):
            e = _until_blank(i)
            _add(5, "companion", lines[i:e])
            i = e
            continue
        if any(ln.startswith(p) for p in _ORIENTATION_NOTE_PREFIXES):
            _add(6, "orientation-note", [ln])
            i += 1
            continue
        # blank / unknown line — cheap filler kept with its neighbors.
        _add(5, "misc", [ln])
        i += 1
        continue
    return blocks


# B-6: fact-class of each segmented block label. A block whose label is absent here
# (``scaffold`` = the <gt-task-brief> tags; ``misc`` = blank/unknown filler) is NOT a
# fact-bearing block and gets no receipt (correct-or-quiet: a receipt is a claim a
# FACT was delivered, never a claim about structural scaffold). File entries carry
# the per-file localization evidence (contract/callers/context, incl. any "Also
# changes:" cochange line), so they map to ``localization``.
_BLOCK_FACT_CLASS: dict[str, str] = {
    "localization-header": "localization",
    "obligations": "obligations",
    "expected-behavior": "obligations",
    "edit-target-contracts": "contract",
    "companion": "scope",
    "graph-map": "graph-map",
    "orientation-note": "orientation",
    # "file-entry-<N>" is matched by prefix below (each N is a distinct label).
}


def _block_receipts_on() -> bool:
    """Whether host-side brief block receipts are enabled.

    ``GT_BLOCK_RECEIPTS`` remains the explicit override. When absent, the
    already-profiled ``GT_INSEAM_METRICS`` capability enables receipts. With
    neither present the metadata stays off. Brief bytes never depend on this.
    """
    import os as _os

    _flag = (
        _os.environ.get("GT_BLOCK_RECEIPTS")
        if "GT_BLOCK_RECEIPTS" in _os.environ
        else _os.environ.get("GT_INSEAM_METRICS")
    )
    return (_flag or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


# --------------------------------------------------------------------------- #
# SM-6 (B, 2026-07-11): the step-0 baked brief reduction — GT_BRIEF_MINIMAL.
#
# DEFAULT-OFF, byte-identical when off, and BAKED (the brief is generated at substrate-
# build time and consumed read-only in-container — gt_agent._substrate_brief), so this
# reduction has ZERO effect on any run until the flag is set at the SM-8 REBAKE.
# Graph-map and contract narration are retired. HIGH reduces to one orientation
# header; MEDIUM/LOW retain compact confidence, contention, and the grep hedge so
# uncertainty cannot become a singular assertion. Reactive def_partition/post_search
# remains the follow-up localization channel after the agent's own search.
# --------------------------------------------------------------------------- #
def _loc_reslot_on() -> bool:
    """GT_LOC_RESLOT — the T0->T2 localization re-slot (registry:
    ``localization.deliver_by=search_result``). When ON, step-0 ships NO localization
    narration at any confidence tier (the calibrated contention rides the reactive
    ranked-localization delivery at the registered search boundary instead). Default
    OFF -> the MEDIUM/LOW retention branch below is byte-identical legacy behavior."""
    import os as _os

    return (_os.environ.get("GT_LOC_RESLOT") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _brief_minimal_on() -> bool:
    """GT_BRIEF_MINIMAL master switch — default OFF, byte-identical. When OFF the brief is
    generated exactly as before (the reducer is never invoked). When ON (set only at the
    SM-8 substrate rebake) the step-0 brief is reduced to obligations + minimal
    orientation; graph-map and contract narration are retired, while MEDIUM/LOW
    localization contention is retained as an indivisible calibrated fact."""
    import os as _os

    return (_os.environ.get("GT_BRIEF_MINIMAL") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


# The segmented-block labels the minimal reduction normally DROPS whole. A MEDIUM/LOW
# ``localization-header`` is conditionally retained; HIGH drops it and keeps one file header.
# ``graph-map`` (<gt-graph-map>) is always dropped;
# ``edit-target-contracts`` + ``companion`` (Other candidates cross-file facts / Related
# files / Scope chain) are the contract/scope NARRATION; ``expected-behavior`` is the
# issue-spec echo. A ``file-entry`` block is REDUCED to its header line (the minimal
# 'which file' orientation), never dropped — handled specially in the reducer. The
# scaffold (<gt-task-brief> tags), ``obligations``, and ``orientation-note`` are KEPT.
_BRIEF_MINIMAL_DROP_LABELS: frozenset = frozenset(
    {"localization-header", "graph-map", "edit-target-contracts", "companion", "expected-behavior"}
)


def _reduce_brief_to_minimal(text: str) -> str:
    """Reduce an assembled brief to obligations + minimal orientation ONLY (SM-6 B).

    Reuses the B-30 rail's :func:`_segment_brief_blocks` — the ONE tested brief taxonomy —
    so the reduction tracks the exact block boundaries the token enforcer already trusts.
    KEEPS: the ``<gt-task-brief>`` scaffold, the ``<gt-obligations>`` block, the
    orientation-note, plus either a HIGH file-entry header or the MEDIUM/LOW compact
    localization contention block. DROPS whole: ``<gt-graph-map>``, HIGH
    ``<gt-localization>``, ``EDIT-TARGET CONTRACTS``, the
    per-file contract/caller/call evidence bodies, the cross-file 'Other candidates'/scope
    hints, and the 'Expected behavior' echo. PURE + deterministic; NEVER called when
    GT_BRIEF_MINIMAL is off (byte-identical). Idempotent: re-reducing a minimal brief
    returns it unchanged (its blocks are already only kept labels + header-only entries)."""
    blocks = _segment_brief_blocks(text)
    # MEDIUM/LOW is not a singular localization assertion: its confidence label,
    # contention set, and grep-confirmation hedge are one indivisible fact.  The
    # old reducer dropped that block but retained a file-entry header, converting
    # uncertainty into a naked top-1 steer.  Keep the already-compact contention
    # block and suppress duplicate file-entry headers.  HIGH remains on the prior
    # minimal path (one orientation header, no localization narration).
    #
    # R4 (run-#3 pilot, 2026-07-18): the MEDIUM/LOW retention is RETIRED under the
    # localization RE-SLOT. Three authorities collided and this branch was the odd one
    # out: the registry declares localization ``deliver_by=search_result`` (T0->T2
    # re-slot, user-ratified), the run workflow sets GT_BRIEF_MINIMAL with the stated
    # intent "retires the step-0 localization narration", yet this branch still shipped
    # a task_start localization header -- which the J3 timing kernel therefore graded
    # WRONG_EVENT on 21/21 live tasks (delivery-grain receipt: actual_event=task_start,
    # decision_open_index=null). Under GT_LOC_RESLOT the calibrated-contention VALUE
    # moves to the reactive ranked-localization delivery at the registered search
    # boundary; step-0 keeps obligations + orientation only. With the re-slot off, the
    # MEDIUM/LOW retention stands unchanged (byte-identical legacy path).
    _loc_tier = _localization_confidence_tier(text)
    _keep_contention = _loc_tier in {"medium", "low"} and not _loc_reslot_on()
    kept: list[str] = []
    # CORRECT-OR-QUIET (2026-07-20, run6 audit D-3): the scaffold (<gt-task-brief>
    # tags) and blank ``misc`` filler carry no model-facing fact. When every
    # SUBSTANTIVE block (obligations / orientation-note / localization / file-entry)
    # is dropped or was never assembled, the reduction previously kept the bare
    # scaffold and shipped a 33-char hollow ``<gt-task-brief>\n\n</gt-task-brief>``
    # that got SEALED + counted as a delivery (smolagents/babel/privacyidea/mpl-29721,
    # block_lineage:[]). Track whether any substantive block survives; if none, return
    # "" so gt_agent._prepend_brief (`if not brief: return instruction`) ships nothing.
    _kept_substantive = False
    for b in blocks:
        label = b["label"]
        if label == "localization-header" and _keep_contention:
            kept.append(b["text"])
            _kept_substantive = True
            continue
        if label in _BRIEF_MINIMAL_DROP_LABELS:
            continue
        if label.startswith("file-entry"):
            # Ultracode review (2026-07-18): under the RE-SLOT, dropping the calibrated
            # contention while keeping these ranked "N. path" header lines would ship a
            # NAKED uncalibrated top-N steer at task_start — worse than both prior forms
            # and still an unregistered step-0 localization. Under GT_LOC_RESLOT step-0
            # keeps obligations + orientation-note ONLY; ranked localization arrives at
            # the registered search boundary. Legacy paths byte-identical.
            if _loc_reslot_on():
                continue
            if _keep_contention:
                continue
            head = b["text"].split("\n", 1)[0]
            if head.strip():
                kept.append(head)  # minimal orientation: the header, not the contract body
                _kept_substantive = True
            continue
        if (
            label == "orientation-note"
            and _loc_reslot_on()
            and b["text"].startswith(_PATH_BEARING_ORIENTATION_PREFIX)
        ):
            # DELTA 3 (run-#3 pilot, 2026-07-20): under the localization RE-SLOT the minimal
            # brief ships NO localization narration. The path-bearing matched note
            # ("Highest-confidence candidate (…): <path>") NAMES a top-1 file, so leaving it
            # here shipped a task_start which-file steer that defeats the re-slot contract and
            # silently passed J3 as "orientation". Retire it exactly like the localization-
            # header and file-entry ranks. The FILELESS orientation notes ("Note: GT found no
            # indexed file …", "Note: GT could not anchor …") name no path and remain the
            # legitimate minimal orientation — they fall through to the keep below. Content
            # discriminator (names-a-path vs fileless), never task/repo-keyed. Legacy path
            # (re-slot off) is byte-identical: the note is kept as before.
            continue
        kept.append(b["text"])
        if label not in ("scaffold", "misc"):
            _kept_substantive = True
    # Collapse the blank-line runs the dropped blocks leave behind (the ``misc`` filler that
    # abutted a retired block) to at most one, and strip leading/trailing blanks — a clean
    # minimal brief, never a hollow one. Cosmetic only: no kept fact is touched.
    out_lines: list[str] = []
    for ln in "\n".join(kept).split("\n"):
        if ln.strip() == "" and (not out_lines or out_lines[-1].strip() == ""):
            continue
        out_lines.append(ln)
    while out_lines and out_lines[-1].strip() == "":
        out_lines.pop()
    # Correct-or-quiet: a scaffold-only reduction ships nothing, never a hollow tag.
    if not _kept_substantive:
        return ""
    return "\n".join(out_lines)


def _localization_confidence_tier(text: str) -> str:
    """Return the rendered localization tier, or ``""`` when absent/unknown."""
    match = _re.search(
        r'<gt-localization\b[^>]*\bconfidence=["\'](high|medium|low)["\']',
        text or "",
        _re.IGNORECASE,
    )
    return match.group(1).lower() if match else ""


def _model_visible_localization_entries(
    brief_text: str,
    candidates: list[FileEntry],
) -> list[FileEntry]:
    """Return candidates whose paths occur in a model-visible candidate block.

    ``.files`` and ``rendered_candidate_count`` are delivery claims.  They must be
    joined to the final bytes, not to the wider internal ranking list.  Restrict
    matching to localization headers and file-entry blocks so a path mentioned only
    as a caller/neighbor is not promoted to a delivered edit candidate.
    """
    candidate_lines: list[str] = []
    for block in _segment_brief_blocks(brief_text or ""):
        if block["label"] != "localization-header" and not block["label"].startswith("file-entry"):
            continue
        candidate_lines.extend(
            line.replace("\\", "/")
            for line in block["text"].splitlines()
            if _re.match(r"^\s*\d+\.\s+", line)
        )
    if not candidate_lines:
        return []
    out: list[FileEntry] = []
    for entry in candidates:
        raw_path = str(entry.path or "").replace("\\", "/")
        normalized_path = _gl_normalize(raw_path)
        variants = [path for path in dict.fromkeys((raw_path, normalized_path)) if path]
        if not variants:
            continue
        if any(
            _re.match(
                rf"^\s*\d+\.\s+{_re.escape(path)}(?![A-Za-z0-9_./-])",
                line,
            )
            for path in variants
            for line in candidate_lines
        ):
            out.append(entry)
    return out


def _brief_minimal_participation(before: str, after: str) -> list[dict]:
    """Typed sidecar for the terminal minimal-brief reduction decision."""
    if not _block_receipts_on():
        return []
    try:
        from groundtruth.runtime.control_participation import (
            build_control_participation,
            participation_to_dict,
        )

        record = build_control_participation(
            feature_id="GT_BRIEF_MINIMAL",
            decision_site="pretask.v1r_brief.minimal_reduction",
            decision="APPLIED" if after != before else "NO_EFFECT",
            iteration=0,
            candidate_bytes=before,
            reason="brief_reduced" if after != before else "already_minimal",
        )
        return [participation_to_dict(record)]
    except Exception as exc:  # sidecar failure never changes brief bytes
        import hashlib as _hashlib

        return [
            {
                "schema": "gt.control_participation.v1",
                "control_ref": {
                    "category": "CAP",
                    "feature_id": "GT_BRIEF_MINIMAL",
                    "role": "eligibility",
                },
                "decision_site": "pretask.v1r_brief.minimal_reduction",
                "decision": "ERROR",
                "iteration": 0,
                "candidate_chars": len(before),
                "candidate_sha256_16": (
                    _hashlib.sha256(before.encode("utf-8", "surrogatepass")).hexdigest()[:16]
                    if before
                    else ""
                ),
                "reason": f"control_record_error:{type(exc).__name__}",
            }
        ]


# The retired step-0 narration markers a minimal (SM-6 B) brief MUST NOT contain, and the
# obligation marker it MUST retain. Kept in lockstep with :data:`_BRIEF_MINIMAL_DROP_LABELS`
# + :func:`_reduce_brief_to_minimal` (the SM-7 gate reddens on a drift). These are the
# byte-level shadows of always-dropped BLOCK labels. HIGH localization is checked
# conditionally by :func:`brief_minimal_certificate`; MEDIUM/LOW is minimal-safe.
# ``graph-map`` -> ``<gt-graph-map>``, ``edit-target-contracts``/``companion`` -> the contract/
# caller/scope narration lines, ``expected-behavior`` -> the issue-spec echo.
_BRIEF_MINIMAL_RETIRED_MARKERS: tuple[str, ...] = (
    "<gt-graph-map>",
    "EDIT-TARGET CONTRACTS",
    "Callers:",
    "Calls:",
    "Context:",
    "Other candidates",
    "Related files",
    "Scope chain",
    "Expected behavior",
)


def brief_minimal_certificate(brief_text: str) -> dict:
    """Certify that a generated brief was ACTUALLY minimalized (SM-6 B / SM-8 arm-integrity).

    ``GT_BRIEF_MINIMAL`` is an UNMAPPED Profile-2 member (no importable backing module), so
    the ``rl_profile`` preflight passes WITHOUT proving the baked brief was minimalized — at
    the SM-8 rebake the GT-on arm could ship the FULL step-0 brief while claiming minimal,
    which silently invalidates the paired GT-on vs GT-off comparison. This function is that
    missing certificate: SM-8 generates the baked brief with ``GT_BRIEF_MINIMAL=1`` and calls
    ``brief_minimal_certificate(brief.brief_text)`` to REFUSE shipping an arm whose brief still
    carries a retired step-0 narration surface.

    PURE read of ``brief_text`` — no I/O, no env, no mutation. NOTHING in the live brief
    pipeline calls it (:func:`generate_v1r_brief` never references it), so adding it is
    byte-identical to the running product — it is an SM-8 rebake / SM-7 gate check only.

    Returns ``{minimal, retired_present, obligations_present, orientation_present}``:
      * ``retired_present`` — the retired markers still present (MUST be empty for minimal);
      * ``obligations_present`` — the ``<gt-obligations>`` behavioral contract is retained;
      * ``orientation_present`` — a 'which file' orientation survives (a numbered file-entry
        header OR the no-match ``Localize with grep`` note);
      * ``minimal`` — True iff NO retired marker is present (the necessary certificate; a full
        arm-integrity pass additionally asserts ``obligations_present``).
    """
    text = brief_text or ""
    retired = [m for m in _BRIEF_MINIMAL_RETIRED_MARKERS if m in text]
    # A compact MEDIUM/LOW contention block is now part of minimal orientation:
    # removing it changes calibrated uncertainty into a singular assertion.  HIGH
    # localization narration remains retired because its one file-entry header is
    # sufficient orientation.
    if "<gt-localization" in text and _localization_confidence_tier(text) not in {
        "medium",
        "low",
    }:
        retired.append("<gt-localization")
    # ITEM-5: the obligations contract is retained under EITHER frame — the ``<gt-obligations>`` tag
    # (default) OR the GT_BRIEF_NATIVE plain checklist header — so a combined MINIMAL+NATIVE rebake
    # still certifies obligations_present (the native form has no tag to match).
    obligations_present = "<gt-obligations>" in text or _OBLIGATION_NATIVE_HEADER in text
    orientation_present = (
        bool(_re.search(r"(?m)^\s*\d+\.\s+\S", text)) or "Localize with grep" in text
    )
    return {
        "minimal": not retired,
        "retired_present": retired,
        "obligations_present": obligations_present,
        "orientation_present": orientation_present,
    }


def _fact_class_for_label(label: str) -> str | None:
    """The fact-class of a segmented block label, or ``None`` for non-fact scaffold/
    filler. File entries (``file-entry-1``, ``file-entry-2``, …) map to localization."""
    if label.startswith("file-entry"):
        return "localization"
    return _BLOCK_FACT_CLASS.get(label)


def _localization_candidate_id(file_path: str) -> str:
    """Stable identity shared by a ranked file and its rendered file-entry block."""
    normalized = _gl_normalize(str(file_path or ""))
    return f"localization:{normalized}" if normalized else "localization:none"


def _brief_block_receipts(
    brief_text: str,
    *,
    localization_candidate_ids: list[str] | None = None,
) -> list[dict]:
    """B-6: assign each fact-bearing brief block a STABLE, DISTINCT delivery receipt.

    Reuses the deterministic :func:`_segment_brief_blocks` segmentation (the SAME
    block boundaries the B-30 dose-rail rebuilds by) and, for each fact-bearing
    block, emits ``{block_id, fact_class, label, char_span:[start,end],
    content_hash}``:

      * ``block_id`` — a stable, DISTINCT handle: the bare label when it occurs once
        in this brief, else ``"<label>#<k>"`` (k = 0-based occurrence). File entries
        are already distinct (``file-entry-1``, ``file-entry-2``, …).
      * ``char_span`` — ``[start, end)`` byte offsets into ``brief_text`` such that
        ``brief_text[start:end] == block_text`` exactly (the segmentation partitions
        the lines contiguously; blocks rejoin with the newline separator).
      * ``content_hash`` — ``sha256`` hex of the block's UTF-8 bytes.

    METADATA ONLY — this is a PURE READ of ``brief_text``; it NEVER mutates it, so a
    caller populating ``block_receipts`` leaves the delivered brief byte-identical.
    Deterministic, LLM-free, no I/O. Scaffold (the <gt-task-brief> tags) and blank/
    unknown filler are not fact-bearing and get no receipt."""
    import hashlib as _hashlib

    if not brief_text:
        return []
    blocks = _segment_brief_blocks(brief_text)
    # Two-pass distinctness: count fact-bearing labels so a label seen once keeps its
    # bare name and a repeated label (e.g. two "companion" blocks) is disambiguated.
    fact_labels = [b["label"] for b in blocks if _fact_class_for_label(b["label"]) is not None]
    _label_total: dict[str, int] = {}
    for _lab in fact_labels:
        _label_total[_lab] = _label_total.get(_lab, 0) + 1
    _has_file_entries = any(block["label"].startswith("file-entry") for block in blocks)

    receipts: list[dict] = []
    offset = 0
    _seen: dict[str, int] = {}
    _file_entry_index = 0
    for b in blocks:
        seg = b["text"]
        start = offset
        end = offset + len(seg)
        offset = end + 1  # the "\n" that _segment_brief_blocks joins blocks on
        fact_class = _fact_class_for_label(b["label"])
        if fact_class is None:
            continue  # scaffold / filler — not a fact
        label = b["label"]
        k = _seen.get(label, 0)
        _seen[label] = k + 1
        block_id = label if _label_total.get(label, 0) <= 1 else f"{label}#{k}"
        if label == "localization-header" and not _has_file_entries and localization_candidate_ids:
            # Minimal MEDIUM/LOW renders the whole contention set in this one
            # physical block and intentionally drops duplicate file-entry blocks.
            # Emit one logical candidate join per visible candidate, sealed to
            # that candidate's exact header line.  These subspans are metadata
            # inside one physical delivery, not multiple doses.
            for candidate_index, candidate_id in enumerate(
                localization_candidate_ids,
                start=1,
            ):
                prefix = "localization:"
                candidate_path = (
                    candidate_id[len(prefix) :] if candidate_id.startswith(prefix) else ""
                )
                matches = (
                    list(
                        _re.finditer(
                            rf"(?m)^[ \t]*\d+\.[ \t]+(?:\./)*/?"
                            rf"{_re.escape(candidate_path)}"
                            rf"(?![A-Za-z0-9_./-])",
                            seg.replace("\\", "/"),
                        )
                    )
                    if candidate_path
                    else []
                )
                line_spans = {
                    (
                        seg.rfind("\n", 0, match.start()) + 1,
                        (
                            len(seg)
                            if seg.find("\n", match.end()) < 0
                            else seg.find("\n", match.end())
                        ),
                    )
                    for match in matches
                }
                if len(line_spans) != 1:
                    continue
                line_start, line_end = next(iter(line_spans))
                candidate_bytes = seg[line_start:line_end]
                receipts.append(
                    {
                        "block_id": f"{block_id}:candidate-{candidate_index}",
                        "fact_class": fact_class,
                        "label": label,
                        "candidate_id": candidate_id,
                        "char_span": [start + line_start, start + line_end],
                        "content_hash": _hashlib.sha256(
                            candidate_bytes.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            continue
        if label.startswith("file-entry"):
            candidate_id = (
                localization_candidate_ids[_file_entry_index]
                if localization_candidate_ids is not None
                and _file_entry_index < len(localization_candidate_ids)
                else f"brief:block:{block_id}"
            )
            _file_entry_index += 1
        else:
            candidate_id = f"brief:block:{block_id}"
        receipts.append(
            {
                "block_id": block_id,
                "fact_class": fact_class,
                "label": label,
                "candidate_id": candidate_id,
                "char_span": [start, end],
                "content_hash": _hashlib.sha256(seg.encode("utf-8")).hexdigest(),
            }
        )
    return receipts


def _candidate_local_contribution_sources(proof: dict) -> list[str]:
    """Deterministically NAME the ACQ sources with a candidate-local witness in one
    rendered candidate proof.

    NAME-ONLY producer authority: this asserts which sources the producer used to
    rank/render THIS candidate, from the producer's own generation data (the graph
    witness, the v7.4 component values, the typed acquisition_sources it emitted).
    The downstream collector RE-VALIDATES each named source's typed witness before it
    may promote a row, so a name here is a producer assertion, not the whole proof.
    ``cochange_history`` keeps its own self-sealed ``cochange_evidence`` path and is
    intentionally excluded here (single authority per source)."""
    found: list[str] = []
    components = proof.get("components")
    components = components if isinstance(components, dict) else {}

    def _pos(value: object) -> bool:
        try:
            return float(value or 0.0) > 0.0
        except (TypeError, ValueError):
            return False

    witness = proof.get("witness")
    if proof.get("witness_verified") is True and isinstance(witness, str) and witness.strip():
        found.append("graph_validity")
    if _pos(components.get("reach")):
        found.append("structural_depth")
    if _pos(components.get("lex")):
        found.append("lexical_FTS5")
    if _pos(components.get("sem")):
        found.append("semantic_embedder")
    if _pos(components.get("body")) or _pos(components.get("content")):
        found.append("body_retrieval")
    sources = proof.get("acquisition_sources")
    if isinstance(sources, dict):
        for name in (
            "resolution_honesty",
            "type_intelligence",
            "LSP",
            "freshness_basis",
            "repo_scope",
            "determinism",
        ):
            if isinstance(sources.get(name), dict):
                found.append(name)
    return sorted(set(found))


def _attest_source_contributions(
    localization_proof: list[dict],
    block_receipts: list[dict],
) -> None:
    """Emit a producer-owned, self-sealed source-contribution attestation onto each
    rendered candidate proof that maps 1:1 to a sealed localization block.

    The attestation binds the candidate to the EXACT delivered block bytes
    (``block_content_sha256`` == the block receipt's content hash) and names the ACQ
    sources the producer used to render that candidate. It is self-sealed
    (``attestation_sha256`` over the canonical attestation minus that field, the same
    scheme as ``_cochange_evidence``'s ``source_identity_sha256``), so any downstream
    tamper of the candidate binding, the block seal, or the source list is detectable.

    HOST-SIDE METADATA — never rendered into ``brief_text``; a PURE derivation of the
    already-computed proof + receipt data, so the delivered brief is byte-identical
    whether or not attestations are populated. A candidate that does not map to exactly
    one sealed localization block gets NO attestation (fail-closed -> the collector
    keeps ``source_contribution_correct`` at ``None``)."""
    seal_by_candidate: dict[str, str] = {}
    ambiguous: set[str] = set()
    for receipt in block_receipts:
        if not isinstance(receipt, dict) or receipt.get("fact_class") != "localization":
            continue
        candidate_id = receipt.get("candidate_id")
        digest = receipt.get("content_hash")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        if not isinstance(digest, str) or _re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            continue
        if candidate_id in seal_by_candidate and seal_by_candidate[candidate_id] != digest:
            # More than one distinct localization block claims this candidate; the
            # producer cannot attest a single delivered binding -> stay silent.
            ambiguous.add(candidate_id)
            continue
        seal_by_candidate[candidate_id] = digest
    for proof in localization_proof:
        if not isinstance(proof, dict):
            continue
        candidate_id = proof.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in ambiguous:
            continue
        digest = seal_by_candidate.get(candidate_id)
        if digest is None:
            continue  # candidate never rendered into a sealed localization block
        attestation: dict[str, object] = {
            "kind": "source_contribution",
            "candidate_id": candidate_id,
            "block_content_sha256": digest,
            "sources": _candidate_local_contribution_sources(proof),
        }
        canonical = json.dumps(
            attestation,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        attestation["attestation_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        proof["contribution_attestation"] = attestation


def _build_obligations_record(
    brief_text: str,
    block_receipts: list[dict],
    issue_text: str,
) -> dict:
    """Bind the delivered obligations block to its build-time extraction record.

    Returns a canonical ``gt.obligations_record.v1`` sidecar (candidate_id + block seal +
    the EXACT issue source identity + the extracted obligations digest/count) or ``{}``.

    Fail-closed and PURE: it reads only the already-persisted V2 obligations artifact
    (``gt_obligations_v2.json`` — written by ``_write_obligations_v2_artifact`` earlier in
    this same brief generation), never re-extracts, and never touches ``brief_text``. Any
    miss (no delivered obligations block, absent/mismatched artifact, span/hash mismatch)
    returns ``{}`` so the obligations attestation stays honestly UNMEASURED rather than
    binding a fabricated record."""
    if not isinstance(block_receipts, list) or not (issue_text or "").strip():
        return {}
    # Only the canonical obligations block is an extraction product.  The
    # ``expected-behavior`` issue echo deliberately shares the coarse
    # obligations fact class for ranking, but it is not clause extraction and
    # must never mint an obligations record or attestation.
    receipt = next(
        (
            r
            for r in block_receipts
            if isinstance(r, dict)
            and r.get("fact_class") == "obligations"
            and r.get("label") == "obligations"
        ),
        None,
    )
    if receipt is None:
        return {}
    candidate_id = receipt.get("candidate_id")
    content_hash = receipt.get("content_hash")
    span = receipt.get("char_span")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(content_hash, str)
        or _re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        or not isinstance(span, list)
        or len(span) != 2
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in span)
        or span[0] < 0
        or span[0] >= span[1]
        or span[1] > len(brief_text)
    ):
        return {}
    block = brief_text[span[0] : span[1]]
    if "<gt-obligations>" not in block and _OBLIGATION_NATIVE_HEADER not in block:
        return {}
    digest = hashlib.sha256(block.encode("utf-8", "surrogatepass")).hexdigest()
    if digest != content_hash:
        return {}
    issue_sha = hashlib.sha256(issue_text.encode("utf-8")).hexdigest()
    # Read the authoritative extracted obligations (V2 artifact) written this generation.
    try:
        artifact_path = os.path.join(
            os.path.dirname(_anchors_path()) or "/tmp", "gt_obligations_v2.json"
        )
        with open(artifact_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("issue_sha256") != issue_sha:
        return {}  # stale/cross-task artifact — never launder another issue's obligations
    clauses = data.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        return {}
    verbatims = [
        " ".join((c.get("verbatim_text") or "").split())
        for c in clauses
        if isinstance(c, dict) and (c.get("verbatim_text") or "").strip()
    ]
    if not verbatims:
        return {}
    obligations_digest = hashlib.sha256(
        json.dumps(verbatims, sort_keys=False, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "schema": "gt.obligations_record.v1",
        "candidate_id": candidate_id,
        "block_content_sha256": content_hash,
        "issue_sha256": issue_sha,
        "issue_revision": f"issue:{issue_sha}",
        "obligation_count": len(verbatims),
        "obligations_digest": obligations_digest,
    }


def _terminal_pretask_mediator_participation(
    brief_text: str,
    block_receipts: list[dict],
    *,
    budget_suppressed: list[str] | None = None,
    content_paths: set[str] | frozenset[str] | None = None,
    content_decision: str = "NO_EFFECT",
    content_reason: str = "no_content_candidate",
    semantic_anchor_paths: set[str] | frozenset[str] | None = None,
    semantic_localizer_paths: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Build terminal mediator rows from the final, model-visible brief only.

    Ranking/rendering may execute repeatedly while the token rail tightens.  This
    function runs after that loop and joins controls only to blocks that survived
    into ``brief_text``.  It is a pure sidecar builder and never changes the brief.
    """
    if not _block_receipts_on():
        return []
    import hashlib as _hashlib
    import os as _os

    suppressed = set(budget_suppressed or ())
    content_ids = {_localization_candidate_id(p) for p in (content_paths or ())}
    anchor_ids = {_localization_candidate_id(p) for p in (semantic_anchor_paths or ())}
    localizer_ids = {_localization_candidate_id(p) for p in (semantic_localizer_paths or ())}
    receipts_by_id = {
        str(r.get("candidate_id", "")): r
        for r in block_receipts
        if isinstance(r, dict) and r.get("candidate_id")
    }

    def _exact_bytes(receipt: dict | None) -> str:
        if not receipt:
            return ""
        span = receipt.get("char_span")
        if (
            not isinstance(span, list)
            or len(span) != 2
            or type(span[0]) is not int
            or type(span[1]) is not int
            or span[0] < 0
            or span[1] < span[0]
            or span[1] > len(brief_text)
        ):
            return ""
        return brief_text[span[0] : span[1]]

    rows: list[dict] = []

    def _append(
        feature_id: str,
        decision_site: str,
        decision: str,
        fact_class: str,
        candidate_id: str,
        candidate_bytes: str,
        reason: str,
    ) -> None:
        try:
            from groundtruth.runtime.control_participation import (
                build_control_participation,
                participation_to_dict,
            )

            rows.append(
                participation_to_dict(
                    build_control_participation(
                        feature_id=feature_id,
                        decision_site=decision_site,
                        decision=decision,
                        iteration=0,
                        candidate_bytes=candidate_bytes,
                        fact_class=fact_class,
                        candidate_id=candidate_id,
                        reason=reason,
                    )
                )
            )
        except Exception as exc:  # sidecar failure never changes delivered bytes
            seal = (
                _hashlib.sha256(candidate_bytes.encode("utf-8", "surrogatepass")).hexdigest()[:16]
                if candidate_bytes
                else ""
            )
            rows.append(
                {
                    "schema": "gt.control_participation.v1",
                    "control_ref": {
                        "category": "CAP",
                        "feature_id": feature_id,
                        "role": "mediator",
                    },
                    "decision_site": decision_site,
                    "decision": "ERROR",
                    "iteration": 0,
                    "candidate_chars": len(candidate_bytes),
                    "candidate_sha256_16": seal,
                    "fact_class": fact_class,
                    "candidate_id": candidate_id,
                    "reason": f"control_record_error:{type(exc).__name__}",
                }
            )

    obligation = next(
        (
            r
            for r in block_receipts
            if isinstance(r, dict)
            and r.get("fact_class") == "obligations"
            and r.get("label") == "obligations"
        ),
        None,
    )
    obligation_bytes = _exact_bytes(obligation)
    obligation_id = (
        str(obligation.get("candidate_id")) if obligation else "brief:block:obligations:none"
    )
    native_on = _brief_native_on()
    native_terminal = bool(obligation_bytes and _OBLIGATION_NATIVE_HEADER in obligation_bytes)
    if native_on:
        native_decision = (
            "APPLIED"
            if native_terminal
            else ("SUPPRESSED" if "obligations" in suppressed else "NO_EFFECT")
        )
        _append(
            "GT_BRIEF_NATIVE",
            "pretask.v1r_brief.native_render",
            native_decision,
            "obligations",
            obligation_id,
            obligation_bytes,
            "native_obligations_terminal"
            if native_terminal
            else (
                "token_rail" if native_decision == "SUPPRESSED" else "no_terminal_obligations_block"
            ),
        )

    ack_on = _ss_ack_form_on()
    if ack_on:
        if not native_on:
            ack_decision, ack_reason = "NO_EFFECT", "requires_brief_native"
        elif native_terminal:
            ack_decision, ack_reason = "APPLIED", "imperative_native_checklist_terminal"
        elif "obligations" in suppressed:
            ack_decision, ack_reason = "SUPPRESSED", "token_rail"
        else:
            ack_decision, ack_reason = "NO_EFFECT", "no_imperative_obligation"
        _append(
            "GT_SS_ACK_FORM",
            "pretask.v1r_brief.ack_requirement_filter",
            ack_decision,
            "obligations",
            obligation_id,
            obligation_bytes,
            ack_reason,
        )

    if (_os.environ.get("GT_CONTENT_LEG") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    ):
        surviving = sorted(content_ids & set(receipts_by_id))
        for candidate_id in surviving:
            _append(
                "GT_CONTENT_LEG",
                "pretask.v1r_brief.content_bm25_rank",
                "APPLIED",
                "localization",
                candidate_id,
                _exact_bytes(receipts_by_id[candidate_id]),
                content_reason,
            )
        if not surviving:
            terminal_decision = (
                content_decision if content_decision in {"ERROR", "SUPPRESSED"} else "NO_EFFECT"
            )
            _append(
                "GT_CONTENT_LEG",
                "pretask.v1r_brief.content_bm25_rank",
                terminal_decision,
                "localization",
                "localization:content:none",
                "",
                content_reason
                if terminal_decision in {"ERROR", "SUPPRESSED"}
                else "no_final_content_candidate",
            )

    if (_os.environ.get("GT_SEM_BODY") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    ):
        semantic_ids = anchor_ids | localizer_ids
        surviving = sorted(semantic_ids & set(receipts_by_id))
        for candidate_id in surviving:
            in_anchor = candidate_id in anchor_ids
            in_localizer = candidate_id in localizer_ids
            reason = (
                "anchor_and_localizer"
                if in_anchor and in_localizer
                else "anchor_only"
                if in_anchor
                else "localizer_only"
            )
            _append(
                "GT_SEM_BODY",
                "pretask.v1r_brief.semantic_body_rank",
                "APPLIED",
                "localization",
                candidate_id,
                _exact_bytes(receipts_by_id[candidate_id]),
                reason,
            )
        if not surviving:
            _append(
                "GT_SEM_BODY",
                "pretask.v1r_brief.semantic_body_rank",
                "NO_EFFECT",
                "localization",
                "localization:semantic-body:none",
                "",
                "no_final_semantic_body_candidate",
            )
    return rows


# B-30: HARD dose-rail enforcement. The budget loops in ``generate_v1r_brief``
# trim breadth (candidate entries) then detail (per-body-line cap) but can bottom
# out — a single entry + the body cap at its floor + fixed header/block overhead —
# STILL over budget (measured: caps 100/20/1 produced 244/242/242 tokens; the rail
# was never enforced). This enforcer guarantees ``_count_tokens(result) <= budget``
# by dropping whole rendered brief blocks LOWEST-priority first (recording each
# suppression), then — only if the protected core still exceeds the cap —
# truncating the residue. The localizer / ``.files`` are untouched: this trims the
# rendered DOSE, never which files the agent is told to consider (BRIEFING.md §3).
#
# Keep-priority (highest survives): active obligation > violated contract >
# causal edge (Witness/Callers) > repo law (Context/Spec) > companion surface
# (co-change/Calls/scope/related) > orientation (graph-map, steer notes). The
# <gt-localization> header, the <gt-task-brief> tags, the <gt-obligations> block,
# "Expected behavior", "EDIT-TARGET CONTRACTS", the file-list headers, and each
# file's Witness/Contract are PROTECTED by the passes; only the final hard
# truncate can trim them, and only when even that core exceeds the cap.
def _enforce_token_rail(text: str, budget: int) -> tuple[str, list[str]]:
    """Trim ``text`` to at most ``budget`` tokens, returning ``(text, suppressed)``.

    Brief-F8 contract: ``budget <= 0`` means the rail is DISABLED — the text is
    returned UNCHANGED with an empty ``suppressed`` list (there is no positive
    ceiling to enforce, and clamping to a hard-truncate would gut the brief to
    ~empty, which no caller wants). Every real caller passes a positive
    ``max_brief_tokens`` (default ``MAX_BRIEF_TOKENS``); a non-positive budget is the
    documented "no ceiling" escape hatch, not a request to erase the brief."""
    suppressed: list[str] = []
    if budget <= 0 or _count_tokens(text) <= budget:
        return text, suppressed
    lines = text.split("\n")

    def _tok() -> int:
        return _count_tokens("\n".join(lines))

    def _drop_tagged(open_tag: str, close_tag: str) -> bool:
        s = next((i for i, ln in enumerate(lines) if ln.strip() == open_tag), None)
        if s is None:
            return False
        e = next((j for j in range(s, len(lines)) if lines[j].strip() == close_tag), None)
        if e is None:
            return False
        del lines[s : e + 1]
        return True

    def _drop_until_blank(pred) -> bool:
        s = next((i for i, ln in enumerate(lines) if pred(ln)), None)
        if s is None:
            return False
        e = s + 1
        while e < len(lines) and lines[e].strip() != "" and not _is_brief_boundary(lines[e]):
            e += 1
        del lines[s:e]
        return True

    def _drop_lines(pred) -> bool:
        keep = [ln for ln in lines if not pred(ln)]
        if len(keep) == len(lines):
            return False
        lines[:] = keep
        return True

    # Lowest priority FIRST. Each op removes ONE unit; loop while still over.
    _passes = [
        ("graph-map", lambda: _drop_tagged("<gt-graph-map>", "</gt-graph-map>")),
        (
            "orientation-note",
            lambda: _drop_until_blank(
                lambda ln: any(ln.startswith(p) for p in _ORIENTATION_NOTE_PREFIXES)
            ),
        ),
        ("scope-chain", lambda: _drop_until_blank(lambda ln: ln.startswith("Scope chain"))),
        (
            "related-files",
            lambda: _drop_until_blank(
                lambda ln: (
                    ln.startswith("Related files to inspect")
                    or ln.startswith("Likely multi-file scope")
                )
            ),
        ),
        (
            "companion-candidates",
            lambda: _drop_until_blank(lambda ln: ln.startswith("Other candidates")),
        ),
        (
            "calls",
            lambda: _drop_lines(
                lambda ln: (
                    ln.lstrip().startswith("Calls:") or ln.lstrip().startswith("Also changes:")
                )
            ),
        ),
        (
            "context",
            lambda: _drop_lines(
                lambda ln: ln.lstrip().startswith("Context:") or ln.lstrip().startswith("Spec:")
            ),
        ),
        ("callers", lambda: _drop_lines(lambda ln: ln.lstrip().startswith("Callers:"))),
    ]
    for label, op in _passes:
        while _tok() > budget and op():
            if label not in suppressed:
                suppressed.append(label)
        if _tok() <= budget:
            break

    text2 = "\n".join(lines)
    if _count_tokens(text2) <= budget:
        return text2, suppressed

    # Phase 2: the passes were insufficient (a single entry's protected core still
    # exceeds the cap). Rebuild keeping HIGHEST-priority blocks first, then re-emit
    # them in original order. This is what lets the top-priority active OBLIGATION
    # survive even though it renders near the END of the brief — a naive prefix
    # truncation would keep the file list and cut the obligation, inverting the
    # doctrine's fact order. Scaffold (localization header + <gt-task-brief> tags)
    # is always kept; blocks that don't fit are recorded as suppressed.
    blocks = _segment_brief_blocks(text2)
    kept = [b for b in blocks if b["priority"] < 0]  # scaffold, always
    _dropped_file = False
    for b in sorted(blocks, key=lambda b: (b["priority"], b["order"])):
        if b["priority"] < 0:
            continue
        _is_file = b["label"].startswith("file-entry")
        # File entries are a rank-ordered prefix: once a higher-ranked entry is
        # dropped, never keep a lower-ranked one (no localization rank inversion).
        if _is_file and _dropped_file:
            if b["label"] not in suppressed:
                suppressed.append(b["label"])
            continue
        trial = sorted(kept + [b], key=lambda x: x["order"])
        if _count_tokens("\n".join(x["text"] for x in trial)) <= budget:
            kept.append(b)
        else:
            if _is_file:
                _dropped_file = True
            if b["label"] not in suppressed:
                suppressed.append(b["label"])
    result = "\n".join(x["text"] for x in sorted(kept, key=lambda x: x["order"]))
    if _count_tokens(result) <= budget:
        return result, suppressed

    # HARD last resort: even the scaffold exceeds the cap (pathologically tiny
    # budget). Char-length binary search (fewer chars => fewer-or-equal tokens for
    # these tokenizers) so the returned text NEVER exceeds the cap.
    if "truncated" not in suppressed:
        suppressed.append("truncated")
    lo, hi = 0, len(result)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _count_tokens(result[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return result[:lo].rstrip(), suppressed


def _file_has_graph_edge(graph_db: str, file_path: str) -> bool:
    """True iff at least one edge (CALLS/CONTAINS/EXTENDS/...) is incident to a node
    defined in ``file_path``. This is the observability probe behind
    ``graph_edge_count`` — it proves a candidate is structurally connected in the
    graph (not a pure lexical/semantic guess). Reuses the same simple per-file
    edge logic ``_file_is_namematch_only`` uses, but counts ANY edge type.

    Returns False on any error / missing db / no edges (honest: absence of proof of
    a graph edge is reported as "no graph edge", never assumed-true)."""
    if not graph_db or not file_path:
        return False
    conn = None
    try:
        conn = sqlite3.connect(graph_db)
        row = conn.execute(
            """
            SELECT 1 FROM edges e JOIN nodes n ON n.id = e.source_id
              WHERE n.file_path = ?
            UNION ALL
            SELECT 1 FROM edges e JOIN nodes n ON n.id = e.target_id
              WHERE n.file_path = ?
            LIMIT 1
            """,
            (file_path, file_path),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _tier_from_loc_header(loc_header: str) -> str:
    """Extract HIGH/MEDIUM/LOW from the rendered ``_localization_header`` block.

    ``_localization_header`` emits ``<gt-localization confidence="high|medium|low">``
    (§4.1). We surface that SAME tier so a metrics reader sees exactly the
    confidence the agent received. Empty header (abstain / correct-or-quiet) ->
    ``"low"`` (no confident steer was delivered)."""
    if not loc_header:
        return "low"
    m = _re.search(r'confidence="(high|medium|low)"', loc_header, _re.IGNORECASE)
    return m.group(1).upper() if m else "low"


class _RankedEntry:
    """A path-only stand-in so acquisition counting needs no DELIVERED entry.

    `_l1_signal_counts` reads only `.path`, `.witness` and `.localizer_confidence` off an entry;
    a ranked record carries its witness inside `components`, which the counter already handles.
    """

    __slots__ = ("path", "witness", "localizer_confidence")

    def __init__(
        self,
        path: str,
        *,
        witness: str = "",
        localizer_confidence: float = 0.0,
    ) -> None:
        self.path = path
        self.witness = witness
        self.localizer_confidence = localizer_confidence


# C15 — the wire form of "this question has no answer on this run", for JSON readers that
# cannot carry ``None``. A reader must never see 0 where the honest answer is "the re-slot
# removed the population this counter is defined over".
_NOT_EVALUABLE = "NOT_EVALUABLE"


def _l1_acquisition_counts(
    graph_db: str,
    records: list[dict],
    *,
    witness_by_file: dict[str, str] | None = None,
    localizer_confidence_by_file: dict[str, float] | None = None,
) -> tuple[int, int, int, int]:
    """The same four signal counts, over what was ACQUIRED — independent of delivery.

    WHY THIS EXISTS. `_l1_signal_counts` counts over the DELIVERED candidate set, which is
    correct for a delivery claim but is stored in fields named `fts5_signal_count`,
    `semantic_signal_count`, `structural_signal_count`, `graph_edge_count`. Those names mean
    ACQUISITION. Under `GT_BRIEF_MINIMAL` + `GT_LOC_RESLOT` the brief reduction deletes every
    localization block, so the delivered set is empty BY CONSTRUCTION and all four read 0.

    Measured cost of that conflation (run 30297116212): the counters read 0 while the SAME run's
    `embedder_certificate.json` reported `semantic_candidate_count: 112` and driving the
    production localizer against that run's own graph produced 50 candidates. The zero was read
    as "the acquisition subsystem is dark", written into the architecture state-of-record, and
    used to redirect a day of work. It was a broken gauge, not a broken subsystem.

    Both facts are wanted. They must not share a name: this answers "what did the legs find",
    `_l1_signal_counts` answers "what reached the model".
    """
    witness_by_file = witness_by_file or {}
    localizer_confidence_by_file = localizer_confidence_by_file or {}
    entries = []
    for record in records:
        if not isinstance(record, dict) or not record.get("path"):
            continue
        path = str(record.get("path", ""))
        normalized = path.replace("\\", "/").lstrip("./").lstrip("/")
        entries.append(
            _RankedEntry(
                path,
                witness=(witness_by_file.get(path) or witness_by_file.get(normalized) or ""),
                localizer_confidence=float(
                    localizer_confidence_by_file.get(path)
                    or localizer_confidence_by_file.get(normalized)
                    or 0.0
                ),
            )
        )
    aligned = [r for r in records if isinstance(r, dict) and r.get("path")]
    return _l1_signal_counts(graph_db, entries, aligned)  # type: ignore[arg-type]


def _acquisition_proof_rows(
    records: list[dict],
    *,
    witness_by_file: dict[str, str],
    witness_verified_by_file: dict[str, bool],
) -> list[dict[str, object]]:
    """Project ranked candidates into acquisition-only proof rows.

    The population is ``records`` (the terminal ranked set), never the rendered
    candidate set. Only the four C16 acquisition-leg inputs are admitted. In
    particular this array cannot carry block seals, contribution attestations,
    co-change evidence, or the extended sources whose proof contract requires a
    delivered localization candidate.
    """
    proof: list[dict[str, object]] = []
    for rank, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        path = str(record.get("path", "") or "")
        if not path:
            continue
        normalized = path.replace("\\", "/").lstrip("./").lstrip("/")
        raw_components = record.get("components")
        raw_components = raw_components if isinstance(raw_components, dict) else {}
        components: dict[str, float] = {}
        for name in ("reach", "lex", "sem"):
            try:
                components[name] = float(raw_components.get(name, 0.0) or 0.0)
            except (TypeError, ValueError):
                components[name] = 0.0
        witness = witness_by_file.get(path) or witness_by_file.get(normalized) or ""
        witness_verified = bool(
            witness_verified_by_file.get(path) or witness_verified_by_file.get(normalized)
        )
        proof.append(
            {
                "candidate_id": _localization_candidate_id(path),
                "rank": rank,
                "path": path,
                "witness": witness,
                "witness_verified": witness_verified,
                "components": components,
            }
        )
    return proof


def _l1_signal_counts(
    graph_db: str,
    entries: list[FileEntry],
    records: list[dict],
) -> tuple[int, int, int, int]:
    """Count, over the RENDERED candidate set, how many candidates carry each
    independent localization signal as a NONZERO contribution. Pure observation —
    no ranking effect.

    Returns ``(graph_edge_count, semantic_signal_count, structural_signal_count,
    fts5_signal_count)``.

    - semantic: v74 ``components['sem']`` > 0 (the ONNX/semantic retrieval score).
    - fts5/BM25 (lexical recall spine): v74 ``components['lex']`` > 0.
    - structural/graph-reach: v74 ``components['reach']`` > 0 OR the candidate
      carries a graph-traversal witness / positive localizer confidence (the
      graph_localizer surfaced it via a CALLS/IMPORTS witness — structural by
      construction).
    - graph_edge_count: per candidate FILE, a real incident edge exists in
      graph.db (``_file_has_graph_edge``).

    ``records`` is the per-entry ``top_records`` slice (same order as ``entries``),
    each a dict with a ``components`` sub-dict from run_v74. A record may be a
    promoted graph-witness candidate with ``components={'witness': conf}`` and no
    ``sem``/``lex`` — those count toward structural via the witness, correctly."""
    graph_edges = 0
    sem = 0
    struct = 0
    fts5 = 0
    # Cache per-path edge presence so repeated paths don't re-query.
    _edge_cache: dict[str, bool] = {}
    for i, entry in enumerate(entries):
        rec = records[i] if i < len(records) else {}
        comps = rec.get("components", {}) if isinstance(rec, dict) else {}

        if float(comps.get("sem", 0.0) or 0.0) > 0.0:
            sem += 1
        if float(comps.get("lex", 0.0) or 0.0) > 0.0:
            fts5 += 1

        _reach = float(comps.get("reach", 0.0) or 0.0)
        _witnessed = (
            bool(getattr(entry, "witness", ""))
            or getattr(entry, "localizer_confidence", 0.0) > 0.0
            or float(comps.get("witness", 0.0) or 0.0) > 0.0
        )
        if _reach > 0.0 or _witnessed:
            struct += 1

        path = entry.path
        if path not in _edge_cache:
            _edge_cache[path] = _file_has_graph_edge(graph_db, path)
        if _edge_cache[path]:
            graph_edges += 1
    return graph_edges, sem, struct, fts5


def _terminal_acquisition_components(
    components: dict[str, float],
    candidate_path: str,
    *,
    body_paths: set[str] | frozenset[str],
) -> dict[str, float]:
    """Attach only terminal, candidate-local acquisition participation facts."""
    out = dict(components)
    normalized_body = {_gl_normalize(path) for path in body_paths}
    if _gl_normalize(candidate_path) in normalized_body:
        # Boolean participation, not a fabricated similarity magnitude: this
        # candidate survived after a real content-FTS or body-embedding leg.
        out["body"] = 1.0
    return out


# --- Decision 26: Cross-Domain Bridging via Co-Change + Test Co-Import ---


def _detect_overconfident_convergence(top_records: list[dict], graph_db: str) -> bool:
    """Detect when all top candidates cluster in same module — symptom-not-cause risk."""
    if len(top_records) < 3:
        return False

    # Check directory concentration
    dirs = [os.path.dirname(r.get("path", "")) for r in top_records[:5]]
    unique_dirs = set(dirs)
    if len(unique_dirs) > 2:
        return False  # Spread across modules — not convergent

    # Check if BM25 dominates (lex component > 50% of total score for all top-5)
    bm25_dominant = all(
        r.get("components", {}).get("lex", 0) > 0.5 * r.get("score", 1)
        for r in top_records[:5]
        if r.get("score", 0) > 0
    )

    return bm25_dominant and len(unique_dirs) <= 2


def _expand_via_cochange(
    symptom_files: list[str], repo_root: str, max_expansion: int = 3
) -> list[dict]:
    """Find files in other modules that co-changed with symptom files in git history."""
    symptom_dirs = {os.path.dirname(f) for f in symptom_files}
    cochange_counts: dict[str, int] = {}
    cochange_rows: dict[str, list[dict[str, object]]] = {}
    source_revision = ""
    current_commit = ""

    # Get last 100 commits
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--name-only", "-100"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    # Parse commits through one flush path so blank separators are not required.
    current_files: list[str] = []

    def _flush_commit() -> None:
        if not current_commit or not current_files:
            return
        symptom_paths = sorted({f for f in current_files if f in symptom_files})
        if not symptom_paths:
            return
        for candidate in dict.fromkeys(current_files):
            if candidate in symptom_files or os.path.dirname(candidate) in symptom_dirs:
                continue
            cochange_counts[candidate] = cochange_counts.get(candidate, 0) + 1
            cochange_rows.setdefault(candidate, []).append(
                {
                    "commit": current_commit,
                    "symptom_paths": symptom_paths,
                }
            )

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            _flush_commit()
            current_commit = ""
            current_files = []
        elif _re.fullmatch(r"[0-9a-f]{40}", line):
            _flush_commit()
            current_commit = line
            if not source_revision:
                source_revision = line
            current_files = []
        else:
            current_files.append(line)

    _flush_commit()

    # Rank by co-change frequency, require >= 2
    ranked = sorted(cochange_counts.items(), key=lambda x: (-x[1], x[0]))
    return [
        {
            "path": f,
            "score": 0.0,
            "components": {"cochange": count},
            "entered_via": "cochange",
            "cochange_evidence": _cochange_evidence(
                candidate_path=f,
                source_revision=source_revision,
                source_rows=cochange_rows.get(f, []),
                history_limit=100,
            ),
        }
        for f, count in ranked[:max_expansion]
        if count >= 2
    ]


def _cochange_evidence(
    *,
    candidate_path: str,
    source_revision: str,
    source_rows: list[dict[str, object]],
    history_limit: int,
) -> dict[str, object]:
    """Build a stable producer-owned truth/freshness witness for one bridge."""
    evidence: dict[str, object] = {
        "kind": "cochange_history",
        "source": "git_log",
        "source_revision": source_revision,
        "history_limit": history_limit,
        "candidate_path": candidate_path,
        "count": len(source_rows),
        "source_rows": source_rows,
    }
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    evidence["source_identity_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return evidence


def _primary_cochange_evidence(
    *,
    candidate_path: str,
    co_change_paths: list[str],
) -> dict[str, object]:
    """Self-sealed co-change witness for a PRIMARY localization candidate.

    Distinct from the cross-domain bridge witness (``_cochange_evidence``): the
    bridge proves a candidate co-changed with the SYMPTOM files across dated
    commits, whereas this proves the delivered localization candidate carries its
    own mined co-change neighbourhood — the files rendered on its
    ``Also changes: …`` line. That neighbourhood comes from the indexer's
    ``cochanges`` table (or the git-log miner) as a ranked file list with no
    per-commit rows, so the witness names the neighbours directly and self-seals
    them with the SAME sha256-over-canonical-JSON scheme the bridge uses. The
    neighbour list is sorted, de-duplicated and never includes the candidate
    itself, so a downstream reader can prove the rows were not mutated.
    """
    kept = sorted({c for c in co_change_paths if isinstance(c, str) and c and c != candidate_path})
    evidence: dict[str, object] = {
        "kind": "cochange_history",
        "source": "primary_path",
        "candidate_path": candidate_path,
        "count": len(kept),
        "co_change_paths": kept,
    }
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    evidence["source_identity_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return evidence


def _primary_cochange_support(
    *,
    candidate_path: str,
    entry_co_changes: list[str] | None,
    components: dict[str, float],
    bridge_evidence: object,
) -> tuple[object, list[str] | None]:
    """Stamp co-change SUPPORT onto a delivered localization candidate's proof.

    Returns ``(cochange_evidence, proof_co_changes)``. Keyed only on the general
    condition — *this delivered candidate carries a real (non-test) co-change
    neighbour list* — never on a task/repo/candidate id. A cross-domain bridge
    candidate already owns its self-sealed ``cochange_evidence`` and
    ``components["cochange"]`` (entered via ``cochange``); it is returned
    untouched so its proof stays byte-identical. Otherwise, when the neighbour
    list is non-empty and no cochange component is present yet, mint the
    primary-path witness and stamp ``components["cochange"]`` in place so the
    ACQ SOURCE-5 join stops being blind to it. When neither applies, the input
    evidence (``None`` for an ordinary candidate) is returned unchanged.
    """
    if isinstance(bridge_evidence, dict):
        return bridge_evidence, None
    # Mirror the rendered "Also changes: …" filter exactly: drop test/demo paths
    # and the candidate itself, then de-duplicate. Only a NON-EMPTY real
    # neighbourhood mints a witness (never a hollow count-0 support row).
    kept = sorted(
        {
            c
            for c in (entry_co_changes or [])
            if isinstance(c, str) and c and c != candidate_path and not _is_test_path(c)
        }
    )
    if not kept or float(components.get("cochange", 0.0) or 0.0) > 0.0:
        return bridge_evidence, None
    evidence = _primary_cochange_evidence(
        candidate_path=candidate_path,
        co_change_paths=kept,
    )
    components["cochange"] = float(evidence["count"])
    return evidence, list(evidence["co_change_paths"])  # type: ignore[arg-type]


def _expand_via_test_coimport(
    symptom_files: list[str], graph_db: str, max_expansion: int = 3
) -> list[dict]:
    """Find cross-domain bridges via shared test importers."""
    symptom_dirs = {os.path.dirname(f) for f in symptom_files}

    try:
        conn = sqlite3.connect(graph_db)

        # Find test files that import any symptom file
        placeholders = ",".join("?" * len(symptom_files))
        test_importers = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type IN ('CALLS', 'IMPORTS')
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nt.file_path IN ({placeholders})
              AND nsrc.is_test = 1
            """,
            symptom_files,
        ).fetchall()

        test_files = [r[0] for r in test_importers]
        if not test_files:
            conn.close()
            return []

        # Find OTHER non-test files imported by those same test files
        test_placeholders = ",".join("?" * len(test_files))
        bridges = conn.execute(
            f"""
            SELECT nt.file_path, COUNT(*) as cnt
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type IN ('CALLS', 'IMPORTS')
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path IN ({test_placeholders})
              AND nt.is_test = 0
              AND nt.file_path NOT IN ({placeholders})
            GROUP BY nt.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            test_files + symptom_files + [max_expansion * 3],
        ).fetchall()

        conn.close()

        # Filter to other modules only
        result: list[dict] = []
        for path, count in bridges:
            if os.path.dirname(path) not in symptom_dirs:
                result.append(
                    {
                        "path": path,
                        "score": 0.0,
                        "components": {"test_coimport": count},
                        "entered_via": "test_coimport",
                    }
                )
            if len(result) >= max_expansion:
                break
        return result
    except Exception:
        return []


# Anchor-proximity floor for the [WARNING] tier. anchor_prox = min(1, n_issue_
# anchors_within_1_hop / 3), so >= 0.33 means >= 1 issue-anchor is a direct
# call-graph neighbour — a real structural subject match, not float noise. Keeps
# anchor-matched but witness-less gold out of the [INFO] drop (BUG-3).
def _anchors_path(*, for_write: bool = False) -> str:
    """Per-task gt_issue_anchors.json path (B10). Mirrors gt_mini_patch._anchors_path so
    the brief's WRITE lands where the in-container consumers READ. Priority:
    GT_ANCHORS_PATH -> $GT_CERT_DIR/gt_issue_anchors.json -> /tmp fallback. GT_CERT_DIR is
    the per-task substrate mount, so keying on it stops two tasks on one host from sharing
    a single mutable /tmp file (determinism / cross-task isolation). for_write=True skips
    the isfile check (the file does not exist yet at write time)."""
    p = os.environ.get("GT_ANCHORS_PATH")
    if p:
        return p
    cert = os.environ.get("GT_CERT_DIR", "")
    if cert:
        cand = os.path.join(cert, "gt_issue_anchors.json")
        if for_write or os.path.isfile(cand):
            return cand
    return "/tmp/gt_issue_anchors.json"


_ANCHOR_PROX_WARN_FLOOR = 0.33


def _entry_confidence_tier(entry: FileEntry, issue_text: str = "") -> str:
    """Per-entry confidence tag per CLAUDE.md:222.

    [VERIFIED] = strong graph backing (callers with code, or issue-text symbol
                 match plus any caller evidence)
    [WARNING]  = mid graph backing (callers shown but only file:line, or test
                 mapping present)
    [INFO]     = lexical/semantic retrieval only, no graph evidence

    Used by render_brief() so the agent can weigh each candidate. Follows
    Cursor-style honesty per .claude/CLAUDE.md: never present low-confidence
    guesses as confident ranked facts.
    """
    relevance_grade = str(getattr(entry, "relevance_grade", "") or "").upper()
    if relevance_grade:
        if relevance_grade == "VERIFIED":
            return "[VERIFIED]"
        if relevance_grade == "WARNING":
            return "[WARNING]"
        return "[INFO]"

    # Legacy/non-localizer fallback. HI-tier rendering format from
    # _caller_contract_for_file is
    # "func_name() in file.py:line `code`". Anchor on "() in " to avoid
    # false positives from paths containing the substring " in ".
    contract_has_func_names = "() in " in (entry.contract or "")
    contract_present = bool(entry.contract)
    # Use function_names (raw names) for issue matching, not functions
    # (which are signatures). Threshold len(fn) > 2 to keep names like "cli".
    issue_match = False
    path_match = False
    if issue_text:
        _it = issue_text.lower()
        _names = entry.function_names or entry.functions
        issue_match = any(fn.lower() in _it for fn in _names if len(fn) > 2)
        # Path-name issue match: a candidate whose file STEM matches an issue
        # keyword is localization evidence INDEPENDENT of graph edges. RUN VERDICT
        # (beancount-931 26619606504): plugins/leafonly.py had reach=0 -> no
        # contract / no test mapping -> was [INFO]-dropped, despite the issue
        # naming the "leafonly plugin". Per .claude/CLAUDE.md, context that does
        # not need edges must fire even on isolated files; an isolated-but-named
        # gold must NOT lose the brief slot to a connected-but-wrong hub.
        # #37: anchor the stem match on a WORD BOUNDARY and raise the specificity
        # floor, so a generic stem (`core`/`base`/`data`) does not promote a file to
        # [WARNING] merely because the substring appears anywhere in the issue text
        # (e.g. "base" inside "database", "core" inside "scoreboard"). Reuse the
        # codebase's own anti-generic rule from _exact_issue_named_files
        # (len >= 5 OR contains "_"): a short, single-token stem is too generic to be
        # localization evidence on its own. Correct-or-quiet; generalized (no per-repo
        # names), language-invariant (filename specificity, not Python-specific).
        _stem = os.path.splitext(os.path.basename(entry.path or ""))[0].lower()
        _stem_specific = len(_stem) >= 5 or "_" in _stem
        path_match = _stem_specific and bool(_re.search(rf"\b{_re.escape(_stem)}\b", _it))

    # A verified GRAPH-TRAVERSAL witness (graph_localizer): the file is connected
    # to an issue-anchored symbol by a DETERMINISTIC CALLS/IMPORTS edge. This is
    # the strongest localization evidence we have — a structural fact, not a
    # lexical guess — so it earns [VERIFIED] on its own (the whole point of the
    # rebuild: importer.py, witnessed via set_fields->set_parse, must be [VERIFIED]
    # even though it loses the keyword contest to pipeline.py).
    if getattr(entry, "witness_verified", False):
        return "[VERIFIED]"
    if contract_has_func_names or (issue_match and contract_present):
        return "[VERIFIED]"
    # An unverified (name_match) witness is real but weak structural evidence —
    # mid-tier, never [VERIFIED] (correct-or-quiet: a name_match is not a fact).
    if getattr(entry, "witness", ""):
        return "[WARNING]"
    # A candidate with positive localizer confidence (entered via path-to-seed
    # or any graph traversal path) carries structural evidence even when no
    # single witness rendered. The localizer scored it > 0, which means it
    # connected to issue-anchored symbols. Research: KGCompass (2025) — the
    # issue-mentioned entity can be a MODULE (path match), not just a function.
    # Correct-or-quiet: localizer_confidence > 0 is real graph evidence, not a
    # lexical guess, so it earns [WARNING] rather than being dropped as [INFO].
    _loc_conf = getattr(entry, "localizer_confidence", 0.0)
    if _loc_conf > 0.1:
        return "[WARNING]"
    # v74 anchor proximity: the file is a 1-hop call-graph neighbour of >=1 symbol
    # NAMED IN THE ISSUE (anchor_prox = min(1, n_anchors_within_1hop / 3); any value
    # >= ~0.33 <=> >=1 anchor neighbour). This is EDGE-INDEPENDENT issue-SUBJECT
    # evidence — exactly the context .claude/CLAUDE.md says must fire even without a
    # verified caller witness ("never gate edge-free issue-subject context behind a
    # connectivity check"). So an anchor-matched file earns [WARNING] and SURVIVES the
    # [INFO] filter, rather than being dropped because its freshly-added gold functions
    # (set_xy1/set_xy2) are absent from the ref-count-ranked function_names so
    # issue_match fails (BUG-3: matplotlib lines.py had anchor_prox=1.0 yet was dropped,
    # leaving the witnessed non-gold hub _base.py as the sole primary edit-target).
    if getattr(entry, "anchor_prox", 0.0) >= _ANCHOR_PROX_WARN_FLOOR:
        return "[WARNING]"
    if contract_present or issue_match or path_match:
        return "[WARNING]"
    return "[INFO]"


def _graph_map_demand_on() -> bool:
    """B-27: the ``<gt-graph-map>`` is DEMAND-PAGED, not a step-0 narration dose.

    Default OFF — a step-0 brief carries NO graph-map (anti-narration doctrine:
    an unmeasured who-calls-whom dose the agent did not ask for is narration, not
    a decision-point fact). The map is emitted ONLY when an explicit demand signal
    (this flag / a future demand hook) requests it; when ON, the emitted block is
    byte-identical to the historical step-0 emission. Byte-identical when off
    (``_with_graph_map`` returns the brief unchanged). Robust truthy parse, parity
    with ``_obligations_v2_on``."""
    import os as _os

    return (_os.environ.get("GT_GRAPH_MAP_DEMAND") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def _with_graph_map(
    brief: str,
    files: list[FileEntry],
    graph_db: str,
    body_line_cap: int = _MAX_BODY_LINE_CHARS,
) -> str:
    """Surface the deterministic 1-hop curation map as a LEADING <gt-graph-map>
    block — callers/callees of the top shown files' focus functions.

    B-27 (demand-gate): DISABLED at step-0 by default. Returns ``brief`` unchanged
    unless ``_graph_map_demand_on()`` (an explicit demand signal) requests the map.
    A step-0 brief is by definition not demand-triggered, so the default path emits
    no graph-map — reconciling gt_layers.md §2.3 (retired at step-0) with the code.

    Also returns ``brief`` unchanged when graph_db is unset, when no shown file has
    a focus function, or when no connection clears the correct-or-quiet bar
    (render_map returns '' — honest abstention, never a guess). The map obeys the
    SAME categorical rule as the caller gate: a deterministic edge renders as a
    fact; a name_match edge renders only ever as ``(unverified)``.

    D3 (CLAUDE.md "the brief's value is the graph map, not the file ranking";
    Lost-in-the-Middle TACL 2024 — primacy beats burial): this who-calls-whom map
    is the UNIQUE value the agent's own grep loop cannot cheaply rebuild, so — WHEN
    demand-paged — it is placed FIRST (immediately after the <gt-task-brief> open
    tag), not appended at ~96% position behind the evidence wall. The map's own
    body lines are capped the same way the evidence bodies are (the "called by:"
    fan-in line can run long). Falls back to a trailing append only when the open
    tag is absent.
    """
    import os as _os

    gateway_on = (_os.environ.get("GT_GATEWAY") or "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )
    if gateway_on or not _graph_map_demand_on():
        return brief
    if not graph_db or not files:
        return brief
    try:
        from groundtruth.pretask.curation_map import build_function_map, render_map
    except Exception:
        return brief

    # FOCUS SELECTION (root cause B, 2026-06-17): the leading <gt-graph-map> must
    # honor the LOCALIZER FILE RANK. The old loop took ONLY function_names[0] per
    # file, so when the #1 file's chosen focus ABSTAINED (empty fact-map, e.g. a
    # freshly-added 0-caller gold or a function with no confident edges),
    # render_map silently skipped it and rendered a LOWER-ranked file instead —
    # the lead ceded, the gold's map block erased. Fix: for each file try its
    # candidate focus functions IN RANK ORDER and keep the FIRST that yields a
    # visible block, so the #1 file's lead is preserved via its NEXT structurally-
    # central function before any lower file can take the lead. Per file we still
    # contribute at most ONE visible block (compact, correct-or-quiet).
    _PER_FILE_FOCUS_TRIES = 3

    def _first_visible_focus(_path: str, _fns: list[str]) -> tuple[str, str] | None:
        for _fn in [x for x in (_fns or []) if x][:_PER_FILE_FOCUS_TRIES]:
            try:
                _m = build_function_map(graph_db, [(_path, _fn)])
            except Exception:
                return None
            if _m and _m[0].has_visible:
                return (_path, _fn)
        return None

    focus: list[tuple[str, str]] = []
    for f in files[:3]:
        _hit = _first_visible_focus(f.path, f.function_names or [])
        if _hit is not None:
            focus.append(_hit)
        elif f.function_names:
            # No visible focus in this file — keep the rank-0 function so a file
            # with edges below the visibility bar is still REPRESENTED in rank
            # order (render_map will drop it if truly empty; this never reorders).
            _f0 = next((x for x in f.function_names if x), "")
            if _f0:
                focus.append((f.path, _f0))
    if not focus:
        return brief
    try:
        block = render_map(build_function_map(graph_db, focus))
    except Exception:
        return brief
    if not block:
        return brief
    # Cap each map body line (the "  calls:" / "  called by:" fan-in lines) so the
    # leading map stays compact; the "<gt-graph-map>"/header/"</gt-graph-map>"
    # structural lines pass through unchanged (already short).
    block = "\n".join(
        _clip_body_line(ln, body_line_cap) if ln.startswith("  ") else ln
        for ln in block.split("\n")
    )
    # Place the map FIRST: inject right after the opening <gt-task-brief> tag so the
    # actionable who-calls-whom map leads the brief the agent reads. Idempotent +
    # correct-or-quiet: a brief without the open tag (empty-files edge case) falls
    # back to the historical trailing append.
    _open = "<gt-task-brief>"
    idx = brief.find(_open)
    if idx == -1:
        return f"{brief}\n{block}"
    insert_at = idx + len(_open)
    return brief[:insert_at] + "\n" + block + brief[insert_at:]


_MAX_EDIT_TARGET_CONTRACT_LINES = 5


def _edit_target_contracts_block(graph_db: str, top: FileEntry) -> list[str]:
    """Render the EDIT-TARGET CONTRACTS sub-block for the top-ranked file, or [].

    Lists each verified callee of the top file's edit-target functions with its
    signature + definition location, e.g.::

        EDIT-TARGET CONTRACTS (importer.py):
          set_fields -> calls set_parse(self, key, string: str)  [beets/dbcore/db.py:722]

    Correct-or-quiet: returns [] (block omitted) when no verified callee with a
    signature exists. Capped to a few lines so the block stays inside budget.
    """
    func_names = top.function_names or []
    if not func_names:
        return []
    try:
        callees = edit_target_callee_contracts(graph_db, top.path, func_names)
    except Exception:
        return []
    if not callees:
        return []
    header = f"EDIT-TARGET CONTRACTS ({os.path.basename(top.path)}):"
    out = [header]
    for cc in callees:
        if len(out) - 1 >= _MAX_EDIT_TARGET_CONTRACT_LINES:
            break
        sig = _callee_sig_args(cc.signature, cc.callee)
        loc = f"  [{cc.file}:{cc.line}]" if cc.line else f"  [{cc.file}]"
        out.append(f"  {cc.caller} -> calls {sig}{loc}")
    # Header alone (no rendered callees) is not a fact — suppress it.
    return out if len(out) > 1 else []


# Behavioral-obligation block (the contract/obligation pillar). The issue's own
# requirement sentences (modals / behavior verbs / API-shape qualifiers) are mined
# DETERMINISTICALLY by spec.extract_spec and ALREADY persisted to
# gt_issue_anchors.json, but were never RENDERED into the brief — so the agent never
# saw the parsed behavioral spec (run-grounded: the rust pest task whose failure was
# coalescing SEMANTICS; the agent never saw the obligation). This block closes that
# gap for ANY issue that states behavioral requirements (generalized — pure regex
# over English requirement grammar, no repo/task/keyword logic; spec.py LEG 1).
#
# Correct-or-quiet (Cursor mentality): an obligation is rendered ONLY when its
# verbatim text overlaps a FOCUS ANCHOR — the rendered edit-target functions'
# identifier tokens (NOT the issue text, which would always "overlap" since the
# obligations are drawn from it) — reusing the SAME passes_relevance_gate the other
# non-edge signals use. No overlap → emit nothing. Fail-closed leakage guard: an
# obligation that names a pytest test (test_*/*_test), a FAIL_TO_PASS / PASS_TO_PASS
# token, or a rendered gold-file path token is DROPPED whole (never benchmaxx off
# the grader's test names — gt_trial §6 leakage rule).
_OBLIGATION_BUDGET = 4  # at most N obligation lines (compact, high-precision)
_OBLIGATION_LINE_CHARS = 200  # per-obligation verbatim cap


def _obligations_v2_on() -> bool:
    """Resolve structured obligations for the production Profile-2 path.

    ``GT_OBLIGATIONS_V2`` remains an explicit diagnostic override/kill switch.
    When it is absent, the live workflow's explicit Profile-2 activation owns
    this behavior through the existing obligation capability; no extra CAP row
    or unreceipted feature flag is introduced.
    """
    import os as _os

    _raw = _os.environ.get("GT_OBLIGATIONS_V2")
    if _raw is not None and _raw.strip():
        return _raw.strip().lower() not in ("0", "false", "no", "off")
    return (_os.environ.get("GT_RL_PROFILE") or "").strip() == "2"


def _dynamic_obligation_budget(n_clauses: int) -> int:
    """T1 budget scaling with spec density: K = clamp(ceil(n/3), 4, 10).
    n<=12 keeps today's dose (4); a 25-clause spec renders 9; ceiling 10 bounds
    the block at <=10x200 chars inside the existing brief token rail."""
    import math as _math

    return max(_OBLIGATION_BUDGET, min(10, _math.ceil(max(0, n_clauses) / 3)))


_V2_KIND_PRIORITY = {"error": 0, "signature": 1, "behavior": 2, "compat": 3, "repro": 4}


def _v2_order_key(row: dict) -> tuple:
    """Deterministic total order: modality strength desc, symbol specificity
    desc (compound-symbol count, then max symbol length), kind priority,
    document order asc."""
    subj = row.get("subject_symbols") or []
    compound = sum(1 for s in subj if ("_" in s or "." in s or any(c.isupper() for c in s[1:])))
    maxlen = max((len(s) for s in subj), default=0)
    return (
        -int(row.get("modality_strength") or 0),
        -compound,
        -maxlen,
        _V2_KIND_PRIORITY.get(row.get("kind") or "", 9),
        int(row.get("_doc_index") or 0),
    )


def _write_obligations_v2_artifact(
    rows: list[dict], gold_path_tokens: set[str], issue_text: str
) -> None:
    """T2: persist the FULL leak-screened clause list next to the anchors file —
    gt_obligations_v2.json (machine, incl. render_path_tokens so the in-container
    T3 screen applies the IDENTICAL leak rule) + gt_obligations.md (the agent's
    checklist; issue-verbatim rows only — adds structure, zero new information).
    Fail-open: the artifact must never break the brief."""
    try:
        import hashlib as _hashlib_v2
        import json as _j
        import os as _os

        target_dir = _os.path.dirname(_anchors_path(for_write=True)) or "/tmp"
        clean = [{k: v for k, v in o.items() if not k.startswith("_")} for o in rows]
        md = [
            "# GT obligations checklist — every requirement extracted from the issue",
            "# Verify each before submitting; tick what you have EXERCISED with a test.",
            "",
        ]
        for o in clean:
            verb = " ".join((o.get("verbatim_text") or "").split())
            md.append(
                f"- [ ] ({o.get('clause_id', '')}, {o.get('kind', '')}, "
                f'{o.get("modality", "")}) "{verb}"'
            )
        md_text = "\n".join(md) + "\n"
        payload = {
            "obligations_version": 2,
            "issue_sha256": _hashlib_v2.sha256(issue_text.encode("utf-8")).hexdigest(),
            "checklist_sha256": _hashlib_v2.sha256(md_text.encode("utf-8")).hexdigest(),
            "render_path_tokens": sorted(gold_path_tokens),
            "clauses": clean,
        }
        # Dual-write target_dir AND /tmp — mirrors the gt_issue_anchors.json persist
        # resilience (this fn's sibling). The proof harness (gt_run_proof.emit_brief)
        # mirrors /tmp/gt_obligations_v2.json -> out_dir into the cert-dir handoff, so
        # writing /tmp is what lets these files reach the AGENT (T2 checklist + T3
        # activation). Without it they die in the brief-gen subprocess's cwd — the
        # 2026-07-08 witness gap (run 28975223607: T1 shipped, T2/T3 never activated).
        _dirs = [target_dir]
        if "/tmp" not in _dirs:
            _dirs.append("/tmp")
        for _d in _dirs:
            try:
                import tempfile as _tempfile_v2

                def _atomic_text(_path: str, _text: str) -> None:
                    _fd, _tmp = _tempfile_v2.mkstemp(prefix=f".{_os.path.basename(_path)}.", dir=_d)
                    try:
                        with _os.fdopen(_fd, "w", encoding="utf-8", newline="") as _f:
                            _f.write(_text)
                            _f.flush()
                            _os.fsync(_f.fileno())
                        # mkstemp is 0600; container-root writer vs non-root host reader
                        # (W1a-PERM class, sweep A2 latent) — publish world-readable so a
                        # future harvest of these artifacts can never silently EACCES.
                        _os.chmod(_tmp, 0o644)
                        _os.replace(_tmp, _path)
                    except BaseException:
                        try:
                            _os.unlink(_tmp)
                        except OSError:
                            pass
                        raise

                # JSON is the bundle commit marker: publish checklist bytes first,
                # then atomically replace their identity/digest-bearing manifest.
                _atomic_text(_os.path.join(_d, "gt_obligations.md"), md_text)
                _atomic_text(
                    _os.path.join(_d, "gt_obligations_v2.json"),
                    _j.dumps(payload),
                )
            except OSError:
                continue
    except Exception:
        pass


_F2P_TOKEN_RE = _re.compile(r"\b(?:FAIL_TO_PASS|PASS_TO_PASS|fail_to_pass|pass_to_pass)\b")
# LEAK LAW — the test-identifier + assertion + test-path screens are SINGLE-SOURCED with the seam
# (Fable-LIPI round-2, 2026-07-11). Round-1 tuned the brief's screen and the seam's prose screen
# (`gt_mini_patch._prose_leaks_test_identity`) SEPARATELY, and each missed a different half (the
# brief missed `assert_eq!` / `.test.ts` / `crate::tests::`; the seam missed the assertion keyword
# entirely; BOTH missed `Test_`/`TEST_` case+underscore variants; AND the brief regex dir leg was
# only `tests?/`, leaking `spec/`/`__tests__/`/`e2e/` file paths — seam round-2 Finding-1). To make
# the invariant STRUCTURAL rather than disciplinary, the brief now screens with the ONE canonical
# PREDICATE `native_render.prose_leaks_test_identity` (name + assertion + the path_policy dir belt),
# the same function the seam calls — they cannot drift. See that module for the language coverage +
# the production near-miss guards (`contest_handler`/`latest_value`/`std::collections`/`Testing*`).
from groundtruth.runtime.native_render import (  # noqa: E402
    prose_leaks_test_identity as _prose_leaks_test_identity,
)


def _obligation_is_leaky(verbatim: str, symbols, gold_path_tokens: set[str]) -> bool:
    """Fail-closed: True if the obligation must NOT be rendered.

    Drops the obligation WHOLE if its verbatim text (or any named symbol) carries a
    test name (ANY language convention — pytest ``test_*``/``*_test``, Go
    ``TestX``, camelCase ``testX``, a ``path.ext::node`` nodeid, or a ``tests/``
    path segment), a pytest ASSERTION keyword, or a FAIL_TO_PASS / PASS_TO_PASS
    marker — or a token equal to a rendered gold-file path component. These are
    grader-coupled surfaces — GT must surface ZERO test references so its output is
    identical if the grader swaps. ONE canonical screen: the assertion + test-name
    legs run HERE so every caller (the ``<gt-obligations>`` block AND the B-1
    Expected-Behavior spec) obeys the SAME leak law (Brief-F2 asymmetry closed).
    """
    low = verbatim or ""
    if _F2P_TOKEN_RE.search(low) or _prose_leaks_test_identity(low):
        return True
    for s in symbols or set():
        sl = str(s)
        if _prose_leaks_test_identity(sl) or _F2P_TOKEN_RE.search(sl):
            return True
        if sl.lower() in gold_path_tokens:
            return True
    return False


_EB_PATTERNS = (
    _re.compile(
        r"(?:^|\n)#{1,3}\s*Expected\s*(?:Behavior|Output|Result)s?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        _re.DOTALL | _re.IGNORECASE,
    ),
    _re.compile(
        r"(?:^|\n)\*\*Expected\s*(?:behavior|output|result)s?\*\*[:\s]*(.*?)(?=\n\*\*|\n#{1,3}|\Z)",
        _re.DOTALL | _re.IGNORECASE,
    ),
)


def _expected_behavior_spec(issue_text: str):
    """The issue's Expected-Behavior spec line, leak-screened (B-1).

    Returns the one-line spec (<=200 chars), or None (correct-or-quiet) when absent
    or carrying a test name / FAIL_TO_PASS marker / assertion. Routes through the
    canonical `_obligation_is_leaky` screen so this surface obeys the SAME leak law
    as the obligations block."""
    if not issue_text:
        return None
    for _pat in _EB_PATTERNS:
        _m = _pat.search(issue_text)
        if _m:
            _txt = (_m.group(1) or "").strip()
            if _txt and len(_txt) > 10:
                _short = _txt[:200].strip()
                # ONE canonical screen: _obligation_is_leaky now covers the assertion
                # keyword AND every test-name convention (Brief-F1/F2), so this surface
                # obeys the IDENTICAL leak law as the <gt-obligations> block.
                if _short and not _obligation_is_leaky(_short, set(), set()):
                    return _short
            return None
    return None


# SS-5 (2026-07-13): the requirement-extractor discipline for the GT_SS_ACK_FORM checklist.
# A rendered obligation row is ``  - [<kind>] <verbatim>`` (kind tag) or ``  - <verbatim>``. The
# ``[<kind>]`` classifier is one of the extractor's fixed grammar classes (error/signature/behavior/
# compat/repro); ``repro`` is a REPRODUCTION FRAGMENT ("when I run X I get Y"), not an imperative
# requirement, so it is dropped. A direct conversational ask or a trailing ``?`` marks a
# non-requirement sentence (a request/question) — also dropped. First-person product proposals
# remain requirements: politeness/epistemic hedging does not erase requested behavior. Everything else is a requirement/
# directive the extractor already validated as requirement grammar, and is KEPT.
_OBLIGATION_ROW_KIND_RE = _re.compile(r"^\[(?P<kind>[a-z][a-z_]*)\]\s+(?P<text>.*)$")
_REPRO_KINDS = frozenset({"repro"})
_OBLIGATION_PLEA_RE = _re.compile(
    r"^(?:please\b|could\s+you\b|can\s+(?:you|we|someone|somebody)\b"
    r"|would\s+you\b|any\s+chance\b|is\s+there\s+(?:a|any|some)\b"
    r"|thank(?:s|\s+you)\b)",
    _re.IGNORECASE,
)


def _parse_obligation_row(row: str) -> tuple[str, str]:
    """Split a rendered obligation row into ``(kind, text)``. ``kind`` is ``""`` when the row
    carries no ``[<kind>]`` classifier prefix. Pure; the inverse of the ``  - {tag}{compact}``
    render at the two build loops."""
    s = row.lstrip()
    if s.startswith("- "):
        s = s[2:]
    m = _OBLIGATION_ROW_KIND_RE.match(s)
    if m:
        return m.group("kind"), (m.group("text") or "").strip()
    return "", s.strip()


def _obligation_is_imperative(kind: str, text: str) -> bool:
    """SS-5 requirement-extractor discipline: True iff the obligation row is an IMPERATIVE
    requirement item (keep), False for a repro fragment or direct ask/question (drop). Pure +
    deterministic — SELECTS requirement sentences, never paraphrases (LLM-free; a rewrite could
    change meaning). Contract mirrors what an SS-2 runtime extractor would expose, so a future
    shared helper can substitute without changing callers."""
    t = (text or "").strip()
    if not t:
        return False
    if (kind or "").strip().lower() in _REPRO_KINDS:
        return False
    if t.rstrip().endswith("?"):
        return False
    if _OBLIGATION_PLEA_RE.match(t):
        return False
    return True


def _obligation_checklist_row(row: str, strip_kind: bool = False) -> str:
    """ITEM-5: transform a rendered ``  - {tag}{compact}`` obligation row into a plain checklist
    item ``- [ ] {tag}{compact}``. Only the bullet changes; the obligation CONTENT (kind tag +
    verbatim text) is preserved verbatim.

    SS-5 ``strip_kind`` (GT_SS_ACK_FORM): drop the leading ``[<kind>]`` classifier so the item is a
    plain requirement line ``- [ ] <verbatim>`` (a native requirements checklist, no GT-metadata
    tag). Default ``strip_kind=False`` is byte-identical to the ITEM-5 GT_BRIEF_NATIVE arm."""
    s = row.lstrip()
    if s.startswith("- "):
        s = s[2:]
    if strip_kind:
        m = _OBLIGATION_ROW_KIND_RE.match(s)
        if m:
            s = m.group("text")
    return "- [ ] " + s


def _obligations_section(rendered: list[str]) -> list[str]:
    """Wrap the rendered obligation rows in the delivered FRAME.

    Default (tag) arm: the ``<gt-obligations>`` block — byte-identical. GT_BRIEF_NATIVE arm: a plain
    requirements checklist (``- [ ] <obligation>``) under a one-line plain header, NO ``<gt-*>`` tag,
    so the obligations ride the model's native requirements-list channel in-distribution. Leak-safe:
    every row already passed ``_obligation_is_leaky`` upstream; the native form only swaps the frame
    + bullet (no test identity can enter here).

    SS-5 GT_SS_ACK_FORM (layered ON TOP of GT_BRIEF_NATIVE): additionally apply the requirement-
    extractor discipline — keep only IMPERATIVE requirement items (drop repro fragments / pleas via
    ``_obligation_is_imperative``) and strip the ``[<kind>]`` classifier. GT_BRIEF_NATIVE-alone (ACK
    OFF) is byte-identical to today's plain-checklist arm. Correct-or-quiet: when no imperative
    requirement survives, the block is dropped entirely (``[]``) rather than shipping an empty
    header."""
    if _brief_native_on():
        if _ss_ack_form_on():
            kept = [r for r in rendered if _obligation_is_imperative(*_parse_obligation_row(r))]
            if not kept:
                return []
            return ["", _OBLIGATION_NATIVE_HEADER] + [
                _obligation_checklist_row(r, strip_kind=True) for r in kept
            ]
        return ["", _OBLIGATION_NATIVE_HEADER] + [_obligation_checklist_row(r) for r in rendered]
    return ["", "<gt-obligations>"] + rendered + ["</gt-obligations>"]


def _render_obligations_block(
    issue_text: str,
    files: list[FileEntry],
    cap,
    anchor_symbols: set[str] | None = None,
    require_anchor: bool = True,
) -> list[str]:
    """Render the ``<gt-obligations>`` behavioral-spec block, or ``[]`` when quiet.

    ``cap`` is the body-line clip closure from ``render_brief``. Returns a list of
    rendered lines (block tags included) or an empty list (correct-or-quiet).

    ``require_anchor`` (default True — byte-identical for every existing caller)
    gates the block on FOCUS/anchor overlap so the spec is never coupled to a
    WRONG located file. On the B-2 no-match path (``generate_v1r_brief`` has NO
    ranked existing file) there IS no located file to be wrong about, so the
    caller passes ``require_anchor=False``: the leak screen (``_obligation_is_leaky``)
    still runs, but the relevance gate is bypassed so the issue's own behavioral
    obligations still reach the agent at step 0.

    ``anchor_symbols`` are the issue's own curated code identifiers
    (``IssueAnchors.symbols`` ∪ ``code_symbols`` ∪ ``unresolved_code_symbols`` —
    BugLocator ICSE 2012 issue-subject tokens). They are unioned with the focus
    function tokens to form the relevance anchor. This is the SUBJECT of the issue
    (``range`` / ``coredump`` / ``bytes``), which an obligation legitimately
    describes even when that symbol is (a) a net-new feature not yet present in any
    indexed function (feature-add tasks — e.g. wasmi ``coredump``), or (b) a
    low-ref-count function truncated out of the top-N focus list (e.g. pest
    ``range`` at rank #13, beyond MAX_FUNCTIONS_PER_FILE). Focus tokens alone miss
    both cases → the whole obligation block was silently dropped on those tasks.
    """
    if not issue_text:
        return []
    try:
        from groundtruth.pretask.spec import extract_spec as _extract_spec
        from groundtruth.config.evidence_markers import (
            identifier_tokens as _id_tokens,
            passes_relevance_gate as _rel_gate,
        )

        # Row-11 fix (gt_math_oh 2026-06-25): read the ALREADY-PERSISTED structured
        # obligations from /tmp/gt_issue_anchors.json (written by generate_v1r_brief
        # at :3342 via spec.to_serializable()). The DeepSWE path reads these via
        # load_obligations; the OH path re-extracted thin — producing weaker output.
        # Bridge: try the persisted file first; fall back to live extraction.
        spec = None
        try:
            import json as _obl_json

            with open(_anchors_path(), encoding="utf-8") as _obl_f:
                _obl_data = _obl_json.load(_obl_f)
            _persisted = _obl_data.get("obligations") or []
            if _obligations_v2_on():
                # V2 artifacts are task-bound.  A shared /tmp fallback can
                # outlive its producer; version alone cannot prevent a valid
                # artifact from another issue being laundered into this brief.
                import hashlib as _obl_hashlib

                _issue_sha = _obl_hashlib.sha256(issue_text.encode("utf-8")).hexdigest()
                if (
                    _obl_data.get("obligations_version") != 2
                    or _obl_data.get("issue_sha256") != _issue_sha
                ):
                    _persisted = []
            if _persisted:
                from groundtruth.pretask.spec import Obligation, IssueSpec

                if _obligations_v2_on():
                    # v2 bridge: carry ALL fields (the v1 two-field reconstruction
                    # silently dropped symbols/keywords — weakening the symbol leg
                    # of the leak check). Flag-gated so flag-off bytes are stable.
                    _obls = [
                        Obligation(
                            verbatim_text=o.get("verbatim_text", ""),
                            kind=o.get("kind", "behavior"),
                            symbols=frozenset(o.get("symbols") or ()),
                            keywords=frozenset(o.get("keywords") or ()),
                            checkable_forms=frozenset(o.get("checkable_forms") or ()),
                            clause_id=o.get("clause_id", ""),
                            modality=o.get("modality", ""),
                            modality_strength=int(o.get("modality_strength") or 0),
                            subject_symbols=frozenset(o.get("subject_symbols") or ()),
                            parent_id=o.get("parent_id", ""),
                            part_index=int(o.get("part_index") or 0),
                            region=o.get("region", "normative"),
                        )
                        for o in _persisted
                        if isinstance(o, dict) and o.get("verbatim_text")
                    ]
                else:
                    _obls = [
                        Obligation(
                            verbatim_text=o.get("verbatim_text", ""),
                            kind=o.get("kind", "behavior"),
                        )
                        for o in _persisted
                        if isinstance(o, dict) and o.get("verbatim_text")
                    ]
                if _obls:
                    spec = IssueSpec(obligations=_obls)
        except Exception:
            pass
        if spec is None:
            if _obligations_v2_on():
                from groundtruth.pretask.spec import extract_spec_v2 as _extract_spec_v2

                spec = _extract_spec_v2(issue_text)
            else:
                spec = _extract_spec(issue_text)
    except Exception:
        return []
    if not spec.obligations:
        return []

    # RELEVANCE ANCHOR — the union of (1) the rendered edit-target functions'
    # identifier tokens and (2) the issue's own curated anchor symbols. An
    # obligation is rendered ONLY when it overlaps THIS anchor, NOT merely the
    # issue text. Raw issue-terms never discriminate (obligations ARE drawn from
    # the issue, so they would always "overlap"); keying on the focus + the
    # curated code-symbols is what makes the gate bite — an obligation about an
    # UNRELATED subsystem (no token overlap) stays quiet. When neither focus
    # functions NOR anchor symbols exist, we cannot anchor → stay quiet for the
    # whole block (correct-or-quiet; never launder the entire issue spec).
    fn_tokens: set[str] = set()
    gold_path_tokens: set[str] = set()
    for f in files:
        # bare names (function_names) are the focus anchor; `functions` holds
        # signatures ("def foo(...)") whose tokens we also harvest as a fallback.
        for fn in list(getattr(f, "function_names", []) or []) + list(
            getattr(f, "functions", []) or []
        ):
            fn_tokens |= _id_tokens(fn)
        # gold-path tokens for the leakage guard — a rendered candidate's path
        # components (basename stem + dir segments) must never appear AS an
        # obligation symbol (that would couple the spec to the located file).
        for seg in _re.split(r"[/\\.]+", str(getattr(f, "path", ""))):
            if len(seg) >= 3:
                gold_path_tokens.add(seg.lower())
    # Issue-SUBJECT anchor: tokenize the curated issue code-symbols and union them
    # in. These are the localizer's own BugLocator-style anchors — the same
    # provenance the brief already trusts for file ranking — so trusting them as an
    # obligation relevance anchor is consistent, not a new heuristic. They are NOT
    # added to gold_path_tokens, so the leakage guard is unaffected (an anchor
    # symbol that coincides with a gold-path segment is still caught there).
    for s in anchor_symbols or set():
        fn_tokens |= _id_tokens(str(s))
    if not fn_tokens and require_anchor:
        return []  # no anchor to gate against — stay quiet

    if _obligations_v2_on():
        # ── GT_OBLIGATIONS_V2 T1 render ──────────────────────────────────────
        # leak screen FIRST (drop whole), then T2 artifact (FULL clean list —
        # relevance gate deliberately NOT applied to the artifact: every row is
        # issue-verbatim text the agent already possesses; the anti-launder rule
        # protects the brief's DOSE, not secrecy), then gate + order + dynamic K.
        rows: list[dict] = []
        for _di, o in enumerate(spec.to_serializable(version=2)):
            _verb = (o.get("verbatim_text") or "").strip() if isinstance(o, dict) else ""
            if not _verb:
                continue
            if _obligation_is_leaky(_verb, o.get("symbols", []), gold_path_tokens):
                continue
            o["_doc_index"] = _di
            rows.append(o)
        _write_obligations_v2_artifact(rows, gold_path_tokens, issue_text)
        # The internal artifact retains every structurally classified row for
        # audit/provenance. Only normative rows are requirements that may enter
        # the model observation; process/evidence rows are never completion
        # obligations, even when their text overlaps the localized symbols.
        deliverable = [o for o in rows if o.get("region", "normative") == "normative"]
        gated = (
            deliverable
            if not require_anchor
            else [
                o for o in deliverable if _rel_gate((o.get("verbatim_text") or ""), None, fn_tokens)
            ]
        )
        gated.sort(key=_v2_order_key)
        k = _dynamic_obligation_budget(len(deliverable))
        rendered_v2: list[str] = []
        seen_v2: set[str] = set()
        for o in gated:
            if len(rendered_v2) >= k:
                break
            full = " ".join((o.get("verbatim_text") or "").split())
            compact = full[:_OBLIGATION_LINE_CHARS].strip()
            if len(full) > _OBLIGATION_LINE_CHARS:
                compact += "…"
            key = compact.lower()
            if not compact or key in seen_v2:
                continue
            seen_v2.add(key)
            kind = o.get("kind", "")
            tag = f"[{kind}] " if kind else ""
            rendered_v2.append(cap(f"  - {tag}{compact}"))
        if not rendered_v2:
            return []
        return _obligations_section(rendered_v2)

    rendered: list[str] = []
    seen: set[str] = set()
    # to_serializable() yields plain dicts (verbatim_text / kind / symbols / …) —
    # the SAME shape already persisted to gt_issue_anchors.json, so this block and
    # the in-container consumers read an identical obligation view.
    for o in spec.to_serializable():
        if len(rendered) >= _OBLIGATION_BUDGET:
            break
        verbatim = (o.get("verbatim_text") or "").strip() if isinstance(o, dict) else ""
        if not verbatim:
            continue
        kind = o.get("kind", "") if isinstance(o, dict) else ""
        symbols = o.get("symbols", []) if isinstance(o, dict) else []
        # Fail-closed leakage guard FIRST (drop whole obligation).
        if _obligation_is_leaky(verbatim, symbols, gold_path_tokens):
            continue
        # Relevance gate: render only when the obligation overlaps the FOCUS anchor
        # (the rendered edit-target functions). No focus overlap → drop this
        # obligation. (issue_terms intentionally empty — focus is the discriminator.)
        # Bypassed on the B-2 no-match path (require_anchor=False): with no located
        # file, there is no wrong file to couple to — the leak screen above still runs.
        if require_anchor and not _rel_gate(verbatim, None, fn_tokens):
            continue
        # Collapse whitespace + cap so a long requirement sentence stays one line.
        compact = " ".join(verbatim.split())[:_OBLIGATION_LINE_CHARS].strip()
        key = compact.lower()
        if not compact or key in seen:
            continue
        seen.add(key)
        tag = f"[{kind}] " if kind else ""
        rendered.append(cap(f"  - {tag}{compact}"))

    if not rendered:
        return []
    return _obligations_section(rendered)


def render_brief(
    files: list[FileEntry],
    *,
    scores: list[float] | None = None,
    scope_files: list[str] | None = None,
    scope_confidence: str = "low",
    scope_chains: list | None = None,
    issue_text: str = "",
    graph_db: str = "",
    emit_confident_line: bool = True,
    body_line_cap: int = _MAX_BODY_LINE_CHARS,
    anchor_symbols: set[str] | None = None,
) -> str:
    if not files:
        return "<gt-task-brief>\n</gt-task-brief>"

    # D1: per-body-line char cap. The budget-enforcement loop in
    # generate_v1r_brief tightens this (not the file LIST) when the brief is over
    # the token rail — trimming DETAIL, never which files the agent is told to
    # consider (BRIEFING.md §3). Local closure so every body-line append below
    # honors the effective cap for this render.
    def _cap(line: str) -> str:
        return _clip_body_line(line, body_line_cap)

    # Confidence-gated framing: if top candidate clearly ahead, directive.
    # If scores are flat, exploratory. Based on score separation of #1 vs #2.
    high_confidence = False
    if scores and len(scores) >= 2 and scores[0] > 0:
        gap = (scores[0] - scores[1]) / scores[0]
        high_confidence = gap > 0.3  # top candidate 30%+ ahead of #2

    # Per-entry confidence tier — used as INTERNAL FILTER, never displayed.
    # Research basis:
    #   - Wang et al. arXiv 2601.07767 (2026): models verbalize confidence but
    #     don't act on it; decision-action gap is robust across models.
    #   - Anthropic "Writing Effective Tools" (2025): explicitly drop "low-level
    #     technical identifiers" from agent-facing payload.
    #   - Squeez arXiv 2604.04979 (2026): verbatim filtered content, no labels,
    #     wins on agent benchmarks.
    # Filter rule: drop [INFO] entries unless ALL entries are [INFO], in which
    # case emit a single honest fallback note (verbatim alternative content).
    tiers = [_entry_confidence_tier(f, issue_text) for f in files]
    all_info = all(t == "[INFO]" for t in tiers)

    lines = ["<gt-task-brief>"]

    if all_info:
        lines.append(
            "Note: GT could not anchor any candidate with graph evidence. "
            "Use grep or code-search on issue keywords to localize."
        )
        # Render only the top-1 lexical match so the agent has at least a
        # starting point. No tier prefix.
        files = files[:1]
        tiers = tiers[:1]
        info_dropped: list[FileEntry] = []  # nothing anchored -> do not front-load weak facts
    else:
        # Filter out [INFO] entries — research says filter hard upstream. Capture the
        # dropped [INFO] candidates FIRST (rank order preserved) so the proactive
        # top-N block below can front-load their deterministic cross-file facts.
        info_dropped = [f for f, t in zip(files, tiers) if t == "[INFO]"]
        files_filtered = [f for f, t in zip(files, tiers) if t != "[INFO]"]
        tiers_filtered = [t for t in tiers if t != "[INFO]"]
        files = files_filtered
        tiers = tiers_filtered

    for i, f in enumerate(files, 1):
        funcs = ", ".join(f.functions) if f.functions else ""
        # No tier prefix on the agent-facing line. Tier was used as filter.
        line = f"{i}. {f.path}"
        if funcs:
            line += f" ({funcs})"
        # D1: cap the file-list line so a long (multi-signature) function list
        # cannot dominate the budget. Signatures are already docstring-stripped
        # (D2 sanitize_signature); this guards the concatenation length.
        lines.append(_cap(line))
        # WITNESS first (primacy): the structural REASON this file is here — the
        # graph edge from an issue-anchored symbol (graph_localizer). This is the
        # localization fact the agent's grep loop cannot cheaply reconstruct
        # (e.g. "set_fields calls set_parse [CALLS]"). Rendered only when present;
        # a name_match witness carries its own "(unverified)" tag from the localizer.
        if getattr(f, "witness", ""):
            lines.append(_cap(f"   Witness: {f.witness}"))
        # CONTRACT pillar first (primacy, Lost-in-the-Middle NeurIPS 2024): the
        # interface facts the agent must preserve — raises / guards / return shape.
        if f.contract_props:
            lines.append(_cap(f"   Contract: {f.contract_props}"))
        # Row-9 fix (gt_math_oh 2026-06-25): siblings/Context rendered LAST
        # (after Callers/Calls/Spec) and was budget-cut every time (600 tokens).
        # Move it up — consistency (sibling functions in the same class/module)
        # is the same importance tier as contract (what to preserve).
        if f.pattern:
            lines.append(_cap(f"   Context: {f.pattern}"))
        if f.spec and issue_text:
            # Relevance gate: spec must overlap with issue terms to avoid red herrings
            _spec_lower = f.spec.lower()
            _issue_lower = issue_text.lower() if issue_text else ""
            _issue_terms = set(_issue_lower.split()) - {
                "the",
                "a",
                "an",
                "is",
                "to",
                "in",
                "of",
                "and",
                "or",
                "for",
                "this",
                "that",
                "with",
                "from",
                "by",
                "on",
                "at",
                "it",
                "be",
                "as",
                "not",
                "but",
                "if",
                "we",
                "i",
            }
            _spec_overlap = any(term in _spec_lower for term in _issue_terms if len(term) > 3)
            _func_overlap = (
                any(fn.lower() in _spec_lower for fn in f.functions) if f.functions else False
            )
            if _spec_overlap or _func_overlap:
                lines.append(_cap(f"   Spec: {f.spec}"))
        elif f.spec and not issue_text:
            lines.append(_cap(f"   Spec: {f.spec}"))
        if f.contract:
            lines.append(_cap(f"   Callers: {f.contract}"))
        if f.co_changes:
            # SWAP-INVARIANT (run16 leak): drop test files from the co-change list — "Also changes:
            # …/test_plots_matplotlib.py" surfaces a test reference. Non-test co-changes are kept.
            _cc = [c for c in f.co_changes if not _is_test_path(c)]
            if _cc:
                lines.append(_cap(f"   Also changes: {', '.join(_cc)}"))
        if f.callees:
            lines.append(_cap(f"   Calls: {', '.join(f.callees)}"))
    # PROACTIVE top-N (GT_PROACTIVE_TOPN, default 1 = OFF). The rich render above
    # covers only the non-[INFO] candidates (usually #1); lower-ranked candidates are
    # [INFO]-dropped as weakly issue-relevant (anti-flood, 1937-1958). But their
    # CONTRACT + verified CALLERS are DETERMINISTIC graph facts, and the agent
    # re-derives them by OPENING those files (measured re-search, 91% of tasks). When
    # enabled, front-load a COMPACT contract/callers block for the next-ranked [INFO]
    # candidates up to the cap — the cross-file facts, without the round-trip.
    # Leakage-safe (no test names — contract/callers only). Bounded per AGENTS.md
    # 2602.11988 (a broad early dump hurts strong agents). Env-gated so the A/B
    # toggles it on ONE baked image; default 1 leaves every existing brief byte-identical.
    try:
        _proactive_topn = int(os.environ.get("GT_PROACTIVE_TOPN", "1") or "1")
    except ValueError:
        _proactive_topn = 1
    if _proactive_topn > 1 and info_dropped:
        _need = _proactive_topn - len(files)  # files == rendered non-[INFO] count here
        if _need > 0:
            _pro_lines: list[str] = []
            for f in info_dropped[:_need]:
                _facts = []
                if f.contract_props:
                    _facts.append(_cap(f"   Contract: {f.contract_props}"))
                if f.contract:
                    _facts.append(_cap(f"   Callers: {f.contract}"))
                if not _facts:
                    continue  # correct-or-quiet: no facts -> no line
                _hdr = f.path + (f" ({', '.join(f.functions)})" if f.functions else "")
                _pro_lines.append(_cap(_hdr))
                _pro_lines.extend(_facts)
            if _pro_lines:
                lines.append(
                    "Other candidates — cross-file facts (so you need not open each first):"
                )
                lines.extend(_pro_lines)
    # EXPECTED BEHAVIOR from issue text — the reporter's own spec for what the code
    # SHOULD do. Extracted from markdown sections like "### Expected Behavior",
    # "Expected:", "Should:", "The fix should". Leakage-safe (it's the issue text
    # the agent already has, curated into a concise spec).
    _eb_spec = _expected_behavior_spec(issue_text)
    if _eb_spec:
        lines.append("")
        lines.append(f"Expected behavior: {_eb_spec}")

    # INTENDED-BEHAVIOR SPEC (research-backed lever): surface the ASSERTION BODIES
    # from tests that target ALL rendered files' functions. The assertion tells
    # the agent WHAT the fix must produce — "assert kern.width == 1.5 * 16" is the
    # behavioral contract the fix must satisfy. GT has this in the assertions table
    # but previously shipped only test NAMES. Research: GenProg/APR (tests as
    # specification, ICSE 2009/TSE 2012), SWE-Tester arXiv 2601.13713 (+10%).
    # Leakage-safe: these are REPO-VISIBLE tests, not the harness's hidden tests.
    #
    # FIX (2026-06-01): previously queried assertions ONLY for files[0] (top-ranked).
    # When the brief mislocates (84% of the time), the agent sees test assertions
    # for the WRONG file. Now queries ALL rendered files so the correct file's
    # assertions are always present. Language-agnostic; generalized.
    # DISABLED (swap-invariant / gt_trial §6 leakage — caught live in run15): this block queried the
    # OFF-LIMITS `assertions` table and surfaced grader TEST NAMES + assertion bodies into the brief
    # ("VERIFY (tests targeting hdiplot.py): test_plot_hdi: assert ax"). "repo-visible tests are
    # leakage-safe" is FALSE — it lets the agent benchmaxx off the grader's test names (the run12
    # finding). GT must surface ZERO test references so its output is identical if the grader swaps.
    if False and graph_db and files:
        try:
            import sqlite3 as _asq

            _aconn = _asq.connect(graph_db)
            _all_spec_lines: list[str] = []
            _total_verify_budget = 5  # total assertion lines across all files
            for _verify_file in files:
                if len(_all_spec_lines) >= _total_verify_budget:
                    break
                _vf_path = _verify_file.path if hasattr(_verify_file, "path") else str(_verify_file)
                _vf_base = os.path.basename(_vf_path)
                _per_file_limit = max(2, _total_verify_budget - len(_all_spec_lines))
                # Two queries: first try linked assertions (target_node_id > 0),
                # then fall back to test-file-to-source-file edge join when
                # target_node_id is 0 (which is ~100% of real repos).
                _assertions = _aconn.execute(
                    """SELECT a.expression, a.expected, tn.name as test_name, tn.file_path as test_file
                    FROM assertions a
                    JOIN nodes tn ON a.test_node_id = tn.id
                    JOIN nodes tgt ON a.target_node_id = tgt.id
                    WHERE tgt.file_path LIKE ? AND a.target_node_id > 0
                    AND a.expression IS NOT NULL AND a.expression != ''
                    ORDER BY length(a.expression) ASC LIMIT ?""",
                    (f"%{_vf_base}", _per_file_limit),
                ).fetchall()
                # Fallback: find tests that CALL functions in this file
                if not _assertions:
                    _assertions = _aconn.execute(
                        """SELECT DISTINCT a.expression, a.expected, tn.name as test_name, tn.file_path as test_file
                        FROM assertions a
                        JOIN nodes tn ON a.test_node_id = tn.id
                        JOIN edges e ON e.source_id = a.test_node_id AND e.type = 'CALLS'
                        JOIN nodes callee ON e.target_id = callee.id
                        WHERE callee.file_path LIKE ?
                        AND a.expression IS NOT NULL AND a.expression != ''
                        ORDER BY length(a.expression) ASC LIMIT ?""",
                        (f"%{_vf_base}", _per_file_limit),
                    ).fetchall()
                if _assertions:
                    _all_spec_lines.append(f"VERIFY (tests targeting {_vf_base}):")
                    for expr, expected, tname, tfile in _assertions:
                        # Collapse whitespace so multi-line assertions render on one line
                        _expr_clean = " ".join((expr or "").split())[:80].strip()
                        if _expr_clean:
                            _tname_short = tname or "?"
                            _line = f"  {_tname_short}: {_expr_clean}"
                            if expected and expected.strip():
                                _line += f" == {expected.strip()[:50]}"
                            _all_spec_lines.append(_line)
                            if len(_all_spec_lines) >= _total_verify_budget + len(files):
                                break
            _aconn.close()
            if _all_spec_lines:
                lines.append("")
                lines.extend(_all_spec_lines)
        except Exception:
            pass

    # EDIT-TARGET CONTRACTS (Task #48, P1 LEVER): the signatures of the methods
    # the top-ranked file's edit-target functions CALL. The deciding "call it with
    # these args" fact — e.g. set_fields -> set_parse(self, key, string: str) — that
    # the agent otherwise burns turns grepping db.py to find. Verified callee edges
    # only (correct-or-quiet: a name_match call target is never claimed). Emitted
    # ONLY when at least one verified callee signature exists; omitted entirely
    # otherwise. Generalized — any file / language.
    if graph_db and files:
        _etc_lines = _edit_target_contracts_block(graph_db, files[0])
        if _etc_lines:
            lines.append("")
            # D1: cap each EDIT-TARGET CONTRACTS callee line — a long Go/Rust typed
            # header (no docstring, just many params) can run to ~250 chars.
            lines.extend(_cap(_l) for _l in _etc_lines)

    # BEHAVIORAL OBLIGATIONS (the contract/obligation pillar, after the contract
    # section + before the scope chain). spec.extract_spec mines the issue's own
    # requirement sentences (modals / behavior verbs / API-shape qualifiers) — they
    # are already persisted to gt_issue_anchors.json but were never RENDERED, so the
    # agent never saw the parsed behavioral spec (run-grounded: the rust pest task's
    # coalescing-semantics requirement). Gated on focus-anchor overlap (correct-or-
    # quiet) + a fail-closed leakage guard (no test-name / FAIL_TO_PASS / gold-path
    # token). Generalized — pure requirement grammar, any repo/language.
    _oblig_lines = _render_obligations_block(issue_text, files, _cap, anchor_symbols)
    if _oblig_lines:
        lines.extend(_oblig_lines)

    # Cross-file scope hint (Signal 1)
    # 2026-06-10 fact-filter (DELIVERY only — scope computation untouched):
    # vendored/minified/generated files (extern/jquery.dataTables.js, PATH B
    # audit) are never rendered as "Related files to inspect".
    if scope_files and scope_confidence in ("high", "medium"):
        # also drop test files — the agent is told not to edit tests, so a test path
        # rendered as "Related files to inspect" is noise (BUG-A surfaced-path leak).
        _deliverable_scope = [
            f
            for f in scope_files
            if not _is_vendored_path(f) and not _is_test_path(f) and not _is_test_or_demo(f)
        ]
        scope_names = [os.path.basename(f) for f in _deliverable_scope[:3]]
        if scope_names and scope_confidence == "high":
            lines.append(f"\nLikely multi-file scope: {', '.join(scope_names)}")
        elif scope_names:
            lines.append(f"\nRelated files to inspect: {', '.join(scope_names)}")

    # Graph-derived scope chains (Signal 2): connected file subgraphs from the
    # call graph showing which files need to change TOGETHER. Addresses the 32%
    # INCOMPLETE_SCOPE failure mode where the agent edits 1 file but the fix
    # needs 2-8 connected files.
    if scope_chains:
        for chain in scope_chains[:2]:
            chain_files = getattr(chain, "files", [])
            chain_desc = getattr(chain, "description", "")
            chain_conf = getattr(chain, "confidence", 0.0)
            # drop test files from the displayed chain — a test in "check ALL" is
            # noise (the agent must not edit tests); emit only if >=2 source files remain.
            # Honor the same deliverable-path filter as Signal 1 + the localization
            # entries (line ~3534): a vendored/minified lib (qunit.js, jquery.dataTables.js)
            # or a demo/example path in the "check ALL" chain is the worst noise — it tells
            # the agent to inspect a third-party file. Drop vendored + demo + test alike.
            _chain_src = [
                f
                for f in chain_files
                if not _is_test_path(f) and not _is_vendored_path(f) and not _is_test_or_demo(f)
            ]
            if len(_chain_src) >= 2 and chain_conf >= 0.5:
                chain_basenames = [os.path.basename(f) for f in _chain_src]
                # D1: cap the basename chain (many "→"-joined files run long). The
                # leading "\n" is preserved so the blank-line separator survives.
                lines.append(
                    "\n"
                    + _cap(
                        f"Scope chain (graph-connected, check ALL): {' → '.join(chain_basenames)}"
                    )
                )
                if chain_desc:
                    # Scrub desc segments that reference a dropped (vendored/test/demo)
                    # file. The desc describes edges over ALL chain files, so a vendored
                    # node (qunit.js, jquery.js) leaks into "Chain:" even when the
                    # basenames above are clean (the witnessed js-Consistency leak:
                    # `qunit.js -> qunit.js (process -> process)`). Keep only segments
                    # free of every dropped basename; if none survive, emit no Chain line.
                    _dropped_bn = {os.path.basename(f) for f in chain_files} - {
                        os.path.basename(f) for f in _chain_src
                    }
                    if _dropped_bn:
                        _segs = [
                            s
                            for s in chain_desc.split(";")
                            if s.strip() and not any(bn in s for bn in _dropped_bn)
                        ]
                        chain_desc = "; ".join(s.strip() for s in _segs)
                    if chain_desc:
                        lines.append(_cap(f"   Chain: {chain_desc}"))

    # Directive ending: gated on both score gap AND top tier being [VERIFIED].
    # Internal gating only — no tier displayed in directive line.
    if not files:
        lines.append("</gt-task-brief>")
        return _with_graph_map("\n".join(lines), files, graph_db, body_line_cap)
    top = files[0]
    # Task #45 (P0 HARM): naming a SINGLE highest-confidence candidate is only safe
    # when the rank is NOT a pure name_match/lexical guess. On beets ev1 the top
    # file (pipeline.py) was name_match-ranked and WRONG, yet this line confidently
    # named it. In addition to a clear score gap (high_confidence = gap>0.3) and a
    # [VERIFIED] tier, SUPPRESS the line when the graph proves the top file's
    # connectivity rests ENTIRELY on name_match edges (no verified backing). When we
    # cannot prove that weakness (no graph_db / no resolution_method column / file
    # has a verified edge / file is isolated), the line still fires — correct-or-
    # quiet on the suppression decision: suppress on PROVEN weakness, not on absence
    # of evidence. The file is still ranked #1 with its own evidence lines.
    _top_namematch_only = _file_is_namematch_only(graph_db, top.path)
    # GATE (rebuilt): the confident "highest-confidence candidate" line fires ONLY
    # when the top file carries a VERIFIED GRAPH-TRAVERSAL WITNESS — a deterministic
    # CALLS/IMPORTS edge from an issue-anchored symbol (graph_localizer). That
    # witness IS the confidence: it is a structural fact, so it does NOT also
    # require a lexical score gap (the witness, not keyword overlap, is what makes
    # importer.py the answer on beets-5495). When the top file has NO verified
    # witness, the line is SUPPRESSED — closing the exact harm where a 0.0-
    # confidence lexical guess (pipeline.py) was rendered as the confident answer.
    # Legacy path retained as a fallback for tasks where the localizer found no
    # anchor at all but the old [VERIFIED]-tier + score-gap signals still hold.
    _top_witnessed = bool(getattr(top, "witness_verified", False))
    # Legacy fallback (no localizer witness anywhere): fire ONLY when the top's
    # [VERIFIED] tier rests on a CALLER-CONTRACT fact ("func() in file:line") —
    # a real structural witness from _caller_contract_for_file — NOT on the weaker
    # "issue keyword matched a function name + some contract present" heuristic
    # that the beets-5495 lexical guess (pipeline.py) satisfied. Correct-or-quiet:
    # a confident directive requires a structural fact, never a keyword coincidence.
    _top_has_caller_fact = "() in " in (getattr(top, "contract", "") or "")
    _fire_confident = _top_witnessed or (
        high_confidence
        and not _top_namematch_only
        and tiers
        and tiers[0] == "[VERIFIED]"
        and _top_has_caller_fact
        and not any(getattr(f, "witness", "") for f in files)  # localizer silent
    )
    if _fire_confident and emit_confident_line:
        # De-prescribed (C2; SWE-PRM NeurIPS 2025: imperative mid-task guidance
        # lowers success, and on a mislocalized rank it actively misdirects — beets
        # was pushed to edit the WRONG file). State the highest-confidence candidate
        # as EVIDENCE; never command an edit ("Edit X first") or a test run.
        note = f"\nHighest-confidence candidate (graph + issue signals): {top.path}"
        if getattr(top, "witness", ""):
            note += f" — graph witness: {top.witness}"
        lines.append(note)
    elif emit_confident_line and not any(getattr(f, "witness_verified", False) for f in files):
        # No candidate carries a verified witness: honest fallback (correct-or-
        # quiet). Only emit when the localizer ran and found nothing AND no other
        # [VERIFIED] tier exists, so we don't over-warn on well-evidenced tasks.
        if all(t != "[VERIFIED]" for t in tiers):
            lines.append(
                "\nNote: GT could not anchor a candidate to the issue via a "
                "verified graph edge — use grep on issue keywords to confirm "
                "the edit target."
            )
    lines.append("</gt-task-brief>")
    return _with_graph_map("\n".join(lines), files, graph_db, body_line_cap)


def _common_region(paths: list[str]) -> str:
    """Shared directory region of the candidate files (dynamic granularity floor).

    When localization is broad (many files, no clear winner) GT shows the REGION the
    edit lives in instead of a wrong specific file — coarse-but-correct beats
    precise-but-wrong (correct-or-quiet expressed as granularity, not silence).
    """
    dirs = [os.path.dirname(p).replace("\\", "/") for p in paths if p]
    if not dirs:
        return ""
    split = [d.split("/") for d in dirs]
    common: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return "/".join(common)


def _defines_func_in_file(graph_db: str, file_path: str, func: str) -> bool:
    """True iff ``func`` is DEFINED (Function/Method/Class/ImplBlock) in ``file_path`` —
    the SAME exact-then-suffix node query ``_edit_target_guard`` uses.

    B2 (2026-07-05): the HIGH ``Edit target: <file> :: <func>`` head asserts ``func`` is
    the EDITABLE target IN that file, but ``func = w.anchor`` can be a CALLER symbol only
    REFERENCED there while defined elsewhere (the sh-744 caller-file/callee-symbol
    conflation) — a confident-WRONG steer, the single worst failure mode. Gate the HIGH
    head on a real def-site; a miss falls through to the MEDIUM candidate list (byte-
    identical to the weak-anchor downgrade). Unreadable graph -> True (permissive: never
    suppress HIGH on an I/O error — prior behavior)."""
    if not graph_db or not func or not file_path:
        return True
    try:
        conn = sqlite3.connect(graph_db)
        try:
            rel = _gl_normalize(file_path)
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE (file_path = ? OR file_path = ?) "
                "AND name = ? AND is_test = 0 "
                "AND label IN ('Function','Method','Class','ImplBlock') LIMIT 1",
                (file_path, rel, func),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT 1 FROM nodes WHERE file_path LIKE ? "
                    "AND name = ? AND is_test = 0 "
                    "AND label IN ('Function','Method','Class','ImplBlock') LIMIT 1",
                    ("%/" + rel, func),
                ).fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return True  # never suppress HIGH on a read failure (prior behavior)


def _edit_target_guard(graph_db: str, file_path: str, func: str) -> tuple[str, int | None]:
    """The exact guard/conditional/return line of the edit-target function, from the
    `properties` table (GT's stored content). This is the editable spec the agent
    acts on (GenProg/APR: the change site), delivered only at HIGH confidence."""
    if not graph_db or not func:
        return "", None
    try:
        conn = sqlite3.connect(graph_db)
        try:
            # BIND TO THE CANDIDATE'S OWN FILE — not just its basename. A "%basename"
            # LIKE matches a SAME-NAMED function in a DIFFERENT file (utils.py/models.py/
            # db.py collisions; "%db.py" even matches "gtdb.py"), so the HIGH-tier
            # "Edit target: <tgt.file_path> :: <func>" header could be followed by a
            # guard/return line that belongs to another file entirely — a confident-WRONG
            # fact (correct-or-quiet violation).
            #
            # BUG-2 (exact-vs-LIKE precedence): the prior single query OR'd the exact
            # match WITH a "%/"||rel suffix LIKE and took ORDER BY start_line LIMIT 1.
            # When the NAMED file's def has a larger start_line than a DIFFERENT file
            # that suffix-matches "%/"||rel (e.g. requested "db.py" matched "b/db.py"),
            # the wrong file's node sorted first and its guard rendered under the named
            # file's header. Fix: try the EXACT path first (stored form OR normalized
            # form); ONLY when no exact row exists fall back to the suffix LIKE. When the
            # named file genuinely has no such node, ABSTAIN ("" ) — never borrow another
            # file's line. is_test = 0 filters OUT only; ORDER BY start_line LIMIT 1 keeps
            # the chosen node deterministic within whichever arm matched.
            rel = _gl_normalize(file_path)
            row = conn.execute(
                "SELECT id FROM nodes "
                "WHERE (file_path = ? OR file_path = ?) "
                "AND name = ? AND is_test = 0 "
                "AND label IN ('Function', 'Method', 'Class', 'ImplBlock') "
                "ORDER BY start_line LIMIT 1",
                (file_path, rel, func),
            ).fetchone()
            if not row:
                # No EXACT match on the named file. Only now consider a suffix LIKE
                # (handles a stored path differing by a leading prefix). The "%/"||rel
                # boundary still blocks the gtdb.py/db.py basename-substring collision.
                row = conn.execute(
                    "SELECT id FROM nodes "
                    "WHERE file_path LIKE ? "
                    "AND name = ? AND is_test = 0 "
                    "AND label IN ('Function', 'Method', 'Class', 'ImplBlock') "
                    "ORDER BY start_line LIMIT 1",
                    ("%/" + rel, func),
                ).fetchone()
            if not row:
                return "", None
            nid = row[0]
            for kind in ("conditional_return", "guard_clause", "boundary_condition"):
                r = conn.execute(
                    "SELECT value, line FROM properties WHERE node_id = ? AND kind = ? "
                    "ORDER BY line LIMIT 1",
                    (nid, kind),
                ).fetchone()
                if r and r[0]:
                    txt = " ".join(str(r[0]).split())[:140]
                    return txt, (int(r[1]) if r[1] else None)
            return "", None
        finally:
            conn.close()
    except Exception:
        return "", None


def _hub_degree_fn(graph_db: str):
    """Return ``(p80, degree_of)`` for per-task hub detection.

    Uses the SAME file in-degree signal the brief's file-list demotion uses
    (``render_brief``: COUNT of CALLS/edges whose target lands in the file).
    ``degree_of(path)`` is the in-degree of that file; ``p80`` is the 80th
    percentile across all files = the hub threshold. On any failure (missing
    db, empty graph) returns ``(inf, ->0)`` so NO file is treated as a hub —
    the header keeps its prior behaviour on graphs we cannot measure.
    """
    import math

    try:
        conn = sqlite3.connect(graph_db)
        try:
            rows = conn.execute(
                "SELECT n.file_path, COUNT(e.id) FROM nodes n "
                "JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' GROUP BY n.file_path"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return math.inf, (lambda p: 0)
        degs = sorted(int(d) for _, d in rows)
        p80 = degs[int(len(degs) * 0.8)]
        by_path = {_gl_normalize(fp): int(d) for fp, d in rows}
        return p80, (lambda p: by_path.get(_gl_normalize(p), 0))
    except Exception:
        return math.inf, (lambda p: 0)


# FIX 4 (2026-06-11, gt_gt §16.5 issue D — the inverted-confidence pattern):
# the audited floor for the SYMBOL-level hub gate. Mechanism (recurring verbatim
# across runs 27307362054/27321848581): a func with very many callers (abs-stepped
# `functions.go::New`; csstree fixture.js) MANUFACTURES the >=2-distinct-witness
# convergence the HIGH gate requires — every caller of the hub is a "distinct
# structural witness" — so centrality, not evidence, stamps HIGH on a non-gold
# file. The floor (>20 callers, the audited magnitude) rails the per-task p80 on
# small/sparse graphs where the quantile collapses to 1-2 and would kill every
# legitimate HIGH; on dense graphs the p80 max-composes ABOVE the floor (dynamic
# pillar). n=2 calibration receipts — Stage 6 (gt_gt §15.4) owns refinement.
_HIGH_PIN_HUB_FANIN_FLOOR = 20


def _symbol_fanin_fn(graph_db: str):
    """Return ``(hub_thr, fanin_of)`` for SYMBOL-level hub detection — the
    symbol twin of ``_hub_degree_fn`` (which gates the candidate FILE; live
    beets-5495). The FILE gate passes when other files are similarly busy
    (the abs-stepped shape) — only the symbol fan-in exposes the hub.

    ``fanin_of(name)`` = COUNT of CALLS edges whose target node carries that
    symbol name (non-test); ``hub_thr`` = max(per-task p80 of that fan-in
    distribution, ``_HIGH_PIN_HUB_FANIN_FLOOR``). On any failure returns
    ``(inf, ->0)`` so NO symbol is treated as a hub — the gate's own failure
    is never a demotion fact (same permissive convention as
    ``_hub_degree_fn``)."""
    import math

    try:
        conn = sqlite3.connect(graph_db)
        try:
            rows = conn.execute(
                "SELECT n.name, COUNT(e.id) FROM nodes n "
                "JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' "
                "WHERE n.is_test = 0 GROUP BY n.name"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return math.inf, (lambda s: 0)
        degs = sorted(int(d) for _, d in rows)
        p80 = degs[min(len(degs) - 1, int(len(degs) * 0.8))]
        thr = max(p80, _HIGH_PIN_HUB_FANIN_FLOOR)
        by_name = {str(n).lower(): int(d) for n, d in rows}
        return thr, (lambda s: by_name.get((s or "").lower(), 0))
    except Exception:
        return math.inf, (lambda s: 0)


def _render_witness_line(w) -> str:
    """One-line render of a SINGLE witness, coherent with the edit target it
    justifies (mirrors ``Candidate.render_witness`` edge formatting). Used so the
    HIGH header's ``reason:`` describes the exact edge that chose ``func`` — not
    an arbitrary other witness on the same file."""
    try:
        if getattr(w, "hop", 0) >= 2:
            direction = getattr(w, "direction", "")
            far = w.src_symbol if direction == "calls_anchor" else w.dst_symbol
            return f"{w.anchor} -> ... -> {far} [{w.edge_type}, {w.hop}-hop]"
        # src_symbol is ALWAYS the caller, dst_symbol the callee (graph_localizer BFS).
        # `{src} called by {dst}` was INVERTED for called_by_anchor; render correctly.
        if getattr(w, "direction", "") == "calls_anchor":
            return f"{w.src_symbol} calls {w.dst_symbol} [{w.edge_type}]"
        return f"{w.dst_symbol} called by {w.src_symbol} [{w.edge_type}]"
    except Exception:
        return ""


def _high_func_support(witnesses, func: str) -> int:
    """Distinct STRUCTURAL issue witnesses (non-defines edges) converging on ``func``.

    D-3 calibration: the HIGH tier names ``func`` = the anchor of ONE max-strength issue
    edge. An issue ANCHOR is a symbol NAMED in the issue — often a REFERENCED symbol (the
    far end of a CALLS edge), not the function to edit (sh-744: HIGH said ``stdout``, gold
    was ``__await__``). A confident-WRONG function is the single worst failure (The
    Distracting Effect, arXiv:2505.06914, 2025 — plausible-but-wrong context drops accuracy
    6-11pp). So we calibrate at the FUNCTION level exactly as the file gate calibrates at
    the file level (KGCompass multi-hop-from-issue-ENTITIES, *plural*): the imperative HIGH
    steer fires only when >=2 distinct structural edges converge on ``func``. A lone-edge
    pick is weak -> caller downgrades to the MEDIUM candidate list (correct-or-quiet; the
    observed good outcomes came from MEDIUM, not HIGH). Distinctness over the full edge
    identity so two views of one edge don't double-count. Pure; no graph read.
    """
    fl = (func or "").lower()
    return len(
        {
            (
                getattr(w, "direction", ""),
                getattr(w, "src_symbol", ""),
                getattr(w, "dst_symbol", ""),
                getattr(w, "edge_type", ""),
            )
            for w in (witnesses or [])
            if (getattr(w, "anchor", "") or "").lower() == fl
            and getattr(w, "direction", "") != "defines_anchor"
        }
    )


def _resolved_witness_tail(graph_db: str, file_path: str) -> str:
    """Compact one-line RESOLVED call-edge witness for a localization-header
    candidate, or '' (correct-or-quiet).

    The ``<gt-localization>`` header is the FIRST block the agent reads (primacy),
    yet on the audited conan run its candidates carried NO call-edge witness at all —
    the resolution reached the agent only reactively via post_view at iters 8/10/49,
    too late to redirect the first move. This attaches the deterministic caller/callee
    FACT (already on disk) right next to the candidate so a resolved edge reaches the
    iter-0 brief that previously did not.

    Renders ``caller() in file:line`` (a caller proves the candidate's symbol is a
    REAL, USED target — the strongest confirmation) and falls back to ``-> callee()
    in file:line``. No source snippet (header stays compact; repo_root not needed).
    Deterministic-provenance + stdlib-shadow-guarded via ``_resolved_witnesses_for_file``;
    never surfaces a name_match. Pure read; no ranking effect.
    """
    if not graph_db or not file_path:
        return ""
    wits = _resolved_witnesses_for_file(graph_db, file_path, repo_root="", max_each=1)
    if not wits:
        return ""
    callers = [w for w in wits if w.get("direction") == "caller"]
    callees = [w for w in wits if w.get("direction") == "callee"]
    if callers:
        w = callers[0]
        sym = w.get("symbol") or "?"
        return f"resolved caller: {sym}() in {w.get('file_path')}:{w.get('line')}"
    if callees:
        w = callees[0]
        sym = w.get("symbol") or "?"
        return f"resolved call: -> {sym}() in {w.get('file_path')}:{w.get('line')}"
    return ""


def _fts5_symbol_rank(graph_db: str, file_path: str, terms: set[str]) -> list[str]:
    """Per-SYMBOL FTS5/BM25 rank WITHIN one file (the lexical half of the R1 leaf
    bridge). Returns symbol names in best→worst BM25 order; [] when nodes_fts is
    absent or no symbol matches (correct-or-quiet — no signal, no contribution).

    Mirrors graph_localizer._fts5_candidates' field-weighting (BLUiR ASE 2013:
    structured field-level lexical anchoring on names beats flat-blob BM25), but
    SCOPED to ``file_path`` and to its non-test Function/Method symbols so the
    rank discriminates WITHIN the file — the symbol-naming granularity, not the
    file-seeding granularity."""
    safe: list[str] = []
    for t in sorted({(s or "").lower() for s in terms}, key=lambda x: (-len(x), x)):
        c = t.replace('"', "")
        if len(c) >= 3 and all(ch.isalnum() or ch == "_" for ch in c):
            safe.append(f'"{c}"')
        if len(safe) >= 20:
            break
    if not safe:
        return []
    try:
        conn = sqlite3.connect(graph_db)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "nodes_fts" not in tables:
                return []  # no FTS5 capability -> lexical half is silent
            rows = conn.execute(
                """SELECT n.name,
                          bm25(nodes_fts, 1.0, 2.0, 0.5, 0.5) AS score
                     FROM nodes_fts
                     JOIN nodes n ON n.id = nodes_fts.rowid
                    WHERE nodes_fts MATCH ?
                      AND n.file_path = ?
                      AND n.is_test = 0
                      AND n.label IN ('Function', 'Method', 'Class', 'ImplBlock')
                    ORDER BY score
                    LIMIT 50""",
                (" OR ".join(safe), file_path),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for nm, _score in rows:
        nm = str(nm or "")
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def _semantic_leaf_names(
    loc, graph_db: str, file_path: str, issue_text: str, limit: int = 3
) -> list[str]:
    """R1 leaf-naming CONTENT bridge (correct-or-quiet, fires ONLY when the
    defines_anchor witness path named nothing). Ranks the file's WITHIN-file
    functions by the issue→code SEMANTIC signal (per-symbol MaxSim, captured in
    ``loc.symbol_semrank_by_file``) fused with the per-symbol FTS5/BM25 lexical
    rank, via Reciprocal Rank Fusion (Cormack SIGIR 2009), then DEMOTES symbol-
    level hubs (``_symbol_fanin_fn`` — the symbol twin of the file hub gate) so the
    central method is not named on a behavior-described issue. Returns [] when
    NEITHER signal is present (embedder off AND no FTS5 match) — the caller then
    degrades to the prior empty tail byte-identically. No task symbols, no weight
    tuning: pure rank fusion + a per-task hub threshold (generalized)."""
    _fn = _gl_normalize(file_path)
    sem_pairs = (getattr(loc, "symbol_semrank_by_file", None) or {}).get(_fn, [])
    sem_rank = {str(nm): i for i, (nm, _c) in enumerate(sem_pairs) if nm}

    terms = {w.lower() for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text or "") if len(w) > 3}
    lex_names = _fts5_symbol_rank(graph_db, file_path, terms) if terms else []
    lex_rank = {nm: i for i, nm in enumerate(lex_names)}

    if not sem_rank and not lex_rank:
        return []  # no content signal -> caller keeps prior behavior (empty tail)

    # Symbol-level hub demotion: a name in the per-task hub fan-in tail (>= thr)
    # is the CENTRAL method, not the issue's behavior site. Hubs sort AFTER non-hubs
    # (a stable secondary key) — never dropped, just not named first.
    _hub_thr, _fanin_of = _symbol_fanin_fn(graph_db)

    names = set(sem_rank) | set(lex_rank)
    _BIG = 10**6

    def _rrf(nm: str) -> float:
        s = 0.0
        if nm in sem_rank:
            s += 1.0 / (60 + sem_rank[nm])
        if nm in lex_rank:
            s += 1.0 / (60 + lex_rank[nm])
        return s

    def _is_hub(nm: str) -> int:
        try:
            return 1 if _fanin_of(nm) >= _hub_thr else 0
        except Exception:
            return 0

    # BUG-5 (relevance floor — correct-or-quiet on naming): a lone weak LEXICAL
    # signal (one symbol matched a single >=3-char issue token via FTS5, with NO
    # semantic corroboration) is the "best of a weak field", not a confident edit
    # target — naming it produces a confident-WRONG "edit this <func>" tail. A
    # bare FTS5 token presence is binary (matched / didn't), so a lexical-only name
    # MUST be corroborated by the semantic rank to be emitted. The semantic rank
    # (MaxSim) is a GRADED relevance score, so its TOP name (rank 0) is allowed to
    # stand alone. A name therefore qualifies iff: it appears in BOTH signals
    # (>=2 agreeing signals), OR it is the #1 semantic match. A lexical-only match
    # — at ANY rank, including the single-match rank-0 case — never qualifies alone.
    # When nothing qualifies, return [] so the caller emits the FILE with NO
    # function tail (RRF agreement, Cormack SIGIR 2009: cross-ranker concord is the
    # trustworthy signal). Generalized: no task symbols, no weight tuning.
    def _qualifies(nm: str) -> bool:
        in_sem = nm in sem_rank
        in_lex = nm in lex_rank
        if in_sem and in_lex:
            return True  # >=2 agreeing signals
        if in_sem and sem_rank[nm] == 0:
            return True  # graded-relevance top match may stand alone
        return False  # lexical-only (or non-top sem-only) -> correct-or-quiet

    qualified = [nm for nm in names if _qualifies(nm)]
    if not qualified:
        return []  # no name clears the relevance floor -> file with no func tail
    ranked = sorted(qualified, key=lambda nm: (_is_hub(nm), -_rrf(nm), nm))
    return ranked[:limit]


def _localization_header(loc, graph_db: str, issue_text: str) -> tuple[str, str]:
    """Confidence-graded localization block, PREPENDED to the brief.

    Returns ``(header_str, primary_path)``. ``primary_path`` is the file the header
    NAMED as #1 (the HIGH edit target, or the first shown candidate) — empty when no
    header fires. The caller (``generate_v1r_brief``) uses it to make the SAME file
    that ``<gt-localization>`` names #1 the ``entries[0]`` that the L1-SCOPE block,
    ``render_brief`` file list, graph-map, and EDIT-TARGET CONTRACTS all key off — so
    the two independently-ordered pipes can no longer name different #1 files (the L1
    cross-wire, confirmed live cfn-lint-3749). Single ordering source, consumed verbatim.

    Granularity scales with RESEARCH-BACKED structural confidence — a verified graph
    edge anchored on an ISSUE-named entity (KGCompass: multi-hop from issue entities),
    NOT raw lexical score (which is high for lexical-subsystem traps like an `overflow`
    validator). Never prescribes one edit imperatively; always leaves the pick to the
    agent (SWE-agent: the agent self-localizes; we augment, not command).

      HIGH   -> file :: function + the exact guard/conditional line to change
                (Agentless hierarchical file->func->edit; GenProg: editable spec).
      MEDIUM -> likely file + candidate function names (agent picks the function).
      LOW    -> region (common module) + top-3 file options to reason over
                (BugLocator/Agentless ranked candidates; agent confirms with grep).
    """
    if loc is None or not getattr(loc, "candidates", None):
        return "", ""
    anchors = {(a or "").lower() for a in (getattr(loc, "anchor_symbols", None) or [])}
    cands = loc.candidates

    # ---- CONSENSUS FACT-LEDGER FORM (GT_CONSENSUS_LEDGER, default off) ----
    # A FORM-only re-grammar of this header — invariant-3 safe: SAME gates, SAME
    # candidate set, SAME order, SAME returned `primary_path`; only the presentation
    # STRING changes. When the flag is set the consensus renders as a VERIFIABLE FACT
    # LEDGER — "N/3 signals agree (grep, structural, semantic)" plus the per-leg
    # receipts from `loc.signals_by_file` — and DROPS the imperative "Edit target:"
    # verdict phrasing, so the model REASONS over cross-ranker agreement (Cormack RRF
    # SIGIR 2009: concord across independent rankers is the trustworthy signal) rather
    # than being handed an override the RL policy discounts. Default OFF => every branch
    # below falls through to the EXACT prior strings (byte-identical, proven by test).
    _ledger = os.environ.get("GT_CONSENSUS_LEDGER", "") == "1"
    _signals_map = getattr(loc, "signals_by_file", None) or {}

    def _sig_receipt(fp: str) -> str:
        # Which of the 3 independent rankers voted this file into its own top-3 —
        # the RECEIPTS behind the agreement COUNT. Leg names only ({grep,structural,
        # semantic}), never a test name / path / assertion, so this adds ZERO leak
        # surface. Empty legs => the honest "0/3" (correct-or-quiet: no consensus).
        # Whitelist the leg names at the render boundary (Fable C2): the count is
        # len(legs), but only the known literals may ever be emitted — so a future
        # producer change to signals_by_file cannot leak an arbitrary token into the
        # agent-visible bytes. Makes "ZERO leak surface" structural, not assumed.
        # "content" is the lexical leg's label when the grep leg is absent (repo-less/
        # MCP path, GT_CONTENT_LEG) — mutually exclusive with "grep", so a file never
        # carries both and the denominator stays 3 ({grep|content}, structural, semantic).
        # It MUST be whitelisted (Fable #6): else the receipt drops it while the HIGH-
        # eligibility gate counts the unfiltered leg, so the header would claim an
        # agreement the receipt denies. Rendered from the SAME filtered basis the gate uses.
        legs = [
            l
            for l in _signals_map.get(_gl_normalize(fp), [])
            if l in ("grep", "content", "structural", "semantic")
        ]
        if legs:
            return f"{len(legs)}/3 signals agree ({', '.join(legs)})"
        return "0/3 signals agree"

    def _issue_edges(c):
        # verified, non-DEFINES (structural edge) witnesses descended from an issue anchor
        return [
            w
            for w in c.witnesses
            if getattr(w, "verified", False)
            and getattr(w, "direction", "") != "defines_anchor"
            and (getattr(w, "anchor", "") or "").lower() in anchors
        ]

    import statistics as _st

    top = cands[0]
    top_edges = _issue_edges(top)
    struct_cands = [c for c in cands if _issue_edges(c)]

    # ---- per-task, data-derived separation (NO absolute score thresholds) ----
    # All cutoffs below are relative to THIS task's score distribution (median gap,
    # MAD) — the QPP score-separation pattern the gate already uses — so nothing is
    # a hardcoded magic number; tiers/breadth scale with the actual data.
    scores = [float(getattr(c, "score", 0.0)) for c in cands]
    _med = _st.median(scores) if scores else 0.0
    _mad = _st.median([abs(s - _med) for s in scores]) if scores else 0.0
    _gaps = [scores[i] - scores[i + 1] for i in range(len(scores) - 1)]
    _med_gap = _st.median(_gaps) if _gaps else 0.0
    _top_gap = (scores[0] - scores[1]) if len(scores) > 1 else (scores[0] if scores else 0.0)
    # "dominant" = the top is separated from the pack by more than the typical
    # per-task gap AND more than one MAD (both per-task, both relative).
    _dominant = (_top_gap > _med_gap) and (_mad == 0.0 or _top_gap > _mad)

    # ---- DYNAMIC breadth K = the EVIDENCE-BACKED contention set: candidates that
    # carry a verified witness (structural evidence), not a raw score percentile. This
    # is hybrid (the set is defined by structural evidence, sized per-task) and it
    # keeps a grep-recovered, structurally-witnessed gold that sits just below the
    # score peak (e.g. weasyprint-2300 block.py at #4) inside the shown options —
    # which an above-median score cut dropped at the boundary. Falls back to the top
    # candidates when none are witnessed. [3..6] is a token-budget rail. ----
    # Test-tooling is NEVER an edit candidate: a vendored / imported-only-by-tests file
    # (testify/spew/...) shown in <gt-localization> misdirects the agent to edit vendored
    # code (witnessed: expr go offered internal/testify/assert as candidate #4). The
    # run_v74 focus-set already hard-filters these; the header candidate list did not.
    # test_tooling_roots is graph-derived (imported only by tests, transitive fixpoint) —
    # language-agnostic, no library names. Correct-or-quiet: keep the original set if the
    # filter would empty it. Same GT_TEST_TOOLING_DEMOTE gate as run_v74 (default ON).
    if os.environ.get("GT_TEST_TOOLING_DEMOTE", "1") != "0":
        _tt_roots = _test_tooling_roots(graph_db)
        if _tt_roots:
            _kept = [c for c in cands if not _is_test_tooling(c.file_path, _tt_roots)]
            if _kept:
                cands = _kept
    # VENDORED / DEMO demote (root cause C, 2026-06-17): a vendored / demo copy
    # (benchmark/libs/mashumaro/common.py, examples/**/qunit.js, third_party/…,
    # site-packages/…) shown in <gt-localization> misdirects the agent to edit
    # vendored code — the same harm the test-tooling filter above prevents, on the
    # path-class axis the graph-derived tooling roots miss. Reuses the single
    # canonical predicates (is_test_or_demo: benchmark/examples/vendor segments;
    # is_vendored_path: third_party/node_modules/site-packages dir markers) — no new
    # ad-hoc list. Correct-or-quiet: keep the original set if the filter would empty
    # it (a vendored-only candidate set still gets a region-level option list).
    _kept = [
        c for c in cands if not _is_test_or_demo(c.file_path) and not _is_vendored_path(c.file_path)
    ]
    if _kept:
        cands = _kept
    _evidenced = sum(1 for c in cands if getattr(c, "relevance_grade", "") == "VERIFIED") or 3
    K = min(max(3, _evidenced), 6, len(cands))
    shown = cands[:K]

    def _defines_funcs(c) -> list[str]:
        fs: list[str] = []
        for w in c.witnesses:
            a = getattr(w, "anchor", "")
            if getattr(w, "direction", "") == "defines_anchor" and a and a not in fs:
                fs.append(a)
        return fs

    # ---- MULTI-SIGNAL AGREEMENT (the grep-floor build) ----
    # The tier now means "how many of the 3 independent rankers (grep / semantic /
    # structural) agree this is the target" — NOT a structural-witness-only count.
    # `agreement_by_file` was computed in graph_localizer.localize() as the per-file
    # count of rankers placing the candidate in their OWN top-3 (0..3). We read the
    # TOP candidate's agreement (the file the header is about). Empty dict / missing
    # key -> 0 (no agreement evidence), which correctly degrades to LOW.
    # Research: cross-ranker agreement (RRF, Cormack SIGIR 2009; CombMIN, Fox & Shaw
    # TREC-2 1994) is a stronger relevance signal than any single ranker.
    _agree_map = getattr(loc, "agreement_by_file", None) or {}
    _top_agreement = int(_agree_map.get(_gl_normalize(top.file_path), 0))

    # ---- HIGH: >=2 of {grep, semantic, structural} agree AND an issue-anchored
    # verified, non-DEFINES edge holds. Agreement is the breadth signal; the
    # structural-edge precondition keeps HIGH rendering file :: function :: line.
    #
    # HUB GATE (live beets-5495 fix): cross-ranker agreement is manufactured by
    # CENTRALITY — a CLI hub (commands.py) lands in every ranker's top-3 because
    # it is connected to everything, not because it is the bug site, so it out-
    # agreed the gold importer.py and HIGH steered the agent to the wrong file.
    # HIGH must NOT fire its imperative steer on a hub. Among HIGH-eligible
    # candidates (issue-witnessed AND agreement>=2, in localizer rank order) we
    # render HIGH about the highest-ranked NON-hub (same per-task in-degree p80
    # the file-list demotion uses). If EVERY eligible candidate is a hub we render
    # NO HIGH and fall through to the option list — correct-or-quiet: a confident
    # wrong steer is worse than handing the agent the candidate set. ----
    _hub_p80, _degree_of = _hub_degree_fn(graph_db)

    def _distinct_issue_anchors(c) -> int:
        # how many DISTINCT issue entities structurally witness this target
        return len({(getattr(w, "anchor", "") or "").lower() for w in _issue_edges(c)})

    # HIGH-ANCHOR GUARD (abs-module-cache-flags fix): the imperative HIGH steer
    # ("Edit target: file :: func") must be backed by >=2 DISTINCT issue entities —
    # KGCompass's multi-hop-from-issue-ENTITIES (plural) signal, which the gate's own
    # docstring cites. A single structural CALLS edge to ONE tangential anchor is NOT
    # enough: e.g. `BeginRepl called by NewTerminal` cleared agreement>=2 via a weak
    # lexical "terminal" match + that lone structural edge, and HIGH then confidently
    # steered a require()/module-cache task at terminal.go — a confident-wrong steer,
    # the single worst failure mode (correct-or-quiet). Requiring multi-anchor support
    # demotes such single-edge picks to the MEDIUM candidate list (agent reasons over
    # them) WITHOUT losing real help: observed good outcomes came from the MEDIUM path,
    # not HIGH. Shared localizer -> fixes both the OH and DeepSWE pipelines at the source.
    _high_elig = [
        c
        for c in cands
        if _issue_edges(c)
        and int(_agree_map.get(_gl_normalize(c.file_path), 0)) >= 2
        and _distinct_issue_anchors(c) >= 2
    ]
    _high_pick = next((c for c in _high_elig if _degree_of(c.file_path) <= _hub_p80), None)
    if _high_pick is not None:
        tgt = _high_pick
        w = max(_issue_edges(tgt), key=lambda x: x.strength())
        func = w.anchor
        # D-3 calibration: keep the imperative HIGH steer ONLY when >=2 distinct
        # structural witnesses converge on the NAMED func (_high_func_support). A
        # lone-edge pick (sh-744: `stdout` via one "stdout called by wait" edge, gold
        # `__await__`) is a confident-WRONG function — the worst failure mode — so
        # downgrade to the MEDIUM candidate list instead. Correct-or-quiet; this is the
        # confidence-gate lever (BRIEFING.md §3/§4), NOT a reach/ranking change — same
        # files, same order; only the top file's tier label changes.
        # FIX 4 — SYMBOL-LEVEL HUB GATE (gt_gt §16.5 issue D, inverted-confidence):
        # the >=2-witness convergence below is MANUFACTURED when `func` itself is a
        # hub (abs-stepped `New`: every caller is a "distinct" witness). The file-
        # level hub gate above passes when other files are similarly busy, so the
        # named FUNC must also clear the per-task symbol fan-in threshold. A
        # hub-anchored pin demotes to the MEDIUM candidate list (correct-or-quiet:
        # a confident-wrong steer is the single worst failure mode). Unreadable
        # graph -> (inf, ->0) -> permissive (prior behavior).
        _sym_hub_thr, _fanin_of = _symbol_fanin_fn(graph_db)
        if (
            _defines_func_in_file(graph_db, tgt.file_path, func)
            and _high_func_support(tgt.witnesses, func) >= 2
            and _fanin_of(func) <= _sym_hub_thr
        ):
            line_txt, line_no = _edit_target_guard(graph_db, tgt.file_path, func)
            # FORM-only (GT_CONSENSUS_LEDGER): the imperative "Edit target:" verdict
            # becomes a fact-ledger head ("file :: func — N/3 signals agree (...)") and
            # the guard line reads as a receipt, not a directive. Flag off => the EXACT
            # prior two strings (byte-identical). SAME target, SAME `primary_path`.
            if _ledger:
                _high_head = f"{tgt.file_path} :: {func} — {_sig_receipt(tgt.file_path)}"
                _guard_label = "guard/return"
            else:
                _high_head = f"Edit target: {tgt.file_path} :: {func}"
                _guard_label = "guard/return to update"
            out = ['<gt-localization confidence="high">', _high_head]
            if line_txt:
                loc_s = f"  [L{line_no}]" if line_no else ""
                out.append(f"  {_guard_label}: {line_txt}{loc_s}")
            # reason MUST justify THIS edit target — render the witness that CHOSE
            # `func` (the max-strength issue edge), not an arbitrary other witness on
            # the file. (Avenue-2 fix: top.render_witness() previously picked an
            # unrelated edge, so "Edit import_files / reason: _parse_logfiles called
            # by _paths_from_logfile" disagreed with itself.)
            wr = _render_witness_line(w)
            if wr:
                out.append(f"  reason: {wr}")
            out.append("</gt-localization>")
            return "\n".join(out), tgt.file_path
        # weak function anchor (<2 converging structural witnesses) -> fall through to
        # the MEDIUM candidate list below (agent reasons over the file's functions).

    # ---- MEDIUM vs LOW is now driven by agreement too: >=1 signal agrees ->
    # MEDIUM (a named candidate set worth reasoning over); 0 signals agree -> LOW
    # (region-level / option list, agent confirms with grep). The region path
    # below is the LOW rendering; it only fires when agreement is absent. ----
    # BUG-4: the tier governs how the SHOWN set (cands[:K]) renders, but reading
    # only `_top_agreement` (cands[0]) stamps the whole set LOW whenever the #1
    # happens to be a lexical-only pick — even if a multi-ranker-agreed #2/#3 sits
    # in `shown`. Compute the tier from the agreement DISTRIBUTION over `shown`:
    # MEDIUM iff ANY shown candidate has >=1 ranker agreement (RRF/CombMIN — a
    # cross-ranker-agreed candidate anywhere in the contention set is real signal,
    # not noise). Empty/missing agreement -> 0 -> LOW (correct-or-quiet).
    _shown_max_agreement = max(
        (int(_agree_map.get(_gl_normalize(c.file_path), 0)) for c in shown),
        default=0,
    )
    _low_tier = _shown_max_agreement < 1

    # ---- LOW (region): no signal agreement AND the shown candidates share an
    # INFORMATIVE common region (a real sub-module, >=2 path components) — summarise
    # by region rather than naming a wrong file. The "many scattered files -> show the
    # region" path. If the only shared prefix is the repo root, region is
    # uninformative and we fall through to the flat option list instead. ----
    region = _common_region([c.file_path for c in shown])
    region_informative = region.count("/") >= 1  # >=2 path components
    if _low_tier and region_informative and len({os.path.dirname(c.file_path) for c in shown}) > 1:
        out = [
            '<gt-localization confidence="low">',
            f"Region: {region}/ — candidate edit targets (reason over these, confirm with grep):",
        ]
        for i, c in enumerate(shown, 1):
            # FORM-only (GT_CONSENSUS_LEDGER): append the per-file N/3 receipt. Flag
            # off => "" => byte-identical to today. SAME lines, SAME order.
            _rc = f"  — {_sig_receipt(c.file_path)}" if _ledger else ""
            out.append(f"  {i}. {c.file_path}{_rc}")
        out.append("</gt-localization>")
        return "\n".join(out), shown[0].file_path

    # ---- MEDIUM / LOW flat option set: a cluster with no HIGH winner -> flat option
    # set (dynamic K), each with its issue-relevant functions; the agent reasons +
    # picks. The confidence LABEL is agreement-driven: >=1 signal agrees -> "medium",
    # 0 signals agree -> "low" (this is the LOW rendering when the region above was
    # uninformative). Keeps the tier == "X signals agree" contract end-to-end. ----
    _tier_label = "low" if _low_tier else "medium"
    # B-9: the grep-confirmation hedge is MANDATORY on this header — it is the branch
    # nearly every real issue hits (all 4 measured live shapes render MEDIUM here), and
    # at 0.67 top-1 precision the agent must re-confirm the target, not treat it as fact
    # (ALREADY_BUILT.md; the LOW-region branch above already carries the same hedge).
    out = [
        f'<gt-localization confidence="{_tier_label}">',
        "Candidate edit targets (reason over these — confirm the edit target with grep):",
    ]
    for i, c in enumerate(shown, 1):
        fs = _defines_funcs(c)
        # R1 leaf-naming bridge: defines_anchor named NOTHING (behavior-described
        # issue — the gold leaf shares no token with a named anchor). Fall back to
        # the issue→code CONTENT signal (per-symbol MaxSim + per-symbol FTS5, RRF-
        # fused, symbol-hub-demoted) so the named leaf is the bug site, not the
        # in-degree hub. Never touches the working anchor path (only fires when fs is
        # empty); byte-identical empty tail when no content signal exists.
        if not fs:
            fs = _semantic_leaf_names(loc, graph_db, c.file_path, issue_text)
        tail = f" — {', '.join(fs[:3])}" if fs else ""
        # FORM-only (GT_CONSENSUS_LEDGER): lead the line with the per-file N/3 receipt,
        # then the candidate funcs. Flag off => "" => byte-identical to today.
        _rc = f" — {_sig_receipt(c.file_path)}" if _ledger else ""
        out.append(f"  {i}. {c.file_path}{_rc}{tail}")
        # Surface the RESOLVED call-edge fact (already on disk) next to the
        # candidate so a confirming edge reaches the iter-0 header — the audited
        # gap where the header's candidates carried no call-edge witness and the
        # resolution only reached the agent reactively (post_view, iters 8/10/49).
        # Deterministic + stdlib-shadow-guarded; correct-or-quiet (no fact -> no line).
    out.append("</gt-localization>")
    return "\n".join(out), shown[0].file_path


def _localization_header_for_entries(
    loc: LocalizerResult | None,
    graph_db: str,
    issue_text: str,
    entries: list[FileEntry],
) -> tuple[str, str, str]:
    """Render one localization order without letting a weak pipe override another.

    HIGH is a singular, structurally gated target and retains the historical
    localizer-primary contract.  MEDIUM/LOW are contention sets: order their shared
    candidates by the terminal evidence order already represented by ``entries``.
    The input objects are never mutated.
    """
    if loc is None or not loc.candidates or not entries:
        return "", "", ""
    terminal_rank = {_gl_normalize(entry.path): index for index, entry in enumerate(entries)}
    original_rank = {id(candidate): index for index, candidate in enumerate(loc.candidates)}
    shared = [
        candidate
        for candidate in loc.candidates
        if _gl_normalize(candidate.file_path) in terminal_rank
    ]
    if not shared:
        # No candidate has terminal evidence.  Under minimal mode the normal
        # file-entry renderer remains the honest orientation; do not retain an
        # unjoinable localizer-only contention block.
        return "", "", ""
    initial_header, _initial_primary = _localization_header(loc, graph_db, issue_text)
    initial_tier = _localization_confidence_tier(initial_header)
    ordered = list(shared)
    if initial_tier in {"medium", "low"}:
        ordered.sort(
            key=lambda candidate: (
                terminal_rank.get(_gl_normalize(candidate.file_path), 10**6),
                original_rank[id(candidate)],
            )
        )
    ordered_loc = replace(loc, candidates=ordered)
    header, primary = _localization_header(ordered_loc, graph_db, issue_text)
    return header, primary, _localization_confidence_tier(header)


# Language-invariant generic identifiers — code builtins + ubiquitous collection methods that are
# NEVER localization anchors even when an issue mentions them (the code equivalent of anchors.py's
# _NL_FUNCTION_WORDS English-function-word filter — a LANGUAGE invariant, NOT a per-task blocklist;
# no domain words). loguru-1297: 'print' (a builtin with a caller edge) corroborated _error_interceptor
# and flanked the gold — this drops it at the source so only specific names seed.
_GENERIC_CODE_NAMES: frozenset[str] = frozenset(
    {
        "print",
        "format",
        "sorted",
        "range",
        "input",
        "repr",
        "round",
        "bytes",
        "bytearray",
        "frozenset",
        "isinstance",
        "hasattr",
        "getattr",
        "setattr",
        "delattr",
        "super",
        "object",
        "property",
        "staticmethod",
        "classmethod",
        "append",
        "extend",
        "insert",
        "remove",
        "items",
        "keys",
        "values",
        "update",
        "split",
        "strip",
        "join",
        "replace",
        "encode",
        "decode",
    }
)


def _exact_issue_named_files(
    issue_text: str,
    graph_db: str,
    issue_anchors=None,
) -> dict[str, list[str]]:
    """{file: [symbols]} for Function/Method/Class/Interface names appearing VERBATIM in the
    issue (gt_gt §4 exact-name seeder). A symbol the issue literally names is the strongest
    localization signal that exists — its DEFINING file MUST be a guaranteed candidate, never
    composite-scored-and-cut.
    Language/repo-agnostic (graph name match), test-blind (is_test=0). LIPI: arviz issue names
    plot_hdi 4x + links hdiplot.py, yet run_v74 never anchored it so the gold was absent from
    ranked_full. SPECIFICITY (proven needed by held-out loguru-1297): an issue-named symbol is a
    localization signal ONLY if it is SPECIFIC — generic names (`__init__`, `print`) appear in the
    issue AND in many files, flooding the guarantee and burying the real gold. So: skip dunders,
    skip short generic names, and skip any name that resolves to MORE than a few files.

    UPDATED (fix 2026-06-10 — §4 candidate-union recall defect). Two generalized gaps closed:
      * CLASS-like definitions count. The label filter was Function/Method only, so an issue
        whose title names the defective CLASS verbatim (defined in <= _MAX_FILES_PER_NAME
        files — a one-line grep for any agent) never earned the guarantee and the gold could
        be absent from every rendered slot. Class/Interface is the same class-like set
        graph_localizer._seed_node_rows already seeds on — ONE definition of "definition"
        across the pipeline.
      * PROVENANCE beats string shape for short names. The `len<5 and no _` skip is a SHAPE
        heuristic against prose collisions; a short name the reporter put in the TITLE or in
        BACKTICKS (IssueAnchors.title_symbols / code_symbols — BugLocator ICSE 2012 summary
        weighting; arXiv:2512.07022 code-mention provenance) is reporter-confirmed, not a
        collision — it bypasses the shape skip ONLY (still subject to the dunder / generic /
        ambiguity gates: confidence-gated, never a free pass).
    """
    import sqlite3 as _sq

    if issue_anchors is None:
        try:
            from groundtruth.pretask.anchors import extract_issue_anchors

            issue_anchors = extract_issue_anchors(issue_text, graph_db)
        except Exception:
            issue_anchors = None
    trusted_symbols = (
        set(getattr(issue_anchors, "symbols", None) or set())
        | set(getattr(issue_anchors, "title_symbols", None) or set())
        | set(getattr(issue_anchors, "code_symbols", None) or set())
    )
    toks = set(trusted_symbols)
    for token in tuple(trusted_symbols):
        toks.update(part for part in token.replace("::", ".").split(".") if part)
    toks |= {t.lower() for t in toks}
    path_provenance = getattr(issue_anchors, "path_provenance", None) or {}
    explicit_paths = {
        _gl_normalize(path)
        for path in (getattr(issue_anchors, "paths", None) or set())
        if path and path_provenance.get(path, "EXPLICIT_PATH") == "EXPLICIT_PATH"
    }
    out: dict[str, list[str]] = {}
    if not toks and not explicit_paths:
        return out
    # Reporter-confirmed provenance (title / backtick code) — exempts ONLY the
    # short-name shape skip below, never the dunder/generic/ambiguity gates.
    _prov: set[str] = set()
    if issue_anchors is not None:
        _prov = set(getattr(issue_anchors, "title_symbols", set()) or set()) | set(
            getattr(issue_anchors, "code_symbols", set()) or set()
        )
    _MAX_FILES_PER_NAME = 3  # a name spread across >3 files is generic, not a specific anchor
    try:
        c = _sq.connect(graph_db)
        # An exact reporter-supplied path is already candidate-local provenance.
        # It does not need a long or distinctive basename: those heuristics only
        # protect prose-derived stems.  Confirm membership against the graph so a
        # nonexistent/scratch path is still correct-or-quiet.
        for (fp,) in c.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE is_test=0 AND file_path IS NOT NULL"
        ):
            if fp and _gl_normalize(str(fp)) in explicit_paths:
                stem = os.path.splitext(os.path.basename(str(fp).replace("\\", "/")))[0]
                out.setdefault(str(fp), [])
                if stem and stem not in out[str(fp)]:
                    out[str(fp)].append(stem)
        _name_files: dict[str, set[str]] = {}
        for name, fp in c.execute(
            "SELECT DISTINCT name, file_path FROM nodes "
            "WHERE is_test=0 AND label IN ('Function', 'Method', 'Class', 'ImplBlock') "
            "AND name IS NOT NULL"
        ):
            if not name or not fp:
                continue
            if name.startswith("__") and name.endswith("__"):  # dunders are never anchors
                continue
            if name.lower() in _GENERIC_CODE_NAMES:  # language builtins are never anchors
                continue
            if len(name) < 5 and "_" not in name and name not in _prov:
                continue  # short generic names: only reporter-confirmed provenance admits them
            if name in toks or name.lower() in toks:
                _name_files.setdefault(name, set()).add(fp)
        for name, files in _name_files.items():
            if len(files) > _MAX_FILES_PER_NAME:  # generic name -> not a specific anchor
                continue
            for fp in sorted(files):  # DETERMINISM (B5-4): `files` is a set
                out.setdefault(fp, [])
                if name not in out[fp]:
                    out[fp].append(name)
        # CAUSE B (gt_math_oh diag, 2026-06-24): the issue often names the gold
        # MODULE (file basename) not its defining SYMBOL — "leafonly plugin",
        # "plugins.chzzk" name leafonly.py / chzzk.py, but the symbol is
        # validate_leaf_only / class Chzzk, so the symbol loop above misses the
        # file. Guarantee a file whose BASENAME STEM the issue names verbatim,
        # under the SAME gates (generic/len/ambiguity) so it cannot flood. This
        # is the GUARANTEE surface (force-promote-past-cut), distinct from the
        # path-rescue recall in v7_4_brief.py:1322 which only seeds candidate_set.
        _stem_files: dict[str, set[str]] = {}
        for (fp,) in c.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE is_test=0 AND file_path IS NOT NULL"
        ):
            if not fp:
                continue
            stem = os.path.splitext(os.path.basename(str(fp).replace("\\", "/")))[0].lower()
            if len(stem) < 5:  # short stems collide with prose
                continue
            if stem in _GENERIC_CODE_NAMES:  # generic module names are never anchors
                continue
            # SELECTIVE: fire ONLY when the issue references the stem AS A MODULE/FILE
            # (dotted path "plugins.chzzk", "<stem>.py", or "<stem> plugin/module"), NOT
            # merely the bare word in prose. A bare-word match floods the guarantee on a
            # long issue (8 files here) and the downstream _promote[:3] cap then drops the
            # real gold. Module-reference is the specific "the issue names this file" signal.
            if _gl_normalize(str(fp)) in explicit_paths:
                _stem_files.setdefault(stem, set()).add(fp)
        for stem, files in _stem_files.items():
            if len(files) > _MAX_FILES_PER_NAME:  # ambiguous stem -> not a specific anchor
                continue
            for fp in files:
                out.setdefault(fp, [])
                if stem not in out[fp]:
                    out[fp].append(stem)
        c.close()
    except Exception:
        pass
    return out


def _exact_name_has_verified_caller(graph_db: str, file_path: str, func_names: list[str]) -> bool:
    """True iff at least one of ``func_names`` defined in ``file_path`` has a
    cross-file caller reaching it through a DETERMINISTIC edge (same_file / import /
    verified_unique / type_flow / lsp ...). This is independent corroboration that an
    issue-named function is a REAL, USED symbol — not a coincidental same-name match
    in a file the issue never meant. Reuses the categorical provenance set
    (_DETERMINISTIC_METHODS) — a name_match edge is NEVER corroboration. Repo- and
    language-agnostic; correct-or-quiet (any error / no method column -> False)."""
    if not graph_db or not file_path or not func_names:
        return False
    conn = None
    try:
        conn = sqlite3.connect(graph_db)
        _, has_method = _has_columns(conn)
        if not has_method:
            return False  # cannot judge provenance -> not corroborated
        _det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
        _norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
        for fname in func_names[:5]:
            support_sql = f"""
                SELECT 1
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND {{file_predicate}}
                  AND nsrc.file_path != nt.file_path
                  AND nsrc.is_test = 0
                  AND LOWER(TRIM(e.resolution_method)) IN ('{_det_sql}')
                LIMIT 1
                """
            row = conn.execute(
                support_sql.format(file_predicate="(nt.file_path = ? OR nt.file_path = ?)"),
                (fname, _norm_fp, "./" + _norm_fp),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    support_sql.format(file_predicate="nt.file_path LIKE ?"),
                    (fname, "%/" + _norm_fp),
                ).fetchone()
            if row is not None:
                return True
        return False
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _apply_evidence_rrf(
    records: list[dict],
    *,
    witness_verified_by_file: dict[str, bool] | None = None,
    loc_rank_by_file: dict[str, int] | None = None,
) -> list[dict]:
    """Final evidence-class RRF reorder of the candidate records.

    Fuses per-class (lexical/semantic/structural/path/historical) reciprocal-rank
    scores so a file backed by MANY independent evidence classes ranks above a
    single-signal file, tie-broken by class count, raw strength, then the
    localizer's own rank and canonical path (incoming order only breaks duplicate-
    path ties).

    P0 #2 fix (2026-07-01) — two defects in the pre-extraction inline version:
      (1) The ``strength > 0`` filter SILENTLY DROPPED records whose evidence
          lives outside the 8-key strength sum — synthesized exact-issue-named
          recall-miss gold (no ``components``), graph-neighbor (``{"path":0.0}``),
          and cochange/test_coimport bridges — undoing the recall/neighborhood
          machinery upstream. They are now KEPT (``_keep_recall_or_bridge``); with
          strength 0 they RRF-sort to the BOTTOM, so recall is preserved without
          ever floating them above real-evidence gold.
      (2) The verified-witness flag lived ONLY in the side-dict
          ``witness_verified_by_file`` and was never written onto the record, so
          ``_issue_evidence_strength`` / ``_positive_evidence_classes`` scored
          verified gold as un-witnessed and multi-signal HUBS out-sorted it. The
          flag (and the localizer rank) are now STAMPED onto each record first.

    Correct-or-quiet: a genuinely hollow set (no real evidence AND no
    recall/neighbor/bridge anchor) still returns ``[]`` so the empty-proof gate
    can halt instead of delivering noise.
    """
    wv = witness_verified_by_file or {}
    lr = loc_rank_by_file or {}

    # (2) Stamp the localizer's side-dict verification ONTO each record so the
    # strength/RRF stage below actually SEES it.
    for _rec in records:
        if not isinstance(_rec, dict):
            continue
        _p = str(_rec.get("path", ""))
        _pn = _p.replace("\\", "/").lstrip("./").lstrip("/")
        if not _rec.get("witness_verified", False) and (wv.get(_p) or wv.get(_pn)):
            _rec["witness_verified"] = True
        if "_loc_rank" not in _rec:
            _r = lr.get(_p)
            if _r is None:
                _r = lr.get(_pn)
            if _r is not None:
                _rec["_loc_rank"] = _r

    def _issue_evidence_strength(rec: dict) -> float:
        comps = rec.get("components", {}) if isinstance(rec, dict) else {}
        if not isinstance(comps, dict):
            return 0.0
        total = 0.0
        for key in ("lex", "sem", "path", "reach", "anchor_prox", "witness", "code_def", "frame"):
            try:
                total += max(0.0, float(comps.get(key, 0.0) or 0.0))
            except Exception:
                continue
        if rec.get("witness_verified", False):
            total += 1.0
        return total

    def _positive_evidence_classes(rec: dict) -> dict[str, float]:
        comps = rec.get("components", {}) if isinstance(rec, dict) else {}
        if not isinstance(comps, dict):
            comps = {}

        def _pos(key: str) -> float:
            try:
                return max(0.0, float(comps.get(key, 0.0) or 0.0))
            except Exception:
                return 0.0

        structural = _pos("reach") + _pos("anchor_prox") + _pos("witness")
        if rec.get("witness_verified", False):
            structural += 1.0
        return {
            "lexical": _pos("lex") + _pos("code_def"),
            "semantic": _pos("sem"),
            "structural": structural,
            "path": _pos("path"),
            "historical": _pos("frame"),
        }

    def _class_count(rec: dict) -> int:
        return sum(1 for v in _positive_evidence_classes(rec).values() if v > 0.0)

    def _ensure_entered_via(rec: dict) -> dict:
        if str(rec.get("entered_via", "") or "").strip():
            return rec
        classes = [k for k, v in _positive_evidence_classes(rec).items() if v > 0.0]
        if not classes:
            return rec
        out = dict(rec)
        out["entered_via"] = "evidence:" + "+".join(classes)
        return out

    def _rrf_evidence_scores(recs: list[dict]) -> dict[int, float]:
        scores = {id(rec): 0.0 for rec in recs}

        def _path(rec: dict) -> str:
            return _gl_normalize(str(rec.get("path", "") or ""))

        def _loc_rank(rec: dict) -> int:
            try:
                return int(rec.get("_loc_rank", 10**6))
            except (TypeError, ValueError):
                return 10**6

        for cls in ("lexical", "semantic", "structural", "path", "historical"):
            ranked = []
            for idx, rec in enumerate(recs):
                val = _positive_evidence_classes(rec).get(cls, 0.0)
                if val > 0.0:
                    ranked.append((idx, rec, val))
            ranked.sort(key=lambda item: (-item[2], _loc_rank(item[1]), _path(item[1]), item[0]))
            for rank, (_idx, rec, _val) in enumerate(ranked, start=1):
                scores[id(rec)] += 1.0 / float(60 + rank)
        return scores

    def _keep_recall_or_bridge(rec: dict) -> bool:
        # Recall/neighborhood/bridge records carry a real localization purpose but
        # no summable evidence component (exact-issue-named recall-miss gold has no
        # `components`; graph-neighbor is {"path":0.0}; cochange/test_coimport
        # bridges use keys outside the strength sum). Keep them so the strength
        # filter can't SILENTLY DROP them; strength-0 records RRF-sort to the
        # bottom, so recall is preserved without floating them above gold.
        if not isinstance(rec, dict):
            return False
        if rec.get("_exact_issue_named"):
            return True
        if str(rec.get("entered_via", "") or "") in (
            "cochange",
            "test_coimport",
            "graph_neighbor",
        ):
            return True
        comps = rec.get("components", {})
        return isinstance(comps, dict) and ("cochange" in comps or "test_coimport" in comps)

    _with_order = list(enumerate(records))
    _evidence_records = [
        (idx, _ensure_entered_via(rec), _issue_evidence_strength(rec)) for idx, rec in _with_order
    ]
    _supported = [
        (idx, rec, strength)
        for idx, rec, strength in _evidence_records
        if strength > 0.0 or _keep_recall_or_bridge(rec)
    ]
    if _supported:
        _rrf = _rrf_evidence_scores([rec for _, rec, _ in _supported])
        _supported.sort(
            key=lambda item: (
                -int(bool(item[1].get("witness_verified", False))),
                -_rrf.get(id(item[1]), 0.0),
                -_class_count(item[1]),
                -item[2],
                int(item[1].get("_loc_rank", 10**6)),
                _gl_normalize(str(item[1].get("path", "") or "")),
                item[0],
            )
        )
        return [rec for _, rec, _ in _supported]
    # Product invariant: no blind delivery. An all-hollow candidate set is a
    # localization failure, not an edit-target list. The live diagnostic gate
    # records/halts this as empty proof instead of silently delivering noise.
    return []


def generate_v1r_brief(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    *,
    bug_id: str = "unknown",
    repo: str = "unknown",
    gold_files: list[str] | None = None,
    max_files: int = MAX_FILES,
    max_brief_tokens: int = MAX_BRIEF_TOKENS,
    weights: dict[str, float] | None = None,
) -> V1RBriefResult:
    # Density check: if the graph is too sparse, GRAPH signals (reach/prox/hub)
    # are noise — zero those and let lexical LEAD. Dense (W_SEM) is FLOORED, not
    # zeroed (§11.6 locked dense-floor policy: floor, never zero, never
    # abort-on-sparse — fix 2026-06-09). The prior hard W_SEM=0.0 here was
    # dead-or-fatal: in proof+require mode forbid_no_sem_config RAISED on every
    # sparse repo, and off-proof the floor in _adapt_weights_for_issue silently
    # resurrected 0 -> 0.25 anyway. Graph sparsity says nothing about the
    # EMBEDDER's health — dense stays alive at the floor while lexical leads.
    _sparse_graph = False
    if weights is None and graph_db:
        try:
            _conn = sqlite3.connect(graph_db)
            _total_edges = _conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            _total_files = _conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[
                0
            ]
            _conn.close()
            _edges_per_file = _total_edges / max(1, _total_files)
            if _edges_per_file < 2.0:
                _sparse_graph = True
                weights = {
                    "W_SEM": _w_sem_floor(),  # dense floored, never zeroed (§11.6)
                    "W_LEX": 0.70,
                    "W_REACH": 0.0,
                    "W_PROX": 0.0,
                    "W_HUB": 0.0,
                    "W_COMMIT": 0.0,
                    "W_PATH": 0.45,
                }
        except Exception:
            pass

    _semantic_anchor_paths: set[str] = set()
    v74 = run_v74(
        issue_text,
        repo_root,
        graph_db,
        bug_id=bug_id,
        repo=repo,
        gold_files=gold_files,
        ablation="C",
        k_anchor=3,
        # Dense seed depth. Env-tunable (default 10 = unchanged): a code-trained embedder
        # ranks the behavior-gold higher, but a top-10 seed cut can still drop it on large
        # repos — deepening the SEED (not just re-ranking) lets the better embedder's recall
        # land (recall agent ab384a9ad8cf05d1a). Pairs with the jina-code A/B.
        k_sem_top=int(os.environ.get("GT_SEM_TOP_K", "10")),
        tau_anchor=0.20,
        max_depth=3,
        min_confidence=EDGE_CONFIDENCE_FLOOR,
        weights=weights,
        focus_size=max_files,
        semantic_body_paths_out=_semantic_anchor_paths,
    )

    if not v74.ranked_full:
        # No candidates ranked (new-file / behavioral no-match). B-2: the OLD path
        # returned an EMPTY <gt-task-brief> — the issue's parsed behavioral spec was
        # LOST at step 0, exactly when a no-match/new-file task most needs it. Render
        # the issue's OWN obligations (leak-screened) plus an honest-uncertainty note
        # so the agent still gets its behavioral contract + a truthful "localize with
        # grep" steer. No located file exists, so the focus-anchor relevance gate is
        # bypassed (require_anchor=False); the leak screen (_obligation_is_leaky) still
        # runs, so zero test-name / FAIL_TO_PASS / gold-path tokens can leak. The
        # embedder WEIGHT is still surfaced (unchanged) so the precheck can tell
        # "embedder on, no candidates" from "embedder off".
        def _nomatch_cap(_l: str) -> str:
            return _clip_body_line(_l, _MAX_BODY_LINE_CHARS)

        _nm_oblig = _render_obligations_block(
            issue_text, [], _nomatch_cap, anchor_symbols=None, require_anchor=False
        )
        _nm_lines = [
            "<gt-task-brief>",
            "Note: GT found no indexed file ranked for this issue (new-file or "
            "behavioral change). Localize with grep/code-search on the issue's terms.",
        ]
        _nm_lines.extend(_nm_oblig)
        _nm_lines.append("</gt-task-brief>")
        _nm_brief = "\n".join(_nm_lines)
        # Brief-F3: the no-match brief must obey the SAME hard token rail (B-30) as the
        # matched path — the early return previously bypassed _enforce_token_rail, so a
        # small max_brief_tokens (or GT_OBLIGATIONS_V2 dynamic-K growth) shipped an
        # over-cap brief. Idempotent: a brief already within budget is byte-identical
        # and _nm_suppressed stays empty.
        _nm_suppressed: list[str] = []
        _nm_control_participation: list[dict] = []
        if _count_tokens(_nm_brief) > max_brief_tokens:
            _nm_brief, _nm_suppressed = _enforce_token_rail(_nm_brief, max_brief_tokens)
        # SM-6 (B): apply the SAME minimal reduction so GT_BRIEF_MINIMAL yields a minimal
        # brief on EVERY path. The no-match brief is already scaffold + grep-orientation
        # note + obligations, so the reducer is a no-op here (nothing to drop); applied for
        # uniformity. DEFAULT-OFF -> byte-identical.
        if _brief_minimal_on():
            _nm_before_minimal = _nm_brief
            _nm_brief = _reduce_brief_to_minimal(_nm_brief)
            _nm_control_participation = _brief_minimal_participation(_nm_before_minimal, _nm_brief)
        # Brief-F7: B-6 per-block receipts also cover the fact-bearing no-match brief
        # (its <gt-obligations> block). Same PURE read as the matched path; brief bytes
        # unchanged. Empty unless receipt/in-seam instrumentation is enabled.
        _nm_receipts = _brief_block_receipts(_nm_brief) if _block_receipts_on() else []
        _nm_control_participation.extend(
            _terminal_pretask_mediator_participation(
                _nm_brief,
                _nm_receipts,
                budget_suppressed=_nm_suppressed,
                semantic_anchor_paths=_semantic_anchor_paths,
            )
        )
        return V1RBriefResult(
            files=[],
            brief_text=_nm_brief,
            token_estimate=_count_tokens(_nm_brief),
            v74_result=v74,
            effective_w_sem=float(getattr(v74, "effective_w_sem", 0.0) or 0.0),
            rendered_candidate_count=0,
            # C15 — these zeros are HONEST on this path and are set EXPLICITLY so that is
            # auditable rather than inherited from a default. This branch is reached only
            # when ``v74.ranked_full`` is empty: the legs ran and ranked nothing, so
            # acquisition is genuinely 0, and nothing was withheld by the reduction, so
            # delivery is genuinely 0 rather than NOT_EVALUABLE. ``delivered_*`` must be
            # stated here because its default is None (NOT_EVALUABLE), which would be the
            # wrong answer on this path.
            acquired_graph_edge_count=0,
            acquired_semantic_signal_count=0,
            acquired_structural_signal_count=0,
            acquired_fts5_signal_count=0,
            delivered_graph_edge_count=0,
            delivered_semantic_signal_count=0,
            delivered_structural_signal_count=0,
            delivered_fts5_signal_count=0,
            delivered_candidate_count=0,
            k_sem_top=int(getattr(v74, "k_sem_top_effective", 0) or 0),
            sem_components=[],
            budget_suppressed=_nm_suppressed,
            block_receipts=_nm_receipts,
            control_participation=_nm_control_participation,
            tokenizer_used=_tokenizer_kind(),
        )

    # Adaptive K: include candidates while score gap is small.
    # Minimum recall guard: always return at least 5 candidates if available.
    # This prevents adaptive K from returning 1 wrong file when recall is low.
    scores = [r.get("score", 0.0) for r in v74.ranked_full]
    # Caller's explicit max_files is an upper bound that must win over the
    # recall floor — never silently exceed it. Clamp the floor to the smaller
    # of the recall target, the caller's cap, and available candidates.
    min_k = min(5, max_files, len(v74.ranked_full))  # floor, capped by max_files
    if len(scores) >= 2:
        gaps = [scores[i] - scores[i + 1] for i in range(min(len(scores) - 1, 10))]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.1
        k = 1
        for i in range(1, min(len(scores), 8)):
            if i < len(gaps) and gaps[i - 1] > median_gap * 2:
                break
            k = i + 1
        top_records = v74.ranked_full[: max(min(k, max_files), min_k)]
    else:
        top_records = v74.ranked_full[:max_files]

    # SINGLE-SOURCE ANCHORS, hoisted (fix 2026-06-10): extracted ONCE here against the
    # SAME graph_db every consumer uses — the exact-name guarantee (both call sites,
    # for title/backtick provenance), the /tmp/gt_issue_anchors.json write, and
    # localize() all reuse THIS object. extract_issue_anchors is deterministic on
    # (issue_text, graph_db), so hoisting is behavior-identical for the later users.
    _anchors_obj = None
    if graph_db:
        try:
            from groundtruth.pretask.anchors import extract_issue_anchors as _eia_h

            _anchors_obj = _eia_h(issue_text, graph_db)
        except Exception:
            _anchors_obj = None

    # gt_gt §4 exact-name GUARANTEE (RANKING fix, not recall): a function named VERBATIM in the
    # issue and present in the graph is the strongest content signal — its file is promoted to the
    # FRONT of the candidates, never composite-scored-and-cut. Pulled from the FULL ranking (so it
    # already passed retrieval); capped to avoid flooding. (LIPI arviz: plot_hdi named 4x, verified
    # caller, was ranked below lexical stats.py and dropped from the top-5.)
    _issue_named = _exact_issue_named_files(issue_text, graph_db, issue_anchors=_anchors_obj)
    if _issue_named:

        def _rf(r):
            return (
                (r.get("path") or r.get("file") or r.get("file_path") or "")
                .replace("\\", "/")
                .lstrip("/")
            )

        _named = {f.replace("\\", "/").lstrip("/"): fns for f, fns in _issue_named.items()}
        _have = {_rf(r) for r in top_records}
        _by_path = {_rf(r): r for r in v74.ranked_full}
        _top_score = float(top_records[0].get("score", 1.0)) if top_records else 1.0
        _promote: list[dict] = []
        for fp in sorted(_named):
            if fp in _have:
                continue
            if fp in _by_path:  # retrieved-but-cut -> pull to front
                _rec = dict(_by_path[fp])
                _rec["_exact_issue_named"] = True  # survive the localize re-rank at :3397
                _promote.append(_rec)
            else:  # recall miss -> synthesize a top record
                _promote.append(
                    {
                        "path": fp,
                        "score": _top_score + 0.01,
                        "functions": _named[fp][:3],
                        "witnesses": [],
                        "_exact_issue_named": True,
                    }
                )
        if _promote:
            top_records = _promote[:3] + top_records

    # Filter non-source files from candidates — changelogs, READMEs, configs, docs
    # rank high on BM25 keywords but are never edit targets
    _NON_SOURCE = {
        "CHANGELOG.md",
        "CHANGES.rst",
        "HISTORY.md",
        "README.md",
        "README.rst",
        "CONTRIBUTING.md",
        "LICENSE",
        "LICENSE.md",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "Makefile",
        "Dockerfile",
        ".gitignore",
    }
    _NON_SOURCE_EXTS = {
        ".rst",
        ".md",
        ".txt",
        ".yml",
        ".yaml",
        ".toml",
        ".lock",
    }
    _NON_SOURCE_NAMES = {name.lower() for name in _NON_SOURCE}

    def _is_non_source_candidate_path(path: str) -> bool:
        _path = str(path or "")
        _basename = os.path.basename(_path).lower()
        _ext = os.path.splitext(_path)[1].lower()
        if _basename.endswith((".test-d.ts", ".test-d.tsx", ".spec-d.ts", ".spec-d.tsx")):
            return True
        _parts = [p for p in _path.replace("\\", "/").lower().split("/") if p]
        if "dts-test" in _parts:
            return True
        if _basename in _NON_SOURCE_NAMES or _ext in _NON_SOURCE_EXTS:
            return True
        # R1 (env-gated): vendored / minified-bundle guard at the DELIVERED seam.
        # A concatenated bundle (e.g. libs/s.js) matches NO path pattern but wins BM25
        # on raw term frequency, evicting the gold from the shallow recall slots and
        # ranking #1 (tutanota). is_vendored_path is path-only; is_minified_file is
        # content-based (mean line length > threshold), so it catches the bundle no
        # path rule sees. This predicate is reused across all candidate seams, so the
        # filter applies coherently everywhere (recall agent ab384a9ad8cf05d1a). Reads
        # the file from repo_root (in closure scope); OSError -> False (safe degrade).
        if os.environ.get("GT_RECALL_PATHCLASS_FILTER", "") == "1":
            try:
                if _is_vendored_path(_path) or _is_minified_file(repo_root, _path):
                    return True
            except Exception:
                pass
        return False

    # De-dup'd (2026-06-15): the nested test-only _is_test_file MISSED demo dirs, so a
    # docs_src/ tutorial .py (no test basename, .py not in _NON_SOURCE_EXTS) survived the
    # candidate filter and was emitted as an edit target (fastapi witness, the real
    # docs_src leak — at candidate RANKING, not render). The single canonical test+demo
    # predicate drops docs_src/examples/... here too.
    top_records = [
        r
        for r in top_records
        if not _is_non_source_candidate_path(r.get("path", ""))
        and not _is_test_or_demo(r.get("path", ""))
    ]
    if not top_records:
        top_records = v74.ranked_full[:max_files]  # fallback if all filtered

    # Path-match preservation: if a candidate has strong path-name match
    # (path component score ≥ 0.5) but didn't make it into top_records,
    # include it by replacing the lowest-scored entry. This prevents
    # BM25-dominant files from pushing out name-matched candidates.
    _top_paths_set = {r.get("path") for r in top_records}
    _path_rescued: list[dict] = []
    for r in v74.ranked_full:
        if r.get("path") in _top_paths_set:
            continue
        comps = r.get("components", {})
        if comps.get("path", 0.0) >= 0.5:
            if not _is_non_source_candidate_path(r.get("path", "")):
                _path_rescued.append(r)
        if len(_path_rescued) >= 2:
            break
    if _path_rescued and len(top_records) >= max_files:
        for pr in _path_rescued:
            if len(top_records) < max_files:
                top_records.append(pr)
            else:
                # Only replace the last record if it is NOT a verified-witnessed
                # candidate. Replacing a verified candidate would discard a
                # structurally-proven localization in favor of a path-rescued guess.
                last = top_records[-1]
                if not last.get("witness_verified", False):
                    top_records[-1] = pr

    # ----- Symbol-anchored graph-witness localization (THE L1 CORE FIX) -----
    # Run the deterministic multi-hop traversal: anchor on issue symbols, BFS
    # graph.db CALLS/IMPORTS, score by witness+lexical+degree. This is the path
    # the old lexical-only ranker lacked — it is what surfaces importer.py on
    # beets-5495 via its set_fields->set_parse witness even though importer.py is
    # NOT a lexical winner. Witnessed candidates are UNIONED with the existing
    # lexical/semantic candidates and PROMOTED above witness-less ones (SWERank
    # hard-negative principle). Correct-or-quiet: if no issue symbol resolves to a
    # graph node, the localizer returns empty and we leave the lexical ranking
    # untouched — exact no-op, no regression on no-anchor tasks.
    _loc: LocalizerResult | None = None
    _witness_by_file: dict[str, str] = {}
    _witness_verified_by_file: dict[str, bool] = {}
    _relevance_by_file: dict[str, str] = {}
    _resolution_methods_by_file: dict[str, frozenset[str]] = {}
    _loc_conf_by_file: dict[str, float] = {}
    # The localizer's OWN rank per file (0 = its #1). This is the authoritative
    # structural localization order; the brief MUST honor it for witnessed files
    # rather than scatter the localizer's #1 behind other candidates or re-sort it
    # by keyword count. (Exact bug, beets-5495: localize ranked importer.py #1 but
    # the integration landed it at ~rank 7 and the keyword boost put hub plugins.py
    # #1, so gold fell below the render cut — proven by checkpoint trace.)
    _loc_rank_by_file: dict[str, int] = {}
    if graph_db:
        try:
            # SINGLE-SOURCE ANCHORS (flow-audit risk #1, proven on matplotlib-27613):
            # extract issue anchors ONCE against the SAME graph_db localize ranks
            # with (cross-checked vs nodes.name), pass them to localize, AND persist
            # them to the canonical /tmp/gt_issue_anchors.json that the in-container
            # consumers read (post_view._contract_pillar / _score_by_issue_relevance
            # / post_edit). Previously the wrapper extracted anchors against
            # _host_graph_db (absent on the default path -> empty/un-cross-checked
            # upload) while localize re-extracted its OWN set, so the contract pillar
            # received an EMPTY set and fell back to the file's first-3 generic
            # functions ([CONTRACT] __init__/__call__/validate_backend instead of
            # cycler/validate_marker). This runs in-container AFTER the wrapper's
            # upload, so its write is authoritative for every downstream consumer.
            import json as _json_anch

            if _anchors_obj is None:  # hoisted single-source extraction (2026-06-10)
                from groundtruth.pretask.anchors import extract_issue_anchors as _eia

                _anchors_obj = _eia(issue_text, graph_db)
            # Oracle Stage 1: the issue-as-SPEC obligations + unresolved_code_symbols
            # (F2) ride the SAME already-shipped artifact. The spec extractor is a
            # SEPARATE consumer of issue_text from anchors (opposite filtering: it
            # KEEPS async/await/returns the anchor extractor drops). Deterministic,
            # no graph dependency, correct-or-quiet (empty list on empty issue).
            try:
                if _obligations_v2_on():
                    from groundtruth.pretask.spec import extract_spec_v2 as _extract_spec

                    _spec = _extract_spec(issue_text)
                    _obligations = _spec.to_serializable(version=2)
                else:
                    from groundtruth.pretask.spec import extract_spec as _extract_spec

                    _spec = _extract_spec(issue_text)
                    _obligations = _spec.to_serializable()
            except Exception:
                _obligations = []
            _anch_payload = {
                "symbols": sorted(_anchors_obj.symbols),
                "paths": sorted(_anchors_obj.paths),
                "test_names": sorted(_anchors_obj.test_names),
                "title_symbols": sorted(getattr(_anchors_obj, "title_symbols", set())),
                "code_symbols": sorted(getattr(_anchors_obj, "code_symbols", set())),
                "unresolved_code_symbols": sorted(
                    getattr(_anchors_obj, "unresolved_code_symbols", set())
                ),
                "symbol_provenance": dict(
                    sorted((getattr(_anchors_obj, "symbol_provenance", {}) or {}).items())
                ),
                "path_provenance": dict(
                    sorted((getattr(_anchors_obj, "path_provenance", {}) or {}).items())
                ),
                "obligations": _obligations,
            }
            # Every anchors artifact is task-bound, regardless of whether the
            # optional v2 obligations renderer is active. Proof admission uses
            # this identity to reject a stale canonical filename from another
            # issue; obligations_version remains the conditional schema marker.
            import hashlib as _hashlib_anch

            _anch_payload["issue_sha256"] = _hashlib_anch.sha256(
                issue_text.encode("utf-8")
            ).hexdigest()
            if _obligations_v2_on():
                # artifact-wins split-brain rule: consumers trust THIS stamp over
                # their own env, so host and container can never disagree.
                _anch_payload["obligations_version"] = 2
            # B10 hardening: write to the per-task path, but if that fails (e.g. a
            # misconfigured/absent $GT_CERT_DIR pointing at a nonexistent dir) fall back to
            # the always-writable /tmp so the anchors artifact is never SILENTLY lost.
            # correct-or-quiet only when BOTH targets are unwritable (unit tests / RO fs).
            _anch_targets = [_anchors_path(for_write=True)]
            if "/tmp/gt_issue_anchors.json" not in _anch_targets:
                _anch_targets.append("/tmp/gt_issue_anchors.json")
            for _ap in _anch_targets:
                try:
                    import tempfile as _tempfile_anch

                    _ad = os.path.dirname(_ap) or "."
                    _afd, _atmp = _tempfile_anch.mkstemp(
                        prefix=f".{os.path.basename(_ap)}.", dir=_ad
                    )
                    try:
                        with os.fdopen(_afd, "w", encoding="utf-8", newline="") as _af:
                            _json_anch.dump(_anch_payload, _af)
                            _af.flush()
                            os.fsync(_af.fileno())
                        # mkstemp is 0600 by contract; this artifact is harvested by a
                        # NON-ROOT host cp while the container writes as root (W1a-PERM
                        # class, sweep A1) — publish world-readable or the copy silently
                        # EACCESes and the anchors artifact vanishes from the upload.
                        os.chmod(_atmp, 0o644)
                        os.replace(_atmp, _ap)
                    except BaseException:
                        try:
                            os.unlink(_atmp)
                        except OSError:
                            pass
                        raise
                    break
                except OSError:
                    continue
            _loc = localize(
                issue_text, graph_db, top_k=8, issue_anchors=_anchors_obj, repo_root=repo_root
            )
        except Exception:
            _loc = None
    if _loc and _loc.candidates:
        _existing = {str(r.get("path", "")) for r in top_records}
        _existing_norm = {p.replace("\\", "/").lstrip("./").lstrip("/") for p in _existing}
        _promoted: list[dict] = []
        for _ci, cand in enumerate(_loc.candidates):
            cf = cand.file_path
            _relevance = str(getattr(cand, "relevance_grade", "INFO") or "INFO")
            _relevance_by_file[cf] = _relevance
            _witness_by_file[cf] = cand.render_witness() if _relevance == "VERIFIED" else ""
            _witness_verified_by_file[cf] = _relevance == "VERIFIED"
            _resolution_methods_by_file[cf] = frozenset(
                str(getattr(witness, "resolution_method", "") or "").strip().lower()
                for witness in cand.witnesses
                if getattr(witness, "verified", False)
                and str(getattr(witness, "resolution_method", "") or "").strip()
            )
            _loc_conf_by_file[cf] = cand.confidence
            _loc_rank_by_file[cf] = _ci
            if _is_non_source_candidate_path(cf):
                continue
            # A witnessed file the lexical path missed is ADDED — this is exactly
            # the beets-5495 case (importer.py absent from lexical candidates).
            if cf not in _existing and cf not in _existing_norm:
                _promoted.append(
                    {
                        "path": cf,
                        "score": cand.score,
                        "components": {"path": 0.0, "witness": cand.confidence},
                        "entered_via": "graph_witness",
                    }
                )
        # Prepend verified-witnessed candidates so they rank ABOVE witness-less
        # lexical hard-negatives, then keep the original lexical order after them.
        # Only verified witnesses jump the queue (correct-or-quiet); name_match
        # witnesses are added but not promoted ahead of lexical winners.
        _verified_promoted = [p for p in _promoted if _witness_verified_by_file.get(p["path"])]
        _unverified_promoted = [
            p for p in _promoted if not _witness_verified_by_file.get(p["path"])
        ]

        # Also reorder EXISTING records: a lexical record that the localizer
        # verified-witnessed should sort ahead of a witness-less one.
        def _is_verified_witnessed(rec: dict) -> bool:
            # gt_gt §4.1: an issue-EXACTLY-named symbol (its function appears verbatim in the
            # issue) ranks with the verified group — it is the strongest anchor that exists.
            if rec.get("_exact_issue_named"):
                return True
            p = str(rec.get("path", ""))
            pn = p.replace("\\", "/").lstrip("./").lstrip("/")
            return bool(_witness_verified_by_file.get(p) or _witness_verified_by_file.get(pn))

        _existing_verified = [r for r in top_records if _is_verified_witnessed(r)]
        _existing_rest = [r for r in top_records if not _is_verified_witnessed(r)]

        # Order ALL verified-witnessed records (promoted + already-present) by the
        # LOCALIZER's own rank, not by which bucket they fell in. Without this,
        # importer.py (localize #1) lands behind query.py/db.py (localize #2/#4)
        # purely because those were absent from the base lexical set and it wasn't.
        def _loc_rank(rec: dict) -> int:
            if rec.get("_exact_issue_named"):
                return -1  # the issue literally names this function -> sort FIRST
            p = str(rec.get("path", ""))
            pn = p.replace("\\", "/").lstrip("./").lstrip("/")
            r = _loc_rank_by_file.get(p)
            if r is None:
                r = _loc_rank_by_file.get(pn)
            return r if r is not None else 10**6

        _all_verified = sorted(_verified_promoted + _existing_verified, key=_loc_rank)
        top_records = _all_verified + _existing_rest + _unverified_promoted

        # GUARANTEE: every verified-witnessed localizer candidate appears in
        # the rendered brief (not dropped by MAX_FILES cut). The agent needs
        # to see graph connections (callers/callees) to navigate to the gold
        # file. GT curates the graph map; the agent navigates.
        # If a verified candidate is in the localizer but ranked below
        # MAX_FILES in top_records, inject it into the top set.
        _rendered_paths = {str(r.get("path", "")) for r in top_records[: max(max_files, 5)]}
        _rendered_norm = {p.replace("\\", "/").lstrip("./").lstrip("/") for p in _rendered_paths}
        for _ci, cand in enumerate(_loc.candidates[:6]):
            if not cand.has_verified_witness:
                continue
            cf = cand.file_path
            if cf in _rendered_norm or cf in _rendered_paths:
                continue
            # This verified candidate would be cut — inject it
            top_records.insert(
                min(len(_all_verified) + 1, len(top_records)),
                {
                    "path": cf,
                    "score": cand.score,
                    "components": {"path": 0.0, "witness": cand.confidence},
                    "entered_via": "graph_witness_guarantee",
                },
            )

        # MIN-SEM GUARANTEE: semantics must REACH the rendered brief, not just be
        # computed. When the composite ranks witness/lexical files into the top set and
        # the sem-scored files fall below it (haystack-8489: rendered top-5 were all
        # witness-only sem=0 while pipeline/component/tracer carried distinct cosines),
        # the embedder is consumed in SCORING but ABSENT from DELIVERY -> the agent never
        # sees the semantic signal and GATE-3 correctly reports it un-consumed. Mirror the
        # verified-witness guarantee above: if NO rendered candidate carries a positive
        # sem component, inject the highest-sem candidate from ranked_full. Generalized
        # (any repo/language); correct-or-quiet (no-op when a sem candidate is already
        # rendered, or when the embedder produced nothing).
        _rendered_now = top_records[: max(max_files, 5)]
        if _rendered_now and not any(
            float(r.get("components", {}).get("sem", 0.0) or 0.0) > 0.0 for r in _rendered_now
        ):
            _sem_pool = [
                r
                for r in v74.ranked_full
                if str(r.get("path", "")) not in {str(x.get("path", "")) for x in top_records}
                and float(r.get("components", {}).get("sem", 0.0) or 0.0) > 0.0
            ]
            if _sem_pool:
                _best_sem = max(
                    _sem_pool, key=lambda r: float(r.get("components", {}).get("sem", 0.0) or 0.0)
                )
                if not _is_non_source_candidate_path(str(_best_sem.get("path", ""))):
                    top_records.insert(min(len(_all_verified) + 1, len(top_records)), _best_sem)

    # Graph neighbor expansion: callers/callees of top-ranked files become
    # candidates themselves. This is the core GT-agent collaboration: L1 gives
    # the NEIGHBORHOOD, not just the ranked list. The agent navigates from there.
    if graph_db and top_records:
        _existing_paths = {r.get("path") for r in top_records}
        _neighbor_candidates: list[dict] = []
        _nc = None
        try:
            _nc = sqlite3.connect(graph_db)
            _conf_clause = _edge_conf_clause(graph_db)
            for rec in top_records[:3]:
                fp = rec.get("path", "")
                if not fp:
                    continue
                # Get callers and callees (1-hop neighbors)
                rows = _nc.execute(
                    f"""
                    SELECT DISTINCT n2.file_path FROM nodes n1
                    JOIN edges e ON e.source_id = n1.id AND e.type = 'CALLS' {_conf_clause}
                    JOIN nodes n2 ON e.target_id = n2.id
                    WHERE n1.file_path = ? AND n2.file_path != ? AND n2.is_test = 0
                    UNION
                    SELECT DISTINCT n1.file_path FROM nodes n2
                    JOIN edges e ON e.target_id = n2.id AND e.type = 'CALLS' {_conf_clause}
                    JOIN nodes n1 ON e.source_id = n1.id
                    WHERE n2.file_path = ? AND n1.file_path != ? AND n1.is_test = 0
                    """,
                    (fp, fp, fp, fp),
                ).fetchall()
                for (neighbor,) in rows:
                    if neighbor in _existing_paths:
                        continue
                    if _is_non_source_candidate_path(neighbor):
                        continue
                    _neighbor_candidates.append(
                        {
                            "path": neighbor,
                            "score": rec.get("score", 0) * 0.8,
                            "components": {"path": 0.0},
                            "entered_via": "graph_neighbor",
                        }
                    )
                    _existing_paths.add(neighbor)
                    if len(_neighbor_candidates) >= 3:
                        break
                if len(_neighbor_candidates) >= 3:
                    break
        except Exception:
            pass
        finally:
            if _nc is not None:
                _nc.close()
        # Insert neighbors after current top records (they'll be ranked 4-7ish)
        top_records.extend(_neighbor_candidates)

    # Cross-domain detection + expansion (Decision 26)
    if _detect_overconfident_convergence(top_records, graph_db):
        symptom_files = [r.get("path", "") for r in top_records[:5]]
        cochange_bridges = _expand_via_cochange(symptom_files, repo_root)
        test_bridges = _expand_via_test_coimport(symptom_files, graph_db)

        # Add bridges at lower score (60% of lowest top-5 score)
        if top_records:
            bridge_score = top_records[min(4, len(top_records) - 1)].get("score", 0) * 0.6
            for bridge in cochange_bridges + test_bridges:
                bridge["score"] = bridge_score
                if bridge["path"] not in {r.get("path") for r in top_records}:
                    top_records.append(bridge)

    # Decision 29: redundancy suppression removed. It killed briefs on too many tasks
    # (required all top-3 to enter via "both" paths), leaving agent with zero localization.
    # The modulus gate below handles the "all candidates are noise" case.

    # Hub demotion: reorder so peripheral files come before hubs.
    # NEVER suppress the brief entirely — an imperfect brief is better than none.
    _indexed_file_count = len(v74.ranked_full) if v74 else 0
    if top_records and graph_db and _indexed_file_count >= 50 and not _sparse_graph:
        conn = None
        try:
            conn = sqlite3.connect(graph_db)
            all_degrees = [
                r[0]
                for r in conn.execute(
                    # I2 (no depth-in-rank): CALLS-scoped degree only — promoted depth
                    # edges (READS/WRITES/DATA_FLOW/…) must not inflate the hub p80 and
                    # reorder the delivered file rank. Matches _hub_degree_fn.
                    "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' GROUP BY n.file_path"
                ).fetchall()
            ]
            if all_degrees:
                p80 = sorted(all_degrees)[int(len(all_degrees) * 0.8)]
                if p80 > 0:
                    top_paths = [str(r.get("path", "")) for r in top_records[:5]]
                    top_degrees = []
                    for p in top_paths:
                        row = conn.execute(
                            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' WHERE n.file_path = ?",
                            (p,),
                        ).fetchone()
                        top_degrees.append(row[0] if row else 0)
                    # Demote hubs behind peripheral candidates (never suppress)
                    hub_records = [r for r, d in zip(top_records[:5], top_degrees) if d > p80]
                    non_hub_records = [r for r, d in zip(top_records[:5], top_degrees) if d <= p80]
                    rest = top_records[5:]
                    if non_hub_records:
                        top_records = non_hub_records + hub_records + rest
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

    # EXACT-PATH COVERAGE FLOOR (2026-06-28). The LAST word on delivered membership,
    # after every reshuffle above (exact-named, path-rescue, witness-promote, sem-
    # inject, neighbor-extend, hub-demote). A file whose path component is an EXACT
    # match (==1.0 — the issue token IS the file stem) is a near-certain localization
    # fact; the magnitude-free RRF fusion flattens it to one-rank-among-N and the
    # downstream reorders can then push it below the delivered cut. Confirmed: bytes
    # `hex.rs` path=1.000 dropped rank-3 (linear) -> not-delivered (rrf). This floor
    # guarantees an EXACT path-match file sits inside the delivered window. It is
    # NOT a threshold (1.0 is categorical exact-match, like an exact issue-named
    # symbol) and NOT signal-magnitude tuning, so a weak path argmax (e.g. 0.3 in a
    # pure-behavior task) is never injected -> no harm to the working regimes. The
    # displaced boundary record stays in top_records (shifts out of the top max_files,
    # not dropped). Membership only; ordering of the rest is untouched.
    if top_records and v74 and getattr(v74, "ranked_full", None):
        _exact = [
            r
            for r in v74.ranked_full
            if float((r.get("components") or {}).get("path", 0.0) or 0.0) >= 0.999
            and str(r.get("path", ""))
            and not _is_non_source_candidate_path(str(r.get("path", "")))
        ]
        if _exact:
            _win = {str(r.get("path", "")) for r in top_records[:max_files]}
            for _el in _exact:
                _ep = str(_el.get("path", ""))
                if _ep in _win:
                    continue
                top_records = [r for r in top_records if str(r.get("path", "")) != _ep]
                top_records.insert(min(max_files - 1, len(top_records)), _el)
                _win = {str(r.get("path", "")) for r in top_records[:max_files]}

    # NOTE (2026-06-28): an EXACT-NAME coverage floor was tried here and REVERTED —
    # it was a net regression (60-case proof 53->52). When an issue names several
    # symbols (e.g. express "Router, request, response"), the floor force-injected the
    # sibling files (router.js/request.js/response.js) and DISPLACED the actual gold
    # (express.js) out of the delivered window. Unlike exact-PATH (path==1.0 requires
    # df==1, a UNIQUE file), exact-NAME has no uniqueness guard, so it over-injects.
    # The 3 residual in-set-buried cases (serde/bat/fzf) are a RANKING problem (gold
    # has decent sem/lex but reach=0, out-ranked at 7-9), not a membership gap — not
    # fixable by a coverage floor without this over-injection harm. Left documented.

    _words = set(w.lower() for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text) if len(w) > 3)
    # CODE-SYMBOL provenance (backtick/fence-marked, IssueAnchors.code_symbols +
    # unresolved_code_symbols). The per-file function rankers (_top_functions /
    # _top_function_names) HOIST a code-symbol-anchored function but only TIEBREAK
    # on an NL word from _words — so a coincidental prose-word match (start /
    # template / check) never out-ranks a structurally-central function. Empty when
    # anchors are unavailable -> rankers fall back to NL-tiebreak-under-degree.
    _code_syms: set[str] = set()
    if _anchors_obj is not None:
        _code_syms = {
            s.lower()
            for s in (
                set(getattr(_anchors_obj, "code_symbols", set()) or set())
                | set(getattr(_anchors_obj, "unresolved_code_symbols", set()) or set())
            )
            if s
        }

    # Bug 8 fix: issue-keyword boost — re-rank candidates by path/function overlap
    # with issue text. Structural ranking alone puts the correct file at #3/#4 when
    # the file name or function names match issue keywords.
    _issue_terms: set[str] = set()
    try:
        _terms_raw = open("/tmp/gt_issue_terms.txt").read().strip()
        _issue_terms = {t.lower() for t in _terms_raw.split("\n") if t.strip()}
    except OSError:
        pass
    if not _issue_terms:
        _issue_terms = _words  # fallback to extracted words from issue_text
    if _issue_terms and len(top_records) > 1:
        # One shared, reused connection for the whole boost — was a fresh connect
        # per candidate (review C10: N connections + leak on exception).
        _ik_conn = None
        try:
            try:
                _ik_conn = sqlite3.connect(graph_db)
            except Exception:
                _ik_conn = None

            def _file_issue_score(rec: dict) -> float:
                fp = str(rec.get("path", "")).lower().replace("\\", "/")
                parts = fp.split("/")
                # Count how many issue terms appear in path components
                path_hits = sum(1 for t in _issue_terms if any(t in p for p in parts))
                # Also check function names if available from graph
                func_hits = 0
                if _ik_conn is not None:
                    try:
                        _func_rows = _ik_conn.execute(
                            "SELECT name FROM nodes WHERE file_path = ? "
                            "AND label IN ('Function', 'Method', 'Class', 'ImplBlock') AND is_test = 0 LIMIT 10",
                            (rec.get("path", ""),),
                        ).fetchall()
                        for (fn,) in _func_rows:
                            if fn.lower() in _issue_terms:
                                func_hits += 2  # function name match is strong signal
                    except Exception:
                        pass
                return path_hits + func_hits

            # Stable sort: within same issue-score, preserve structural ranking.
            # PRIMARY key is the verified graph witness (SWERank hard-negative
            # principle): a file the localizer proved via a deterministic edge
            # MUST NOT be demoted below a lexical hard-negative by keyword count.
            # importer.py (witnessed, few keyword hits) stays ahead of pipeline.py
            # (no witness, many keyword hits). Falls back to issue-score then the
            # original index for witness-less files — no-op when no witness exists.
            def _verified_key(rec: dict) -> int:
                p = str(rec.get("path", ""))
                pn = p.replace("\\", "/").lstrip("./").lstrip("/")
                return (
                    0
                    if (_witness_verified_by_file.get(p) or _witness_verified_by_file.get(pn))
                    else 1
                )

            # Among verified-witnessed files, the LOCALIZER's rank is authoritative
            # and MUST dominate keyword count — otherwise a hub (plugins.py) with
            # more issue-keyword hits jumps ahead of localize #1 (importer.py). For
            # witness-less files this is 10**6 (a tie), so they still order by
            # keyword score exactly as before — no regression on no-witness tasks.
            def _loc_rank_key(rec: dict) -> int:
                p = str(rec.get("path", ""))
                pn = p.replace("\\", "/").lstrip("./").lstrip("/")
                r = _loc_rank_by_file.get(p)
                if r is None:
                    r = _loc_rank_by_file.get(pn)
                return r if r is not None else 10**6

            _issue_scores = [
                (_verified_key(r), _loc_rank_key(r), _file_issue_score(r), i, r)
                for i, r in enumerate(top_records)
            ]
            _issue_scores.sort(key=lambda x: (x[0], x[1], -x[2], x[3]))
            top_records = [r for *_, r in _issue_scores]
        finally:
            if _ik_conn is not None:
                _ik_conn.close()
    # FINAL exact-name GUARANTEE (after ALL reordering): the verified-witness rebuild can drop a
    # synthesized issue-named record, so re-assert it here, right before entries are built. gt_gt
    # §4: a function named verbatim in the issue is the strongest anchor — its file MUST render.
    #
    # KINK #5 (residual brief noise): the guarantee used to FRONT-INJECT *every* issue-named file
    # not already in the top (`top_records = _inj + top_records`), sorted only by retrieval score.
    # A function name that is SPECIFIC ENOUGH to pass _exact_issue_named_files (not a dunder, len>=5,
    # in <=3 files) can still appear COINCIDENTALLY in a non-gold file (arviz: inference_data.py /
    # utils.py; loguru: _error_interceptor.py). Force-prepending those flanked the gold with
    # non-gold issue-named files. Fix (correct-or-quiet, content+confidence-gated, NOT reach):
    # split the injections into CORROBORATED vs COINCIDENCE.
    #   CORROBORATED (>=1 independent signal) -> keep the front-promotion (the rescue purpose):
    #     - verified graph-traversal witness (_witness_verified_by_file), OR
    #     - the issue-named function has a DETERMINISTIC cross-file caller edge (real, used symbol), OR
    #     - retrieval itself already ranked the file in its native top-`max_files`
    #       (the guarantee is only protecting it from a downstream reorder drop).
    #   COINCIDENCE (specific-but-unbacked same-name, ranked low/absent natively) -> still injected
    #     (recall guarantee preserved) but APPENDED AFTER the native top candidates, capped, NEVER
    #     forced to the front. The gold (already in the native top: arviz hdiplot.py #0, loguru
    #     _datetime.py top-3) keeps its slot; pure-coincidence matches drop below it.
    # Research: BLUiR ASE 2013 / FINAL_REPORT lever #4 (deterministic method, name_match != fact),
    # SWERank ICLR 2025 (verified witness hard-negative), §4 (content + gate, not reach).
    _ein = _exact_issue_named_files(issue_text, graph_db, issue_anchors=_anchors_obj)
    if _ein:

        def _rfp(r):
            return (r.get("path") or r.get("file") or "").replace("\\", "/").lstrip("/")

        _ein_n = {f.replace("\\", "/").lstrip("/"): fns for f, fns in _ein.items()}
        _bp = {_rfp(r): r for r in v74.ranked_full}
        # Native retrieval rank by normalized path (0 = retrieval's #1). Used to detect
        # "retrieval already ranked it high" corroboration without re-scoring.
        _native_rank = {_rfp(r): i for i, r in enumerate(v74.ranked_full)}
        _topsc = float(top_records[0].get("score", 1.0)) if top_records else 1.0
        _in_top = {_rfp(r) for r in top_records[:max_files]}

        def _is_corroborated(_fp: str, _funcs: list[str]) -> bool:
            # (a) verified graph-traversal witness (structural fact)
            if _witness_verified_by_file.get(_fp) or _witness_verified_by_file.get(
                _fp.replace("\\", "/").lstrip("./").lstrip("/")
            ):
                return True
            # (b) retrieval natively ranked this file in its own top-`max_files`
            _nr = _native_rank.get(_fp)
            if _nr is not None and _nr < max_files:
                return True
            # (c) the issue-named function is a REAL, USED symbol (deterministic caller edge)
            if _exact_name_has_verified_caller(graph_db, _fp, _funcs):
                return True
            return False

        _front: list[dict] = []  # corroborated -> keep front-promotion
        _back: list[dict] = []  # coincidence -> append below native top, capped
        for _fp in sorted(_ein_n):  # DETERMINISM (B5-4): dict-from-set order
            if _fp in _in_top:
                # Preserve the trusted exact-name provenance on an already-present
                # record. Later vendor filtering can remove its stronger sibling;
                # without this stamp the surviving source record becomes hollow and
                # the terminal RRF incorrectly drops it.
                for _existing_record in top_records:
                    if _rfp(_existing_record) == _fp:
                        _existing_record["_exact_issue_named"] = True
                continue
            _r = _bp.get(_fp) or {
                "path": _fp,
                "score": _topsc + 0.01,
                "functions": _ein_n[_fp][:3],
                "witnesses": [],
                "_exact_issue_named": True,
            }
            if _is_corroborated(_fp, _ein_n[_fp]):
                _front.append(_r)
            else:
                _back.append(_r)
        # DETERMINISM (B5-4): break score ties by path so the _back[:2] cap picks the same
        # two files run-to-run (stable sort otherwise preserves hashseed-dependent order).
        _front.sort(key=lambda r: (-float(r.get("score", 0.0)), r.get("path", "")))
        _back.sort(key=lambda r: (-float(r.get("score", 0.0)), r.get("path", "")))
        # Front-promote ONLY corroborated injections. Coincidence injections go AFTER the native
        # top candidates (preserve the gold's slot), capped to 2 so they don't flood the brief.
        _MAX_COINCIDENCE_INJ = 2
        if _back:
            top_records = _front + top_records + _back[:_MAX_COINCIDENCE_INJ]
        else:
            top_records = _front + top_records
        _seen = set()
        _dedup = []
        for _r in top_records:
            _k = _rfp(_r)
            if _k in _seen:
                continue
            _seen.add(_k)
            _dedup.append(_r)
        top_records = _dedup
    # VENDORED / DEMO demote (root cause C, 2026-06-17) — FAIL-CLOSED chokepoint.
    # The upstream candidate filter drops test/demo paths, but the LATER injection
    # paths (path-rescue, _exact_issue_named_files front/back promotion, hub
    # re-sort) re-admit candidates WITHOUT re-checking the path class — so a
    # vendored copy (benchmark/libs/mashumaro/common.py, examples/**/qunit.js,
    # third_party/…) leaked into the brief's edit-target list (py+js §4). Drop them
    # here, after EVERY injection, so no path can re-admit them. Reuses the single
    # canonical predicates (is_test_or_demo catches benchmark/examples/vendor
    # segments; is_vendored_path catches third_party/node_modules/site-packages dir
    # markers) — no new ad-hoc list. Correct-or-quiet: if this would empty the set,
    # keep the pre-filter records (never collapse to a blank brief), mirroring the
    # upstream candidate-filter fallback.
    _kept = [
        r
        for r in top_records
        if not _is_non_source_candidate_path(r.get("path", "") or "")
        and not _is_test_or_demo(r.get("path", "") or "")
        and not _is_vendored_path(r.get("path", "") or "")
    ]
    top_records = _kept

    # Final evidence-class RRF reorder. Extracted to module scope (_apply_evidence_rrf)
    # so the terminal ranking stage is unit-testable in isolation, and so the two P0 #2
    # fixes live in one place: (1) recall/neighbor/bridge records are not silently
    # dropped by the strength filter; (2) the localizer's side-dict verification is
    # stamped onto each record so verified gold is not out-sorted by multi-signal hubs.
    top_records = _apply_evidence_rrf(
        top_records,
        witness_verified_by_file=_witness_verified_by_file,
        loc_rank_by_file=_loc_rank_by_file,
    )

    entries: list[FileEntry] = []
    for rec in top_records:
        path = str(rec.get("path", ""))
        score = float(rec.get("score", 0.0))
        funcs = _top_functions(graph_db, path, issue_terms=_words, code_symbols=_code_syms)
        neighbors = _issue_relevant_neighbors(
            graph_db,
            path,
            repo_root,
            _words,
        )
        func_names = _top_function_names(
            graph_db, path, issue_terms=_words, code_symbols=_code_syms
        )
        contract = _caller_contract_for_file(graph_db, path, repo_root, func_names)
        contract_props = contract_line(graph_db, path, func_names)
        siblings = _sibling_context(graph_db, path, func_names)
        last_chg = _last_change(path, repo_root)
        # Prefer the indexer's mined cochanges table (fast, worktree-safe); fall
        # back to the git-log miner when the table is absent/empty.
        co_changes = _co_change_from_table(graph_db, path) or _co_change_files(path, repo_root)
        spec_parts = [_function_spec(graph_db, path, fn, repo_root) for fn in func_names[:2]]
        spec = " | ".join(s for s in spec_parts if s)
        pattern = f"{siblings}" if siblings else ""
        if last_chg:
            pattern = f"{pattern} | Last: {last_chg}" if pattern else f"Last: {last_chg}"
        # Attach the graph-traversal witness (if the localizer surfaced this file).
        # Look up under both raw and normalized path forms since top_records may
        # carry either depending on which stage admitted the candidate.
        _pn = path.replace("\\", "/").lstrip("./").lstrip("/")
        _wit = _witness_by_file.get(path) or _witness_by_file.get(_pn) or ""
        _wit_ver = bool(_witness_verified_by_file.get(path) or _witness_verified_by_file.get(_pn))
        _wit_conf = _loc_conf_by_file.get(path) or _loc_conf_by_file.get(_pn) or 0.0
        _relevance = _relevance_by_file.get(path) or _relevance_by_file.get(_pn) or ""
        if not _relevance:
            _path_provenance = getattr(_anchors_obj, "path_provenance", None) or {}
            _explicit_paths = {
                _gl_normalize(p)
                for p in (getattr(_anchors_obj, "paths", None) or set())
                if p and _path_provenance.get(p, "EXPLICIT_PATH") == "EXPLICIT_PATH"
            }
            if _pn in _explicit_paths or rec.get("_exact_issue_named"):
                _relevance = "VERIFIED"
            else:
                _comps = rec.get("components") or {}
                _classes = {
                    "lexical": float(_comps.get("lex", 0.0) or 0.0)
                    + float(_comps.get("code_def", 0.0) or 0.0),
                    "semantic": float(_comps.get("sem", 0.0) or 0.0),
                    "structural": float(_comps.get("reach", 0.0) or 0.0)
                    + float(_comps.get("anchor_prox", 0.0) or 0.0)
                    + float(_comps.get("witness", 0.0) or 0.0),
                    "path": float(_comps.get("path", 0.0) or 0.0),
                    "historical": float(_comps.get("frame", 0.0) or 0.0),
                }
                _relevance = (
                    "WARNING" if sum(value > 0 for value in _classes.values()) >= 2 else "INFO"
                )
        # v74 anchor proximity for this candidate (edge-independent issue-subject
        # signal) — carried onto the FileEntry so _entry_confidence_tier can keep an
        # anchor-matched file out of the [INFO] drop (BUG-3). Records are dicts with a
        # `components` sub-dict; fall back to a flat key, then 0.0.
        _aprox = float(
            (rec.get("components") or {}).get("anchor_prox", rec.get("anchor_prox", 0.0)) or 0.0
        )
        entries.append(
            FileEntry(
                path=path,
                score=score,
                functions=funcs,
                callees=neighbors,
                co_changes=co_changes,
                contract=contract,
                contract_props=contract_props,
                pattern=pattern,
                spec=spec,
                function_names=func_names,
                witness=_wit,
                witness_verified=_wit_ver,
                localizer_confidence=_wit_conf,
                anchor_prox=_aprox,
                relevance_grade=_relevance,
            )
        )

    # ---- L1 CROSS-WIRE FIX (confidence-aware single ordering source) ----
    # HIGH is a singular structural target, so its localizer primary aligns every
    # file-keyed block. MEDIUM/LOW are contention sets: their header follows the
    # already-terminal evidence order and MUST NOT reorder `entries`. This prevents
    # a weak localizer #1 from overriding stronger fused evidence while keeping the
    # brief internally consistent. Pure reorder of a frozen localizer view; no
    # candidate is added/dropped and the inputs are not mutated.
    if _brief_minimal_on():
        # Minimal bytes and their telemetry share one bounded population. Apply
        # max_files before either renderer so no visible header candidate can sit
        # outside `.files`/localization_proof.
        entries = entries[:max_files]
        _loc_header, _loc_primary, _loc_tier = _localization_header_for_entries(
            _loc,
            graph_db,
            issue_text,
            entries,
        )
    else:
        # Preserve the historical full renderer exactly when the kill-switch is
        # off. Candidate-population alignment is a Profile-2/minimal behavior.
        _loc_header, _loc_primary = _localization_header(
            _loc,
            graph_db,
            issue_text,
        )
        _loc_tier = _localization_confidence_tier(_loc_header)
    if _loc_tier == "high" and _loc_header and _loc_primary and entries:
        _lp_norm = _gl_normalize(_loc_primary)
        _pi = next(
            (i for i, e in enumerate(entries) if _gl_normalize(e.path) == _lp_norm),
            None,
        )
        if _pi not in (None, 0):
            entries.insert(0, entries.pop(_pi))
    # BUG-3 instrumentation: prove whether anchor_prox actually reaches the FileEntry on
    # the LIVE brief path (the l1_ranking_diagnosis showed 1.0, but the rendered brief
    # dropped those files — telemetry-vs-delivery gap). Logs the per-entry tier + anchor_prox
    # so a single re-run reveals if anchor_prox is 0 at runtime (run_v74 anchor extraction
    # issue) vs a tier/plumbing bug. stderr → captured to gt_brief_stderr.log.
    try:
        import sys as _sys

        _ap_cov = sum(
            1 for e in entries if getattr(e, "anchor_prox", 0.0) >= _ANCHOR_PROX_WARN_FLOOR
        )
        _ap_dump = ", ".join(
            f"{os.path.basename(e.path)}:ap={getattr(e, 'anchor_prox', 0.0):.3f}:tier={_entry_confidence_tier(e, issue_text)}"
            for e in entries[:8]
        )
        print(
            f"[GT_META] BUG3_ANCHOR_PROX entries={len(entries)} ap_ge_floor={_ap_cov} | {_ap_dump}",
            file=_sys.stderr,
            flush=True,
        )
    except Exception:
        pass

    # WIRE n_components (was a DEAD signal: computed in localize(), zero consumers).
    # Its stated consumer is "8-dp logging" — emit it on the SAME GT_META/stderr
    # channel (-> gt_brief_stderr.log) the harness already captures, NOT a new sink.
    # n_components counts ALL clusters among the top candidates (connected scope
    # chains + disjoint singletons); >1 = a FRAGMENTED edit-set whose disjoint
    # clusters the scope-chain section cannot show (the INCOMPLETE_SCOPE early
    # warning). Reads the singleton count that is UNIQUE to n_components (not derivable
    # from scope_chains alone) — no duplication. Diagnostic only: no ranking, no brief
    # content, so no BRIEFING measurement obligation.
    try:
        import sys as _sys_nc

        _nc = int(getattr(_loc, "n_components", 0) or 0)
        _nc_chains = len(getattr(_loc, "scope_chains", None) or [])
        print(
            f"[GT_META] SCOPE_COMPONENTS n_components={_nc:.8f} "
            f"rendered_chains={_nc_chains:.8f} fragmented={1.0 if _nc > 1 else 0.0:.8f}",
            file=_sys_nc.stderr,
            flush=True,
        )
    except Exception:
        pass

    # Compute cross-file scope (Signal 1)
    _scope_files: list[str] = []
    _scope_confidence = "low"
    if graph_db and entries and not _sparse_graph:
        from groundtruth.config.signal_thresholds import (
            SCOPE_MIN_CALLER_FILES,
            SCOPE_MIN_EDGE_CONFIDENCE,
            SCOPE_HIGH_RESOLUTION_METHODS,
            log_threshold_use,
        )

        _sc = None
        try:
            _sc = sqlite3.connect(graph_db)
            _top_path = entries[0].path
            _has_conf = _has_confidence(graph_db)
            if _has_conf:
                _scope_rows = _sc.execute(
                    """SELECT DISTINCT nsrc.file_path, e.resolution_method, e.confidence
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
                       ORDER BY e.confidence DESC LIMIT 10""",
                    (_top_path, _top_path),
                ).fetchall()
            elif _has_resolution_method(graph_db):
                # No confidence column but resolution_method present: pull the REAL
                # method so the BUG-6 categorical gate below can drop name_match
                # scope files. Synthesize a floor-clearing conf only for FACT rows.
                _scope_rows = _sc.execute(
                    f"""SELECT DISTINCT nsrc.file_path, e.resolution_method, 1.0 as conf
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
                         AND LOWER(TRIM(e.resolution_method)) IN ({_DET_METHOD_INLIST})
                       LIMIT 10""",
                    (_top_path, _top_path),
                ).fetchall()
            else:
                # Neither column: cannot prove provenance -> emit NO scope files
                # (correct-or-quiet; do not render unverified name_match scope).
                _scope_rows = []
            _sc.close()
            _sc = None

            # BUG-6: the MEDIUM scope branch (`Related files to inspect`) below is
            # fed by `_distinct_files`, which was built from EVERY scope row — the
            # raw pull is `ORDER BY e.confidence DESC LIMIT 10` with NO method gate
            # and NO confidence floor, so a file reached only via a name_match edge
            # rendered as a related file (the fact-filter protects FACT rows, not
            # this RANKING surface — parity gap with `_high_distinct`, which DOES
            # gate on SCOPE_HIGH_RESOLUTION_METHODS). Gate `_distinct_files` on the
            # canonical FACT set (DETERMINISTIC_RESOLUTION_METHODS, imported) plus
            # the _NAME_MATCH_FLOOR confidence floor, mirroring the high branch.
            # name_match is NEVER in the FACT set, so this strips speculative scope
            # while keeping every structurally-resolved caller file.
            _det_lower = {m.lower() for m in DETERMINISTIC_RESOLUTION_METHODS}

            def _scope_row_is_fact(r) -> bool:
                _m = str(r[1] or "").strip().lower()
                if _m not in _det_lower:
                    return False
                try:
                    return float(r[2]) >= _NAME_MATCH_FLOOR
                except (TypeError, ValueError):
                    return False

            _distinct_files = list(
                dict.fromkeys(r[0] for r in _scope_rows if _scope_row_is_fact(r))
            )
            _high_conf_files = [
                r[0]
                for r in _scope_rows
                if r[1] in SCOPE_HIGH_RESOLUTION_METHODS
                and float(r[2]) >= SCOPE_MIN_EDGE_CONFIDENCE
            ]
            _high_distinct = list(dict.fromkeys(_high_conf_files))

            if len(_high_distinct) >= SCOPE_MIN_CALLER_FILES:
                _scope_files = _high_distinct[:3]
                _scope_confidence = "high"
            elif len(_distinct_files) >= SCOPE_MIN_CALLER_FILES:
                _scope_files = _distinct_files[:3]
                _scope_confidence = "medium"

            log_threshold_use(
                "L1_SCOPE",
                _scope_confidence,
                f"top={_top_path} distinct={len(_distinct_files)} high={len(_high_distinct)}",
            )
        except Exception:
            pass
        finally:
            if _sc is not None:
                _sc.close()

    # Derive scores from `entries` (each FileEntry.score is the same value its
    # top_records row produced) so the render_brief #1-vs-#2 gap calc tracks the
    # SAME order render_brief renders — required after the L1 cross-wire reorder of
    # `entries` above (a positional top_records slice would pair the gap with the
    # pre-reorder order).
    _scores = [float(getattr(e, "score", 0.0)) for e in entries]
    _scope_chains = getattr(_loc, "scope_chains", []) if _loc else []
    # PREPEND the confidence-graded localization header (Agentless hierarchical
    # localize: granularity scales with research-backed structural confidence). When
    # it fires it OWNS the localization steer, so the brief's legacy singular
    # "highest-confidence candidate" line is suppressed (no contradictory steers).
    # `_loc_header`/`_loc_primary` were computed ABOVE (the L1 cross-wire fix), before
    # L1-SCOPE, so entries[0] is already the header's primary — do NOT recompute here.
    _emit_old = _loc_header == ""

    # Issue-SUBJECT anchor symbols for the obligation relevance gate. The curated
    # code identifiers the localizer already extracted from the issue (the same
    # provenance used for file ranking + persisted to gt_issue_anchors.json). They
    # let an obligation about a net-new feature (wasmi `coredump`) or a low-ref
    # truncated function (pest `range`) anchor against the issue's own subject when
    # it is absent from the top-N focus functions. Empty when anchors unavailable
    # → the gate falls back to focus-only (byte-identical to prior behavior).
    _oblig_anchor_syms: set[str] = set()
    if _anchors_obj is not None:
        _oblig_anchor_syms = {
            s
            for s in (
                set(getattr(_anchors_obj, "symbols", set()) or set())
                | set(getattr(_anchors_obj, "code_symbols", set()) or set())
                | set(getattr(_anchors_obj, "unresolved_code_symbols", set()) or set())
            )
            if s
        }

    def _render(body_line_cap: int = _MAX_BODY_LINE_CHARS):
        return render_brief(
            entries,
            scores=_scores,
            scope_files=_scope_files,
            scope_confidence=_scope_confidence,
            scope_chains=_scope_chains,
            issue_text=issue_text,
            graph_db=graph_db,
            emit_confident_line=_emit_old,
            body_line_cap=body_line_cap,
            anchor_symbols=_oblig_anchor_syms,
        )

    _body_cap = _MAX_BODY_LINE_CHARS
    brief_text = _render(_body_cap)
    tok = _count_tokens((_loc_header + "\n" + brief_text) if _loc_header else brief_text)

    # Decouple localization BREADTH from the evidence token budget while rendering.
    # `_loc_files` keeps the full rank-ordered set through detail trimming; after the
    # final bytes are assembled, `.files` is joined back to paths actually visible in
    # candidate blocks. Before the decoupling, trimming prematurely gutted localization
    # to 1-2 files and
    # dropping golds the localizer ranked #0-#5 (proven on the held-out sweep:
    # geopandas-3226 gold @rank0 and sqllineage-557 @rank5 vanished from .files even
    # though the ranker placed them at/near the top; delivered Recall@5 fell to 0.40
    # vs the bare localizer's 0.60 = grep parity). The token budget governs how much
    # per-file evidence the agent reads, NOT which files it is told to consider.
    _loc_files = list(entries)
    # C16: acquisition evidence is projected from the terminal ranked population
    # before token/minimal/re-slot delivery reduction can remove candidates.
    _acquisition_proof = _acquisition_proof_rows(
        top_records,
        witness_by_file=_witness_by_file,
        witness_verified_by_file=_witness_verified_by_file,
    )
    while tok > max_brief_tokens and len(entries) > 1:
        entries = entries[:-1]
        _scores = _scores[: len(entries)]
        brief_text = _render(_body_cap)
        tok = _count_tokens((_loc_header + "\n" + brief_text) if _loc_header else brief_text)

    # D1 — ENFORCE the budget by trimming DETAIL, not the file LIST. The loop above
    # can bottom out at len(entries)==1 while still over budget when a single
    # entry's evidence bodies (a multi-clause Contract, a long Callers/Chain line,
    # the leading graph-map fan-in) sum past the rail. Per CLAUDE.md ("compact,
    # high-precision"; treat token bloat without outcome gain as a regression) and
    # BRIEFING.md §3 (token budget trims DETAIL, never which files the agent is
    # told to consider), progressively tighten the per-body-line cap on the SAME
    # rendered entries until under budget. This counts the FULL brief_text — which
    # already includes the (now-leading) <gt-graph-map> via _with_graph_map — so the
    # graph-map's bytes are inside the rail too. Floored at a readable minimum;
    # rank-neutral (candidate order untouched). Idempotent: a brief already
    # under budget never enters this loop and is byte-identical to before.
    _BODY_CAP_FLOOR = 80
    while tok > max_brief_tokens and _body_cap > _BODY_CAP_FLOOR:
        _body_cap = max(_BODY_CAP_FLOOR, _body_cap - 60)
        brief_text = _render(_body_cap)
        tok = _count_tokens((_loc_header + "\n" + brief_text) if _loc_header else brief_text)

    if _loc_header:
        brief_text = _loc_header + "\n" + brief_text

    # B-30: HARD rail. The two loops above trim breadth (entries) then detail
    # (body cap) but can bottom out — one entry + body cap at floor + fixed
    # header/block overhead — STILL over budget (measured: caps 100/20/1 ->
    # 244/242/242 tokens; the declared cap was never enforced). Enforce it as an
    # inviolable ceiling: drop whole rendered brief blocks lowest-priority first,
    # record what was suppressed, and truncate the residue only as a last resort.
    # Rank/localizer untouched (`_loc_files` was captured before the loops);
    # this trims DOSE, never which files the agent is told to consider. Idempotent:
    # a brief already within budget is unchanged (byte-identical) and _budget_suppressed
    # stays empty.
    _budget_suppressed: list[str] = []
    if _count_tokens(brief_text) > max_brief_tokens:
        brief_text, _budget_suppressed = _enforce_token_rail(brief_text, max_brief_tokens)

    # SM-6 (B): step-0 brief reduction — GT_BRIEF_MINIMAL (DEFAULT-OFF, byte-identical).
    # Applied AFTER the token rail so the reduced text drives token_estimate + block
    # receipts below. Retires <gt-graph-map>/<gt-localization>/contract narration, keeps
    # obligations + minimal 'which file' orientation (localization rides reactive
    # def_partition). BAKED: dormant until the SM-8 rebake sets the flag at generation.
    _control_participation: list[dict] = []
    # C15 — how many candidates were model-visible BEFORE the reduction ran. Used below to
    # PROVE (not assume from a flag) that the reduction is what emptied the delivered set,
    # which is the difference between NOT_EVALUABLE and an honest 0.
    _delivered_pre_reduction = 0
    if _brief_minimal_on():
        _before_minimal = brief_text
        _delivered_pre_reduction = len(
            _model_visible_localization_entries(_before_minimal, _loc_files)
        )
        brief_text = _reduce_brief_to_minimal(brief_text)
        _control_participation = _brief_minimal_participation(_before_minimal, brief_text)

    # --- L1 signal-provenance counts (observability; no ranking effect) ---
    # Count over the DELIVERED candidate set (final-byte joined; identical to `.files`).
    # Align each delivered entry to its top_records dict (carrying run_v74
    # `components`) by path so semantic/structural/fts5 contributions are read
    # from the ACTUAL signals computed during localization, not re-derived.
    _delivered = _model_visible_localization_entries(brief_text, _loc_files)
    _rec_by_path: dict[str, dict] = {}
    for _r in top_records:
        _rp = str(_r.get("path", ""))
        if _rp and _rp not in _rec_by_path:
            _rec_by_path[_rp] = _r
    _aligned_records = [_rec_by_path.get(e.path, {}) for e in _delivered]
    if os.environ.get("GT_DEBUG_L1") == "1":
        import sys as _sys_dbg

        _comp = [
            (
                str(_r.get("path", ""))[-44:],
                {k: round(float(v), 3) for k, v in (_r.get("components") or {}).items()},
            )
            for _r in top_records[:5]
        ]
        _join = [
            (
                getattr(e, "path", "")[-44:],
                "MATCH" if getattr(e, "path", "") in _rec_by_path else "MISS",
            )
            for e in _delivered[:8]
        ]
        print(f"[GT_DEBUG_L1] ranked_full_components={_comp}", file=_sys_dbg.stderr, flush=True)
        print(f"[GT_DEBUG_L1] delivered_vs_record_join={_join}", file=_sys_dbg.stderr, flush=True)
        print(
            f"[GT_DEBUG_L1] n_top_records={len(top_records)} n_delivered={len(_delivered)} embedder={os.environ.get('GT_FORCE_ONNX_EMBEDDER', '?')}",
            file=_sys_dbg.stderr,
            flush=True,
        )
    try:
        _ge, _sem_c, _struct_c, _fts5_c = _l1_signal_counts(graph_db, _delivered, _aligned_records)
    except Exception:
        _ge = _sem_c = _struct_c = _fts5_c = 0

    # C15 — the ACQUISITION fact, over the RANKED set, delivery-independent. This is the
    # number that answers "did the legs find anything"; the four above answer "what reached
    # the model". They are computed from the same signals and differ ONLY in population, so
    # acquired >= delivered always holds and the pair is itself the re-slot's delivery gap.
    try:
        _acq_ge, _acq_sem, _acq_struct, _acq_fts5 = _l1_acquisition_counts(
            graph_db,
            top_records,
            witness_by_file=_witness_by_file,
            localizer_confidence_by_file=_loc_conf_by_file,
        )
    except Exception:
        _acq_ge = _acq_sem = _acq_struct = _acq_fts5 = 0

    # NOT_EVALUABLE iff the reduction is what emptied delivery — candidates WERE visible
    # before it ran and none are now. Anything else (a genuine empty ranking, or delivered
    # candidates that simply carry no signal) reports an honest integer.
    _reduction_emptied_delivery = (not _delivered) and _delivered_pre_reduction > 0
    if _reduction_emptied_delivery:
        _d_ge = _d_sem = _d_struct = _d_fts5 = _d_count = None
    else:
        _d_ge, _d_sem, _d_struct, _d_fts5 = _ge, _sem_c, _struct_c, _fts5_c
        _d_count = len(_delivered)

    _conf_tier = _tier_from_loc_header(_loc_header)

    # --- Embedder-CONSUMPTION metrics over the RENDERED candidates ---
    # sem_components reads components['sem'] from the SAME per-entry top_records
    # alignment that _l1_signal_counts uses, so semantic_signal_count ==
    # sum(1 for s in sem_components if s > 0) by construction (auditable). The
    # effective W_SEM and the relative sem cap come from run_v74 (the single point
    # where every zeroing branch converges). rendered_candidate_count == len(files).
    _sem_components = [
        float((_r.get("components", {}) if isinstance(_r, dict) else {}).get("sem", 0.0) or 0.0)
        for _r in _aligned_records
    ]
    _eff_w_sem = float(getattr(v74, "effective_w_sem", 0.0) or 0.0)
    _k_sem_top = int(getattr(v74, "k_sem_top_effective", 0) or 0)
    _terminal_body_paths = {
        _gl_normalize(str(_p))
        for _p in (
            set(_semantic_anchor_paths)
            | set(getattr(_loc, "content_leg_paths", frozenset()) or ())
            | set(getattr(_loc, "semantic_body_paths", frozenset()) or ())
        )
    }
    _localization_proof: list[dict[str, object]] = []
    for _i, (_e, _r) in enumerate(zip(_delivered, _aligned_records), start=1):
        _comps_raw = (_r.get("components", {}) if isinstance(_r, dict) else {}) or {}
        _components: dict[str, float] = {}
        for _ck, _cv in _comps_raw.items():
            try:
                _components[str(_ck)] = float(_cv or 0.0)
            except Exception:
                continue
        _proof_path = str(getattr(_e, "path", "") or "")
        _components = _terminal_acquisition_components(
            _components,
            _proof_path,
            body_paths=_terminal_body_paths,
        )
        # ACQ SOURCE-5 (cochange_history) + INFLUENCE-5 (cochange_prior): the
        # candidate's mined co-change neighbours (its "Also changes: …" line) are
        # a first-class SUPPORT signal, but were never stamped onto the emitted
        # proof — so both features sat dark on every ordinary localization
        # candidate. Stamp components["cochange"] and a self-sealed primary-path
        # witness here (bridge candidates keep their own sealed witness untouched).
        _bridge_cochange_ev = _r.get("cochange_evidence") if isinstance(_r, dict) else None
        _cochange_ev, _cc_for_proof = _primary_cochange_support(
            candidate_path=_proof_path,
            entry_co_changes=getattr(_e, "co_changes", None),
            components=_components,
            bridge_evidence=_bridge_cochange_ev,
        )
        _localization_proof.append(
            {
                "candidate_id": _localization_candidate_id(_proof_path),
                "rank": _i,
                "path": _proof_path,
                "score": float(getattr(_e, "score", 0.0) or 0.0),
                "function_names": list(getattr(_e, "function_names", []) or [])[:8],
                "witness": getattr(_e, "witness", "") or "",
                "witness_verified": bool(getattr(_e, "witness_verified", False)),
                "relevance_grade": str(getattr(_e, "relevance_grade", "INFO") or "INFO"),
                "localizer_confidence": float(getattr(_e, "localizer_confidence", 0.0) or 0.0),
                "anchor_prox": float(getattr(_e, "anchor_prox", 0.0) or 0.0),
                "components": _components,
                "semantic_component": float(_components.get("sem", 0.0) or 0.0),
                "lex_component": float(_components.get("lex", 0.0) or 0.0),
                "reach_component": float(_components.get("reach", 0.0) or 0.0),
                "path_component": float(_components.get("path", 0.0) or 0.0),
                "witness_component": float(_components.get("witness", 0.0) or 0.0),
                "entered_via": str(_r.get("entered_via", "") if isinstance(_r, dict) else ""),
                "cochange_evidence": (
                    _cochange_ev if _components.get("cochange", 0.0) > 0.0 else None
                ),
                "acquisition_sources": _candidate_acquisition_sources(
                    graph_db,
                    repo_root,
                    str(getattr(_e, "path", "") or ""),
                    _resolution_methods_by_file.get(
                        str(getattr(_e, "path", "") or ""),
                        _resolution_methods_by_file.get(
                            str(getattr(_e, "path", "") or "")
                            .replace("\\", "/")
                            .lstrip("./")
                            .lstrip("/"),
                            frozenset(),
                        ),
                    ),
                ),
            }
        )
        # Data-lineage pointer for the primary-path co-change witness only. Bridge
        # candidates (``_cc_for_proof is None``) keep their exact prior proof shape.
        if _cc_for_proof is not None:
            _localization_proof[-1]["co_changes"] = _cc_for_proof

    # --- AUDIT snapshots (READ-ONLY; gated by GT_AUDIT_DIR; no ranking effect) ---
    # Persists the absorption lineage: for each rendered entry, the LIVE (exact-path)
    # semantic alignment that the product uses AND a CONSISTENT-id alignment over a
    # normalized path index of the SAME top_records. Where live_sem==0 but
    # consistent_sem>0, the score existed upstream and the exact-path join dropped it
    # (the conan seam). When GT_AUDIT_DIR is unset this whole block is skipped.
    _audit_dir = os.environ.get("GT_AUDIT_DIR")
    if _audit_dir:
        try:
            import json as _json_a

            def _norm_p(p):
                return str(p or "").replace("\\", "/").lstrip("./").lstrip("/")

            _norm_rec: dict[str, dict] = {}
            for _r in top_records:
                _np = _norm_p(_r.get("path", ""))
                if _np and _np not in _norm_rec:
                    _norm_rec[_np] = _r
            _rendered_snap = []
            for _i, _e in enumerate(_delivered):
                _np = _norm_p(getattr(_e, "path", ""))
                _live_sem = float(_sem_components[_i]) if _i < len(_sem_components) else 0.0
                _cons_sem = float(
                    (_norm_rec.get(_np, {}).get("components", {}) or {}).get("sem", 0.0) or 0.0
                )
                _routes = list(getattr(_e, "routes", []) or [])
                _ev = getattr(_e, "entered_via", "") or ""
                if _ev and _ev not in _routes:
                    _routes.append(_ev)
                _rendered_snap.append(
                    {
                        "candidate_id": f"{_np}:{getattr(_e, 'start_line', 0) or 0}:"
                        f"{getattr(_e, 'symbol', '') or os.path.basename(_np)}",
                        "path": getattr(_e, "path", ""),
                        "live_join": "MATCH" if getattr(_e, "path", "") in _rec_by_path else "MISS",
                        "live_sem": _live_sem,
                        "consistent_sem": _cons_sem,
                        "routes": _routes,
                    }
                )
            _sem_snap = [
                {
                    "candidate_id": f"{_norm_p(_r.get('path', ''))}:0:"
                    f"{os.path.basename(_norm_p(_r.get('path', '')))}",
                    "path": _r.get("path", ""),
                    "sem": float((_r.get("components", {}) or {}).get("sem", 0.0) or 0.0),
                    "components": _r.get("components", {}),
                }
                for _r in top_records
            ]
            os.makedirs(_audit_dir, exist_ok=True)
            with open(
                os.path.join(_audit_dir, "10_candidates_rendered.json"), "w", encoding="utf-8"
            ) as _f:
                _json_a.dump(_rendered_snap, _f, indent=2, default=str)
            with open(
                os.path.join(_audit_dir, "08_candidates_semantic_scored.json"),
                "w",
                encoding="utf-8",
            ) as _f:
                _json_a.dump(_sem_snap, _f, indent=2, default=str)
        except Exception:
            pass  # audit snapshot must never affect the brief

    # B-6: per-block delivery receipts (sidecar metadata; default OFF, additive).
    # Computed from the FINAL brief_text so spans/hashes match exactly what the agent
    # sees. A PURE READ — brief_text is NOT modified, so the delivered brief bytes are
    # byte-identical whether or not receipts are populated.
    _rendered_candidate_ids = [
        _localization_candidate_id(str(getattr(_e, "path", "") or "")) for _e in _delivered
    ]
    _block_receipts = (
        _brief_block_receipts(
            brief_text,
            localization_candidate_ids=_rendered_candidate_ids,
        )
        if _block_receipts_on()
        else []
    )
    # B-ACQ: seal each rendered candidate's ACQ source contribution to its exact
    # delivered block bytes. PURE metadata mutation on _localization_proof (already a
    # sidecar); brief_text is untouched, so the delivered brief stays byte-identical.
    if _block_receipts:
        _attest_source_contributions(_localization_proof, _block_receipts)
    # Cluster-2b: bind the delivered obligations block to its build-time extraction record
    # (sidecar; brief_text untouched, byte-identical). Empty when there is no obligations
    # block or no persisted extraction (fail-closed -> obligations attestation UNMEASURED).
    _obligations_record = (
        _build_obligations_record(brief_text, _block_receipts, issue_text)
        if _block_receipts
        else {}
    )
    _control_participation.extend(
        _terminal_pretask_mediator_participation(
            brief_text,
            _block_receipts,
            budget_suppressed=_budget_suppressed,
            content_paths=set(getattr(_loc, "content_leg_paths", frozenset()) or ()),
            content_decision=str(getattr(_loc, "content_leg_decision", "NO_EFFECT") or "NO_EFFECT"),
            content_reason=str(
                getattr(_loc, "content_leg_reason", "no_content_candidate")
                or "no_content_candidate"
            ),
            semantic_anchor_paths=_semantic_anchor_paths,
            semantic_localizer_paths=set(getattr(_loc, "semantic_body_paths", frozenset()) or ()),
        )
    )

    result = V1RBriefResult(
        files=_delivered,
        brief_text=brief_text,
        token_estimate=_count_tokens(brief_text),
        v74_result=v74,
        graph_edge_count=_ge,
        semantic_signal_count=_sem_c,
        structural_signal_count=_struct_c,
        fts5_signal_count=_fts5_c,
        confidence_tier=_conf_tier,
        acquired_graph_edge_count=_acq_ge,
        acquired_semantic_signal_count=_acq_sem,
        acquired_structural_signal_count=_acq_struct,
        acquired_fts5_signal_count=_acq_fts5,
        acquisition_proof=_acquisition_proof,
        delivered_graph_edge_count=_d_ge,
        delivered_semantic_signal_count=_d_sem,
        delivered_structural_signal_count=_d_struct,
        delivered_fts5_signal_count=_d_fts5,
        delivered_candidate_count=_d_count,
        effective_w_sem=_eff_w_sem,
        rendered_candidate_count=len(_delivered),
        k_sem_top=_k_sem_top,
        sem_components=_sem_components,
        localization_proof=_localization_proof,
        budget_suppressed=_budget_suppressed,
        block_receipts=_block_receipts,
        control_participation=_control_participation,
        obligations_record=_obligations_record,
        tokenizer_used=_tokenizer_kind(),  # Brief-F9: which token counter ran
    )

    # Structured telemetry: emit L1 candidates as JSON for wrapper to parse
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        try:
            import json as _json

            l1_items = []
            for entry in _delivered:
                # confidence_score now reflects the GRAPH-TRAVERSAL witness
                # strength (graph_localizer) when this file was witnessed, falling
                # back to the v74 lexical score otherwise. This is the fix for the
                # gt_run_summary l1_confidence_score=0.0 symptom: a witnessed top
                # candidate (importer.py) now reports its real structural
                # confidence instead of the lexical 0.0.
                _conf = (
                    entry.localizer_confidence if entry.localizer_confidence > 0 else entry.score
                )
                _reason = (
                    f"graph_witness={entry.witness}"
                    if entry.witness
                    else f"V1R score={entry.score:.3f}"
                )
                l1_items.append(
                    {
                        "kind": "l1_candidate",
                        "file_path": entry.path,
                        "confidence": _conf,
                        "confidence_score": _conf,
                        "witnessed": bool(entry.witness),
                        "witness_verified": entry.witness_verified,
                        "relevance_grade": entry.relevance_grade,
                        "witness": entry.witness,
                        "source": "graph_traversal" if entry.witness else "graph_db",
                        "reason": _reason,
                        "text": ", ".join(entry.functions[:3]) if entry.functions else "",
                    }
                )

            # RESOLVED call-edge witnesses -> structured evidence items in the kinds
            # the telemetry reader consumes (telemetry/metrics._compute_l1_metrics):
            #   - kind="l1_graph_edge", source="CALLS"  -> l1_candidates_with_call_edge_count
            #   - kind="l1_confirming_edge"             -> l1_primary_witness_file/symbol/type
            # The audited run reported l1_candidates_with_call_edge_count=0 +
            # l1_primary_witness_file='N/A — no confirming edge' even though the
            # resolution was on disk: the L1 structured payload never surfaced the
            # deterministic caller/callee edges as confirming evidence. These items
            # close that gap. Deterministic-provenance + stdlib-shadow-guarded
            # (_resolved_witnesses_for_file); a name_match is NEVER emitted here.
            _primary_emitted = False
            for entry in _delivered:
                try:
                    _wits = _resolved_witnesses_for_file(graph_db, entry.path, repo_root)
                except Exception:
                    _wits = []
                for _w in _wits:
                    l1_items.append(
                        {
                            "kind": "l1_graph_edge",
                            "file_path": entry.path,  # the CANDIDATE this edge confirms
                            "source": "CALLS",
                            "direction": _w.get("direction"),  # caller | callee
                            "symbol": _w.get("symbol", ""),
                            "edge_file": _w.get("file_path", ""),
                            "line": _w.get("line", 0),
                            "confidence": 1.0,  # deterministic edge = fact
                            "reason": "resolved CALLS edge (deterministic provenance)",
                        }
                    )
                # The PRIMARY confirming witness for this candidate is its first
                # resolved CALLER (a caller proves the candidate's symbol is a real,
                # used target — the strongest confirmation). Emit ONE per task: the
                # first candidate that carries a resolved caller.
                if not _primary_emitted:
                    _caller = next((w for w in _wits if w.get("direction") == "caller"), None)
                    if _caller is not None:
                        l1_items.append(
                            {
                                "kind": "l1_confirming_edge",
                                "file_path": entry.path,
                                "symbol": _caller.get("symbol", ""),
                                "source": "CALLS",
                                "edge_file": _caller.get("file_path", ""),
                                "line": _caller.get("line", 0),
                                "confidence": 1.0,
                                "reason": "resolved cross-file caller (deterministic)",
                            }
                        )
                        _primary_emitted = True

            _call_edge_count = sum(
                1
                for it in l1_items
                if it.get("kind") == "l1_graph_edge" and it.get("source") == "CALLS"
            )
            _confirming = next(
                (it.get("file_path") for it in l1_items if it.get("kind") == "l1_confirming_edge"),
                None,
            )
            structured = {
                "candidates": l1_items,
                "candidate_count": len(_delivered),
                # Provenance counts (same definitions as the V1RBriefResult fields):
                # a candidate counts toward a signal iff that signal contributed a
                # nonzero score / a real graph edge exists. These let a fail-closed
                # gate prove the brief is multi-signal, not lexical-only/hollow.
                "graph_edge_count": _ge,
                "semantic_signal_count": _sem_c,
                "structural_signal_count": _struct_c,
                "fts5_signal_count": _fts5_c,
                "confidence_tier": _conf_tier,
                # C15: the ACQUISITION fact under a name that cannot be misread as
                # delivery, plus delivery under a name that cannot be misread as
                # acquisition. "NOT_EVALUABLE" (never 0) when the re-slot reduction is
                # what emptied the delivered set.
                "acquired_graph_edge_count": _acq_ge,
                "acquired_semantic_signal_count": _acq_sem,
                "acquired_structural_signal_count": _acq_struct,
                "acquired_fts5_signal_count": _acq_fts5,
                "acquisition_proof": _acquisition_proof,
                "delivered_graph_edge_count": _NOT_EVALUABLE if _d_ge is None else _d_ge,
                "delivered_semantic_signal_count": _NOT_EVALUABLE if _d_sem is None else _d_sem,
                "delivered_structural_signal_count": _NOT_EVALUABLE
                if _d_struct is None
                else _d_struct,
                "delivered_fts5_signal_count": _NOT_EVALUABLE if _d_fts5 is None else _d_fts5,
                "delivered_candidate_count": _NOT_EVALUABLE if _d_count is None else _d_count,
                # Embedder-CONSUMPTION metrics (same definitions as the
                # V1RBriefResult fields): effective_w_sem>0 with
                # semantic_signal_count==0 / all-zero sem_components ==
                # present-but-unconsumed embedder.
                "effective_w_sem": _eff_w_sem,
                "rendered_candidate_count": len(_delivered),
                "k_sem_top": _k_sem_top,
                "sem_components": _sem_components,
                # legacy proxy (callees present) kept for back-compat readers
                "neighbor_present_count": sum(1 for e in _delivered if e.callees),
                "signature_count": sum(1 for e in _delivered if e.functions),
                "witnessed_count": sum(1 for e in _delivered if e.witness),
                "verified_witness_count": sum(1 for e in _delivered if e.witness_verified),
                # Resolved deterministic call-edge witnesses surfaced at iter-0 — the
                # signal the audited run reported as 0 / 'N/A'.
                "l1_candidates_with_call_edge_count": _call_edge_count,
                "l1_primary_witness_file": _confirming or "N/A — no confirming edge",
                "warnings": [],
                "abstain_reason": None,
            }
            if not _delivered:
                structured["abstain_reason"] = "no_candidates"
            with open("/tmp/gt_l1_structured.json", "w") as _f:
                _json.dump(structured, _f)
        except Exception:
            pass

    return result
