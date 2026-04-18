#!/usr/bin/env python3
"""GT Intelligence Layer v15 — reads graph.db from Go indexer, produces ranked evidence.

7 evidence families, scored 0-3:
  IMPORT:    correct import paths for callees in other files
  CALLER:    how cross-file callers use the target's return value
  SIBLING:   behavioral norms from sibling methods in the same class
  TEST:      assertions from test functions that reference the target
  IMPACT:    blast radius (caller count + critical path)
  TYPE:      return type contract from annotation + caller confirmation
  PRECEDENT: last git commit that touched the target function

v15: Relaxed admissibility — edges with same_file, import, OR name_match pass through
(cross-file import resolution via symbol name). If same_file leaks across files are
detected, same_file is dropped but import + name_match remain.
Output: tiered high-confidence (score>=2) + additional context (score=1).
Enhanced pre-task briefing: upfront evidence before the PR description.

Usage:
    python3 gt_intel.py --db=/tmp/gt_graph.db --file=src/model.py --root=/app
    python3 gt_intel.py --db=/tmp/gt_graph.db --file=src/model.py --root=/app --log=/tmp/ev.jsonl
    python3 gt_intel.py --db=/tmp/gt_graph.db --briefing --issue-text="fix do_encrypt" --root=/app
    python3 gt_intel.py --db=/tmp/gt_graph.db --enhanced-briefing --issue-text=@/tmp/issue.txt --root=/app
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass

# ── v1.0.4: GT telemetry helper ───────────────────────────────────────────

def _log_gt_telemetry(file: str, event: str, detail: str = "") -> None:
    """Write a structured telemetry event for GT hook observability."""
    try:
        import time as _t
        entry = {"ts": _t.strftime("%H:%M:%S"), "file": file, "event": event}
        if detail:
            entry["detail"] = detail[:200]
        with open("/tmp/gt_telemetry.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── v17: Staleness detection ───────────────────────────────────────────────

def _log_freshness(source_file: str, status: str) -> None:
    """v1.0.4 telemetry: log freshness confidence for debugging."""
    try:
        import json as _json
        with open("/tmp/gt_freshness.jsonl", "a") as f:
            f.write(_json.dumps({"file": source_file, "freshness": status}) + "\n")
    except Exception:
        pass


def check_staleness(db_path: str, source_file: str, root: str) -> str | None:
    """Return a warning string if graph.db is behind the source file.

    v1.0.4: Also checks file_hashes table for hash-based freshness.
    Returns 'SUPPRESS' if evidence should be suppressed entirely (stale hash),
    or a warning string for informational staleness, or None if fresh.
    """
    try:
        src_path = os.path.join(root, source_file) if not os.path.isabs(source_file) else source_file
        if not os.path.exists(src_path):
            return f"{os.path.basename(source_file)} no longer exists — evidence may reference deleted code"

        # v1.0.4: Hash-based freshness check via file_hashes table
        try:
            import hashlib
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT content_hash FROM file_hashes WHERE file_path = ?",
                (source_file,),
            ).fetchone()
            conn.close()
            if row:
                with open(src_path, "rb") as f:
                    current_hash = hashlib.sha256(f.read()).hexdigest()
                if current_hash != row[0]:
                    _log_freshness(source_file, "suppressed_stale_hash")
                    return "SUPPRESS"
                _log_freshness(source_file, "fresh_by_hash")
                return None
            else:
                _log_freshness(source_file, "no_hash_entry")
        except Exception:
            pass  # Fall through to mtime check

        # Fallback: mtime-based check
        db_mtime = os.path.getmtime(db_path)
        if os.path.getmtime(src_path) > db_mtime:
            _log_freshness(source_file, "stale_by_mtime")
            return f"graph.db is behind {os.path.basename(source_file)} — evidence may be stale"
        _log_freshness(source_file, "fresh_by_mtime")
    except OSError:
        pass
    return None


# ── v1.0.4: Test file filter ──────────────────────────────────────────────
_TEST_PATH_PATTERNS = frozenset({
    "test_", "_test.", ".test.", ".spec.", "conftest.py",
    "/tests/", "/test/", "__tests__/", "/spec/",
})


def _is_test_path(path: str) -> bool:
    """v1.0.4: Return True if path looks like a test file.
    Test files must NEVER appear in TARGET or ALSO."""
    path_lower = path.lower().replace("\\", "/")
    return any(p in path_lower for p in _TEST_PATH_PATTERNS)


# ── v15: Admissibility gate ────────────────────────────────────────────────
# Edges with verified resolution pass (Go indexer is source of truth).
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})


def _resolution_sql_in() -> tuple[str, tuple[str, ...]]:
    """SQL IN clause placeholders and bound values for current VERIFIED_RESOLUTIONS."""
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    return ",".join("?" * len(methods)), methods


# Minimum confidence threshold for evidence inclusion.
# Edges below this are excluded from callers/callees/tests queries.
MIN_CONFIDENCE = 0.5


def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    """Check if the edges table has a confidence column (v14+ indexer)."""
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _confidence_clause(has_confidence: bool, alias: str = "e") -> str:
    """Return SQL clause for confidence filtering, or empty string for old DBs."""
    if has_confidence:
        return f" AND {alias}.confidence >= {MIN_CONFIDENCE}"
    return ""


def is_admissible(resolution_method: str) -> bool:
    """True if resolution_method is allowed through the gate."""
    return resolution_method in VERIFIED_RESOLUTIONS


def verify_admissibility_gate(conn: sqlite3.Connection) -> bool:
    """Check for same_file edges that cross file boundaries (resolution leak).
    If found, narrow VERIFIED_RESOLUTIONS to import + name_match only."""
    global VERIFIED_RESOLUTIONS
    try:
        row = conn.execute("""
            SELECT COUNT(*) FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.resolution_method = 'same_file'
              AND s.file_path != t.file_path
        """).fetchone()
        leaks = row[0] if row else 0
        if leaks > 0:
            print(f"WARNING: {leaks} same_file cross-file leaks — removing same_file from gate",
                  file=sys.stderr)
            VERIFIED_RESOLUTIONS = frozenset({"import", "name_match"})
            return False
    except Exception:
        pass
    return True


# ── Six-invariant evidence-block gate ───────────────────────────────────────
# Consolidated admissibility check applied at block-emission time. Every
# injected <gt-evidence> block must pass all six. Failures are counted in the
# per-task JSON log under briefing.admissibility_gate.rejection_reasons.

_CONCISE_MAX_LINES = 5       # lines per block (matches prior ad-hoc cap)
_BUDGET_MAX_BLOCKS = 3       # evidence blocks per task (prior BUDGET invariant)


def admit_evidence_block(
    block_text: str,
    resolution_methods: list[str] | None = None,
    source_file: str | None = None,
    caller_count: int | None = None,
    has_assertion: bool = False,
    blocks_emitted_so_far: int = 0,
) -> tuple[bool, str | None]:
    """Return (admitted, rejection_invariant).

    Invariants (ordered; first failure wins):
      CONFIDENCE — at least one edge method in VERIFIED_RESOLUTIONS.
      NOT_TEST   — source_file must not be a test path.
      NO_SPAM    — BREAKING/STALE lines require caller_count > 0.
      BUDGET     — already-emitted block count must be below cap.
      CONCISE    — block text must be non-empty and within line cap.
      HAS_VALUE  — ≥1 deterministic edge OR ≥1 assertion.
    """
    if not block_text or not block_text.strip():
        return False, "CONCISE"

    if resolution_methods is not None:
        if not any(is_admissible(m) for m in resolution_methods):
            return False, "CONFIDENCE"

    if source_file is not None and _is_test_path(source_file):
        return False, "NOT_TEST"

    if ("BREAKING:" in block_text or "STALE:" in block_text) and not (
        caller_count is not None and caller_count > 0
    ):
        return False, "NO_SPAM"

    if blocks_emitted_so_far >= _BUDGET_MAX_BLOCKS:
        return False, "BUDGET"

    line_count = sum(1 for ln in block_text.splitlines() if ln.strip())
    if line_count > _CONCISE_MAX_LINES:
        return False, "CONCISE"

    deterministic = frozenset({"same_file", "import"})
    has_det_edge = bool(
        resolution_methods and any(m in deterministic for m in resolution_methods)
    )
    if not (has_det_edge or has_assertion):
        return False, "HAS_VALUE"

    return True, None


# ── Data types ──────────────────────────────────────────────────────────────

@dataclass
class EvidenceNode:
    family: str       # CALLER, SIBLING, TEST, IMPACT, TYPE
    score: int        # 0-3
    name: str
    file: str
    line: int
    source_code: str  # real source lines
    summary: str

@dataclass
class GraphNode:
    id: int
    label: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    return_type: str
    is_exported: bool
    is_test: bool
    language: str
    parent_id: int


class _ConnGraphReader:
    """Minimal substrate-compatible reader over the legacy sqlite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_node_by_id(self, node_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "label": row[1],
            "name": row[2],
            "qualified_name": row[3] or "",
            "file_path": row[4],
            "start_line": row[5] or 0,
            "end_line": row[6] or 0,
            "signature": row[7] or "",
            "return_type": row[8] or "",
            "is_exported": bool(row[9]),
            "is_test": bool(row[10]),
            "language": row[11] or "",
            "parent_id": row[12] or 0,
        }

    def get_callers(self, node_id: int) -> list[dict]:
        ph, methods = _resolution_sql_in()
        conf_clause = _confidence_clause(_has_confidence_column(self._conn))
        rows = self._conn.execute(f"""
            SELECT e.source_id, e.source_line, e.source_file,
                   e.resolution_method, COALESCE(e.confidence, 1.0),
                   n.name, n.file_path
            FROM edges e
            JOIN nodes n ON n.id = e.source_id
            WHERE e.target_id = ? AND e.type = 'CALLS'
              AND e.resolution_method IN ({ph}){conf_clause}
        """, (node_id, *methods)).fetchall()
        return [
            {
                "source_id": row[0],
                "source_line": row[1] or 0,
                "source_file": row[2] or "",
                "resolution_method": row[3] or "",
                "confidence": row[4] or 0.0,
                "source_name": row[5] or "",
                "source_file_path": row[6] or "",
            }
            for row in rows
        ]

    def get_properties(self, node_id: int, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self._conn.execute(
                "SELECT id, node_id, kind, value, line, confidence FROM properties WHERE node_id = ? AND kind = ?",
                (node_id, kind),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, node_id, kind, value, line, confidence FROM properties WHERE node_id = ?",
                (node_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "node_id": row[1],
                "kind": row[2] or "",
                "value": row[3] or "",
                "line": row[4] or 0,
                "confidence": row[5] or 0.0,
            }
            for row in rows
        ]

# ── v1.0.4: Structured localization state ─────────────────────────────────
# Research basis: BugCerberus (hierarchical localization), Think-Search-Patch
# (candidate refinement), SWE-bench-Live (localization is critical but imperfect).
# Confidence gates the strength of the message, not just whether GT speaks.

@dataclass
class LocalizationCandidate:
    """A candidate target for the fix, with hierarchical confidence."""
    node: GraphNode
    confidence: float           # 0.0-1.0 overall resolution confidence
    tier: str                   # "verified", "likely", "possible"
    file_confidence: float      # how sure about the FILE (may be higher than symbol)
    symbol_confidence: float    # how sure about the specific FUNCTION
    reasons: list               # ["name_match", "file_mentioned", "stack_trace", ...]


@dataclass
class LocalizationState:
    """Structured localization state — confidence-gated, not free-form text."""
    candidates: list            # list[LocalizationCandidate]
    structural_unlocked: bool   # True only when top candidate is "verified"
    issue_identifiers: list     # identifiers extracted from issue text


def compute_localization(
    conn: sqlite3.Connection,
    issue_text: str,
    root: str = "",
) -> LocalizationState:
    """Compute structured localization state from issue text + graph.

    Phases: extract identifiers → resolve targets → rerank → assign tiers.
    Structural guidance (OBLIGATION/CALLER) is only unlocked for verified targets.
    """
    identifiers = extract_identifiers_from_issue(issue_text)
    if not identifiers:
        return LocalizationState(candidates=[], structural_unlocked=False, issue_identifiers=[])

    # Resolve using existing machinery
    resolved = resolve_briefing_targets(conn, identifiers, max_targets=3)
    if not resolved:
        return LocalizationState(candidates=[], structural_unlocked=False, issue_identifiers=identifiers)

    issue_lower = issue_text.lower()
    candidates = []
    for node, tier in resolved:
        rc = _resolution_confidence_for_node(conn, node, identifiers)

        # Hierarchical: file confidence >= symbol confidence
        file_conf = min(1.0, rc + 0.1)  # file is slightly easier to get right
        sym_conf = rc

        reasons = []
        # Reranking boosts (Phase 4)
        # Direct file path mention in issue text
        if node.file_path.lower() in issue_lower or os.path.basename(node.file_path).lower() in issue_lower:
            file_conf = min(1.0, file_conf + 0.2)
            reasons.append("file_mentioned_in_issue")

        # Multiple identifiers pointing to same file
        file_hits = sum(1 for ident in identifiers if ident.lower() in node.file_path.lower())
        if file_hits >= 2:
            file_conf = min(1.0, file_conf + 0.1)
            reasons.append(f"multi_identifier_file_hit({file_hits})")

        # Stack trace file match
        import re as _re
        stack_files = _re.findall(r'File "([^"]+)"', issue_text)
        for sf in stack_files:
            if os.path.basename(sf) == os.path.basename(node.file_path):
                file_conf = min(1.0, file_conf + 0.3)
                sym_conf = min(1.0, sym_conf + 0.1)
                reasons.append("stack_trace_match")
                break

        if not reasons:
            reasons.append("graph_resolution")

        # Re-assess tier based on boosted confidence
        effective_conf = max(rc, (file_conf + sym_conf) / 2)
        if effective_conf >= 0.85:
            tier = "verified"
        elif effective_conf >= 0.6:
            tier = "likely"
        else:
            tier = "possible"

        candidates.append(LocalizationCandidate(
            node=node, confidence=effective_conf, tier=tier,
            file_confidence=file_conf, symbol_confidence=sym_conf,
            reasons=reasons,
        ))

    # Sort by confidence descending
    candidates.sort(key=lambda c: -c.confidence)

    # Structural guidance unlocked only for verified top candidate
    structural_unlocked = len(candidates) > 0 and candidates[0].tier == "verified"

    return LocalizationState(
        candidates=candidates,
        structural_unlocked=structural_unlocked,
        issue_identifiers=identifiers,
    )


def _resolution_confidence_for_node(
    conn: sqlite3.Connection, node: GraphNode, identifiers: list[str]
) -> float:
    """Compute resolution confidence for a specific node (wrapper for existing scoring)."""
    # Use the name quality + module score + ambiguity + centrality formula
    name_q = 1.0 if node.qualified_name and any(
        i.lower() == node.name.lower() for i in identifiers
    ) else 0.8

    # Module score: overlap between issue identifiers and file path tokens
    path_tokens = set(node.file_path.replace("/", " ").replace(".", " ").replace("_", " ").lower().split())
    ident_tokens = set(i.lower() for i in identifiers)
    overlap = len(path_tokens & ident_tokens)
    mod_score = min(1.0, overlap / max(len(ident_tokens), 1))

    # Ambiguity: check how many nodes share this name
    count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE name = ? AND is_test = 0",
        (node.name,)
    ).fetchone()[0]
    if count <= 1:
        ambiguity = 1.0
    elif count == 2:
        ambiguity = 0.7
    elif count <= 5:
        ambiguity = 0.4
    else:
        ambiguity = 0.2

    # Centrality: caller count
    callers = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
        (node.id,)
    ).fetchone()[0]
    import math
    centrality = min(1.0, math.log(callers + 1) / 5.0)

    return 0.3 * name_q + 0.4 * mod_score + 0.2 * ambiguity + 0.1 * centrality


def format_localization_briefing(
    state: LocalizationState,
    conn: sqlite3.Connection,
    root: str,
) -> str:
    """Format localization state into a confidence-gated micro-briefing.

    High confidence → structural guidance (OBLIGATION, CALLER, TEST).
    Medium → candidate shortlist, no structural constraints.
    Low → minimal hint only.
    """
    if not state.candidates:
        return ""

    top = state.candidates[0]
    lines = []

    if state.structural_unlocked and top.tier == "verified":
        # HIGH CONFIDENCE: show target + structural evidence
        lines.append(f"[GT] Target: {top.node.name}() at {top.node.file_path}:{top.node.start_line} (high confidence)")
        # Compute evidence for this target
        evidence = compute_evidence(conn, root, top.node)
        selected = rank_and_select(evidence)
        for ev in selected[:3]:
            bullet = _evidence_constraint_bullet(ev, top.node)
            lines.append(f"  {bullet}")

    elif top.tier == "likely":
        # MEDIUM CONFIDENCE: show candidate shortlist
        lines.append("[GT] Likely candidates (investigate before editing):")
        for i, c in enumerate(state.candidates[:3]):
            lines.append(f"  {i+1}. {c.node.name}() at {c.node.file_path}:{c.node.start_line}")

    else:
        # LOW CONFIDENCE: minimal hint
        files = list(dict.fromkeys(c.node.file_path for c in state.candidates[:3]))
        if files:
            lines.append(f"[GT] Low confidence. Possibly relevant: {', '.join(files)}")

    return "\n".join(lines) if lines else ""


# ── Source code reader ──────────────────────────────────────────────────────

def read_lines(root: str, rel_path: str, start: int, end: int) -> str:
    """Read source lines from a file. Returns dedented text."""
    abs_path = os.path.join(root, rel_path)
    try:
        with open(abs_path, "r", errors="replace") as f:
            lines = f.readlines()
        chunk = lines[max(0, start - 1):min(end, len(lines))]
        if not chunk:
            return ""
        min_indent = min((len(l) - len(l.lstrip()) for l in chunk if l.strip()), default=0)
        return "".join(l[min_indent:] if len(l) > min_indent else l for l in chunk).rstrip()
    except (OSError, IndexError):
        return ""

# ── Graph queries ───────────────────────────────────────────────────────────

def get_target_node(conn: sqlite3.Connection, file_path: str, function_name: str = "") :
    """Find the primary target node in the given file."""
    cur = conn.cursor()

    if function_name:
        cur.execute(
            "SELECT * FROM nodes WHERE file_path=? AND name=? AND label IN ('Function','Method')"
            " AND is_test = 0 LIMIT 1",
            (file_path, function_name),
        )
    else:
        # Pick the node with the most incoming CALLS edges
        cur.execute("""
            SELECT n.* FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
            WHERE n.file_path = ? AND n.label IN ('Function', 'Method', 'Class')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY COUNT(e.id) DESC
            LIMIT 1
        """, (file_path,))

    row = cur.fetchone()
    if not row:
        # Try fuzzy match on file path suffix
        cur.execute("""
            SELECT n.* FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
            WHERE n.file_path LIKE ? AND n.label IN ('Function', 'Method', 'Class')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY COUNT(e.id) DESC
            LIMIT 1
        """, ("%" + os.path.basename(file_path),))
        row = cur.fetchone()

    if not row:
        return None
    # v1.0.4: Double-check path isn't a test file
    node = _row_to_node(row)
    if _is_test_path(node.file_path):
        return None
    return node


def get_callers(conn: sqlite3.Connection, target_id: int, target_file: str) -> list[tuple[GraphNode, int, str, str]]:
    """Get cross-file callers of target. Returns (caller_node, call_line, source_file, resolution_method)."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    conf_clause = _confidence_clause(_has_confidence_column(conn))
    cur.execute(f"""
        SELECT n.*, e.source_line, e.source_file, e.resolution_method
        FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND e.source_file != ?
          AND e.resolution_method IN ({ph}){conf_clause}
        LIMIT 10
    """, (target_id, target_file, *methods))

    results = []
    for row in cur.fetchall():
        node = _row_to_node(row[:-3])
        call_line = row[-3] or 0
        source_file = row[-2] or ""
        resolution_method = row[-1] or ""
        results.append((node, call_line, source_file, resolution_method))
    return results


def get_siblings(conn: sqlite3.Connection, target_id: int) -> list[GraphNode]:
    """Get sibling methods (same parent class)."""
    cur = conn.cursor()
    # First find the parent
    cur.execute("SELECT parent_id FROM nodes WHERE id=?", (target_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return []
    parent_id = row[0]

    cur.execute(
        "SELECT * FROM nodes WHERE parent_id=? AND label IN ('Function','Method') AND id!=?",
        (parent_id, target_id),
    )
    return [_row_to_node(r) for r in cur.fetchall()]


def get_tests(conn: sqlite3.Connection, target_id: int) -> list[GraphNode]:
    """Get test functions that call the target."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    conf_clause = _confidence_clause(_has_confidence_column(conn))
    cur.execute(f"""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
          AND e.resolution_method IN ({ph}){conf_clause}
        LIMIT 5
    """, (target_id, *methods))
    return [_row_to_node(r) for r in cur.fetchall()]


def get_all_callers_count(conn: sqlite3.Connection, target_id: int) -> tuple[int, int]:
    """Returns (total_callers, unique_files). Only counts admissible edges."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    conf_clause = _confidence_clause(_has_confidence_column(conn), alias="edges")
    cur.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT source_file)
        FROM edges WHERE target_id=? AND type='CALLS'
          AND resolution_method IN ({ph}){conf_clause}
    """, (target_id, *methods))
    row = cur.fetchone()
    return (row[0] or 0, row[1] or 0) if row else (0, 0)


def _row_to_node(row) -> GraphNode:
    return GraphNode(
        id=row[0], label=row[1], name=row[2], qualified_name=row[3] or "",
        file_path=row[4], start_line=row[5] or 0, end_line=row[6] or 0,
        signature=row[7] or "", return_type=row[8] or "",
        is_exported=bool(row[9]), is_test=bool(row[10]),
        language=row[11] or "", parent_id=row[12] or 0,
    )

# ── Caller usage classification ────────────────────────────────────────────

CRITICAL_PATHS = {"auth", "security", "session", "password", "token",
                  "permission", "payment", "crypto", "login", "credential",
                  "middleware", "core"}


def classify_caller_usage(root: str, file_path: str, call_line: int) -> tuple[int, str, str]:
    """v20: Read lines around a call site and classify usage.

    Returns (score, summary, call_line_text) — the actual source line is the spec.
    """
    text = read_lines(root, file_path, max(1, call_line - 1), call_line + 2)
    if not text:
        return 1, "invokes", ""

    # Extract the actual call line for the spec
    lines = text.splitlines()
    call_text = lines[min(1, len(lines) - 1)].strip() if lines else ""
    if len(call_text) > 120:
        call_text = call_text[:117] + "..."

    # Score 3: destructure or type assertion
    if re.search(r'(\w+)\s*,\s*(\w+)\s*=\s*', text):
        return 3, f"called as: {call_text}", call_text
    if re.search(r'isinstance\(', text):
        return 3, f"called as: {call_text}", call_text
    if re.search(r'\.\w+\b', text) and not re.search(r'\.\w+\s*\(', text):
        return 3, f"called as: {call_text}", call_text

    # Score 2: conditional usage
    if re.search(r'if\s+.*\w+\(', text):
        return 2, f"called as: {call_text}", call_text
    if re.search(r'(==|!=|is |is not |>=|<=|>|<)\s*', text):
        return 2, f"called as: {call_text}", call_text
    if re.search(r'assert', text):
        return 2, f"called as: {call_text}", call_text

    # Score 1: just invokes
    return 1, f"called as: {call_text}" if call_text else "invokes", call_text


def is_critical_path(file_path: str) -> bool:
    fp = file_path.lower()
    basename = os.path.basename(fp)
    # Exclude test files from critical path classification
    if (basename.startswith("test_") or "_test." in basename or ".test." in basename
            or ".spec." in basename or basename.endswith("Test") or basename.endswith("Tests")
            or "/test/" in fp or "/tests/" in fp or "/__tests__/" in fp or "/spec/" in fp):
        return False
    return any(kw in fp for kw in CRITICAL_PATHS)

# ── Assertion extraction ────────────────────────────────────────────────────

ASSERTION_PATTERNS = {
    "python": [r'assert\w*\s*\((.{5,80})\)', r'self\.assert\w+\((.{5,80})\)', r'pytest\.raises\((\w+)\)'],
    "go": [r't\.\w+\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)', r'require\.\w+\((.{5,80})\)'],
    "javascript": [r'expect\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)'],
    "typescript": [r'expect\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)'],
    "java": [r'assert\w+\((.{5,80})\)', r'assertEquals\((.{5,80})\)', r'@Test'],
    "kotlin": [r'assert\w+\((.{5,80})\)', r'assertEquals\((.{5,80})\)', r'shouldBe\s+(.{5,40})'],
    "rust": [r'assert!\((.{5,80})\)', r'assert_eq!\((.{5,80})\)', r'assert_ne!\((.{5,80})\)'],
    "csharp": [r'Assert\.\w+\((.{5,80})\)', r'\[Fact\]', r'\[Test\]'],
    "php": [r'\$this->assert\w+\((.{5,80})\)', r'@test'],
    "ruby": [r'expect\((.{5,80})\)', r'assert_equal\s+(.{5,80})', r'assert_raises\s*\((.{5,40})\)'],
    "swift": [r'XCTAssert\w*\((.{5,80})\)', r'XCTFail\((.{5,80})\)'],
    "scala": [r'assert\w*\((.{5,80})\)', r'should\w*\s+(.{5,40})'],
    "elixir": [r'assert\s+(.{5,80})', r'assert_raise\s+(.{5,40})', r'refute\s+(.{5,80})'],
    "lua": [r'assert\((.{5,80})\)', r'lu\.assert\w+\((.{5,80})\)'],
}


def extract_assertions(root: str, node: GraphNode, db_conn=None) -> list[str]:
    """v16: Extract assertion specs from test functions.

    Strategy:
    1. Try graph.db assertions table first (works for all languages, populated by gt-index v16+)
    2. For Python: fall back to ast.parse() for readable assertion expressions
    3. For other languages: fall back to regex patterns
    """
    # Path 1: graph.db assertions table (language-agnostic)
    if db_conn is not None and node.id:
        try:
            cursor = db_conn.execute(
                "SELECT kind, expression FROM assertions WHERE test_node_id = ? LIMIT 8",
                (node.id,),
            )
            rows = cursor.fetchall()
            if rows:
                return [row[1][:120] for row in rows if row[1]]
        except Exception:
            pass  # Table may not exist in older DBs

    # Path 2: Python AST (highest quality)
    if node.language == "python":
        return _extract_assertions_ast(root, node)

    # Path 3: regex fallback (all languages)
    return _extract_assertions_regex(root, node)


def _extract_assertions_ast(root: str, node: GraphNode) -> list[str]:
    """v20: AST-based assertion extraction for Python tests.

    Returns verbatim assertion expressions using ast.unparse().
    Includes setup-as-spec: walks back up to 3 lines for subject variable construction.
    """
    import ast as _ast

    source = read_lines(root, node.file_path, node.start_line, node.end_line)
    if not source.strip():
        return []
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        # Fallback to regex
        return _extract_assertions_regex(root, node)

    source_lines = source.splitlines()
    assertions: list[str] = []
    seen: set[str] = set()

    for stmt in _ast.walk(tree):
        # Plain assert statements: assert func(x) == y
        if isinstance(stmt, _ast.Assert) and stmt.test is not None:
            try:
                expr = _ast.unparse(stmt.test)
                if len(expr) > 120:
                    expr = expr[:117] + "..."
                if expr not in seen:
                    # Check for setup-as-spec: variable construction in preceding lines
                    setup = _find_setup_line(source_lines, getattr(stmt, "lineno", 0) - 1)
                    if setup:
                        assertions.append(f"setup: {setup}")
                    assertions.append(f"assert {expr}")
                    seen.add(expr)
            except Exception:
                pass

        # Method-style assertions: self.assertEqual(a, b)
        if isinstance(stmt, _ast.Call) and isinstance(stmt.func, _ast.Attribute):
            method = stmt.func.attr
            if not method.startswith("assert"):
                continue
            try:
                if method == "assertEqual" and len(stmt.args) >= 2:
                    lhs = _ast.unparse(stmt.args[0])[:60]
                    rhs = _ast.unparse(stmt.args[1])[:60]
                    spec = f"{lhs} == {rhs}"
                elif method == "assertRaises" and stmt.args:
                    exc = _ast.unparse(stmt.args[0])[:40]
                    spec = f"raises {exc}"
                elif method == "assertIn" and len(stmt.args) >= 2:
                    spec = f"{_ast.unparse(stmt.args[0])[:40]} in {_ast.unparse(stmt.args[1])[:40]}"
                elif method in ("assertTrue", "assertFalse") and stmt.args:
                    spec = f"{'not ' if method == 'assertFalse' else ''}{_ast.unparse(stmt.args[0])[:60]}"
                elif method == "assertNotEqual" and len(stmt.args) >= 2:
                    spec = f"{_ast.unparse(stmt.args[0])[:40]} != {_ast.unparse(stmt.args[1])[:40]}"
                elif method == "assertIsNone" and stmt.args:
                    spec = f"{_ast.unparse(stmt.args[0])[:60]} is None"
                elif method == "assertIsNotNone" and stmt.args:
                    spec = f"{_ast.unparse(stmt.args[0])[:60]} is not None"
                else:
                    args_str = ", ".join(_ast.unparse(a)[:30] for a in stmt.args[:3])
                    spec = f"{method}({args_str})"

                if len(spec) > 120:
                    spec = spec[:117] + "..."
                if spec not in seen:
                    setup = _find_setup_line(source_lines, getattr(stmt, "lineno", 0) - 1)
                    if setup:
                        assertions.append(f"setup: {setup}")
                    assertions.append(spec)
                    seen.add(spec)
            except Exception:
                pass

        # pytest.raises(ExcType)
        if (isinstance(stmt, _ast.Call) and isinstance(stmt.func, _ast.Attribute)
                and stmt.func.attr == "raises"
                and isinstance(getattr(stmt.func, "value", None), _ast.Name)
                and stmt.func.value.id == "pytest"
                and stmt.args):
            try:
                exc = _ast.unparse(stmt.args[0])[:40]
                spec = f"raises {exc}"
                if spec not in seen:
                    assertions.append(spec)
                    seen.add(spec)
            except Exception:
                pass

    return assertions[:8]  # v20: allow up to 8 (2 tests × ~4 assertions)


def _find_setup_line(source_lines: list[str], assertion_line_idx: int) -> str | None:
    """v20: Find setup-as-spec line preceding an assertion.

    Walks back up to 3 lines looking for variable construction (assignment with
    constructor call, .create(), .build(), etc.) that likely sets up the test subject.
    """
    for offset in range(1, 4):
        idx = assertion_line_idx - offset
        if idx < 0 or idx >= len(source_lines):
            continue
        line = source_lines[idx].strip()
        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue
        # Check for constructor/factory patterns
        if "=" in line and any(kw in line for kw in ("(", ".create(", ".build(", ".objects.")):
            if len(line) > 100:
                line = line[:97] + "..."
            return line
        # Stop walking if we hit something that's not setup
        break
    return None


def _extract_assertions_regex(root: str, node: GraphNode) -> list[str]:
    """Regex fallback for non-Python or unparseable test functions."""
    text = read_lines(root, node.file_path, node.start_line, node.end_line)
    _GENERIC_ASSERTION_PATTERNS = [r'assert\w*\s*\((.{5,80})\)', r'expect\((.{5,80})\)', r'assert\s+(.{5,80})']
    patterns = ASSERTION_PATTERNS.get(node.language, _GENERIC_ASSERTION_PATTERNS)
    assertions = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            a = m.group(0).strip()
            if len(a) > 120:
                a = a[:117] + "..."
            assertions.append(a)
    return assertions[:5]

# ── Pre-task briefing (v12) ─────────────────────────────────────────────────

_NOISE_WORDS = frozenset({
    "True", "False", "None", "self", "cls", "args", "kwargs", "return", "import",
    "from", "class", "def", "if", "else", "for", "while", "try", "except", "with",
    "as", "in", "not", "and", "or", "is", "the", "a", "an", "to", "of", "this",
    "that", "it", "be", "have", "do", "will", "should", "can", "may", "The",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "object", "type", "print", "len", "range", "open", "file", "pass", "raise",
    "break", "continue", "lambda", "yield", "global", "nonlocal", "del",
    # v13: expanded noise words
    "would", "could", "been", "each", "any", "all", "new", "old", "get", "doesn",
    "when", "into", "but", "was", "has", "are", "its", "were", "more",
    "than", "then", "also", "only", "same", "such", "like", "some", "use",
    "used", "using", "make", "made", "need", "needs", "see", "way", "work",
    "works", "working", "case", "cases", "note", "added", "fix", "fixed",
    "null", "undefined", "var", "let", "const", "func", "struct", "interface",
    "package", "module", "require", "export", "default", "static", "public",
    "private", "protected", "abstract", "final", "void", "string", "number",
    "boolean", "error", "Error", "nil", "fmt", "log",
})


def extract_identifiers_from_issue(issue_text: str) -> list[str]:
    """Parse issue text for function names, class names, file paths, error names.
    v13: widened extraction for better coverage."""
    identifiers: set[str] = set()

    # Backtick-quoted identifiers: `function_name`, `ClassName.method`
    identifiers.update(re.findall(r'`([a-zA-Z_][\w.]*)`', issue_text))

    # CamelCase words (likely class names, 2+ humps)
    identifiers.update(re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', issue_text))

    # v17: Single-hump CamelCase in code context only (backticks, after class/import)
    identifiers.update(re.findall(r'`([A-Z][a-z]{3,})`', issue_text))
    identifiers.update(re.findall(
        r'(?:class|import|isinstance|issubclass|type)\s*[\s(]+([A-Z][a-z]{3,})',
        issue_text, re.I))

    # File paths mentioned (v16: expanded to all supported languages)
    identifiers.update(re.findall(
        r'[\w/]+\.(?:py|go|js|ts|rs|java|rb|php|c|cpp|h|hpp|cs|kt|scala|swift|ex|exs|lua|ml|elm|jsx|tsx|mjs|cjs|groovy)\b',
        issue_text))

    # snake_case identifiers (2+ parts, likely function names)
    identifiers.update(re.findall(r'\b([a-z]+_[a-z_]+)\b', issue_text))

    # Error/Exception/Failure/Warning/Panic class names (v13: added Panic)
    identifiers.update(re.findall(r'\b(\w+(?:Error|Exception|Failure|Warning|Panic))\b', issue_text))

    # dotted references like module.function
    identifiers.update(re.findall(r'\b([a-zA-Z_]\w+\.[a-zA-Z_]\w+)\b', issue_text))

    # v13: Words after function/method/class keywords
    identifiers.update(re.findall(
        r'(?:function|method|class|module|package|func|def|struct|interface)\s+[`"]?(\w+)',
        issue_text, re.I))

    # v13: Code paths without extension (src/lib/pkg/internal/cmd/app prefixed)
    identifiers.update(re.findall(r'(?:src|lib|pkg|internal|cmd|app)/[\w/]+', issue_text))

    # v17: Python traceback file paths (File "django/db/backends/utils.py", line 73)
    identifiers.update(re.findall(r'File "([^"]+\.py)", line \d+', issue_text))

    # v17: Python traceback function names (..., in function_name)
    identifiers.update(re.findall(r', in (\w+)\s*$', issue_text, re.MULTILINE))

    # v16: Java/Kotlin stack traces (at com.foo.Bar.method(Bar.java:42))
    identifiers.update(re.findall(r'at\s+([\w.]+)\(([\w]+\.(?:java|kt)):(\d+)\)', issue_text))

    # v16: Go panic traces (goroutine N, file.go:line)
    identifiers.update(re.findall(r'([\w/]+\.go):(\d+)', issue_text))
    identifiers.update(re.findall(r'panic:\s+(.+?)$', issue_text, re.MULTILINE))

    # v16: Rust backtrace (at src/foo/bar.rs:42:10)
    identifiers.update(re.findall(r'at\s+([\w/]+\.rs):(\d+)', issue_text))

    # v16: JS/TS V8 stack trace (at Object.method (file.js:42:10))
    identifiers.update(re.findall(r'at\s+(?:\w+\.)?(\w+)\s+\(([\w/.]+\.[jt]sx?):(\d+)', issue_text))

    # v16: C# stack trace (at Namespace.Class.Method() in file.cs:line 42)
    identifiers.update(re.findall(r'at\s+([\w.]+)\(\)\s+in\s+([\w/\\]+\.cs):line\s+(\d+)', issue_text))

    # Filter noise
    filtered = []
    for ident in identifiers:
        # Skip noise words
        if ident in _NOISE_WORDS:
            continue
        # Skip very short identifiers (likely noise)
        if len(ident) < 3:
            continue
        # Skip pure file extensions
        if ident.startswith("."):
            continue
        filtered.append(ident)

    # Deduplicate preserving order, limit to 20
    seen: set[str] = set()
    result = []
    for ident in sorted(filtered, key=len, reverse=True):
        # For dotted refs, also extract the parts
        if "." in ident:
            parts = ident.split(".")
            for part in parts:
                if part not in seen and part not in _NOISE_WORDS and len(part) >= 3:
                    seen.add(part)
            if ident not in seen:
                seen.add(ident)
                result.append(ident)
        elif ident not in seen:
            seen.add(ident)
            result.append(ident)
        if len(result) >= 20:
            break

    return result


def _tokenize_text(text: str) -> set[str]:
    """Split text into lowercase tokens for module scoring. Language-agnostic."""
    tokens: set[str] = set()
    # Split on whitespace, punctuation, camelCase boundaries, snake_case
    raw = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)  # camelCase split
    for part in re.split(r'[\s/._\-:,;(){}[\]"\'`<>]+', raw.lower()):
        if len(part) >= 3 and part not in _NOISE_WORDS:
            tokens.add(part)
    return tokens


def _module_score(file_path: str, issue_tokens: set[str]) -> float:
    """Score how well a node's file path matches the issue context. 0.0-1.0."""
    if not issue_tokens or not file_path:
        return 0.0
    path_tokens = _tokenize_text(file_path)
    if not path_tokens:
        return 0.0
    overlap = len(issue_tokens & path_tokens)
    # Normalize by the smaller set to avoid penalizing long paths
    return min(1.0, overlap / max(1, min(len(issue_tokens), len(path_tokens))))


def _resolution_confidence(
    candidates: list[GraphNode], issue_tokens: set[str],
    conn: sqlite3.Connection,
) -> list[tuple[GraphNode, float, str]]:
    """Compute resolution confidence for each candidate. Returns (node, rc, tier).

    Resolution confidence is SEPARATE from edge confidence.
    Edge confidence = "is this call relationship real?"
    Resolution confidence = "is this the node the user means?"

    Weights: module_score(0.4) + name_quality(0.3) + ambiguity_penalty(0.2) + centrality(0.1)
    """
    if not candidates:
        return []

    ambiguity = len(candidates)
    ambiguity_score = {1: 1.0, 2: 0.7}.get(ambiguity, 0.4 if ambiguity <= 5 else 0.1)

    # Batch query caller counts for centrality
    ids = [c.id for c in candidates]
    max_callers = 1
    caller_counts: dict[int, int] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT target_id, COUNT(*) FROM edges WHERE target_id IN ({placeholders}) "
            f"AND type='CALLS' GROUP BY target_id", ids,
        ).fetchall()
        for row in rows:
            caller_counts[row[0]] = row[1]
            max_callers = max(max_callers, row[1])

    results: list[tuple[GraphNode, float, str]] = []
    for candidate in candidates:
        # Name quality: qualified_name match > exact name
        name_q = 0.8  # default: exact name match
        if candidate.qualified_name and any(
            candidate.qualified_name.lower().endswith(t) for t in issue_tokens if len(t) >= 4
        ):
            name_q = 1.0

        # Module score: file path overlap with issue
        mod_score = _module_score(candidate.file_path, issue_tokens)

        # Centrality: normalized log caller count
        cc = caller_counts.get(candidate.id, 0)
        centrality = min(1.0, (cc / max(1, max_callers)) if max_callers > 0 else 0.0)

        # Resolution confidence
        rc = 0.3 * name_q + 0.4 * mod_score + 0.2 * ambiguity_score + 0.1 * centrality

        # Determine tier
        tier = "abstain"
        if rc >= 0.85:
            tier = "verified"
        elif rc >= 0.6:
            tier = "likely"
        elif rc >= 0.4:
            tier = "possible"

        results.append((candidate, rc, tier))

    # Sort by rc descending
    results.sort(key=lambda x: -x[1])

    # Apply gap check: [VERIFIED] only if gap to #2 >= 0.15
    if len(results) >= 2 and results[0][2] == "verified":
        gap = results[0][1] - results[1][1]
        if gap < 0.15:
            results[0] = (results[0][0], results[0][1], "likely")

    return results


def resolve_briefing_targets(
    conn: sqlite3.Connection, identifiers: list[str], max_targets: int = 2,
) -> list[tuple[GraphNode, str]]:
    """v19: Resolve targets with disambiguation. Returns (node, tier) tuples.

    Uses module scoring + resolution confidence to avoid false-positive targeting.
    Abstains on ambiguous identifiers rather than guessing wrong.
    """
    cur = conn.cursor()
    targets: list[tuple[GraphNode, str]] = []
    issue_tokens = set()
    for ident in identifiers:
        issue_tokens |= _tokenize_text(ident)

    symbols_shown = 0
    for ident in identifiers:
        if symbols_shown >= max_targets:
            break
        if "/" in ident and "." in ident:
            continue
        search_name = ident.split(".")[-1] if "." in ident else ident

        # Retrieve ALL candidates (up to 50) instead of LIMIT 2
        rows = cur.execute("""
            SELECT * FROM nodes
            WHERE LOWER(name) = LOWER(?) AND is_test = 0
            LIMIT 50
        """, (search_name,)).fetchall()

        if not rows:
            continue

        candidates = [_row_to_node(r) for r in rows]

        if len(candidates) == 1:
            # Unambiguous — single match, always accept
            targets.append((candidates[0], "verified"))
            symbols_shown += 1
        else:
            # Ambiguous — use resolution confidence to disambiguate
            scored = _resolution_confidence(candidates, issue_tokens, conn)
            if scored and scored[0][2] != "abstain":
                targets.append((scored[0][0], scored[0][2]))
                symbols_shown += 1
            # else: abstain — skip this identifier entirely

    # v17 fallback: use file paths from tracebacks to find functions
    if not targets:
        file_idents = [i for i in identifiers if "/" in i and ("." in i or i.startswith("src/"))]
        for fident in file_idents[:3]:
            rows = cur.execute("""
                SELECT * FROM nodes
                WHERE file_path LIKE ? AND is_test = 0
                  AND label IN ('Function', 'Method')
                ORDER BY start_line ASC
                LIMIT 2
            """, (f"%{fident}%",)).fetchall()
            for row in rows:
                targets.append((_row_to_node(row), "likely"))
                if len(targets) >= max_targets:
                    break
            if targets:
                break

    # Qualified name fallback
    if not targets:
        for ident in identifiers:
            if len(ident) < 4:
                continue
            rows = cur.execute("""
                SELECT * FROM nodes
                WHERE qualified_name LIKE ? AND is_test = 0
                LIMIT 5
            """, (f"%{ident}%",)).fetchall()
            if rows:
                candidates = [_row_to_node(r) for r in rows]
                scored = _resolution_confidence(candidates, issue_tokens, conn)
                if scored and scored[0][2] != "abstain":
                    targets.append((scored[0][0], scored[0][2]))
                    break

    return targets[:max_targets]


def _briefing_line_for_node(node: EvidenceNode, target: GraphNode) -> str:
    """Single compact line for enhanced briefing."""
    if node.family == "CALLER":
        loc = f"{os.path.basename(node.file)}:{node.line}" if node.line else node.file
        return f"{node.name}() at {loc} — {node.summary}"
    if node.family == "IMPORT":
        return node.source_code or f"{node.name} from {node.file}"
    if node.family == "SIBLING":
        return f"{node.summary} (see {node.file})"
    if node.family == "TEST":
        if node.source_code:
            return f"{node.name} in {node.file}: {node.source_code.replace(chr(10), ' ')[:200]}"
        return f"{node.name} in {node.file} — {node.summary}"
    if node.family == "IMPACT":
        return node.summary
    if node.family == "TYPE":
        return f"MUST satisfy return contract: {node.summary}"
    if node.family == "PRECEDENT":
        return (node.summary or "")[:200]
    return node.summary


def generate_enhanced_briefing(
    conn: sqlite3.Connection, root: str, identifiers: list[str], max_lines: int = 8,
) -> str:
    """v19: Pre-exploration report with tiered confidence framing.

    Uses resolution confidence (module scoring + ambiguity detection) to determine
    whether to emit [VERIFIED] (directive), [LIKELY] (suggestion), or abstain.
    """
    target_tuples = resolve_briefing_targets(conn, identifiers, max_targets=2)
    if not target_tuples:
        return generate_pretask_briefing(conn, root, identifiers, max_lines=min(8, max_lines))

    lines: list[str] = []

    for target, tier in target_tuples:
        if len(lines) >= max_lines - 2:
            break

        loc = f"{target.file_path}:{target.start_line}" if target.start_line else target.file_path
        sig = (target.signature or target.name or "")[:100]
        qn = target.qualified_name or target.name

        # v19: Tiered framing based on resolution confidence
        if tier == "verified":
            lines.append(f"[VERIFIED] FIX HERE: {qn}() at {loc} (1.00)")
        elif tier == "likely":
            lines.append(f"[LIKELY] Relevant: {qn}() at {loc}")
        else:  # "possible"
            lines.append(f"[POSSIBLE] Consider: {qn}() at {loc}")

        if sig:
            lines.append(f"  signature: {sig}")

        candidates = compute_evidence(conn, root, target)
        selected = rank_and_select(candidates, max_high=3, max_low=0)
        high = [n for n in selected if n.score >= 2]
        low = [n for n in selected if n.score == 1]

        if high and len(lines) < max_lines:
            for n in high:
                if len(lines) >= max_lines:
                    break
                conf = f"{n.score / 3:.2f}"
                lines.append(f"  [VERIFIED] {_briefing_line_for_node(n, target)} ({conf})")

        if low and len(lines) < max_lines:
            for n in low:
                if len(lines) >= max_lines:
                    break
                conf = f"{n.score / 3:.2f}"
                lines.append(f"  [WARNING] {_briefing_line_for_node(n, target)} ({conf})")

    # Emit structured target metadata for hook consumption (P0.4)
    # This allows hooks to consume targets without regex-scraping display text
    import json as _json
    target_meta = []
    for target, tier in target_tuples:
        target_meta.append({
            "symbol": target.qualified_name or target.name,
            "file": target.file_path,
            "line": target.start_line,
            "tier": tier,
        })
    if target_meta:
        lines.append(f"<!-- GT_TARGETS:{_json.dumps(target_meta)} -->")

    return format_gt_output(
        lines[:max_lines + 1],  # +1 for metadata line
        fallback_ok="No codebase context found.",
    )


def generate_pretask_briefing(
    conn: sqlite3.Connection, root: str, identifiers: list[str], max_lines: int = 5,
) -> str:
    """v14: Query graph.db for matching symbols. Returns max 5-line directive briefing."""
    cur = conn.cursor()
    bullets: list[str] = []
    found_symbols: list[str] = []
    symbols_shown = 0

    # Build list of admissible resolution methods for queries
    res_methods = ",".join(f"'{r}'" for r in VERIFIED_RESOLUTIONS)

    for ident in identifiers:
        if symbols_shown >= 2:
            break

        # Skip file paths
        if "/" in ident and "." in ident:
            continue

        search_name = ident.split(".")[-1] if "." in ident else ident

        rows = cur.execute("""
            SELECT id, label, name, qualified_name, file_path, start_line
            FROM nodes
            WHERE LOWER(name) = LOWER(?) AND is_test = 0
            LIMIT 2
        """, (search_name,)).fetchall()

        for row in rows:
            if symbols_shown >= 2:
                break
            node_id, label, name, qname, fpath, sline = row
            found_symbols.append(name)
            symbols_shown += 1

            # FIX HERE line
            loc = f"{fpath}:{sline}" if sline else fpath
            bullets.append(f"FIX HERE: {qname or name}() \u2192 {loc}")

            # Top caller
            caller = cur.execute(f"""
                SELECT n.name, n.file_path
                FROM edges e JOIN nodes n ON e.source_id = n.id
                WHERE e.target_id = ? AND e.type = 'CALLS'
                  AND e.resolution_method IN ({res_methods}) AND n.is_test = 0
                LIMIT 1
            """, (node_id,)).fetchone()
            if caller:
                bullets.append(f"CALLERS: {caller[0]}() expects return value")

            # Test
            test = cur.execute(f"""
                SELECT n.name, n.file_path
                FROM edges e JOIN nodes n ON e.source_id = n.id
                WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
                  AND e.resolution_method IN ({res_methods})
                LIMIT 1
            """, (node_id,)).fetchone()
            if test:
                bullets.append(f"TEST: {test[1]}::{test[0]}")

    # v17 fallback: use file paths from tracebacks to find functions in those files
    if not found_symbols:
        file_idents = [i for i in identifiers if "/" in i and ("." in i or i.startswith("src/"))]
        for fident in file_idents[:3]:
            rows = cur.execute("""
                SELECT id, label, name, qualified_name, file_path, start_line
                FROM nodes
                WHERE file_path LIKE ? AND is_test = 0
                  AND label IN ('Function', 'Method')
                ORDER BY start_line ASC
                LIMIT 3
            """, (f"%{fident}%",)).fetchall()
            for row in rows:
                node_id, label, name, qname, fpath, sline = row
                found_symbols.append(name)
                loc = f"{fpath}:{sline}" if sline else fpath
                bullets.append(f"FIX HERE: {qname or name}() → {loc}")
                if len(bullets) >= 2:
                    break
            if found_symbols:
                break

    # v14 fallback 1: substring match for identifiers >= 4 chars
    if not found_symbols:
        for ident in identifiers:
            if len(ident) < 4:
                continue
            rows = cur.execute("""
                SELECT id, label, name, qualified_name, file_path, start_line
                FROM nodes
                WHERE qualified_name LIKE ? AND is_test = 0
                LIMIT 2
            """, (f'%{ident}%',)).fetchall()
            for row in rows:
                node_id, label, name, qname, fpath, sline = row
                found_symbols.append(name)
                loc = f"{fpath}:{sline}" if sline else fpath
                bullets.append(f"FIX HERE: {qname or name}() \u2192 {loc}")
                if len(bullets) >= 2:
                    break
            if found_symbols:
                break

    # v14 fallback 2: top entry points by caller count
    if not found_symbols:
        top_nodes = cur.execute(f"""
            SELECT n.name, n.qualified_name, n.file_path, n.start_line,
                   COUNT(e.source_id) as caller_count
            FROM nodes n
            JOIN edges e ON e.target_id = n.id
            WHERE e.type = 'CALLS' AND e.resolution_method IN ({res_methods})
              AND n.label IN ('Function','Method') AND n.is_test = 0
              AND n.file_path NOT LIKE '%test%'
            GROUP BY n.id
            ORDER BY caller_count DESC
            LIMIT 3
        """).fetchall()
        for name, qname, fpath, sline, cnt in top_nodes:
            found_symbols.append(name)
            loc = f"{fpath}:{sline}" if sline else fpath
            bullets.append(f"ENTRY POINT: {qname or name}() \u2192 {loc} ({cnt} callers)")

    if not bullets:
        return format_gt_output([], fallback_ok="No symbols matched in graph.")

    lines = ["\u26a0\ufe0f CODEBASE CONTEXT:"]
    for b in bullets[:max_lines - 1]:
        lines.append(f"\u2022 {b}")
    return format_gt_output(lines)


# ── Git precedent (v12) ────────────────────────────────────────────────────

def get_git_precedent(root: str, file_path: str, start_line: int, end_line: int) -> str | None:
    """Find the last commit that touched lines near this function. Returns formatted block or None."""
    try:
        # Get recent commits for this file
        result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--follow", "--", file_path],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        commits = result.stdout.strip().split("\n")

        for commit_line in commits[:3]:
            commit_hash = commit_line.split()[0]

            # Get the diff for this commit on this file
            diff_result = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}", "--", file_path],
                cwd=root, capture_output=True, text=True, timeout=5,
            )
            if diff_result.returncode != 0 or not diff_result.stdout:
                continue

            # Check if diff touches our function's line range
            diff_lines = diff_result.stdout.split("\n")
            touches_function = False
            relevant_hunks: list[str] = []

            for line in diff_lines:
                if line.startswith("@@"):
                    match = re.search(r"\+(\d+)", line)
                    if match:
                        hunk_start = int(match.group(1))
                        if start_line - 10 <= hunk_start <= end_line + 10:
                            touches_function = True

                if touches_function and (line.startswith("+") or line.startswith("-")):
                    if not line.startswith("+++") and not line.startswith("---"):
                        relevant_hunks.append(line[:100])

            if touches_function and relevant_hunks:
                commit_msg = " ".join(commit_line.split()[1:])
                short_hash = commit_hash[:7]
                lines = [f"commit: {commit_msg[:70]} ({short_hash})"]
                # v20: normalize before/after labels instead of raw +/- prefixes
                for hunk in relevant_hunks[:4]:
                    stripped = hunk[1:].strip()  # remove +/- prefix
                    if not stripped:
                        continue
                    if hunk.startswith("-"):
                        lines.append(f"  before: {stripped[:100]}")
                    elif hunk.startswith("+"):
                        lines.append(f"  after:  {stripped[:100]}")
                return "\n".join(lines)

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# ── Evidence computation ────────────────────────────────────────────────────

def get_callees(conn: sqlite3.Connection, target_id: int) -> list[GraphNode]:
    """Get functions that the target calls (outgoing CALLS edges)."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    conf_clause = _confidence_clause(_has_confidence_column(conn))
    cur.execute(f"""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.target_id
        WHERE e.source_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({ph}){conf_clause}
        LIMIT 10
    """, (target_id, *methods))
    return [_row_to_node(r) for r in cur.fetchall()]


def compute_evidence(conn: sqlite3.Connection, root: str, target: GraphNode) -> list[EvidenceNode]:
    """Compute ranked evidence for a target function.

    7 families (all preserved, no filtering):
      IMPORT: correct import paths for cross-file callees
      CALLER: cross-file callers with usage classification
      SIBLING: behavioral norms from sibling methods
      TEST: test functions with assertions
      IMPACT: blast radius (caller count + critical path)
      TYPE: return type contract
      PRECEDENT: last git commit
    """
    # SUBSTRATE_SHIM: delegate to substrate if available
    try:
        from groundtruth.substrate.adapter import try_substrate_evidence
        _db_row = conn.execute("PRAGMA database_list").fetchone()
        _db_path = _db_row[2] if _db_row else ""
        substrate_result = try_substrate_evidence(
            db_path=_db_path,
            target_name=target.name,
            target_file=target.file_path,
            root=root,
        )
        if substrate_result is not None:
            return [EvidenceNode(**r) for r in substrate_result]
    except ImportError:
        pass  # Substrate not installed — expected in bare containers
    except TypeError as _te:
        import sys as _sys
        print(f"[GT_TELEMETRY] substrate_schema_mismatch: {_te}", file=_sys.stderr)
    except Exception as _exc:
        import sys as _sys2
        print(f"[GT_TELEMETRY] substrate_failed: {_exc}", file=_sys2.stderr)

    def _format_import_for_language(callee: GraphNode, language: str) -> str:
        """Generate language-appropriate import statement."""
        path = callee.file_path
        name = callee.name
        if not name:
            return ""
        if language == "python":
            mod = path.replace("/", ".").replace("\\", ".")
            if mod.endswith(".py"):
                mod = mod[:-3]
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            return f"from {mod} import {name}"
        elif language == "go":
            pkg = os.path.dirname(path)
            return f'import "{pkg}"  // {name}'
        elif language in ("javascript", "typescript"):
            mod = os.path.splitext(path)[0]
            return f"import {{ {name} }} from './{mod}'"
        elif language in ("java", "kotlin"):
            mod = os.path.splitext(path)[0].replace("/", ".")
            return f"import {mod}.{name};"
        elif language == "rust":
            mod = os.path.splitext(path)[0].replace("/", "::")
            return f"use {mod}::{name};"
        elif language == "csharp":
            ns = os.path.dirname(path).replace("/", ".")
            return f"using {ns};  // {name}"
        elif language == "ruby":
            mod = os.path.splitext(path)[0]
            return f"require '{mod}'  # {name}"
        elif language == "php":
            ns = os.path.splitext(path)[0].replace("/", "\\")
            return f"use {ns}\\{name};"
        else:
            return f"{name} (from {path})"

    candidates: list[EvidenceNode] = []

    # Family 0: IMPORT — correct import paths for callees
    # This is the #1 hallucination prevention signal
    callees = get_callees(conn, target.id)
    seen_imports = set()
    for callee in callees:
        if callee.file_path == target.file_path:
            continue  # same file, no import needed
        import_stmt = _format_import_for_language(callee, target.language)
        key = (callee.name, callee.file_path)
        if key in seen_imports:
            continue
        seen_imports.add(key)
        sig = callee.signature if callee.signature else callee.name
        candidates.append(EvidenceNode(
            family="IMPORT", score=2,
            name=callee.name, file=callee.file_path, line=callee.start_line,
            source_code=import_stmt,
            summary=f"signature: {sig[:80]}",
        ))

    # Family 1: CALLER — cross-file callers with usage classification
    # v13: get_callers() already filters to admissible edges only (same_file, import)
    callers = get_callers(conn, target.id, target.file_path)
    for caller_node, call_line, source_file, resolution_method in callers:
        score, summary, call_text = classify_caller_usage(root, source_file, call_line)
        if score >= 1:
            # v20: use actual call line as source_code instead of 3-line window
            code = call_text if call_text else read_lines(root, source_file, max(1, call_line - 1), call_line + 2)
            candidates.append(EvidenceNode(
                family="CALLER", score=score,
                name=caller_node.name, file=source_file, line=call_line,
                source_code=code, summary=summary,
            ))

    # Family 2: SIBLING — behavioral consistency norms from same class
    # v6: Specific pattern extraction, not just counts. This is the proven flip family.
    siblings = get_siblings(conn, target.id)
    if len(siblings) >= 2:
        # Analyze specific patterns across siblings
        sib_patterns = []

        # Pattern 1: Return type consistency
        ret_types = [s.return_type for s in siblings if s.return_type]
        if ret_types:
            common_rt = Counter(ret_types).most_common(1)[0]
            rt_fraction = common_rt[1] / len(siblings)
            if rt_fraction >= 0.7:
                if target.return_type and target.return_type != common_rt[0]:
                    sib_patterns.append((3, f"INCONSISTENCY: {common_rt[1]}/{len(siblings)} siblings return {common_rt[0]}, but {target.name} returns {target.return_type}"))
                else:
                    sib_patterns.append((2, f"{common_rt[1]}/{len(siblings)} siblings return {common_rt[0]} — preserve this convention"))

        # Pattern 2: Signature shape (param count) consistency
        sib_param_counts = []
        for s in siblings:
            if s.signature:
                params = s.signature.split("(", 1)[-1].rstrip(")")
                count = len([p for p in params.split(",") if p.strip() and p.strip() != "self"]) if params.strip() else 0
                sib_param_counts.append(count)
        if sib_param_counts:
            common_pc = Counter(sib_param_counts).most_common(1)[0]
            if common_pc[1] / len(siblings) >= 0.8:
                target_params = 0
                if target.signature:
                    tp = target.signature.split("(", 1)[-1].rstrip(")")
                    target_params = len([p for p in tp.split(",") if p.strip() and p.strip() != "self"]) if tp.strip() else 0
                if target_params != common_pc[0]:
                    sib_patterns.append((2, f"{common_pc[1]}/{len(siblings)} siblings take {common_pc[0]} params, {target.name} takes {target_params}"))

        # Pattern 3: Exception raising pattern
        sib_exceptions = []
        for s in siblings:
            exc_props = conn.execute(
                "SELECT value FROM properties WHERE node_id = ? AND kind = 'exception_type'",
                (s.id,)
            ).fetchall()
            sib_exceptions.extend(r[0] for r in exc_props)
        if sib_exceptions:
            common_exc = Counter(sib_exceptions).most_common(1)[0]
            if common_exc[1] >= 2:
                sib_patterns.append((2, f"{common_exc[1]} siblings raise {common_exc[0]} — {target.name} should follow this pattern"))

        # Emit the strongest pattern, or fall back to generic count
        if sib_patterns:
            sib_patterns.sort(key=lambda x: -x[0])
            best_score, best_summary = sib_patterns[0]
            candidates.append(EvidenceNode(
                family="SIBLING", score=best_score,
                name=target.name, file=target.file_path, line=target.start_line,
                source_code="", summary=best_summary,
            ))
            # Add second pattern if available and different
            if len(sib_patterns) > 1:
                candidates.append(EvidenceNode(
                    family="SIBLING", score=sib_patterns[1][0],
                    name=target.name, file=target.file_path, line=target.start_line,
                    source_code="", summary=sib_patterns[1][1],
                ))
        else:
            # Fallback: generic sibling count (lower score)
            candidates.append(EvidenceNode(
                family="SIBLING", score=1,
                name=target.name, file=target.file_path, line=target.start_line,
                source_code="",
                summary=f"sibling method in same class ({len(siblings)} total)",
            ))

    # Family 3: TEST — test functions with assertions
    tests = get_tests(conn, target.id)
    for test_node in tests:
        assertions = extract_assertions(root, test_node)
        if assertions:
            code = "\n".join(assertions[:3])
            candidates.append(EvidenceNode(
                family="TEST", score=2,
                name=test_node.name, file=test_node.file_path, line=test_node.start_line,
                source_code=code, summary=f"{len(assertions)} assertions",
            ))
        else:
            # Even without extractable assertions, knowing the test file is valuable
            candidates.append(EvidenceNode(
                family="TEST", score=1,
                name=test_node.name, file=test_node.file_path, line=test_node.start_line,
                source_code="", summary=f"test function references {target.name}",
            ))

    # Family 4: IMPACT — blast radius (lowered threshold from 5 to 2)
    total_callers, unique_files = get_all_callers_count(conn, target.id)
    critical = is_critical_path(target.file_path)
    if total_callers >= 2 or critical:
        candidates.append(EvidenceNode(
            # v1.0.4: only critical-path IMPACT scores high; generic caller count is context
            family="IMPACT", score=2 if critical else 1,
            name=target.name, file=target.file_path, line=0,
            source_code="",
            summary=f"{total_callers} callers in {unique_files} files" +
                    (" — CRITICAL PATH" if critical else ""),
        ))

    # Family 5: TYPE — return type from annotation or signature
    if target.return_type:
        score = 1
        if any(c.score >= 2 and "destruct" in c.summary for c in candidates if c.family == "CALLER"):
            score = 2
        candidates.append(EvidenceNode(
            family="TYPE", score=score,
            name=target.name, file=target.file_path, line=target.start_line,
            source_code="", summary=f"returns {target.return_type}",
        ))

    # Family 6: PRECEDENT — last git commit touching this function (v12)
    # v1.0.4: filter formatting-only precedents (black, whitespace changes)
    precedent = get_git_precedent(root, target.file_path, target.start_line, target.end_line)
    if precedent:
        _prec_lower = precedent.lower()
        _is_formatting_only = any(kw in _prec_lower for kw in (
            "[black]", "black format", "whitespace", "pep8", "autopep8",
            "isort", "ruff format", "yapf", "pyink",
        ))
        if not _is_formatting_only:
            candidates.append(EvidenceNode(
                family="PRECEDENT", score=1,  # v1.0.4: context-only, not constraint
                name=target.name, file=target.file_path, line=target.start_line,
                source_code="", summary=precedent,
            ))

    # Family 7: OBLIGATION — behavioral contracts from callers (v1.0.4)
    # Bootstrap sys.path so groundtruth_v2 is importable inside containers.
    # The package may be at: /tmp/groundtruth_v2/, /root/tools/groundtruth/bin/,
    # or alongside this script.
    _obligation_ok = False
    try:
        from groundtruth.contracts.extractors.obligation_extractor import ObligationExtractor

        obligations = ObligationExtractor().extract(_ConnGraphReader(conn), target.id)
        for ob in obligations[:3]:
            candidates.append(EvidenceNode(
                family="OBLIGATION", score=3 if ob.tier == "verified" else 2,
                name=target.name, file=target.file_path, line=target.start_line,
                source_code="", summary=ob.predicate,
            ))
        _obligation_ok = len(obligations) > 0
    except ImportError as e:
        _log_gt_telemetry(target.file_path, "obligation_import_failed", str(e))
    except Exception as e:
        _log_gt_telemetry(target.file_path, "obligation_error", str(e))

    # Family 8: NEGATIVE — disproval signals (v1.0.4)
    # Fires only on post-edit (when target file has been modified)
    try:
        # Check callees of the target: do they still exist?
        callees = get_callees(conn, target.id)
        for callee in callees:
            # Symbol not exported but called from another file
            if not callee.is_exported and callee.file_path != target.file_path:
                candidates.append(EvidenceNode(
                    family="NEGATIVE", score=3,
                    name=callee.name, file=callee.file_path, line=callee.start_line,
                    source_code="",
                    summary=f"NOT EXPORTED: {callee.name} in {callee.file_path} is not exported",
                ))
    except Exception:
        pass  # Graceful degradation

    return candidates


# ── Ranking + selection ─────────────────────────────────────────────────────

def _estimate_tokens(node: EvidenceNode) -> int:
    """Rough token estimate for an evidence node (1 token ≈ 4 chars)."""
    text = f"{node.family} {node.name} {node.summary} {node.source_code}"
    return max(5, len(text) // 4)


#
# Per-family score floors (2026 research-grounded confidence gating).
# Families with high precision in test-free debugging get lower floors
# (admitted more readily); families with weak signal get stricter floors
# (suppressed unless evidence is strong).
#
# Ranking basis (test-free real-world debugging, 2026):
#   CALLER (runtime usage)   — TraceCoder 2602.06875, DAIRA 2603.22048
#   IMPORT (verified dep)    — deterministic, cannot be wrong
#   OBLIGATION (contract)    — Sepidband 2604.05481 predicate extraction
#   NEGATIVE (structural)    — deterministic
#   PRECEDENT (fix history)  — AgentSZZ 2604.02665 (temporal > blame)
#   IMPACT (blast radius)    — supporting
#   TYPE (annotation)        — supporting
#   SIBLING (class norm)     — weaker without tests
#   TEST (assertions)        — lowest in test-free scenarios
#
FAMILY_FLOOR: dict[str, int] = {
    "CALLER": 1,     # high-precision usage fact — admit on presence
    "IMPORT": 1,     # verified dependency — admit on presence
    "OBLIGATION": 2, # predicate must be strongly supported
    "NEGATIVE": 2,
    "CRITIQUE": 2,
    "IMPACT": 1,
    "TYPE": 1,
    "PRECEDENT": 2,  # git-blame-class signal — only when concrete
    "SIBLING": 2,    # behavioral norm — only when pattern is strong
    "TEST": 2,       # test-free default: suppress weak test evidence
}


def rank_and_select(
    candidates: list[EvidenceNode],
    max_high: int = 4,
    max_low: int = 2,
    token_budget: int = 450,
) -> list[EvidenceNode]:
    """Token-budgeted knapsack selection with per-family confidence gating.

    Gating (2026 research):
      - Each family has a score floor from FAMILY_FLOOR; nodes below are dropped.
      - Structural families (NEGATIVE/OBLIGATION/CRITIQUE) rank ahead.
      - Within same score, CALLER/IMPORT/PRECEDENT rank ahead of TYPE/SIBLING/TEST
        (test-free signal strength).
    """
    # Boost negative/explicit constraint evidence (constraint violations signal).
    for c in candidates:
        if c.family == "TEST" and any(kw in c.summary.lower() for kw in ("raises", "error", "exception", "false", "not")):
            c.score = max(c.score, 3)
        if c.family == "OBLIGATION" and any(kw in c.summary.lower() for kw in ("must remain", "must continue", "must be")):
            c.score = max(c.score, 3)

    # Per-family confidence gate — drop nodes below family floor.
    gated = [c for c in candidates if c.score >= FAMILY_FLOOR.get(c.family, 1)]

    # Sort: score DESC, then structural > contextual, then 2026 family rank.
    _STRUCTURAL = {"NEGATIVE", "OBLIGATION", "CRITIQUE"}
    family_priority = {
        "NEGATIVE": 0, "OBLIGATION": 1, "CRITIQUE": 2,
        "CALLER": 3, "IMPORT": 4, "PRECEDENT": 5,
        "IMPACT": 6, "TYPE": 7, "SIBLING": 8, "TEST": 9,
    }
    gated.sort(key=lambda c: (-c.score, 0 if c.family in _STRUCTURAL else 1, family_priority.get(c.family, 10)))

    selected: list[EvidenceNode] = []
    family_counts: dict[str, int] = {}
    tokens_used = 0

    # Per-family caps (2026 rank: usage facts + structural get more slots).
    family_max = {
        "NEGATIVE": 2, "OBLIGATION": 2, "CRITIQUE": 2,
        "CALLER": 3, "IMPORT": 2, "PRECEDENT": 1,
        "IMPACT": 1, "TYPE": 1, "SIBLING": 1, "TEST": 2,
    }

    for c in gated:
        fam_count = family_counts.get(c.family, 0)
        fam_cap = family_max.get(c.family, 1)
        if fam_count >= fam_cap:
            continue
        est = _estimate_tokens(c)
        if tokens_used + est > token_budget and selected:
            continue  # skip if over budget (but always include at least 1)
        selected.append(c)
        family_counts[c.family] = fam_count + 1
        tokens_used += est

    return selected

# ── Evidence logging ───────────────────────────────────────────────────────

def log_evidence(
    candidates: list[EvidenceNode],
    selected: list[EvidenceNode],
    target: GraphNode,
    log_path: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Write comprehensive evidence log as JSON for post-run analysis.
    v13: includes admissibility breakdown."""
    # v13: query edge resolution method distribution for this target
    edge_counts: dict[str, int] = {"same_file": 0, "import": 0, "name_match": 0}
    if conn is not None:
        try:
            cur = conn.cursor()
            rows = cur.execute("""
                SELECT resolution_method, COUNT(*) FROM edges
                WHERE (target_id = ? OR source_id = ?) AND type = 'CALLS'
                GROUP BY resolution_method
            """, (target.id, target.id)).fetchall()
            for method, count in rows:
                if method:
                    edge_counts[method] = edge_counts.get(method, 0) + count
        except Exception:
            pass

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": "v15",
        "target": {"name": target.name, "file": target.file_path, "line": target.start_line},
        "candidates": [
            {"family": c.family, "score": c.score, "name": c.name,
             "file": c.file, "line": c.line, "summary": c.summary}
            for c in candidates
        ],
        "selected": [
            {"family": c.family, "score": c.score, "name": c.name,
             "file": c.file, "summary": c.summary}
            for c in selected
        ],
        "post_edit_evidence_shown": len(selected) > 0,
        "post_edit_families_shown": sorted(set(c.family for c in selected)),
        "post_edit_suppressed": len(selected) == 0 and len(candidates) > 0,
        "v15_admissibility": {
            "edges_same_file": edge_counts.get("same_file", 0),
            "edges_import": edge_counts.get("import", 0),
            "edges_name_match": edge_counts.get("name_match", 0),
            "admissible_candidates": len(candidates),
            "output_gate_passed": len(selected) >= 1,
            "name_match_allowed": "name_match" in VERIFIED_RESOLUTIONS,
        },
    }

    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # never fail the main pipeline for logging


# ── Output formatting ───────────────────────────────────────────────────────

def _evidence_constraint_bullet(node: EvidenceNode, target: GraphNode) -> str:
    """One diagnostic bullet for post-edit / tiered output.

    Diagnostic framing (no force/MUST/DO NOT) per 2026 research
    (SWE-PRM 2509.02360, TraceCoder 2602.06875): factual observation
    leaves the fix decision with the agent.
    """
    if node.family == "CALLER":
        loc = f"{os.path.basename(node.file)}:{node.line}" if node.line else node.file
        return f"caller {node.name}() at {loc}: {node.summary}"
    if node.family == "IMPORT":
        return f"import path: {node.source_code}" if node.source_code else f"import source: {node.name} from {node.file}"
    if node.family == "SIBLING":
        return f"sibling pattern: {node.summary}"
    if node.family == "TEST":
        if node.source_code:
            return f"test {node.name} in {node.file}: {node.source_code[:120]}"
        return f"test {node.name} in {node.file}"
    if node.family == "IMPACT":
        return f"blast radius: {node.summary}"
    if node.family == "TYPE":
        return f"return type: {target.return_type or node.summary}"
    if node.family == "PRECEDENT":
        return f"last commit: {node.summary}"
    if node.family == "OBLIGATION":
        return f"observed contract: {node.summary}"
    if node.family == "NEGATIVE":
        return f"structural signal: {node.summary}"
    if node.family == "CRITIQUE":
        return f"breaking change: {node.summary}"
    return node.summary


def format_output(selected: list[EvidenceNode], target: GraphNode, root: str) -> str:
    """Tiered: high-confidence (score>=2) then additional context (score==1)."""
    def _full_block(node: EvidenceNode) -> list[str]:
        loc = f"{node.file}:{node.line}" if node.line else node.file
        block = [f"[{node.family}] {node.name} @ {loc}"]
        if node.summary:
            block.append(f"  -> {node.summary}")
        if node.source_code:
            for code_line in node.source_code.split("\n")[:8]:
                block.append(f"  {code_line}")
        return block

    high = [n for n in selected if n.score >= 2]
    low = [n for n in selected if n.score == 1]
    lines: list[str] = []

    target_code = read_lines(root, target.file_path, target.start_line, min(target.end_line, target.start_line + 5))
    lines.append(f"[VERIFIED] TARGET: {target.name} ({target.file_path}:{target.start_line}) (1.00)")
    if target_code:
        for code_line in target_code.split("\n")[:5]:
            lines.append(f"  {code_line}")

    if high:
        for node in high[:4]:
            lines.extend(_full_block(node))
    if low:
        for node in low[:2]:
            lines.extend(_full_block(node))

    while lines and not lines[-1].strip():
        lines.pop()
    return format_gt_output(lines)


def _score_to_tier(node: EvidenceNode) -> str:
    """Map evidence score to tier tag.

    Uses edge_confidence if available (v14+ indexer), otherwise falls back
    to score-based tiers for backward compatibility.
    """
    edge_conf = getattr(node, "edge_confidence", None)
    if edge_conf is not None and isinstance(edge_conf, (int, float)):
        if edge_conf >= 0.9:
            return "VERIFIED"
        if edge_conf >= 0.5:
            return "WARNING"
        return "INFO"
    # Fallback for old graph.db without confidence column
    if node.score >= 2:
        return "VERIFIED"
    if node.score >= 1:
        return "WARNING"
    return "INFO"


def format_gt_output(
    lines: list[str],
    *,
    staleness_warning: str | None = None,
    fallback_ok: str = "No high-confidence findings.",
) -> str:
    """Single formatting gate. All gt_intel output paths go through here.

    Guarantees: <gt-evidence> wrapper always present, never returns "".
    """
    header: list[str] = []
    if staleness_warning:
        header.append(f"[STALE] {staleness_warning}")
    if not lines:
        body = "\n".join(header + [f"[OK] {fallback_ok}"])
    else:
        body = "\n".join(header + lines)
    return f"<gt-evidence>\n{body}\n</gt-evidence>"


def format_reminder(
    selected: list[EvidenceNode], target: GraphNode,
    staleness_warning: str | None = None,
) -> str:
    """Post-edit reinforcement with <gt-evidence> wrapper and tier tags."""
    lines: list[str] = []
    for node in selected[:3]:
        tier = _score_to_tier(node)
        bullet = _evidence_constraint_bullet(node, target)[:240]
        conf = f"{node.score / 3:.2f}"  # normalize score 0-3 to 0.0-1.0
        lines.append(f"[{tier}] {bullet} ({conf})")
    return format_gt_output(
        lines,
        staleness_warning=staleness_warning,
        fallback_ok="No high-confidence findings for this edit.",
    )

# ── vNext: Nullability contract check ───────────────────────────────────────

def _check_nullability_contract(
    conn: sqlite3.Connection,
    node_id: int,
    diff_text: str,
) -> str | None:
    """Check whether a diff violates a non-None return contract.

    Fires when ALL three conditions hold:
      1. The node has a properties row: kind='return_shape', value='value'
         (produced by gt-index for any language — not Python-specific)
      2. The node has at least one properties row: kind='caller_usage'
         (any value — records that callers depend on the return value)
      3. The diff_text contains 'return None' (the violating change)

    Returns a warning string on violation, None otherwise.
    This function is fully generic — it queries graph.db by node_id only.
    """
    if "return None" not in diff_text:
        return None

    # Condition 1: return_shape = 'value'
    row_shape = conn.execute(
        "SELECT 1 FROM properties WHERE node_id = ? AND kind = 'return_shape' AND value = 'value' LIMIT 1",
        (node_id,),
    ).fetchone()
    if not row_shape:
        return None

    # Condition 2: caller_usage exists (any value)
    row_usage = conn.execute(
        "SELECT 1 FROM properties WHERE node_id = ? AND kind = 'caller_usage' LIMIT 1",
        (node_id,),
    ).fetchone()
    if not row_usage:
        return None

    return "[CONTRACT] Must not return None — callers depend on a non-None return value"


def _get_diff_text(root: str, file_path: str) -> str:
    """Return the current git diff for a single file, or empty string on failure.

    Called when no explicit diff text is provided. Uses git diff HEAD so it
    captures staged and unstaged changes against the last commit.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", file_path],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout or ""
    except Exception:
        return ""


# ── Briefing persistence (v-hybrid: /tmp/gt_briefing.txt for SWE-agent templating) ──

# Hard ceilings from the RWRR compiled swing plan (Codebase-Memory 10%-token target).
_BRIEFING_MAX_TOKENS = 500
_BRIEFING_MAX_LINES = 40
_BRIEFING_MAX_SYMBOLS = 20


def _count_tokens(text: str) -> int:
    """Approximate token count. Uses tiktoken cl100k_base when available; falls
    back to whitespace split / 0.75 otherwise (DS V3.2's tokenizer is not published)."""
    try:
        import tiktoken  # type: ignore[import-not-found]
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Graceful fallback: 1 token ≈ 0.75 whitespace-words for English.
        words = len(text.split())
        return int(round(words / 0.75)) if words else 0


def _count_briefing_symbols(text: str) -> int:
    """Count distinct symbol-looking tokens emitted by the briefing.
    Looks for path:line tokens, which every VERIFIED/LIKELY/POSSIBLE line has."""
    return len(re.findall(r"[\w./\\-]+\.[a-z]{1,5}:\d+", text))


def _persist_briefing(
    briefing_text: str,
    identifiers: list[str],
    write_path: str,
    meta_path: str,
) -> None:
    """Write briefing to a file (idempotent) and, optionally, a JSON meta sidecar.

    Empty briefings are NOT persisted — never inject a generic stub (non-negotiable).
    """
    if not write_path:
        return
    if not briefing_text or not briefing_text.strip():
        return

    # Enforce hard ceilings by line count; token trimming is handled upstream
    # by fold/preview/full hierarchy, but we still truncate as a safety net.
    lines = briefing_text.splitlines()
    if len(lines) > _BRIEFING_MAX_LINES:
        lines = lines[: _BRIEFING_MAX_LINES - 1]
        lines.append(f"… and {len(briefing_text.splitlines()) - len(lines)} more (ceiling reached)")
        briefing_text = "\n".join(lines)

    try:
        with open(write_path, "w") as f:
            f.write(briefing_text)
    except Exception as exc:
        print(f"WARNING: failed to write briefing to {write_path}: {exc}", file=sys.stderr)
        return

    if meta_path:
        meta = {
            "token_count": _count_tokens(briefing_text),
            "line_count": sum(1 for ln in briefing_text.splitlines() if ln.strip()),
            "symbol_count": _count_briefing_symbols(briefing_text),
            "identifier_count": len(identifiers),
            "max_tokens": _BRIEFING_MAX_TOKENS,
            "max_lines": _BRIEFING_MAX_LINES,
            "max_symbols": _BRIEFING_MAX_SYMBOLS,
            "within_token_budget": _count_tokens(briefing_text) <= _BRIEFING_MAX_TOKENS,
        }
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            print(f"WARNING: failed to write briefing meta to {meta_path}: {exc}", file=sys.stderr)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GT Intelligence — ranked evidence from code graph")
    parser.add_argument("--db", required=True, help="Path to graph.db from gt-index")
    parser.add_argument("--file", default="", help="Source file being edited (relative path)")
    parser.add_argument("--function", default="", help="Specific function name (optional)")
    parser.add_argument("--root", default="/testbed", help="Project root directory")
    parser.add_argument("--max-lines", type=int, default=20, help="Max output lines")
    parser.add_argument("--log", default="", help="Path to write evidence log JSON (append mode)")
    parser.add_argument("--briefing", action="store_true", help="Pre-task briefing mode (compact)")
    parser.add_argument(
        "--enhanced-briefing",
        action="store_true",
        help="Pre-exploration briefing: graph evidence upfront (recommended)",
    )
    parser.add_argument("--reminder", action="store_true", help="With --file: print 1-3 line reminder only")
    parser.add_argument("--issue-text", default="", help="Issue text for briefing (or @file to read from file)")
    parser.add_argument("--affected-tests", default="", help="Print test files affected by changes to this source file")
    parser.add_argument(
        "--diff-file", default="",
        help="Path to a file containing the current diff text (optional). "
             "If omitted, gt_intel runs 'git diff HEAD' against --file automatically.",
    )
    parser.add_argument(
        "--write-briefing", default="",
        help="When used with --briefing/--enhanced-briefing, also write the briefing text to this path.",
    )
    parser.add_argument(
        "--briefing-meta", default="",
        help="When used with --write-briefing, emit a JSON sidecar with token/line/symbol counts.",
    )
    args = parser.parse_args()

    # v20: affected-tests mode — fast path, no evidence computation
    if args.affected_tests:
        tests = affected_tests(args.db, args.affected_tests, args.root)
        output = format_affected_tests(tests, args.affected_tests)
        if output:
            print(output)
        return

    if not os.path.exists(args.db):
        print(f"ERROR: graph.db not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)

    # v15: check for same_file resolution leaks
    verify_admissibility_gate(conn)

    def _issue_body() -> str:
        issue_text = args.issue_text
        if issue_text.startswith("@") and os.path.exists(issue_text[1:]):
            issue_text = open(issue_text[1:]).read()
        return issue_text

    # Enhanced briefing — upfront evidence (preferred over --briefing)
    if args.enhanced_briefing:
        issue_text = _issue_body()
        identifiers = extract_identifiers_from_issue(issue_text)
        if identifiers:
            briefing_text = generate_enhanced_briefing(conn, args.root, identifiers)
        else:
            briefing_text = format_gt_output([], fallback_ok="No identifiers extracted from issue.")
        print(briefing_text)
        _persist_briefing(briefing_text, identifiers, args.write_briefing, args.briefing_meta)
        conn.close()
        return

    # Briefing mode — extract identifiers from issue, query graph
    if args.briefing:
        issue_text = _issue_body()
        identifiers = extract_identifiers_from_issue(issue_text)
        if identifiers:
            briefing_text = generate_pretask_briefing(conn, args.root, identifiers)
        else:
            briefing_text = format_gt_output([], fallback_ok="No identifiers extracted from issue.")
        print(briefing_text)
        _persist_briefing(briefing_text, identifiers, args.write_briefing, args.briefing_meta)
        conn.close()
        return

    # Normalize file path
    file_path = args.file if args.file else ""
    if not file_path:
        conn.close()
        return
    if os.path.isabs(file_path):
        file_path = os.path.relpath(file_path, args.root)
    file_path = file_path.replace("\\", "/")

    # Find target
    target = get_target_node(conn, file_path, args.function)
    if not target:
        # No target found — emit [OK] so GT is never silent
        print(format_gt_output([], fallback_ok="No target function found in graph."))
        conn.close()
        return

    # v17: staleness detection (v1.0.4: hash-based suppression)
    staleness = check_staleness(args.db, target.file_path, args.root)
    if staleness == "SUPPRESS":
        print(format_gt_output([], fallback_ok="Evidence suppressed — file changed since last index."))
        conn.close()
        return

    # Compute evidence
    candidates = compute_evidence(conn, args.root, target)
    selected = rank_and_select(candidates)

    # Log evidence (always, even if suppressed)
    if args.log:
        log_evidence(candidates, selected, target, args.log, conn=conn)

    # vNext: nullability contract check — fires when diff adds 'return None' to a
    # function that has return_shape=value + caller_usage in the properties table.
    # Reads diff from --diff-file if provided, otherwise runs git diff HEAD.
    _nullability_warning: str | None = None
    try:
        if args.diff_file and os.path.isfile(args.diff_file):
            with open(args.diff_file) as _df:
                _diff_text = _df.read()
        else:
            _diff_text = _get_diff_text(args.root, target.file_path)
        if _diff_text:
            _nullability_warning = _check_nullability_contract(conn, target.id, _diff_text)
    except Exception:
        pass  # never let this block evidence output

    # Format and print (never silent)
    if args.reminder:
        reminder_output = format_reminder(selected, target, staleness_warning=staleness)
        if _nullability_warning:
            # Inject the contract warning inside the <gt-evidence> block
            reminder_output = reminder_output.replace(
                "</gt-evidence>",
                f"[VERIFIED] {_nullability_warning} (1.00)\n</gt-evidence>",
            )
        print(reminder_output)
    else:
        if selected:
            evidence_output = format_output(selected, target, args.root)
        else:
            evidence_output = format_gt_output([], staleness_warning=staleness,
                                               fallback_ok="No ranked evidence for this target.")
        if _nullability_warning:
            evidence_output = evidence_output.replace(
                "</gt-evidence>",
                f"[CONTRACT] {_nullability_warning}\n</gt-evidence>",
            )
        print(evidence_output)

    conn.close()


def affected_tests(db_path: str, changed_file: str, root: str = "") -> list[str]:
    """Find test files affected by changes to a source file.

    Uses the call graph to trace: changed_file → functions defined there →
    callers of those functions → test files containing those callers.

    This is the TDAD approach (Test-Driven Agentic Development) that reduces
    regressions by 70% by telling the agent which tests to run after an edit.
    """
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        # Get all functions defined in the changed file
        changed_nodes = conn.execute(
            "SELECT id, name FROM nodes WHERE file_path = ?",
            (changed_file,)
        ).fetchall()

        if not changed_nodes:
            # Try with root prefix stripped
            if root and changed_file.startswith(root):
                rel = changed_file[len(root):].lstrip("/")
                changed_nodes = conn.execute(
                    "SELECT id, name FROM nodes WHERE file_path = ?",
                    (rel,)
                ).fetchall()

        if not changed_nodes:
            return []

        node_ids = [r[0] for r in changed_nodes]

        # v1.0.4: Apply the same trust gating as get_callers()/get_tests()
        ph, methods = _resolution_sql_in()
        has_conf = _has_confidence_column(conn)
        # Bare table: column without alias prefix; joined: "e." prefix
        conf_bare = f" AND confidence >= {MIN_CONFIDENCE}" if has_conf else ""
        conf_e = _confidence_clause(has_conf, alias="e")

        # Find all callers of these functions (admissible edges only)
        caller_ids = set()
        for nid in node_ids:
            for row in conn.execute(
                f"SELECT DISTINCT source_id FROM edges WHERE target_id = ? AND type = 'CALLS'"
                f" AND resolution_method IN ({ph}){conf_bare}",
                (nid, *methods)
            ):
                caller_ids.add(row[0])

        # Find which of those callers are in test files
        test_files = set()
        if caller_ids:
            placeholders = ",".join("?" * len(caller_ids))
            for row in conn.execute(
                f"SELECT DISTINCT file_path FROM nodes WHERE id IN ({placeholders}) AND is_test = 1",
                list(caller_ids)
            ):
                test_files.add(row[0])

        # Also find test files that directly reference the changed file's functions
        # (with trust gating on edges)
        for nid in node_ids:
            for row in conn.execute(
                f"""SELECT DISTINCT n.file_path FROM nodes n
                   JOIN edges e ON e.source_id = n.id
                   WHERE e.target_id = ? AND n.is_test = 1
                   AND e.resolution_method IN ({ph}){conf_e}""",
                (nid, *methods)
            ):
                test_files.add(row[0])

        return sorted(test_files)[:10]  # Cap at 10 most relevant test files
    finally:
        conn.close()


def format_affected_tests(test_files: list[str], changed_file: str) -> str:
    """Format affected test files as a concise recommendation."""
    if not test_files:
        return ""
    lines = [f"\n[GT] Tests affected by changes to {changed_file}:"]
    for tf in test_files[:5]:  # Show top 5
        lines.append(f"  RUN: {tf}")
    if len(test_files) > 5:
        lines.append(f"  ... and {len(test_files) - 5} more test files")
    return "\n".join(lines)


# ── v1.0.4: Standalone CRITIQUE for hook integration ────────────────────────

def compute_critique_standalone(db_path: str, file_path: str, root: str) -> str | None:
    """Compute post-edit CRITIQUE without requiring groundtruth_v2 imports.

    Called from within Docker containers via python3 -c. Returns formatted
    CRITIQUE lines or None if no structural issues found.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)

        # Get functions in the edited file
        nodes = conn.execute(
            "SELECT id, name, signature FROM nodes WHERE file_path = ? AND label IN ('Function', 'Method')",
            (file_path,),
        ).fetchall()
        if not nodes:
            conn.close()
            return None

        lines: list[str] = []
        for node_id, node_name, old_sig in nodes:
            # Check for callers that might break
            callers = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'"
                " AND resolution_method IN ('same_file', 'import')",
                (node_id,),
            ).fetchone()
            caller_count = callers[0] if callers else 0

            if caller_count == 0:
                continue

            # Check: does the current file on disk have a different signature?
            src_path = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path
            if not os.path.exists(src_path):
                continue

            try:
                with open(src_path, "r") as f:
                    content = f.read()
            except Exception:
                continue

            # Find the function definition in the current file
            import re as _re
            pattern = _re.compile(rf"def\s+{_re.escape(node_name)}\s*\(([^)]*)\)")
            match = pattern.search(content)

            if match:
                # Symbol still exists — compare signature/arity
                new_params = match.group(1).strip()
                if old_sig:
                    old_match = pattern.search(old_sig) or _re.search(r"\(([^)]*)\)", old_sig)
                    if old_match:
                        old_params = old_match.group(1).strip()
                        old_count = len([p for p in old_params.split(",") if p.strip() and p.strip() != "self" and "=" not in p]) if old_params else 0
                        new_count = len([p for p in new_params.split(",") if p.strip() and p.strip() != "self" and "=" not in p]) if new_params else 0
                        if new_count > old_count:
                            lines.append(
                                f"BREAKING: {node_name}() added {new_count - old_count} required param(s);"
                                f" {caller_count} caller(s) use old arity"
                            )
            else:
                # Symbol missing from file — report stale removal
                caller_files = conn.execute(
                    "SELECT DISTINCT source_file FROM edges WHERE target_id = ? AND type = 'CALLS' LIMIT 5",
                    (node_id,),
                ).fetchall()
                file_list = ", ".join(r[0] for r in caller_files if r[0])
                lines.append(f"STALE: {node_name}() removed; {caller_count} reference(s) in {file_list or 'other files'}")

        conn.close()
        return "\n".join(lines[:5]) if lines else None

    except Exception:
        return None


if __name__ == "__main__":
    main()
