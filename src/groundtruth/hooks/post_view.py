"""Post-view hook — structural coupling enrichment for file reads.

Called by OpenHands PostToolUse hook on file_editor view operations.
Composes: PatternRoleClassifier + shared-state coupling detection.
Outputs 0-5 compact structural notes to stdout.

Usage:
    python -m groundtruth.hooks.post_view --root=/testbed --db=/tmp/gt_index.db --file=<path>
"""

from __future__ import annotations

import argparse
import ast
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from groundtruth.hooks.logger import log_hook
from groundtruth.runtime.sanitizer import clip_balanced

_VENDOR_PATTERNS = ("static/", "vendor/", "node_modules/", "dist/", ".min.", "assets/")

# ---------------------------------------------------------------------------
# L3b token-cap ENFORCEMENT (was logged-but-exceeded; runs showed 5355 tokens)
# ---------------------------------------------------------------------------
# gt_gt.md §8: the prepend budget is MAX_BRIEF_TOKENS=600; L3b graph-navigation
# has its own per-band cap. Previously the cap was only LOGGED / opportunistically
# checked per line, so the full `out` list could blow far past budget. Below we
# actually TRIM the rendered lines to a total token budget, dropping the
# LOWEST-priority navigation lines first and PRESERVING the highest-confidence
# evidence (contract pillar, ego-graph, test assertions). Token estimate = chars//4.

_L3B_MAX_TOKENS = 600  # total L3b render budget (==MAX_BRIEF_TOKENS prepend rail)
_GT_LAYER_EVENTS_PV = os.environ.get(
    "GT_LAYER_EVENTS_PATH", "/tmp/gt_layer_events.jsonl"
)

# Priority of an L3b line: LOWER number = MORE important (kept longest). The
# trimmer drops from the highest-priority-number (lowest value) lines first —
# i.e. generic navigation goes before contracts/tests.
def _l3b_line_priority(line: str) -> int:
    s = (line or "").lstrip()
    # 0 — interface contract / signatures / return shape: the facts the agent
    #     must preserve; never drop while anything lower exists.
    if s.startswith(("[CONTRACT]", "[SIGNATURE]", "[RAISES]", "PRESERVE:", "MUST PRESERVE:")):
        return 0
    # 1 — test assertions: the behavioral spec the fix must satisfy.
    if s.startswith("[TEST]"):
        return 1
    # 2 — ego-graph / focus / progress markers (oriented, confident).
    if s.startswith(("[FOCUS:", "[Progress:")) or "ego" in s.lower()[:6]:
        return 2
    # 3 — direct caller edges (who depends on this).
    if s.startswith("Called by:"):
        return 3
    # 4 — callees / importers / exception navigation (supplementary).
    if s.startswith(("Calls into:", "Imported by:", "[CATCHES]", "[RAISES]")):
        return 4
    # 5 — everything else (specs, misc nav) trimmed first.
    return 5


def _estimate_l3b_tokens(lines: list[str]) -> int:
    """Token estimate for the rendered L3b block (chars//4, newline-joined)."""
    if not lines:
        return 0
    return len("\n".join(lines)) // 4 + 1


def _enforce_l3b_cap(
    lines: list[str], max_tokens: int = _L3B_MAX_TOKENS
) -> tuple[list[str], int, bool]:
    """Trim ``lines`` so the rendered block is WITHIN ``max_tokens``.

    Drops the lowest-priority lines (highest ``_l3b_line_priority`` value, then
    from the tail within a priority band) until the estimate fits, preserving the
    highest-confidence evidence. Returns ``(kept_lines, final_token_count,
    cap_enforced)`` where ``cap_enforced`` is True iff any line was dropped.
    Original line ORDER is preserved among the kept lines."""
    if _estimate_l3b_tokens(lines) <= max_tokens:
        return lines, _estimate_l3b_tokens(lines), False

    # Stable index + priority so we can drop worst-first while keeping order.
    indexed = list(enumerate(lines))
    # Drop order: highest priority-number first; ties broken by LATER position
    # (tail of a band trimmed before its head).
    drop_order = sorted(
        indexed, key=lambda it: (_l3b_line_priority(it[1]), it[0]), reverse=True
    )
    dropped: set[int] = set()
    for idx, _ln in drop_order:
        kept = [l for i, l in indexed if i not in dropped]
        if _estimate_l3b_tokens(kept) <= max_tokens:
            break
        dropped.add(idx)
        # Never drop the single most-important line (priority 0 at top) to nothing —
        # if only priority-0 lines remain and we are still over, stop (correct-or-
        # quiet: deliver the contract even if marginally over, never empty).
        _remaining = [l for i, l in indexed if i not in dropped]
        if _remaining and all(_l3b_line_priority(l) == 0 for l in _remaining):
            break
    kept = [l for i, l in indexed if i not in dropped]
    return kept, _estimate_l3b_tokens(kept), True


def _emit_l3b_cap_event(
    file_path: str, final_tokens: int, cap_enforced: bool, dropped: int
) -> None:
    """Side-channel: persist the L3b cap outcome to gt_layer_events JSONL so the
    metrics reader sees the FINAL token count + cap_enforced flag. Best-effort."""
    try:
        import json as _json
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "layer": "L3b",
            "hook": "post_view",
            "file_path": file_path,
            "l3b_final_tokens": int(final_tokens),
            "l3b_cap_enforced": bool(cap_enforced),
            "l3b_lines_dropped": int(dropped),
            "l3b_max_tokens": _L3B_MAX_TOKENS,
            "l3b_exceeded_cap": bool(final_tokens > _L3B_MAX_TOKENS),
        }
        with open(_GT_LAYER_EVENTS_PV, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec) + "\n")
    except Exception:
        pass


def _edge_filter(db_path: str, *, alias: str = "e") -> str:
    """Categorical edge filter shared with L3 (Layer 2.2/2.3).

    Reuses post_edit's _edge_filter_for_db so caller/callee queries gate on
    categorical signals (resolution_method / trust_tier / candidate_count)
    when the post-merge schema is present, and fall back to numeric
    confidence on older indexes. Single source of truth across L3 and L3b.
    """
    try:
        from groundtruth.hooks.post_edit import _edge_filter_for_db
        return _edge_filter_for_db(db_path, alias=alias)
    except Exception:
        return f"COALESCE({alias}.confidence, 0.5) >= 0.7"


def _contract_pillar(conn: sqlite3.Connection, needle: str, issue_terms: set[str] | None = None) -> list[str]:
    """Contract pillar (CLAUDE.md:80-86) — signature + return type per function.

    ALWAYS-FIRE: this evidence comes from the `nodes` table (signature,
    return_type), which needs NO graph edges. Per CLAUDE.md:86, never gate
    always-available context behind a connectivity check. Renders on EVERY
    view regardless of caller count.

    When issue_terms supplied, prioritizes functions whose names overlap the
    issue (the agent is most likely about to touch those). Caps at 3 lines
    to stay compact (Anthropic context-engineering: high signal density).

    Returns a list of "[CONTRACT] name(sig) -> ret" lines (verbatim, no
    confidence labels — the data is structurally certain from the parser).
    """
    # Issue ANCHORS (the symbols the issue NAMES) are the decisive signal for which
    # function in this file the agent cares about — SWERank ICLR 2025: issue-named
    # entities localize the edit target. Computed BEFORE the fetch (was after) so an
    # anchored function defined DEEP in a large file still makes the candidate set.
    # THE BUG THIS FIXES (proven on a live beets-5495 trajectory): importer.py::
    # set_fields is the 39th of 102 functions, so the old `ORDER BY start_line LIMIT
    # 30` cut it BEFORE ranking — the pillar then showed generic top-of-file methods
    # (progress_write/_setup_logging) instead of the issue function. Generalizes to
    # any file where the issue function is defined past the first 30. Drop generic/
    # dunder anchors (__init__, ...) — extraction noise that matches a file's generic
    # methods (matplotlib-27613).
    def _generic_anchor(_n: str) -> bool:
        _s = (_n or "").strip()
        return _s.startswith("__") and _s.endswith("__")
    _anch_data = _load_issue_anchors()
    _anchor_syms = {
        s.lower() for s in _anch_data.get("symbols", [])
        if not _generic_anchor(s)
    }
    # PROVENANCE tiers (research-backed, 3-tier):
    #   300 = code_symbols (backtick-wrapped — reporter explicitly marked as code)
    #         Reformulate, Retrieve, Localize arXiv:2512.07022 2025
    #   200 = title_symbols (issue title/heading — BugLocator ICSE 2012)
    #   100 = body anchors (everywhere else — weakest tier)
    # Prose-only common words (check/set/get/run) already REMOVED from symbols
    # by extract_issue_anchors, so they never seed the graph localizer.
    _code_syms = {
        s.lower() for s in _anch_data.get("code_symbols", [])
        if not _generic_anchor(s)
    }
    _title_syms = {
        s.lower() for s in _anch_data.get("title_symbols", [])
        if not _generic_anchor(s)
    }

    # Anchor-matched functions sort to the FRONT of the fetch (CASE ... THEN 0) so
    # they ALWAYS survive the LIMIT regardless of definition position; the rest fill
    # by definition order. _relevance below then ranks the anchor to the top. When
    # there are no anchors the fetch is byte-identical to the old definition-order
    # query (suppression gate unchanged on no-signal tasks).
    try:
        if _anchor_syms:
            _ph = ",".join("?" * len(_anchor_syms))
            rows = conn.execute(
                "SELECT name, signature, return_type FROM nodes "
                "WHERE file_path = ? AND label IN ('Function','Method') AND is_test = 0 "
                "AND signature IS NOT NULL AND signature != '' "
                f"ORDER BY CASE WHEN LOWER(name) IN ({_ph}) THEN 0 ELSE 1 END, start_line "
                "LIMIT 30",
                (needle, *sorted(_anchor_syms)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, signature, return_type FROM nodes "
                "WHERE file_path = ? AND label IN ('Function','Method') AND is_test = 0 "
                "AND signature IS NOT NULL AND signature != '' "
                "ORDER BY start_line LIMIT 30",
                (needle,),
            ).fetchall()
    except sqlite3.Error:
        return []
    if not rows:
        return []

    def _relevance(r) -> int:
        name = (r[0] or "").lower()
        # 3-tier provenance: code (backtick) > title > body anchor.
        if name in _code_syms:
            score = 300
        elif name in _title_syms:
            score = 200
        elif name in _anchor_syms:
            score = 100
        else:
            score = 0
        if issue_terms:
            parts = set(name.replace("__", "_").split("_"))
            score += len(parts & issue_terms)
        return score

    ranked = sorted(rows, key=_relevance, reverse=True)

    # Correct-or-quiet (.claude/CLAUDE.md Pillar 3; SWE-PRM NeurIPS 2025:
    # prescriptive/front-loaded WRONG context lowers resolution). When we KNOW the
    # functions the issue names (anchors present) and NONE of them is in this file
    # — and no term overlaps either — the file's first-3 signatures are NOT "the
    # contract the agent needs"; they are misranked, authoritative-looking noise
    # pinned at salience position 0 (Lost-in-the-Middle NeurIPS 2024). Suppress
    # rather than guess. On a weak-signal task (no anchors at all) we cannot judge,
    # so we keep the always-fire behavior (CLAUDE.md:86) and show definition order.
    # FIX (relevance bug, live beets-5495 trajectory): the old guard `_anchor_syms
    # and …` only suppressed when anchors were non-empty, so an EMPTY anchor set fell
    # through to the "always-fire first-3" path and delivered generic file-top
    # functions (progress_write/_setup_logging/set_config) instead of the issue's
    # function (set_fields). Suppress whenever there is ANY relevance signal (anchors
    # OR issue terms) and the top function matches none of it — correct-or-quiet,
    # never generic noise at salience position 0. Only a TRULY blind task (no anchors
    # AND no terms) keeps the always-fire definition-order fallback.
    if (_anchor_syms or issue_terms) and _relevance(ranked[0]) == 0:
        return []

    # Dedup identical rendered signatures: a same-name overload (two format(self,
    # value) methods) must not consume two of the three slots with byte-identical
    # text (audit: duplicate-contract-lines).
    lines: list[str] = []
    _seen: set[str] = set()
    _delivered_fns: list[str] = []
    for name, sig, ret in ranked:
        sig_text = sig if sig else f"{name}(...)"
        if ret and ret not in ("None", "") and "->" not in sig_text:
            line = f"[CONTRACT] {sig_text} -> {ret}"
        else:
            line = f"[CONTRACT] {sig_text}"
        if line in _seen:
            continue
        _seen.add(line)
        lines.append(line)
        _delivered_fns.append(name)
        if len(lines) >= 3:
            break

    # def-use flow (data_flow base) for EACH shown contract function that has
    # parameter-flows — it rides with the signature it describes. Previously gated to
    # ranked[0] ONLY, which silently dropped data_flow whenever the single top
    # function had no params (e.g. paths(self)) even though a shown sibling like
    # album(self, paths, dirs) carried flows (proven on beets-5495, 2026-06-04). Still
    # bounded by the shown set (≤3) + deduped, so no force-feed; relevance>0 keeps a
    # blind/no-anchor view signature-only.
    # No extra relevance gate: the signatures above already cleared the suppression
    # gate (line ~156). flows rides with the signature it describes — gating it
    # separately on _relevance(ranked[0]) dropped it live whenever the hook passed
    # different issue_terms than the standalone path (beets-5495, flows=0 despite the
    # fix loaded). If a signature is shown, its param-flows ship with it.
    if _delivered_fns:
        _seen_flow: set[str] = set()
        for _fn in _delivered_fns:
            try:
                _fr = conn.execute(
                    "SELECT p.value FROM properties p JOIN nodes n ON p.node_id = n.id "
                    "WHERE n.file_path = ? AND n.name = ? AND p.kind = 'data_flow' "
                    "AND COALESCE(p.confidence, 1.0) >= 0.5 AND p.value LIKE '% -> %' "
                    "ORDER BY p.line LIMIT 1",
                    (needle, _fn),
                ).fetchone()
            except sqlite3.Error:
                continue
            if _fr and _fr[0] and _fr[0] not in _seen_flow:
                _seen_flow.add(_fr[0])
                lines.append(f"[CONTRACT] flows: {_fr[0]}")
    return lines


# NOTE: read-time Consistency/Completeness pillars were considered and
# REJECTED (research-backed, 2026-05-28). CodePlan FSE 2024: co-change as
# passive read context = the baseline that scored 0/7 (the win needed active
# edit-time propagation). FitRepair ASE 2023: twins help at fix-CONSTRUCTION
# (edit), proven at generation time, not orientation. Lost-in-Middle TACL
# 2024 + Context-Rot (Chroma 2025): front-loading context whose relevance
# isn't yet known harms. So Consistency stays at L3 post-edit (edit phase)
# and Completeness stays at L3 post-edit (completeness check phase). Each
# context pillar fires at the phase it serves; the stronger graph makes the
# content at each existing phase more correct, not relocated to read.


def _is_vendor_path(fp: str) -> bool:
    """Return True if file path looks like vendored/static/minified code."""
    norm = fp.replace("\\", "/")
    # Check if any pattern appears as a path segment (preceded by / or at start)
    for p in _VENDOR_PATTERNS:
        if p == ".min.":
            if ".min." in norm:
                return True
        elif f"/{p}" in norm or norm.startswith(p):
            return True
    return False

# Layer 2 (Agent-State Tracker) — FINAL_ARCH_V2 §3. Imported lazily inside
# functions where the in-process AgentState is passed; otherwise the loaders
# below fall back to the legacy /tmp files (subprocess compatibility).
from groundtruth.state.agent_state import (
    LEGACY_BRIEF_CANDIDATES_PATH,
    LEGACY_ISSUE_TERMS_PATH,
    LEGACY_VIEWED_PATH,
)

_GT_LOG = os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")


def _append_gt_log(event: str, detail: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}\tpost_view\t{event}"
    if detail:
        line += f"\t{detail}"
    try:
        with open(_GT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _status_line(kind: str, detail: str) -> str:
    return f"[GT_STATUS] {kind}:{detail}"


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
    ambiguous (correct-or-quiet — never echoes a path-shaped string back). All
    call sites bind the result into a SQL ``WHERE file_path = ?`` clause where a
    ``None`` bind yields 0 rows, so the consuming evidence block stays silent.
    """
    from groundtruth.index.path_resolver import resolve_to_stored_path

    db_path = _db_path_from_conn(conn)
    if not db_path:
        return None
    return resolve_to_stored_path(query_path, db_path)


def _same_stored_file(a: str, b: str) -> bool:
    """True when two stored-path strings refer to the SAME source file.

    Used by the C2 symbol-homonym guard: a bare function name may resolve to a
    node whose ``file_path`` differs from the viewed file. We normalize
    separators and leading ``./`` / ``/`` then require either exact equality or
    that one path is a COMPONENT-ALIGNED suffix of the other (so ``a/importer.py``
    matches ``importer.py`` but never ``b/importer.py`` or ``zero.py``).
    Generalized — pure path identity, no hardcoded names. Empty / None inputs
    are never "same" (correct-or-quiet: unknown → suppress)."""
    if not a or not b:
        return False
    pa = a.replace("\\", "/").lstrip("./").lstrip("/")
    pb = b.replace("\\", "/").lstrip("./").lstrip("/")
    if pa == pb:
        return True
    parts_a = pa.split("/")
    parts_b = pb.split("/")
    n = min(len(parts_a), len(parts_b))
    if n == 0:
        return False
    # Component-aligned suffix match: every shared trailing component is equal.
    return parts_a[-n:] == parts_b[-n:]


def _read_file(root: str, relpath: str) -> str:
    try:
        path = relpath if os.path.isabs(relpath) else os.path.join(root, relpath)
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_test_file(filepath: str) -> bool:
    fp = "/" + filepath.lower().replace("\\", "/")
    base = os.path.basename(fp)
    if base.startswith("test_"):
        return True
    return any(p in fp for p in ["/tests/", "/test/", "/testing/", "/fixtures/"])


def _classify_role(method_name: str, method_node: ast.FunctionDef) -> str:
    """Classify a method's role based on AST patterns."""
    if method_name == "__init__":
        return "stores"
    # Check for Store context on self.attrs
    written = set()
    for child in ast.walk(method_node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
            and isinstance(child.ctx, ast.Store)
        ):
            written.add(child.attr)
    if len(written) >= 2:
        return "stores"

    serialize_names = ("deconstruct", "serialize", "to_dict", "as_dict", "get_params")
    if any(s in method_name.lower() for s in serialize_names):
        return "serializes"

    if method_name in ("__eq__", "__ne__", "__hash__", "__lt__", "__le__", "__gt__", "__ge__"):
        return "compares"

    validate_names = ("validate", "check", "clean", "verify")
    if any(s in method_name.lower() for s in validate_names):
        return "validates"

    for child in ast.walk(method_node):
        if isinstance(child, ast.Raise):
            return "validates"

    return "reads"


def _get_role_label(role: str) -> str:
    return {
        "stores": "stores",
        "serializes": "serializes to kwargs",
        "compares": "compares",
        "validates": "checks",
        "reads": "reads",
    }.get(role, role)


def _load_issue_terms(state: object | None = None) -> set[str]:
    """Issue keywords for issue-aware navigation.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_issue_terms.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        terms = getattr(state, "issue_terms", None)
        if terms:
            return set(terms)
    try:
        text = open(LEGACY_ISSUE_TERMS_PATH, encoding="utf-8").read()
        return set(text.strip().split("\n")) if text.strip() else set()
    except OSError:
        return set()


def _load_issue_anchors() -> dict:
    """Load issue anchors (symbols, paths, test_names) written by wrapper."""
    try:
        import json as _json
        raw = open("/tmp/gt_issue_anchors.json", encoding="utf-8").read().strip()
        if not raw:
            return {"symbols": [], "paths": [], "test_names": []}
        return _json.loads(raw)
    except (OSError, ValueError):
        return {"symbols": [], "paths": [], "test_names": []}


def _score_by_issue_relevance(
    files: list[tuple[str, int]], root: str, issue_terms: set[str],
) -> list[tuple[str, int, int]]:
    """Re-rank neighbor files by issue terms + anchor symbol/path matches."""
    _anchors = _load_issue_anchors()
    _anchor_syms = set(s.lower() for s in _anchors.get("symbols", []))
    _anchor_paths = set(p.lower() for p in _anchors.get("paths", []))

    if not issue_terms and not _anchor_syms and not _anchor_paths:
        return [(f, cnt, 0) for f, cnt in files]
    scored = []
    for fp, cnt in files:
        fp_lower = fp.lower()
        # Anchor symbol match against path components (strong signal)
        anchor_hits = sum(2 for s in _anchor_syms if s in fp_lower)
        anchor_hits += sum(2 for p in _anchor_paths if p in fp_lower)
        # Issue term match against file content (existing behavior)
        term_hits = 0
        if issue_terms:
            try:
                text = open(os.path.join(root, fp), encoding="utf-8", errors="ignore").read(200_000).lower()
                term_hits = sum(1 for t in issue_terms if t in text)
            except OSError:
                pass
        scored.append((fp, cnt, anchor_hits + term_hits))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


def _load_visited_files(state: object | None = None) -> set[str]:
    """Already-viewed file paths.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_viewed.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        visited = getattr(state, "visited_files_set", None)
        if callable(visited):
            try:
                got = visited()
                if got:
                    return set(got)
            except Exception:
                pass
    try:
        text = open(LEGACY_VIEWED_PATH, encoding="utf-8").read()
        return {ln.strip() for ln in text.strip().split("\n") if ln.strip()}
    except OSError:
        return set()


def _load_brief_candidates(state: object | None = None) -> set[str]:
    """Brief candidate file paths.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_brief_candidates.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        cands = getattr(state, "brief_candidates", None)
        if cands:
            try:
                return {str(c) for c in cands}
            except TypeError:
                pass
    try:
        text = open(LEGACY_BRIEF_CANDIDATES_PATH, encoding="utf-8").read()
        return {ln.strip() for ln in text.strip().split("\n") if ln.strip()}
    except OSError:
        return set()


def _classify_layer_inline(file_path: str) -> str:
    """Classify a file into an architectural layer based on path components."""
    parts = file_path.lower().replace("\\", "/").split("/")
    for layer, keywords in [
        ("controller", ["controller", "handler", "endpoint", "view", "route", "api"]),
        ("service", ["service", "usecase", "manager"]),
        ("model", ["model", "entity", "schema", "domain"]),
        ("test", ["test", "spec", "fixture"]),
        ("util", ["util", "helper", "common", "lib"]),
    ]:
        if any(kw in part for part in parts for kw in keywords):
            return layer
    return ""


def _in_degree_for_file(cur: "sqlite3.Cursor", file_path: str) -> int:
    """Get total incoming edge count for a file (used for hub penalty)."""
    try:
        row = cur.execute(
            """
            SELECT COUNT(*) FROM edges e
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nt.file_path = ?
              AND e.type = 'CALLS'
            """,
            (file_path,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _top_functions_for_file(
    cur: "sqlite3.Cursor", file_path: str, limit: int = 2,
    edge_filter: str = "COALESCE(e.confidence, 0.5) >= 0.7",
) -> list[tuple[str, int]]:
    """Get top functions in a file by reference count, boosted by anchor match.

    edge_filter: categorical filter clause (shared with caller/callee queries)
    so ref counts shown to the agent use the same edge-selection semantics.
    """
    try:
        rows = cur.execute(
            f"""
            SELECT n.name, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id
              AND {edge_filter}
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY ref_count DESC, n.name
            LIMIT ?
            """,
            (file_path, limit * 3),
        ).fetchall()
        funcs = [(row[0], row[1]) for row in rows]
        _anchors = _load_issue_anchors()
        _syms = set(s.lower() for s in _anchors.get("symbols", []))
        if _syms:
            def _boost(item: tuple) -> tuple:
                name, cnt = item
                boost = 1000 if name.lower() in _syms else 0
                return (boost + cnt, name)
            funcs.sort(key=_boost, reverse=True)
        return funcs[:limit]
    except Exception:
        return []


def graph_navigation(
    relpath: str, db_path: str, *, limit: int = 5, iteration_ratio: float = 0.0,
    _evidence_accumulator: list[dict] | None = None,
    state: object | None = None,
) -> tuple[list[str], int]:
    """Graph.db navigation context — callers, callees, importers.

    Issue-aware: ranks neighbors by relevance to the current issue so the
    agent sees connections that matter, not just high-edge-count hubs.

    Optimizations:
    1. Confidence filter (>= 0.5) on edge queries
    2. Suppress already-visited files
    3. Brief candidate annotation [CANDIDATE]
    4. Hub-penalized ranking: score = cnt * (1 - min(1, in_degree/50))
    5. Symbol-level hints: file::func1,func2 (Nx)
    """
    if not os.path.isfile(db_path):
        return [], 0
    needle = relpath.replace("\\", "/").lstrip("./")
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            return [], 0

    # Resolve needle to stored path in graph.db. Guard against corrupt db:
    # a failure here must not crash the hook — return gracefully so the
    # Contract pillar block can still attempt to fire on a readable nodes table.
    try:
        needle = _resolve_file_path(conn, needle)
    except sqlite3.Error as _rfp_exc:
        print(f"[GT_META] graph_navigation_resolve_error: {_rfp_exc}", file=sys.stderr, flush=True)
        conn.close()
        return [], 0

    # Improvement 2: Load already-visited files for suppression
    visited_files = _load_visited_files(state)
    # Improvement 3: Load brief candidates for annotation
    brief_candidates = _load_brief_candidates(state)
    # Layer 2: record this view in AgentState if one was supplied
    if state is not None:
        record_view = getattr(state, "record_view", None)
        if callable(record_view):
            try:
                record_view(needle)
            except Exception:
                pass

    # Feature-flagged iteration-aware decay using telemetry constants
    rebuild_l3b = os.environ.get("GT_REBUILD_L3B", "0") == "1"
    _edge_limit_before = limit
    _decay_applied = False
    _iteration_band = "early_0_25"
    if rebuild_l3b:
        try:
            from groundtruth.telemetry.constants import L3B_EDGE_LIMITS, BAND_EARLY, BAND_MID, BAND_LATE, BAND_FINAL
            from groundtruth.telemetry.schemas import get_iteration_band
            _iteration_band = get_iteration_band(int(iteration_ratio * 100), 100)
            _configured_limit = L3B_EDGE_LIMITS.get(_iteration_band, limit)
            if _configured_limit < limit:
                limit = _configured_limit
                _decay_applied = True
        except ImportError:
            if iteration_ratio >= 0.85:
                limit = 1
                _decay_applied = True
            elif iteration_ratio >= 0.60:
                limit = max(1, limit // 2)
                _decay_applied = True

    # Progress tracking
    total_candidates = int(os.environ.get("GT_L3B_TOTAL_CANDIDATES", "0"))

    out: list[str] = []
    total_callers = 0
    try:
        cur = conn.cursor()

        # Big-repo BFS cap: reduce candidate limit for large graphs
        _node_count = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        if _node_count > 5000:
            limit = min(limit, 3)

        # Layer 2.3: categorical edge filter (shared with L3). Gates on
        # resolution_method / trust_tier / candidate_count when post-merge
        # schema present; numeric fallback otherwise.
        _ef = _edge_filter(db_path)

        # Callers: files that call functions in this file
        cur.execute(
            f"""
            SELECT DISTINCT nsrc.file_path, COUNT(*) as cnt
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND {_ef}
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ?
              AND nsrc.file_path != ?
              AND nsrc.is_test = 0
            GROUP BY nsrc.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (needle, needle, limit * 4),  # fetch more for filtering; SWAP-INVARIANT: no test callers
        )
        callers = [(row[0], row[1]) for row in cur.fetchall()]
        # Get one representative source_line per caller file for code snippet
        _caller_source_lines: dict[str, int] = {}
        for caller_fp, _ in callers[:10]:
            row = cur.execute(
                f"""SELECT e.source_line FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                  AND {_ef}
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path = ? AND nsrc.file_path = ? AND e.source_line > 0
                ORDER BY e.confidence DESC LIMIT 1""",
                (needle, caller_fp),
            ).fetchone()
            if row:
                _caller_source_lines[caller_fp] = row[0]
        total_callers = len(callers)

        # Callees: files this file calls into
        cur.execute(
            f"""
            SELECT DISTINCT nt.file_path, COUNT(*) as cnt
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
              AND {_ef}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
            GROUP BY nt.file_path
            ORDER BY cnt DESC
            LIMIT 40
            """,
            (needle, needle),
        )
        callees = cur.fetchall()

        # Improvement 2: Suppress already-visited files
        if visited_files:
            callers = [(fp, cnt) for fp, cnt in callers if fp not in visited_files]
            callees = [(fp, cnt) for fp, cnt in callees if fp not in visited_files]

        # Filter vendor/static JS (PRIOR-005)
        callers = [(fp, cnt) for fp, cnt in callers if not _is_vendor_path(fp)]
        callees = [(fp, cnt) for fp, cnt in callees if not _is_vendor_path(fp)]

        # Re-rank both by issue relevance
        issue_terms = _load_issue_terms(state)
        root = os.environ.get("GT_REPO_ROOT", "/testbed")
        if issue_terms:
            ranked_callers = _score_by_issue_relevance(callers, root, issue_terms)
            ranked_callees = _score_by_issue_relevance(callees, root, issue_terms)
            top_callers = [(f, cnt) for f, cnt, _ in ranked_callers[:limit * 2]]
            top_callees = [(f, cnt) for f, cnt, _ in ranked_callees[:limit * 2]]
        else:
            top_callers = callers[:limit * 2]
            top_callees = callees[:limit * 2]

        # Improvement 4: Hub-penalized ranking (repo-relative hub scale)
        # Compute p90 in-degree once for this graph instead of hardcoded 50
        # Only count CALLS edges — EXTENDS/IMPLEMENTS are architectural, not hub indicators
        all_degrees = [r[0] for r in cur.execute(
            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' AND COALESCE(e.confidence, 0.5) >= 0.7 GROUP BY n.file_path ORDER BY 1"
        ).fetchall()]
        hub_scale = all_degrees[int(len(all_degrees) * 0.9)] if all_degrees else 50

        def _hub_penalized_score(fp: str, cnt: int) -> float:
            in_deg = _in_degree_for_file(cur, fp)
            return cnt * (1.0 - min(1.0, in_deg / float(hub_scale)))

        top_callers = sorted(top_callers, key=lambda x: _hub_penalized_score(x[0], x[1]), reverse=True)[:limit]
        top_callees = sorted(top_callees, key=lambda x: _hub_penalized_score(x[0], x[1]), reverse=True)[:limit]

        # Structured capture: decay metadata + edges
        _primary_edge_file: str | None = None
        _primary_edge_kind: str | None = None
        if _evidence_accumulator is not None:
            _evidence_accumulator.append({
                "kind": "l3b_decay_metadata",
                "decay_applied": _decay_applied,
                "edge_limit_before": _edge_limit_before,
                "edge_limit_after": limit,
                "iteration_band": _iteration_band,
                "broad_navigation_after_60pct": iteration_ratio >= 0.60 and not _decay_applied,
            })
        # Mark primary edge (top caller, or top callee if no caller)
        if top_callers:
            _primary_edge_file = top_callers[0][0]
            _primary_edge_kind = "READ_CALLER_CONTRACT"
        elif top_callees:
            _primary_edge_file = top_callees[0][0]
            _primary_edge_kind = "READ_CONSUMER"

        if _evidence_accumulator is not None:
            for i, (fp, cnt) in enumerate(top_callers):
                _evidence_accumulator.append({
                    "kind": "l3b_caller_edge", "file_path": fp,
                    "text": f"{cnt} calls", "source": "graph_db",
                    "reason": f"calls symbol in {needle}",
                    "primary_edge": i == 0,
                })
            for i, (fp, cnt) in enumerate(top_callees):
                _evidence_accumulator.append({
                    "kind": "l3b_callee_edge", "file_path": fp,
                    "text": f"{cnt} calls", "source": "graph_db",
                    "reason": f"called by symbol in {needle}",
                    "primary_edge": i == 0 and not top_callers,
                })

        # Primary-edge rendering (GT_L3B_PRIMARY_EDGE)
        _l3b_primary = os.environ.get("GT_L3B_PRIMARY_EDGE", "0") == "1"

        # Improvement 3 + 5: Brief candidate annotation + symbol-level hints + layer tag
        def _format_neighbor(fp: str, cnt: int, source_line: int = 0) -> str:
            funcs = _top_functions_for_file(cur, fp, limit=2, edge_filter=_ef)
            func_names = ",".join(name for name, _ in funcs) if funcs else ""
            suffix = ""
            if any(fp == c or fp.endswith("/" + c) or c.endswith("/" + fp) for c in brief_candidates):
                suffix = " [CANDIDATE]"
            # L3b+ Enhancement: layer classification tag
            _layer_tag = _classify_layer_inline(fp)
            if _layer_tag:
                suffix += f" [{_layer_tag}]"
            # Show actual caller code line (mechanism #1: consumption visibility)
            code_snippet = ""
            if source_line > 0 and root:
                try:
                    full_path = os.path.join(root, fp)
                    with open(full_path, encoding="utf-8", errors="ignore") as _cf:
                        lines = _cf.readlines()
                    if source_line <= len(lines):
                        code_snippet = clip_balanced(lines[source_line - 1].strip(), 90)
                except OSError:
                    pass
            if code_snippet:
                return f"{fp}:{source_line} `{code_snippet}`{suffix}"
            if func_names:
                return f"{fp}::{func_names} ({cnt}x){suffix}"
            return f"{fp} ({cnt}x){suffix}"

        # Token caps per band (approx chars = tokens * 4)
        _char_caps = {"early_0_25": 1000, "mid_25_60": 640, "late_60_85": 320, "final_85_100": 0}
        _char_cap = _char_caps.get(_iteration_band, 1000) if _l3b_primary else 99999

        if _l3b_primary and iteration_ratio >= 0.25 and _primary_edge_file:
            # After early band: render ONLY primary edge
            primary_formatted = _format_neighbor(_primary_edge_file, top_callers[0][1] if top_callers else (top_callees[0][1] if top_callees else 0))
            label = "Called by" if top_callers else "Calls into"
            line = f"{label}: {primary_formatted}"
            if len(line) <= _char_cap:
                out.append(line)
        elif _l3b_primary and iteration_ratio >= 0.85:
            pass  # Final: silent unless tied to edit/failure
        else:
            # Early band or flag off: render all (original behavior)
            if top_callers:
                caller_files = [_format_neighbor(fp, cnt, _caller_source_lines.get(fp, 0)) for fp, cnt in top_callers]
                out.append(f"Called by: {', '.join(caller_files)}")
            # Rule 3 (R2/R5): Suppress callees during read-only exploration.
            # Callee info is useful for edit propagation (post_edit.py handles
            # that). During exploration, callees add noise the agent doesn't
            # follow. Research: Agentless phase separation, SE-agent lifecycle.

        # Importers: skip after 60% iteration (Change 4)
        # Importers: skip after 60% iteration, AND only then presence-probe (B10:
        # short-circuit avoids a wasted IMPORTS probe when the block is suppressed
        # anyway). Presence-probe: some indexes materialize 0 IMPORTS edges; an empty
        # "Imported by:" on such a graph FALSELY implies the file is un-imported.
        if not (rebuild_l3b and iteration_ratio >= 0.60) and cur.execute(
            "SELECT 1 FROM edges WHERE type = 'IMPORTS' LIMIT 1"
        ).fetchone():
            _resolved_imp = _resolve_file_path(conn, needle)
            _ef_imp = _edge_filter(db_path)
            cur.execute(
                # B9: a conf-1.0 import edge with NULL resolution_method/trust_tier
                # would be dropped by the categorical filter alone — keep it via the
                # numeric fallback (latent on the current indexer, defensive).
                f"""
                SELECT DISTINCT nsrc.file_path
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'IMPORTS'
                  AND ({_ef_imp} OR COALESCE(e.confidence, 0.5) >= 0.5)
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path = ?
                  AND nsrc.file_path != ?
                LIMIT ?
                """,
                (_resolved_imp, _resolved_imp, limit),
            )
            importers = [fp for (fp,) in cur.fetchall() if fp not in visited_files]
            if importers:
                out.append(f"Imported by: {', '.join(importers)}")
                # Structured capture: importers
                if _evidence_accumulator is not None:
                    for fp in importers:
                        _evidence_accumulator.append({
                            "kind": "l3b_importer_edge", "file_path": fp,
                            "source": "graph_db", "reason": f"imports from {needle}",
                        })

        # L4b-1: Exception path evidence (Calcagno et al. NFM 2015)
        # Rule 5 (R4): Only emit RAISES/CATCHES when issue keywords match
        # error-handling terms. OpenAI: "relevant context, not all context."
        _ERROR_KEYWORDS = frozenset({
            "error", "exception", "raise", "raises", "catch", "catches",
            "handle", "handler", "traceback", "crash", "fail", "failure",
            "throw", "thrown", "except", "unexpected",
        })
        _issue_terms_exc = set()
        try:
            _it_path = "/tmp/gt_issue_terms.txt"
            if os.path.exists(_it_path):
                with open(_it_path, encoding="utf-8", errors="ignore") as _itf:
                    _issue_terms_exc = {line.strip().lower() for line in _itf if line.strip()}
        except Exception:
            pass
        _issue_has_error_kw = bool(_issue_terms_exc & _ERROR_KEYWORDS)
        _has_props = False
        try:
            cur.execute("SELECT 1 FROM properties LIMIT 1")
            _has_props = True
        except Exception:
            pass
        if _has_props and _issue_has_error_kw:
            _exc_props = cur.execute(
                "SELECT p.kind, p.value FROM properties p "
                "JOIN nodes n ON p.node_id = n.id "
                "WHERE n.file_path = ? "
                "AND p.kind IN ('exception_flow','exception_handler') "
                "LIMIT 5",
                (needle,),
            ).fetchall()
            if _exc_props:
                _exc_parts = []
                for kind, val in _exc_props:
                    tag = "CATCHES" if kind == "exception_handler" else "RAISES"
                    # C1: stored exception_flow/handler is source text; balance-aware
                    # clip so a truncated raise/except never reaches the agent.
                    if isinstance(val, str) and val:
                        val = clip_balanced(val)
                    _exc_parts.append(f"[{tag}] {val}")
                out.append(" | ".join(_exc_parts))

        # Progress tracking (Change 4)
        if rebuild_l3b and total_candidates > 0 and visited_files:
            out.insert(0, f"[Progress: visited {len(visited_files)}/{total_candidates} connected files]")

        # Late-phase focus tag (Change 4)
        if rebuild_l3b and iteration_ratio >= 0.85 and out:
            out.insert(0, "[FOCUS: late-phase, showing only top connection]")

    except Exception as exc:
        print(f"[GT_META] graph_navigation_error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return [], 0
    finally:
        conn.close()

    # Four-pillar ego-graph: prepend ONLY when confident.
    # RepoGraph ICLR 2025: ego-graph flattened as context, 32.8% improvement.
    # Three safety gates (learned from weasyprint regression):
    #   1. Graph must be strong (>= 0.9 confidence gate)
    #   2. Function must be issue-relevant (not just most-called)
    #   3. Must have callers passing the gate (otherwise silence)
    try:
        from groundtruth.graph.ego import ego_graph as _ego
        _issue_terms = _load_issue_terms(state)  # Fix D: pass state so terms load
        _ego_conn = sqlite3.connect(db_path)
        _ego_conn.row_factory = sqlite3.Row
        _funcs = _ego_conn.execute(
            "SELECT name, file_path FROM nodes "
            "WHERE file_path = ? AND label IN ('Function','Method') AND is_test = 0 "
            "LIMIT 10",
            (needle,),
        ).fetchall()
        _ego_conn.close()
        # Pick issue-relevant function (not most-called — avoids hub bias)
        _best_func = None
        if _issue_terms and _funcs:
            for _f in _funcs:
                if _f["name"].lower() in _issue_terms:
                    _best_func = _f["name"]
                    break
        if _best_func:
            _eg = _ego(db_path, _best_func, needle, k=1, min_confidence=0.9)
            # C2 symbol-homonym guard (correct-or-quiet): _best_func is a BARE
            # function NAME. ego_graph resolves it to a center node, but with a
            # LIMIT-1 / most-called tiebreak it can land on a homonym defined in
            # a DIFFERENT file (the beets set_fields()-in-zero.py bug, where the
            # agent is editing importer.py::set_fields). Require that the
            # resolved center lives in the SAME file the agent is viewing; if the
            # name resolves to a file other than the viewed file, stay silent
            # rather than show a wrong-file homonym. Generalized: pure path
            # identity, no hardcoded names.
            _center_ok = (
                _eg.center is not None
                and _same_stored_file(getattr(_eg.center, "file_path", ""), needle)
            )
            if _center_ok and len(_eg.callers) > 0:
                # DISABLED (swap-invariant — run15 leak): strip ALL test-derived
                # content from the ego render before it reaches the agent. The
                # EgoGraph.render() output (inserted into `out`) otherwise surfaces
                # (a) a "Tests:" block built from `test_assertions` (test name +
                # assertion body), and (b) caller lines for `is_test` callers (a
                # test function NAME + test file basename). Per the SWAP-INVARIANT,
                # GT must surface ZERO test references. We null the test_assertions
                # list and drop test-flagged callers BEFORE render, then filter any
                # residual test line defensively. Contract / non-test callers /
                # consistency / callees pillars are unaffected.
                _eg.test_assertions = []
                _eg.nodes = {
                    _nid: _n for _nid, _n in _eg.nodes.items()
                    if not getattr(_n, "is_test", False)
                    or (_eg.center is not None and _nid == _eg.center.id)
                }
                _eg.edges = [
                    _e for _e in _eg.edges
                    if _e.source_id in _eg.nodes and _e.target_id in _eg.nodes
                ]
                _ego_text = _eg.render(max_tokens=150)
                if _ego_text:
                    # Defensive: drop any line that still carries a test marker or a
                    # "Tests:" section header (belt-and-suspenders vs the swap-invariant).
                    _ego_lines = []
                    _in_tests_block = False
                    for _el in _ego_text.split("\n"):
                        _els = _el.strip()
                        if _els == "Tests:":
                            _in_tests_block = True
                            continue
                        if _in_tests_block:
                            # Tests: items are indented; a non-indented line ends it.
                            if _el.startswith("  ") or _el.startswith("\t"):
                                continue
                            _in_tests_block = False
                        if "[test]" in _els.lower():
                            continue
                        _ego_lines.append(_el)
                    _ego_text = "\n".join(_ego_lines).strip()
                if _ego_text:
                    out.insert(0, _ego_text)
                    print(f"[GT_META] ego_graph_view: func={_best_func} callers={len(_eg.callers)} "
                          f"guards={len(_eg.guards)} tests={len(_eg.test_assertions)}", file=sys.stderr, flush=True)
            elif _eg.center is not None and not _center_ok:
                print(f"[GT_META] ego_graph_homonym_suppressed: func={_best_func} "
                      f"center_file={getattr(_eg.center, 'file_path', '')} viewed={needle}",
                      file=sys.stderr, flush=True)
    except Exception as _ego_exc:
        print(f"[GT_META] ego_graph_view_error: {type(_ego_exc).__name__}: {_ego_exc}", file=sys.stderr, flush=True)

    # Fix B (CLAUDE.md:86): Contract pillar ALWAYS fires on the main path.
    # signature/return come from the `nodes` table — no graph edges needed.
    # Never gate this behind a caller-connectivity check; a function with 0
    # high-confidence callers is exactly where the agent is most blind.
    # Prepend so the agent sees the contract before caller/callee navigation.
    try:
        _cp_conn = sqlite3.connect("file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro", uri=True)
        try:
            _contract_lines = _contract_pillar(_cp_conn, needle, _load_issue_terms(state))
        finally:
            _cp_conn.close()
        if _contract_lines:
            # Insert at front so Contract leads the evidence (U-shaped salience).
            for _cl in reversed(_contract_lines):
                out.insert(0, _cl)
            print(
                f"[GT_META] contract_pillar: file={needle} lines={len(_contract_lines)}",
                file=sys.stderr, flush=True,
            )
    except Exception as _cp_exc:
        print(f"[GT_META] contract_pillar_error: {type(_cp_exc).__name__}: {_cp_exc}", file=sys.stderr, flush=True)

    # DISABLED (swap-invariant — run15 leak): test-assertion surfacing for the
    # VIEWED file. This block queried the `assertions` table (test-derived) and
    # `tn.name as test_name` (a test function's name) and appended
    # `[TEST] {test_name}: {expression}` lines to `out`, which is returned to
    # main() and printed to stdout (the wrapper delivers it to the agent). Per
    # the SWAP-INVARIANT, GT must surface ZERO test references to the agent — no
    # test function names, no assertion bodies. The entire emitter is disabled.
    # (The `assertions` table remains available for INTERNAL gating elsewhere; it
    # is only its AGENT-SURFACED rendering that is forbidden.)
    #
    # Original block (test-name + assertion-body → agent) removed from the
    # render path. Do NOT re-enable without a non-test-derived contract source.

    # --- L3b TOKEN-CAP ENFORCEMENT (the fix: trim, don't just log) ---
    # Trim the rendered nav lines to the total L3b budget, dropping the lowest-
    # priority navigation first and preserving contract/test/ego lines. Persist
    # the final token count + cap_enforced so metrics can verify the cap held.
    _pre_count = len(out)
    _pre_tokens = _estimate_l3b_tokens(out)
    out, _final_tokens, _cap_enforced = _enforce_l3b_cap(out, _L3B_MAX_TOKENS)
    _dropped = _pre_count - len(out)
    _emit_l3b_cap_event(needle, _final_tokens, _cap_enforced, _dropped)
    if _cap_enforced:
        print(
            f"[GT_META] l3b_cap_enforced: file={needle} pre_tokens={_pre_tokens} "
            f"final_tokens={_final_tokens} dropped={_dropped} cap={_L3B_MAX_TOKENS}",
            file=sys.stderr, flush=True,
        )

    return out, total_callers


def _file_function_spec(db_path: str, file_path: str, repo_root: str) -> str:
    """Show parallel patterns in the viewed file's main functions.

    Delivered at VIEW time = before the agent edits. This is the pre-edit
    specification surface that prevents incomplete fixes.
    """
    try:
        conn = sqlite3.connect(db_path)
        _resolved_spec = _resolve_file_path(conn, file_path)
        rows = conn.execute(
            "SELECT name, start_line, end_line FROM nodes "
            "WHERE file_path = ? AND label IN ('Function','Method') AND is_test = 0 "
            "ORDER BY start_line LIMIT 5",
            (_resolved_spec,),
        ).fetchall()
        conn.close()
    except Exception:
        return ""

    if not rows:
        return ""

    from groundtruth.hooks.post_edit import _make_template

    full_path = os.path.join(repo_root, file_path)
    try:
        with open(full_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

    specs = []
    for name, start, end in rows:
        if not start or not end:
            continue
        func_lines = all_lines[max(0, start - 1):min(len(all_lines), end)]
        templates: dict[str, list[str]] = {}
        for line in func_lines:
            stripped = line.strip()
            if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
                continue
            tmpl = _make_template(stripped)
            if tmpl not in templates:
                templates[tmpl] = []
            templates[tmpl].append(stripped)

        groups = [(t, lns) for t, lns in templates.items() if 2 <= len(lns) <= 8]
        if groups:
            groups.sort(key=lambda x: -len(x[1]))
            cases = [ln if len(ln) <= 45 else clip_balanced(ln, 42) + "..." for ln in groups[0][1][:4]]
            specs.append(f"{name} handles: {' | '.join(cases)}")

    if not specs:
        return ""
    return "Spec: " + specs[0]


def _test_file_targets(db_path: str, test_file_path: str, repo_root: str = "") -> list[str]:
    """Find source functions called by this test file and issue-relevant assertions."""
    try:
        conn = sqlite3.connect(db_path)
        _resolved_test = _resolve_file_path(conn, test_file_path)
        rows = conn.execute(
            f"""SELECT DISTINCT nt.name, nt.file_path FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ? AND nsrc.is_test = 1 AND nt.is_test = 0
            AND {_edge_filter(db_path)}
            LIMIT 5""",
            (_resolved_test,),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    lines = [f"Calls into: {fpath}::{name}()" for name, fpath in rows]
    # DISABLED (swap-invariant — run15 leak): assertion-body surfacing from the
    # viewed test file. This block regex-scraped `assert .../expect(...).to...`
    # lines out of the test source and appended them as `[TEST] {assertion}`,
    # which main() prints to stdout (delivered to the agent). Per the
    # SWAP-INVARIANT, GT must surface ZERO test references — no assertion bodies.
    # The `Calls into:` lines above are source-function targets (NOT test-derived)
    # and are KEPT; only the `[TEST] {assertion}` emission is removed.
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-view enrichment hook")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--file", required=True, help="File path to enrich")
    parser.add_argument("--iteration-ratio", type=float, default=0.0)
    parser.add_argument("--total-candidates", type=int, default=0)
    parser.add_argument("--structured-output", action="store_true")
    args = parser.parse_args()

    start = time.time()
    _append_gt_log("fire", f"root={args.root} file={args.file} db={args.db}")
    log_entry = {
        "hook": "post_view",
        "endpoint": "understand",
        "file": args.file,
        "classes_found": 0,
        "coupled_classes": 0,
    }

    filepath = args.file
    if _is_test_file(filepath):
        targets = _test_file_targets(args.db, filepath, repo_root=args.root)
        if targets:
            for t in targets:
                print(t)
            status = _status_line("success", f"test_targets:{len(targets)}")
        else:
            status = _status_line("skipped", "test_file_no_targets")
        print(status, file=sys.stderr)
        _append_gt_log("status", status)
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    # Pass total_candidates via env for graph_navigation to pick up
    if args.total_candidates > 0:
        os.environ["GT_L3B_TOTAL_CANDIDATES"] = str(args.total_candidates)

    # Graph navigation is PRIMARY — shows the agent where this file
    # connects so agent + GT collaborate on localization
    _accum = [] if args.structured_output else None
    nav_lines, total_callers = graph_navigation(
        filepath, args.db, iteration_ratio=args.iteration_ratio,
        _evidence_accumulator=_accum,
    )

    # Function spec: show parallel patterns in viewed file's functions (pre-edit context)
    spec_line = _file_function_spec(args.db, filepath, args.root)
    if spec_line:
        nav_lines.append(spec_line)

    if nav_lines:
        print("\n".join(nav_lines))
        if args.structured_output and _accum:
            import json as _json
            print("__GT_STRUCTURED__")
            print(_json.dumps(_accum))
        status = _status_line("success", f"{len(nav_lines)}_items")
        print(status, file=sys.stderr)
        _append_gt_log("status", status)
    else:
        status = _status_line("no_evidence", "no_graph_edges")
        print(status, file=sys.stderr)
        _append_gt_log("status", status)

    log_entry["output_lines"] = len(nav_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        status = _status_line("error", f"{type(exc).__name__}:{exc}")
        print(status, file=sys.stderr)
        _append_gt_log("status", status)
