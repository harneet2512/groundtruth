"""Post-edit hook v5 -- graph.db-driven evidence with priority-ordered output.

Called by OpenHands PostToolUse hook on file_editor operations.
Priority order (stop when 300 tokens / ~1200 chars reached):
  1. Caller CODE lines (from graph.db edges.source_line -> read actual line from file)
  2. Sibling function pattern (from graph.db parent_id -> read sibling body snippet)
  3. Signature + return type (from graph.db nodes.signature)
  4. Test assertions (bonus only when available)

Falls back to legacy 5-family evidence when graph.db produces nothing.
Synced with L1 brief: briefed candidates get FULL evidence, 1-hop neighbors get
graph-aware evidence, unbriefed files get minimal (signature + nearest candidate).

Usage:
    python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from groundtruth.hooks.logger import log_hook
from groundtruth.runtime.sanitizer import clip_balanced

_GT_LOG = os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards in a string for use with ESCAPE '\\\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _db_path_from_conn(conn) -> str:
    """Recover the on-disk graph.db path from an open sqlite3 connection.

    Hook connections are always opened on an on-disk graph.db (``args.db``), so
    ``PRAGMA database_list`` reliably yields the file path even for read-only
    URI connections. Returns "" if it cannot be determined.
    """
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and file:
                return file
    except Exception:
        pass
    return ""


def _resolve_file_path(conn, query_path: str):
    """Resolve a query path to the canonical stored path in graph.db.

    DOC_OF_HONOR §1.1 — delegates to the ONE universal resolver
    (``path_resolver.resolve_to_stored_path``) instead of reinventing inline
    normalization. Handles container paths (/workspace/instance_id/file.py),
    host paths, and MCP paths.

    Returns the exact stored path, or ``None`` when the path is unknown or
    ambiguous (correct-or-quiet — never echoes a path-shaped string back).
    Callers must treat ``None`` as "skip this evidence block": a ``None`` SQL
    bind already yields 0 rows, and any non-SQL use is guarded at the call site.
    """
    from groundtruth.index.path_resolver import resolve_to_stored_path

    db_path = _db_path_from_conn(conn)
    if not db_path:
        return None
    return resolve_to_stored_path(query_path, db_path)


def _open_graph_db(db_path: str):
    """Open graph.db in read-only WAL mode with busy timeout."""
    import sqlite3
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _append_gt_log(event: str, detail: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}\tpost_edit\t{event}"
    if detail:
        line += f"\t{detail}"
    try:
        with open(_GT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _status_line(kind: str, detail: str) -> str:
    return f"[GT_STATUS] {kind}:{detail}"


# ---------------------------------------------------------------------------
# Improved L3 evidence: graph.db-driven, priority-ordered, code-first
# ---------------------------------------------------------------------------

_MAX_EVIDENCE_CHARS = 2000  # ~500 tokens — expanded for richer sibling/test/caller content
_BRIEF_CANDIDATES_PATH = "/tmp/gt_brief_candidates.txt"
_EDITED_FILES_PATH = "/tmp/gt_edited_files.txt"
_ISSUE_TERMS_PATH = "/tmp/gt_issue_terms.txt"
_ISSUE_ANCHORS_PATH = "/tmp/gt_issue_anchors.json"

# Phase 6 evidence: sibling/pattern selection was USELESS in 13/15 real tasks.
# Silenced pending selection algorithm repair. Set True to re-enable.
_SIBLING_EVIDENCE_ENABLED = True


# Categorical edge filter (Layer 2.2 — DOC_OF_HONOR §2.2).
#
# Replaces hardcoded numeric `confidence >= 0.6` with a categorical
# combination of post-merge Layer-0 signals: resolution_method, trust_tier,
# candidate_count. Aligns with the three mandatory properties
# (.claude/CLAUDE.md): dynamic per-edge categorical decision, hybrid 3-signal
# combination, confidence-gated at the FILTER level (no agent-facing label).
#
# Research basis:
#   - Anthropic "Writing Effective Tools" (2025): filter hard upstream,
#     verbatim downstream.
#   - Squeez arXiv 2604.04979 (2026): aggressive pre-display filtering.
#   - PyCG ICSE 2021: structural resolution methods (same_file, import,
#     type_flow) are the trustworthy categorical signals.
#
# Returns a SQL fragment (no leading AND) used in WHERE clauses on the
# `edges` table aliased as ``e``.
_STRONG_RESOLUTION_METHODS = (
    "same_file",
    "import",
    "verified_unique",
    "type_flow",
    "import_type",
    "lsp_verified",
    "impl_method",
    "inherited",
    "unique_method",
    "return_type",
)
_STRONG_TRUST_TIERS = ("CERTIFIED", "CANDIDATE")
_SUPPRESSED_TRUST_TIER = "SUPPRESSED"


def _categorical_edge_filter_clause(*, alias: str = "e") -> str:
    """SQL fragment selecting edges that pass the categorical filter.

    Use in queries like:
        SELECT ... FROM edges e WHERE e.type = 'CALLS' AND <clause> ...

    The fragment evaluates True when EITHER:
      - resolution_method is in the strong categorical set, OR
      - resolution_method is name_match with candidate_count <= 1 (unique
        by name — graph could not disambiguate other candidates), OR
      - trust_tier is CERTIFIED or CANDIDATE.

    AND trust_tier is not SUPPRESSED (hard exclude).
    """
    strong_methods = ", ".join(f"'{m}'" for m in _STRONG_RESOLUTION_METHODS)
    strong_tiers = ", ".join(f"'{t}'" for t in _STRONG_TRUST_TIERS)
    return (
        f"(("
        f"{alias}.resolution_method IN ({strong_methods}) "
        f"OR (COALESCE({alias}.trust_tier, 'SPECULATIVE') IN ({strong_tiers}) "
        f"AND LOWER(COALESCE({alias}.resolution_method, '')) != 'name_match')"
        f") AND COALESCE({alias}.trust_tier, 'SPECULATIVE') != '{_SUPPRESSED_TRUST_TIER}')"
    )


def _legacy_confidence_filter_clause(*, alias: str = "e", min_conf: float = 0.6) -> str:
    """Backward-compatible numeric confidence filter.

    Used as a fallback when graph.db doesn't have the post-merge categorical
    columns populated (e.g. older indexes). Picked dynamically by the helper
    below based on schema check.
    """
    return f"COALESCE({alias}.confidence, 0.5) >= {min_conf}"


def _edge_filter_for_db(db_path: str, *, alias: str = "e", min_conf: float = 0.6) -> str:
    """Pick the right filter clause based on what the graph.db supports.

    Returns the categorical clause when trust_tier + candidate_count columns
    are present in the schema; falls back to the legacy numeric clause
    otherwise. Single-source helper for all caller/callee queries.
    """
    try:
        import sqlite3 as _sq
        conn = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        conn.close()
        if "trust_tier" in cols and "candidate_count" in cols and "resolution_method" in cols:
            return _categorical_edge_filter_clause(alias=alias)
    except Exception:
        pass
    return _legacy_confidence_filter_clause(alias=alias, min_conf=min_conf)


# G7 isolation-gate marker classification (Layer 2.2 / CLAUDE.md:59).
# Caller-derived markers legitimately can't exist when a function has 0
# callers. Pillar markers (Contract/Consistency/Completeness) must survive
# isolation per the four-pillar always-fire rule. Both bracket and colon
# token shapes are listed because the renderer emits both.
_G7_CALLER_DERIVED_PREFIXES = (
    "[CALLERS]", "[CALLER]", "CALLERS:", "[REVIEW]",
    "[PROPAGATE]", "[IMPACT]", "[MISMATCH]", "[CONTRACT]",
)
_G7_PILLAR_KEEP_PREFIXES = (
    "[SIGNATURE]", "[RETURN_TYPE]", "[BEHAVIORAL CONTRACT]",
    "PRESERVE:", "MUTATES:", "ACCUMULATES:",
    "[RAISES]", "[CATCHES]", "PARAMS:",
    "[OVERRIDE]", "[INTERFACE]",
    "[TWIN]", "TWINS:", "[SIMILAR]", "[PATTERN]",
    # "Calls into:" is the edited fn's OWN outbound callee contract — it
    # requires ZERO callers and is Contract/Completeness evidence (TASK #49),
    # so it must survive the isolation gate per the four-pillar always-fire
    # rule (the agent most needs to know what it calls when nothing calls it).
    "Calls into:",
    "[TEST]", "[COMPLETENESS]", "[SCOPE]",
    "[CO-CHANGE]", "[BOUNDARY]", "[SECURITY]", "[SERDE]",
    "[CONCURRENCY]", "[CONFIG]", "[ORDER]", "[RESOURCE]",
    "FIELD:", "READS:",
)


def g7_filter_isolated(func_parts: list[str], sig: str = "") -> list[str]:
    """Filter evidence for a structurally isolated function (0 callers/siblings/peers).

    Per CLAUDE.md:59 four-pillar always-fire rule: drop only caller-derived
    markers (which can't exist at 0 callers); keep all
    Contract/Consistency/Completeness markers. If nothing survives, fall back
    to [SIGNATURE] (Contract pillar minimum), then to an honest isolation note.

    Pure function — module-level for unit testing.
    """
    def _drop_caller_derived(p: str) -> bool:
        s = p.lstrip()
        return any(s.startswith(pfx) for pfx in _G7_CALLER_DERIVED_PREFIXES)

    def _keep_pillar(p: str) -> bool:
        s = p.lstrip()
        if any(s.startswith(pfx) for pfx in _G7_PILLAR_KEEP_PREFIXES):
            return True
        # L5 advisories (start with `L` followed by a digit) — keep.
        if s and s[0] == "L" and len(s) > 1 and s[1].isdigit():
            return True
        return False

    kept = [p for p in func_parts if _keep_pillar(p) and not _drop_caller_derived(p)]
    if not kept and sig:
        kept = [f"[SIGNATURE] {sig}"]
    elif not kept:
        kept = [
            "[INFO] Function appears isolated: no callers, peers, "
            "or stored contract. Review carefully before edit."
        ]
    return kept


def _resolve_node_id(db_path: str, file_path: str, func_name: str) -> int | None:
    """Resolve a function name to a node ID in graph.db.

    Returns None when: no candidates, no suffix match, or db error.
    When ambiguous (multiple suffix matches), disambiguates by
    is_exported then lowest node_id. (ECOOP 2024: Indirection-Bounded CG)
    """
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3 as _sq_resolve
        conn = _sq_resolve.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        has_exported = "is_exported" in cols
        exp_col = ", is_exported" if has_exported else ""
        candidates = conn.execute(
            f"SELECT id, file_path{exp_col} FROM nodes WHERE name = ? AND label IN ('Function', 'Method')",
            (func_name,),
        ).fetchall()
        conn.close()
    except Exception:
        return None
    if not candidates:
        return None

    norm_parts = file_path.replace("\\", "/").lstrip("./").lstrip("/").split("/")
    matched: list[tuple[int, int]] = []  # (node_id, is_exported)
    best_match_len = -1
    for row in candidates:
        node_id = row[0]
        graph_path = row[1]
        is_exp = row[2] if has_exported and len(row) > 2 else 0
        graph_parts = graph_path.replace("\\", "/").split("/")
        if len(graph_parts) <= len(norm_parts):
            if norm_parts[-len(graph_parts):] == graph_parts:
                if len(graph_parts) > best_match_len:
                    best_match_len = len(graph_parts)
                    matched = [(node_id, is_exp)]
                elif len(graph_parts) == best_match_len:
                    matched.append((node_id, is_exp))
    if len(matched) == 1:
        return matched[0][0]
    if len(matched) > 1:
        exported = [m for m in matched if m[1]]
        if len(exported) == 1:
            return exported[0][0]
        pool = exported if exported else matched
        pool.sort(key=lambda m: m[0])
        print(
            f"[GT_META] resolve_disambiguated: {func_name}@{file_path} "
            f"matched={len(matched)} picked_id={pool[0][0]} (tiebreak)",
            file=sys.stderr, flush=True,
        )
        return pool[0][0]
    print(
        f"[GT_META] resolve_no_suffix_match: {func_name}@{file_path} "
        f"candidates={len(candidates)} — returning None (no suffix match, won't guess wrong file)",
        file=sys.stderr, flush=True,
    )
    return None


def _load_issue_anchors() -> dict:
    """Load issue anchors (symbols, paths, test_names) written by wrapper."""
    try:
        import json as _json
        raw = open(_ISSUE_ANCHORS_PATH, encoding="utf-8").read().strip()
        if not raw:
            return {"symbols": [], "paths": [], "test_names": []}
        return _json.loads(raw)
    except (OSError, ValueError):
        return {"symbols": [], "paths": [], "test_names": []}


def _load_issue_terms() -> set[str]:
    """Load issue keywords written by wrapper at task start."""
    try:
        raw = open(_ISSUE_TERMS_PATH, encoding="utf-8").read().strip()
        if not raw:
            return set()
        return set(raw.lower().split("\n"))
    except OSError:
        return set()


def _compute_caller_relevance(caller: dict[str, str], issue_terms: set[str]) -> float:
    """Fraction of issue terms that appear in caller's file path + code."""
    if not issue_terms:
        return 0.5  # neutral when no issue terms available
    text = (caller.get("file", "") + " " + caller.get("code", "")).lower()
    hits = sum(1 for t in issue_terms if t in text)
    return hits / len(issue_terms)


def _annotate_evidence_header(
    callers: list[dict[str, str]],
    issue_terms: set[str],
    db_path: str = "",
    file_path: str = "",
) -> str:
    """Generate task-relevance annotation header for callers.

    Phase 4 (Contrastive Evidence): when keyword overlap is 0, query graph.db
    for connected files that DO have keyword overlap >= 2 with the issue.
    """
    if not callers or not issue_terms:
        return ""

    relevant_count = sum(
        1 for c in callers if _compute_caller_relevance(c, issue_terms) > 0
    )

    if relevant_count == 0:
        header = "[NOTE] Callers of this file show 0 keyword overlap with the issue.\n"

        # Phase 4: find connected files with keyword overlap
        if db_path and file_path and os.path.exists(db_path):
            try:
                import sqlite3 as _sq3

                conn = _sq3.connect(db_path)
                conn.row_factory = _sq3.Row
                _resolved_eh = _resolve_file_path(conn, file_path)

                # Get files connected to the edited file (calls or called-by)
                connected_rows = conn.execute(
                    """SELECT DISTINCT n2.file_path
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                         AND COALESCE(e.confidence, 0.5) >= 0.7
                       JOIN nodes n2 ON (n2.id = e.source_id OR n2.id = e.target_id)
                       WHERE n1.file_path = ? AND n2.file_path != ?
                         AND e.type = 'CALLS'
                       LIMIT 20""",
                    (_resolved_eh, _resolved_eh),
                ).fetchall()
                conn.close()

                suggestions: list[str] = []
                for crow in connected_rows:
                    cf = crow["file_path"]
                    cf_lower = cf.lower()
                    overlap = sum(1 for t in issue_terms if t in cf_lower)
                    if overlap >= 2:
                        suggestions.append(f"Connected file {cf} has {overlap} keyword matches")
                    if len(suggestions) >= 2:
                        break

                if suggestions:
                    header += "\n".join(suggestions) + "\n"
            except Exception as e:
                _append_gt_log("header_suggestions_error", str(e))

        return header
    return ""


def _extract_usage_contract(callers: list[dict[str, str]]) -> str:
    """Show literal caller code lines — the actual usage context.

    Takes already-captured caller dicts with 'code' field and formats them
    as literal evidence the agent can reason about directly.
    """
    if not callers:
        return ""

    lines: list[str] = []
    for c in callers[:3]:
        code = c.get("code", "")
        caller_file = c.get("file", "")
        line_num = c.get("line", "")
        if not code:
            continue
        code_clean = code.replace(" | ", " → ").strip()
        if len(code_clean) > 150:
            # Balance-aware clip: a raw code[:147] can split a caller line
            # mid-string/expr. clip_balanced keeps the longest well-formed
            # prefix; "..." signals the elision.
            code_clean = clip_balanced(code_clean, 147) + "..."
        if caller_file and line_num:
            lines.append(f"{caller_file}:{line_num} `{code_clean}`")
        elif code_clean:
            lines.append(f"`{code_clean}`")

    if not lines:
        return ""
    return "CALLERS: " + " | ".join(lines)


def _classify_return_usage(lines_after: list[str]) -> str:
    """Classify how a caller uses the return value of a function call.

    Research: ICSE caller context windows — understanding return value usage
    patterns (truthiness check, error guard, attribute access, assignment)
    helps the agent understand caller expectations and avoid breaking them
    when modifying return types or adding error paths.

    Examines the 2 lines after the call site for usage patterns.
    """
    for line in lines_after[:2]:
        stripped = line.strip()
        if re.search(r"\bif\s+(not\s+)?\w+", stripped):
            return "truthiness_check"
        if re.search(r"\braise\b|\bassert\b", stripped):
            return "error_guard"
        if re.search(r"\[|\.\w+\(", stripped):
            return "attribute_access"
    return "assignment"


import re as _re

_TEMPLATE_SUBS = [
    (_re.compile(r'"[^"]*"'), 'STRING'),
    (_re.compile(r"'[^']*'"), 'STRING'),
    (_re.compile(r'\b\d+\b'), 'NUM'),
]


def _make_template(line: str) -> str:
    """Reduce a code line to its structural pattern by replacing literals."""
    t = line.strip()
    for pat, repl in _TEMPLATE_SUBS:
        t = pat.sub(repl, t)
    return t


def _detect_structural_twins(
    file_path: str,
    func_start: int,
    func_end: int,
) -> str:
    """Find structural twins within a function — lines sharing the same pattern.

    Detects when a function has multiple lines with identical structure but
    different values (e.g., multiple env var checks, multiple regex patterns,
    multiple elif branches). Shows them so the agent verifies consistency.
    """
    try:
        with open(file_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

    start = max(0, func_start - 1)
    end = min(len(all_lines), func_end)
    func_lines = all_lines[start:end]

    templates: dict[str, list[tuple[int, str]]] = {}
    for i, line in enumerate(func_lines):
        stripped = line.strip()
        if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if stripped in ("pass", "else:", "try:", "finally:", "except:", "break", "continue"):
            continue
        tmpl = _make_template(stripped)
        if tmpl not in templates:
            templates[tmpl] = []
        templates[tmpl].append((start + i + 1, stripped))

    twin_groups = [(tmpl, entries) for tmpl, entries in templates.items()
                   if len(entries) >= 2 and len(entries) <= 6]

    if not twin_groups:
        return ""

    twin_groups.sort(key=lambda x: -len(x[1]))
    best = twin_groups[0]
    entries = best[1]

    parts: list[str] = []
    for line_num, code in entries[:3]:
        code_short = code if len(code) <= 70 else clip_balanced(code, 67) + "..."
        parts.append(f"L{line_num}: `{code_short}`")

    return "TWINS: " + " | ".join(parts)


def _detect_edit_propagation(
    db_path: str, file_path: str, func_name: str, repo_root: str,  # noqa: ARG001
) -> str:
    """Find call sites that may need updating after a function edit.

    Research: CodePlan (FSE 2024) — 5/7 repos pass with propagation vs 0/7 without.
    After editing a function, callers that pass specific args or destructure
    the return value may need corresponding updates.
    """
    try:
        import sqlite3 as _sql
        conn = _sql.connect(db_path)
        _resolved_fp = _resolve_file_path(conn, file_path)
        _filter_clause = _edge_filter_for_db(db_path)
        rows = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND {_filter_clause}
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path = ?
              AND nsrc.file_path != nt.file_path
              AND nsrc.is_test = 0
              AND e.source_line > 0
            ORDER BY e.source_line
            LIMIT 5
            """,
            (func_name, _resolved_fp),
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        sites: list[str] = []
        for caller_file, line_num in rows[:3]:
            sites.append(f"{caller_file}:{line_num}")

        if sites:
            return f"[PROPAGATE] {len(rows)} call sites may need updating: {', '.join(sites)}"
    except Exception as e:
        _append_gt_log("propagation_check_error", str(e))
    return ""


def _classify_file_kind(file_path: str) -> str:
    """Classify a file as source, test, or config for co-change phrasing."""
    norm = "/" + file_path.replace("\\", "/").lower().lstrip("/")
    base = os.path.basename(file_path).lower()
    if base.startswith("test_") or base.endswith("_test.py") or "/tests/" in norm or "/test/" in norm:
        return "test"
    if base.endswith((".yml", ".yaml", ".toml", ".cfg", ".ini", ".json", ".xml")):
        return "config"
    return "source"


def _co_change_reminder(file_path: str, repo_root: str, edited_files: list[str]) -> str:
    """Show files that historically co-change but haven't been edited yet.

    Confidence-gated per signal_thresholds.py constants.
    File classification: source/test/config with appropriate phrasing.
    Reconciles with Decision 26 co-change expansion (same data, L3 delivery).
    """
    from groundtruth.config.signal_thresholds import (
        COCHANGE_HIGH_THRESHOLD,
        COCHANGE_MEDIUM_THRESHOLD,
        COCHANGE_WINDOW_COMMITS,
        log_threshold_use,
    )
    norm_fp = file_path.replace("\\", "/").lstrip("/")

    co_counts: dict[str, int] = {}

    # Fast path: use pre-mined cochanges table from graph.db
    _db_path = os.environ.get("GT_GRAPH_DB", "")
    if _db_path and os.path.exists(_db_path):
        try:
            import sqlite3 as _sq
            _cc_conn = _sq.connect(_db_path)
            _cc_conn.row_factory = _sq.Row
            _tables = {r[0] for r in _cc_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "cochanges" in _tables:
                _esc = _escape_like(norm_fp)
                for row in _cc_conn.execute(
                    "SELECT file_b AS partner, count FROM cochanges WHERE file_a LIKE ? ESCAPE '\\' "
                    "UNION SELECT file_a AS partner, count FROM cochanges WHERE file_b LIKE ? ESCAPE '\\' "
                    "ORDER BY count DESC LIMIT 10",
                    (f"%{_esc}", f"%{_esc}"),
                ).fetchall():
                    partner = row["partner"]
                    if partner and not norm_fp.endswith(partner) and not partner.endswith(norm_fp):
                        co_counts[partner] = row["count"]
            _cc_conn.close()
        except Exception:
            pass

    # Fallback: mine git log if cochanges table empty or unavailable
    if not co_counts:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:__COMMIT__", f"-{COCHANGE_WINDOW_COMMITS}"],
                cwd=repo_root, capture_output=True, text=True, timeout=10,
                env=_git_env(),
            )
            if result.returncode == 0:
                current_commit_files: list[str] = []
                for line in result.stdout.splitlines():
                    if line.strip() == "__COMMIT__":
                        if norm_fp in current_commit_files:
                            for f in current_commit_files:
                                if f != norm_fp and not f.endswith((".md", ".rst", ".txt", ".lock")):
                                    co_counts[f] = co_counts.get(f, 0) + 1
                        current_commit_files = []
                    elif line.strip():
                        current_commit_files.append(line.strip().replace("\\", "/").lstrip("/"))
                if norm_fp in current_commit_files:
                    for f in current_commit_files:
                        if f != norm_fp and not f.endswith((".md", ".rst", ".txt", ".lock")):
                            co_counts[f] = co_counts.get(f, 0) + 1
        except Exception:
            pass

    edited_set = set(edited_files)
    unedited_co = [(f, c) for f, c in co_counts.items() if f not in edited_set and c >= COCHANGE_MEDIUM_THRESHOLD]
    unedited_co.sort(key=lambda x: -x[1])

    if not unedited_co:
        print(f"[GT_META] cochange: source=git_log file={file_path} pairs=0", file=sys.stderr, flush=True)
        return ""

    print(f"[GT_META] cochange: source=git_log file={file_path} pairs={len(unedited_co)}", file=sys.stderr, flush=True)

    top_file, top_count = unedited_co[0]
    file_kind = _classify_file_kind(top_file)

    if top_count >= COCHANGE_HIGH_THRESHOLD:
        confidence = "high"
    elif top_count >= COCHANGE_MEDIUM_THRESHOLD:
        confidence = "medium"
    else:
        return ""

    log_threshold_use("COCHANGE", confidence, f"file={top_file} count={top_count} kind={file_kind}")

    # Phrasing by file kind
    if file_kind == "test":
        action = "Test may need updating"
    elif file_kind == "config":
        action = "Config may need corresponding update"
    else:
        action = "Check if changes needed"

    top_parts = []
    for f, c in unedited_co[:2]:
        top_parts.append(f"{f} ({c}x)")

    if confidence == "high":
        return f"[CO-CHANGE] {', '.join(top_parts)} changed with this file in {top_count}/{COCHANGE_WINDOW_COMMITS} commits — {action.lower()}"
    else:
        return f"[CO-CHANGE] {', '.join(top_parts)} often changes with this file ({top_count} commits) — {action.lower()}"


def _scope_completeness(edited_files: list[str], file_path: str, repo_root: str) -> str:
    """Warn if edit scope seems incomplete based on historical patterns.

    Research: 60% of SWE-bench-Verified requires multi-component patches.
    Agents systematically under-edit (ASE 2025 multi-hunk study).
    """
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:COMMIT", "-30", "--", file_path],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
            env=_git_env(),
        )
        if result.returncode != 0:
            return ""
    except Exception:
        return ""

    commit_file_counts: list[int] = []
    current_count = 0
    for line in result.stdout.splitlines():
        if line.strip() == "COMMIT":
            if current_count > 0:
                commit_file_counts.append(current_count)
            current_count = 0
        elif line.strip():
            current_count += 1
    if current_count > 0:
        commit_file_counts.append(current_count)

    if not commit_file_counts:
        return ""

    avg_files = sum(commit_file_counts) / len(commit_file_counts)
    current_edited = len(set(edited_files))

    if avg_files > 1.5 and current_edited == 1:
        return f"[SCOPE] commits to this file typically touch {avg_files:.1f} files — you've only edited {current_edited} so far"
    return ""


def _compose_scope_signal(
    db_path: str, file_path: str, func_name: str, repo_root: str, edited_files: list[str],
) -> str:
    """Compose a single multi-file scope signal from three independent mechanisms.

    Fires when 2+ mechanisms agree, or when 1 mechanism has high-confidence evidence.
    Output <=120 chars. Research: WANG-MENG-2018 (52-58% multi-entity),
    ARISE-2026 (structural retrieval +1.7pp).
    """
    prop = _detect_edit_propagation(db_path, file_path, func_name, repo_root)
    co = _co_change_reminder(file_path, repo_root, edited_files)
    scope = _scope_completeness(edited_files, file_path, repo_root)

    signals = [(prop, "propagation"), (co, "co_change"), (scope, "scope")]
    fired = [(msg, kind) for msg, kind in signals if msg]

    if len(fired) >= 2:
        return fired[0][0][:120]
    if len(fired) == 1:
        msg, kind = fired[0]
        if kind == "propagation":
            return msg[:120]
        if kind == "co_change" and "high" in msg.lower():
            return msg[:120]
        return ""
    return ""


def _read_lines_file(path: str) -> list[str]:
    """Read a file containing one path per line. Returns [] on any error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []


def _read_source_line(full_path: str, line_no: int, extra_lines: int = 0, end_line: int = 0) -> str:
    """Read a source line + optional context lines after it. Returns '' on failure."""
    try:
        lines_to_read: list[str] = []
        base_indent = -1
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i == line_no:
                    lines_to_read.append(line.rstrip())
                    base_indent = len(line) - len(line.lstrip())
                elif lines_to_read and len(lines_to_read) <= extra_lines:
                    if end_line and i > end_line:
                        break
                    stripped = line.rstrip()
                    if not stripped:
                        break
                    cur_indent = len(line) - len(line.lstrip())
                    if cur_indent < base_indent:
                        break
                    if any(stripped.lstrip().startswith(kw) for kw in ("def ", "async def ", "class ", "func ", "function ", "fn ")):
                        break
                    lines_to_read.append(stripped)
                elif lines_to_read:
                    break
        return " | ".join(lines_to_read) if lines_to_read else ""
    except OSError:
        return ""


def _read_source_lines(full_path: str, start: int, end: int) -> str:
    """Read lines [start, end] from a source file. Returns '' on failure."""
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f, 1):
                if i >= start and i <= end:
                    lines.append(line.rstrip())
                if i > end:
                    break
            return "\n".join(lines)
    except OSError:
        return ""




def _get_callers_from_graph(
    db_path: str, file_path: str, function_name: str, repo_root: str,
    seen_files: list[str], limit: int = 5
) -> list[dict[str, str]]:
    """Query graph.db for cross-file callers with confidence >= 0.5.

    Returns list of dicts: {file, line, caller_name, code}
    Filters out callers from files the agent has already visited.
    """
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        # Disambiguate target node first
        resolved_target_id = _resolve_node_id(db_path, file_path, function_name)
        if resolved_target_id is None:
            return results

        conn = _open_graph_db(db_path)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        has_confidence = "confidence" in cols
        has_resolution = "resolution_method" in cols

        # Layer 2.2 categorical filter: replaces hardcoded `confidence >= 0.6`.
        # Picks categorical clause when post-merge schema columns are present,
        # falls back to legacy numeric clause otherwise.
        edge_filter = _edge_filter_for_db(db_path)
        conf_select = ", e.confidence" if has_confidence else ""
        res_select = ", e.resolution_method" if has_resolution else ""
        query = f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line{conf_select}{res_select}
            FROM edges e
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE e.target_id = ? AND e.type = 'CALLS'
              AND {edge_filter}
              AND nsrc.file_path NOT IN (SELECT file_path FROM nodes WHERE id = ?)
            ORDER BY {"e.confidence DESC," if has_confidence else ""} e.source_line
            LIMIT ?
        """
        rows = conn.execute(query, (resolved_target_id, resolved_target_id, limit + 10)).fetchall()

        if has_confidence:
            _all_count = conn.execute(
                "SELECT COUNT(*) FROM edges e "
                "JOIN nodes nsrc ON e.source_id = nsrc.id "
                "WHERE e.target_id = ? AND e.type = 'CALLS' "
                "AND nsrc.file_path NOT IN (SELECT file_path FROM nodes WHERE id = ?)",
                (resolved_target_id, resolved_target_id),
            ).fetchone()[0]
            if _all_count > len(rows):
                print(
                    f"[GT_META] categorical_filter: {function_name} total_callers={_all_count} "
                    f"after_filter={len(rows)} excluded={_all_count - len(rows)}",
                    file=sys.stderr, flush=True,
                )

        # Removed numeric 0.5 fallback. Per research (Squeez 2604.04979,
        # Anthropic 2025): no low-confidence display fallback. If categorical
        # filter returns 0 callers, the agent gets no caller evidence — that
        # is the honest signal, not a degraded fallback.

        seen_norm = {s.replace("\\", "/").lstrip("/") for s in seen_files}

        for row in rows:
            caller_file = row["file_path"]
            source_line = row["source_line"]
            caller_name = row["name"]
            caller_norm = caller_file.replace("\\", "/").lstrip("/")

            # Mark whether agent has seen this file
            is_unseen = caller_norm not in seen_norm

            code = ""
            pre_context = ""
            caller_end = row["end_line"] or 0
            if source_line and source_line > 0:
                full_path = os.path.join(repo_root, caller_file)
                code = _read_source_line(full_path, source_line, extra_lines=2, end_line=caller_end)
                if source_line > 1:
                    pre_context = _read_source_line(full_path, source_line - 1).strip()[:90]

            # Extract edge confidence if available
            edge_conf = 0.5  # default when column absent
            if has_confidence:
                try:
                    edge_conf = float(row["confidence"] or 0.5)
                except (TypeError, ValueError):
                    edge_conf = 0.5

            res_method = ""
            if has_resolution:
                try:
                    res_method = str(row["resolution_method"] or "")
                except (IndexError, KeyError):
                    pass

            # Classify how the caller uses the return value
            # Research: type-constrained generation — knowing usage pattern
            # (truthiness_check, error_guard, attribute_access, assignment)
            # prevents breaking callers when modifying return types.
            usage = ""
            if code:
                _code_lines = code.split(" | " if " | " in code else "\n")
                _after_call = _code_lines[1:] if len(_code_lines) > 1 else []
                if _after_call:
                    usage = _classify_return_usage(_after_call)

            # P11: map caller arguments to callee parameters
            arg_mapping = ""
            if code and function_name:
                try:
                    param_rows = conn.execute(
                        "SELECT value FROM properties WHERE node_id = ? AND kind = 'param' ORDER BY line",
                        (resolved_target_id,)
                    ).fetchall()
                    if param_rows:
                        param_names = [r[0].split(":")[0].split("=")[0].strip() for r in param_rows]
                        arg_mapping = _map_args_to_params(code, function_name, param_names)
                except Exception:
                    pass

            results.append({
                "file": caller_file,
                "line": str(source_line or "?"),
                "caller_name": caller_name,
                "code": code,
                "pre_context": pre_context,
                "unseen": "1" if is_unseen else "0",
                "confidence": str(edge_conf),
                "resolution_method": res_method,
                "return_usage": usage,
                "arg_mapping": arg_mapping,
            })

            if len(results) >= limit:
                break

        # Phase 2: Dynamic Hops — follow thin wrappers (max 2 hops total)
        # If only 1 caller exists, check if it's a thin wrapper (<3 callers itself)
        # and if so, append the wrapper's callers for additional context.
        if len(results) == 1:
            wrapper = results[0]
            wrapper_name = wrapper["caller_name"]
            wrapper_file = wrapper["file"]
            _resolved_wrapper = _resolve_file_path(conn, wrapper_file)

            hop2_query = f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                  AND {edge_filter}
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path = ? AND nt.name = ?
                  AND nsrc.file_path != nt.file_path
                ORDER BY {"e.confidence DESC," if has_confidence else ""} e.source_line
                LIMIT 5
            """
            hop2_rows = conn.execute(
                hop2_query, (_resolved_wrapper, wrapper_name, )
            ).fetchall()

            # Only follow if the wrapper has <3 callers (thin wrapper pattern)
            if 0 < len(hop2_rows) < 3:
                for h2row in hop2_rows:
                    h2_file = h2row["file_path"]
                    h2_line = h2row["source_line"]
                    h2_name = h2row["name"]
                    h2_norm = h2_file.replace("\\", "/").lstrip("/")

                    is_unseen = h2_norm not in seen_norm

                    code = ""
                    h2_end = h2row["end_line"] or 0
                    if h2_line and h2_line > 0:
                        full_path = os.path.join(repo_root, h2_file)
                        code = _read_source_line(
                            full_path, h2_line, extra_lines=2, end_line=h2_end
                        )
                    if code:
                        code = f"[via wrapper] {code}"

                    results.append({
                        "file": h2_file,
                        "line": str(h2_line or "?"),
                        "caller_name": h2_name,
                        "code": code,
                        "unseen": "1" if is_unseen else "0",
                        "confidence": str(float(wrapper.get("confidence", "0.5")) * 0.9),
                    })

                    if len(results) >= limit:
                        break

        conn.close()

        issue_terms = _load_issue_terms()
        if issue_terms and len(results) > 1:
            def _issue_score(caller: dict) -> int:
                text = (caller.get("file", "") + " " + caller.get("code", "")).lower()
                return sum(1 for t in issue_terms if t in text)
            results.sort(key=lambda c: _issue_score(c), reverse=True)

    except Exception as e:
        _append_gt_log("get_callers_error", str(e))

    return results


def _get_signature_from_graph(db_path: str, file_path: str, function_name: str) -> str:
    """Get function signature + return type from graph.db."""
    import sqlite3 as _sqlite3

    try:
        node_id = _resolve_node_id(db_path, file_path, function_name)
        if node_id is None:
            return ""
        conn = _open_graph_db(db_path)
        row = conn.execute(
            "SELECT signature, return_type FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        conn.close()
        if row:
            sig = row["signature"] or ""
            ret = row["return_type"] or ""
            if sig:
                return sig if ret and ret in sig else f"{sig} -> {ret}" if ret else sig
            elif ret:
                return f"def {function_name}(...) -> {ret}"
        return ""
    except Exception:
        return ""


def _get_siblings_from_graph(
    db_path: str, file_path: str, function_name: str, repo_root: str
) -> list[dict[str, str]]:
    """Get sibling functions (same class/file) from graph.db with a body snippet."""
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        resolved_id = _resolve_node_id(db_path, file_path, function_name)
        if resolved_id is None:
            return []

        conn = _open_graph_db(db_path)

        # Get parent_id from the resolved node
        target = conn.execute(
            "SELECT id, parent_id FROM nodes WHERE id = ?",
            (resolved_id,),
        ).fetchone()
        if not target:
            conn.close()
            return []

        node_id = target["id"]
        parent_id = target["parent_id"]

        # Get siblings
        _resolved_sib_path = _resolve_file_path(conn, file_path)
        if parent_id and parent_id > 0:
            siblings = conn.execute(
                "SELECT name, start_line, end_line, signature, file_path FROM nodes "
                "WHERE parent_id = ? AND id != ? AND label IN ('Function', 'Method') "
                "ORDER BY start_line LIMIT 3",
                (parent_id, node_id),
            ).fetchall()
        else:
            siblings = conn.execute(
                "SELECT name, start_line, end_line, signature, file_path FROM nodes "
                "WHERE file_path = ? AND id != ? AND label IN ('Function', 'Method') "
                "AND (parent_id IS NULL OR parent_id = 0) "
                "ORDER BY start_line LIMIT 3",
                (_resolved_sib_path, node_id),
            ).fetchall()
        conn.close()

        _DUNDER_SKIP = {"__init__", "__repr__", "__str__", "__eq__", "__hash__", "__del__",
                        "__len__", "__iter__", "__next__", "__contains__", "__getitem__",
                        "__setitem__", "__enter__", "__exit__", "__call__", "__bool__"}
        for sib in siblings:
            sib_name = sib["name"]
            if sib_name in _DUNDER_SKIP:
                continue
            sib_sig = sib["signature"] or ""
            sib_file = sib["file_path"]
            start = sib["start_line"] or 0
            end = sib["end_line"] or 0

            # Read sibling body — enough to capture calling conventions, kwargs patterns
            snippet = ""
            if start > 0 and end > 0:
                full_path = os.path.join(repo_root, sib_file)
                body_start = start + 1  # skip def line
                body_end = min(start + 12, end)  # up to 12 lines
                snippet = _read_source_lines(full_path, body_start, body_end)

            results.append({
                "name": sib_name,
                "signature": sib_sig,
                "snippet": snippet.strip(),
            })

    except Exception as e:
        _append_gt_log("get_siblings_error", str(e))

    return results


def _get_interface_peers_from_graph(
    db_path: str, file_path: str, function_name: str, repo_root: str,
    edited_files: list[str] | None = None,
) -> list[dict[str, str]]:
    """Find same-method implementations across classes sharing an interface/base.

    Strategy (ordered by precision):
    1. Inheritance: class C extends/implements B → find all other classes that
       also extend B → return their version of function_name
    2. Fallback: same-directory files with same method name (name-match peers)

    Prioritizes files the agent already edited (shows what they wrote as pattern).
    """
    import sqlite3 as _sq

    results: list[dict[str, str]] = []
    edited = set(edited_files or [])

    try:
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        norm_path = file_path.replace("\\", "/").lstrip("/")

        # Diagnostic: count EXTENDS/IMPLEMENTS edges in this graph.db
        _ext_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE type IN ('EXTENDS', 'IMPLEMENTS') AND COALESCE(confidence, 0.5) >= 0.5"
        ).fetchone()[0]
        print(
            f"[GT_META] peer_detection: func={function_name} file={norm_path} "
            f"extends_edges_in_db={_ext_count}",
            file=sys.stderr, flush=True,
        )

        # Find the class containing this method (disambiguated)
        _resolved_peer_id = _resolve_node_id(db_path, file_path, function_name)
        method_node = None
        if _resolved_peer_id is not None:
            method_node = conn.execute(
                "SELECT id, parent_id FROM nodes WHERE id = ?",
                (_resolved_peer_id,),
            ).fetchone()
        if not method_node or not method_node["parent_id"]:
            print(
                f"[GT_META] peer_detection: no method node or no parent_id, "
                f"fallback to name_match. method_found={method_node is not None}",
                file=sys.stderr, flush=True,
            )
            conn.close()
            return _get_name_match_peers(db_path, file_path, function_name, repo_root, edited)

        class_id = method_node["parent_id"]
        class_node = conn.execute(
            "SELECT name, label FROM nodes WHERE id = ?", (class_id,)
        ).fetchone()
        print(
            f"[GT_META] peer_detection: class={class_node['name'] if class_node else '?'} "
            f"class_id={class_id}",
            file=sys.stderr, flush=True,
        )

        # Strategy 1: Find parent via EXTENDS/IMPLEMENTS edges
        parent_edges = conn.execute(
            "SELECT target_id, type FROM edges "
            "WHERE source_id = ? AND type IN ('EXTENDS', 'IMPLEMENTS') LIMIT 3",
            (class_id,),
        ).fetchall()
        print(
            f"[GT_META] peer_detection: extends_edges_from_class={len(parent_edges)} "
            f"targets={[(pe['target_id'], pe['type']) for pe in parent_edges]}",
            file=sys.stderr, flush=True,
        )

        peer_class_ids: list[int] = []
        for pe in parent_edges:
            parent_id = pe["target_id"]
            # Find all other classes that extend/implement the same parent
            siblings = conn.execute(
                "SELECT DISTINCT source_id FROM edges "
                "WHERE target_id = ? AND type IN ('EXTENDS', 'IMPLEMENTS') "
                "AND COALESCE(confidence, 0.5) >= 0.5 "
                "AND source_id != ?",
                (parent_id, class_id),
            ).fetchall()
            peer_class_ids.extend(s["source_id"] for s in siblings)

            # Also include the parent class itself (base class method)
            peer_class_ids.append(parent_id)

        if not peer_class_ids:
            conn.close()
            return _get_name_match_peers(db_path, file_path, function_name, repo_root, edited)

        # Find the same method in peer classes
        placeholders = ",".join("?" for _ in peer_class_ids)
        peer_methods = conn.execute(
            f"SELECT name, file_path, start_line, end_line, signature FROM nodes "
            f"WHERE parent_id IN ({placeholders}) AND name = ? "
            f"AND label IN ('Function', 'Method') "
            f"ORDER BY file_path LIMIT 5",
            (*peer_class_ids, function_name),
        ).fetchall()

        for pm in peer_methods:
            pm_file = pm["file_path"]
            pm_norm = pm_file.replace("\\", "/").lstrip("/")
            if pm_norm == norm_path:
                continue  # skip self
            start = pm["start_line"] or 0
            end = pm["end_line"] or 0
            snippet = ""
            if start > 0 and end > 0:
                full_path = os.path.join(repo_root, pm_file)
                body_start = start  # include def line
                body_end = min(start + 12, end)
                snippet = _read_source_lines(full_path, body_start, body_end)

            is_edited = any(pm_norm.endswith(ef) or ef.endswith(pm_norm) for ef in edited)
            results.append({
                "name": function_name,
                "file": pm_norm,
                "signature": pm["signature"] or "",
                "snippet": snippet.strip(),
                "edited": is_edited,
            })

        conn.close()

        # Sort: already-edited files first (shows agent's own pattern)
        results.sort(key=lambda r: (not r["edited"], r["file"]))

    except Exception as e:
        _append_gt_log("get_interface_peers_error", str(e))

    return results[:3]


def _get_name_match_peers(
    db_path: str, file_path: str, function_name: str, repo_root: str,
    edited: set[str],
) -> list[dict[str, str]]:
    """Fallback: find same-method-name in same directory (no inheritance edges needed)."""
    import sqlite3 as _sq

    # Skip names that exist in every class
    if function_name.startswith("__") and function_name.endswith("__"):
        return []
    if function_name in ("setUp", "tearDown", "setup", "teardown", "main", "run"):
        return []

    results: list[dict[str, str]] = []
    try:
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        _resolved_peer = _resolve_file_path(conn, file_path)
        if _resolved_peer is None:
            # Unknown / ambiguous path -> stay silent (correct-or-quiet).
            conn.close()
            return []
        parent_dir = "/".join(_resolved_peer.split("/")[:-1])
        if not parent_dir:
            conn.close()
            return []

        peers = conn.execute(
            "SELECT DISTINCT file_path, start_line, end_line, signature FROM nodes "
            "WHERE file_path LIKE ? AND name = ? AND label IN ('Function', 'Method') "
            "AND file_path != ? "
            "ORDER BY file_path LIMIT 5",
            (f"{parent_dir}/%", function_name, _resolved_peer),
        ).fetchall()
        conn.close()

        for pm in peers:
            pm_file = pm["file_path"]
            pm_norm = pm_file.replace("\\", "/").lstrip("/")
            start = pm["start_line"] or 0
            end = pm["end_line"] or 0
            snippet = ""
            if start > 0 and end > 0:
                full_path = os.path.join(repo_root, pm_file)
                body_start = start
                body_end = min(start + 12, end)
                snippet = _read_source_lines(full_path, body_start, body_end)

            is_edited = any(pm_norm.endswith(ef) or ef.endswith(pm_norm) for ef in edited)
            results.append({
                "name": function_name,
                "file": pm_norm,
                "signature": pm["signature"] or "",
                "snippet": snippet.strip(),
                "edited": is_edited,
            })

        results.sort(key=lambda r: (not r["edited"], r["file"]))
    except Exception as e:
        _append_gt_log("get_name_match_peers_error", str(e))

    return results[:3]


def _get_override_chain(
    db_path: str, file_path: str, method_name: str
) -> list[dict[str, str]]:
    """Find parent class implementations of this method via EXTENDS edges."""
    results: list[dict[str, str]] = []
    try:
        conn = _open_graph_db(db_path)
        _resolved_fp = _resolve_file_path(conn, file_path)
        parent_node = conn.execute(
            "SELECT n.id, n.name FROM nodes n "
            "JOIN nodes m ON m.parent_id = n.id "
            "WHERE m.name = ? AND m.file_path = ? AND n.label = 'Class' LIMIT 1",
            (method_name, _resolved_fp),
        ).fetchone()
        if not parent_node:
            conn.close()
            return []
        class_id, class_name = parent_node["id"], parent_node["name"]
        rows = conn.execute(
            """WITH RECURSIVE ancestors AS (
                SELECT n.id, n.name, n.file_path, 0 as depth
                FROM nodes n WHERE n.id = ?
                UNION ALL
                SELECT n2.id, n2.name, n2.file_path, a.depth + 1
                FROM ancestors a
                JOIN edges e ON e.source_id = a.id AND e.type IN ('EXTENDS','IMPLEMENTS')
                JOIN nodes n2 ON n2.id = e.target_id
                WHERE a.depth < 5
            )
            SELECT m.name, m.file_path, m.signature, a.name as class_name
            FROM ancestors a
            JOIN nodes m ON m.parent_id = a.id AND m.name = ?
            WHERE a.depth > 0
            ORDER BY a.depth LIMIT 3""",
            (class_id, method_name),
        ).fetchall()
        conn.close()
        for row in rows:
            results.append({
                "method": row["name"],
                "file": row["file_path"],
                "signature": row["signature"] or "",
                "class": row["class_name"],
            })
    except Exception as e:
        _append_gt_log("override_chain_error", str(e))
    return results


def _find_same_name_twins(
    db_path: str, node_id: int, func_name: str, file_path: str
) -> list[tuple[str, int]]:
    """TASK #50: find same-name sibling definitions (twins) of the edited fn.

    A "twin" is another function/method with the SAME name as the edited
    function, in the SAME file (and/or the same parent class), at a DIFFERENT
    source line. This is the highest-precision consistency signal: when a repo
    defines ``set_fields`` twice (e.g. once on ``ImportTask`` and once on
    ``SingletonImportTask`` in the same module), a fix to one almost always must
    be mirrored to the other. The fingerprint ``[SIMILAR]`` path missed this
    because two short identical methods can have divergent call-fingerprints.

    Generalized: keys only on name + path/class identity from graph.db — no
    repo/task-specific logic. Excludes the edited node itself (by id) and any
    homonym in a DIFFERENT file (that is a coincidental name clash, not a twin).

    Returns ``[(twin_name, twin_start_line), ...]`` ordered by line, capped.
    Correct-or-quiet: returns ``[]`` on any error or when no twin exists.
    """
    if not func_name or not os.path.exists(db_path):
        return []
    try:
        conn = _open_graph_db(db_path)
        # Resolve the edited node's stored file_path + parent_id so we compare
        # against the canonical path the graph stored (not the raw host path).
        me = conn.execute(
            "SELECT file_path, parent_id, start_line FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if me is None:
            conn.close()
            return []
        my_path = me["file_path"]
        my_parent = me["parent_id"]
        my_line = me["start_line"]
        rows = conn.execute(
            "SELECT id, file_path, parent_id, start_line FROM nodes "
            "WHERE name = ? AND label IN ('Function', 'Method') "
            "AND id != ? AND is_test = 0",
            (func_name, node_id),
        ).fetchall()
        conn.close()
        twins: list[tuple[str, int]] = []
        for r in rows:
            same_file = r["file_path"] == my_path
            same_class = (
                my_parent is not None
                and r["parent_id"] is not None
                and r["parent_id"] == my_parent
            )
            if not (same_file or same_class):
                continue
            line = r["start_line"] or 0
            if same_file and line == (my_line or 0):
                # Defensive: identical line in the same file is the node itself.
                continue
            twins.append((func_name, line))
        # De-dup by line, order by line ascending, cap to keep the signal tight.
        seen: set[int] = set()
        ordered: list[tuple[str, int]] = []
        for nm, ln in sorted(twins, key=lambda t: t[1]):
            if ln in seen:
                continue
            seen.add(ln)
            ordered.append((nm, ln))
        return ordered[:2]
    except Exception:
        return []


def _find_similar_functions(
    db_path: str, node_id: int, file_path: str
) -> list[tuple[str, str, int]]:
    """Find functions with similar fingerprints in the same package."""
    try:
        conn = _open_graph_db(db_path)
        my_fp = conn.execute(
            "SELECT value FROM properties WHERE node_id = ? AND kind = 'fingerprint'",
            (node_id,),
        ).fetchone()
        if not my_fp:
            conn.close()
            return []
        my_parts = dict(p.split(":", 1) for p in my_fp[0].split("|") if ":" in p)
        my_complexity = int(my_parts.get("complexity", "0"))
        my_calls = set(my_parts.get("calls", "").split(",")) - {""}
        if not my_calls:
            conn.close()
            return []
        pkg_dir = "/".join(file_path.replace("\\", "/").split("/")[:-1])
        if not pkg_dir:
            conn.close()
            return []
        _esc_pkg = _escape_like(pkg_dir)
        candidates = conn.execute(
            "SELECT n.name, n.file_path, p.value FROM properties p "
            "JOIN nodes n ON p.node_id = n.id "
            "WHERE p.kind = 'fingerprint' AND n.file_path LIKE ? ESCAPE '\\' "
            "AND n.id != ? AND n.is_test = 0 LIMIT 20",
            (f"{_esc_pkg}/%", node_id),
        ).fetchall()
        conn.close()
        similar = []
        for row in candidates:
            parts = dict(p.split(":", 1) for p in row["value"].split("|") if ":" in p)
            c = int(parts.get("complexity", "0"))
            if abs(c - my_complexity) <= 3:
                their_calls = set(parts.get("calls", "").split(",")) - {""}
                shared = my_calls & their_calls
                if len(shared) >= 2:
                    similar.append((row["name"], row["file_path"], len(shared)))
        return sorted(similar, key=lambda x: -x[2])[:2]
    except Exception:
        return []


def _get_test_assertions_from_graph(
    db_path: str, file_path: str, function_name: str
) -> list[dict[str, str]]:
    """Get test assertions targeting this function from graph.db."""
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        # Disambiguate target node first
        resolved_target_id = _resolve_node_id(db_path, file_path, function_name)
        if resolved_target_id is None:
            return results

        conn = _open_graph_db(db_path)

        # Check if assertions table exists
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "assertions" not in tables:
            conn.close()
            return []

        rows = conn.execute(
            """SELECT a.kind, a.expression, a.expected, a.line, n.name as test_name, n.file_path
               FROM assertions a
               JOIN nodes n ON a.test_node_id = n.id
               WHERE a.target_node_id = ?
               ORDER BY a.line LIMIT 8""",
            (resolved_target_id,),
        ).fetchall()

        # 2-hop fallback: find tests that assert foo() where foo() CALLS this function
        if not rows:
            rows = conn.execute(
                """SELECT DISTINCT a.kind, a.expression, a.expected, a.line, tn.name as test_name, tn.file_path
                   FROM assertions a
                   JOIN nodes tn ON a.test_node_id = tn.id
                   JOIN edges e ON a.target_node_id = e.source_id AND e.type = 'CALLS'
                   WHERE e.target_id = ? AND a.target_node_id > 0
                   ORDER BY a.line LIMIT 3""",
                (resolved_target_id,),
            ).fetchall()

        conn.close()

        # Rank by issue-keyword overlap + helper file deprioritization
        # PRIOR-003: _common.py/conftest.py/helper.py must not outrank direct tests
        _HELPER_PATTERNS = ("_common.py", "conftest.py", "helper.py", "helpers.py",
                            "fixtures.py", "utils.py", "base.py")
        _issue_terms = _load_issue_terms()
        if len(rows) > 1:
            def _test_relevance(r):
                score = 0
                # Deprioritize helper/support files (TCTracer ICSE 2020: naming convention signal)
                fp = (r["file_path"] or "").lower()
                if any(hp in fp for hp in _HELPER_PATTERNS):
                    score -= 100
                # Issue keyword overlap
                if _issue_terms:
                    text = ((r["test_name"] or "") + " " + (r["expression"] or "")).lower()
                    score += sum(1 for t in _issue_terms if t in text)
                return score
            rows = sorted(rows, key=_test_relevance, reverse=True)

        for row in rows[:3]:
            results.append({
                "kind": row["kind"] or "",
                "expression": row["expression"] or "",
                "expected": row["expected"] or "",
                "test_name": row["test_name"] or "",
                "test_file": row["file_path"] or "",
            })
    except Exception as e:
        _append_gt_log("get_test_assertions_error", str(e))

    return results


def _discover_test_files_by_convention(
    db_path: str, file_path: str, repo_root: str = ""
) -> list[str]:
    """TCTracer naming convention: find test files without graph edges.

    Searches graph.db nodes for test files matching naming patterns:
    - test_<stem>.py for <stem>.py
    - <stem>_test.py
    - test_<stem>_*.py (prefix match)
    Then validates they exist on disk.
    """
    import sqlite3 as _sq
    stem = os.path.splitext(os.path.basename(file_path))[0]
    if not stem or not db_path:
        return []
    if not repo_root:
        repo_root = os.environ.get("GT_REPO_ROOT", "/testbed")
    patterns = [f"test_{stem}", f"{stem}_test", f"test_{stem}s", f"tests_{stem}"]
    try:
        conn = _sq.connect(db_path)
        all_test_files = conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE is_test = 1"
        ).fetchall()
        conn.close()
        matched = []
        for (tf,) in all_test_files:
            tf_stem = os.path.splitext(os.path.basename(tf))[0]
            if any(tf_stem == p or tf_stem.startswith(p + "_") for p in patterns):
                full = os.path.join(repo_root, tf)
                if os.path.isfile(full):
                    matched.append(tf)
        return matched[:5]
    except Exception:
        return []


def _get_test_assertions_from_file(
    db_path: str, file_path: str, function_name: str, repo_root: str = ""
) -> list[str]:
    """Fallback: find test files via graph edges + naming convention, grep for assertions."""
    import sqlite3 as _sq
    if not repo_root:
        repo_root = os.environ.get("GT_REPO_ROOT", "/testbed")
    try:
        conn = _sq.connect(db_path)
        _resolved_test_path = _resolve_file_path(conn, file_path)
        rows = conn.execute(
            """SELECT DISTINCT nsrc.file_path FROM nodes nt
            JOIN edges e ON e.target_id = nt.id
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ? AND nsrc.is_test = 1
            LIMIT 3""",
            (_resolved_test_path,),
        ).fetchall()
        conn.close()
        # TCTracer naming convention fallback: if graph edges found no test
        # files, discover via test_<stem>.py pattern (graph-independent).
        if not rows:
            convention_files = _discover_test_files_by_convention(db_path, file_path, repo_root)
            if convention_files:
                rows = [(f,) for f in convention_files]
        assertions = []
        for (test_file,) in rows:
            try:
                full = os.path.join(repo_root, test_file)
                with open(full, encoding="utf-8", errors="ignore") as tf:
                    for line in tf:
                        stripped = line.strip()
                        _is_assertion = (
                            stripped.startswith("assert")
                            or stripped.startswith("self.assert")
                            or ".assert_called" in stripped
                            or ".assert_any_call" in stripped
                            or ".assert_not_called" in stripped
                            or ".assert_has_calls" in stripped
                        )
                        if function_name in stripped and _is_assertion:
                            assertions.append(f"{test_file}: {clip_balanced(stripped, 120)}")
                            if len(assertions) >= 3:
                                return assertions
            except OSError:
                continue
        if not assertions:
            issue_terms = _load_issue_terms()
            if issue_terms:
                for (test_file,) in rows:
                    try:
                        full = os.path.join(repo_root, test_file)
                        with open(full, encoding="utf-8", errors="ignore") as tf:
                            for line in tf:
                                stripped = line.strip()
                                if stripped.startswith(("assert", "self.assert", "expect(", "EXPECT_", "CHECK(")) or ".assert_called" in stripped or ".assert_any_call" in stripped or ".assert_not_called" in stripped or ".assert_has_calls" in stripped:
                                    hits = sum(1 for t in issue_terms if t in stripped.lower())
                                    if hits > 0:
                                        assertions.append(f"{test_file}: {clip_balanced(stripped, 120)}")
                                        if len(assertions) >= 3:
                                            return assertions
                    except OSError:
                        continue
        # Patch F: anchor-based test discovery (bonus visible-test evidence)
        if not assertions:
            _anchors = _load_issue_anchors()
            _test_names = _anchors.get("test_names", [])
            if _test_names:
                for (test_file,) in rows:
                    try:
                        full = os.path.join(repo_root, test_file)
                        in_target_func = False
                        with open(full, encoding="utf-8", errors="ignore") as tf:
                            for line in tf:
                                stripped = line.strip()
                                if any(tn in stripped for tn in _test_names if stripped.startswith(("def ", "func ", "fn "))):
                                    in_target_func = True
                                elif in_target_func and stripped.startswith(("def ", "func ", "fn ", "class ")):
                                    in_target_func = False
                                elif in_target_func and stripped.startswith(("assert", "self.assert", "expect(", "EXPECT_", "CHECK(")):
                                    assertions.append(f"{test_file}: {clip_balanced(stripped, 120)}")
                                    if len(assertions) >= 3:
                                        return assertions
                    except OSError:
                        continue
        return assertions
    except Exception:
        return []


def _find_nearest_candidate(
    file_path: str, brief_candidates: list[str], db_path: str
) -> str:
    """Find the nearest brief candidate connected to this file via graph.db edges."""
    import sqlite3 as _sqlite3

    if not brief_candidates:
        return ""
    try:
        conn = _open_graph_db(db_path)
        _resolved_nc = _resolve_file_path(conn, file_path)

        for cand in brief_candidates:
            _resolved_cand = _resolve_file_path(conn, cand)
            # Check if there's an edge between this file and the candidate
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM edges e
                   JOIN nodes nsrc ON e.source_id = nsrc.id
                   JOIN nodes ntgt ON e.target_id = ntgt.id
                   WHERE COALESCE(e.confidence, 0.5) >= 0.7
                     AND ((nsrc.file_path = ? AND ntgt.file_path = ?)
                      OR (nsrc.file_path = ? AND ntgt.file_path = ?))
                   LIMIT 1""",
                (_resolved_nc, _resolved_cand, _resolved_cand, _resolved_nc),
            ).fetchone()
            if row and row[0] > 0:
                conn.close()
                return cand

        conn.close()
    except Exception as e:
        _append_gt_log("pick_best_candidate_error", str(e))

    # If no graph connection found, return first candidate as reference
    return brief_candidates[0] if brief_candidates else ""


def _signature_param_count(signature: str) -> int | None:
    """Parameter count from signature like 'def f(a, b, c=1)'. Excludes self/cls."""
    if not signature:
        return None
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return 0
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    filtered = [p for p in parts if p.split(":")[0].split("=")[0].strip() not in ("self", "cls")]
    return len(filtered)


def _signature_has_varargs(signature: str) -> bool:
    """Check if signature contains *args or **kwargs."""
    if not signature:
        return False
    return "*" in signature


def _signature_default_count(signature: str) -> int:
    """Count parameters with default values."""
    if not signature:
        return 0
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return 0
    inner = m.group(1).strip()
    if not inner:
        return 0
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    return sum(1 for p in parts if "=" in p and p.split(":")[0].split("=")[0].strip() not in ("self", "cls"))


def _extract_call_arity(code: str, function_name: str) -> int | None:
    """Approximate arity of how function_name is called in a code snippet."""
    if not code or not function_name:
        return None
    idx = code.find(function_name + "(")
    if idx < 0:
        return None
    open_idx = idx + len(function_name)
    depth = 0
    args = 0
    has_content = False
    for ch in code[open_idx:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                if has_content:
                    args += 1
                break
        elif depth == 1 and ch == ",":
            args += 1
        elif depth == 1 and not ch.isspace():
            has_content = True
    return args


def _check_arity_mismatch(
    new_signature: str,
    func_name: str,
    callers: list[dict[str, str]],
    edited_files: list[str],
) -> str:
    """Compare new signature arity against caller call arity.

    Returns a warning string or '' if no mismatch.
    Suppresses when: *args/**kwargs present, no callers, all callers edited,
    or defaults cover the gap.
    """
    from groundtruth.config.signal_thresholds import (
        SIGNATURE_HIGH_CONFIDENCE_METHODS,
        SIGNATURE_MEDIUM_CONFIDENCE_METHODS,
        log_threshold_use,
    )

    if _signature_has_varargs(new_signature):
        return ""

    new_arity = _signature_param_count(new_signature)
    if new_arity is None:
        return ""

    default_count = _signature_default_count(new_signature)
    min_required = new_arity - default_count

    mismatches = []
    for c in callers[:5]:
        caller_file = c.get("file", "")
        # Skip callers the agent already edited
        if any(caller_file in ef or ef in caller_file for ef in edited_files):
            continue
        call_arity = _extract_call_arity(c.get("code", ""), func_name)
        if call_arity is None:
            continue
        # Mismatch: caller passes fewer args than minimum required
        if call_arity < min_required:
            res_method = c.get("resolution_method", "")
            if res_method in SIGNATURE_HIGH_CONFIDENCE_METHODS:
                confidence = "high"
            elif res_method in SIGNATURE_MEDIUM_CONFIDENCE_METHODS:
                confidence = "medium"
            else:
                confidence = "medium"
            mismatches.append((caller_file, c.get("line", "?"), call_arity, confidence))

    if not mismatches:
        return ""

    # Use highest confidence among mismatches
    best_conf = "high" if any(m[3] == "high" for m in mismatches) else "medium"
    caller_refs = ", ".join(f"{m[0]}:{m[1]}" for m in mismatches[:2])

    log_threshold_use(
        "SIGNATURE_MISMATCH", best_conf,
        f"func={func_name} new_arity={new_arity} min_required={min_required} mismatches={len(mismatches)}",
    )

    if best_conf == "high":
        return (
            f"[GT_CONTRACT high] {func_name}() now requires {min_required}+ args. "
            f"{len(mismatches)} caller(s) pass fewer: {caller_refs}. Update callers."
        )
    else:
        return (
            f"[GT_CONTRACT medium] Possible arity change in {func_name}(). "
            f"Caller at {caller_refs} may need update."
        )


def _classify_test_target(test_file: str, test_name: str) -> str:
    """Classify a test target as real test, conftest fixture, utility, or non-test.

    Returns 'real_test', 'conftest', 'test_utility', or 'non_test'.
    """
    base = os.path.basename(test_file).lower()
    if base == "conftest.py":
        return "conftest"
    if base.startswith("utils") or base == "helpers.py" or base.startswith("common"):
        return "test_utility"
    if not base.startswith("test_") and not base.endswith("_test.py"):
        return "non_test"
    return "real_test"


# Fix 2 (5-lang parity): language-aware test runner dispatch.
# Detects language from test file extension and emits the correct runner.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".kt": "kotlin",
}


def _format_test_command(test_file: str, test_name: str) -> str:
    """Format the test runner command based on the test file's language.

    Detects language from the file extension and returns the idiomatic
    test invocation for that language. Falls back to pytest for unknown
    extensions (safe default: Python is the most common case).
    """
    ext = os.path.splitext(test_file)[1].lower()
    lang = _EXT_TO_LANG.get(ext, "python")

    if lang == "python":
        return f"pytest {test_file}::{test_name}"
    elif lang == "go":
        # Go test: -run takes a regex matching the test function name.
        # Package path derived from the test file's directory.
        pkg_dir = os.path.dirname(test_file) or "."
        return f"go test -run {test_name} ./{pkg_dir}"
    elif lang == "rust":
        return f"cargo test {test_name}"
    elif lang in ("typescript", "javascript"):
        return f"npx jest {test_file}"
    elif lang in ("java", "kotlin"):
        return f"mvn test -Dtest={test_name}"
    else:
        return f"pytest {test_file}::{test_name}"


def _get_targeted_verification_suggestion(
    db_path: str, file_path: str, function_names: list[str],
) -> str:
    """Query graph.db for test file connected to edited function.

    Returns labeled suggestion: [GT_VERIFY high/medium/low] Run: <command>
    Labels based on edge resolution_method and test target classification.
    Language-aware: emits the correct test runner per file extension.
    No suppression — all confidence levels emitted with labels.
    """
    from groundtruth.config.signal_thresholds import (
        VERIFY_LABEL_HIGH_METHODS,
        VERIFY_LABEL_MEDIUM_METHODS,
        log_threshold_use,
    )
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        _resolved_verify = _resolve_file_path(conn, file_path)
        if _resolved_verify is None:
            # Unknown / ambiguous path -> stay silent (correct-or-quiet).
            conn.close()
            return ""

        # Check if resolution_method column exists
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        has_resolution = "resolution_method" in cols

        # Build file path matcher: exact match OR LIKE suffix fallback
        _esc_verify = _escape_like(_resolved_verify)
        _file_clause = "(n1.file_path = ? OR n1.file_path LIKE ? ESCAPE '\\')"
        _file_params_base = (_resolved_verify, f"%/{_esc_verify}")

        for func_name in function_names[:2]:
            _params = _file_params_base + (func_name,)
            if has_resolution:
                rows = conn.execute(
                    f"""SELECT DISTINCT n2.file_path, n2.name,
                              COALESCE(e.resolution_method, '') as res_method,
                              COALESCE(e.confidence, 0.5) as conf
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                         AND COALESCE(e.confidence, 0.5) >= 0.5
                       JOIN nodes n2 ON (
                           CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END = n2.id
                       )
                       WHERE {_file_clause} AND n1.name = ? AND n2.is_test = 1
                       ORDER BY e.confidence DESC
                       LIMIT 3""",
                    _params,
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT DISTINCT n2.file_path, n2.name, '' as res_method, 0.5 as conf
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                       JOIN nodes n2 ON (
                           CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END = n2.id
                       )
                       WHERE {_file_clause} AND n1.name = ? AND n2.is_test = 1
                       LIMIT 3""",
                    _params,
                ).fetchall()

            if not rows:
                continue

            # Pick best candidate and label it
            for row in rows:
                test_file = row[0]
                test_name = row[1]
                res_method = row[2]
                edge_conf = row[3]

                target_class = _classify_test_target(test_file, test_name)

                # Confidence label — check disqualifiers first
                if target_class in ("conftest", "test_utility", "non_test"):
                    label = "low"
                elif edge_conf < 0.5:
                    label = "low"
                elif res_method in VERIFY_LABEL_HIGH_METHODS and target_class == "real_test":
                    label = "high"
                elif res_method in VERIFY_LABEL_MEDIUM_METHODS and target_class == "real_test":
                    label = "medium"
                else:
                    label = "medium"

                log_threshold_use(
                    "VERIFY_LABEL", label,
                    f"test={test_file}::{test_name} res={res_method} conf={edge_conf:.2f} class={target_class}",
                )
                conn.close()
                return f"[GT_VERIFY {label}] Run: {_format_test_command(test_file, test_name)}"

        # Fallback: assertions table (target-linked tests)
        try:
            conn.execute("SELECT 1 FROM assertions LIMIT 1")
            _tgt_clause = "(tgt.file_path = ? OR tgt.file_path LIKE ? ESCAPE '\\')"
            _assert_rows = conn.execute(
                f"SELECT DISTINCT tn.file_path, tn.name FROM assertions a "
                f"JOIN nodes tn ON a.test_node_id = tn.id "
                f"JOIN nodes tgt ON a.target_node_id = tgt.id "
                f"WHERE {_tgt_clause} AND a.target_node_id > 0 "
                f"LIMIT 2",
                _file_params_base,
            ).fetchall()
            if _assert_rows:
                _tf, _tn = _assert_rows[0]
                conn.close()
                return f"[GT_VERIFY medium] Run: {_format_test_command(_tf, _tn)}"
        except Exception:
            pass

        conn.close()
    except Exception as e:
        _append_gt_log("get_verification_hint_error", str(e))
    return ""


def format_risk_evidence(
    callers: list[dict[str, str]],
    function_name: str,
    confidence: float,
) -> list[str]:
    """Format caller evidence using confidence-gated risk framing.

    Rendering tiers:
      - confidence >= 0.9 and callers >= 3: risk warning with top files
      - confidence >= 0.9 and callers 1-2: factual caller code lines
      - confidence >= 0.5 (but < 0.9): soft unverified note
      - confidence < 0.5 or no callers: silence (empty list)

    Returns a list of formatted evidence lines (0-3 items max).
    """
    if not callers:
        return []

    num_callers = len(callers)

    if confidence >= 0.9 and num_callers >= 3:
        unique_files = list(dict.fromkeys(c["file"] for c in callers))
        top_files = ", ".join(
            f.rsplit("/", 1)[-1] if "/" in f else f
            for f in unique_files[:3]
        )
        lines: list[str] = [
            f"[CONTRACT] {num_callers} callers depend on {function_name}() — changes here affect {top_files}:"
        ]
        for c in callers[:2]:
            lines.append(_format_caller_line(c))
        return lines

    if confidence >= 0.9:
        lines = [f"[CONTRACT] callers of {function_name}():"]
        for c in callers[:2]:
            lines.append(_format_caller_line(c))
        return lines

    lines = [f"[CONTRACT ~] possible callers of {function_name}() (unverified):"]
    for c in callers[:2]:
        lines.append(_format_caller_line(c))
    return lines


def _format_param_display(param_value: str) -> str:
    """'x: int = 5' → 'x: int [optional, default=5]'"""
    if "=" in param_value:
        name_type, default = param_value.rsplit("=", 1)
        return f"{name_type.strip()} [optional, default={default.strip()}]"
    pv = param_value.strip()
    # Don't double-stamp when the source value already carries the tag
    # (live defect: "PARAMS: lib [required] [required]").
    return pv if pv.endswith("[required]") or "[required]" in pv else f"{pv} [required]"


# [BEHAVIORAL CONTRACT] line normalization (C1c empty-value suppression +
# C1d dedup + ordering). Each contract line is ``  <PREFIX> <value>`` where
# PREFIX is either ``WORD:`` (PARAMS:/PRESERVE:/FIELD:/READS:) or ``[MARKER]``
# (RAISES/RETURNS/RESOURCE/CATCHES/...). A line carries no fact when its value
# is empty — correct-or-quiet, drop it (the verified empty ``PRESERVE:`` haystack
# defect). Guards/returns/raises are the deciding contract content; they must
# render BEFORE params/resources so a downstream char cap (owned by the wrapper)
# keeps them. Pure / module-level for unit testing.
_CONTRACT_HIGH_VALUE_PREFIXES = (
    "PRESERVE:", "[RAISES]", "[RETURNS]", "[CATCHES]", "[BOUNDARY]",
)
# Conditional-return / classified-return lines render as ``L<line>: <expr>`` —
# high-value return-path facts. Matched by shape (not a bare ``L`` prefix, which
# would falsely promote any future ``L``-prefixed marker).
_CONTRACT_RETURN_LINE_RE = re.compile(r"^L\d+:")
_CONTRACT_LOW_VALUE_PREFIXES = (
    "PARAMS:", "[RESOURCE]", "FIELD:", "READS:", "[CONFIG]", "[ORDER]",
    "[CONCURRENCY]", "[SECURITY]", "[SERDE]", "[TWIN]",
)


def _contract_line_value(line: str) -> str:
    """Return the VALUE part of a ``  <PREFIX> <value>`` contract line, i.e. the
    text after a leading ``WORD:`` or ``[MARKER]`` prefix. When the line has no
    recognizable prefix the whole stripped line is treated as the value."""
    s = line.strip()
    if s.startswith("["):
        close = s.find("]")
        if close != -1:
            return s[close + 1:].strip()
    m = _re.match(r"^[A-Za-z_]\w*:\s*(.*)$", s)
    if m:
        return m.group(1).strip()
    return s


def _contract_sort_rank(line: str) -> int:
    """0 for high-value (guards/returns/raises), 1 for low-value (params/
    resources/fields), 2 for everything else — used as a STABLE sort key so
    high-value contract content survives a downstream cap (C1d ordering)."""
    s = line.strip()
    if any(s.startswith(p) for p in _CONTRACT_HIGH_VALUE_PREFIXES) or _CONTRACT_RETURN_LINE_RE.match(s):
        return 0
    if any(s.startswith(p) for p in _CONTRACT_LOW_VALUE_PREFIXES):
        return 1
    return 2


def _normalize_contract_lines(lines: list[str]) -> list[str]:
    """Drop empty-value lines, dedup (first-occurrence order), then stably reorder
    so guards/returns/raises precede params/resources.

    C1c: a contract line whose VALUE is blank carries no fact -> drop it; if every
    line is empty the result is [] and the caller suppresses the whole header.
    C1d: exact-duplicate lines are collapsed; high-value content sorts ahead of
    low-value content (stable within each rank). Correct-or-quiet, generalized."""
    kept: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if not _contract_line_value(ln):
            continue  # C1c: empty value -> no fact
        key = ln.strip()
        if key in seen:
            continue  # C1d: exact duplicate
        seen.add(key)
        kept.append(ln)
    # C1d ordering: stable sort by value-rank keeps guards/returns/raises first.
    kept.sort(key=_contract_sort_rank)
    return kept


def _map_args_to_params(call_line: str, func_name: str, params: list[str]) -> str:
    """Map caller arguments to callee parameters.

    'result = validate(token, strict=True)' + params=['value','strict']
    -> 'passes token→value, strict=True→strict'
    """
    import re as _re_map
    # Find the function call and extract args with balanced-paren awareness
    start_match = _re_map.search(rf'{_re_map.escape(func_name)}\s*\(', call_line)
    if not start_match:
        return ""
    start = start_match.end()
    depth, end = 1, start
    while end < len(call_line) and depth > 0:
        if call_line[end] == '(':
            depth += 1
        elif call_line[end] == ')':
            depth -= 1
        end += 1
    if depth != 0:
        return ""
    arg_str = call_line[start:end - 1]
    # Split on commas at depth 0 only
    args: list[str] = []
    current, d = [], 0
    for ch in arg_str:
        if ch == '(' : d += 1
        elif ch == ')': d -= 1
        elif ch == ',' and d == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current).strip())
    args = [a for a in args if a]
    mappings: list[str] = []
    for i, arg in enumerate(args[:len(params)]):
        if "=" in arg and "==" not in arg:
            mappings.append(arg)
        else:
            mappings.append(f"{arg}→{params[i]}")
    return ", ".join(mappings[:3])


def _format_caller_line(c: dict) -> str:
    code = c.get("code", "")
    pre = c.get("pre_context", "")
    usage = c.get("return_usage", "")
    mapping = c.get("arg_mapping", "")
    usage_tag = f" [{usage}]" if usage and usage != "assignment" else ""
    mapping_tag = f" passes {mapping}" if mapping else ""
    if code:
        _code_raw = code.split(" | ")[0] if " | " in code else code
        code_first = clip_balanced(_code_raw, 120)
        if pre:
            return f"  {c['file']}:{c['line']} `{pre} >> {code_first}`{mapping_tag}{usage_tag}"
        return f"  {c['file']}:{c['line']} `{code_first}`{mapping_tag}{usage_tag}"
    return f"  {c['file']}:{c['line']}{usage_tag}"


def _format_callee_entry(name: str, signature: str, file_path: str) -> str:
    """Render one "Calls into:" entry with the callee's signature.

    TASK #49: the decisive callee (e.g. ``set_parse(self, key, string: str)``)
    was previously listed by bare name with no contract, so the agent could not
    see what arguments/types it expects. We join ``nodes.signature`` so each
    callee shows its interface.

    Correct-or-quiet: when the signature is missing/blank, fall back to the bare
    name rather than emitting a misleading placeholder. The signature is rendered
    as-is when it already embeds the name (most tree-sitter specs store
    ``name(params)``); otherwise the name is prefixed so a bare ``(params)`` or
    param-list signature still reads as ``name(params)``.

    Pure function — module-level for unit testing.
    """
    name = (name or "").strip()
    sig = (signature or "").strip()
    if not sig:
        return f"{name} ({file_path})"
    # Drop any trailing "-> ret" so the callee line stays compact; the contract
    # that matters at the call site is the parameter list.
    head = sig.split(" -> ")[0].strip() if " -> " in sig else sig
    # Strip a leading "def "/"func "/"fn " keyword if the spec stored one.
    for _kw in ("def ", "func ", "fn ", "function "):
        if head.startswith(_kw):
            head = head[len(_kw):].strip()
            break
    if name and not head.startswith(name + "(") and not head.startswith(name + " "):
        # Signature is a bare param list (e.g. "(self, key, string: str)") or a
        # different shape — prefix the name so the entry reads name(params).
        if head.startswith("("):
            rendered = f"{name}{head}"
        else:
            rendered = f"{name}({head})" if not head.startswith("(") and "(" not in head else f"{name} {head}"
    else:
        rendered = head
    return f"{rendered} ({file_path})"


def _passes_relevance_gate(
    candidate_text: str, issue_terms: set[str], fn_tokens: set[str]
) -> bool:
    """TASK #47 relevance gate for non-edge signals ([RECALL]/[SIMILAR]/[FORMAT]).

    A non-edge signal is only rendered when its text overlaps EITHER the issue
    terms OR the edited function's identifier tokens. The categorical edge filter
    gates CALLS/IMPORTS edges by structural trust, but these signals are derived
    from fingerprints / stale dumps / fixture keys and carry no edge — so they
    need a relevance gate or they inject noise unkeyed to the edit.

    Correct-or-quiet: when neither issue terms nor fn tokens are available we
    cannot judge relevance, so we DROP the signal (return False) rather than
    laundering an unrelated entry as evidence. (Wrong info that misdirects the
    agent is worse than no info.)

    Pure function — module-level for unit testing.
    """
    text = (candidate_text or "").lower()
    if not text:
        return False
    anchor = {t for t in (issue_terms or set()) if t} | {t for t in (fn_tokens or set()) if t}
    if not anchor:
        # No relevance anchor available — stay silent rather than guess.
        return False
    return any(a in text for a in anchor)


def _identifier_tokens(name: str) -> set[str]:
    """Split a snake_case / camelCase identifier into lowercase sub-tokens.

    Used to build the edited function's relevance anchor for the TASK #47 gate.
    ``set_fields`` -> {"set", "fields", "set_fields"}; ``embedAlbum`` ->
    {"embed", "album", "embedalbum"}.
    """
    import re as _re
    n = (name or "").strip()
    if not n:
        return set()
    parts = _re.split(r"[_\W]+|(?<=[a-z0-9])(?=[A-Z])", n)
    toks = {p.lower() for p in parts if p and len(p) >= 3}
    toks.add(n.lower())
    return toks


def generate_improved_evidence(
    file_path: str,
    function_names: list[str],
    db_path: str,
    repo_root: str,
    *,
    mode: str = "post_edit",
    iteration_ratio: float = 0.0,
    diff_text: str = "",
    _evidence_accumulator: list[dict] | None = None,
) -> str:
    """Generate priority-ordered evidence from graph.db.

    Priority order (stop at 1200 chars / ~300 tokens):
      1. Caller CODE lines (unseen by agent first)
      2. Sibling function pattern
      3. Signature + return type
      4. Test assertions (bonus)

    Decision 22 Fix 5: L3 fully decoupled from L1. Evidence depth is
    determined by the file's graph connectivity (edge confidence), not
    by whether L1 produced candidates. Files with high-confidence edges
    (≥0.5) get full evidence; files with only low-confidence or no edges
    get signature-only.

    Dynamic:
      - Tracks edited_files for unseen-caller prioritization
      - Decay: full on first 3 edits, lighter after
    """
    if not os.path.exists(db_path):
        return ""

    # Load trajectory state
    edited_files = _read_lines_file(_EDITED_FILES_PATH)
    edit_count = len(edited_files)

    # L3 POST-EDIT = VERIFICATION layer.
    # All evidence fires on every file regardless of graph connectivity.
    # The confidence filter is inside each query (conf >= 0.5 for callers).
    # If a query returns nothing, that mechanism simply produces no output.
    # Previously gated behind file_class == "connected" which silently
    # blocked all evidence on files with sparse graph data — exactly where
    # the agent needs help most.

    # Decay: after 3 edits, reduce evidence density
    base_max = 3 if edit_count <= 3 else 2
    max_callers = base_max  # adjusted per-function below

    # Feature-flagged mode support (Change 3)
    rebuild_l3 = os.environ.get("GT_REBUILD_L3", "0") == "1"
    effective_mode = mode if rebuild_l3 else "post_edit"
    effective_ratio = iteration_ratio if rebuild_l3 else 0.0

    # Late-repair mode: linear decay instead of binary cliff (Change 4)
    if effective_mode == "post_edit" and effective_ratio > 0.40:
        decay = min(0.5, (effective_ratio - 0.40) * 0.83)  # 0 at 0.40, 0.5 at 1.0
        effective_max_chars = int(_MAX_EVIDENCE_CHARS * (1.0 - decay))
    else:
        effective_max_chars = _MAX_EVIDENCE_CHARS

    output_parts: list[str] = []
    chars_used = 0

    # Post-failure mode header
    if effective_mode == "post_failure":
        output_parts.append("[GT L3: post_failure]")
        chars_used += 25

    for func_name in function_names:  # budget-based cap replaces hard [:3] limit
        if chars_used > effective_max_chars * 0.8 and func_name != function_names[0]:
            print(f"[GT_META] func_budget_exhausted: skipping {func_name} chars_used={chars_used}/{effective_max_chars}", file=sys.stderr, flush=True)
            break
        func_parts: list[str] = []
        callers: list[dict[str, str]] = []
        total_callers = 0

        # --- Late-repair: only signature + top 1 caller (Change 4) ---
        if effective_ratio >= 0.80 and effective_mode == "post_edit":
            sig = _get_signature_from_graph(db_path, file_path, func_name)
            if sig:
                func_parts.append(f"SIGNATURE: {sig}")
                if " -> " in sig:
                    ret_type = sig.split(" -> ")[-1].strip()
                    if ret_type and ret_type != "None":
                        func_parts.append(f"MUST PRESERVE: returns {ret_type}")
            callers = _get_callers_from_graph(
                db_path, file_path, func_name, repo_root,
                seen_files=edited_files, limit=3,
            )
            if callers:
                func_parts.append("TOP CALLER:")
                c = callers[0]
                code = c["code"]
                if code:
                    func_parts.append(f"  {c['file']}:{c['line']}  → {code}")
            # Skip full evidence pipeline for late repair
            if func_parts:
                block = "\n".join(func_parts)
                if chars_used + len(block) <= effective_max_chars:
                    output_parts.append(block)
                    chars_used += len(block) + 1
            continue

        # --- Priority 0.5: Behavioral Contract (conditional structure + return paths) ---
        # Fires on every edit. On sh-744, contract on subsequent edit caught bad __await__
        # removal → agent self-corrected → flip. Cannot suppress without killing flips.
        if chars_used < effective_max_chars - 200:
            try:
                func_body_for_contract = ""
                func_start = None
                func_end = None
                _bc_node_id = None
                try:
                    import sqlite3 as _sq_bc
                    if not os.path.exists(db_path):
                        print(f"[GT_META] behavioral_contract: db_missing:{db_path}", file=sys.stderr, flush=True)
                    else:
                        _conn_bc = _sq_bc.connect(db_path)
                        # P0-1: generalized path suffix resolver
                        # Query by name only, then match by path component suffix in Python
                        _runtime_parts = file_path.replace("\\", "/").lstrip("./").lstrip("/").split("/")
                        _candidates_bc = _conn_bc.execute(
                            "SELECT id, start_line, end_line, file_path FROM nodes WHERE name = ?",
                            (func_name,),
                        ).fetchall()
                        _conn_bc.close()
                        _row_bc = None
                        _best_match_len = -1
                        for _nid, _start, _end, _graph_path in _candidates_bc:
                            _graph_parts = _graph_path.replace("\\", "/").split("/")
                            # Check if graph path components are a suffix of runtime path components
                            if len(_graph_parts) <= len(_runtime_parts):
                                if _runtime_parts[-len(_graph_parts):] == _graph_parts:
                                    if len(_graph_parts) > _best_match_len:
                                        _best_match_len = len(_graph_parts)
                                        _row_bc = (_start, _end)
                                        _bc_node_id = _nid
                        if _row_bc:
                            func_start, func_end = _row_bc
                        else:
                            print(f"[GT_META] behavioral_contract: no_node:{func_name}@{file_path} candidates={len(_candidates_bc)}", file=sys.stderr, flush=True)
                except Exception as _bc_db_exc:
                    print(f"[GT_META] behavioral_contract_db_error: {_bc_db_exc}", file=sys.stderr, flush=True)
                print(f"[GT_META] behavioral_contract: func={func_name} file={file_path} start={func_start} end={func_end}", file=sys.stderr, flush=True)
                if func_start and func_end:
                    full_path = os.path.join(repo_root, file_path) if repo_root else file_path
                    try:
                        with open(full_path, encoding="utf-8", errors="ignore") as _f_bc:
                            all_lines = _f_bc.readlines()
                        func_body_for_contract = "".join(all_lines[func_start - 1 : func_end])
                    except OSError as _bc_os_exc:
                        print(f"[GT_META] behavioral_contract_file_error: {_bc_os_exc}", file=sys.stderr, flush=True)
                print(f"[GT_META] behavioral_contract: body_len={len(func_body_for_contract)}", file=sys.stderr, flush=True)
                # B2: also handle short bodies (<=20 chars) as full-body contract
                if func_body_for_contract and len(func_body_for_contract) <= 20:
                    _body_lines_short = func_body_for_contract.splitlines()
                    _body_only_short = _body_lines_short[1:] if len(_body_lines_short) > 1 else _body_lines_short
                    if _body_only_short and len(_body_only_short) <= 5:
                        func_parts.append(f"[BEHAVIORAL CONTRACT] (full body — {len(_body_only_short)} lines)")
                        for _bl in _body_only_short:
                            func_parts.append(f"  {_bl.rstrip()}")
                if func_body_for_contract and len(func_body_for_contract) > 20:
                    # --- Properties-first path: query graph.db properties table ---
                    _props_contract_lines: list[str] = []
                    _props_param_lines: list[str] = []
                    _props_used = False
                    if _bc_node_id is not None and os.path.exists(db_path):
                        try:
                            _props_conn = _open_graph_db(db_path)
                            _props = _props_conn.execute(
                                "SELECT kind, value, line FROM properties WHERE node_id = ? ORDER BY line",
                                (_bc_node_id,)
                            ).fetchall()
                            _props_conn.close()
                            if _props:
                                _props_used = True
                                _raises_types: list[str] = []  # exception_type values (dedup, cap 3)
                                _return_shapes: list[str] = []  # return_shape values (dedup, cap 2)
                                _exc_flow_values: list[str] = []  # emitted exception_flow values (for Tier-A dedup)
                                for _prop in _props:
                                    _pk, _pv, _pl = _prop["kind"], _prop["value"], _prop["line"]
                                    # C1 chokepoint: every {_pv} render below is an
                                    # arbitrary SOURCE-TEXT VALUE stored by the indexer
                                    # (guard/raise/catch/conditional/field/etc.). An older
                                    # indexer build may have stored it byte-truncated
                                    # mid-string/expr; clip_balanced repairs it to the
                                    # longest well-formed prefix so a truncation can never
                                    # reach the agent unbalanced. No-op on balanced values
                                    # (short identifiers like param/exception_type/return_shape
                                    # pass through unchanged).
                                    if isinstance(_pv, str) and _pv:
                                        _pv = clip_balanced(_pv)
                                    if _pk == "guard_clause":
                                        # C1c: a blank guard value carries no fact
                                        # (the verified empty ``PRESERVE:`` haystack
                                        # defect) — drop it at the source rather than
                                        # emit a header+empty line. _normalize_contract_lines
                                        # is the belt-and-suspenders below.
                                        if _pv and _pv.strip():
                                            _props_contract_lines.append(f"  PRESERVE: {_pv}")
                                    elif _pk == "conditional_return":
                                        _props_contract_lines.append(f"  L{_pl}: {_pv}")
                                    elif _pk == "side_effect":
                                        _props_contract_lines.append(f"  {_pv}")
                                    elif _pk == "security_tag":
                                        _props_contract_lines.append(f"  [SECURITY] {_pv}")
                                    elif _pk == "serialization_pair":
                                        _props_contract_lines.append(f"  [SERDE] {_pv}")
                                    elif _pk == "param":
                                        _props_param_lines.append(_pv)
                                    elif _pk == "exception_flow":
                                        _props_contract_lines.append(f"  [RAISES] {_pv}")
                                        _exc_flow_values.append(_pv)
                                    elif _pk == "exception_type":
                                        # Indexer emits 100s/repo; dedup + cap 3, aggregated on one line below.
                                        if _pv and _pv not in _raises_types and len(_raises_types) < 3:
                                            _raises_types.append(_pv)
                                    elif _pk == "exception_handler":
                                        _props_contract_lines.append(f"  [CATCHES] {_pv}")
                                    elif _pk == "class_field":
                                        _props_contract_lines.append(f"  FIELD: {_pv}")
                                    elif _pk == "field_read":
                                        _props_contract_lines.append(f"  READS: {_pv}")
                                    elif _pk == "boundary_condition":
                                        _props_contract_lines.append(f"  [BOUNDARY] {_pv}")
                                    elif _pk == "concurrency_pattern":
                                        _props_contract_lines.append(f"  [CONCURRENCY] {_pv}")
                                    elif _pk == "config_read":
                                        _props_contract_lines.append(f"  [CONFIG] {_pv}")
                                    elif _pk == "call_order":
                                        _props_contract_lines.append(f"  [ORDER] {_pv}")
                                    elif _pk == "resource_pattern":
                                        _props_contract_lines.append(f"  [RESOURCE] {_pv}")
                                    elif _pk == "structural_twin":
                                        _props_contract_lines.append(f"  [TWIN] {_pv}")
                                    elif _pk == "return_shape":
                                        # Indexer emits 100s-1000s/repo; dedup + cap 2, rendered below.
                                        if _pv and _pv not in _return_shapes and len(_return_shapes) < 2:
                                            _return_shapes.append(_pv)
                                    elif _pk == "visibility":
                                        pass  # stored for MCP query, not displayed inline
                                    elif _pk == "fingerprint":
                                        pass  # stored for MCP query, not displayed
                                # Aggregate exception_type / return_shape (deduped, capped above).
                                # Insert at front (protected position) so the END-popping budget
                                # trim drops low-value per-prop detail first, not these high-value
                                # lines. PARAMS is inserted at index 0 later, pushing these below it.
                                # Tier-A dedup: drop exception_type values already covered by an
                                # emitted Tier-B exception_flow line ("WHEN cond: raise ExcType(...)").
                                if _raises_types and _exc_flow_values:
                                    _flow_blob = " ".join(_exc_flow_values)
                                    _raises_types = [
                                        _rt for _rt in _raises_types if _rt not in _flow_blob
                                    ]
                                # LSP-enriched return type: nodes.return_type carries richer
                                # type info (e.g. "Optional[Box]", "Result<T, E>") stored by
                                # resolve.py's LSP hover pass or tree-sitter extraction.
                                # Prefer it over mined return_shape (e.g. "dict", "list")
                                # when it is longer/richer. Language-agnostic: render as-is.
                                _node_return_type = ""
                                try:
                                    _rt_conn = _open_graph_db(db_path)
                                    _rt_row = _rt_conn.execute(
                                        "SELECT return_type FROM nodes WHERE id = ?",
                                        (_bc_node_id,),
                                    ).fetchone()
                                    _rt_conn.close()
                                    if _rt_row and _rt_row["return_type"]:
                                        _node_return_type = str(_rt_row["return_type"]).strip()
                                except Exception:
                                    pass
                                _agg_lines: list[str] = []
                                if _node_return_type and _node_return_type not in ("None", ""):
                                    # LSP/tree-sitter return_type is the authoritative typed
                                    # return annotation; mined return_shape is a structural
                                    # approximation. When both exist, use the richer one.
                                    if _return_shapes:
                                        # Append mined shape only if it adds info not in
                                        # the typed return_type (e.g. "dict" vs "Dict[str, Any]").
                                        _shape_extras = [
                                            s for s in _return_shapes
                                            if s.lower() not in _node_return_type.lower()
                                        ]
                                        if _shape_extras:
                                            _agg_lines.append(f"  [RETURNS] {_node_return_type} (shape: {', '.join(_shape_extras)})")
                                        else:
                                            _agg_lines.append(f"  [RETURNS] {_node_return_type}")
                                    else:
                                        _agg_lines.append(f"  [RETURNS] {_node_return_type}")
                                elif _return_shapes:
                                    _agg_lines.append(f"  [RETURNS] {', '.join(_return_shapes)}")
                                if _raises_types:
                                    _agg_lines.append(f"  [RAISES] {', '.join(_raises_types)}")
                                for _agg_line in _agg_lines:
                                    _props_contract_lines.insert(0, _agg_line)
                                if _props_param_lines:
                                    # C1d: dedup params (first-occurrence order) so the
                                    # PARAMS line never repeats a param (the verified
                                    # ``PARAMS: lib [required] [required]`` ev47 defect).
                                    _seen_params: set[str] = set()
                                    _formatted_params: list[str] = []
                                    for _p in _props_param_lines:
                                        _fp = _format_param_display(_p)
                                        if _fp in _seen_params:
                                            continue
                                        _seen_params.add(_fp)
                                        _formatted_params.append(_fp)
                                    _props_contract_lines.insert(0, f"  PARAMS: {', '.join(_formatted_params)}")
                                _kind_counts: dict[str, int] = {}
                                for _p_item in _props:
                                    _kc_key = _p_item["kind"]
                                    _kind_counts[_kc_key] = _kind_counts.get(_kc_key, 0) + 1
                                print(f"[GT_META] properties_query: node_id={_bc_node_id} total={len(_props)} kinds={_kind_counts}", file=sys.stderr, flush=True)
                        except Exception as _props_exc:
                            print(f"[GT_META] behavioral_contract_properties_error: {_props_exc}", file=sys.stderr, flush=True)
                            _props_used = False

                    if _props_used and _props_contract_lines:
                        # C1c+C1d: drop empty-value lines, dedup, and reorder so
                        # guards/returns/raises precede params/resources BEFORE the
                        # char cap pops from the end — the cap now drops low-value
                        # content first, never the deciding guards/raises.
                        _props_contract_lines = _normalize_contract_lines(_props_contract_lines)
                        # Properties-based contract
                        _contract_block = "\n".join(_props_contract_lines)
                        if len(_contract_block) > 800:
                            while _props_contract_lines and len("\n".join(_props_contract_lines)) > 800:
                                _props_contract_lines.pop()
                        # C1c: suppress the header entirely when nothing survived
                        # (correct-or-quiet — never a [BEHAVIORAL CONTRACT] with no body).
                        if _props_contract_lines:
                            func_parts.append("[BEHAVIORAL CONTRACT]")
                            func_parts.extend(_props_contract_lines)
                    else:
                        # Regex fallback for non-Go-indexed repos or old databases
                        print(f"[GT_META] properties_fallback: using regex extraction (no properties in graph.db)", file=sys.stderr, flush=True)
                        from groundtruth.evidence.change import (
                            _regex_extract_guards,
                            _regex_extract_mutations,
                            _regex_extract_accumulations,
                            _classify_return_statements,
                        )
                        guards = _regex_extract_guards(func_body_for_contract)
                        mutations = _regex_extract_mutations(func_body_for_contract)
                        accumulations = _regex_extract_accumulations(func_body_for_contract)
                        classified_returns = _classify_return_statements(
                            func_body_for_contract, func_start or 1
                        )
                        _has_substance = (
                            len(guards) >= 1
                            or len(mutations) >= 1
                            or len(accumulations) >= 1
                            or len(classified_returns) >= 2
                        )
                        if _has_substance:
                            contract_lines: list[str] = []
                            if guards:
                                for gt_type, gt_cond in guards[:3]:
                                    gt_cond = clip_balanced(gt_cond) if gt_cond else gt_cond
                                    contract_lines.append(f"  PRESERVE: if {gt_cond} then {gt_type}")
                            if mutations:
                                _mut_targets = ", ".join(t for _, t in mutations[:4])
                                contract_lines.append(f"  MUTATES: {_mut_targets}")
                            if accumulations:
                                for _acc_type, _acc_var in accumulations[:3]:
                                    if _acc_type == "append_build":
                                        contract_lines.append(f"  ACCUMULATES: {_acc_var} via .append()")
                                    elif _acc_type == "increment":
                                        contract_lines.append(f"  ACCUMULATES: {_acc_var} via +=")
                                    elif _acc_type == "string_compose":
                                        contract_lines.append(f"  ACCUMULATES: {_acc_var} via string composition")
                            if classified_returns:
                                for rp_line, rp_kind, rp_text in classified_returns[:4]:
                                    if rp_kind == "VOID_SIDE_EFFECT":
                                        contract_lines.append("  VOID_SIDE_EFFECT")
                                    else:
                                        rp_text = clip_balanced(rp_text) if rp_text else rp_text
                                        contract_lines.append(f"  L{rp_line}: {rp_text}")
                            # C1c+C1d: dedup, drop empty-value lines, and reorder so
                            # guards/returns precede the rest BEFORE the char cap pops
                            # from the end (same normalization as the properties path).
                            contract_lines = _normalize_contract_lines(contract_lines)
                            # Budget enforcement: 200-800 chars
                            _contract_block = "\n".join(contract_lines)
                            if len(_contract_block) > 800:
                                while contract_lines and len("\n".join(contract_lines)) > 800:
                                    contract_lines.pop()
                            if contract_lines:
                                func_parts.append("[BEHAVIORAL CONTRACT]")
                                func_parts.extend(contract_lines)
                        else:
                            # B2: Fallback for void/short functions — emit full body as contract
                            # when no guards, mutations, accumulations, and <2 return paths.
                            _body_lines = func_body_for_contract.splitlines()
                            # Skip def line (first line) to get body-only lines
                            _body_only = _body_lines[1:] if len(_body_lines) > 1 else _body_lines
                            if _body_only and len(_body_only) <= 5:
                                func_parts.append(f"[BEHAVIORAL CONTRACT] (full body — {len(_body_only)} lines)")
                                for _bl in _body_only:
                                    func_parts.append(f"  {_bl.rstrip()}")
            except Exception as _bc_outer_exc:
                print(f"[GT_META] behavioral_contract_outer_error: {type(_bc_outer_exc).__name__}: {_bc_outer_exc}", file=sys.stderr, flush=True)

        # --- Priority 1: Caller CODE lines (verification: did you break dependents?) ---
        callers = _get_callers_from_graph(
            db_path, file_path, func_name, repo_root,
            seen_files=edited_files,
            limit=base_max + 10,
        )
        total_callers = len(callers)

        unseen_callers = [c for c in callers if c["unseen"] == "1"]
        seen_callers = [c for c in callers if c["unseen"] == "0"]
        ordered_callers = unseen_callers + seen_callers

        # Patch E: boost callers matching issue anchors to top
        _anchors = _load_issue_anchors()
        _anchor_syms = set(s.lower() for s in _anchors.get("symbols", []))
        _anchor_paths = set(p.lower() for p in _anchors.get("paths", []))
        if _anchor_syms or _anchor_paths:
            def _anchor_score(c: dict) -> int:
                fp = (c.get("file", "") or "").lower()
                cn = (c.get("caller_name", "") or "").lower()
                score = 0
                if any(s in cn for s in _anchor_syms):
                    score += 2
                if any(p in fp for p in _anchor_paths):
                    score += 2
                if any(s in fp for s in _anchor_syms):
                    score += 1
                return score
            _pre_order = [c.get("file", "") for c in ordered_callers[:3]]
            ordered_callers = sorted(ordered_callers, key=_anchor_score, reverse=True)
            _post_order = [c.get("file", "") for c in ordered_callers[:3]]
            if _pre_order != _post_order:
                print(
                    f"[GT_META] anchor_rerank: {func_name} before={_pre_order} after={_post_order}",
                    file=sys.stderr, flush=True,
                )

        # Determine aggregate confidence for this caller set
        # Use median confidence (CG Risk Detection 2025: density-weighted aggregate)
        caller_confidences = [
            float(c.get("confidence", "0.5")) for c in ordered_callers
        ]
        if caller_confidences:
            _sorted_conf = sorted(caller_confidences)
            _n = len(_sorted_conf)
            aggregate_confidence = (_sorted_conf[(_n - 1) // 2] + _sorted_conf[_n // 2]) / 2.0
        else:
            aggregate_confidence = 0.0

        # Confidence-gated risk-warning evidence framing
        risk_lines = format_risk_evidence(
            ordered_callers, func_name, aggregate_confidence,
        )
        if risk_lines:
            func_parts.extend(risk_lines)

        # L3+ Enhancement: callees of edited function (what does it call?)
        resolved_target_id = _resolve_node_id(db_path, file_path, func_name)
        if resolved_target_id and db_path and os.path.exists(db_path):
            try:
                _callees_conn = _open_graph_db(db_path)
                _resolved_callees_fp = _resolve_file_path(_callees_conn, file_path)
                # Layer 2.2: callee direction uses same categorical filter as
                # callers (twin query, was missed in first pass — verifier-found).
                _callee_filter = _edge_filter_for_db(db_path)
                _callees = _callees_conn.execute(
                    f"SELECT DISTINCT nt.file_path, nt.name, nt.signature "
                    f"FROM edges e "
                    f"JOIN nodes nt ON e.target_id = nt.id "
                    f"WHERE e.source_id = ? AND e.type = 'CALLS' "
                    f"AND {_callee_filter} "
                    f"AND nt.file_path != ? "
                    f"LIMIT 5",
                    (resolved_target_id, _resolved_callees_fp),
                ).fetchall()
                _callees_conn.close()
                if _callees:
                    # TASK #49: render each callee WITH its signature so the
                    # agent sees the contract it must satisfy at the call site
                    # (correct-or-quiet falls back to bare name when no sig).
                    _callee_text = "Calls into: " + ", ".join(
                        _format_callee_entry(
                            c["name"],
                            (c["signature"] if "signature" in c.keys() else "") or "",
                            c["file_path"],
                        )
                        for c in _callees[:3]
                    )
                    func_parts.append(_callee_text)
                    if _evidence_accumulator is not None:
                        for _ce in _callees[:3]:
                            _evidence_accumulator.append({
                                "kind": "l3_callee",
                                "file_path": _ce["file_path"],
                                "symbol": _ce["name"],
                                "source": "graph_db",
                                "reason": f"called by {func_name}",
                            })
            except Exception:
                pass

        # --- Priority 2: Signature + return type + arity mismatch detection ---
        sig = _get_signature_from_graph(db_path, file_path, func_name)
        if sig:
            sig_line = f"[SIGNATURE] {sig}"
            if callers and aggregate_confidence >= 0.9:
                if " -> " in sig:
                    ret_type = sig.split(" -> ")[-1].strip()
                    if ret_type and ret_type != "None":
                        sig_line += f" → {len(callers)} callers expect {ret_type} return"
                else:
                    sig_line += f" — {len(callers)} callers depend on this"
            func_parts.append(sig_line)

            # Diff-aware arity check: compare new sig vs caller call arity
            _arity_warning = _check_arity_mismatch(
                sig, func_name, ordered_callers, edited_files,
            )
            if _arity_warning:
                func_parts.append(_arity_warning)
                if _evidence_accumulator is not None:
                    _evidence_accumulator.append({
                        "kind": "l3_signature_mismatch",
                        "file_path": file_path, "symbol": func_name,
                        "text": _arity_warning, "source": "graph_db",
                    })
            # Structured capture: signature
            if _evidence_accumulator is not None:
                _evidence_accumulator.append({
                    "kind": "l3_signature", "file_path": file_path,
                    "symbol": func_name, "text": sig, "source": "graph_db",
                })

        # Structured capture: callers
        if _evidence_accumulator is not None and callers:
            for c in callers[:5]:
                _evidence_accumulator.append({
                    "kind": "l3_caller_code", "file_path": c["file"],
                    "symbol": c.get("caller_name", ""),
                    "line_start": int(c.get("line", 0) or 0),
                    "text": c.get("code", ""), "source": "graph_db",
                    "reason": "calls edited function",
                    })

        # === EVIDENCE PRIORITY ORDER ===
        # 1. Callers + Signature (already appended above)
        # 2. Interface peers — same method in implementing classes (highest value for multi-file)
        # 3. Test assertions — what tests expect
        # 4. Sibling pattern — same-class different method
        # 5. Structural twins, propagation, co-change, scope (supplementary)

        # --- Priority 2: Interface peers (same method, different implementing class) ---
        peers = []
        _skip_peer = func_name.startswith("__") and func_name.endswith("__")
        if not _skip_peer:
            peers = _get_interface_peers_from_graph(
                db_path, file_path, func_name, repo_root,
                edited_files=edited_files,
            )
            if peers:
                for peer in peers[:2]:
                    peer_base = os.path.basename(peer["file"])
                    edited_tag = " (your earlier edit)" if peer["edited"] else ""
                    if peer["snippet"]:
                        func_parts.append(
                            f"[PEER] {peer_base}::{func_name}(){edited_tag}:\n{clip_balanced(peer['snippet'], 300)}"
                        )
                    elif peer["signature"]:
                        func_parts.append(
                            f"[PEER] {peer_base}::{func_name}(){edited_tag}: {clip_balanced(peer['signature'], 120)}"
                        )
                if _evidence_accumulator is not None:
                    for peer in peers[:2]:
                        _evidence_accumulator.append({
                            "kind": "l3_interface_peer",
                            "file_path": peer["file"],
                            "symbol": func_name,
                            "text": clip_balanced(peer["snippet"], 200) or clip_balanced(peer["signature"], 120),
                            "source": "graph_db",
                        })

        # --- Priority 2c: Override chain (parent class methods) ---
        overrides = _get_override_chain(db_path, file_path, func_name)
        for ovr in overrides[:1]:
            sig_display = f" — {ovr['signature'][:80]}" if ovr["signature"] else ""
            func_parts.append(f"[OVERRIDE] {ovr['class']}.{ovr['method']}() at {ovr['file']}{sig_display}")

        # --- Priority 3: Test assertions ---
        assertions = _get_test_assertions_from_graph(db_path, file_path, func_name)
        if assertions:
            _KIND_OP = {
                "assertEqual": "==", "assertEquals": "==", "assertNotEqual": "!=",
                "assertTrue": "is true", "assertFalse": "is false",
                "assertIn": "in", "assertNotIn": "not in",
                "assertIs": "is", "assertIsNone": "is None",
                "assertRaises": "raises", "assert_raises": "raises",
                "assert_equal": "==", "assert_true": "is true",
            }
            for a in assertions[:2]:
                expr = clip_balanced(a["expression"], 100) if a["expression"] else ""
                expected = clip_balanced(a["expected"], 50) if a["expected"] else ""
                test_ref = f"{a['test_name']}" if a["test_name"] else "test"
                test_file_base = os.path.basename(a.get("test_file", "")) if a.get("test_file") else ""
                file_tag = f" ({test_file_base})" if test_file_base else ""
                kind = a.get("kind", "")
                op = _KIND_OP.get(kind, "==")
                if expr:
                    if kind in ("assertRaises", "assert_raises"):
                        func_parts.append(f"[TEST] {test_ref}{file_tag} expects {expr} to raise {expected}")
                    else:
                        func_parts.append(f"[TEST] {test_ref}{file_tag} expects: {expr} {op} {expected}")
        if not assertions:
            file_assertions = _get_test_assertions_from_file(
                db_path, file_path, func_name, repo_root
            )
            if file_assertions:
                for fa in file_assertions[:3]:
                    func_parts.append(f"[TEST] {fa}")
                assertions = [{"test_file": "", "test_name": "", "expression": fa} for fa in file_assertions]
        elif assertions:
            # Supplement: if graph assertions have 0 issue-keyword relevance, add file-grep
            _it = _load_issue_terms()
            if _it:
                _any_relevant = any(
                    sum(1 for t in _it if t in ((a.get("expression", "") + " " + a.get("test_name", "")).lower()))
                    for a in assertions
                )
                if not _any_relevant:
                    _supp = _get_test_assertions_from_file(db_path, file_path, func_name, repo_root)
                    for fa in _supp[:2]:
                        func_parts.append(f"[TEST] {fa}")

        if _evidence_accumulator is not None and assertions:
            for a in assertions[:2]:
                _evidence_accumulator.append({
                    "kind": "l3_test_assertion", "file_path": a.get("test_file", ""),
                    "symbol": a.get("test_name", ""), "text": a.get("expression", ""),
                    "source": "graph_db",
                })

        # --- Priority 3b: Test completeness signal ---
        if db_path and resolved_target_id and os.path.exists(db_path):
            try:
                _tc_conn = _open_graph_db(db_path)
                _tc_fp = _resolve_file_path(_tc_conn, file_path)
                _tc_tables = {r[0] for r in _tc_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "assertions" in _tc_tables:
                    _tc_groups = _tc_conn.execute(
                        """SELECT DISTINCT n.name FROM assertions a
                           JOIN nodes n ON a.test_node_id = n.id
                           JOIN nodes tgt ON a.target_node_id = tgt.id
                           WHERE tgt.file_path = ?
                           ORDER BY n.name LIMIT 20""",
                        (_tc_fp,),
                    ).fetchall()
                    if len(_tc_groups) > 1:
                        _tc_names = [r[0] for r in _tc_groups]
                        func_parts.append(
                            f"[COMPLETENESS] {len(_tc_names)} test groups target this file: "
                            f"{', '.join(_tc_names[:5])} — verify ALL pass"
                        )
                _tc_conn.close()
            except Exception:
                pass

        # --- Priority 4: Sibling pattern (same class, different method) ---
        # B1: Sibling output suppressed — useless in 13/15 Phase 6 tasks.
        # Still queried so G7 silence gate can check `not siblings`.
        siblings = _get_siblings_from_graph(db_path, file_path, func_name, repo_root)
        if siblings and (_anchor_syms or _anchor_paths):
            def _sib_anchor_score(s: dict) -> int:
                sn = s.get("name", "").lower()
                ss = (s.get("snippet", "") or s.get("signature", "")).lower()
                return sum(2 for sym in _anchor_syms if sym in sn or sym in ss)
            siblings.sort(key=_sib_anchor_score, reverse=True)
        # B1: sibling [PATTERN] output suppressed from agent evidence.
        # _get_siblings_from_graph() and sorting retained for G7 gate + accumulator.
        if _SIBLING_EVIDENCE_ENABLED:
            if siblings and len(siblings) >= 2:
                # Dynamic gate: show [PATTERN] only when sibling shares state
                # with the edited function (change-may-impact, CodePlan FSE 2024).
                # Run obligation_check to find siblings with shared self.attrs.
                _impact_siblings: set[str] = set()
                try:
                    from groundtruth.hooks.obligation_check import find_obligations
                    _obs = find_obligations(file_path, repo_root, {func_name})
                    for _o in _obs:
                        for sib in siblings:
                            if sib["name"] in _o:
                                _impact_siblings.add(sib["name"])
                except Exception:
                    pass

                # Fix 1 (5-lang parity): when obligation_check returns empty
                # (non-Python — ast.parse fails on Go/TS/JS/Rust), fall back to
                # graph.db properties table. The Go indexer extracts class_field
                # and field_read properties for ALL languages via tree-sitter.
                # If the edited function and a sibling share at least 1 property
                # of kind class_field or field_read, they share state.
                if not _impact_siblings and db_path and _bc_node_id:
                    try:
                        _prop_conn = _open_graph_db(db_path)
                        # Get class_field/field_read values for the edited function
                        _edited_props = {
                            r["value"]
                            for r in _prop_conn.execute(
                                "SELECT value FROM properties "
                                "WHERE node_id = ? AND kind IN ('class_field', 'field_read')",
                                (_bc_node_id,),
                            ).fetchall()
                        }
                        if _edited_props:
                            # Resolve sibling node IDs and check for shared properties
                            for sib in siblings:
                                _sib_id = _resolve_node_id(db_path, file_path, sib["name"])
                                if _sib_id is None:
                                    continue
                                _sib_props = {
                                    r["value"]
                                    for r in _prop_conn.execute(
                                        "SELECT value FROM properties "
                                        "WHERE node_id = ? AND kind IN ('class_field', 'field_read')",
                                        (_sib_id,),
                                    ).fetchall()
                                }
                                if _edited_props & _sib_props:
                                    _impact_siblings.add(sib["name"])
                        _prop_conn.close()
                    except Exception:
                        pass
                for sib in siblings[:2]:
                    if sib["name"] not in _impact_siblings:
                        continue
                    if sib["snippet"]:
                        func_parts.append(f"[PATTERN] sibling {sib['name']}() does:\n{clip_balanced(sib['snippet'], 300)}")
                    elif sib["signature"]:
                        func_parts.append(f"[PATTERN] sibling {sib['name']}(): {clip_balanced(sib['signature'], 120)}")
                    break

            if _evidence_accumulator is not None:
                for sib in siblings[:2]:
                    _evidence_accumulator.append({
                        "kind": "l3_sibling_pattern", "file_path": file_path,
                        "symbol": sib.get("name", ""),
                        "text": sib.get("snippet", "") or sib.get("signature", ""),
                        "source": "graph_db",
                    })

        # --- Same-name twin detection (P3) + Fingerprint similarity (P4) ---
        # TASK #50: a same-name definition in the same file/class is the
        # highest-precision consistency signal (a partial-fix trap). Render it
        # ABOVE the fuzzy fingerprint [SIMILAR] match.
        # TASK #47: gate the fuzzy [SIMILAR] signal on relevance — it is a
        # non-edge, fingerprint-derived guess and must overlap the issue or the
        # edited fn's tokens, else it injects noise (e.g. unrelated embed_album).
        if resolved_target_id and db_path:
            _twins = _find_same_name_twins(
                db_path, resolved_target_id, func_name, file_path
            )
            _twin_base = file_path.replace("\\", "/").rsplit("/", 1)[-1]
            for _twin_name, _twin_line in _twins[:2]:
                _loc = f"{_twin_base}:{_twin_line}" if _twin_line else _twin_base
                func_parts.append(
                    f"[TWIN] {_twin_name}() also defined at {_loc} — "
                    f"apply the fix here too"
                )

            _sim_issue_terms = _load_issue_terms()
            _sim_fn_tokens = _identifier_tokens(func_name)
            # Require a same-name twin OR a strong fingerprint match (>=3 shared
            # calls) that also passes the relevance gate. Two-shared-call fuzzy
            # matches are too weak to surface as standalone evidence.
            similar = _find_similar_functions(db_path, resolved_target_id, file_path)
            for sim_name, sim_file, shared_count in similar[:1]:
                if shared_count < 3 and sim_name != func_name:
                    continue
                sim_base = sim_file.rsplit("/", 1)[-1] if "/" in sim_file else sim_file
                _sim_line = (
                    f"[SIMILAR] {sim_name}() in {sim_base} shares {shared_count} calls"
                )
                _sim_relevant = (
                    sim_name == func_name
                    or _passes_relevance_gate(
                        sim_name + " " + sim_base, _sim_issue_terms, _sim_fn_tokens
                    )
                )
                if _sim_relevant:
                    func_parts.append(_sim_line)

        # G7 isolation gate (Layer 2.2 — CLAUDE.md:59 four-pillar always-fire).
        # When function has 0 callers, 0 siblings, 0 peers, the graph cannot
        # provide Callers (pillar 3) evidence. But Contract (pillar 1),
        # Consistency (pillar 2), Completeness (pillar 4) come from
        # nodes/properties/cochanges — they DO NOT depend on graph edges
        # and MUST fire always per the constitution.
        #
        # Old behavior: drop most evidence, keep only typed signatures.
        # New behavior: keep all four-pillar evidence (Contract, Consistency,
        # Completeness markers); only filter out caller-derived markers that
        # can't exist when there are no callers.
        #
        # Research: Anthropic "Writing Effective Tools" (2025) — filter is
        # categorical (drop caller-derived content), not numeric (drop by
        # confidence). Render verbatim what graph DOES know.
        if total_callers == 0 and not siblings and not peers:
            _kept = g7_filter_isolated(func_parts, sig)
            _suppressed = len(func_parts) - len(_kept)
            _kept_kinds = [
                p.lstrip().split(":")[0].split("]")[0] + ("]" if p.lstrip().startswith("[") else ":")
                for p in _kept[:5]
            ]
            print(
                f"[GT_META] g7_gate: func={func_name} input={len(func_parts)} "
                f"kept={len(_kept)} suppressed={_suppressed} "
                f"kept_types={_kept_kinds}",
                file=sys.stderr, flush=True,
            )
            func_parts = _kept

        # --- Priority 5 (supplementary): twins, propagation, co-change, scope ---
        # Gate raised from 7 to 10 so supplementary signals fire even when
        # primary signals (callers, signature, peers, tests, siblings) are rich.
        # The final cap at line ~1643 still trims to 10 items max.
        # B1: structural twins output suppressed alongside sibling output.
        if _SIBLING_EVIDENCE_ENABLED:  # structural twins
            if len(func_parts) < 10:
                try:
                    _tc = _open_graph_db(db_path)
                    _resolved_tw = _resolve_file_path(_tc, file_path)
                    _frow = _tc.execute(
                        "SELECT start_line, end_line FROM nodes WHERE file_path = ? AND name = ? AND label IN ('Function','Method') LIMIT 1",
                        (_resolved_tw, func_name),
                    ).fetchone()
                    _tc.close()
                    if _frow and _frow[0] and _frow[1]:
                        full_file = os.path.join(repo_root, file_path)
                        twin_line = _detect_structural_twins(full_file, _frow[0], _frow[1])
                        if twin_line:
                            func_parts.append(f"  {twin_line}")
                except Exception as e:
                    _append_gt_log("structural_twins_error", str(e))

        if len(func_parts) < 10:
            scope_signal = _compose_scope_signal(
                db_path, file_path, func_name, repo_root, edited_files,
            )
            if scope_signal:
                func_parts.append(f"  {scope_signal}")

        # --- Priority 6: Issue obligation check + mismatch + format contracts ---
        try:
            from groundtruth.evidence.issue_obligations import load_and_check
            obligation_warnings = load_and_check(diff_text or "")
            print(f"[GT_META] obligation_check: diff_len={len(diff_text or '')} warnings={len(obligation_warnings)} issue_exists={os.path.exists('/tmp/gt_issue.txt')}", file=sys.stderr, flush=True)
            for ow in obligation_warnings[:2]:
                func_parts.insert(0, ow)
        except Exception as _ob_exc:
            print(f"[GT_META] obligation_error: {type(_ob_exc).__name__}: {_ob_exc}", file=sys.stderr, flush=True)
        try:
            from groundtruth.evidence.mismatch import detect_stale_references
            mismatch_warnings = detect_stale_references(
                db_path, file_path, func_name, diff_text or "", repo_root,
            )
            for mw in mismatch_warnings[:2]:
                func_parts.insert(0, mw)
        except Exception as _mm_exc:
            msg = f"{type(_mm_exc).__name__}: {_mm_exc}"
            _append_gt_log("mismatch_error", msg)
            print(f"[GT_META] mismatch_error: {msg}", file=sys.stderr, flush=True)
        try:
            from groundtruth.evidence.format_contract import mine_return_shape
            fmt_lines = mine_return_shape(db_path, file_path, func_name, repo_root)
            func_parts.extend(fmt_lines[:2])
        except Exception as _fmt_exc:
            msg = f"{type(_fmt_exc).__name__}: {_fmt_exc}"
            _append_gt_log("format_contract_error", msg)
            print(f"[GT_META] format_contract_error: {msg}", file=sys.stderr, flush=True)

        # --- Issue-text grounding: re-rank by issue relevance ---
        try:
            from groundtruth.evidence.issue_grounding import (
                load_issue_anchors, score_evidence_line,
            )
            _anchors_g = load_issue_anchors()
            if _anchors_g and len(func_parts) > 2:
                scored = [(score_evidence_line(p, _anchors_g), i, p) for i, p in enumerate(func_parts)]
                scored.sort(key=lambda x: (-x[0], x[1]))
                func_parts = [p for _, _, p in scored]
        except Exception as _ground_exc:
            msg = f"{type(_ground_exc).__name__}: {_ground_exc}"
            _append_gt_log("issue_grounding_error", msg)
            print(f"[GT_META] issue_grounding_error: {msg}", file=sys.stderr, flush=True)

        # Budget-based evidence cap — char limit is the real constraint, not item count
        _remaining_budget = effective_max_chars - chars_used
        _accumulated = 0
        _budget_parts: list[str] = []
        for _bp in func_parts:
            _accumulated += len(_bp) + 1  # +1 for newline
            if _accumulated > _remaining_budget and _budget_parts:
                break
            _budget_parts.append(_bp)
        func_parts = _budget_parts

        # U-shaped attention reorder (Lost in the Middle NeurIPS 2024 + R6/R7):
        # FINAL pass — after all mutations (insert(0), extend, cap).
        # REVIEW/PRESERVE first (verification-first, R6 "Agents Don't Know When
        # to Stop", R7 CodeR verification stages), signature next (primacy),
        # tests last (recency). Everything else middle.
        _PRIMACY = ("PRESERVE:", "[REVIEW]", "[SIGNATURE]")
        _RECENCY = ("[TEST]", "[COMPLETENESS]")
        _u_pri = [p for p in func_parts if any(p.lstrip().startswith(pfx) for pfx in _PRIMACY)]
        _u_rec = [p for p in func_parts if any(p.lstrip().startswith(pfx) for pfx in _RECENCY)]
        _u_mid = [p for p in func_parts if p not in _u_pri and p not in _u_rec]
        func_parts = _u_pri + _u_mid + _u_rec

        # Accumulate
        if func_parts:
            block = "\n".join(func_parts)
            if chars_used + len(block) > effective_max_chars:
                remaining = effective_max_chars - chars_used
                if remaining > 50:
                    last_nl = block.rfind("\n", 0, remaining)
                    block = block[:last_nl] if last_nl > 0 else block[:remaining]
                    output_parts.append(block)
                break
            output_parts.append(block)
            chars_used += len(block) + 1  # +1 for separator newline

    if not output_parts:
        return ""

    # Targeted verification: always fire (not gated on GT_REBUILD_L3 — runs in-container)
    if chars_used < effective_max_chars - 80:
        verify_line = _get_targeted_verification_suggestion(db_path, file_path, function_names)
        if verify_line:
            output_parts.append(verify_line)
            if _evidence_accumulator is not None:
                _evidence_accumulator.append({
                    "kind": "l3_targeted_verification",
                    "text": verify_line, "source": "graph_db",
                    "reason": "targeted test for edited symbol",
                })

    # Change impact: PREPEND before existing evidence (TDAD 2026).
    # Shows what the edit impacts — verified callers only (>=0.9).
    # Agent sees: what breaks, which tests to run.
    if function_names and db_path:
        try:
            from groundtruth.graph.ego import change_impact
            for _fn in function_names[:1]:
                _impact = change_impact(db_path, _fn, file_path, max_depth=2, min_confidence=0.9)
                if _impact:
                    _imp_lines = ["Impact:"]
                    for _imp in _impact[:3]:
                        _tag = " [test]" if _imp["is_test"] else ""
                        _hop = "direct" if _imp["hop"] == 1 else f"{_imp['hop']}-hop"
                        _imp_lines.append(f"  {_hop}: {_imp['name']}() in {os.path.basename(_imp['file'])}:{_imp['line']}{_tag}")
                    # Test suggestion from impact
                    _test_impacts = [i for i in _impact if i["is_test"]]
                    if _test_impacts:
                        _test_cmd = _test_impacts[0]
                        _imp_lines.append(f"Verify: pytest {_test_cmd['file']}::{_test_cmd['name']}")
                    output_parts.insert(0, "\n".join(_imp_lines))
        except Exception:
            pass

    # Wrap in structured format
    norm_path = file_path.replace("\\", "/").lstrip("/")
    mode_attr = f' mode="{effective_mode}"' if rebuild_l3 and effective_mode != "post_edit" else ""
    header = f'<gt-evidence trigger="post_edit:{norm_path}"{mode_attr}>'
    footer = "</gt-evidence>"
    body = "\n".join(output_parts)

    # Final cap check using effective max
    full_output = f"{header}\n{body}\n{footer}"
    if len(full_output) > effective_max_chars + 100:
        body = body[: effective_max_chars - len(header) - len(footer) - 5]
        full_output = f"{header}\n{body}\n{footer}"

    return full_output


def _git_env() -> dict[str, str]:
    """Git environment that handles safe.directory in containers."""
    import copy

    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


def _detect_workspace_root(provided_root: str) -> str:
    """Detect the actual workspace root dynamically.

    1. Try git rev-parse --show-toplevel from the provided root.
    2. If that fails, scan /workspace/*/ for a .git directory.
    3. Fall back to the provided root.
    """
    # Step 1: try git rev-parse from the provided root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=provided_root,
            timeout=5,
            env=_git_env(),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return toplevel
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, NotADirectoryError):
        pass

    # Step 2: scan /workspace/*/ for a .git directory
    try:
        workspace_dirs = _glob.glob("/workspace/*/")
        for candidate in sorted(workspace_dirs):
            if os.path.isdir(os.path.join(candidate, ".git")):
                return candidate.rstrip("/")
    except OSError:
        pass

    # Step 3: fall back to the provided root
    return provided_root


def _is_view_operation() -> bool:
    """Return True if the current hook invocation is for a view-only operation.

    OpenHands sets TOOL_INPUT or OPENHANDS_TOOL_INPUT to a JSON payload
    containing the tool arguments. If the payload has {"command": "view"}
    we skip all processing — no diff was produced.
    """
    for env_var in ("TOOL_INPUT", "OPENHANDS_TOOL_INPUT"):
        raw = os.environ.get(env_var, "")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("command") == "view":
                return True
        except (json.JSONDecodeError, ValueError):
            pass
    return False


_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".py",
        ".go",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".cs",
        ".php",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".rb",
        ".ex",
        ".exs",
        ".lua",
        ".ml",
        ".groovy",
        ".gradle",
        ".mjs",
        ".cjs",
    }
)


def _get_modified_files(root: str) -> list[str]:
    """Get modified source files from git diff (all supported languages)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return [
            f.strip()
            for f in result.stdout.strip().split("\n")
            if f.strip() and os.path.splitext(f.strip())[1].lower() in _SUPPORTED_EXTENSIONS
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _git_diff_path(root: str, relpath: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", relpath],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _is_untracked(root: str, relpath: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relpath],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
            env=_git_env(),
        )
        return result.returncode != 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True


def _synthetic_diff_new_file(relpath: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join("+" + ln for ln in lines)
    return (
        f"diff --git a/{relpath} b/{relpath}\nnew file\n--- /dev/null\n+++ b/{relpath}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def _read_file(root: str, relpath: str) -> str:
    try:
        with open(os.path.join(root, relpath), "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _read_text_file(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _git_show_head_file(root: str, relpath: str) -> str:
    if not relpath:
        return ""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{relpath}"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _reconstruct_old_content_from_diff(diff_text: str, relpath: str) -> str:
    """Rebuild old-side content from unified diff hunks for one file."""
    if not diff_text:
        return ""
    target = relpath.strip().replace("\\", "/").lstrip("/")
    if not target:
        return ""
    lines = diff_text.splitlines()
    in_file = False
    in_hunk = False
    old_lines: list[str] = []
    for line in lines:
        if line.startswith("+++ b/"):
            file_path = line[6:].strip().replace("\\", "/").lstrip("/")
            in_file = file_path == target
            in_hunk = False
            continue
        if not in_file:
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("---") or line.startswith("diff --git"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            old_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
    return "\n".join(old_lines).strip()


def _extract_diff_added_lines(diff_text: str, relpath: str) -> list[str]:
    target = relpath.strip().replace("\\", "/").lstrip("/")
    lines = diff_text.splitlines()
    in_file = False
    in_hunk = False
    added: list[str] = []
    for line in lines:
        if line.startswith("+++ b/"):
            file_path = line[6:].strip().replace("\\", "/").lstrip("/")
            in_file = file_path == target
            in_hunk = False
            continue
        if not in_file:
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


def _count_top_level_args(arg_blob: str) -> int:
    blob = arg_blob.strip()
    if not blob:
        return 0
    depth = 0
    count = 1
    for ch in blob:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


class _SimpleFinding:
    def __init__(self, family: str, message: str, confidence: float) -> None:
        self.family = family
        self.message = message
        self.confidence = confidence


def _sibling_pattern_fallback(source: str, diff_text: str, relpath: str) -> list[_SimpleFinding]:
    """Detect constructor-pattern drift in data-heavy files."""
    if not source or not diff_text or not relpath:
        return []
    call_re = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\(([^()\n]*)\)")
    all_calls = call_re.findall(source)
    if not all_calls:
        return []

    freq: dict[str, int] = {}
    arg_hist: dict[str, list[int]] = {}
    for ctor, args in all_calls:
        freq[ctor] = freq.get(ctor, 0) + 1
        arg_hist.setdefault(ctor, []).append(_count_top_level_args(args))

    repeated_ctors = {k for k, v in freq.items() if v >= 5}
    if not repeated_ctors:
        return []

    mode_args: dict[str, int] = {}
    for ctor in repeated_ctors:
        counts: dict[int, int] = {}
        for arg_count in arg_hist.get(ctor, []):
            counts[arg_count] = counts.get(arg_count, 0) + 1
        mode_args[ctor] = max(counts, key=counts.get) if counts else 0

    findings: list[_SimpleFinding] = []
    for line in _extract_diff_added_lines(diff_text, relpath):
        match = call_re.search(line)
        if not match:
            continue
        ctor, args_blob = match.group(1), match.group(2)
        if ctor not in repeated_ctors:
            continue
        observed = _count_top_level_args(args_blob)
        expected = mode_args.get(ctor, observed)
        if observed != expected:
            findings.append(
                _SimpleFinding(
                    family="pattern",
                    message=(
                        f"{ctor} constructor shape mismatch in sibling pattern "
                        f"(expected {expected} args, got {observed})"
                    ),
                    confidence=0.72,
                )
            )
    return findings


def _merge_modified_with_explicit(
    root: str, modified: list[str], explicit: str
) -> tuple[list[str], str]:
    """Merge wrapper-provided file path into modified list + diff (handles new/untracked files)."""

    diff_text = _get_diff_text(root)
    exp = explicit.strip().replace("\\", "/").lstrip("/")
    if not exp:
        return modified, diff_text

    join_path = os.path.join(root, exp)
    merged = list(modified)
    if exp not in merged and os.path.isfile(join_path):
        merged = [exp] + [f for f in merged if f != exp]

    if not os.path.isfile(join_path):
        return merged, diff_text

    p_diff = _git_diff_path(root, exp)
    file_marker = f"+++ b/{exp}"
    if p_diff.strip():
        if not diff_text.strip() or file_marker not in diff_text:
            diff_text = p_diff if not diff_text.strip() else diff_text + "\n" + p_diff
    elif _is_untracked(root, exp):
        synth = _synthetic_diff_new_file(exp, _read_file(root, exp))
        if not diff_text.strip() or file_marker not in diff_text:
            diff_text = synth if not diff_text.strip() else diff_text + "\n" + synth

    return merged, diff_text


def _extract_changed_func_names(diff_text: str) -> dict[str, list[str]]:
    """Parse diff to find changed function names per file.

    Returns dict: filepath -> list of function names in changed line ranges.
    """

    # Parse diff for file + line ranges
    changes: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif (
            line.startswith("@@")
            and current_file
            and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS
        ):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                changes.setdefault(current_file, []).append((start, start + count - 1))

    # Map line ranges to function names
    result: dict[str, list[str]] = {}
    for fpath, ranges in changes.items():
        # We'd need to parse the CURRENT file to find functions at those lines
        # This is done by the caller who has the AST
        result[fpath] = []  # Populated later when we have the source

    return result


def _find_funcs_at_lines(
    source: str, line_ranges: list[tuple[int, int]], file_path: str = "", store=None
) -> list[str]:
    """Find function/method names that overlap with given line ranges.

    Uses graph.db node positions when available, falls back to Python AST.
    """
    # Path 1: graph.db (language-agnostic)
    if store and file_path:
        try:
            funcs = store.get_functions_in_file(file_path)
            if funcs:
                names = []
                for func in funcs:
                    fs, fe = func["start_line"], func["end_line"]
                    for ls, le in line_ranges:
                        if fs <= le and ls <= fe:
                            names.append(func["name"])
                            break
                if names:
                    return names
        except Exception as e:
            _append_gt_log("detect_changed_funcs_error", str(e))

    # Path 2: Python AST (for .py files)
    if file_path.endswith(".py") or not file_path:
        import ast as _ast

        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return []
        func_names = []
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_start = node.lineno
                func_end = getattr(node, "end_lineno", func_start + 50)
                for ls, le in line_ranges:
                    if func_start <= le and ls <= func_end:
                        func_names.append(node.name)
                        break
        return func_names

    # Path 3: Regex fallback for non-Python without graph.db
    func_names = []
    lines = source.splitlines()
    func_pattern = re.compile(
        r"\s*(?:(?:pub\s+)?(?:async\s+)?(?:def|func|function|fn|fun)\s+)(\w+)"
    )
    for ls, le in line_ranges:
        for i in range(max(0, ls - 10), min(len(lines), le + 5)):
            m = func_pattern.match(lines[i] if i < len(lines) else "")
            if m and m.group(1) not in func_names:
                func_names.append(m.group(1))
    return func_names


def _apply_abstention(findings: list, min_confidence: float | None = None) -> list:
    """Universal abstention across all evidence families (Dynamic/Agnostic)."""
    if min_confidence is None:
        # SweRank-style: reduce abstention floor to allow more signal in sparse repos.
        # Fallback to 0.40 instead of 0.55 to prevent the 'hard funnel' failure mode.
        min_confidence = float(os.environ.get("GT_MIN_CONFIDENCE", "0.40"))

    passed = []
    for f in findings:
        conf = getattr(f, "confidence", 0)
        if conf < min_confidence:
            continue
        # Skip private methods
        msg = getattr(f, "message", "")
        if msg.startswith("_") and not msg.startswith("__init__"):
            continue
        passed.append(f)
    return passed


def _format_evidence(item) -> str:
    """Format a single evidence item as a compact one-liner."""
    family = getattr(item, "family", "?")
    family_tag = f"GT_{str(family).upper()}"

    # CallerExpectation: "3 callers destructure return as (x, y)"
    if hasattr(item, "usage_type"):
        detail = getattr(item, "detail", "")
        return f"GT: {detail} [{family_tag}]"

    # TestExpectation: "test_serialize:42 asserts format X"
    if hasattr(item, "assertion_type"):
        test_func = getattr(item, "test_func", "test")
        line = getattr(item, "line", "?")
        assertion = getattr(item, "assertion_type", "")
        # test-assertion expected is a source-text VALUE — balance-aware clip so a
        # truncated literal/expr never reaches the agent.
        expected = clip_balanced(getattr(item, "expected", "") or "", 60)
        return f"GT: {test_func}:{line} {assertion} {expected} [{family_tag}]"

    # PatternEvidence, ChangeEvidence, StructuralEvidence: have "message"
    msg = getattr(item, "message", str(item))
    if len(msg) > 140:
        msg = msg[:137] + "..."
    return f"GT: {msg} [{family_tag}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook v4")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument(
        "--file",
        default="",
        help="Repo-relative path touched in this edit (fallback when git diff is empty)",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument("--diff", default="", help="Path to unified diff text")
    parser.add_argument("--old-content", default="", help="Path to previous file content")
    parser.add_argument("--mode", default="post_edit", choices=["post_edit", "post_failure", "late_repair"])
    parser.add_argument("--iteration-ratio", type=float, default=0.0)
    parser.add_argument("--structured-output", action="store_true")
    args = parser.parse_args()

    start = time.time()
    _append_gt_log("fire", f"root={args.root} file={args.file or '-'} db={args.db}")

    # Skip view operations immediately — no diff was produced
    if _is_view_operation():
        status = _status_line("skipped", "view_operation")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)
        return

    # Detect the actual workspace root (handles /testbed vs /workspace/django/ etc.)
    root = _detect_workspace_root(args.root)

    log_entry = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": root,
        "root_provided": args.root,
        "evidence": {},
    }

    modified_files = _get_modified_files(root)
    modified_files, diff_text = _merge_modified_with_explicit(root, modified_files, args.file)
    provided_diff_text = _read_text_file(args.diff)
    if provided_diff_text.strip():
        diff_text = provided_diff_text
        if args.file:
            explicit = args.file.strip().replace("\\", "/").lstrip("/")
            if explicit and explicit not in modified_files:
                modified_files = [explicit] + modified_files

    explicit_file = args.file.strip().replace("\\", "/").lstrip("/")
    old_content_source = "none"
    old_content_text = ""
    if args.old_content:
        old_content_text = _read_text_file(args.old_content)
        if old_content_text:
            old_content_source = "provided_old_content"
    if not old_content_text and explicit_file and diff_text:
        old_content_text = _reconstruct_old_content_from_diff(diff_text, explicit_file)
        if old_content_text:
            old_content_source = "reconstructed_from_diff"
    if not old_content_text and explicit_file:
        old_content_text = _git_show_head_file(root, explicit_file)
        if old_content_text:
            old_content_source = "git_show_head"
    log_entry["old_content_source"] = old_content_source
    if old_content_text:
        log_entry["old_content_bytes"] = len(old_content_text.encode("utf-8", errors="replace"))

    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        status = _status_line("no_evidence", "no_modified_files")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)
        return

    log_entry["files_changed"] = modified_files

    # Open GraphStore for language-agnostic evidence (v16+)
    graph_store = None
    try:
        from groundtruth.index.graph_store import GraphStore, is_graph_db

        if os.path.exists(args.db) and is_graph_db(args.db):
            graph_store = GraphStore(args.db)
            graph_store.initialize()
    except Exception as e:
        _append_gt_log("graph_store_init_error", str(e))
        graph_store = None

    # Parse diff for changed line ranges per file
    diff_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif (
            line.startswith("@@")
            and current_file
            and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS
        ):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                s = int(match.group(1))
                c = int(match.group(2)) if match.group(2) else 1
                diff_ranges.setdefault(current_file, []).append((s, s + c - 1))

    # Find changed function names per file
    changed_funcs: dict[str, list[str]] = {}
    for fpath, ranges in diff_ranges.items():
        source = _read_file(root, fpath)
        if source:
            changed_funcs[fpath] = _find_funcs_at_lines(
                source, ranges, file_path=fpath, store=graph_store
            )

    # === IMPROVED L3: graph.db-driven priority-ordered evidence ===
    # Decision 22 Fix 5: L3 decoupled from L1 — gate on graph connectivity,
    # not on whether the brief produced candidates. Files with high-confidence
    # edges (≥0.5) in the graph get improved evidence regardless of L1 state.
    improved_output = ""
    if os.path.exists(args.db):
        try:
            all_func_names: list[str] = []
            primary_file = explicit_file or (modified_files[0] if modified_files else "")
            if primary_file and primary_file in changed_funcs:
                all_func_names = changed_funcs[primary_file]
            elif changed_funcs:
                for _fp, _fns in changed_funcs.items():
                    if _fns:
                        all_func_names = _fns
                        primary_file = _fp
                        break

            _accum: list[dict] | None = [] if args.structured_output else None
            if all_func_names and primary_file:
                import sqlite3 as _sq_gate
                _has_edges = None
                try:
                    _gc = _sq_gate.connect(args.db)
                    _resolved_pf = _resolve_file_path(_gc, primary_file)
                    _has_edges = _gc.execute(
                        "SELECT 1 FROM edges e JOIN nodes n ON (e.target_id=n.id OR e.source_id=n.id) "
                        "WHERE n.file_path = ? LIMIT 1", (_resolved_pf,)
                    ).fetchone()
                    _gc.close()
                except Exception as e:
                    _append_gt_log("improved_l3_gate_error", str(e))
                if _has_edges or all_func_names:
                    improved_output = generate_improved_evidence(
                        file_path=primary_file,
                        function_names=all_func_names,
                        db_path=args.db,
                        repo_root=root,
                        mode=args.mode,
                        iteration_ratio=args.iteration_ratio,
                        diff_text=diff_text,
                        _evidence_accumulator=_accum,
                    )
                else:
                    _append_gt_log("improved_l3_skipped", f"no_edges:{primary_file}")
        except Exception as e:
            _append_gt_log("improved_evidence_error", str(e))
            improved_output = ""

    if improved_output:
        # Improved evidence succeeded -- emit it and skip legacy families
        log_entry["evidence_source"] = "improved_l3"
        log_entry["output"] = improved_output
        log_entry["output_lines"] = len(improved_output.splitlines())
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        print(improved_output)
        if args.structured_output and _accum:
            print("__GT_STRUCTURED__")
            print(json.dumps(_accum))
        status = _status_line("success", "improved_l3")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)
        return

    # === LEGACY FALLBACK: 5 evidence families ===
    all_findings = []

    # === EVIDENCE FAMILY 1: CHANGE (before/after AST diff) ===
    change_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.change import ChangeAnalyzer

        analyzer = ChangeAnalyzer(store=graph_store)
        change_items = analyzer.analyze(root, diff_text)
        change_signal["ran"] = True
        change_signal["items_found"] = len(change_items)
        all_findings.extend(change_items)
    except Exception as e:
        import traceback

        change_signal["error"] = str(e)
        change_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["change"] = change_signal

    # === EVIDENCE FAMILY 2: CONTRACT (caller usage + test assertions) ===
    contract_signal = {
        "ran": False,
        "callers_analyzed": 0,
        "tests_analyzed": 0,
        "items_found": 0,
        "after_abstention": 0,
    }
    try:
        from groundtruth.evidence.contract import CallerUsageMiner, TestAssertionMiner

        caller_miner = CallerUsageMiner(root, store=graph_store)
        test_miner = TestAssertionMiner(root, store=graph_store)

        caller_files: list[str] = []
        test_files: list[str] = []
        if graph_store:
            try:
                for fpath in modified_files:
                    result = graph_store.get_importers_of_file(fpath)
                    importers = getattr(result, "value", []) or []
                    if importers:
                        for imp in importers:
                            if "test" in imp.lower():
                                test_files.append(imp)
                            else:
                                caller_files.append(imp)
            except Exception as e:
                _append_gt_log("contract_importers_error", str(e))

        contract_signal["callers_analyzed"] = len(caller_files)
        contract_signal["tests_analyzed"] = len(test_files)

        # Mine caller expectations for each changed function
        for fpath, funcs in changed_funcs.items():
            caller_node_ids = []
            if graph_store:
                try:
                    symbols_result = graph_store.get_symbols_in_file(fpath)
                    if hasattr(symbols_result, "value") and symbols_result.value:
                        caller_node_ids = [s.id for s in symbols_result.value if s.name in funcs]
                except Exception as e:
                    _append_gt_log("graph_symbol_lookup_error", str(e))
                    caller_node_ids = []
            for func_name in funcs:
                caller_items = caller_miner.mine(
                    func_name,
                    caller_files,
                    caller_node_ids=caller_node_ids,
                )
                all_findings.extend(caller_items)

        # Mine test assertions (pass function names for targeted graph.db queries)
        for fpath in modified_files:
            funcs = changed_funcs.get(fpath, [])
            for func_name in funcs or [None]:
                test_items = test_miner.mine(fpath, test_files, symbol_name=func_name)
                all_findings.extend(test_items)

        contract_signal["ran"] = True
        contract_items_count = sum(
            1 for f in all_findings if getattr(f, "family", "") == "contract"
        )
        contract_signal["items_found"] = contract_items_count
        if contract_items_count == 0:
            pattern_fallback_count = 0
            for fpath in modified_files:
                source = _read_file(root, fpath)
                fallback_items = _sibling_pattern_fallback(source, diff_text, fpath)
                pattern_fallback_count += len(fallback_items)
                all_findings.extend(fallback_items)
            if pattern_fallback_count:
                contract_signal["pattern_fallback_items"] = pattern_fallback_count
    except Exception as e:
        import traceback

        contract_signal["error"] = str(e)
        contract_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["contract"] = contract_signal

    # === EVIDENCE FAMILY 3: PATTERN (sibling analysis) ===
    pattern_signal = {"ran": False, "siblings_found": 0, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.pattern import SiblingAnalyzer

        sibling_analyzer = SiblingAnalyzer(store=graph_store)

        for fpath, funcs in changed_funcs.items():
            source = _read_file(root, fpath)
            if not source:
                continue
            for func_name in funcs:
                pattern_items = sibling_analyzer.analyze(source, func_name, file_path=fpath)
                all_findings.extend(pattern_items)

        pattern_signal["ran"] = True
        pattern_signal["items_found"] = sum(
            1 for f in all_findings if getattr(f, "family", "") == "pattern"
        )
    except Exception as e:
        pattern_signal["error"] = str(e)
    log_entry["evidence"]["pattern"] = pattern_signal

    # === EVIDENCE FAMILY 4: STRUCTURAL (obligations + contradictions + conventions) ===
    structural_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.structural import (
            run_obligations,
            run_contradictions,
            run_conventions,
        )

        store = None
        graph = None
        try:
            from groundtruth.index.store import SymbolStore
            from groundtruth.index.graph import ImportGraph

            store = SymbolStore(args.db)
            store.initialize()
            graph = ImportGraph(store)
        except Exception as e:
            _append_gt_log("structural_signal_init_error", str(e))

        struct_items = []
        if store and graph and diff_text:
            struct_items.extend(run_obligations(store, graph, diff_text))
        if store:
            struct_items.extend(run_contradictions(store, root, modified_files))
        struct_items.extend(run_conventions(root, modified_files))

        structural_signal["ran"] = True
        structural_signal["items_found"] = len(struct_items)
        all_findings.extend(struct_items)
    except Exception as e:
        structural_signal["error"] = str(e)
    log_entry["evidence"]["structural"] = structural_signal

    # === EVIDENCE FAMILY 5: SEMANTIC (call-site voting + arg affinity + guard consistency) ===
    semantic_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.semantic.call_site_voting import CallSiteVoter
        from groundtruth.evidence.semantic.argument_affinity import ArgumentAffinityChecker
        from groundtruth.evidence.semantic.guard_consistency import GuardConsistencyChecker

        voter = CallSiteVoter()
        affinity = ArgumentAffinityChecker()
        guard = GuardConsistencyChecker()

        semantic_items = []
        remaining_time = max(2.0, 8.0 - (time.time() - start))

        if diff_text:
            semantic_items.extend(voter.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(affinity.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(guard.analyze(root, diff_text, time_budget=remaining_time / 3))

        semantic_signal["ran"] = True
        semantic_signal["items_found"] = len(semantic_items)
        all_findings.extend(semantic_items)
    except Exception as e:
        semantic_signal["error"] = str(e)
    log_entry["evidence"]["semantic"] = semantic_signal

    # === ABSTENTION ===
    passed = _apply_abstention(all_findings)

    # Update after_abstention counts per family
    for family_name in ("change", "contract", "pattern", "structural", "semantic"):
        count = sum(1 for f in passed if getattr(f, "family", "") == family_name)
        log_entry["evidence"].get(family_name, {})["after_abstention"] = count

    log_entry["abstention_summary"] = {
        "total_raw": len(all_findings),
        "total_emitted": len(passed),
        "total_suppressed": len(all_findings) - len(passed),
    }

    # === FORMAT OUTPUT ===
    output_lines = []
    if passed:
        # Sort by confidence descending, take top N
        passed.sort(key=lambda f: -getattr(f, "confidence", 0))
        for item in passed[: args.max_items]:
            output_lines.append(_format_evidence(item))

    output = "\n".join(output_lines)
    log_entry["output"] = output
    log_entry["output_lines"] = len(output_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)

    if output:
        print(output)
        status = _status_line("success", f"{len(output_lines)}_items")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)
    else:
        status = _status_line("no_evidence", "abstention_filtered")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        status = _status_line("error", f"{type(exc).__name__}:{exc}")
        print(status, file=sys.stderr, flush=True)
        _append_gt_log("status", status)
