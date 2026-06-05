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
from dataclasses import dataclass, field

# Single source of truth for the categorical correct-or-quiet rule lives in
# curation_map: an edge is a caller FACT only when its resolution_method is
# deterministic (compiler/LSP/structurally verified); a name_match edge is NEVER
# a fact, no matter its confidence. Reuse those constants so v1r's caller
# evidence and the <gt-graph-map> obey one identical rule.
from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _NAME_MATCH_FLOOR,
    _has_columns,
)
from groundtruth.pretask.v7_4_brief import V74BriefResult, run_v74
from groundtruth.pretask.contract_map import (
    _callee_sig_args,
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
    """
    if not _has_confidence(graph_db):
        return ""
    try:
        from groundtruth.hooks.post_edit import _edge_filter_for_db

        return "AND " + _edge_filter_for_db(graph_db, alias=alias, min_conf=EDGE_CONFIDENCE_FLOOR)
    except Exception:
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
    test_mappings: list[str] = field(default_factory=list)
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
    graph_edge_count: int = 0        # candidates backed by >=1 real graph edge
    semantic_signal_count: int = 0   # candidates with a nonzero semantic/ONNX score
    structural_signal_count: int = 0 # candidates with a nonzero structural/graph-reach score
    fts5_signal_count: int = 0       # candidates scored by / entering via FTS5/BM25 (lexical)
    confidence_tier: str = "low"     # HIGH/MEDIUM/LOW from _localization_header


def _top_functions(graph_db: str, file_path: str, limit: int = MAX_FUNCTIONS_PER_FILE) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        rows = conn.execute(
            f"""
            SELECT n.name, n.signature, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id {conf_clause}
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
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
            title = row[1] if row[1] else row[0]
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
) -> list[str]:
    """Return raw function NAMES (not signatures) for contract lookup.

    Prioritizes functions whose names appear in issue_terms (bug-relevant),
    then falls back to most-referenced functions.
    """
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        # Issue-matched function names sort to the FRONT (CASE ... THEN 0) so a
        # low-ref-count issue function (e.g. set_fields) SURVIVES the LIMIT. The old
        # query ordered by ref_count THEN issue-matched in Python, so an issue
        # function outside the top-20-by-references was dropped before the match
        # could see it — the same large-file cut that hid the L3b contract (live
        # beets-5495). SWERank ICLR 2025: issue-named entities are the edit target.
        _terms = sorted({t.lower() for t in (issue_terms or set()) if t and len(t) > 2})
        if _terms:
            _ph = ",".join("?" * len(_terms))
            rows = conn.execute(
                f"""
                SELECT n.name, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id {conf_clause}
                WHERE n.file_path = ? AND n.label IN ('Function', 'Method') AND n.is_test = 0
                GROUP BY n.id
                ORDER BY CASE WHEN LOWER(n.name) IN ({_ph}) THEN 0 ELSE 1 END, ref_count DESC, n.name
                LIMIT 20
                """,
                (file_path, *_terms),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT n.name, COUNT(e.id) AS ref_count
                FROM nodes n
                LEFT JOIN edges e ON e.target_id = n.id {conf_clause}
                WHERE n.file_path = ? AND n.label IN ('Function', 'Method') AND n.is_test = 0
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

    if issue_terms:
        terms_lower = {t.lower() for t in issue_terms}
        issue_matched = [r[0] for r in rows if r[0].lower() in terms_lower]
        others = [r[0] for r in rows if r[0].lower() not in terms_lower]
        return (issue_matched + others)[:limit]

    return [row[0] for row in rows[:limit]]


def _test_files_for(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = _edge_conf_clause(graph_db)
        rows = conn.execute(
            f"""
            SELECT DISTINCT n2.file_path
            FROM nodes n1
            JOIN edges e ON e.target_id = n1.id {conf_clause}
            JOIN nodes n2 ON e.source_id = n2.id
            WHERE n1.file_path = ?
              AND n2.is_test = 1
              AND n2.file_path != n1.file_path
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


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
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ? AND nt.file_path != ? AND nt.is_test = 0
            UNION
            SELECT DISTINCT nsrc.file_path FROM nodes nt
            JOIN edges e ON e.target_id = nt.id {conf_clause}
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
            """,
            (file_path, file_path, file_path, file_path),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    scored: list[tuple[str, int]] = []
    for (neighbor,) in rows:
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
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
              AND nt.is_test = 0
            LIMIT ?
            """,
            (file_path, file_path, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


# Retained for backward-compat / external references. The caller gate below no
# longer keys off these thresholds — provenance (resolution_method), not a bare
# confidence cutoff, decides whether a caller is a fact.
CALLER_CONFIDENCE_HI = 0.9
CALLER_CONFIDENCE_LO = 0.7
MAX_CALLERS_PER_FUNC = 2


# Standard-library / builtin module names whose attribute calls (os.walk,
# os.path.join, itertools.chain, ...) get name-matched to a same-named PROJECT
# function by the indexer. A project file with a function named walk/join/split/
# open/load collides with stdlib on EVERY repo — this is general, not
# benchmark-shaped.
_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
        "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
        "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
        "functools", "operator", "collections", "subprocess", "contextlib",
    }
)


def _is_stdlib_shadow(code: str, target_name: str) -> bool:
    """True when ``code`` calls ``<stdlib_module>.<target_name>(`` — i.e. a stdlib
    attribute call the indexer name-matched to a project function of the same name
    (the proven ``os.walk`` -> ``account.walk`` false caller).

    Defends against an indexer that records such an edge with a DETERMINISTIC
    ``resolution_method`` (so the provenance gate alone would trust it). This is a
    secondary defense; the primary fix is the resolver's provenance. Repo- and
    language-agnostic.
    """
    if not code or not target_name:
        return False
    for m in _re.finditer(r"([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(", code):
        head = m.group(1).split(".")[0]
        if m.group(2) == target_name and head in _STDLIB_MODULES:
            return True
    return False


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
        # Facts-first ordering: deterministic-provenance edges sort before
        # name_match, so the over-fetch LIMIT can never cut a real fact off behind
        # a run of higher-confidence name_match rows.
        _det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
        _norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
        for fname in func_names[:2]:
            # No confidence gate in SQL — fetch cross-file callers and classify by
            # provenance in Python. Over-fetch so non-fact rows don't crowd out
            # the deterministic ones before the per-func cap.
            rows = conn.execute(
                f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, {conf_sel}, {method_sel}
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

            for caller_file, source_line, caller_name, conf, method in rows:
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
                is_fact = (method or "").strip().lower() in _DETERMINISTIC_METHODS
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
                    hint = f"{caller_file}:{source_line}"
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


def _sibling_context(graph_db: str, file_path: str, func_names: list[str]) -> str:
    """Find sibling functions in the same class/module — parallel implementations.

    General mechanism: if the candidate has function X, show what OTHER functions
    exist at the same scope level. These are the patterns to follow.
    """
    if not func_names:
        return ""
    try:
        conn = sqlite3.connect(graph_db)
        rows = conn.execute(
            """
            SELECT DISTINCT n.name
            FROM nodes n
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
              AND n.name NOT IN ({})
            ORDER BY n.start_line
            LIMIT 8
            """.format(",".join("?" * len(func_names))),
            (file_path, *func_names),
        ).fetchall()
        conn.close()
        names = [r[0] for r in rows if len(r[0]) > 2 and not r[0].startswith("_")]
        return ", ".join(names[:5]) if names else ""
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
            "AND label IN ('Function','Method') LIMIT 1",
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

    ranked = sorted(co_counts.items(), key=lambda x: -x[1])
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
            "ORDER BY count DESC LIMIT ?",
            (_norm, _norm, _norm, _norm, limit),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


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
        _witnessed = bool(getattr(entry, "witness", "")) or getattr(
            entry, "localizer_confidence", 0.0
        ) > 0.0 or float(comps.get("witness", 0.0) or 0.0) > 0.0
        if _reach > 0.0 or _witnessed:
            struct += 1

        path = entry.path
        if path not in _edge_cache:
            _edge_cache[path] = _file_has_graph_edge(graph_db, path)
        if _edge_cache[path]:
            graph_edges += 1
    return graph_edges, sem, struct, fts5


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

    # Get last 100 commits
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--name-only", "-100"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    # Parse commits — each commit block starts with a hash line, followed by file paths
    current_files: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            # End of commit block — check for co-changes
            if current_files:
                symptom_in_commit = any(f in current_files for f in symptom_files)
                if symptom_in_commit:
                    for f in current_files:
                        if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                            cochange_counts[f] = cochange_counts.get(f, 0) + 1
            current_files = []
        elif _re.match(r"^[0-9a-f]{7,12}\s", line):
            # This is a commit hash line (e.g., "abc1234 Fix bug")
            # Process previous block
            if current_files:
                symptom_in_commit = any(f in current_files for f in symptom_files)
                if symptom_in_commit:
                    for f in current_files:
                        if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                            cochange_counts[f] = cochange_counts.get(f, 0) + 1
            current_files = []
        else:
            # This is a file path
            current_files.append(line)

    # Process final block
    if current_files:
        symptom_in_commit = any(f in current_files for f in symptom_files)
        if symptom_in_commit:
            for f in current_files:
                if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                    cochange_counts[f] = cochange_counts.get(f, 0) + 1

    # Rank by co-change frequency, require >= 2
    ranked = sorted(cochange_counts.items(), key=lambda x: -x[1])
    return [
        {"path": f, "score": 0.0, "components": {"cochange": count}, "entered_via": "cochange"}
        for f, count in ranked[:max_expansion]
        if count >= 2
    ]


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
    # HI-tier rendering format from _caller_contract_for_file is
    # "func_name() in file.py:line `code`". Anchor on "() in " to avoid
    # false positives from paths containing the substring " in ".
    contract_has_func_names = "() in " in (entry.contract or "")
    contract_present = bool(entry.contract)
    has_test_mapping = bool(entry.test_mappings)

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
        _stem = os.path.splitext(os.path.basename(entry.path or ""))[0].lower()
        path_match = len(_stem) > 3 and _stem in _it

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
    if contract_present or has_test_mapping or issue_match or path_match:
        return "[WARNING]"
    return "[INFO]"


def _with_graph_map(brief: str, files: list[FileEntry], graph_db: str) -> str:
    """Append the deterministic 1-hop curation map as a sibling <gt-graph-map>
    block — callers/callees of the top shown files' focus functions.

    Returns ``brief`` unchanged when graph_db is unset, when no shown file has a
    focus function, or when no connection clears the correct-or-quiet bar
    (render_map returns '' — honest abstention, never a guess). The map obeys the
    SAME categorical rule as the caller gate: a deterministic edge renders as a
    fact; a name_match edge renders only ever as ``(unverified)``. This is the
    graph MAP the agent's own grep loop cannot cheaply build, so it orients in
    fewer turns and keeps budget for the fix.
    """
    if not graph_db or not files:
        return brief
    focus: list[tuple[str, str]] = []
    for f in files[:3]:
        for fn in (f.function_names or [])[:1]:
            if fn:
                focus.append((f.path, fn))
    if not focus:
        return brief
    try:
        from groundtruth.pretask.curation_map import build_function_map, render_map

        block = render_map(build_function_map(graph_db, focus))
    except Exception:
        return brief
    if not block:
        return brief
    return f"{brief}\n{block}"


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
) -> str:
    if not files:
        return "<gt-task-brief>\n</gt-task-brief>"

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
    else:
        # Filter out [INFO] entries — research says filter hard upstream.
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
        lines.append(line)
        # WITNESS first (primacy): the structural REASON this file is here — the
        # graph edge from an issue-anchored symbol (graph_localizer). This is the
        # localization fact the agent's grep loop cannot cheaply reconstruct
        # (e.g. "set_fields calls set_parse [CALLS]"). Rendered only when present;
        # a name_match witness carries its own "(unverified)" tag from the localizer.
        if getattr(f, "witness", ""):
            lines.append(f"   Witness: {f.witness}")
        # CONTRACT pillar first (primacy, Lost-in-the-Middle NeurIPS 2024): the
        # interface facts the agent must preserve — raises / guards / return shape.
        if f.contract_props:
            lines.append(f"   Contract: {f.contract_props}")
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
                lines.append(f"   Spec: {f.spec}")
        elif f.spec and not issue_text:
            lines.append(f"   Spec: {f.spec}")
        if f.contract:
            lines.append(f"   Callers: {f.contract}")
        if f.pattern:
            lines.append(f"   Context: {f.pattern}")
        if f.co_changes:
            lines.append(f"   Also changes: {', '.join(f.co_changes)}")
        if f.callees:
            lines.append(f"   Calls: {', '.join(f.callees)}")
        if f.test_mappings:
            lines.append(f"   Tests: {', '.join(f.test_mappings)}")

    # EXPECTED BEHAVIOR from issue text — the reporter's own spec for what the code
    # SHOULD do. Extracted from markdown sections like "### Expected Behavior",
    # "Expected:", "Should:", "The fix should". Leakage-safe (it's the issue text
    # the agent already has, curated into a concise spec).
    if issue_text:
        import re as _re_eb
        _eb_patterns = [
            _re_eb.compile(r"(?:^|\n)#{1,3}\s*Expected\s*(?:Behavior|Output|Result)s?\s*\n(.*?)(?=\n#{1,3}\s|\Z)", _re_eb.DOTALL | _re_eb.IGNORECASE),
            _re_eb.compile(r"(?:^|\n)\*\*Expected\s*(?:behavior|output|result)s?\*\*[:\s]*(.*?)(?=\n\*\*|\n#{1,3}|\Z)", _re_eb.DOTALL | _re_eb.IGNORECASE),
        ]
        for _pat in _eb_patterns:
            _eb_match = _pat.search(issue_text)
            if _eb_match:
                _eb_text = _eb_match.group(1).strip()
                if _eb_text and len(_eb_text) > 10:
                    _eb_short = _eb_text[:200].strip()
                    if _eb_short:
                        lines.append("")
                        lines.append(f"Expected behavior: {_eb_short}")
                break

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
    if graph_db and files:
        try:
            import sqlite3 as _asq
            _aconn = _asq.connect(graph_db)
            _all_spec_lines: list[str] = []
            _total_verify_budget = 5  # total assertion lines across all files
            for _verify_file in files:
                if len(_all_spec_lines) >= _total_verify_budget:
                    break
                _vf_path = _verify_file.path if hasattr(_verify_file, 'path') else str(_verify_file)
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
                            _tname_short = (tname or "?")
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
            lines.extend(_etc_lines)

    # Cross-file scope hint (Signal 1)
    if scope_files and scope_confidence in ("high", "medium"):
        scope_names = [os.path.basename(f) for f in scope_files[:3]]
        if scope_confidence == "high":
            lines.append(f"\nLikely multi-file scope: {', '.join(scope_names)}")
        else:
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
            if len(chain_files) >= 2 and chain_conf >= 0.5:
                chain_basenames = [os.path.basename(f) for f in chain_files]
                lines.append(
                    f"\nScope chain (graph-connected, check ALL): "
                    f"{' → '.join(chain_basenames)}"
                )
                if chain_desc:
                    lines.append(f"   Chain: {chain_desc}")

    # Directive ending: gated on both score gap AND top tier being [VERIFIED].
    # Internal gating only — no tier displayed in directive line.
    if not files:
        lines.append("</gt-task-brief>")
        return _with_graph_map("\n".join(lines), files, graph_db)
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
        # as EVIDENCE; never command an edit ("Edit X first") or a test run
        # ("Verify: pytest"). The file is already ranked #1 with its Tests: line.
        note = f"\nHighest-confidence candidate (graph + issue signals): {top.path}"
        if getattr(top, "witness", ""):
            note += f" — graph witness: {top.witness}"
        if top.test_mappings:
            note += f" — covering test: {top.test_mappings[0]}"
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
    return _with_graph_map("\n".join(lines), files, graph_db)


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


def _edit_target_guard(graph_db: str, file_path: str, func: str) -> tuple[str, int | None]:
    """The exact guard/conditional/return line of the edit-target function, from the
    `properties` table (GT's stored content). This is the editable spec the agent
    acts on (GenProg/APR: the change site), delivered only at HIGH confidence."""
    if not graph_db or not func:
        return "", None
    try:
        conn = sqlite3.connect(graph_db)
        try:
            base = os.path.basename(file_path)
            row = conn.execute(
                "SELECT id FROM nodes WHERE file_path LIKE ? AND name = ? "
                "AND label IN ('Function','Method') LIMIT 1",
                (f"%{base}", func),
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
                "JOIN edges e ON e.target_id = n.id GROUP BY n.file_path"
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
        rel = "calls" if getattr(w, "direction", "") == "calls_anchor" else "called by"
        return f"{w.src_symbol} {rel} {w.dst_symbol} [{w.edge_type}]"
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
    return len({
        (
            getattr(w, "direction", ""),
            getattr(w, "src_symbol", ""),
            getattr(w, "dst_symbol", ""),
            getattr(w, "edge_type", ""),
        )
        for w in (witnesses or [])
        if (getattr(w, "anchor", "") or "").lower() == fl
        and getattr(w, "direction", "") != "defines_anchor"
    })


def _localization_header(loc, graph_db: str, issue_text: str) -> str:
    """Confidence-graded localization block, PREPENDED to the brief.

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
        return ""
    anchors = {(a or "").lower() for a in (getattr(loc, "anchor_symbols", None) or [])}
    cands = loc.candidates

    def _issue_edges(c):
        # verified, non-DEFINES (structural edge) witnesses descended from an issue anchor
        return [
            w for w in c.witnesses
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
    _evidenced = sum(1 for c in cands if c.has_verified_witness) or 3
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
        c for c in cands
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
        if _high_func_support(tgt.witnesses, func) >= 2:
            line_txt, line_no = _edit_target_guard(graph_db, tgt.file_path, func)
            out = ['<gt-localization confidence="high">',
                   f"Edit target: {tgt.file_path} :: {func}"]
            if line_txt:
                loc_s = f"  [L{line_no}]" if line_no else ""
                out.append(f"  guard/return to update: {line_txt}{loc_s}")
            # reason MUST justify THIS edit target — render the witness that CHOSE
            # `func` (the max-strength issue edge), not an arbitrary other witness on
            # the file. (Avenue-2 fix: top.render_witness() previously picked an
            # unrelated edge, so "Edit import_files / reason: _parse_logfiles called
            # by _paths_from_logfile" disagreed with itself.)
            wr = _render_witness_line(w)
            if wr:
                out.append(f"  reason: {wr}")
            out.append("</gt-localization>")
            return "\n".join(out)
        # weak function anchor (<2 converging structural witnesses) -> fall through to
        # the MEDIUM candidate list below (agent reasons over the file's functions).

    # ---- MEDIUM vs LOW is now driven by agreement too: >=1 signal agrees ->
    # MEDIUM (a named candidate set worth reasoning over); 0 signals agree -> LOW
    # (region-level / option list, agent confirms with grep). The region path
    # below is the LOW rendering; it only fires when agreement is absent. ----
    _low_tier = _top_agreement < 1

    # ---- LOW (region): no signal agreement AND the shown candidates share an
    # INFORMATIVE common region (a real sub-module, >=2 path components) — summarise
    # by region rather than naming a wrong file. The "many scattered files -> show the
    # region" path. If the only shared prefix is the repo root, region is
    # uninformative and we fall through to the flat option list instead. ----
    region = _common_region([c.file_path for c in shown])
    region_informative = region.count("/") >= 1  # >=2 path components
    if _low_tier and region_informative and len({os.path.dirname(c.file_path) for c in shown}) > 1:
        out = ['<gt-localization confidence="low">',
               f"Region: {region}/ — candidate edit targets (reason over these, confirm with grep):"]
        for i, c in enumerate(shown, 1):
            out.append(f"  {i}. {c.file_path}")
        out.append("</gt-localization>")
        return "\n".join(out)

    # ---- MEDIUM / LOW flat option set: a cluster with no HIGH winner -> flat option
    # set (dynamic K), each with its issue-relevant functions; the agent reasons +
    # picks. The confidence LABEL is agreement-driven: >=1 signal agrees -> "medium",
    # 0 signals agree -> "low" (this is the LOW rendering when the region above was
    # uninformative). Keeps the tier == "X signals agree" contract end-to-end. ----
    _tier_label = "low" if _low_tier else "medium"
    out = [f'<gt-localization confidence="{_tier_label}">',
           "Candidate edit targets (reason over these):"]
    for i, c in enumerate(shown, 1):
        fs = _defines_funcs(c)
        tail = f" — {', '.join(fs[:3])}" if fs else ""
        out.append(f"  {i}. {c.file_path}{tail}")
    out.append("</gt-localization>")
    return "\n".join(out)


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
    # Density check: if graph is too sparse, graph signals are noise — use BM25 only
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
                    "W_SEM": 0.0,
                    "W_LEX": 0.70,
                    "W_REACH": 0.0,
                    "W_PROX": 0.0,
                    "W_HUB": 0.0,
                    "W_COMMIT": 0.0,
                    "W_PATH": 0.45,
                }
        except Exception:
            pass

    v74 = run_v74(
        issue_text,
        repo_root,
        graph_db,
        bug_id=bug_id,
        repo=repo,
        gold_files=gold_files,
        ablation="C",
        k_anchor=3,
        k_sem_top=10,
        tau_anchor=0.20,
        max_depth=3,
        min_confidence=EDGE_CONFIDENCE_FLOOR,
        weights=weights,
        focus_size=max_files,
    )

    if not v74.ranked_full:
        return V1RBriefResult(
            files=[],
            brief_text="<gt-task-brief>\n</gt-task-brief>",
            token_estimate=4,
            v74_result=v74,
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
    }

    def _is_test_file(path: str) -> bool:
        bn = os.path.basename(path)
        name_no_ext = os.path.splitext(bn)[0]
        return (
            bn.startswith("test_")
            or bn.startswith("tests_")
            or bn.endswith("_test.py")
            or bn.endswith("_test.go")
            or bn.endswith(".test.ts")
            or bn.endswith(".test.js")
            or bn.endswith(".spec.ts")
            or bn.endswith(".spec.js")
            or name_no_ext.endswith("Test")      # Java: UserServiceTest.java
            or name_no_ext.startswith("Test")     # Java: TestUserService.java
            or bn.endswith("_test.rs")            # Rust: foo_test.rs
            or "/test/" in path
            or "/tests/" in path
            or "/test_" in path
            or "/__tests__/" in path              # JS/React convention
        )

    top_records = [
        r
        for r in top_records
        if os.path.basename(r.get("path", "")) not in _NON_SOURCE
        and os.path.splitext(r.get("path", ""))[1].lower() not in _NON_SOURCE_EXTS
        and not _is_test_file(r.get("path", ""))
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
            bn = os.path.basename(r.get("path", ""))
            ext = os.path.splitext(bn)[1].lower()
            if bn not in _NON_SOURCE and ext not in _NON_SOURCE_EXTS:
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
            from groundtruth.pretask.anchors import extract_issue_anchors as _eia
            import json as _json_anch
            _anchors_obj = _eia(issue_text, graph_db)
            try:
                with open("/tmp/gt_issue_anchors.json", "w", encoding="utf-8") as _af:
                    _json_anch.dump({
                        "symbols": sorted(_anchors_obj.symbols),
                        "paths": sorted(_anchors_obj.paths),
                        "test_names": sorted(_anchors_obj.test_names),
                        "title_symbols": sorted(getattr(_anchors_obj, "title_symbols", set())),
                        "code_symbols": sorted(getattr(_anchors_obj, "code_symbols", set())),
                    }, _af)
            except OSError:
                pass  # non-container / read-only /tmp (e.g. unit tests) — no consumer
            _loc = localize(issue_text, graph_db, top_k=8, issue_anchors=_anchors_obj,
                           repo_root=repo_root)
        except Exception:
            _loc = None
    if _loc and _loc.candidates:
        _existing = {str(r.get("path", "")) for r in top_records}
        _existing_norm = {p.replace("\\", "/").lstrip("./").lstrip("/") for p in _existing}
        _promoted: list[dict] = []
        for _ci, cand in enumerate(_loc.candidates):
            cf = cand.file_path
            _witness_by_file[cf] = cand.render_witness()
            _witness_verified_by_file[cf] = cand.has_verified_witness
            _loc_conf_by_file[cf] = cand.confidence
            _loc_rank_by_file[cf] = _ci
            bn = os.path.basename(cf)
            ext = os.path.splitext(bn)[1].lower()
            if bn in _NON_SOURCE or ext in _NON_SOURCE_EXTS:
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
        _verified_promoted = [
            p for p in _promoted if _witness_verified_by_file.get(p["path"])
        ]
        _unverified_promoted = [
            p for p in _promoted if not _witness_verified_by_file.get(p["path"])
        ]
        # Also reorder EXISTING records: a lexical record that the localizer
        # verified-witnessed should sort ahead of a witness-less one.
        def _is_verified_witnessed(rec: dict) -> bool:
            p = str(rec.get("path", ""))
            pn = p.replace("\\", "/").lstrip("./").lstrip("/")
            return bool(
                _witness_verified_by_file.get(p) or _witness_verified_by_file.get(pn)
            )

        _existing_verified = [r for r in top_records if _is_verified_witnessed(r)]
        _existing_rest = [r for r in top_records if not _is_verified_witnessed(r)]

        # Order ALL verified-witnessed records (promoted + already-present) by the
        # LOCALIZER's own rank, not by which bucket they fell in. Without this,
        # importer.py (localize #1) lands behind query.py/db.py (localize #2/#4)
        # purely because those were absent from the base lexical set and it wasn't.
        def _loc_rank(rec: dict) -> int:
            p = str(rec.get("path", ""))
            pn = p.replace("\\", "/").lstrip("./").lstrip("/")
            r = _loc_rank_by_file.get(p)
            if r is None:
                r = _loc_rank_by_file.get(pn)
            return r if r is not None else 10**6

        _all_verified = sorted(
            _verified_promoted + _existing_verified, key=_loc_rank
        )
        top_records = _all_verified + _existing_rest + _unverified_promoted

        # GUARANTEE: every verified-witnessed localizer candidate appears in
        # the rendered brief (not dropped by MAX_FILES cut). The agent needs
        # to see graph connections (callers/callees) to navigate to the gold
        # file. GT curates the graph map; the agent navigates.
        # If a verified candidate is in the localizer but ranked below
        # MAX_FILES in top_records, inject it into the top set.
        _rendered_paths = {str(r.get("path", "")) for r in top_records[:max(max_files, 5)]}
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
                    JOIN edges e ON e.source_id = n1.id {_conf_clause}
                    JOIN nodes n2 ON e.target_id = n2.id
                    WHERE n1.file_path = ? AND n2.file_path != ? AND n2.is_test = 0
                    UNION
                    SELECT DISTINCT n1.file_path FROM nodes n2
                    JOIN edges e ON e.target_id = n2.id {_conf_clause}
                    JOIN nodes n1 ON e.source_id = n1.id
                    WHERE n2.file_path = ? AND n1.file_path != ? AND n1.is_test = 0
                    """,
                    (fp, fp, fp, fp),
                ).fetchall()
                for (neighbor,) in rows:
                    if neighbor in _existing_paths:
                        continue
                    bn = os.path.basename(neighbor)
                    ext = os.path.splitext(bn)[1].lower()
                    if bn in _NON_SOURCE or ext in _NON_SOURCE_EXTS:
                        continue
                    _neighbor_candidates.append(
                        {
                            "path": neighbor,
                            "score": rec.get("score", 0) * 0.8,
                            "components": {"path": 0.0},
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
                    "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id GROUP BY n.file_path"
                ).fetchall()
            ]
            if all_degrees:
                p80 = sorted(all_degrees)[int(len(all_degrees) * 0.8)]
                if p80 > 0:
                    top_paths = [str(r.get("path", "")) for r in top_records[:5]]
                    top_degrees = []
                    for p in top_paths:
                        row = conn.execute(
                            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id WHERE n.file_path = ?",
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

    _words = set(w.lower() for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text) if len(w) > 3)

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
                            "AND label IN ('Function', 'Method') AND is_test = 0 LIMIT 10",
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
                return 0 if (
                    _witness_verified_by_file.get(p)
                    or _witness_verified_by_file.get(pn)
                ) else 1

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

    entries: list[FileEntry] = []
    for rec in top_records:
        path = str(rec.get("path", ""))
        score = float(rec.get("score", 0.0))
        funcs = _top_functions(graph_db, path)
        tests = _test_files_for(graph_db, path)
        neighbors = _issue_relevant_neighbors(
            graph_db,
            path,
            repo_root,
            _words,
        )
        func_names = _top_function_names(graph_db, path, issue_terms=_words)
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
        _wit_ver = bool(
            _witness_verified_by_file.get(path) or _witness_verified_by_file.get(_pn)
        )
        _wit_conf = _loc_conf_by_file.get(path) or _loc_conf_by_file.get(_pn) or 0.0
        # v74 anchor proximity for this candidate (edge-independent issue-subject
        # signal) — carried onto the FileEntry so _entry_confidence_tier can keep an
        # anchor-matched file out of the [INFO] drop (BUG-3). Records are dicts with a
        # `components` sub-dict; fall back to a flat key, then 0.0.
        _aprox = float(
            (rec.get("components") or {}).get("anchor_prox", rec.get("anchor_prox", 0.0))
            or 0.0
        )
        entries.append(
            FileEntry(
                path=path,
                score=score,
                functions=funcs,
                test_mappings=tests,
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
            )
        )

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
            else:
                _scope_rows = _sc.execute(
                    """SELECT DISTINCT nsrc.file_path, '' as res, 0.5 as conf
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
                       LIMIT 10""",
                    (_top_path, _top_path),
                ).fetchall()
            _sc.close()
            _sc = None

            _distinct_files = list(dict.fromkeys(r[0] for r in _scope_rows))
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

    _scores = [r.get("score", 0.0) for r in top_records[: len(entries)]]
    _scope_chains = getattr(_loc, "scope_chains", []) if _loc else []
    # PREPEND the confidence-graded localization header (Agentless hierarchical
    # localize: granularity scales with research-backed structural confidence). When
    # it fires it OWNS the localization steer, so the brief's legacy singular
    # "highest-confidence candidate" line is suppressed (no contradictory steers).
    _loc_header = _localization_header(_loc, graph_db, issue_text)
    _emit_old = _loc_header == ""

    def _render():
        return render_brief(
            entries,
            scores=_scores,
            scope_files=_scope_files,
            scope_confidence=_scope_confidence,
            scope_chains=_scope_chains,
            issue_text=issue_text,
            graph_db=graph_db,
            emit_confident_line=_emit_old,
        )

    brief_text = _render()
    tok = _estimate_tokens((_loc_header + "\n" + brief_text) if _loc_header else brief_text)

    # Decouple localization BREADTH from the evidence token budget. The delivered
    # candidate list (.files) keeps the full rank-ordered localization set; only the
    # rendered EVIDENCE bodies in brief_text are trimmed to the token rail. Before
    # this, the trim popped entries -> .files, gutting localization to 1-2 files and
    # dropping golds the localizer ranked #0-#5 (proven on the held-out sweep:
    # geopandas-3226 gold @rank0 and sqllineage-557 @rank5 vanished from .files even
    # though the ranker placed them at/near the top; delivered Recall@5 fell to 0.40
    # vs the bare localizer's 0.60 = grep parity). The token budget governs how much
    # per-file evidence the agent reads, NOT which files it is told to consider.
    _loc_files = list(entries)
    while tok > max_brief_tokens and len(entries) > 1:
        entries = entries[:-1]
        _scores = _scores[: len(entries)]
        brief_text = _render()
        tok = _estimate_tokens((_loc_header + "\n" + brief_text) if _loc_header else brief_text)

    if _loc_header:
        brief_text = _loc_header + "\n" + brief_text

    # --- L1 signal-provenance counts (observability; no ranking effect) ---
    # Count over the DELIVERED candidate set (.files == _loc_files[:max_files]).
    # Align each delivered entry to its top_records dict (carrying run_v74
    # `components`) by path so semantic/structural/fts5 contributions are read
    # from the ACTUAL signals computed during localization, not re-derived.
    _delivered = _loc_files[:max_files]
    _rec_by_path: dict[str, dict] = {}
    for _r in top_records:
        _rp = str(_r.get("path", ""))
        if _rp and _rp not in _rec_by_path:
            _rec_by_path[_rp] = _r
    _aligned_records = [_rec_by_path.get(e.path, {}) for e in _delivered]
    try:
        _ge, _sem_c, _struct_c, _fts5_c = _l1_signal_counts(
            graph_db, _delivered, _aligned_records
        )
    except Exception:
        _ge = _sem_c = _struct_c = _fts5_c = 0
    _conf_tier = _tier_from_loc_header(_loc_header)

    result = V1RBriefResult(
        files=_delivered,
        brief_text=brief_text,
        token_estimate=_estimate_tokens(brief_text),
        v74_result=v74,
        graph_edge_count=_ge,
        semantic_signal_count=_sem_c,
        structural_signal_count=_struct_c,
        fts5_signal_count=_fts5_c,
        confidence_tier=_conf_tier,
    )

    # Structured telemetry: emit L1 candidates as JSON for wrapper to parse
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        try:
            import json as _json

            l1_items = []
            for entry in entries:
                # confidence_score now reflects the GRAPH-TRAVERSAL witness
                # strength (graph_localizer) when this file was witnessed, falling
                # back to the v74 lexical score otherwise. This is the fix for the
                # gt_run_summary l1_confidence_score=0.0 symptom: a witnessed top
                # candidate (importer.py) now reports its real structural
                # confidence instead of the lexical 0.0.
                _conf = (
                    entry.localizer_confidence
                    if entry.localizer_confidence > 0
                    else entry.score
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
                        "witness": entry.witness,
                        "source": "graph_traversal" if entry.witness else "graph_db",
                        "reason": _reason,
                        "text": ", ".join(entry.functions[:3]) if entry.functions else "",
                    }
                )
            structured = {
                "candidates": l1_items,
                "candidate_count": len(entries),
                # Provenance counts (same definitions as the V1RBriefResult fields):
                # a candidate counts toward a signal iff that signal contributed a
                # nonzero score / a real graph edge exists. These let a fail-closed
                # gate prove the brief is multi-signal, not lexical-only/hollow.
                "graph_edge_count": _ge,
                "semantic_signal_count": _sem_c,
                "structural_signal_count": _struct_c,
                "fts5_signal_count": _fts5_c,
                "confidence_tier": _conf_tier,
                # legacy proxy (callees present) kept for back-compat readers
                "neighbor_present_count": sum(1 for e in entries if e.callees),
                "test_edge_count": sum(1 for e in entries if e.test_mappings),
                "signature_count": sum(1 for e in entries if e.functions),
                "witnessed_count": sum(1 for e in entries if e.witness),
                "verified_witness_count": sum(1 for e in entries if e.witness_verified),
                "warnings": [],
                "abstain_reason": None,
            }
            if not entries:
                structured["abstain_reason"] = "no_candidates"
            with open("/tmp/gt_l1_structured.json", "w") as _f:
                _json.dump(structured, _f)
        except Exception:
            pass

    return result
