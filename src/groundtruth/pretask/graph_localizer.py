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
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _NAME_MATCH_FLOOR,
    _has_columns,
    _open_ro,
)
from groundtruth.index.repo_scope import RepoScope, for_read
from groundtruth.confidence import dynamic_cutoff, is_seed_pollutant


def _repo_frag(scope: "RepoScope | None", alias: str = "") -> tuple[str, tuple]:
    """SM-9a: (SQL fragment, params) scoping a ``nodes`` read to the active repo, or
    ("", ()) when there is no scope (single-repo / legacy / no scope passed). Central
    so every seed producer scopes identically and single-repo stays byte-identical."""
    return scope.node_filter(alias) if scope is not None else ("", ())

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
# Subject bonus: a file that DEFINES the issue's SUBJECT symbol (the broken
# function, named earliest in the issue) is the likely EDIT TARGET. This must
# dominate the raw centrality (degree) prior — otherwise a high-in-degree CALLEE
# (db.py::set_parse) out-ranks the CALLER the issue is actually about
# (importer.py::set_fields), which is the RepoGraph/SWERank hub-bias failure.
# Set above W_DEGREE so the subject always beats a pure centrality tie, but below
# W_LEX/W_WITNESS so it never overturns a stronger structural/lexical signal.
W_SUBJECT = 0.15
# Inter-candidate connectivity: how many OTHER candidate files this file has
# verified edges to/from. The edit target sits at the structural crossroads
# of the issue-relevant code. This is the graph's value-add over grep —
# grep finds files with keywords, the graph finds files at the CENTER of
# the keyword-relevant code cluster. Weight above W_DEGREE (it's a stronger
# structural signal) but below W_WITNESS (direct edge > neighborhood count).
# NOTE (doc correction): Personalized PageRank is NOT implemented — there is no
# W_PPR constant and no PPR computation in this module. Multi-hop structural
# proximity is captured instead by the path-decay reach signal (`_path_decay_scores`,
# Dijkstra over verified edges) fused via RRF (GT_LOC_FUSION_V2). This paragraph is
# retained only to record that PPR was considered and deliberately NOT built (reach
# already over-promotes hubs; the architecture subordinates structural reach on
# purpose — adding diffused PPR mass would worsen the hub-bias, not help).

# Witness strength by edge provenance (correct-or-quiet).
# EDGE witnesses (CALLS/IMPORTS): structural evidence — the file is connected
# to an issue symbol via a real code dependency. This is the GRAPH's value-add.
# DEFINES witnesses: lexical evidence — the file merely defines a symbol whose
# name appears in the issue. This is what grep/BM25 already gives you; it
# carries no structural depth. Must score BELOW edge witnesses so the graph
# actually adds ranking value over grep (LIPI diagnosis: when both scored 1.0,
# the localizer degenerated to expensive BM25 — 23% hit@1).
_WITNESS_VERIFIED = 1.0     # verified EDGE witness (CALLS/IMPORTS)
_WITNESS_DEFINES = 0.55     # DEFINES witness — above name_match but below edges
_WITNESS_NAMEMATCH = 0.45   # unverified name_match edge
# Hop decay is applied inline in Witness.strength() as 1/(1+hop).
#
# DEFINES vs verified-EDGE strength inversion guard (#57). A DEFINES witness is
# ALWAYS hop-0, so its raw decay factor is 1/(1+0)=1.0 and `0.55*conf` could BEAT
# a verified 1-hop CALLS edge (1.0*conf*0.5=0.50) on the raw scalar that feeds
# Candidate.confidence (the [VERIFIED] render gate) and the W_WITNESS score term.
# That is a lexical signal out-ranking a structural one — the exact inversion this
# module exists to kill. The sort already tiers correctly (_witness_tier), but the
# SCALAR did not. Fix: the DEFINES scalar is CAPPED strictly below the minimum
# possible verified-EDGE strength. The deepest BFS hop is bounded by
# _dynamic_max_hop (ceiling 3) and localize(max_hop=3), so a verified edge's
# weakest decay factor is 1/(1+_MAX_DECAY_HOP). _WITNESS_DEFINES_CEIL sits just
# under _WITNESS_VERIFIED*that-decay, so any verified edge (hop 1..max) outranks
# any DEFINES regardless of confidence. Generalized (no repo/task logic); the cap
# is derived from the module's own hop bound, not tuned.
_MAX_DECAY_HOP = 3
_WITNESS_DEFINES_CEIL = _WITNESS_VERIFIED * (1.0 / (1.0 + _MAX_DECAY_HOP)) * 0.95

# Hub guard for the degree prior — tanh saturates so a 500-caller hub doesn't
# linearly dominate a 5-caller specific module. Matches hub_penalty.HUB_SCALE.
_HUB_SCALE = 50.0

_MIN_ANCHOR_LEN = 3

# Degree edge-type scope (D5 fix, 2026-06-13). fan_out/fan_in and the file-level
# in-degree centrality prior MUST count ONLY structural/navigation edges — the
# edges that represent how an agent actually traverses the code (CALLS / CONTAINS /
# EXTENDS / IMPLEMENTS / IMPORTS / RE_EXPORTS / COMPOSES / ...). When relationship
# promotion ships (depth), it mints PROMOTED relationship edge types and/or stamps
# `resolution_method LIKE 'promote_%'`. Those inflate node degree (~1.88x measured;
# 868 nodes gain fan_out purely from promoted edges), defeating the SLOC<=4/
# fan_out==0 trivial-validator and regressing ranking (a trivial validator suddenly
# looks structurally connected). The trivial-validator and the degree prior must
# EXCLUDE the promoted relationship edges so degree reflects navigation only.
#
# BLACKLIST, not whitelist: live graphs already carry legitimate structural types
# beyond the canonical five (measured: RE_EXPORTS for JS/TS re-exports, COMPOSES for
# struct/class composition). A whitelist of {CALLS,CONTAINS,EXTENDS,IMPLEMENTS,
# IMPORTS} would WRONGLY drop those and change degree on current graphs. Excluding
# the promoted RELATIONSHIP types (+ promote_% provenance) is the only construction
# that is a strict no-op today and a fix once promotion ships.
#
# DEGRADE-SAFE: on current live graphs (no promotion pass yet) NO edge has a promoted
# type and NO edge has `resolution_method LIKE 'promote_%'` (measured: 0 across all
# scanned live graphs), so this predicate matches every existing edge — identical
# counts to the unfiltered query. It becomes load-bearing only once promoted edges
# exist.
_PROMOTED_EDGE_TYPES: tuple[str, ...] = (
    "DATA_FLOW", "READS", "WRITES", "RAISES", "CO_SERIALIZES", "PRECEDES",
)
# SQL predicate (alias-parameterized) that EXCLUDES promoted relationship edges and
# any promoted-provenance edge from a degree count. `{a}` is the edge-table alias.
def _degree_edge_filter(a: str) -> str:
    types_not_in = ", ".join("'" + t + "'" for t in _PROMOTED_EDGE_TYPES)
    return (
        f"{a}.type NOT IN ({types_not_in}) "
        f"AND ({a}.resolution_method IS NULL "
        f"OR {a}.resolution_method NOT LIKE 'promote_%')"
    )


# Shared FTS5 DDL — single source of truth so schema changes don't diverge
# across the Go indexer, Python fallback, and preflight script.
# STANDALONE FTS5 (NOT content='nodes' external-content): the query-path fallback
# builds nodes_fts in a PRIVATE in-memory db with graph.db ATTACHed read-only, so the
# read-only query path NEVER writes graph.db (was: CREATE/INSERT INTO the source db ->
# mutated a read-only artifact + changed its sha256 + could race a concurrent reader).
# An external-content table binds to a `nodes` table in its OWN db; here `nodes` lives in
# the attached `src`, so we store the column VALUES to keep the index self-contained.
_FTS5_CREATE = """
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, qualified_name, signature, file_path
)"""
_FTS5_POPULATE = """
INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path)
SELECT id, name, COALESCE(qualified_name, ''), COALESCE(signature, ''), file_path
FROM src.nodes WHERE is_test = 0
"""

# Composite rerank weights (Phase 1: FTS5 + path decay added).
# The formula is:
#   Score(f) = W_BM25 * BM25_norm + W_PATH_DECAY * PathDecay_norm
#            + W_WITNESS * witness_norm + W_SUBJECT * subject_norm
#            + W_LEX * lex_norm + W_DEGREE * deg_norm - W_GEN * gen_flag
#
# BM25 and PathDecay are NEW signals that ADD to the existing witness/lex/degree
# scoring. They do not replace any existing signal — backward compatible.
W_BM25 = 0.35
# W_PATH_DECAY = path-decay reach in localize()'s composite (feeds entries[0], the
# agent's #1). BRIEFING §3 lever #2: 0.30→0.15 "cut reach-family dominance" (BLUiR
# ASE 2013). This is the @1 lever (the run_v74 RRF reach-drop only moves the candidate
# window, not the headline — integration audit a728d099c70ce7213). Env-tunable so the
# OSS-60 falsifier can A/B it one variable at a time; default 0.30 = unchanged.
W_PATH_DECAY = float(os.environ.get("GT_LOC_W_PATH_DECAY", "0.30"))

# GREP-FLOOR (Phase 4) — placement of depth-injected (grep-MISSED) candidates
# relative to the grep-recalled floor. The human's call; default conservative.
#   "strictly_below_floor"        — injected candidates sit BENEATH all grep-recalled
#                                    files, no interleaving (default, precision-safe).
#   "interleave_short_deterministic" — allow <=1-hop deterministic injections to
#                                    interleave into the floor (recall-leaning; tune
#                                    on the 5-lang set AFTER the human sees numbers).
INJECTION_PLACEMENT = os.environ.get("GT_INJECTION_PLACEMENT", "strictly_below_floor")

# ── LOCALIZER FUSION V2 (Reciprocal-Rank-Fusion re-architecture) ──────────────
# Default OFF. When OFF, NONE of the V2 code paths are entered and the emitted
# ranking is byte-identical to the magnitude composite (the mandatory diff gate).
#
# Rationale (Cormack et al., SIGIR 2009; k=60): the magnitude composite at the
# score body AVERAGES zero-valued companion terms for file classes that
# structurally can't score on an axis (config files, binary entrypoints,
# proc-macro crates), drowning a surface that already maxes at gold (path=1.0 or
# lex=1.0). RRF is rank-based and dilution-immune: a list that ranks gold #1
# contributes 1/(k+1) regardless of the other lists' scores. It also needs no
# score normalization and is robust when any one list is weak.
#
# Two genuinely independent surfaces are added (the only ones not keyed on
# nodes.name, per CLAUDE.md Lim#6): L5 file-CONTENT IDF-coverage (on-disk body
# tokens) and L6 path-IDF coverage (file_path tokens). On behavior-described
# issues (`nl_gap`, via the already-shipped _classify_issue_lexicality) the name
# lists L1/L2/L4 are demoted by widening their RRF-k (~10x smaller contribution)
# so the independents LEAD. SPLADE/dense are explicitly OUT of scope (add a model;
# overfit risk). The Go-indexer body-FTS (true BM25F) is Phase 2 (needs REBUILD).
GT_LOC_FUSION_V2 = os.environ.get("GT_LOC_FUSION_V2", "1") != "0"          # BAKED default ON — independent-surface RRF fusion. Proven generalizing 2026-06-29: OSS-60 +2 @1/+2 in8 AND held-out +1, 0 per-lang regression (vs every prior approach that overfit). Revert: GT_LOC_FUSION_V2=0.
GT_LOC_BEHAVIOR_LEAD = os.environ.get("GT_LOC_BEHAVIOR_LEAD", "0") != "0"  # BAKED default OFF — the behavior-gate SUPPRESSED the held-out lift (fuse_full held-out +0 vs fuse_nolead +1); plain RRF+independents generalizes better.
GT_LOC_RRF_K = int(os.environ.get("GT_LOC_RRF_K", "60"))                    # Cormack default
GT_LOC_RRF_K_DEMOTE = int(os.environ.get("GT_LOC_RRF_K_DEMOTE", "600"))     # name-list k on nl_gap
GT_LOC_CONTENT_MAXFILES = int(os.environ.get("GT_LOC_CONTENT_MAXFILES", "200"))  # bound on-disk reads
GT_LOC_PRF = os.environ.get("GT_LOC_PRF", "0") != "0"                       # optional RM3, default OFF
# Ablation control (Arm 3): RRF re-fusion of the EXISTING name-keyed surfaces
# only — excludes the two new independent lists L5/L6. Default OFF (no effect
# unless fusion on). Separates the magnitude-dilution win from the independent-
# surface win in the A/B (see arm definitions).
GT_LOC_FUSION_EXCLUDE_INDEP = os.environ.get("GT_LOC_FUSION_EXCLUDE_INDEP", "0") != "0"

# Reach edge-type scope — trickle-down of the indexer depth fix (RE_EXPORTS/COMPOSES,
# 2026-06-29). The path-decay reach (the @1 ranking lever, W_PATH_DECAY) traverses
# CALLS/IMPORTS by default, mirroring the CALLS-pure transitive closure. With
# GT_LOC_REACH_DEPTH=1 it ALSO follows the structural depth edges (RE_EXPORTS
# barrel->source, COMPOSES class->field-type, EXTENDS/IMPLEMENTS) so the newly-emitted
# depth edges feed @1.
# DEFAULT OFF — KEEP OFF (measured harmful, 2026-06-29). A/B on the depth-fixed digest:
# OSS-60 @1 flat (+0) / in8 -1 (rust regressed). Root cause (rnd_rust_regex_1308 miss_diag):
# depth edges give non-gold files nonzero reach (regex has ~137 IMPLEMENTS + many COMPOSES =
# hubs), and a lexically-perfect gold with reach=0.0 gets outranked by depth-reach-boosted
# hubs -> falls out of delivered top-8. This is BRIEFING #3 (reach over-promotes hubs;
# Rust worst-hit, traits implemented by many structs). The depth edges trickle-down via the
# MAP + degree + brief (where they belong), NOT the @1 reach. Do NOT enable without a
# hub-gated redesign that proves a held-out lift with 0 per-lang regression.
GT_LOC_REACH_DEPTH = os.environ.get("GT_LOC_REACH_DEPTH", "0") != "0"
_REACH_EDGE_TYPES = ("CALLS", "IMPORTS")
if GT_LOC_REACH_DEPTH:
    _REACH_EDGE_TYPES = ("CALLS", "IMPORTS", "RE_EXPORTS", "COMPOSES", "EXTENDS", "IMPLEMENTS")
_REACH_EDGE_TYPES_SQL = ", ".join("'" + _t + "'" for _t in _REACH_EDGE_TYPES)


def _is_test_block_name(sym: str) -> bool:
    """Test-framework block names that carry test DESCRIPTIONS, not code symbols.
    Mocha `it('should remove...')`, Jest `test('...')`, describe blocks.
    The indexer stores these as node names; rendering them as witnesses leaks
    test descriptions into the brief (LEAKAGE). Defense-in-depth: the BFS
    already filters is_test=0 on neighbors, but this catches any that slip
    through (e.g. from seed-minting or cross-hop provenance)."""
    s = (sym or "").strip()
    if (s.startswith("it:") or s.startswith("it ") or
            s.startswith("describe:") or s.startswith("describe ") or
            s.startswith("test(") or s.startswith("test ") or
            " should " in s):
        return True
    # E2 (Fable 2026-07-05): also catch pytest/unittest test SYMBOL names — test_foo (^test_),
    # foo_test (_test$), TestClass (^Test[A-Z]) — that a stale graph (is_test flag unset) can
    # slip into a witness field. Correct-or-quiet: suppressing a witness never invents one.
    if s.startswith("test_") or s.endswith("_test"):
        return True
    return s.startswith("Test") and len(s) > 4 and s[4].isupper()


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


from groundtruth.delivery.path_policy import is_generated as _is_generated
from groundtruth.delivery.path_policy import is_test_path as _is_test_path_pp
from groundtruth.delivery.path_policy import is_vendored_path as _is_vendored_path_pp


# Test-file detection is delegated to the CANONICAL segment-based predicate
# (delivery.path_policy.is_test_path, imported above as _is_test_path_pp) so the localizer's
# non-source demote and the brief-render test filter agree. A local substring predicate lived here
# and drifted from path_policy (it matched "testing/" as a substring, which P11 removed as
# production-ambiguous) — B-Finding2 (Fable LIPI) removed it to end the re-divergence.


def _fts5_candidates(
    conn: sqlite3.Connection,
    issue_tokens: set[str],
    limit: int = 50,
    scope: "RepoScope | None" = None,
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
    import sys

    if not issue_tokens:
        return []

    # PROOF MODE (Stage 2): FTS5 is a MANDATORY Go-built capability, not a helper.
    # nodes_fts must already exist (Go indexer with -tags sqlite_fts5), be populated,
    # and answer a real MATCH — else this is the silent degrade to a python-side
    # rebuild the plan forbids. Raises in proof mode; no-op (returns) otherwise so
    # the dev/CI fallback below is byte-identical.
    from groundtruth.runtime import proof as _proof
    if _proof.is_proof_mode():
        _proof.assert_fts5_native(conn, where="L1 retrieval")

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
                print("[GT L1] FTS5: no database path available, skipping", file=sys.stderr)
                return []
            try:
                print("[GT L1] FTS5: nodes_fts missing, building read-only in-memory index", file=sys.stderr)
                # READ-ONLY fallback: build the FTS index in a PRIVATE in-memory db with
                # graph.db ATTACHed read-only (mode=ro), so this query path NEVER writes
                # graph.db (was: sqlite3.connect(_db_path) + CREATE/INSERT INTO the source
                # -> mutated a read-only artifact, changed its sha256, could race a reader).
                # uri=True enables SQLITE_OPEN_URI so the ATTACH honors the mode=ro filename;
                # resolve() guarantees the absolute path as_uri() requires.
                import pathlib
                _src_uri = pathlib.Path(_db_path).resolve().as_uri() + "?mode=ro"
                _fts_conn = sqlite3.connect(":memory:", uri=True)
                _fts_conn_owned = True
                _fts_conn.execute("ATTACH DATABASE ? AS src", (_src_uri,))
                _fts_conn.execute(_FTS5_CREATE)
                # SM-9a: scope the in-memory index population to the active repo so the
                # rebuilt nodes_fts never carries another repo's symbols. src.nodes has
                # repo_id; _FTS5_POPULATE ends with `WHERE is_test = 0`. No-op single-repo.
                _src_frag, _src_params = _repo_frag(scope)
                _fts_conn.execute(_FTS5_POPULATE + _src_frag, _src_params)
                _fts_conn.commit()
                _n_rows = _fts_conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
                print(f"[GT L1] FTS5: in-memory creation OK ({_n_rows} rows, graph.db untouched)",
                      file=sys.stderr)
            except (sqlite3.Error, ValueError, OSError) as _fts_err:
                print(f"[GT L1] FTS5: in-memory creation FAILED: {_fts_err}", file=sys.stderr)
                if _fts_conn_owned:
                    try:
                        _fts_conn.close()
                    except Exception:
                        pass
                return []
        else:
            print("[GT L1] FTS5: nodes_fts exists, querying directly", file=sys.stderr)
    except sqlite3.Error as _tbl_err:
        print(f"[GT L1] FTS5: table check failed: {_tbl_err}", file=sys.stderr)
        return []

    # Build FTS5 MATCH query: join tokens with OR for broad recall.
    # Filter tokens: skip very short (< 3 chars) and escape FTS5 special chars.
    safe_tokens = []
    for t in sorted(issue_tokens, key=lambda x: (-len(x), x)):
        # FTS5 special chars: *, ^, ", (, ), :, +, -, NOT, AND, OR, NEAR
        # Wrap each token in double quotes to treat as literal phrase.
        cleaned = t.replace('"', '')
        if len(cleaned) >= 3 and all(c.isalnum() or c == '_' for c in cleaned):
            safe_tokens.append(f'"{cleaned}"')
        if len(safe_tokens) >= 20:
            break

    if not safe_tokens:
        if _fts_conn_owned:
            try:
                _fts_conn.close()
            except Exception:
                pass
        return []

    match_expr = " OR ".join(safe_tokens)

    # LEAK INVARIANT (Fable S1/L2, reproduced live): the Go-built external-content nodes_fts
    # is now is_test-filtered at INSERT (store/sqlite.go), but a STALE or externally-built
    # graph.db could still carry test rows — and a test symbol seeded here becomes an
    # FTS5_SEED/BFS root that renders as `fts5 match: …`, leaking the FAIL_TO_PASS surface.
    # Defense-in-depth: on the direct-graph.db path (not owned) JOIN nodes and exclude
    # is_test. The in-memory fallback (owned) is already population-filtered (_FTS5_POPULATE
    # WHERE is_test = 0) and has no local `nodes` to join — so keep its plain query.
    if _fts_conn_owned:
        _fts_query = """SELECT rowid, name, file_path,
                      bm25(nodes_fts, 1.0, 2.0, 0.5, 0.5) as score
               FROM nodes_fts
               WHERE nodes_fts MATCH ?
               ORDER BY score, file_path, name, rowid
               LIMIT ?"""
        _fts_params: tuple = (match_expr, limit)
    else:
        # SM-9a: the direct path JOINs the real nodes (which carry repo_id) -> scope
        # the MATCH to the active repo. Frag param sits between MATCH and LIMIT.
        _f_n, _p_n = _repo_frag(scope, "n")
        _fts_query = """SELECT nodes_fts.rowid, nodes_fts.name, nodes_fts.file_path,
                      bm25(nodes_fts, 1.0, 2.0, 0.5, 0.5) as score
               FROM nodes_fts
               JOIN nodes n ON n.id = nodes_fts.rowid
               WHERE nodes_fts MATCH ? AND COALESCE(n.is_test, 0) = 0""" + _f_n + """
               ORDER BY score, nodes_fts.file_path, nodes_fts.name, nodes_fts.rowid
               LIMIT ?"""
        _fts_params = (match_expr, *_p_n, limit)
    try:
        rows = _fts_conn.execute(_fts_query, _fts_params).fetchall()
    except sqlite3.Error as _q_err:
        print(f"[GT L1] FTS5: query failed: {_q_err}", file=sys.stderr)
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
    if results:
        print(f"[GT L1] FTS5: query returned {len(results)} candidates", file=sys.stderr)
    else:
        print("[GT L1] FTS5: no candidates found", file=sys.stderr)
    return results


def _split_camel_subtokens(s: str) -> list[str]:
    """Split a PascalCase/camelCase run into word parts (parity with the Go indexer's
    splitCamel in content_fts.go): boundary at a lower->Upper transition and at an
    acronym-run->Upper+lower transition ("HTTPServer" -> HTTP, Server)."""
    if not s:
        return []
    out: list[str] = []
    start = 0
    # ASCII range tests (NOT str.islower/isupper) to mirror Go splitCamel EXACTLY: the Go
    # indexer uses 'a'<=c<='z' / 'A'<=c<='Z', so a Unicode-aware Python predicate would
    # split non-ASCII identifiers at boundaries the stored content never split → query/
    # index parity break on those idents. Empty nxt ("") fails 'a'<=""<="z" → no false split.
    for i in range(1, len(s)):
        prev, cur = s[i - 1], s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        lower_to_upper = ("a" <= prev <= "z") and ("A" <= cur <= "Z")
        acronym_end = ("A" <= cur <= "Z") and ("a" <= nxt <= "z") and ("A" <= prev <= "Z")
        if lower_to_upper or acronym_end:
            out.append(s[start:i])
            start = i
    out.append(s[start:])
    return out


# Common English/boilerplate words skipped by BOTH L1 lexical legs (grep + content-BM25)
# so an over-common token never dominates the OR-query and dilutes BM25 (Fable #10).
_L1_STOPWORDS = frozenset({
    "that", "this", "with", "from", "have", "been", "will",
    "when", "what", "which", "were", "they", "their", "does",
    "should", "would", "could", "about", "some", "other",
    "into", "more", "than", "each", "also", "after", "before",
})

# Max query terms fed to the content-BM25 MATCH (kept the rarest = most discriminative,
# per DF ordering below). Module-level so tests can shrink it to exercise the ordering.
_CONTENT_FTS_TERM_CAP = 30


def _content_fts_candidates(
    conn: sqlite3.Connection,
    issue_tokens: set[str],
    limit: int = 50,
    issue_text: str = "",
    scope: "RepoScope | None" = None,
) -> list[tuple[int, str, str, float]]:
    """BM25 retrieval over per-symbol BODY content (symbol_content_fts, the Go B1 index).

    The content leg's retriever. Matches issue terms — AND their camelCase/snake_case
    SUB-TOKENS, so a camelCase issue symbol matches the stored sub-tokens the Go indexer
    split — against function BODY vocabulary (identifiers, string literals, comments) that
    no name-only surface indexes. Returns (node_id, name, file_path, bm25_score) with test
    symbols excluded (they are also excluded at index time — belt and suspenders against a
    body-text leak). Graceful []: symbol_content_fts is absent on a graph.db built before
    B1 or without FTS5, so an empty return simply leaves the lexical slot vacant.

    Research: BLUiR (ASE 2013) structured lexical anchoring + sub-token splitting; the
    body-content surface is the stratum-B (behavior-described) complement to nodes_fts.
    """
    import sys

    if not issue_tokens:
        return []
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        return []
    if "symbol_content_fts" not in tables:
        return []

    # Expand each issue token with its camelCase/snake_case sub-tokens (the query MUST
    # be split the same way the stored content was, or a camelCase symbol never matches).
    _expanded: set[str] = set()
    for t in issue_tokens:
        _expanded.add(t)
        for part in str(t).split("_"):
            for sub in _split_camel_subtokens(part):
                if len(sub) >= 2:
                    _expanded.add(sub)

    # Case-aware expansion from the RAW issue text: _issue_terms lowercases every token
    # BEFORE it reaches here (getUserById -> getuserbyid), erasing the camelCase boundary so
    # the loop above finds nothing — partial-match then works only for snake_case langs
    # (Python/Rust) and is DEAD for camelCase (JS/TS/Go/Java, 4 of 6 Tier-1). Splitting the
    # ORIGINAL-case issue words restores it (sub-tokens lowercased to match the case-folded
    # unicode61 FTS content). issue_text="" => byte-identical to the pre-fix behaviour.
    if issue_text:
        for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text):
            for part in w.split("_"):
                for sub in _split_camel_subtokens(part):
                    if len(sub) >= 2:
                        _expanded.add(sub.lower())

    # Well-formed, non-stopword candidates (len>=3, alnum/_).
    _cands: list[str] = []
    for t in _expanded:
        cleaned = t.replace('"', "")
        if (len(cleaned) >= 3 and cleaned.lower() not in _L1_STOPWORDS
                and all(c.isalnum() or c == "_" for c in cleaned)):
            _cands.append(cleaned)
    # SOTA term selection (Fable #4): order by ASCENDING document frequency in
    # symbol_content_fts — rarest = most discriminative (IDF-like) — instead of longest-first,
    # which anti-selects short distinctive domain tokens (tls/acl/ssl) under a common word cap.
    # Data-driven + repo-local + deterministic (tie-break -len, token). df==0 tokens retrieve
    # nothing, so they sort LAST (harmless, but never displace a df>=1 term). To bound the FTS
    # COUNT probes on a huge issue, only the 120 longest candidates are scored (the rest are
    # the least likely to be distinctive multi-word compounds anyway).
    _probe = sorted(set(_cands), key=lambda x: (-len(x), x))[:120]

    def _df(tok: str) -> int:
        try:
            return conn.execute(
                "SELECT count(*) FROM symbol_content_fts WHERE symbol_content_fts MATCH ?",
                (f'"{tok}"',),
            ).fetchone()[0]
        except sqlite3.Error:
            return 1 << 30
    _dfmap = {t: _df(t) for t in _probe}
    _ranked = sorted(_probe, key=lambda x: (_dfmap[x] == 0, _dfmap[x], -len(x), x))
    safe_tokens = [f'"{t}"' for t in _ranked[:_CONTENT_FTS_TERM_CAP]]
    if not safe_tokens:
        return []
    match_expr = " OR ".join(safe_tokens)

    _f_n, _p_n = _repo_frag(scope, "n")  # SM-9a: scope the body-content JOIN to active repo
    try:
        rows = conn.execute(
            """SELECT c.rowid, n.name, n.file_path, bm25(symbol_content_fts) AS score
                 FROM symbol_content_fts c JOIN nodes n ON n.id = c.rowid
                WHERE symbol_content_fts MATCH ? AND COALESCE(n.is_test, 0) = 0""" + _f_n + """
                ORDER BY score, n.file_path, c.rowid
                LIMIT ?""",
            (match_expr, *_p_n, limit),
        ).fetchall()
    except sqlite3.Error as _q_err:
        print(f"[GT L1] content-fts: query failed: {_q_err}", file=sys.stderr)
        return []

    out: list[tuple[int, str, str, float]] = []
    for row in rows:
        if row and row[0] is not None:
            score = -float(row[3]) if row[3] is not None else 0.0
            out.append((int(row[0]), str(row[1]), _normalize(str(row[2])), score))
    return out


def _content_margin_threshold() -> "float | None":
    """The measurement-derived RELATIVE margin the content-BM25 FUSION leg must clear to
    contribute to the composite (``GT_CONTENT_MARGIN``, a fraction in [0, 1)). Returns
    ``None`` — the UNCALIBRATED / correct-or-quiet state — when the env is unset, empty,
    unparseable, or out of range. When ``None`` the fusion leg ABSTAINS entirely (it never
    adds an RRF term), so the pipeline is byte-identical to the leg-off path. The VALUE is
    NOT hardcoded here (house rule: no arbitrary thresholds): it comes from the measured
    top-hit-margin distribution via ``scripts/measure_brief.py`` — OWED until that measures
    it, and until then the leg is deliberately silent on the grep-non-empty (fusion) path."""
    raw = os.getenv("GT_CONTENT_MARGIN")
    if raw is None or not raw.strip():
        return None
    try:
        v = float(raw.strip())
    except ValueError:
        return None
    return v if 0.0 <= v < 1.0 else None


def _content_leg_margin_ok(cfts: "list[tuple[int, str, str, float]]", threshold: float) -> bool:
    """The MARGIN-GATE: does the content-BM25 leg have a CLEARLY-SEPARATED top hit?

    ``cfts`` rows carry ``-bm25`` (higher = better). The gate passes iff the relative
    separation of the best hit over the runner-up, ``(top1 - top2) / top1``, is at least
    ``threshold`` — i.e. the leg found ONE body that dominates, not a flat field of weak
    near-ties. A single hit is maximally separated (passes); an empty/degenerate field or a
    non-positive top score fails. Deterministic (BM25 is deterministic), pure, comparative
    (relative, not an absolute score cutoff), so it generalizes across repos/score scales.

    This is the correct-or-quiet core: a WEAK margin -> the leg contributes ≈0 (abstains),
    so it never converts a confident lexical/structural localization into body-BM25-first
    noise; only a confident content hit shifts the composite rank."""
    scores = sorted((float(c[3]) for c in cfts), reverse=True)
    if not scores or scores[0] <= 0.0:
        return False
    if len(scores) == 1:
        return True
    top1, top2 = scores[0], scores[1]
    return ((top1 - top2) / top1) >= float(threshold)


def _path_decay_scores(
    conn: sqlite3.Connection,
    seed_node_ids: list[int],
    has_conf: bool,
    max_hop: int = 3,
    beta: float = 0.85,
    min_edge_conf: float = 0.5,
    has_method: bool = False,
    has_trust_tier: bool = False,
) -> dict[str, float]:
    """KGCompass-style path decay scoring over the call graph.

    Walk call graph from seeds using Dijkstra-style BFS. Edge weight =
    1/confidence, so high-confidence edges (verified imports at 1.0) are
    cheap paths and speculative name_match edges (0.4) are expensive.

    Path cost L(f) = sum(1/confidence) along the shortest path from any seed.
    Score S(f) = beta^L(f). Verified import edges yield short paths with
    minimal decay; speculative name_match edges yield long paths with heavy
    decay — exactly the correct-or-quiet property.

    Edge ADMISSION uses _edge_admitted — the SAME predicate as the witness BFS
    (#54): SUPPRESSED tier and stdlib-shadow name_match edges are HARD-EXCLUDED,
    and an unverified edge below ``min_edge_conf`` is dropped, so a file can never
    earn decay mass through an edge the witness layer rejected. ``has_method`` /
    ``has_trust_tier`` let the SELECT pull ``resolution_method`` / ``trust_tier``
    so the predicate sees the same provenance the witness BFS does.

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
    method_sel = "e.resolution_method" if has_method else "''"
    tier_sel = "e.trust_tier" if has_trust_tier else "''"

    while pq:
        cost, nid, hops = heapq.heappop(pq)

        # Skip if we already found a cheaper path to this node.
        if cost > best_cost.get(nid, float('inf')):
            continue

        if hops >= max_hop:
            continue

        # Expand neighbors in both directions (out-edges and in-edges). SELECT the
        # same provenance columns the witness BFS reads (name / method / tier) so
        # the shared _edge_admitted predicate (#54) sees identical inputs — no
        # SQL-level conf_where; admission happens once, in Python, for both walks.
        for match_col, join_col in [("e.source_id", "e.target_id"),
                                     ("e.target_id", "e.source_id")]:
            try:
                rows = conn.execute(
                    f"""SELECT {join_col} AS nbr_id, n.file_path, {conf_sel},
                               n.name, {method_sel}, {tier_sel}
                        FROM edges e
                        JOIN nodes n ON {join_col} = n.id
                        WHERE {match_col} = ?
                          AND e.type IN ({_REACH_EDGE_TYPES_SQL})
                          AND n.is_test = 0
                        ORDER BY n.file_path, {join_col}
                        LIMIT 100""",
                    (nid,),
                ).fetchall()
            except sqlite3.Error:
                continue

            for nbr_id, nbr_file, conf, nbr_name, method, tier in rows:
                if nbr_id is None or nbr_file is None:
                    continue
                nbr_id = int(nbr_id)
                nbr_file = _normalize(str(nbr_file))
                verified = _is_verified(method)
                # ADMISSION (shared predicate). Use the witness-BFS conf convention
                # (0.0 when missing) so the SAME edge is admitted/rejected in both
                # traversals; the cost weight below keeps its own 1.0 default.
                try:
                    admit_conf = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    admit_conf = 0.0
                if not _edge_admitted(
                    verified, admit_conf, tier, str(nbr_name or ""), min_edge_conf
                ):
                    continue
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
    # Exact resolver provenance for edge witnesses. Empty for lexical DEFINES /
    # grep/path seeds and legacy synthetic witnesses. This is audit-only source
    # lineage; it does not affect strength, ordering, or rendered bytes.
    resolution_method: str = ""

    def strength(self) -> float:
        conf = self.confidence if self.confidence > 0 else (1.0 if self.verified else 0.5)
        decay = 1.0 / (1.0 + self.hop)
        if self.direction == "defines_anchor":
            # DEFINES is lexical (the file merely defines a same-named symbol) and is
            # ALWAYS hop-0, so its decay is 1.0. Cap the scalar strictly below the
            # weakest possible verified-EDGE strength (#57) so a hop-0 DEFINES can
            # never out-rank a real structural edge on the scalar that feeds
            # Candidate.confidence / the [VERIFIED] gate / the W_WITNESS term. The
            # _witness_tier sort already orders tiers; this keeps the SCALAR honest.
            return min(_WITNESS_DEFINES * conf * decay, _WITNESS_DEFINES_CEIL)
        if self.verified:
            base = _WITNESS_VERIFIED
        else:
            base = _WITNESS_NAMEMATCH
        return base * conf * decay


@dataclass(frozen=True)
class Candidate:
    file_path: str
    score: float
    witnesses: list[Witness]
    lex_hits: int  # # of issue terms intersecting this file's symbol/path identifiers
    degree: int
    confidence: float  # best-witness strength, 0..1 (drives the render gate)
    # Issue relevance is deliberately separate from structural edge validity.
    # One deterministic edge proves that edge; it does not prove this file is an
    # edit target for the current issue.
    relevance_grade: str = "INFO"

    @property
    def edge_verified(self) -> bool:
        return any(
            w.verified and w.direction != "defines_anchor"
            for w in self.witnesses
        )

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
        # LEAK GUARD (Fable L8, defense-in-depth): EVERY render branch below prints
        # w.anchor / w.src_symbol / w.dst_symbol verbatim (hop-2 "{anchor} -> ... -> {far}",
        # "defines {anchor}", "{seed}: {src or anchor}"). The nodes_fts is_test fix keeps
        # test nodes out of the BFS, but a test NAME could still reach a witness field via a
        # stale index or cross-hop provenance — and the old filter guarded only the
        # edge-witness src/dst, NEVER the anchor. Drop any witness whose anchor OR either
        # endpoint is a test-block name up front, so all three branches inherit the guard.
        _safe = [
            w for w in self.witnesses
            # E2 (Fable 2026-07-05): admission defense — drop a witness whose FILE is a test
            # path (mirrors §19.1 caller-line closure). The name guards below miss a real code
            # symbol that merely LIVES in tests/ on a stale graph (is_test flag unset).
            if not _is_test_path_pp(w.file_path)
            and not _is_test_block_name(w.anchor)
            and not _is_test_block_name(w.src_symbol)
            and not _is_test_block_name(w.dst_symbol)
        ]
        if not _safe:
            return ""
        edge_wits = [
            w for w in _safe if w.direction != "defines_anchor"
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
            # src_symbol is ALWAYS the caller, dst_symbol ALWAYS the callee (the BFS at
            # ~line 2351 assigns src=caller/dst=callee for BOTH directions). The prior
            # `{src} called by {dst}` for called_by_anchor was INVERTED — it read as
            # "caller called by callee" (witnessed: `main called by BeginRepl` when main
            # CALLS BeginRepl). Render the caller->callee fact correctly, candidate-first.
            _calls_anchor = w.direction == "calls_anchor"
            # PROVENANCE TAG (fix 2026-06-09, correct-or-quiet): an edge whose
            # resolution_method is NOT in curation_map.DETERMINISTIC_RESOLUTION_METHODS
            # renders with an explicit "(unverified)" marker. Witness.verified IS
            # that single-source predicate (_is_verified(method) == method in
            # _DETERMINISTIC_METHODS). v1r_brief's renderer documented this tag
            # ("a name_match witness carries its own '(unverified)' tag from the
            # localizer") but it was never emitted — a name guess rendered exactly
            # like a structural fact (witness-pipe laundering).
            tag = "" if w.verified else " (unverified)"
            if w.hop >= 2:
                far = w.src_symbol if w.direction == "calls_anchor" else w.dst_symbol
                return (
                    f"{w.anchor} -> ... -> {far} "
                    f"[{w.edge_type}, {w.hop}-hop]{tag}"
                )
            body = (
                f"{w.src_symbol} calls {w.dst_symbol}" if _calls_anchor
                else f"{w.dst_symbol} called by {w.src_symbol}"
            )
            return f"{body} [{w.edge_type}]{tag}"
        w = max(_safe, key=lambda x: x.strength())
        # SEED-TYPED witnesses (fix 2026-06-09): only the exact-name seeder mints
        # the "defines {name} (issue symbol)" DEFINES fact. A grep/path/FTS5 seed
        # is a retrieval ENTRY POINT — render it as what it is, never as a
        # fabricated issue-symbol definition.
        if w.edge_type == "DEFINES":
            return f"defines {w.anchor} (issue symbol)"
        _seed_label = {
            "GREP_SEED": "grep match",
            "PATH_SEED": "path match",
            "FTS5_SEED": "fts5 match",
            "CONTENT_SEED": "content match",
        }.get(w.edge_type, "seed match")
        return f"{_seed_label}: {w.src_symbol or w.anchor}"


@dataclass(frozen=True)
class LocalizerResult:
    candidates: list[Candidate]
    anchor_symbols: list[str]
    confidence: float            # best candidate confidence (0 when no anchor hit)
    confident: bool              # passes the per-task data-derived gate
    gate_reason: str             # why confident / not (telemetry)
    scope_chains: list[ScopeChain] = field(default_factory=list)
    graph_stats: dict = field(default_factory=dict)
    # WIDE-scope edit-set telemetry (Task-2 slice 1, additive). n_components = the
    # number of connected components among the top candidates under the typed-edge
    # union-find (1 = one cohesive edit-set; >1 = disjoint clusters). 0 when no
    # scope chains were built. Consumed only by future deep/wide gating + 8-dp
    # logging; absent on early returns (defaults to 0 — byte-identical today).
    n_components: int = 0
    # MULTI-SIGNAL AGREEMENT (the grep-floor build): per-file count of how many
    # of the three independent rankers (grep / structural / semantic) place this
    # file's candidate in their OWN top-3. 0..3. Empty {} when no candidates were
    # ranked (early returns). The graded header consumes this so confidence="X"
    # means "X of {grep,semantic,structural} agree" rather than a structural-only
    # witness count. Rank fusion / multi-signal agreement (Cormack RRF SIGIR 2009;
    # CombMIN Fox & Shaw TREC-2 1994) — agreement across independent rankers is a
    # stronger relevance signal than any single ranker.
    agreement_by_file: dict[str, int] = field(default_factory=dict)
    # LEG ATTRIBUTION (additive, default {}): per-file list of WHICH independent
    # rankers placed the file in their own top-3 — a subset of
    # ["grep","structural","semantic"], stable order. `agreement_by_file` is the
    # COUNT; this is the NAMED legs behind it. The consensus-form header renders
    # "3/3 (grep, structural, semantic)" from this so the model sees leg
    # attribution (the grep vote pre-registers what its own grep will return),
    # not just a bare number. Empty {} when no candidates ranked (byte-identical
    # to today until consumed). Kept in lockstep with agreement_by_file:
    # len(signals_by_file[f]) == agreement_by_file[f] for every f.
    signals_by_file: dict[str, list[str]] = field(default_factory=dict)
    # R1 leaf-naming bridge (additive, default {}): per-file ranked per-SYMBOL semantic
    # scores — {file_norm: [(symbol_name, cosine), ...]} high→low — captured from the
    # SAME per-symbol MaxSim cosines that produce the file's semantic score (previously
    # discarded). Consumed by v1r_brief._localization_header to rank WITHIN-FILE leaves
    # by the issue→code semantic signal that reached the gold file, instead of raw
    # in-degree (which names the hub on behavior-described issues). Empty {} when the
    # embedder is off / no candidate scored — degrades the symbol-naming stage to its
    # prior in-degree behavior byte-identically (correct-or-quiet).
    symbol_semrank_by_file: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # Render-neutral mediator evidence.  Paths are normalized and restricted to
    # candidates that survive this localizer's final top-k.
    content_leg_paths: frozenset[str] = frozenset()
    content_leg_decision: str = "NO_EFFECT"
    content_leg_reason: str = "feature_not_reached"
    semantic_body_paths: frozenset[str] = frozenset()


def _normalize(fp: str) -> str:
    """Canonical candidate-map key — the SINGLE path chokepoint every candidate
    map in this module keys through (witnesses/degrees/decay/grep/sem/agreement/
    scope). Backslashes -> '/', then a './' PREFIX is stripped (repeatable: '././'),
    then exactly one leading '/' (absolute tolerance).

    BUG-1 (path-key over-strip): the prior body was ``.lstrip("./").lstrip("/")``.
    ``str.lstrip("./")`` is a CHARSET strip, not a prefix strip — it greedily ate
    EVERY leading '.'/'/' char, so a real top-level dotfile dir lost its dot
    (``.github/x.py`` -> ``github/x.py``) and two genuinely-distinct files collapsed
    to one key (``./.config/app.py`` and ``config/app.py`` both -> ``config/app.py``),
    splitting/merging the gold candidate. control/paths.py:normalize documents this
    exact hazard. Fix: anchored ``./``-PREFIX strip (leading dots in real path
    components preserved), then one leading '/'. Common cases are byte-identical
    (``./beets/importer.py`` -> ``beets/importer.py``; ``beets/importer.py`` ->
    itself); only the over-strip pathology changes."""
    text = fp.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/"):
        text = text[1:]
    return text


def _candidate_relevance_grade(
    file_path: str,
    witnesses: list[Witness],
    *,
    trusted_anchors: set[str],
    explicit_paths: set[str],
    independent_signals: list[str] | tuple[str, ...] = (),
) -> str:
    """Grade candidate-local issue relevance without laundering edge truth.

    VERIFIED requires an explicit issue path, an exact trusted definition, or a
    deterministic candidate-local relation descended from a trusted anchor.
    Multiple independent retrieval legs earn WARNING; a single retrieval/edge
    signal is INFO; no support is ABSTAIN.
    """
    normalized = _normalize(file_path)
    normalized_paths = {_normalize(path) for path in explicit_paths if path}
    if normalized and normalized in normalized_paths:
        return "VERIFIED"
    trusted = {(anchor or "").lower() for anchor in trusted_anchors if anchor}
    for witness in witnesses:
        if not witness.verified or (witness.anchor or "").lower() not in trusted:
            continue
        if witness.direction == "defines_anchor" or witness.edge_type in {"CALLS", "IMPORTS"}:
            return "VERIFIED"
    if len(set(independent_signals)) >= 2:
        return "WARNING"
    if witnesses or independent_signals:
        return "INFO"
    return "ABSTAIN"


def _struct_witness_tier(c: "Candidate") -> int:
    """SWERank hard-negative tier for the structural ranker (BUG-4). Lower = better:
        0  verified CLOSE structural witness  (CALLS/IMPORTS, hop <= 1)
        1  verified DISTANT structural witness (hop >= 2) — a real edge, just far
        2  verified DEFINES only — name-equality, NOT a structural fact
        3  unverified witness only
        4  no witness
    A verified distant edge (tier 1) out-ranks a bare name-equality DEFINES
    (tier 2): the edge is a structural fact, the DEFINES a same-name coincidence."""
    if not c.has_verified_witness:
        return 3 if c.witnesses else 4
    has_close = any(
        w.verified and w.direction != "defines_anchor" and w.hop <= 1
        for w in c.witnesses
    )
    if has_close:
        return 0
    has_distant_structural = any(
        w.verified and w.direction != "defines_anchor" and w.hop >= 2
        for w in c.witnesses
    )
    return 1 if has_distant_structural else 2


def _final_relevance_key(
    c: "Candidate", subject_pos: "dict[str, int]"
) -> tuple[float, int, int, str]:
    """The relevance-bearing tie-break appended AFTER the RRF term in the final
    localizer sort (BUG-2). Orders by best-witness strength (desc), then issue
    lexical-token coverage (desc), then SUBJECT POSITION (the file defining the
    issue's first-named / broken function ranks first), then — only as the LAST
    resort — the path string. Replaces the bare ``c.file_path`` tie-break so an
    alphabetical path can never decide the cap slot ahead of a real relevance
    signal. Generalized: every key is a per-task structural/issue signal, no repo
    or task IDs."""
    return (
        -float(c.confidence),
        -int(c.lex_hits),
        int(subject_pos.get(c.file_path, 10**9)),
        c.file_path,
    )


def _issue_terms(issue_text: str) -> set[str]:
    return {
        w.lower()
        for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text or "")
        if len(w) >= _MIN_ANCHOR_LEN
    }


# camelCase / snake_case / digit boundaries — split an identifier into its
# semantic components so lexical matching is BOUNDARY-aware, not substring.
_IDENT_SPLIT_RE = _re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")


def _ident_components(sym: str) -> set[str]:
    """Lowercased components of an identifier (set_fields -> {set, fields};
    setUpClass -> {set, up, class}; offset -> {offset}). The component set is
    what boundary-aware lexical matching tests membership against, so a 3-char
    issue term like ``set`` matches ``set_fields`` (a real component) but NOT
    ``settings`` / ``reset`` / ``offset`` (single, indivisible tokens). Mirrors
    the path-seed component discipline (#56)."""
    if not sym:
        return set()
    parts = {p.lower() for p in _IDENT_SPLIT_RE.split(sym) if p}
    parts.add(sym.lower())  # the whole identifier is itself a component
    return parts


def _lex_hit(term: str, symset: set[str]) -> bool:
    """Boundary-aware BLUiR field-level lexical hit (#56). A term scores iff it
    equals a whole identifier COMPONENT of some symbol (the precise signal), or
    — only for distinctive terms (len >= 4, mirroring the path-seed >= 4 floor)
    — it equals the full symbol stem. Replaces the old ``t in s or s in t``
    unbounded substring, which made ``set`` match settings/reset/offset/dataset
    and re-introduced the exact lexical-overconnect this module exists to kill."""
    t = term.lower()
    if len(t) < _MIN_ANCHOR_LEN:
        return False
    for s in symset:
        if not s or len(s) <= 2:
            continue
        if t == s:
            return True
        if t in _ident_components(s):
            return True
    return False


def _seed_node_rows(
    conn: sqlite3.Connection, anchors: set[str], scope: "RepoScope | None" = None
) -> list[tuple[int, str, str]]:
    """(node_id, name, file_path) for every Function/Method/Class node whose name
    is an issue anchor. These are the BFS seeds (KGCompass entity seeding).

    ``scope`` (SM-9a): when the graph indexes >1 repository, scope the name-match to
    the active repo so a same-named symbol from another repo is not seeded. None /
    single-repo -> byte-identical."""
    if not anchors:
        return []
    _f_bare, _p_bare = _repo_frag(scope)
    _f_c, _p_c = _repo_frag(scope, "c")
    out: list[tuple[int, str, str]] = []
    # A set's iteration order varies with PYTHONHASHSEED. The 400-item chunk
    # boundary is observable, so establish one canonical anchor order first.
    anchors_l = sorted(anchors)
    # Chunk to stay under SQLite's variable limit on huge anchor sets.
    for i in range(0, len(anchors_l), 400):
        chunk = anchors_l[i : i + 400]
        ph = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT id, name, file_path FROM nodes "
                f"WHERE name IN ({ph}) AND is_test = 0 "
                f"AND label IN ('Function','Method','Class','Interface')" + _f_bare,
                tuple(chunk) + _p_bare,
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0] is not None and r[2]:
                out.append((int(r[0]), str(r[1]), _normalize(str(r[2]))))

    # QUALIFIED dotted anchors (fix 2026-06-10 — §4 anchor-extraction defect):
    # anchors like ``Class.method`` can never match the bare-name IN(...) query
    # (nodes store bare names), so the issue's most specific anchor seeded
    # NOTHING here. Resolve the qualified pair: the node named ``tail`` whose
    # parent is named ``qualifier`` (or whose qualified_name matches). Pure
    # recall addition — a dotted anchor that resolves seeds its REAL definition
    # node; unresolvable dotted anchors stay non-seeding (correct-or-quiet).
    _seen_ids = {nid for nid, _, _ in out}
    for anc in sorted(anchors):
        if "." not in anc:
            continue
        parts = [p for p in anc.split(".") if p]
        if len(parts) < 2:
            continue
        qualifier, tail = parts[-2], parts[-1]
        try:
            qrows = conn.execute(
                "SELECT c.id, c.name, c.file_path FROM nodes c "
                "JOIN nodes p ON c.parent_id = p.id "
                "WHERE c.name = ? AND p.name = ? AND c.is_test = 0" + _f_c,
                (tail, qualifier) + _p_c,
            ).fetchall()
            if not qrows:
                qrows = conn.execute(
                    "SELECT id, name, file_path FROM nodes "
                    "WHERE (qualified_name = ? OR qualified_name LIKE ?) "
                    "AND is_test = 0" + _f_bare,
                    (anc, f"%.{qualifier}.{tail}") + _p_bare,
                ).fetchall()
        except sqlite3.Error:
            continue
        for r in qrows:
            if r and r[0] is not None and r[2] and int(r[0]) not in _seen_ids:
                _seen_ids.add(int(r[0]))
                out.append((int(r[0]), str(r[1]), _normalize(str(r[2]))))
    # SQLite row order is physical state, not relevance. Traversal is capped
    # downstream, so use a semantic total order before returning the seeds.
    out.sort(key=lambda row: (row[2], row[1], row[0]))
    return out


def _path_to_seeds(
    conn: sqlite3.Connection,
    issue_tokens: set[str],
    existing_seed_files: set[str],
    limit: int = 10,
    scope: "RepoScope | None" = None,
) -> list[tuple[int, str, str]]:
    """Seed from files whose PATH contains an issue token.

    When "flex" doesn't match any function name but matches
    layout/flex.py, add functions from that file as seeds.
    This closes the gap where issue tokens name MODULES not FUNCTIONS.

    Research: KGCompass (2025) -- 89.7% of bugs need multi-hop from
    the issue-mentioned entity. The entity can be a MODULE, not just
    a function. BLUiR (ASE 2013) -- structured field-level anchoring
    on file paths catches module-level references that function-name
    seeding misses.

    Args:
        conn: read-only connection to graph.db.
        issue_tokens: lowercased issue tokens (len >= 3).
        existing_seed_files: normalized file paths already seeded by
            _seed_node_rows. Tokens whose path matches a file that is
            ALREADY seeded (by any mechanism) are SKIPPED here to
            avoid double-seeding the same file.
        limit: max total path-seeded nodes returned.

    Returns:
        (node_id, name, file_path) tuples for functions/methods/classes
        in path-matched files.
    """
    import sys

    if not issue_tokens:
        return []

    # Filter: tokens >= 4 chars (3 is too short for path matching — "set"
    # matches settings/, dataset/, reset.py).
    path_tokens = sorted(
        (t for t in issue_tokens if len(t) >= 4),
        key=lambda t: (-len(t), t),
    )
    if not path_tokens:
        return []

    _f_bare, _p_bare = _repo_frag(scope)  # SM-9a active-repo scope (no-op single-repo)
    out: list[tuple[int, str, str]] = []
    seen_ids: set[int] = set()
    seen_files: set[str] = set(existing_seed_files)  # dedup by file, not just node ID

    for token in path_tokens:
        if len(out) >= limit:
            break
        # Match token as a path COMPONENT only — no broad substring.
        # /token.ext (file stem) or /token/ (directory name).
        # token.ext (root-level file like setup.py).
        # Broad %token% was noise (LIPI review: "set" → settings/).
        # Directory patterns first (stronger), then root-level last.
        patterns = [f"%/{token}.%", f"%/{token}/%", f"{token}.%"]
        _found_any = False
        for pat in patterns:
            if len(out) >= limit:
                break
            try:
                rows = conn.execute(
                    "SELECT id, name, file_path FROM ("
                    "SELECT id, name, file_path, "
                    "ROW_NUMBER() OVER ("
                    "PARTITION BY file_path ORDER BY name, "
                    "CASE WHEN start_line IS NULL THEN 1 ELSE 0 END, start_line, "
                    "CASE WHEN end_line IS NULL THEN 1 ELSE 0 END, end_line, "
                    "COALESCE(signature,''), id"
                    ") AS file_rank FROM nodes "
                    "WHERE file_path LIKE ? AND is_test = 0 "
                    "AND label IN ('Function','Method','Class')" + _f_bare + ") "
                    "WHERE file_rank = 1 "
                    "ORDER BY file_path, name, id LIMIT 5",
                    (pat,) + _p_bare,
                ).fetchall()
                if rows:
                    _found_any = True
            except sqlite3.Error:
                continue
            for r in rows:
                fp = _normalize(str(r[2])) if r and r[2] else ""
                if r and r[0] is not None and fp and int(r[0]) not in seen_ids and fp not in seen_files:
                    seen_ids.add(int(r[0]))
                    seen_files.add(fp)
                    out.append((int(r[0]), str(r[1]), fp))
                    if len(out) >= limit:
                        break

    # COMPOUND path-token recall (2026-06-27): when the issue contains
    # multiple tokens and a file path contains TWO OR MORE of them, that
    # file is more specific than one matching a single token. Promotes
    # child implementations over parent modules: issue "HOTP token" →
    # tokens/hotptoken.py (both "hotp" and "token" in path) outranks
    # token.py (only "token"). Runs AFTER single-token pass so compound
    # matches get PRIORITY (prepended, not appended). Generalized: any
    # repo structure where child files combine qualifier + stem in their
    # path (the standard naming convention across all languages).
    if len(path_tokens) >= 2:
        try:
            all_files = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT file_path FROM nodes WHERE is_test = 0" + _f_bare
                    + " ORDER BY file_path",
                    _p_bare,
                ).fetchall() if r[0]
            ]
            compound_hits: list[tuple[str, int]] = []
            for fp in all_files:
                fp_lower = fp.lower()
                hit_count = sum(1 for t in path_tokens if t in fp_lower)
                if hit_count >= 2:
                    compound_hits.append((fp, hit_count))
            compound_hits.sort(key=lambda item: (-item[1], _normalize(item[0])))
            compound_new: list[tuple[int, str, str]] = []
            for fp, _hc in compound_hits[:limit]:
                nfp = _normalize(fp)
                if nfp in seen_files:
                    continue
                rows = conn.execute(
                    "SELECT id, name FROM nodes WHERE file_path = ? "
                    "AND is_test = 0 AND label IN ('Function','Method','Class')" + _f_bare + " "
                    "ORDER BY name, id LIMIT 1", (fp,) + _p_bare
                ).fetchall()
                if rows and rows[0][0] is not None:
                    nid = int(rows[0][0])
                    if nid not in seen_ids:
                        seen_ids.add(nid)
                        seen_files.add(nfp)
                        compound_new.append((nid, str(rows[0][1]), nfp))
            if compound_new:
                out = compound_new + out
                print(
                    f"[GT L1] path-to-seed: {len(compound_new)} COMPOUND nodes "
                    f"(2+ tokens in path) prepended",
                    file=sys.stderr,
                )
        except (sqlite3.Error, Exception):
            pass

    if out:
        print(
            f"[GT L1] path-to-seed: {len(out)} total nodes seeded from "
            f"{len(path_tokens)} path tokens",
            file=sys.stderr,
        )

    return out


def _path_match_token(fp: str, issue_tokens: set[str]) -> str:
    """The issue token that PATH-matched this file — re-derived with the same
    component patterns ``_path_to_seeds`` matched on (``/{token}.``, ``/{token}/``,
    leading ``{token}.``). Longest hit wins; ``""`` when nothing re-derives.
    Display-only (the seed-typed witness line); never affects ranking."""
    fpl = "/" + (fp or "").lower()
    hits = [
        t for t in issue_tokens
        if len(t) >= 4 and (f"/{t}." in fpl or f"/{t}/" in fpl)
    ]
    return max(hits, key=len) if hits else ""


def _grep_to_seeds(
    issue_tokens: set[str],
    repo_root: str,
    conn: sqlite3.Connection,
    max_seeds: int = 20,
    priority_tokens: set[str] | None = None,
) -> list[tuple[int, str, str]]:
    """Grep-recall seeding: subsume grep so GT can never have worse recall.

    Runs ripgrep (`rg -l`, files-with-matches) — or a Python-walk fallback — over the
    repo for issue tokens, and for each HIT FILE returns its first few non-test graph
    nodes as additional BFS seeds. NOTE: this is FILE-granularity, not line-granularity —
    `-l` yields no line numbers, so the seeds are the file's leading nodes, NOT the node
    enclosing the matched line. File-level recall is the guarantee; ranking is left to the
    downstream legs (grep_score_by_file, structural, semantic).

    This is mechanism B from the recall analysis: use grep for recall, graph
    for rank. GT seeds only on name-matched Function/Method/Class/Interface
    nodes today (_seed_node_rows), missing files whose code CONTAINS issue
    tokens in string literals, attributes, variable names, or function bodies.
    Grep finds those. This function bridges the gap.

    Research: SWERank (2025) retrieve→rerank — the retrieve must have at
    least grep-grade recall; the rerank adds structural depth.
    """
    import shutil
    import subprocess
    import sys as _sys_grep

    if not repo_root or not (issue_tokens or priority_tokens):
        return []

    # GREENFIELD priority (gt_gt §4 grep-fallback wiring, 2026-06-10):
    # reporter-marked code tokens with ZERO graph nodes
    # (IssueAnchors.unresolved_code_symbols — go `require`, env vars, rust
    # `::`-pair tails) are the MOST specific grep anchors for feature issues,
    # but the length-sorted top-10 cut below crowds them out behind longer
    # prose words. They go FIRST, capped at 5 (longest first) so a fenced
    # example block can never flood the grep query. No-op when empty.
    _prio = sorted(
        {t for t in (priority_tokens or set()) if len(t) >= _MIN_ANCHOR_LEN},
        key=lambda t: (-len(t), t),
    )[:5]
    _prio_low = {t.lower() for t in _prio}

    # Pick distinctive tokens (skip very short or very common words)
    tokens = _prio + [t for t in sorted(
        (t for t in issue_tokens if len(t) >= 4 and t not in _L1_STOPWORDS),
        key=lambda t: (-len(t), t),
    )[:10] if t.lower() not in _prio_low]

    if not tokens:
        return []

    print(
        f"[GT L1] grep-to-seed: searching {len(tokens)} tokens in {repo_root}",
        file=_sys_grep.stderr,
    )

    # Check rg availability ONCE before the loop. If rg is in PATH, use
    # it for ALL tokens. If not, use Python walk for ALL. Don't switch
    # mid-loop (Bug 4: inconsistent coverage from partial rg failures).
    #
    # PINNABLE BACKEND (2026-07-28). `shutil.which("rg")` reads the ambient PATH, so the SAME
    # commit seeds differently on a host with ripgrep than on one without -- the two backends
    # do not return identical file sets. That made every before/after comparison through
    # `ss_gate` un-citable across machines: the gate strips the whole GT_* namespace but has no
    # control over PATH, so it believed it was hermetic and was not. Same defect class as the
    # ambient `gt-index` binary, which that harness already closes by pinning `GT_INDEX_BIN` to
    # a guaranteed-absent path.
    #
    # `auto` (the default, and the value when the var is unset) is byte-identical to the
    # previous behaviour, so production is untouched; a harness that needs reproducibility pins
    # `python` (or `rg`) explicitly and gets the same seeding on every host.
    _backend = (os.environ.get("GT_L1_GREP_BACKEND") or "auto").strip().lower()
    if _backend == "python":
        _rg_available = False
    elif _backend == "rg":
        _rg_available = True
    else:
        _rg_available = shutil.which("rg") is not None
    if not _rg_available:
        print(
            "[GT L1] grep-to-seed: rg not in PATH, using Python walk",
            file=_sys_grep.stderr,
        )

    # Run ripgrep (or Python walk) for each token, collect file hits
    hit_files: dict[str, set[int]] = {}
    if _rg_available:
        for token in tokens:
            try:
                result = subprocess.run(
                    # -F (fixed-strings): the token is a LITERAL, not a regex. Without it a
                    # priority anchor with an UNBALANCED metachar (foo(, a::b[) is a regex
                    # PARSE ERROR → rg exits non-zero → that token silently contributes ZERO
                    # recall on exactly the most specific anchors; and a BALANCED one (x.y)
                    # was a valid regex whose `.` matched any char, so `x_y` matched by
                    # accident — -F kills that false positive too (precision, not just recall).
                    # -F also matches the Python-walk fallback's literal `in` semantics
                    # (coverage parity). `--` guards a token beginning with `-`.
                    ["rg", "-n", "--no-heading", "-l", "-i", "-F", "--", token, repo_root],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        fp = line.strip()
                        if fp:
                            rel = os.path.relpath(fp, repo_root).replace("\\", "/")
                            hit_files.setdefault(rel, set()).add(0)
            except subprocess.TimeoutExpired:
                continue
            except FileNotFoundError:
                # rg invocation failed for THIS token (binary vanished after the
                # shutil.which check / transient spawn failure). Fix 2026-06-09:
                # CONTINUE to the next token — the prior `break` silently aborted
                # the remaining tokens' recall (and no Python-walk fallback runs
                # on this branch, so those tokens were simply LOST).
                continue
        print(
            f"[GT L1] grep-to-seed: rg found {len(hit_files)} files",
            file=_sys_grep.stderr,
        )
    else:
        # Python fallback: walk once, check all tokens per file
        _source_exts = (
            ".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".java", ".kt", ".scala", ".rb", ".php", ".swift",
            ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cs",
        )  # Fable #9: match the rg leg's coverage on rg-less hosts (was missing tsx/jsx/kt/…)
        try:
            for dirpath, dirnames, filenames in os.walk(repo_root):
                dirnames.sort()
                for fname in sorted(filenames):
                    if not any(fname.endswith(ext) for ext in _source_exts):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    try:
                        with open(fpath, encoding="utf-8", errors="ignore") as fh:
                            content = fh.read(500_000).lower()
                        for token in tokens:
                            if token.lower() in content:
                                rel = os.path.relpath(fpath, repo_root).replace("\\", "/")
                                hit_files.setdefault(rel, set()).add(0)
                                break  # file matched at least one token, no need to check more
                    except OSError:
                        continue
        except Exception as _walk_err:
            print(
                f"[GT L1] grep-to-seed: Python walk FAILED: {_walk_err}",
                file=_sys_grep.stderr,
            )

    if not hit_files:
        print("[GT L1] grep-to-seed: no files matched", file=_sys_grep.stderr)
        return []

    # Score files by number of distinct tokens they contain
    file_scores: list[tuple[str, int]] = []
    for fp, lines in hit_files.items():
        # Count how many distinct issue tokens hit this file
        try:
            fpath = os.path.join(repo_root, fp)
            with open(fpath, encoding="utf-8", errors="ignore") as _fh:
                content = _fh.read(500_000).lower()
            hits = sum(1 for t in tokens if t.lower() in content)
            file_scores.append((fp, hits))
        except OSError:
            file_scores.append((fp, 1))

    file_scores.sort(key=lambda x: (-x[1], _normalize(x[0])))
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
                "ORDER BY name, id LIMIT 5",
                (norm,),
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if r and r[0] is not None and r[0] not in seen_ids:
                seen_ids.add(int(r[0]))
                seeds.append((int(r[0]), str(r[1]), _normalize(str(r[2]))))
    print(
        f"[GT L1] grep-to-seed: mapped to {len(seeds)} seed nodes",
        file=_sys_grep.stderr,
    )
    return seeds


def _role_discount_for_function(
    conn: sqlite3.Connection, file_path: str, func_name: str,
) -> float:
    """Research-backed role discount for a SPECIFIC function (not file-level).

    Checks the DEFINES-witnessed function's own SLOC + fan_out + fan_in.
    A trivial validator `overflow(keyword)` (SLOC=3, fan_out=0) gets 0.2.
    A complex implementation `block_box_layout()` (SLOC=50+) gets 1.0.

    Herbold PeerJ 2019: {SLOC < 4, NoMethodInvocations} => NotFaulty (90%+).
    ARISE 2025: score = α×rel×role + β×proximity (α=0.3, β=0.5).
    """
    try:
        # fan_out/fan_in count ONLY structural/navigation edges (D5). Promoted
        # relationship edges (DATA_FLOW/READS/WRITES/RAISES/...) must not inflate
        # degree or a trivial validator would falsely look connected (fan_out>0).
        _ef = _degree_edge_filter("e")
        row = conn.execute(
            f"""SELECT
                COALESCE(n.end_line - n.start_line, 0) as sloc,
                (SELECT COUNT(*) FROM edges e
                 WHERE e.source_id = n.id AND {_ef}) as fan_out,
                (SELECT COUNT(*) FROM edges e
                 WHERE e.target_id = n.id AND {_ef}) as fan_in
            FROM nodes n WHERE n.file_path = ? AND n.name = ?
            AND n.is_test = 0 AND n.label IN ('Function', 'Method')
            LIMIT 1""",
            (file_path, func_name),
        ).fetchone()
        if not row:
            return 1.0
        sloc, fan_out, fan_in = row[0] or 0, row[1] or 0, row[2] or 0
        if sloc <= 4 and fan_out == 0:
            return 0.2
        if sloc <= 10 and fan_in > 0 and (fan_in / max(fan_out, 1)) > 3:
            return 0.5
        return 1.0
    except sqlite3.Error:
        return 1.0


def _file_degrees(conn: sqlite3.Connection, files: set[str]) -> dict[str, int]:
    """In-degree (incoming structural edges) per file — the centrality prior.

    Counts ONLY structural/navigation edges (D5): promoted relationship edges
    (DATA_FLOW/READS/WRITES/RAISES/...) must not inflate the degree prior that
    feeds W_DEGREE, or hubs gain artificial centrality once promotion ships.
    Degrade-safe: a no-op on current live graphs (all edges already structural).
    """
    if not files:
        return {}
    deg: dict[str, int] = {}
    files_l = list(files)
    _ef = _degree_edge_filter("e")
    for i in range(0, len(files_l), 400):
        chunk = files_l[i : i + 400]
        ph = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT n.file_path, COUNT(e.id) FROM nodes n "
                f"JOIN edges e ON e.target_id = n.id "
                f"WHERE n.file_path IN ({ph}) AND {_ef} GROUP BY n.file_path",
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


def _edge_admitted(
    verified: bool,
    conf_f: float,
    tier: str | None,
    nbr_name: str | None,
    min_edge_conf: float,
) -> bool:
    """ONE edge-admission predicate, shared by the witness BFS and the path-decay
    traversal (#54). Both walks must reject the SAME edges so a file can never earn
    path-decay mass through an edge the witness layer dropped (a phantom decay path
    with zero admitted witnesses). The categorical rule (curation_map.py): admit IFF
      * trust_tier is NOT 'SUPPRESSED' (hard exclude), AND
      * the edge is a deterministic FACT (verified) OR confidence >= min_edge_conf, AND
      * it is NOT a stdlib-shadow name_match (nbr_name in _STDLIB_ATTRS, unverified).
    Verified edges are never confidence- or shadow-filtered. Generalized, no
    repo/task logic — purely a function of the edge's own provenance fields."""
    if str(tier or "").strip().upper() == "SUPPRESSED":
        return False
    if not verified and conf_f < min_edge_conf:
        return False
    if not verified and nbr_name and nbr_name in _STDLIB_ATTRS:
        return False
    return True


def _graph_stats(conn: sqlite3.Connection, has_conf: bool) -> dict:
    """Per-graph density + confidence distribution for dynamic BFS calibration.

    Reuses confidence._repo_stats (cached by db path+mtime+size) for the
    heavy queries, then adds the confidence percentiles that _repo_stats
    doesn't compute. One source of truth for "is this graph dense/sparse."
    """
    stats: dict = {"node_count": 0, "edge_count": 0, "avg_degree": 0.0,
                   "conf_p50": 1.0, "conf_p90": 1.0, "high_conf_frac": 1.0}
    try:
        # Reuse the cached _repo_stats for node/edge/degree data
        from groundtruth.confidence import _repo_stats
        rs = _repo_stats(conn)
        stats["node_count"] = rs.n_files * 5  # approximate: ~5 functions/file
        # Get actual counts only if _repo_stats didn't cover them
        stats["node_count"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE is_test = 0"
        ).fetchone()[0] or 0
        stats["edge_count"] = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE type IN ('CALLS','IMPORTS')"
        ).fetchone()[0] or 0
        if stats["node_count"] > 0:
            stats["avg_degree"] = stats["edge_count"] / stats["node_count"]
        if has_conf and stats["edge_count"] > 0:
            row = conn.execute(
                "SELECT COUNT(*), "
                "       SUM(CASE WHEN confidence >= 0.5 THEN 1 ELSE 0 END) "
                "FROM edges WHERE type IN ('CALLS','IMPORTS') AND confidence IS NOT NULL"
            ).fetchone()
            total_conf = row[0] or 0
            high_count = row[1] or 0
            if total_conf > 0:
                stats["high_conf_frac"] = high_count / total_conf
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
    except Exception:
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

    Research basis:
    - KGCompass (2025): 74% of bugs at 2-hop, 14.4% at 3-hop, 1.4% at 4-hop.
      With β=0.6 decay: 3-hop score = 0.216 (significant), 4-hop = 0.13 (marginal).
      Practical maximum is 3 hops.
    - RepoGraph (ICLR 2025): k=1 ego-graph is strongest; diminishing returns
      beyond k=2 for dense graphs.

    Dynamic: uses graph density + high-confidence edge fraction.
    Dense graphs with many verified edges → 2 hops (plenty of reliable paths).
    Sparse graphs OR low verified fraction → 3 hops (need more reach).
    """
    deg = stats.get("avg_degree", 0.0)
    hi_frac = stats.get("high_conf_frac", 1.0)
    if deg >= 5.0 and hi_frac >= 0.7:
        return 2  # dense + mostly verified → 2 hops enough
    if deg < 2.0:
        return 3  # very sparse → need depth
    if hi_frac < 0.4:
        return 3  # mostly speculative → need more paths to find verified ones
    return 2  # default for medium graphs


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
    # WIDE-scope additive fields (Task-2 slice 1, 2026-06-13). Populated by the
    # union-find connected-edit-set builder; ABSENT/empty on graphs with no typed
    # promote edges so today's output stays byte-identical (consumers read via
    # getattr with defaults). `edge_tiers` parallels `edges`: per chain-edge trust
    # in {'CERTIFIED','CANDIDATE','SPECULATIVE'} so the renderer can tag a
    # promote-derived scope member as CANDIDATE and an unverified name_match edge as
    # SPECULATIVE — neither laundered as a verified fact (correct-or-quiet).
    edge_tiers: tuple[str, ...] = field(default_factory=tuple)


# WIDE connected-edit-set edge scope (Task-2 slice 1). The scope builder traverses
# CALLS/IMPORTS *and* the promoted relationship edges so a file pulled into the
# edit-set ONLY by a typed promote edge (the measured aiomonitor webui/app.py win:
# directed-UNREACHABLE under CALLS-only → in-scope via a single READS edge) joins
# the connected component. These edges feed SCOPE-COMPLETENESS ONLY — they are
# NEVER added to _degree_edge_filter (degree/hub prior) or _rrf3 (file rank), so
# they cannot re-introduce the BRIEFING §4 hub-over-promotion failure. DEGRADE-SAFE:
# on current live graphs no edge carries a promoted type or `promote_%` provenance
# (measured: 0 across scanned graphs), so widening the SELECT adds zero rows and the
# union-find over CALLS/IMPORTS reproduces today's connected components.
_SCOPE_EDGE_TYPES: tuple[str, ...] = (
    "CALLS", "IMPORTS",
) + _PROMOTED_EDGE_TYPES + ("USES",)


def _scope_edge_trust(verified: bool, method: str | None, conf_f: float,
                      trust_tier: str | None = None) -> str | None:
    """Trust tier of a candidate scope edge, or None to DROP it.

    CERTIFIED   — deterministic resolution_method (a fact: import/same_file/type_flow/
                  lsp/…). CANDIDATE — a promotion-derived edge (`resolution_method LIKE
                  'promote_%'`): derived from existing facts, rendered with an explicit
                  CANDIDATE tag, never laundered as verified. SPECULATIVE — an admitted
                  name_match edge at/above the floor (``conf_f >= _NAME_MATCH_FLOOR``):
                  kept ONLY to preserve byte-identical behavior on today's graphs (the
                  old builder admitted exactly this set) and rendered with an explicit
                  ``(unverified)`` tag, never as a fact. Below the floor → None (drop).

    ADDITIVITY: admission is identical to the prior builder (``verified OR conf_f >=
    _NAME_MATCH_FLOOR``), so with no promote edges present the SAME edges form the SAME
    components as before — the CERTIFIED/CANDIDATE/SPECULATIVE split is a render tag,
    not a new filter. The new typed promote edges (CANDIDATE) only ADD reach.
    """
    if (trust_tier or "").strip().upper() == "SUPPRESSED":
        return None  # parity with the witness BFS _edge_admitted; never a scope member
    if verified:
        return "CERTIFIED"
    m = (method or "").strip().lower()
    if m.startswith("promote_"):
        return "CANDIDATE"
    if conf_f >= _NAME_MATCH_FLOOR:
        return "SPECULATIVE"
    return None


def _build_scope_chains(
    candidates: list["Candidate"],
    conn: sqlite3.Connection,
    has_conf: bool,
    max_chains: int = 3,
) -> list[ScopeChain]:
    """Extract scope chains from candidates — the connected edit-set (Task-2 slice 1).

    Union-find over the top candidate files using CALLS/IMPORTS *and* the promoted
    relationship edges (READS/WRITES/DATA_FLOW/…), admitting only CERTIFIED
    (deterministic) or CANDIDATE (promote_%) edges. The connected component a
    confident seed belongs to IS the recovered edit-set — "these N files move
    together." This generalizes the one measured ceiling-break (aiomonitor
    webui/app.py joined the edit-set via a READS edge CALLS-only could not reach).

    SCOPE-COMPLETENESS only: the typed edges never enter _rrf3 (file rank) or
    _degree_edge_filter (hub prior) — BRIEFING §4 (reach over-promotes hubs) is
    honored because following a *specific typed relationship from a confident seed*
    is not ranking-by-centrality. Correct-or-quiet: a sub-floor name_match edge
    (``conf_f < _NAME_MATCH_FLOOR``) is dropped, so a wide scope is never built on a
    guess; a promote_% edge is admitted but rendered with an explicit CANDIDATE tag,
    never laundered as a verified fact.

    PURELY ADDITIVE — admission is IDENTICAL to the prior builder (``verified OR
    conf_f >= _NAME_MATCH_FLOOR``); ``_scope_edge_trust`` only SPLITS that same
    admitted set into render tiers (CERTIFIED / CANDIDATE / SPECULATIVE) and the
    union-find reproduces the same connected components the old BFS did. DEGRADE-SAFE:
    with no promote edges present (today) only CALLS/IMPORTS rows are returned and the
    component partition is byte-identical to before; the new typed promote edges
    (CANDIDATE) only ADD reach once promotion ships. No ranking weight or score
    formula is touched — this feeds SCOPE, not rank or degree.
    """
    if len(candidates) < 2:
        return []

    top_files = [c.file_path for c in candidates[:8]]
    if not top_files:
        return []

    conf_sel = "e.confidence" if has_conf else "1.0"
    _types_in = ",".join("'" + t + "'" for t in _SCOPE_EDGE_TYPES)
    # Cross-file edges between top candidates, over the widened scope edge set. Pull
    # trust_tier so a SUPPRESSED edge is hard-excluded (parity with the witness BFS);
    # legacy schemas without the column fall back to a neutral 'SPECULATIVE' default.
    ph = ",".join("?" for _ in top_files)
    _params = tuple(top_files) + tuple(top_files)

    def _scope_rows(tier_sel: str):
        return conn.execute(
            f"""
            SELECT DISTINCT ns.file_path, nt.file_path, e.type,
                   ns.name, nt.name, {conf_sel}, e.resolution_method, {tier_sel}
            FROM edges e
            JOIN nodes ns ON e.source_id = ns.id
            JOIN nodes nt ON e.target_id = nt.id
            WHERE ns.file_path IN ({ph}) AND nt.file_path IN ({ph})
              AND ns.file_path != nt.file_path
              AND e.type IN ({_types_in})
            """,
            _params,
        ).fetchall()
    try:
        rows = _scope_rows("COALESCE(e.trust_tier, 'SPECULATIVE')")
    except sqlite3.OperationalError:
        try:
            rows = _scope_rows("'SPECULATIVE'")  # legacy: no trust_tier column
        except sqlite3.Error:
            return []
    except sqlite3.Error:
        return []

    if not rows:
        return []

    # UNION-FIND over CERTIFIED/CANDIDATE edges → connected edit-sets.
    parent: dict[str, str] = {fp: fp for fp in top_files}

    def _find(x: str) -> str:
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        # path-compress
        while parent.get(x, x) != root:
            parent[x], x = root, parent[x]
        return root

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # Edge records keyed by the (src,dst) file pair we MERGE on, kept for description
    # + per-edge trust rendering. Undirected for edit-set membership (a file pulled
    # in by an upward READS link belongs regardless of edge direction).
    edge_recs: list[tuple[str, str, str, str, float, str]] = []
    for src_fp, dst_fp, etype, src_name, dst_name, conf, method, trust_tier in rows:
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        verified = _is_verified(method)
        tier = _scope_edge_trust(verified, method, conf_f, trust_tier)
        if tier is None:
            continue  # below the name_match floor — never a scope edge
        if src_fp not in parent:
            parent[src_fp] = src_fp
        if dst_fp not in parent:
            parent[dst_fp] = dst_fp
        sym_pair = f"{src_name} → {dst_name}"
        edge_recs.append((src_fp, dst_fp, str(etype or "CALLS"), sym_pair, conf_f, tier))
        _union(src_fp, dst_fp)

    if not edge_recs:
        return []

    # Group files + the edges that connected them by component root.
    comp_files: dict[str, list[str]] = {}
    comp_edges: dict[str, list[tuple[str, str, str, str, float, str]]] = {}
    for fp in top_files:
        comp_files.setdefault(_find(fp), []).append(fp)
    for rec in edge_recs:
        comp_edges.setdefault(_find(rec[0]), []).append(rec)

    chains: list[ScopeChain] = []
    for root, files in comp_files.items():
        recs = comp_edges.get(root, [])
        if len(files) < 2 or not recs:
            continue
        chain_edges: list[tuple[str, str, str, str]] = []
        edge_tiers: list[str] = []
        desc_parts: list[str] = []
        chain_conf = 1.0
        for src, dst, etype, sym, conf_f, tier in recs:
            chain_edges.append((src, dst, etype, sym))
            edge_tiers.append(tier)
            chain_conf = min(chain_conf, conf_f)
            # TRUST-TAG (correct-or-quiet, Pillar 3): a CANDIDATE (promote_%) edge is
            # derived-from-facts; a SPECULATIVE (name_match >= floor) edge is a NAME
            # GUESS. NEITHER is a verified fact, so BOTH carry an explicit tag in the
            # rendered description; only CERTIFIED (deterministic) renders bare. This
            # makes _scope_edge_trust's contract true AT THE RENDER — a name_match scope
            # edge is never laundered as a fact. (Closes the SPECULATIVE-untagged leak
            # that no edge_tiers consumer was closing; NOT byte-identical to the prior
            # builder ON PURPOSE — the prior bare render WAS the laundering.)
            _tag = (
                " (CANDIDATE)" if tier == "CANDIDATE"
                else " (unverified)" if tier == "SPECULATIVE"
                else ""
            )
            desc_parts.append(
                f"{os.path.basename(src)} → {os.path.basename(dst)} ({sym}){_tag}"
            )
        # Preserve seed-first ordering: keep top_files order within the component.
        ordered = [f for f in top_files if _find(f) == root]
        chains.append(ScopeChain(
            files=ordered,
            edges=chain_edges,
            confidence=chain_conf,
            description="; ".join(desc_parts),
            edge_tiers=tuple(edge_tiers),
        ))

    chains.sort(key=lambda c: (-len(c.files), -c.confidence, c.files[0] if c.files else ""))
    return chains[:max_chains]




_EMBEDDER = None
_EMBEDDER_TRIED = False


class _OnnxEmbedderAdapter:
    """Adapts the deterministic ONNX EmbeddingModel (groundtruth.memory.enrich.embed,
    no torch) to the SentenceTransformer `.encode(texts)` interface that
    _semantic_score_by_file uses. By construction texts[0] is the issue (QUERY) and
    texts[1:] are file docs (PASSAGES) — preserving the E5 query/passage asymmetry a
    uniform encode would lose. This is the container-viable embedder path: light
    (~90MB onnx), fast, deps = onnxruntime + tokenizers (vs torch's ~2GB)."""

    def __init__(self, model):
        from groundtruth.memory.enrich.embed import DEFAULT_EMBED_DIM
        self._m = model
        # Width of the zero-fallback vectors when encode() is called with no texts.
        # Read the model's true dim (CHANGE 2: 768 for gte-modernbert, 384 for e5).
        self.dim = getattr(model, "dim", DEFAULT_EMBED_DIM)

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        import numpy as np
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        q = self._m.embed(texts[0], is_query=True)
        ps = self._m.embed_batch(texts[1:], is_query=False) if len(texts) > 1 else []
        return np.asarray([q, *ps], dtype=np.float32)


def _get_embedder():
    """Embedder for issue->code SEMANTIC retrieval — the bridge for cases where the
    gold shares no surface tokens with the issue (the wall grep/graph cannot cross).
    Loaded once. Tries, in order: (1) sentence-transformers (if installed); (2) the
    deterministic ONNX EmbeddingModel (container-viable, no torch — works only if
    onnxruntime + the model files under models/ are present, baked into the image);
    (3) None -> semantic signal off, deterministic 2-signal fallback. Research: dense
    passage / code retrieval (CodeBERT/UniXcoder) localizes where lexical IR fails."""
    global _EMBEDDER, _EMBEDDER_TRIED
    if _EMBEDDER_TRIED:
        return _EMBEDDER
    _EMBEDDER_TRIED = True
    # Warm ONCE here (cached via _EMBEDDER/_EMBEDDER_TRIED). The wrapper calls this
    # at task INIT — off the brief's critical path — so the 90MB onnx load never
    # cold-starts mid-brief while the agent waits. The two failures are captured so
    # the GT_REQUIRE_EMBEDDER gate can report exactly which path was unavailable
    # instead of silently zeroing the semantic ranker (the 30-task-run failure mode).
    _st_err: Exception | None = None
    _onnx_err: Exception | None = None
    # GT_FORCE_ONNX_EMBEDDER=1 skips sentence-transformers so this half uses the
    # IDENTICAL container ONNX _OnnxEmbedderAdapter (code-tuned default, e5 fallback) that
    # run_v74 uses — BRIEFING.md §5: block sentence_transformers, BOTH halves on the SAME
    # surface, or the numbers are worthless. Both halves call get_embedding_model() no-arg
    # so they resolve to the identical (model, dim) — the invariant holds across CHANGE 2.
    _force_onnx = os.environ.get("GT_FORCE_ONNX_EMBEDDER") == "1"
    # GT_REQUIRE_EMBEDDER=1 means the CONFIGURED model, full stop (ST-hole fix
    # 2026-06-09, mirrors v7_4_brief._get_model): without this, sentence-transformers
    # loaded FIRST and satisfied "required" with an ARBITRARY host model — silent
    # substitution that desyncs the two semantic halves (BRIEFING.md §2: block
    # sentence_transformers; both halves on the SAME ONNX surface). Under require,
    # the ST step is skipped: configured-ONNX-or-raise. ST stays available when off.
    _require_embedder = os.environ.get("GT_REQUIRE_EMBEDDER") == "1"
    if not _force_onnx and not _require_embedder:
        try:
            from sentence_transformers import SentenceTransformer
            # CODE-AWARE embedder (CodeSearchNet query->code; LIPI on sqllineage-557:
            # a general sentence model ranks generic files above the specific code gold).
            _EMBEDDER = SentenceTransformer(
                os.environ.get("GT_EMBED_MODEL", "flax-sentence-embeddings/st-codesearch-distilroberta-base"))
            return _EMBEDDER
        except Exception as e:
            _st_err = e
    # Container-viable ONNX path (no torch). CHANGE 2 chain: the code-tuned default
    # (gte-modernbert) ONNX → e5/384 → None. Each step loads only if onnxruntime + that
    # model's files are baked; both halves (run_v74 + localize) walk the SAME chain so they
    # stay matched (BRIEFING.md §2 — a half-on / mismatched pipeline gives worthless numbers).
    from groundtruth.memory.enrich.embed import (
        E5_DIM,
        E5_MODEL,
        _default_embed_model,
        get_embedding_model,
    )
    try:
        _m = get_embedding_model()  # code-tuned default (GT_EMBED_MODEL_NAME/DIM)
        _m._ensure_loaded()         # raises if onnxruntime / model files absent
        _EMBEDDER = _OnnxEmbedderAdapter(_m)
        return _EMBEDDER
    except Exception as e:
        _onnx_err = e
    # NO-FALLBACK under GT_REQUIRE_EMBEDDER (audit Stage-3 fix): when the run REQUIRES the
    # embedder, the CONFIGURED model (gte-modernbert) must LOAD or the run RAISES. We MUST NOT
    # silently substitute e5 here — that is the silent-substitution the audit flagged, and it
    # would also DESYNC the two halves if one loaded gte and the other e5. e5 stays available
    # ONLY for the sqlite-vec MEMORY store (which calls get_embedding_model(E5_MODEL, E5_DIM)
    # directly) and for the GRACEFUL non-proof path — NEVER as a proof-path embedder
    # fallback. (_require_embedder is read ONCE above: it now also gates the ST step,
    # so "required" can never be satisfied by an arbitrary host model.)
    if not _require_embedder:
        try:
            # Transition fallback: e5/384 if the code-tuned ONNX is absent/unloadable.
            _m5 = get_embedding_model(E5_MODEL, E5_DIM)
            _m5._ensure_loaded()
            _EMBEDDER = _OnnxEmbedderAdapter(_m5)
            return _EMBEDDER
        except Exception as e:
            _onnx_err = RuntimeError(f"code-tuned: {_onnx_err!r}; e5: {e!r}")
    _EMBEDDER = None
    # Fail-loud on a paid run: a silently-off OR silently-substituted semantic ranker collapses
    # the HIGH tier's 3-ranker agreement to 2 and poisons every localization-quality result.
    if _require_embedder:
        _configured = _default_embed_model()
        raise RuntimeError(
            f"GT_REQUIRE_EMBEDDER=1 but the CONFIGURED embedder '{_configured}' did not load "
            "(no silent e5 substitution on the proof path) — semantic ranking would be 0 or run "
            f"on the wrong model. sentence-transformers: {_st_err!r}; configured ONNX "
            f"(onnxruntime + baked model files): {_onnx_err!r}. "
            "Install onnxruntime and ship the configured model files (or sentence-transformers), "
            "or unset GT_REQUIRE_EMBEDDER. Refusing to run a degraded/substituted paid localization."
        )
    return _EMBEDDER


def _sem_passage_budget() -> int:
    """``GT_SEM_PASSAGE_BUDGET``: hard per-call ENCODE budget for
    _semantic_score_by_file (encode-blowup fix 2026-06-09; run 27249519544:
    29/113 tasks SIGKILL exit-137 during BRIEF generation on big repos). Counts
    passages actually SENT to the embedder — cache hits are free. Generous but
    FINITE default; clamped to >=1 so there is never silent infinite work.
    Malformed values fall back to the default (correct-or-quiet)."""
    default = 4096
    raw = os.environ.get("GT_SEM_PASSAGE_BUDGET")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return default


def _sem_pool_files(top_k: int) -> int:
    """``GT_SEM_POOL_FILES``: how many top pre-semantic candidates the semantic
    ranker scores (the localize call-site pool). Default ``max(6*top_k, 48)`` —
    generous headroom above the final window so the semantic signal can still
    PROMOTE a mid-pack candidate. Clamped to >= top_k so the final window is
    always fully inside the scored pool (the final-top_k ⊆ pool invariant)."""
    default = max(6 * max(top_k, 1), 48)
    raw = os.environ.get("GT_SEM_POOL_FILES")
    if raw:
        try:
            return max(int(raw), max(top_k, 1))
        except (TypeError, ValueError):
            pass
    return default


# B2 (stratum-B vocab): default-OFF body enrichment, GT_SEM_BODY-gated. The passage
# form is name+signature+behavioral-props (docstring/call_order/guards) — it lacks the
# DOMAIN VOCABULARY ("Redis", "TLS", "handshake") that lives in the body's
# identifiers/strings/comments, so behavior-described issues (stratum B) score 0. The
# vocabulary is now mined INTO graph.db at INDEX time (parser.go extractBodyChannels)
# as the string_literals/body_terms/calls property kinds; the query-time file reader
# (_body_terms) is retired. Both semantic halves read those channels through the ONE
# shared _symbol_body_map assembler below so a symbol's passage text/hash is identical
# in both halves under the flag. OFF -> name+sig+props passage, BYTE-IDENTICAL to today.


def _boilerplate_stoplist(node_terms: "dict[int, str]") -> "set[str]":
    """Boilerplate stoplist derived from the repo's OWN document-frequency distribution.

    C2b — parameter-free: a token is boilerplate iff its document frequency (# symbols
    whose body_terms contain it) exceeds ``mean + 1*std`` of the DF distribution (a
    z-score > 1). The coefficient is exactly 1 (an allowed constant); the reference
    ``mean``/``std`` come from THIS repo's own symbols — never a hardcoded tuned number,
    never a fixed "top X%" cut. Needs >= 2 distinct tokens to have a distribution; below
    that, no stoplisting (correct-or-quiet). DF distributions of code identifiers are
    heavy right-tailed (most tokens appear once), so mean+std sits well above the mass of
    rare domain tokens and catches only the genuinely ubiquitous boilerplate."""
    if not node_terms:
        return set()
    from collections import Counter
    df: "Counter[str]" = Counter()
    for tv in node_terms.values():
        for t in set(tv.split()):
            df[t] += 1
    if len(df) < 2:
        return set()
    import math
    vals = list(df.values())
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    cut = mean + std  # z > 1: distribution-derived, coefficient == 1 (allowed)
    return {t for t, c in df.items() if c > cut}


_sem_body_noop_warned = False  # C1: one-shot latch for the body-less-graph warning


def _symbol_body_map(
    conn: "sqlite3.Connection",
    body_on: bool,
    enriched_node_ids_out: "set[int] | None" = None,
) -> "dict[int, str]":
    """node_id -> the ``body_snippet`` passed to ``symbol_passage`` — the SINGLE source
    of per-symbol passage BODY, shared by BOTH semantic halves
    (``graph_localizer._assemble_symbol_passages`` and
    ``anchor_select._get_file_embeddings``). Routing both halves through this map is
    what makes a given symbol produce IDENTICAL passage text — and therefore an
    identical ``passage_hash`` -> exactly ONE shared-cache encode — under GT_SEM_BODY.

    OFF (``body_on=False``): ``" ".join`` of up to 4 behavioral props
    (docstring/call_order/guard_clause/conditional_return). This is BYTE-IDENTICAL to
    the value both halves already produced OFF (each did its own
    ``" ".join(node_body[nid])`` over the same query), so OFF stays byte-identical.

    ON: the fixed-salience template ``string_literals -> body_terms(stoplisted) ->
    docstring -> calls -> non-docstring props``. The domain-vocabulary channels
    (string_literals/body_terms) LEAD so a verbose docstring/calls list can never push
    the stratum-B signal past ``symbol_passage``'s char cap. A node with no channel
    content degrades to its OFF props. Query errors (older graph / absent table) are
    swallowed -> ON degrades to today's props (correct-or-quiet); the caller-facing
    ``nodes`` read stays outside this helper so its ``sqlite3.Error`` still fail-closes."""
    # OFF props — the byte-identical source for BOTH modes (ON uses it as the degrade
    # fallback). A missing `properties` table degrades to name+sig-only passages.
    node_body: dict[int, list[str]] = {}
    try:
        for nid, val in conn.execute(
            "SELECT p.node_id, p.value FROM properties p JOIN nodes n ON n.id=p.node_id "
            "WHERE n.is_test=0 AND p.kind IN "
            "('docstring','call_order','guard_clause','conditional_return')"):
            if nid is None:
                continue
            lst = node_body.setdefault(int(nid), [])
            if len(lst) < 4:
                lst.append(str(val))
    except sqlite3.Error:
        pass  # properties table absent -> name+signature passages only
    if not body_on:
        return {nid: " ".join(v) for nid, v in node_body.items()}

    # ON: the body-channel vocabulary + template parts, all FROM graph.db (a SEPARATE
    # query so the OFF path above is untouched).
    node_strings: dict[int, str] = {}
    node_calls_v: dict[int, str] = {}
    node_terms: dict[int, str] = {}
    node_docstring: dict[int, str] = {}
    node_props2: dict[int, list[str]] = {}  # non-docstring behavioral props (trailing)
    try:
        for nid, kind, val in conn.execute(
            "SELECT p.node_id, p.kind, p.value FROM properties p JOIN nodes n "
            "ON n.id=p.node_id WHERE n.is_test=0 AND p.kind IN "
            "('string_literals','body_terms','calls','docstring',"
            "'call_order','guard_clause','conditional_return')"):
            if nid is None:
                continue
            nid = int(nid)
            v = str(val)
            if kind == "string_literals":
                node_strings[nid] = v
            elif kind == "calls":
                node_calls_v[nid] = v
            elif kind == "body_terms":
                node_terms[nid] = v
            elif kind == "docstring":
                node_docstring.setdefault(nid, v)
            else:  # call_order / guard_clause / conditional_return
                lst = node_props2.setdefault(nid, [])
                if len(lst) < 4:
                    lst.append(v)
    except sqlite3.Error:
        pass  # new kinds absent (older graph) -> ON degrades to today's props

    # C1 (Fable 2026-07-05): GT_SEM_BODY is ON but the graph carries ZERO body-channel
    # rows (body_terms + string_literals absent) — the substrate was NOT baked with the
    # index-time body channels, so ON silently degrades to the OFF props and the flag is a
    # NO-OP that still attests "body enrichment". Warn LOUDLY (once, stderr = harness log);
    # under proof mode fail-CLOSED so a paid GT_SEM_BODY run cannot grade a body-less graph
    # as body-enriched. (Non-proof: warn + degrade, correct-or-quiet.)
    if not node_terms and not node_strings:
        global _sem_body_noop_warned
        import os as _os, sys as _sys
        if not _sem_body_noop_warned:
            _sem_body_noop_warned = True
            _sys.stderr.write(
                "[GT_WARN] GT_SEM_BODY=1 but graph has 0 body-channel rows "
                "(body_terms/string_literals absent) — semantic body enrichment is a "
                "NO-OP; rebuild the substrate with the body channels.\n")
            _sys.stderr.flush()
        if _os.environ.get("GT_PROOF_MODE") == "1" and _os.environ.get("GT_BASELINE") != "1":
            raise RuntimeError(
                "GT_SEM_BODY=1 under proof mode but graph has 0 body-channel rows "
                "(substrate not baked with body channels) — refusing to grade a body-less "
                "graph as body-enriched.")

    stop = _boilerplate_stoplist(node_terms)
    ids = (set(node_body) | set(node_strings) | set(node_calls_v)
           | set(node_terms) | set(node_docstring) | set(node_props2))
    out: dict[int, str] = {}
    for nid in ids:
        off_body = " ".join(node_body.get(nid, []))
        _terms = " ".join(t for t in node_terms.get(nid, "").split() if t not in stop)
        # G5 fixed-salience template: the domain-vocabulary channels LEAD so a verbose
        # docstring or calls list cannot truncate them out of symbol_passage's char cap.
        body = " ".join(x for x in (
            node_strings.get(nid, ""),
            _terms,
            node_docstring.get(nid, ""),
            node_calls_v.get(nid, ""),
            " ".join(node_props2.get(nid, [])),
        ) if x).strip()
        if not body:
            body = off_body  # degrade -> today's OFF props
        out[nid] = body
        if enriched_node_ids_out is not None and body and body != off_body:
            enriched_node_ids_out.add(nid)
    return out


def _assemble_symbol_passages(
    graph_db: str, want: "set[str]", body_on: bool, want_sym: bool = False,
    enriched_files_out: "set[str] | None" = None,
) -> "tuple[dict[str, list[str]], dict[str, list[str]]]":
    """Build per-file ordered per-symbol passages from graph.db (NO file I/O).

    Per-symbol passage BODY comes from the shared ``_symbol_body_map`` (the SAME source
    ``anchor_select._get_file_embeddings`` reads), so a given symbol's passage text/hash
    is identical in both halves. OFF (``body_on=False``): each passage is
    ``symbol_passage(name, sig, PROPS)`` (docstring/call_order/guard/conditional_return
    join) — BYTE-IDENTICAL to the pre-C2b code. ON: the fixed-salience body-channel
    template (see ``_symbol_body_map``). ``is_test`` symbols are excluded at source
    (leak=0). ``sqlite3.Error`` on the ``nodes`` read propagates (proof-mode fail-close)."""
    from groundtruth.memory.enrich.embed import symbol_passage

    file_passages: dict[str, list[str]] = {}
    file_symnames: dict[str, list[str]] = {}
    conn = sqlite3.connect(graph_db)
    try:
        enriched_node_ids: set[int] = set()
        body_map = _symbol_body_map(
            conn, body_on,
            enriched_node_ids if enriched_files_out is not None else None,
        )
        # The cap uses stable source identity, not graph insertion id. Relevance-first
        # selection within the cap is a separate measured concern.
        _symbol_rows = conn.execute(
            "SELECT id, file_path, name, COALESCE(signature,''), "
            "start_line, end_line FROM nodes WHERE is_test=0"
        ).fetchall()
        _symbol_rows.sort(key=lambda row: (
            _normalize(str(row[1] or "")),
            row[4] is None, int(row[4] or 0),
            row[5] is None, int(row[5] or 0),
            str(row[2] or ""), str(row[3] or ""), int(row[0] or 0),
        ))
        for nid, fp, nm, sig, _sl, _el in _symbol_rows:
            k = _normalize(fp)
            if k not in want or len(file_passages.get(k, [])) >= 80:
                continue
            body = body_map.get(int(nid), "") if nid is not None else ""
            passage = symbol_passage(nm or "", sig or "", body)
            if passage:  # correct-or-quiet: never embed a blank symbol
                file_passages.setdefault(k, []).append(passage)
                if enriched_files_out is not None and int(nid) in enriched_node_ids:
                    enriched_files_out.add(k)
                if want_sym:
                    file_symnames.setdefault(k, []).append(str(nm or ""))
    finally:
        conn.close()
    return file_passages, file_symnames


def _semantic_score_by_file(
    issue_text: str, graph_db: str, files: "Iterable[str]",
    *, symbol_scores_out: "dict[str, list[tuple[str, float]]] | None" = None,
    body_enriched_files_out: "set[str] | None" = None,
) -> dict[str, float]:
    """Semantic similarity between the issue and each candidate file's CODE CONTENT.

    CHANGE 1 — symbol-level granularity. Each non-test SYMBOL in a candidate file is
    embedded as its own short ``"{name} {signature}\\n{behavioral props}"`` passage
    (~80-token cap) and the file scores by ColBERT MaxSim over its symbols —
    ``alpha*max_i(cos_i) + (1-alpha)*mean(top_k cos_i)`` — so a file's gold function
    is not averaged into its 60 siblings (which collapsed sibling cosines to a flat
    0.80-0.84 band). Demand-scoped to the candidate ``files`` set only. Return CONTRACT
    is byte-identical: ``dict[file_path -> float]``; empty dict when no embedder.

    R1 leaf-naming bridge (correct-or-quiet, additive): the per-SYMBOL MaxSim cosines
    computed here for the FILE score were previously discarded. When ``symbol_scores_out``
    is supplied, it is populated in place with ``{file_norm: [(symbol_name, cosine), ...]}``
    so the symbol-naming stage (``v1r_brief._localization_header``) can rank WITHIN-FILE
    leaves by the same semantic signal that reached the gold file — instead of raw
    in-degree (which names the hub on behavior-described issues). The returned float
    dict is unchanged; ``symbol_scores_out`` defaults to ``None`` so every existing
    caller is byte-identical, and it stays empty whenever the embedder is off.

    ENCODE DISCIPLINE (fix 2026-06-09, gt_gt §11.2 "cache by node-content hash"):
    ``files`` iteration order is the PRIORITY order (the call site passes the
    pre-semantic candidate ranking). Vectors are content-addressed in the shared
    bounded-LRU ``embed._PASSAGE_VEC_CACHE`` (model+dim+version-keyed
    ``passage_hash``) so the SAME passage — scored again this task by the other
    semantic half, or on a later call — is never re-encoded. Fresh encodes are
    hard-capped by ``GT_SEM_PASSAGE_BUDGET``; past the budget, the lowest-priority
    files simply stay unscored (absent from the result, never a fake 0.0) and ONE
    ``[GT_SEM] passage budget hit (X/Y encoded)`` line goes to stderr — bounded
    beats killed."""
    files = list(files)
    model = _get_embedder()
    # PROOF MODE (Stage 3): a required embedder with a non-empty candidate set must
    # produce a real semantic ranking — the silent {} returns below (DB error, no
    # docs, encode exception, all-zero) hide a dark semantic ranker, which collapses
    # localize's 3-ranker agreement. Guard each, raise in proof mode only.
    from groundtruth.runtime import proof as _proof
    from groundtruth.memory.enrich.embed import (
        _PASSAGE_VEC_CACHE,
        PASSAGE_CACHE_VERSION,
        aggregate_symbol_cosines,
        model_identity,
        passage_hash,
        read_agg_params,
        symbol_passage,
    )
    # Stage 3: prove localize uses the SAME embedder identity as run_v74/v1r (model-root
    # divergence -> raise in proof mode). Wires the never-called assert_same_embedder_identity.
    _proof.assert_same_embedder_identity(graph_db, "localize")
    _proof_on = _proof.is_proof_mode() and _proof.require_embedder() and bool(files)
    if model is None:
        # FAIL-CLOSED by construction (not by relying on _get_embedder's raise winning
        # the race): under GT_REQUIRE_EMBEDDER a None localize embedder is a silent-OFF
        # semantic pipeline — refuse it. Outside proof/require it degrades quietly.
        _proof.require(
            not (_proof.is_proof_mode() and _proof.require_embedder()),
            "semantic_embedder_present",
            "GT_REQUIRE_EMBEDDER=1 but the localize embedder is None — semantic OFF",
        )
        return {}
    if not files:
        return {}
    want = {_normalize(f) for f in files}
    _want_sym = symbol_scores_out is not None
    # C2b: assemble per-symbol passages from graph.db. Read the flag at CALL time (Fable
    # #10) so a test/harness toggling GT_SEM_BODY is honoured. OFF -> name+signature+PROPS
    # passages, BYTE-IDENTICAL to the pre-C2b code; ON -> the graph.db body-channel template
    # (no file I/O — the query-time disk read is retired, so repo_root is no longer needed).
    _body_on = os.environ.get("GT_SEM_BODY", "") not in ("", "0", "false", "no")
    _enriched_files: set[str] = set()
    try:
        file_passages, file_symnames = _assemble_symbol_passages(
            graph_db, want, _body_on, _want_sym,
            _enriched_files if body_enriched_files_out is not None else None,
        )
    except sqlite3.Error as _e:
        if _proof_on:
            _proof.require(False, "semantic_db_read", str(_e))
        return {}
    # Drop files that yielded no embeddable symbol.
    file_passages = {k: v for k, v in file_passages.items() if v}
    if not file_passages:
        if _proof_on:
            _proof.require(False, "semantic_docs_present",
                           f"{len(want)} candidates but 0 symbol passages assembled from graph.db")
        return {}
    import numpy as np
    # PRIORITY ORDER = the caller's iteration order (the call site passes the
    # pre-semantic candidate ranking). The encode budget truncates from the BACK,
    # so the lowest-priority candidates lose their semantic scores first.
    order: list[str] = []
    _seen_f: set[str] = set()
    for f in files:
        k = _normalize(f)
        if k in file_passages and k not in _seen_f:
            _seen_f.add(k)
            order.append(k)
    # CONTENT-ADDRESSED CACHE + HARD ENCODE BUDGET (fix 2026-06-09). Every unique
    # passage is looked up in the shared bounded-LRU (embed._PASSAGE_VEC_CACHE,
    # model+dim+version-keyed passage_hash — gt_gt §11.2 "cache by node-content
    # hash"); only MISSES are encoded, capped at GT_SEM_PASSAGE_BUDGET. vec_by_hash
    # pins this call's vectors locally so LRU eviction can never drop one between
    # lookup and scoring.
    model_name, dim = model_identity(model)
    budget = _sem_passage_budget()
    hash_of: dict[str, str] = {}
    vec_by_hash: dict[str, "np.ndarray"] = {}
    seen_hashes: set[str] = set()
    to_encode: list[str] = []
    to_encode_hashes: list[str] = []
    for f in order:
        for p in file_passages[f]:
            h = hash_of.get(p)
            if h is None:
                h = passage_hash(p, model_name, dim, PASSAGE_CACHE_VERSION)
                hash_of[p] = h
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            cached = _PASSAGE_VEC_CACHE.get(h)
            if cached is not None:
                vec_by_hash[h] = np.asarray(cached, dtype=np.float32)
            elif len(to_encode) < budget:
                to_encode.append(p)
                to_encode_hashes.append(h)
            # else: over budget — this passage stays unscored (bounded beats killed).
    n_hits = len(vec_by_hash)
    # ONE encode over the issue (texts[0] -> QUERY) + the cache-missed passages
    # (texts[1:] -> PASSAGES); the _OnnxEmbedderAdapter preserves the e5 asymmetry.
    texts = [issue_text[:2000]] + to_encode
    try:
        embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    except Exception as _e:
        if _proof_on:
            _proof.require(False, "semantic_encode", str(_e))
        return {}
    embs = np.asarray(embs, dtype=np.float32)
    q = embs[0]
    for h, vec in zip(to_encode_hashes, embs[1:]):
        v = np.asarray(vec, dtype=np.float32)
        vec_by_hash[h] = v
        _PASSAGE_VEC_CACHE[h] = v
    n_skipped = len(seen_hashes) - n_hits - len(to_encode)
    if n_skipped > 0:
        # Correct-or-quiet: ONE line, stderr only (never agent-visible stdout).
        import sys as _sys
        print(
            f"[GT_SEM] passage budget hit ({len(to_encode)}/{len(seen_hashes)} encoded; "
            f"{n_hits} cached; {n_skipped} unique passages skipped)",
            file=_sys.stderr,
        )
    alpha, top_k = read_agg_params()
    _res: dict[str, float] = {}
    for f in order:
        cosines = []
        # R1: per-symbol (name, cosine) for the leaf-naming bridge. Index-aligned with
        # file_passages[f] via file_symnames[f]; only assembled when requested.
        _sym_pairs: list[tuple[str, float]] = []
        _names = file_symnames.get(f, []) if _want_sym else []
        for _i, p in enumerate(file_passages[f]):
            v = vec_by_hash.get(hash_of[p])
            if v is None:
                continue  # over-budget passage — score the file on what IS available
            c = float(np.dot(q, v))
            if np.isfinite(c):
                cosines.append(c)
                if _want_sym and _i < len(_names) and _names[_i]:
                    # Floor negatives at 0 (same convention as aggregate_symbol_cosines:
                    # a symbol pointing AWAY from the issue is no evidence, not negative).
                    _sym_pairs.append((_names[_i], c if c > 0.0 else 0.0))
        if not cosines:
            continue  # fully unscored (budget) — absent from the result, never a fake 0.0
        _res[f] = aggregate_symbol_cosines(cosines, alpha=alpha, top_k=top_k)
        if _want_sym and _sym_pairs:
            # Best cosine per symbol name (a name may recur — overloads/methods),
            # ranked high→low. symbol_scores_out is the out-param; mutate in place.
            _best: dict[str, float] = {}
            for _nm, _c in _sym_pairs:
                if _c > _best.get(_nm, -1.0):
                    _best[_nm] = _c
            symbol_scores_out[f] = sorted(  # type: ignore[index]
                _best.items(), key=lambda kv: (-kv[1], kv[0])
            )
    if _proof_on and _res:
        _nz = sum(1 for v in _res.values() if v and v > 0)
        _proof.require(_nz > 0, "semantic_ranks_nonzero",
                       f"all {len(_res)} semantic ranks zero/flat for {len(want)} candidates")
    if body_enriched_files_out is not None:
        body_enriched_files_out.update(_enriched_files & set(_res))
    return _res


def _localize_legacy(
    issue_text: str,
    graph_db: str,
    *,
    issue_anchors: IssueAnchors | None = None,
    max_hop: int = 3,
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
    import sys

    _content_candidate_paths: set[str] = set()
    _content_decision = "NO_EFFECT"
    _content_reason = "feature_not_reached"

    if not graph_db or not os.path.exists(graph_db):
        return LocalizerResult([], [], 0.0, False, "no_graph_db")

    if issue_anchors is None:
        try:
            issue_anchors = extract_issue_anchors(issue_text, graph_db)
        except Exception:
            issue_anchors = IssueAnchors()

    anchors = {a for a in issue_anchors.symbols if len(a) >= _MIN_ANCHOR_LEN}
    anchor_list = sorted(anchors)
    # Phase 1 (grep-floor): issue-token-node-name anchors are a TIE-BREAK HINT, not
    # the seed. Do NOT early-return when no issue token equals a node name — grep
    # recall (string match over file CONTENT, incl. data-access sites like
    # box.style['overflow']) is the seed/floor and runs below. Only bail when there
    # is neither a symbol anchor NOR a repo to grep NOR the content-BM25 leg NOR any issue
    # text for the FTS5 leg. B1's symbol_content_fts lives INSIDE graph.db, so it seeds the
    # repo-less path with no anchor (the exact stratum-B case it exists for).
    # L5 (Fable): the FTS5 leg (_fts5_candidates below) ALSO lives inside graph.db and builds
    # an in-memory index when the persisted nodes_fts is absent — so it can seed the repo-less /
    # no-anchor path with ZERO external deps. Coupling this bail to `not repo_root` alone made
    # FTS5 DEAD on that path (the carve-out named only the content leg). If the graph has nodes
    # and the issue has tokens, let FTS5 try; the `if not seeds` net below still returns
    # no_anchor_hit when it and every other leg come up empty, so nothing is over-claimed. The
    # measured/paid path (repo_root present) never hit this bail → byte-identical there.
    _fts_seedable = bool(issue_text and issue_text.strip())
    if not anchors and not repo_root and os.getenv("GT_CONTENT_LEG") != "1" and not _fts_seedable:
        return LocalizerResult([], [], 0.0, False, "no_anchor_hit")

    # Phase 1/2: the set of files GREP recalled (string match over content). This is
    # the FLOOR — no name-equality signal may demote a grep-recalled file below a
    # non-recalled one. Populated in the grep block; empty when no repo_root.
    grep_recalled: set[str] = set()
    # Per-file grep STRENGTH (distinct issue-token coverage). Drives the within-floor
    # rank fusion so a lexically-strong gold (grep #1) is not buried by structural
    # reranking — the go-cli regression. Empty when no repo_root.
    grep_score_by_file: dict[str, float] = {}

    conn = _open_ro(graph_db)
    if conn is None:
        try:
            conn = sqlite3.connect(graph_db)
        except sqlite3.Error:
            return LocalizerResult([], [], 0.0, False, "graph_open_failed")

    try:
        # SM-9a MULTI-REPO consumer: resolve the active-repo read scope ONCE. On a
        # single-repo / legacy graph this is a no-op (byte-identical). On a multi-repo
        # graph where repo_root cannot be resolved to a stored repo, FAIL CLOSED —
        # return empty (correct-or-quiet) rather than seeding/ranking candidates from
        # the WRONG repository. When resolved, the seed producers below are scoped to
        # the active repo's partition.
        _repo_scope = for_read(conn, repo_root)
        if _repo_scope.is_multi_repo and not _repo_scope.resolved:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            return LocalizerResult([], anchor_list, 0.0, False, "multi_repo_unresolved")
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

        seeds = _seed_node_rows(conn, anchors, scope=_repo_scope)

        # SEED PROVENANCE (fix 2026-06-09): ONLY these exact-name seeds — the
        # issue literally names a symbol defined in the file — may mint the
        # "defines {name} (issue symbol)" DEFINES witness below. Grep/path/FTS5
        # seeds are retrieval ENTRY POINTS (string/path/BM25 matches), recorded
        # here so the witness loop can mint an honest seed-typed witness instead
        # of a fabricated DEFINES at confidence 1.0.
        _exact_seed_ids: set[int] = {s[0] for s in seeds}
        _seed_provenance: dict[int, tuple[str, str]] = {}
        _grep_token_by_file: dict[str, str] = {}

        # PATH-TO-SEED: match issue tokens against file PATHS, not just
        # function NAMES. Closes the gap where "flex" matches layout/flex.py
        # but no function is named "flex" (function is flex_layout). Only
        # tokens that did NOT already match a function name are considered
        # (name-match seeds are stronger). Additive: can never remove seeds.
        # Research: KGCompass (2025) — the issue-mentioned entity can be a
        # MODULE, not just a function.
        terms = _issue_terms(issue_text)
        _existing_seed_files = {s[2] for s in seeds}  # normalized file paths already seeded
        try:
            _path_seeds = _path_to_seeds(conn, terms, _existing_seed_files, limit=10, scope=_repo_scope)
            if _path_seeds:
                existing_ids = {s[0] for s in seeds}
                for ps in _path_seeds:
                    if ps[0] not in existing_ids:
                        seeds.append(ps)
                        existing_ids.add(ps[0])
                        _seed_provenance[ps[0]] = (
                            "PATH_SEED", _path_match_token(ps[2], terms)
                        )
        except Exception as _path_err:
            print(
                f"[GT L1] path-to-seed: FAILED: {_path_err}",
                file=sys.stderr,
            )

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
            # Deterministic fact-set from curation_map (single source — fix
            # 2026-06-09): the prior hand-rolled ('import','same_file','lsp')
            # subset missed type_flow/verified_unique/impl_method/… so genuinely
            # verified seeds counted as unverified in the quality gate.
            _det_in_sq = ",".join("'" + m + "'" for m in sorted(_DETERMINISTIC_METHODS))
            for _, sname, sfp in seeds:
                try:
                    _v = conn.execute(
                        "SELECT "
                        "(SELECT COUNT(*) FROM edges e JOIN nodes n ON n.id = e.source_id "
                        f" WHERE n.file_path = ? AND e.resolution_method IN ({_det_in_sq})) "
                        "+ "
                        "(SELECT COUNT(*) FROM edges e JOIN nodes n ON n.id = e.target_id "
                        f" WHERE n.file_path = ? AND e.resolution_method IN ({_det_in_sq}))",
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
        # Phase 1 (grep-floor): grep recall is the SEED/FLOOR, not a quality-gated
        # supplement. Seed quality never gates grep OFF — a high name-match seed
        # quality must not suppress the string-world recall that is the whole point
        # (box.style['overflow'] in layout/*.py). Every grep-hit file mapping to a
        # graph node enters `grep_recalled` (floor membership).
        # DYNAMIC recall budget: scale grep breadth with repo SIZE (more files -> more
        # legitimate candidates to recall) and widen further when name-match seed
        # quality is below the composite midpoint (we lean harder on grep). Per-task,
        # not a fixed cap; the rails (15..60) are an operational token budget only.
        _base_limit = max(15, min(60, int(_stats.get("node_count", 0) / 60)))
        _grep_limit = _base_limit if _seed_quality >= 0.5 else int(_base_limit * 1.6)
        # B1: tracks whether the CONTENT-BM25 fallback (below) filled the lexical slot,
        # so the agreement vote names the leg honestly ("content" vs "grep").
        _content_leg_used = False
        # SM-10 FUSION (redirect 2026-07-12): the content-BM25 leg as an ALWAYS-CONSIDERED,
        # provenance-SEPARATED, MARGIN-GATED fusion leg on the grep-NON-empty path (distinct
        # from the grep-empty fallback above, so it can never double-count the grep signal).
        # Empty unless GT_CONTENT_LEG is on AND a measured GT_CONTENT_MARGIN clears the gate ->
        # byte-identical off / uncalibrated. Its per-file score seeds a distinct RRF leg below.
        _content_fusion_score: dict[str, float] = {}
        if repo_root:
            try:
                # GREENFIELD wiring (gt_gt §4, 2026-06-10): unresolved code
                # symbols (0 graph nodes — the feature TO BE BUILT) have no
                # name-match/FTS5 node to seed; the literal-token grep over
                # source is their ONLY entry into the candidate set.
                grep_seeds = _grep_to_seeds(
                    terms, repo_root, conn, max_seeds=_grep_limit,
                    priority_tokens=set(getattr(
                        issue_anchors, "unresolved_code_symbols", None) or set()),
                )
                if grep_seeds:
                    existing_ids = {s[0] for s in seeds}
                    for gs in grep_seeds:
                        grep_recalled.add(_normalize(gs[2]))
                        if gs[0] not in existing_ids:
                            seeds.append(gs)
                            existing_ids.add(gs[0])
                            _seed_provenance[gs[0]] = ("GREP_SEED", "")
                    _grep_seed_used = True
                # Grep STRENGTH per recalled file = distinct issue-token coverage
                # (the same signal grep-only ranks by). Used for within-floor rank
                # fusion. One read per recalled file (recalled set is small).
                _gtoks = [t.lower() for t in terms if len(t) >= 4]
                for _fp in grep_recalled:
                    try:
                        _txt = open(os.path.join(repo_root, _fp), encoding="utf-8",
                                    errors="ignore").read(500_000).lower()
                        _hit_toks = [t for t in _gtoks if t in _txt]
                        grep_score_by_file[_fp] = len(_hit_toks)
                        if _hit_toks:
                            # Longest matched token = the displayed grep-witness
                            # token ("grep match: {token}"). Display-only.
                            _grep_token_by_file[_fp] = max(_hit_toks, key=len)
                    except OSError:
                        grep_score_by_file[_fp] = 0
            except Exception as _grep_err:
                print(
                    f"[GT L1] grep-to-seed: FAILED: {_grep_err}",
                    file=sys.stderr,
                )

        # B1 CONTENT-BM25 FALLBACK LEG (Fable hybrid verdict; GT_CONTENT_LEG, default-off).
        # When the live-grep leg recalled NOTHING — repo_root absent (the graph.db-only /
        # MCP path), rg failed/timed out, or genuinely zero hits — the lexical slot is
        # VACANT and a behavior-described (stratum-B) task loses its content signal and can
        # bail with no floor. symbol_content_fts is a persistent BM25 index over per-symbol
        # BODY vocabulary (Go-built, test symbols excluded at source) that lives INSIDE
        # graph.db, so it serves content ranking with NO live checkout. It fires ONLY when
        # grep_recalled is empty -> MUTUALLY EXCLUSIVE with the grep leg -> it can NEVER
        # double-count the lexical signal in RRF / the agreement vote. It fills the SAME
        # slot (grep_recalled + grep_score_by_file) under a DISTINCT leg label ("content")
        # so the consensus stays honest. Default-off: the measured paid path (repo present
        # -> grep succeeds) is byte-identical; this only lights the grep-dead lane.
        if os.getenv("GT_CONTENT_LEG") == "1":
            try:
                _cfts = _content_fts_candidates(conn, terms, limit=_grep_limit, issue_text=issue_text, scope=_repo_scope)
            except Exception as _cfts_err:
                print(f"[GT L1] content-fts leg: FAILED: {_cfts_err}", file=sys.stderr)
                _cfts = []
                _content_decision = "ERROR"
                _content_reason = "content_fts_query_failed"
            if _cfts and not grep_recalled:
                # GREP-EMPTY FALLBACK (UNCHANGED): the lexical slot is VACANT, so a lossy top-K
                # BM25 hit beats no floor at all — fill the grep slot exactly as before. The
                # margin-gate does NOT apply here (there is no grep signal to protect against).
                _existing_ids_c = {s[0] for s in seeds}
                for _nid, _cname, _cfp, _cscore in _cfts:
                    grep_recalled.add(_cfp)
                    if _cscore > grep_score_by_file.get(_cfp, float("-inf")):
                        grep_score_by_file[_cfp] = _cscore
                    if _nid not in _existing_ids_c:
                        seeds.append((_nid, _cname, _cfp))
                        _existing_ids_c.add(_nid)
                        _seed_provenance[_nid] = ("CONTENT_SEED", "")
                _content_leg_used = True
                _content_candidate_paths.update(_normalize(row[2]) for row in _cfts)
                _content_decision = "APPLIED"
                _content_reason = "vacant_lexical_slot"
                print(
                    f"[GT L1] content-fts leg: filled vacant lexical slot with "
                    f"{len(_cfts)} BM25 body-content candidates",
                    file=sys.stderr,
                )
            elif _cfts:
                # GREP-NON-EMPTY FUSION (SM-10 redirect 2026-07-12): the content-BM25 leg is
                # ALWAYS considered — a DISTINCT, provenance-separated fusion leg alongside the
                # live grep leg (double-count is prevented by the separate "content" leg, NOT by
                # a grep-empty exclusion). MARGIN-GATED: it contributes a distinct RRF leg ONLY
                # when a MEASURED GT_CONTENT_MARGIN clears (a clearly-separated top body hit);
                # a weak margin OR an uncalibrated (unset) threshold -> ABSTAIN -> byte-identical
                # to the grep-only pipeline (correct-or-quiet — never body-BM25-first noise).
                _mthr = _content_margin_threshold()
                if _mthr is not None and _content_leg_margin_ok(_cfts, _mthr):
                    _existing_ids_c = {s[0] for s in seeds}
                    for _nid, _cname, _cfp, _cscore in _cfts:
                        if _cscore > _content_fusion_score.get(_cfp, float("-inf")):
                            _content_fusion_score[_cfp] = _cscore
                        if _nid not in _existing_ids_c:
                            seeds.append((_nid, _cname, _cfp))
                            _existing_ids_c.add(_nid)
                            _seed_provenance[_nid] = ("CONTENT_SEED", "")
                    _content_candidate_paths.update(_content_fusion_score)
                    _content_decision = "APPLIED"
                    _content_reason = "margin_cleared"
                    print(
                        f"[GT L1] content-fts FUSION leg: margin>={_mthr:.4f} cleared, "
                        f"{len(_content_fusion_score)} distinct body-content files fused",
                        file=sys.stderr,
                    )
                else:
                    _content_decision = "SUPPRESSED"
                    _content_reason = (
                        "margin_uncalibrated" if _mthr is None
                        else "margin_not_cleared"
                    )
            elif not _cfts and _content_decision != "ERROR":
                _content_decision = "NO_EFFECT"
                _content_reason = "no_content_fts_candidates"

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
            fts5_hits = _fts5_candidates(conn, terms, limit=50, scope=_repo_scope)
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
                        _seed_provenance[nid] = ("FTS5_SEED", str(name or ""))
                _fts5_seed_used = True
        except Exception as _fts_err:
            print(
                f"[GT L1] FTS5-to-seed: FAILED: {_fts_err}",
                file=sys.stderr,
            )

        if not seeds:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            return LocalizerResult([], anchor_list, 0.0, False, "no_anchor_hit")

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

        # ALL-POLLUTANT detection: are ALL exact-name seeds generic homonyms?
        # When yes, name-match seeds are all noise — demote them below
        # grep/path so content-match leads. Data-driven via is_seed_pollutant.
        _exact_names = [s[1] for s in seeds if s[0] in _exact_seed_ids]
        _all_seeds_pollutant = bool(_exact_names) and all(
            is_seed_pollutant(n, conn) for n in _exact_names
        )

        # IDF-scaled seed confidence (2026-06-26, BLUiR ASE 2013).
        # Non-exact seeds (path, grep, fts5) carry domain-match signal whose
        # value is inversely proportional to how many files the matched token
        # covers (IDF). A token matching 1 file is highly discriminating; a
        # token matching 500 files is noise. Continuous — no hardcoded tier
        # boundaries. Adapts to every codebase via the graph's own file count.
        #
        # confidence = FLOOR + (CEILING - FLOOR) * idf_ratio
        # idf_ratio  = log2(N / df) / log2(N)   ∈ [0, 1]
        #
        # FLOOR (0.35) = existing non-exact seed grade (grep/fts5 baseline).
        # CEILING (0.60) = between demoted-DEFINES (0.45) and verified (1.0) —
        # a maximally specific non-exact seed outranks any homonym but not a
        # unique verified name. Both are architectural constants from the
        # existing witness hierarchy, not arbitrary picks.
        _SEED_CONF_FLOOR = 0.35
        _SEED_CONF_CEILING = 0.60
        try:
            _total_graph_files = conn.execute(
                "SELECT COUNT(DISTINCT file_path) FROM nodes"
            ).fetchone()[0] or 1
        except sqlite3.Error:
            _total_graph_files = 1

        def _idf_seed_confidence(df: int) -> float:
            if _total_graph_files <= 1 or df <= 0:
                return _SEED_CONF_FLOOR
            _idf = math.log2(_total_graph_files / max(1, df)) / math.log2(
                max(2, _total_graph_files)
            )
            return _SEED_CONF_FLOOR + (_SEED_CONF_CEILING - _SEED_CONF_FLOOR) * min(
                1.0, _idf
            )

        # Precompute per-token document frequency for path seeds.
        _path_token_df: dict[str, int] = {}
        for sid, name, fp in seeds:
            _kind, _tok = _seed_provenance.get(sid, ("SEED", ""))
            if _kind == "PATH_SEED" and _tok and _tok not in _path_token_df:
                try:
                    _pats = [f"%/{_tok}.%", f"%/{_tok}/%", f"{_tok}.%"]
                    _df = 0
                    for _pp in _pats:
                        _c = conn.execute(
                            "SELECT COUNT(DISTINCT file_path) FROM nodes "
                            "WHERE file_path LIKE ?", (_pp,)
                        ).fetchone()
                        _df += (_c[0] if _c else 0)
                    _path_token_df[_tok] = max(1, _df)
                except sqlite3.Error:
                    _path_token_df[_tok] = 1

        # Precompute per-token document frequency for grep seeds.
        _grep_token_df: dict[str, int] = {}
        for sid, name, fp in seeds:
            _kind, _tok = _seed_provenance.get(sid, ("SEED", ""))
            if _kind == "GREP_SEED":
                _gt = _tok or _grep_token_by_file.get(fp, "")
                if _gt and _gt not in _grep_token_df:
                    _grep_token_df[_gt] = sum(
                        1 for _gf in grep_recalled
                        if _gt.lower() in _gf.lower()
                    ) or 1

        for sid, name, fp in seeds:
            if sid not in _exact_seed_ids:
                _kind, _tok = _seed_provenance.get(sid, ("SEED", ""))
                if _kind == "GREP_SEED" and not _tok:
                    _tok = _grep_token_by_file.get(fp, "")
                if _kind == "PATH_SEED" and _tok:
                    _seed_conf = _idf_seed_confidence(_path_token_df.get(_tok, 1))
                elif _kind == "GREP_SEED" and _tok:
                    _seed_conf = _idf_seed_confidence(
                        _grep_token_df.get(_tok, len(grep_recalled) or 1)
                    )
                else:
                    _seed_conf = _SEED_CONF_FLOOR
                witnesses_by_file.setdefault(fp, []).append(
                    Witness(
                        file_path=fp, anchor=name, edge_type=_kind,
                        direction="defines_anchor", verified=False,
                        confidence=_seed_conf, hop=0,
                        src_symbol=_tok or name, dst_symbol=name,
                    )
                )
                continue
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
            # ALL-POLLUTANT demotion (2026-06-26): when EVERY exact-name seed
            # is a pollutant (zero unique definitions — all anchors are generic
            # verbs like create/display/login), name-match seeds are ALL noise.
            # Demote them to 0.30 (below the grep/path IDF floor 0.35) so
            # file-content overlap and path-domain signals LEAD the ranking
            # instead of being buried under generic homonym seeds. Data-driven:
            # _all_seeds_pollutant is computed once from is_seed_pollutant over
            # the exact-seed set (not hardcoded per anchor). When at least ONE
            # seed is unique, pollutants stay at 0.45 (existing behavior —
            # the unique seed anchors the ranking, pollutants are tiebreakers).
            _poll_conf = 0.30 if _all_seeds_pollutant else 0.45
            witnesses_by_file.setdefault(fp, []).append(
                Witness(
                    file_path=fp, anchor=name, edge_type="DEFINES",
                    direction="defines_anchor", verified=_def_verified,
                    confidence=1.0 if _def_verified else _poll_conf,
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
                        f"AND e.type IN ('CALLS','IMPORTS') AND n.is_test = 0 "
                        # DETERMINISM (Fable L9): the sibling decay walk has an ORDER BY;
                        # this BFS did not, so anchor_of_id (first-row-wins for a neighbor
                        # reachable from >1 frontier node) and render_witness's tie order
                        # were query-plan-dependent. Fix insertion order deterministically.
                        f"ORDER BY n.file_path, {join_col}, {match_col}, e.type"
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
                        # _edge_admitted is the ONE predicate shared with the
                        # path-decay traversal (#54): admit IFF the edge is a FACT
                        # (deterministic resolution_method) OR confidence >=
                        # _dyn_conf, with trust_tier='SUPPRESSED' and stdlib-shadow
                        # name_match edges HARD-EXCLUDED. Sharing it guarantees a file
                        # can never earn path-decay mass through an edge this BFS
                        # rejects. CLAUDE.md edge-filter rule + Pillar 4
                        # (.claude/CLAUDE.md:24 "confidence-gated AT THE FILTER LEVEL").
                        if not _edge_admitted(verified, conf_f, tier, nbr_name, _dyn_conf):
                            continue

                        frontier_anchor = anchor_of_id.get(int(fr_id), "")
                        src_name = name_of_id.get(int(fr_id), frontier_anchor)

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
                                resolution_method=str(method or "").strip().lower(),
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
            return LocalizerResult([], anchor_list, 0.0, False, "no_witness",
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
                has_method=has_method, has_trust_tier=has_trust_tier,
            )
        except Exception:
            pass

        # ---- RERANK ----
        all_files = set(witnesses_by_file.keys())
        degrees = _file_degrees(conn, all_files)
        # Pre-compute role discounts for DEFINES-witness functions (Herbold 2019).
        # Checks the SPECIFIC function that matched the issue keyword, not
        # the file's largest function. Must run before conn closes.
        _role_discounts: dict[str, float] = {}
        for fp, wits in witnesses_by_file.items():
            defines_wits = [w for w in wits if w.direction == "defines_anchor"]
            if defines_wits:
                # Use the strongest DEFINES witness's anchor (the function name)
                best_def = max(defines_wits, key=lambda w: w.strength())
                _role_discounts[fp] = _role_discount_for_function(
                    conn, fp, best_def.anchor
                )
        # Downgrade DEFINES witnesses for Herbold-trivial functions
        # (SLOC<=4, fan_out=0). A trivial function matching an issue keyword
        # by name is NOT a verified structural fact — it's a lexical coincidence.
        # Demoting verified→unverified moves it from the verified bucket to
        # the unverified bucket in the sort, so structural-edge-witnessed
        # files rank above it. Research: Herbold PeerJ 2019 (90%+ NotFaulty).
        for fp, rd in _role_discounts.items():
            if rd <= 0.2:
                new_wits = []
                for w in witnesses_by_file.get(fp, []):
                    if w.direction == "defines_anchor" and w.verified:
                        new_wits.append(Witness(
                            file_path=w.file_path, anchor=w.anchor,
                            edge_type=w.edge_type, direction=w.direction,
                            verified=False, confidence=0.45,
                            hop=w.hop, src_symbol=w.src_symbol,
                            dst_symbol=w.dst_symbol,
                            resolution_method=w.resolution_method,
                        ))
                    else:
                        new_wits.append(w)
                witnesses_by_file[fp] = new_wits

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


    # TEST-TOOLING demote (vendored assertion/debug libs imported only by tests, e.g.
    # internal/testify, internal/spew — escape the vendor/ markers, lexically match an
    # issue's error/panic vocabulary, and out-rank the real gold; NEVER a feature edit
    # target). Graph-derived from IMPORTS edges (no library names), reuses the EXISTING
    # test-file demote magnitude (no new weight/threshold). Computed ONCE.
    try:
        from groundtruth.delivery.path_policy import (
            test_tooling_roots as _tt_roots_fn, is_test_tooling as _is_tt,
        )
        _tt_roots = _tt_roots_fn(graph_db) if graph_db else frozenset()
    except Exception:
        _tt_roots = frozenset()

        def _is_tt(fp: str, roots: frozenset) -> bool:
            return False

    # ── LOCALIZER FUSION V2 (RRF over per-surface rank lists) ─────────────────
    # Computed ONCE, before the per-candidate scoring loop, only when the master
    # flag is set. Produces _rrf_score_by_file[fp]; the scoring loop swaps the
    # magnitude composite for this rank-fused score. When the flag is OFF this
    # block is skipped entirely and `score` is byte-identical to the prior path.
    _rrf_score_by_file: dict[str, float] = {}
    if GT_LOC_FUSION_V2:
        # Lazy imports (only on the gated path; avoids any import-cycle on the
        # default path and keeps OFF byte-identical).
        from groundtruth.pretask.hybrid import (
            SignalHit as _SignalHit,
            reciprocal_rank_fusion as _rrf,
        )
        from groundtruth.pretask.v7_4_brief import (
            _classify_issue_lexicality as _classify_lex,
        )

        _issue_toks = sorted({t.lower() for t in terms if len(t) >= 4})

        # L1 witness/structural + L4 lex — per-file, same computation the scoring
        # loop uses (kept in lockstep so the lists reflect the real surfaces).
        _l1_witness: dict[str, float] = {}
        _l4_lex: dict[str, float] = {}
        for _fp, _wits in witnesses_by_file.items():
            _l1_witness[_fp] = max((w.strength() for w in _wits), default=0.0)
            _ss = {os.path.splitext(os.path.basename(_fp))[0].lower()}
            for _w in _wits:
                _ss.add(_w.src_symbol.lower())
                _ss.add(_w.dst_symbol.lower())
            _lh = sum(1 for t in terms if _lex_hit(t, _ss))
            _l4_lex[_fp] = min(1.0, _lh / 5.0)

        # L5 file-CONTENT IDF-coverage (INDEPENDENT of nodes.name — reads bodies).
        # Bounded to GT_LOC_CONTENT_MAXFILES candidate files. Each token weighted
        # by its IDF over the READ set (NOT raw count — the Lim#6 "calendar in 34
        # files" guard). Bodies cached for optional PRF (RM3) below.
        _l5_content: dict[str, float] = {}
        _bodies: dict[str, str] = {}
        if not GT_LOC_FUSION_EXCLUDE_INDEP and repo_root and _issue_toks:
            _cand_files = sorted(witnesses_by_file.keys())[:GT_LOC_CONTENT_MAXFILES]
            _tok_present: dict[str, list[str]] = {}  # fp -> tokens present
            _tok_df: dict[str, int] = {}             # token -> distinct files in read set
            for _fp in _cand_files:
                try:
                    _txt = open(
                        os.path.join(repo_root, _fp), encoding="utf-8",
                        errors="ignore",
                    ).read(500_000).lower()
                except OSError:
                    continue
                _bodies[_fp] = _txt
                _present = [t for t in _issue_toks if t in _txt]
                if _present:
                    _tok_present[_fp] = _present
                    for t in _present:
                        _tok_df[t] = _tok_df.get(t, 0) + 1
            _nread = max(1, len(_bodies))
            for _fp, _present in _tok_present.items():
                _l5_content[_fp] = sum(
                    math.log((_nread + 1.0) / _tok_df[t]) for t in _present
                )

            # Optional RM3/PRF (Phase 1.5, gated): expand the content query once
            # from the top content files' bodies, then recompute L5. Amplifies an
            # existing content foothold; no-op when no file matched. Reuses the
            # cached bodies (no extra disk).
            if GT_LOC_PRF and _l5_content:
                from collections import Counter as _Counter
                _top = sorted(_l5_content, key=lambda f: -_l5_content[f])[:3]
                _ctr: _Counter = _Counter()
                for _fp in _top:
                    for _bt in _re.findall(r"[a-z_]\w{3,}", _bodies.get(_fp, "")):
                        _ctr[_bt] += 1
                _expand = [
                    t for t, _ in _ctr.most_common(40)
                    if t not in _issue_toks
                ][:10]
                if _expand:
                    _edf = {
                        t: max(1, sum(1 for b in _bodies.values() if t in b))
                        for t in _expand
                    }
                    for _fp, _txt in _bodies.items():
                        _extra = sum(
                            math.log((_nread + 1.0) / _edf[t])
                            for t in _expand if t in _txt
                        )
                        if _extra:
                            _l5_content[_fp] = _l5_content.get(_fp, 0.0) + _extra

        # L6 path-IDF coverage (INDEPENDENT of nodes.name — file_path tokens).
        # df computed over the candidate path set; weight via the existing
        # _idf_seed_confidence. Pure string, no disk.
        _l6_path_idf: dict[str, float] = {}
        if not GT_LOC_FUSION_EXCLUDE_INDEP and _issue_toks:
            _cand_paths = list(witnesses_by_file.keys())
            _path_df: dict[str, int] = {}
            for t in _issue_toks:
                _path_df[t] = sum(1 for p in _cand_paths if t in p.lower())
            for _fp in _cand_paths:
                _pl = _fp.lower()
                _hit = [t for t in _issue_toks if t in _pl]
                if _hit:
                    _l6_path_idf[_fp] = sum(
                        _idf_seed_confidence(_path_df[t]) for t in _hit
                    )

        # Behavior-lead gate: on nl_gap, widen the name lists' RRF-k (~10x smaller
        # contribution) so the independent + reach lists LEAD. identifier_heavy /
        # mixed keep all lists at k=60 (regression-safe).
        _lexclass = _classify_lex(issue_text, issue_anchors, graph_db=graph_db)
        _demote = GT_LOC_BEHAVIOR_LEAD and _lexclass == "nl_gap"
        _k_name = GT_LOC_RRF_K_DEMOTE if _demote else GT_LOC_RRF_K

        def _mklist(_d: dict[str, float]) -> list:
            _items = [(f, s) for f, s in _d.items() if s > 0]
            _items.sort(key=lambda kv: (-kv[1], kv[0]))  # deterministic ties
            return [_SignalHit(file=f, score=s) for f, s in _items]

        # Name-keyed lists (k_name) and independent/reach lists (k_base). RRF is
        # additive across lists, so fusing the two groups separately and summing
        # the per-file scores == one fused pass with per-list k. (The shipped
        # reciprocal_rank_fusion takes a single k; this is how we get per-list k.)
        _name_lists = {
            "witness": _mklist(_l1_witness),         # L1
            "fts5": _mklist(_fts5_score_by_file),    # L2
            "lex": _mklist(_l4_lex),                 # L4
        }
        _indep_lists = {"path_decay": _mklist(_path_decay_by_file)}  # L3 (reach)
        if not GT_LOC_FUSION_EXCLUDE_INDEP:
            _indep_lists["content"] = _mklist(_l5_content)    # L5
            _indep_lists["path_idf"] = _mklist(_l6_path_idf)  # L6
        _maxf = max(50, len(witnesses_by_file) + 1)
        for _fh in _rrf(_name_lists, k=_k_name, max_files=_maxf):
            _rrf_score_by_file[_fh.file] = (
                _rrf_score_by_file.get(_fh.file, 0.0) + _fh.score
            )
        for _fh in _rrf(_indep_lists, k=GT_LOC_RRF_K, max_files=_maxf):
            _rrf_score_by_file[_fh.file] = (
                _rrf_score_by_file.get(_fh.file, 0.0) + _fh.score
            )

    candidates: list[Candidate] = []
    _cand_subject_pos: dict[str, int] = {}
    for fp, wits in witnesses_by_file.items():
        best_strength = max((w.strength() for w in wits), default=0.0)
        stem = os.path.splitext(os.path.basename(fp))[0].lower()
        symset = {stem}
        for w in wits:
            symset.add(w.src_symbol.lower())
            symset.add(w.dst_symbol.lower())
        lex_hits = sum(1 for t in terms if _lex_hit(t, symset))
        lex_norm = min(1.0, lex_hits / 5.0)
        deg = degrees.get(fp, 0)
        deg_norm = math.tanh(deg / _HUB_SCALE)
        subject_bonus = _subject_bonus_by_file.get(fp, 0.0)

        bm25_raw = _fts5_score_by_file.get(fp, 0.0)
        bm25_norm = (bm25_raw / _bm25_max) if _bm25_max > 0 else 0.0
        decay_raw = _path_decay_by_file.get(fp, 0.0)
        decay_norm = (decay_raw / _decay_max) if _decay_max > 0 else 0.0

        _rd = _role_discounts.get(fp, 1.0)
        _best_wit = max(wits, key=lambda w: w.strength()) if wits else None
        _best_is_defines = _best_wit and _best_wit.direction == "defines_anchor"
        _text_discount = _rd if _best_is_defines else 1.0
        _raw_score = (
            W_BM25 * bm25_norm * _text_discount
            + W_PATH_DECAY * decay_norm * _text_discount
            + W_WITNESS * best_strength * _text_discount
            + W_LEX * lex_norm * _text_discount
            + W_SUBJECT * subject_bonus * _text_discount
            + W_DEGREE * deg_norm
        )
        _weight_sum = W_BM25 + W_PATH_DECAY + W_WITNESS + W_LEX + W_SUBJECT + W_DEGREE
        if GT_LOC_FUSION_V2:
            # RRF re-fusion (rank-based, dilution-immune). Files absent from every
            # list score 0.0 here — they still sit inside the downstream grep
            # floor / tier ordering, which RRF only reorders within/above.
            score = _rrf_score_by_file.get(fp, 0.0)
        else:
            score = _raw_score / _weight_sum if _weight_sum > 0 else _raw_score
        # L3/L7 (Fable): the test/generated/tooling demote is applied as an ORDERING stratum
        # in the FINAL SORT (see _nonsource_stratum below), NOT as a score subtraction. Under
        # V2 fusion the sort keys on rank fusion — never c.score — so a `score -= 0.4/0.5` was
        # DEAD (a .pb.go / vendored testify re-entered top-k) AND it polluted the confidence
        # gate's flatness MAD with negative demote outliers (L7). Keeping c.score clean fixes
        # both; GT_TEST_TOOLING_DEMOTE still gates the tooling stratum.
        _tt_on = os.environ.get("GT_TEST_TOOLING_DEMOTE", "1") != "0"  # default ON; "0"=A/B baseline
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

    # SWERank hard-negative ordering with structural-edge refinement.
    # Four tiers:
    #   0 = verified CLOSE structural witness (CALLS/IMPORTS at hop <=1)
    #   1 = verified DISTANT structural (hop >=2) — a real edge, just far  (BUG-4: 1a)
    #   2 = verified DEFINES only — name-equality, NOT a structural fact   (BUG-4: 1b)
    #   3 = unverified witness only
    #   4 = no witness
    # BUG-4 (2026-06-15): the old tier 1 collapsed "verified DEFINES" and "verified
    # DISTANT structural" into ONE bucket, so a name-equality DEFINES tied a real
    # (if distant) CALLS/IMPORTS edge. A verified distant edge is a structural FACT;
    # a DEFINES is only a same-name coincidence (Witness.strength already caps it
    # below the weakest verified edge). Split them: distant-structural ABOVE
    # defines-only. The prior worry — "don't let a far edge beat the file defining
    # the BROKEN function" — is handled by the SUBJECT-POSITION key (BUG-2), which
    # lifts the file defining the first-named/broken function regardless of tier.
    _witness_tier = _struct_witness_tier

    # Phase 2 (GREP FLOOR): grep recall is the floor. A grep-recalled file may NEVER
    # be demoted below a non-recalled one by any name-equality signal (witness tier,
    # subject, lex). PRIMARY sort key; the existing structural ordering only reorders
    # WITHIN a floor bucket. When grep did not run (grep_recalled empty) the floor is
    # a no-op and the legacy ordering stands unchanged (backward compatible).
    #
    # Phase 4 (INJECTION_PLACEMENT): a non-recalled candidate that depth INJECTED sits
    # strictly below the floor (default) or, under interleave_short_deterministic,
    # joins the floor iff it has a <=1-hop deterministic-edge witness.
    _have_floor = bool(grep_recalled)
    # When the floor is sourced from the CONTENT leg (Fable #5), it is a LOSSY top-K
    # BM25 retrieval, NOT recall-grade string-match-over-the-repo like grep. Grep earns
    # absolute floor authority (near-exhaustive); content-BM25 does not. So in content-
    # mode a structural hop-0 witness that missed the BM25 top-K must still JOIN the
    # floor (compete on the composite rank) rather than sink below every content hit —
    # else the flag would convert precise structural localization into body-BM25-first
    # ranking. Only affects the GT_CONTENT_LEG/repo-less path; byte-identical otherwise.
    _floor_is_content = _content_leg_used

    def _grep_floor(c: Candidate) -> int:
        if not _have_floor:
            return 0
        if _normalize(c.file_path) in grep_recalled:
            return 0
        # SM-10 FUSION (redirect 2026-07-12): a MARGIN-CLEARED content-BM25 hit earns FLOOR
        # authority alongside grep, so a confident behaviour-described (stratum-B) body match
        # can COMPETE on the composite rank instead of sinking below every grep file. Gated by
        # the margin (only a clearly-separated top hit enters `_content_fusion_score`), so a
        # weak match never displaces grep — and empty on the off/uncalibrated path (no-op).
        if _content_fusion_score and _normalize(c.file_path) in _content_fusion_score:
            return 0
        if (INJECTION_PLACEMENT == "interleave_short_deterministic" or _floor_is_content) and any(
            w.verified and w.direction != "defines_anchor" and w.hop <= 1
            for w in c.witnesses
        ):
            return 0
        return 1

    # Phase 3 (EDGE-vs-STRING discriminator): a NON-recalled candidate earns a rank
    # slot only if it reaches the recalled set as an EDGE — a verified non-DEFINES
    # CALLS/IMPORTS witness (deterministic structural reach). A non-recalled file
    # whose only evidence is a DEFINES (name-equality) or unverified witness is a
    # string-world coincidence (the `overflow` validator case): verified-but-
    # irrelevant -> it sinks below everything (content-only, never displaces grep
    # recall). No-op for grep-recalled files (authority comes from recall, not depth).
    def _depth_authority(c: Candidate) -> int:
        if not _have_floor or _normalize(c.file_path) in grep_recalled:
            return 0
        # SM-10 FUSION: a margin-cleared content hit is a real body match (not a string-world
        # coincidence), so it carries authority like an edge-reached file — else it would be
        # authority-demoted below the floor it just earned. Margin-gated + empty off (no-op).
        if _content_fusion_score and _normalize(c.file_path) in _content_fusion_score:
            return 0
        has_edge_reach = any(
            w.verified and w.direction != "defines_anchor" for w in c.witnesses
        )
        return 0 if has_edge_reach else 1

    # ---- WITHIN-FLOOR RANK FUSION (fixes the go-cli regression) ----
    # Order grep-recalled candidates by the BETTER of two ranks: their grep rank
    # (lexical token coverage) and their structural rank (witness tier + score). A
    # file that is #1 in EITHER ranker floats up — so a lexically-strong gold
    # (grep #1, structurally weak among many same-named files: go-cli api.go) is no
    # longer buried by structural-only reranking, while structural wins (ts/js/py)
    # are kept. Hybrid (two independent rankers), per-task (ranks from this task's
    # own distributions), no tuned threshold. Rank fusion / CombMIN (Fox & Shaw
    # TREC-2 1994); cf. Reciprocal Rank Fusion (Cormack et al. SIGIR 2009).
    # BUG-2 (2026-06-15): the struct order tie-broke on `-c.score` then `c.file_path`.
    # When the subject-defining file (the one that DEFINES the issue's first-named,
    # i.e. BROKEN, function) and its callee both carry an equal-strength verified
    # hop-0 witness, the callee's in-degree/BM25 mass tips the composite `score` a
    # hair above the subject file (measured beets set_fields/set_parse: callee 0.6706
    # vs subject 0.6540), and an alphabetically-earlier callee path then seals the cap
    # slot. The edit target is the file defining the broken function, not its callee.
    # Lift SUBJECT POSITION above `-c.score` (relevance before composite noise), then
    # `-confidence` (best-witness strength) and `-lex_hits` before the path string —
    # the path is the LAST resort, never a relevance decider. Subject position is a
    # per-task issue-text signal (no repo/task IDs); ties on it fall back to the old
    # `-score` ordering, so this is a strict refinement of the prior key.
    _struct_order = sorted(
        candidates,
        key=lambda c: (_witness_tier(c),
                       _cand_subject_pos.get(c.file_path, 10**9),
                       -c.score, -c.confidence, -c.lex_hits, c.file_path),
    )
    _struct_rank = {id(c): i for i, c in enumerate(_struct_order)}
    _grecalled = sorted(
        (c for c in candidates if _normalize(c.file_path) in grep_recalled),
        key=lambda c: (-grep_score_by_file.get(_normalize(c.file_path), 0), c.file_path),
    )
    _grep_rank = {id(c): i for i, c in enumerate(_grecalled)}
    _BIG = 10**6
    # WITHIN-FLOOR ORDER = GREP SPINE + SPECIFIC-EVIDENCE PROMOTION.
    # Held-out lesson (flow-traced on sqllineage/privacyidea): structural reranking by
    # witness VOLUME is net-harmful vs grep — hub files with 100s of generic witnesses
    # buried the specific gold, and neither RRF nor degree-normalization fixed it. So
    # grep order is the SPINE; the graph PROMOTES a file above grep order ONLY when it
    # carries a verified, non-DEFINES (edge) witness anchored on an ISSUE symbol —
    # specific structural evidence, never popularity. Hubs have volume but no
    # issue-anchored witness, so they do not promote and grep order stands: GT MATCHES
    # grep where it has no specific signal, and only BEATS grep where the graph sees a
    # real issue-anchored edge grep cannot. SWERank retrieve->rerank applied
    # conservatively (promote-only). struct_rank is a tiebreaker, never a demoter.
    # SEMANTIC RANKER — the issue->code bridge grep and graph cannot cross. Dense
    # cosine between the issue and each candidate file's code content (names +
    # docstrings + call_order/guards from the graph). This is the signal that finally
    # localizes the cases where the gold shares NO surface tokens with the issue
    # (weasyprint overflow->block_box_layout; sqllineage MetaDataProvider->create_insert).
    # Fused with grep (lexical) and struct (graph) by 3-way Reciprocal Rank Fusion —
    # ONE pipeline, three signals. No-op (deterministic) when no embedder is available.
    #
    # ENCODE ONLY WHAT GETS RANKED (fix 2026-06-09; run 27249519544: 29/113 tasks
    # SIGKILL exit-137 during BRIEF generation on big repos): this used to score the
    # FULL pre-truncation candidate set — hundreds of witnessed files × ≤80
    # ONNX-encoded passages per task, before the top_k cut below. The semantic pool
    # is now the top _sem_pool_files(top_k) candidates under the DETERMINISTIC
    # pre-semantic ordering (grep floor, depth authority, 2-way grep+struct RRF —
    # the SAME primary keys as the final sort). Soundness of the cap: the semantic
    # term can only ADD RRF mass, pool members are exactly the candidates with the
    # most 2-way mass within each (floor, authority) stratum, and every non-pool /
    # unscored candidate falls to rank _BIG in _sem_rank — so at least pool-size
    # candidates always outrank any non-pool candidate and the final top_k window
    # is ALWAYS a subset of the pool. The agreement vote's semantic top-3 is
    # therefore computed over every candidate that can actually render. What is
    # given up: a candidate BELOW the pool cutoff can no longer be promoted into
    # the window by semantic rank alone (bounded beats killed).
    def _rrf2(c: Candidate) -> float:
        return (1.0 / (60 + _grep_rank.get(id(c), _BIG))
                + 1.0 / (60 + _struct_rank.get(id(c), _BIG)))

    _sem_pool = sorted(
        candidates,
        key=lambda c: (_grep_floor(c), _depth_authority(c), -_rrf2(c), c.file_path),
    )[:_sem_pool_files(top_k)]
    # R1: capture the per-symbol semantic scores (previously discarded) so the
    # symbol-naming stage can rank within-file leaves by the same signal. The float
    # return is unchanged; _symbol_semrank stays {} when the embedder is off.
    _symbol_semrank: dict[str, list[tuple[str, float]]] = {}
    _semantic_body_scored_files: set[str] = set()
    _sem = _semantic_score_by_file(
        issue_text, graph_db, [c.file_path for c in _sem_pool],
        symbol_scores_out=_symbol_semrank,
        body_enriched_files_out=_semantic_body_scored_files,
    )
    # Rank ONLY scored candidates: a file with no semantic score (outside the pool,
    # over the encode budget, or no embeddable symbols) must fall to rank _BIG
    # (≈0 RRF mass) — never inherit a small list-position rank from a 0.0 default.
    _sem_order = sorted(
        (c for c in candidates if _normalize(c.file_path) in _sem),
        key=lambda c: (-_sem.get(_normalize(c.file_path), 0.0), c.file_path),
    ) if _sem else []
    _sem_rank = {id(c): i for i, c in enumerate(_sem_order)}

    # ---- CONTENT-BM25 FUSION LEG (SM-10 redirect, margin-gated) ----
    # A DISTINCT, provenance-separated ranker over the body-BM25 score (grep-non-empty path).
    # Populated ONLY when GT_CONTENT_LEG is on AND the measured GT_CONTENT_MARGIN gate cleared
    # (see the content-leg block above), so `_content_fusion_score` is empty — and this rank
    # dict is empty — on every off / uncalibrated / weak-margin path => `_rrf3` and the
    # agreement vote below are byte-identical. It never fills the grep slot, so it can never
    # double-count the lexical signal; it is its OWN leg in the fusion + agreement vote.
    _content_order = sorted(
        (c for c in candidates if _normalize(c.file_path) in _content_fusion_score),
        key=lambda c: (
            -_content_fusion_score.get(_normalize(c.file_path), 0.0),
            c.file_path,
        ),
    ) if _content_fusion_score else []
    _content_rank = {id(c): i for i, c in enumerate(_content_order)}

    # ---- MULTI-SIGNAL AGREEMENT COUNT (the grep-floor build) ----
    # For each candidate, count how many of the THREE independent rankers
    # (grep / structural / semantic) place it in their OWN top-3. This is a
    # multi-signal AGREEMENT measure, not a structural-witness count: a file the
    # three independent signals all surface near the top is far more likely the
    # edit target than one only the graph (structure) witnesses. The graded
    # header consumes this so `<gt-localization confidence=X>` means "X of the 3
    # signals agree." The within-floor sort is NOT touched — this only OBSERVES
    # the rank dicts. _sem_rank is absent when no embedder is available, so the
    # max attainable agreement is 2 in the deterministic (no-semantic) path.
    # Research: Reciprocal Rank Fusion (Cormack et al. SIGIR 2009) / CombMIN
    # (Fox & Shaw TREC-2 1994) — cross-ranker agreement is a stronger relevance
    # signal than any single ranker's score.
    _TOP_N_AGREE = 3
    _agreement_by_file: dict[str, int] = {}
    _signals_by_file: dict[str, list[str]] = {}
    for c in candidates:
        _legs: list[str] = []
        if _grep_rank.get(id(c), _BIG) < _TOP_N_AGREE:
            # The lexical slot is the grep leg, OR (when grep recalled nothing) the B1
            # content-BM25 fallback that filled it — name it honestly so the consensus
            # ledger never claims "grep" agreed when no grep ran.
            _legs.append("content" if _content_leg_used else "grep")
        if _struct_rank.get(id(c), _BIG) < _TOP_N_AGREE:
            _legs.append("structural")
        if _sem_rank and _sem_rank.get(id(c), _BIG) < _TOP_N_AGREE:
            _legs.append("semantic")
        # SM-10: the margin-gated body-content leg is a DISTINCT agreement signal (a file the
        # grep, structural AND content legs all surface is stronger still). Empty rank dict on
        # the off/uncalibrated/weak-margin path => this never adds a leg (byte-identical).
        if _content_rank and _content_rank.get(id(c), _BIG) < _TOP_N_AGREE:
            _legs.append("content")
        _agree = len(_legs)
        _fnorm = _normalize(c.file_path)
        # A file may have multiple candidate rows; keep the MAX agreement seen, and
        # the NAMED legs from that same max row so signals stay in lockstep with count.
        if _agree > _agreement_by_file.get(_fnorm, -1):
            _agreement_by_file[_fnorm] = _agree
            _signals_by_file[_fnorm] = _legs

    def _rrf3(c: Candidate) -> float:
        s = 1.0 / (60 + _grep_rank.get(id(c), _BIG)) + 1.0 / (60 + _struct_rank.get(id(c), _BIG))
        if _sem_rank:
            s += 1.0 / (60 + _sem_rank.get(id(c), _BIG))
        # SM-10: the margin-gated content leg adds a DISTINCT RRF term (only when it cleared
        # the gate -> _content_rank non-empty; otherwise a no-op, so byte-identical off).
        if _content_rank:
            s += 1.0 / (60 + _content_rank.get(id(c), _BIG))
        return s

    # BUG-2 (2026-06-15): the final sort tie-broke `-_rrf3` ties on `c.file_path` ASC
    # — with the embedder off and no grep recall, `_rrf3` collapses to
    # `1/(60+struct_rank)` and rounded-score ties hand the cap slot to whichever path
    # sorts alphabetically first (a string-world coincidence). Insert relevance-bearing
    # keys BEFORE the path: best-witness strength, then lex_hits, then subject position.
    # The path string is the LAST resort. No-op when `_rrf3` already separates (the
    # common case); load-bearing only on exact ties (grep-floor-only / no-embedder).
    # GT_LOC_SEM_LED — "give semantic more power" (the embedder understands code+meaning;
    # grep/struct/bm25/path-decay/degree are lexical+graph matchers that go BLIND on a
    # behavior-described issue with no shared tokens, yet they currently decide entries[0]).
    # When the flag is set AND the embedder produced per-file scores, lead the final order
    # with the semantic MAGNITUDE (the cosine itself — NOT its RRF rank, because RRF discards
    # the very margin that is the signal); grep-floor/depth/rrf become tiebreaks. Default off
    # => _sem_led_key returns 0.0 for every candidate => the sort is byte-identical. Pairs with
    # the LINEAR (magnitude) run_v74 fusion + a code embedder; measured on OSS-60 + held-out.
    _sem_led = bool(_sem) and os.environ.get("GT_LOC_SEM_LED", "") == "1"
    # CONFIDENCE GATE (generalization fix). FORCED semantic-lead overfits: it helped the tuned
    # OSS-60 but REGRESSED unseen held-out repos (TS recall 4/5->1/3), because on cases where the
    # embedder is wrong it OVERRIDES the correct lexical/structural pick. Fix: lead with semantic
    # ONLY when the embedder DISCRIMINATES for this issue — the top per-file score clearly above
    # the field (dispersion = (top-median)/top >= threshold). On a FLAT distribution the embedder
    # "doesn't know", so fall back to grep/struct. NQC score-dispersion (Shtok et al. TOIS 2012).
    # GT_SEM_LED_MIN_DISP default 0.15; GT_SEM_LED_GATE=0 disables the gate (= old forced lead).
    if _sem_led and os.environ.get("GT_SEM_LED_GATE", "1") != "0":
        _svals = sorted((v for v in _sem.values() if v > 0), reverse=True)
        if len(_svals) >= 3:
            _stop, _smed = _svals[0], _svals[len(_svals) // 2]
            _sdisp = (_stop - _smed) / _stop if _stop > 0 else 0.0
            if _sdisp < float(os.environ.get("GT_SEM_LED_MIN_DISP", "0.15")):
                _sem_led = False  # flat -> embedder uncertain -> don't override lex/struct
        else:
            _sem_led = False  # too few scored files to judge confidence

    def _sem_led_key(c: "Candidate") -> float:
        if not _sem_led:
            return 0.0
        return -round(_sem.get(_normalize(c.file_path), 0.0), 4)

    def _nonsource_stratum(c: "Candidate") -> int:
        # L3 (Fable): test / generated / vendored-tooling files sink BELOW real source in the
        # final order — restoring the demote that the dead `score -= 0.4/0.5` no longer
        # delivered under V2 fusion. Placed AFTER the grep-recall floor + depth authority (a
        # grep-recalled file is NEVER pushed below a non-recalled one — the Phase-2 invariant)
        # but ABOVE rank fusion, so a high-RRF .pb.go / vendored testify cannot outrank real
        # source. 2 = generated (the old strongest -0.5), 1 = test/tooling (-0.4), 0 = source.
        # Same toggle: GT_TEST_TOOLING_DEMOTE=0 keeps the tooling stratum off (A/B baseline).
        fp = c.file_path
        if _is_generated(fp):
            return 2
        # W3 FIX 3 (name-index false-positive / vendored-asset demote): a VENDORED web asset
        # (static/, vendor/, contrib assets, minified .min.js/.css — path_policy.is_vendored_path)
        # can name-match ONE issue token inside a huge minified library and, on its hub in-degree,
        # out-rank real source (measured: privacyidea static/contrib/js/jquery.js — deg=736 hub,
        # lex=1 — ranked #1 while the gold eventhandler stayed at ~19). This is exactly the
        # "hub demotion" lever BRIEFING §3/§4 names as CORRECT (NOT graph-reach). The existing
        # strata only caught generated-suffix + test dirs, leaving vendored assets un-sunk.
        # Harm-reduction / correct-or-quiet: no real-source gold is ever under a vendored path,
        # so this can only remove noise. Uses the EXISTING policy predicate (no new heuristic).
        # Call-time flag (default OFF → byte-identical; kill-switch = keep OFF; ship = "1").
        if os.environ.get("GT_LOC_VENDOR_DEMOTE", "0") == "1" and _is_vendored_path_pp(fp):
            return 1
        # B-Finding2 (Fable LIPI): use the CANONICAL segment-based test predicate
        # (path_policy.is_test_path) — the same one the brief-render filter uses — NOT a local
        # substring predicate. The old local `_is_test_file` still matched "testing/" as a
        # SUBSTRING, which P11 deliberately removed from path_policy as production-ambiguous
        # (Django `django/test`, Go `testing` helpers), so the demote sank real source that the
        # render kept. Routing through is_test_path makes the demote and the render agree.
        if _is_test_path_pp(fp) or (_tt_on and _is_tt(fp, _tt_roots)):
            return 1
        return 0

    candidates.sort(
        key=lambda c: (
            _sem_led_key(c),         # GT_LOC_SEM_LED: semantic magnitude LEADS (else 0.0 no-op)
            _grep_floor(c),          # Phase 2: grep recall floor (PRIMARY when sem not led)
            _depth_authority(c),     # Phase 3: string-world non-recalled sinks
            _nonsource_stratum(c),   # L3: non-source (test/generated/tooling) sinks below source
            -_rrf3(c),               # lexical + structural + SEMANTIC rank fusion
            *_final_relevance_key(c, _cand_subject_pos),  # relevance before the path string
        )
    )
    # Diagnostic-only (env-gated, stderr, zero behavior change): per-candidate
    # component ranks so a parity audit can see WHICH ranker (grep/struct/sem)
    # buries a known-gold candidate. Off unless GT_LOCALIZE_DEBUG is set.
    if os.environ.get("GT_LOCALIZE_DEBUG"):
        import sys as _sys
        for _i, _c in enumerate(candidates[:25], start=1):
            _sys.stderr.write(
                f"[L1DBG] #{_i:2} {os.path.basename(_c.file_path):28} "
                f"grep={_grep_rank.get(id(_c), -1)} struct={_struct_rank.get(id(_c), -1)} "
                f"sem={_sem_rank.get(id(_c), -1)} floor={_grep_floor(_c)} "
                f"depth={_depth_authority(_c)} score={_c.score}\n"
            )
    candidates = candidates[:top_k]
    _path_provenance = getattr(issue_anchors, "path_provenance", None) or {}
    _explicit_issue_paths = {
        path for path in (getattr(issue_anchors, "paths", None) or set())
        if _path_provenance.get(path, "EXPLICIT_PATH") == "EXPLICIT_PATH"
    }
    candidates = [
        replace(
            candidate,
            relevance_grade=_candidate_relevance_grade(
                candidate.file_path,
                candidate.witnesses,
                trusted_anchors=anchors,
                explicit_paths=_explicit_issue_paths,
                independent_signals=_signals_by_file.get(
                    _normalize(candidate.file_path), []
                ),
            ),
        )
        for candidate in candidates
    ]
    _final_candidate_paths = {_normalize(c.file_path) for c in candidates}
    _content_terminal_paths = frozenset(
        _content_candidate_paths & _final_candidate_paths)
    _semantic_body_terminal_paths = frozenset(
        _semantic_body_scored_files & _final_candidate_paths)

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

    # WIDE edit-set telemetry (Task-2 slice 1, additive): connected-component count
    # among the top candidates under the typed-edge union-find. Each multi-file chain
    # is one component; every top candidate NOT pulled into a chain is its own
    # singleton component. 0 when there is nothing to scope. Pure observation of the
    # chains already built — no extra query, no effect on ranking.
    _n_components = 0
    if candidates:
        _chained_files = {_normalize(f) for ch in _chains for f in ch.files}
        _singletons = sum(
            1 for c in candidates if _normalize(c.file_path) not in _chained_files
        )
        _n_components = len(_chains) + _singletons

    # ---- CONFIDENCE GATE (data-derived, per-task) ----
    # Two-stage gate: (1) structural evidence check, (2) score-separation check.
    #
    # Stage 1 checks whether the top candidate has verified structural edges.
    # Stage 2 validates whether the score distribution actually discriminates
    # the top candidate from the rest — preventing the "confident-but-wrong"
    # failure where verified witnesses exist everywhere but the ranking is flat.
    #
    # Research basis for Stage 2:
    #   - NQC / QPP (Shtok et al. SIGIR 2012, revisited 2019): score stdev as
    #     a retrieval confidence proxy; low variance = flat ranking = uncertain.
    #   - Score gap (QPP since Cronen-Townsend SIGIR 2002): simplest confidence
    #     signal; calibrated per-task via MAD (not absolute threshold).
    #   - DEFINES witness ratio (data-derived, 0.80σ separation on holdout):
    #     high ratio = evidence is lexical name-match, not structural edges.
    #   - TOIS 2025 caveat: QPP thresholds don't transfer across collections,
    #     so all checks use per-task distribution metrics (MAD, CV), not absolutes.
    best = candidates[0]
    if best.relevance_grade != "VERIFIED":
        return LocalizerResult(
            candidates, anchor_list, best.confidence, False,
            f"top_relevance_{best.relevance_grade.lower()}",
            scope_chains=_chains, graph_stats=_stats,
            agreement_by_file=_agreement_by_file, signals_by_file=_signals_by_file,
            n_components=_n_components, symbol_semrank_by_file=_symbol_semrank,
            content_leg_paths=_content_terminal_paths,
            content_leg_decision=_content_decision,
            content_leg_reason=_content_reason,
            semantic_body_paths=_semantic_body_terminal_paths,
        )

    verified = [c for c in candidates if c.relevance_grade == "VERIFIED"]
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

    # ---- STAGE 2: score-separation check (QPP-inspired) ----
    # Even with verified witnesses, if the score distribution is flat the
    # system cannot distinguish #1 from #2 and should not stamp "primary
    # target." Three intrinsic signals vote; if >=2 fire, downgrade.
    if confident and len(candidates) >= 2:
        _sep_flags = 0
        _sep_parts: list[str] = []

        # Signal 1: score gap < MAD (per-task, dynamic).
        # MAD = median absolute deviation of all candidate scores.
        # If the gap between the top-2 SCORES is within 1 MAD, it's noise.
        # BUG-2 follow-through (2026-06-15): the gap is the spread between the two
        # HIGHEST RAW SCORES (`scores` is already score-sorted desc), NOT the signed
        # difference between positions 0 and 1 of `candidates`. Since BUG-2 reorders
        # `candidates` by RELEVANCE (subject position can put a slightly-lower-raw-
        # score subject file at index 0), `candidates[0].score - candidates[1].score`
        # could go NEGATIVE and spuriously trip this flatness check on a perfectly
        # separated distribution. The score distribution's flatness is a property of
        # the SCORES, independent of presentation order.
        _all_scores = [c.score for c in candidates]
        _median_s = statistics.median(_all_scores)
        _mad = statistics.median([abs(s - _median_s) for s in _all_scores])
        _gap = scores[0] - scores[1]
        if _mad > 0 and _gap < _mad:
            _sep_flags += 1
            _sep_parts.append(f"gap<MAD({_gap:.4f}<{_mad:.4f})")

        # Signal 2: defines ratio > 0.5 in top-5 (categorical evidence check).
        # DEFINES witnesses = lexical name match only, no structural edge.
        # High ratio = the localizer found names, not call/import edges.
        _top5 = candidates[:5]
        _total_wit = sum(len(c.witnesses) for c in _top5)
        _defines_wit = sum(
            1 for c in _top5 for w in c.witnesses
            if w.direction == "defines_anchor"
        )
        _def_ratio = _defines_wit / _total_wit if _total_wit > 0 else 0.0
        if _def_ratio > 0.5:
            _sep_flags += 1
            _sep_parts.append(f"defines={_def_ratio:.2f}>0.5")

        # Signal 3: coefficient of variation < 0.03 in top-5 (flat scores).
        # CV = stdev / mean; low CV = all top candidates score nearly the same.
        _top5_scores = [c.score for c in _top5]
        if len(_top5_scores) >= 2:
            _cv_mean = statistics.mean(_top5_scores)
            _cv = statistics.stdev(_top5_scores) / _cv_mean if _cv_mean > 0 else 0.0
            if _cv < 0.03:
                _sep_flags += 1
                _sep_parts.append(f"cv={_cv:.4f}<0.03")

        if _sep_flags >= 2:
            confident = False
            gate_reason = f"score_separation_fail({'+'.join(_sep_parts)})"
            import sys
            print(
                f"[GT L1] score-separation downgrade: {gate_reason}",
                file=sys.stderr,
            )

    return LocalizerResult(
        candidates, anchor_list, best.confidence, confident, gate_reason,
        scope_chains=_chains, graph_stats=_stats,
        agreement_by_file=_agreement_by_file, signals_by_file=_signals_by_file,
        n_components=_n_components, symbol_semrank_by_file=_symbol_semrank,
        content_leg_paths=_content_terminal_paths,
        content_leg_decision=_content_decision,
        content_leg_reason=_content_reason,
        semantic_body_paths=_semantic_body_terminal_paths,
    )


def localize(
    issue_text: str,
    graph_db: str,
    *,
    issue_anchors: IssueAnchors | None = None,
    max_hop: int = 3,
    top_k: int = 8,
    repo_root: str = "",
) -> LocalizerResult:
    """Compatibility projection with an isolated, fail-open vNext shadow.

    The legacy implementation owns the returned object.  Shadow computation
    receives that object only after it is complete and cannot mutate it.
    """
    result = _localize_legacy(
        issue_text,
        graph_db,
        issue_anchors=issue_anchors,
        max_hop=max_hop,
        top_k=top_k,
        repo_root=repo_root,
    )
    if os.getenv("GT_LOC_VNEXT_SHADOW", "0") == "1":
        from groundtruth.pretask.localization_vnext.shadow import (
            record_shadow_projection,
        )

        record_shadow_projection(
            issue_text=issue_text,
            repository_root=repo_root,
            graph_db=graph_db,
            legacy_result=result,
            source_projection="localize",
        )
    return result
