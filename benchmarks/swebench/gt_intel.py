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

# ── v15: Admissibility gate ────────────────────────────────────────────────
# Edges with verified resolution pass (Go indexer is source of truth).
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})


def _resolution_sql_in() -> tuple[str, tuple[str, ...]]:
    """SQL IN clause placeholders and bound values for current VERIFIED_RESOLUTIONS."""
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    return ",".join("?" * len(methods)), methods


def is_admissible(resolution_method: str) -> bool:
    """True if resolution_method is allowed through the gate."""
    return resolution_method in VERIFIED_RESOLUTIONS


def verify_admissibility_gate(conn: sqlite3.Connection) -> bool:
    """Check for same_file edges that cross file boundaries (resolution leak).
    If found, narrow VERIFIED_RESOLUTIONS to import + name_match only."""
    global VERIFIED_RESOLUTIONS
    try:
        leaks = conn.execute("""
            SELECT COUNT(*) FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.resolution_method = 'same_file'
              AND s.file_path != t.file_path
        """).fetchone()[0]
        if leaks > 0:
            print(f"WARNING: {leaks} same_file cross-file leaks — removing same_file from gate",
                  file=sys.stderr)
            VERIFIED_RESOLUTIONS = frozenset({"import", "name_match"})
            return False
    except Exception:
        pass
    return True


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
            "SELECT * FROM nodes WHERE file_path=? AND name=? AND label IN ('Function','Method') LIMIT 1",
            (file_path, function_name),
        )
    else:
        # Pick the node with the most incoming CALLS edges
        cur.execute("""
            SELECT n.* FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
            WHERE n.file_path = ? AND n.label IN ('Function', 'Method', 'Class')
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
            GROUP BY n.id
            ORDER BY COUNT(e.id) DESC
            LIMIT 1
        """, ("%" + os.path.basename(file_path),))
        row = cur.fetchone()

    if not row:
        return None
    return _row_to_node(row)


def get_callers(conn: sqlite3.Connection, target_id: int, target_file: str) -> list[tuple[GraphNode, int, str, str]]:
    """Get cross-file callers of target. Returns (caller_node, call_line, source_file, resolution_method)."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    cur.execute(f"""
        SELECT n.*, e.source_line, e.source_file, e.resolution_method
        FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND e.source_file != ?
          AND e.resolution_method IN ({ph})
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
    cur.execute(f"""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
          AND e.resolution_method IN ({ph})
        LIMIT 5
    """, (target_id, *methods))
    return [_row_to_node(r) for r in cur.fetchall()]


def get_all_callers_count(conn: sqlite3.Connection, target_id: int) -> tuple[int, int]:
    """Returns (total_callers, unique_files). Only counts admissible edges."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    cur.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT source_file)
        FROM edges WHERE target_id=? AND type='CALLS'
          AND resolution_method IN ({ph})
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


def classify_caller_usage(root: str, file_path: str, call_line: int) -> tuple[int, str]:
    """Read lines around a call site and classify usage. Returns (score, summary)."""
    text = read_lines(root, file_path, max(1, call_line - 1), call_line + 2)
    if not text:
        return 1, "invokes"

    # Score 3: destructure or type assertion
    if re.search(r'(\w+)\s*,\s*(\w+)\s*=\s*', text):
        return 3, "destructures return as tuple"
    if re.search(r'isinstance\(', text):
        return 3, "isinstance check on return"
    if re.search(r'\.\w+\b', text) and not re.search(r'\.\w+\s*\(', text):
        return 3, "accesses attribute on return"

    # Score 2: conditional usage
    if re.search(r'if\s+.*\w+\(', text):
        return 2, "checks return in conditional"
    if re.search(r'(==|!=|is |is not |>=|<=|>|<)\s*', text):
        return 2, "compares return value"
    if re.search(r'assert', text):
        return 2, "asserts on return"

    # Score 1: just invokes
    return 1, "invokes without using return"


def is_critical_path(file_path: str) -> bool:
    fp = file_path.lower()
    return any(kw in fp for kw in CRITICAL_PATHS)

# ── Assertion extraction ────────────────────────────────────────────────────

ASSERTION_PATTERNS = {
    "python": [r'assert\w*\s*\((.{5,80})\)', r'self\.assert\w+\((.{5,80})\)', r'pytest\.raises\((\w+)\)'],
    "go": [r't\.\w+\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)'],
    "javascript": [r'expect\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)'],
    "typescript": [r'expect\((.{5,80})\)', r'assert\.\w+\((.{5,80})\)'],
}


def extract_assertions(root: str, node: GraphNode) -> list[str]:
    """Extract assertion statements from a test function."""
    text = read_lines(root, node.file_path, node.start_line, node.end_line)
    patterns = ASSERTION_PATTERNS.get(node.language, ASSERTION_PATTERNS["python"])
    assertions = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            a = m.group(0).strip()
            if len(a) > 100:
                a = a[:97] + "..."
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
    "would", "could", "been", "each", "any", "all", "new", "old", "get",
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

    # File paths mentioned (v13: added jsx, tsx, mjs, cjs)
    identifiers.update(re.findall(r'[\w/]+\.(?:py|go|js|ts|rs|java|rb|php|c|cpp|h|jsx|tsx|mjs|cjs)\b', issue_text))

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


def resolve_briefing_targets(
    conn: sqlite3.Connection, identifiers: list[str], max_targets: int = 2,
) -> list[GraphNode]:
    """Resolve up to max_targets graph nodes from issue identifiers (same logic as pretask briefing)."""
    cur = conn.cursor()
    targets: list[GraphNode] = []
    ph, methods = _resolution_sql_in()

    symbols_shown = 0
    for ident in identifiers:
        if symbols_shown >= max_targets:
            break
        if "/" in ident and "." in ident:
            continue
        search_name = ident.split(".")[-1] if "." in ident else ident
        rows = cur.execute("""
            SELECT * FROM nodes
            WHERE LOWER(name) = LOWER(?) AND is_test = 0
            LIMIT 2
        """, (search_name,)).fetchall()
        for row in rows:
            if symbols_shown >= max_targets:
                break
            targets.append(_row_to_node(row))
            symbols_shown += 1

    if not targets:
        for ident in identifiers:
            if len(ident) < 4:
                continue
            rows = cur.execute("""
                SELECT * FROM nodes
                WHERE qualified_name LIKE ? AND is_test = 0
                LIMIT 2
            """, (f"%{ident}%",)).fetchall()
            for row in rows:
                targets.append(_row_to_node(row))
                if len(targets) >= max_targets:
                    break
            if targets:
                break

    if not targets:
        top_nodes = cur.execute(f"""
            SELECT n.*, COUNT(e.source_id) as caller_count
            FROM nodes n
            JOIN edges e ON e.target_id = n.id
            WHERE e.type = 'CALLS' AND e.resolution_method IN ({ph})
              AND n.label IN ('Function','Method') AND n.is_test = 0
              AND n.file_path NOT LIKE '%test%'
            GROUP BY n.id
            ORDER BY caller_count DESC
            LIMIT 3
        """, methods).fetchall()
        for row in top_nodes:
            targets.append(_row_to_node(row[:-1]))
            if len(targets) >= max_targets:
                break

    return targets[:max_targets]


def _briefing_line_for_node(node: EvidenceNode, target: GraphNode) -> str:
    """Single compact line for enhanced briefing."""
    if node.family == "CALLER":
        loc = f"{os.path.basename(node.file)}:{node.line}" if node.line else node.file
        snippet = (node.source_code or "").replace("\n", " ").strip()[:120]
        base = f"{node.name}() at {loc} — {node.summary}"
        if snippet:
            return f"{base} | {snippet}"
        return base
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
    conn: sqlite3.Connection, root: str, identifiers: list[str], max_lines: int = 25,
) -> str:
    """Pre-exploration report: locations + tiered evidence (callers, tests, imports)."""
    targets = resolve_briefing_targets(conn, identifiers, max_targets=2)
    if not targets:
        return generate_pretask_briefing(conn, root, identifiers, max_lines=min(8, max_lines))

    lines: list[str] = ["\u26a0\ufe0f CODEBASE CONTEXT (pre-exploration):"]

    for target in targets:
        if len(lines) >= max_lines - 1:
            break
        loc = f"{target.file_path}:{target.start_line}" if target.start_line else target.file_path
        sig = (target.signature or target.name or "")[:100]
        qn = target.qualified_name or target.name
        lines.append(f"\u2022 FIX HERE: {qn}() \u2192 {loc}")
        if sig:
            lines.append(f"  signature: {sig}")

        candidates = compute_evidence(conn, root, target)
        selected = rank_and_select(candidates, max_high=4, max_low=2)
        high = [n for n in selected if n.score >= 2]
        low = [n for n in selected if n.score == 1]

        if high and len(lines) < max_lines:
            lines.append("  HIGH-CONFIDENCE:")
            for n in high:
                if len(lines) >= max_lines:
                    break
                lines.append(f"    \u2022 {_briefing_line_for_node(n, target)}")

        if low and len(lines) < max_lines:
            lines.append("  ADDITIONAL CONTEXT:")
            for n in low:
                if len(lines) >= max_lines:
                    break
                lines.append(f"    \u2022 {_briefing_line_for_node(n, target)}")

    return "\n".join(lines[:max_lines])


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
        return ""

    lines = ["\u26a0\ufe0f CODEBASE CONTEXT:"]
    for b in bullets[:max_lines - 1]:
        lines.append(f"\u2022 {b}")
    return "\n".join(lines)


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
                lines = [f"commit: {commit_msg[:80]}"]
                for hunk in relevant_hunks[:6]:
                    lines.append(f"  {hunk}")
                return "\n".join(lines)

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# ── Evidence computation ────────────────────────────────────────────────────

def get_callees(conn: sqlite3.Connection, target_id: int) -> list[GraphNode]:
    """Get functions that the target calls (outgoing CALLS edges)."""
    cur = conn.cursor()
    ph, methods = _resolution_sql_in()
    cur.execute(f"""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.target_id
        WHERE e.source_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({ph})
        LIMIT 10
    """, (target_id, *methods))
    return [_row_to_node(r) for r in cur.fetchall()]


def compute_evidence(conn: sqlite3.Connection, root: str, target: GraphNode) -> list[EvidenceNode]:
    """Compute ranked evidence for a target function.

    6 families:
      IMPORT: correct import paths for functions the target calls
      CALLER: cross-file callers with usage classification
      SIBLING: behavioral norms from sibling methods
      TEST: test functions with assertions
      IMPACT: blast radius (caller count + critical path)
      TYPE: return type contract
    """
    candidates: list[EvidenceNode] = []

    # Family 0: IMPORT — correct import paths for callees
    # This is the #1 hallucination prevention signal
    callees = get_callees(conn, target.id)
    seen_imports = set()
    for callee in callees:
        if callee.file_path == target.file_path:
            continue  # same file, no import needed
        # Build import path from file path
        import_path = callee.file_path.replace("/", ".").replace("\\", ".")
        if import_path.endswith(".py"):
            import_path = import_path[:-3]
        if import_path.endswith(".__init__"):
            import_path = import_path[:-9]
        key = (callee.name, import_path)
        if key in seen_imports:
            continue
        seen_imports.add(key)
        sig = callee.signature if callee.signature else callee.name
        candidates.append(EvidenceNode(
            family="IMPORT", score=2,
            name=callee.name, file=callee.file_path, line=callee.start_line,
            source_code=f"from {import_path} import {callee.name}" if callee.name else "",
            summary=f"signature: {sig[:80]}",
        ))

    # Family 1: CALLER — cross-file callers with usage classification
    # v13: get_callers() already filters to admissible edges only (same_file, import)
    callers = get_callers(conn, target.id, target.file_path)
    for caller_node, call_line, source_file, resolution_method in callers:
        score, summary = classify_caller_usage(root, source_file, call_line)
        if score >= 1:
            code = read_lines(root, source_file, max(1, call_line - 1), call_line + 2)
            candidates.append(EvidenceNode(
                family="CALLER", score=score,
                name=caller_node.name, file=source_file, line=call_line,
                source_code=code, summary=summary,
            ))

    # Family 2: SIBLING — behavioral norms from same class
    siblings = get_siblings(conn, target.id)
    if len(siblings) >= 2:
        # Show the best sibling as a pattern example (even without return type norm)
        best_sib = max(siblings, key=lambda s: (s.end_line - s.start_line))
        code = read_lines(root, best_sib.file_path, best_sib.start_line,
                          min(best_sib.end_line, best_sib.start_line + 6))
        if code:
            candidates.append(EvidenceNode(
                family="SIBLING", score=1,
                name=best_sib.name, file=best_sib.file_path, line=best_sib.start_line,
                source_code=code,
                summary=f"sibling method in same class ({len(siblings)} total)",
            ))

        # Upgrade to score 3 if return type norm exists
        ret_types = [s.return_type for s in siblings if s.return_type]
        if ret_types:
            common = Counter(ret_types).most_common(1)[0]
            if common[1] / max(len(siblings), 1) >= 0.7:
                candidates[-1].score = 3
                candidates[-1].summary = f"returns {common[0]} ({common[1]}/{len(siblings)} siblings agree)"

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
            family="IMPACT", score=2 if (total_callers >= 3 or critical) else 1,
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
    precedent = get_git_precedent(root, target.file_path, target.start_line, target.end_line)
    if precedent:
        candidates.append(EvidenceNode(
            family="PRECEDENT", score=2,
            name=target.name, file=target.file_path, line=target.start_line,
            source_code="", summary=precedent,
        ))

    return candidates

# ── Ranking + selection ─────────────────────────────────────────────────────

def rank_and_select(
    candidates: list[EvidenceNode],
    max_high: int = 4,
    max_low: int = 2,
) -> list[EvidenceNode]:
    """Tiered selection: score>=2 first (recall-preserving), then score==1. Max 1 per family."""
    high = [c for c in candidates if c.score >= 2]
    high.sort(key=lambda c: (-c.score, c.family))
    low = [c for c in candidates if c.score == 1]
    low.sort(key=lambda c: (-c.score, c.family))

    selected: list[EvidenceNode] = []
    family_used: set[str] = set()

    def take_from(pool: list[EvidenceNode], cap: int) -> None:
        count = 0
        for c in pool:
            if count >= cap:
                break
            if c.family in family_used:
                continue
            selected.append(c)
            family_used.add(c.family)
            count += 1

    take_from(high, max_high)
    take_from(low, max_low)

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
    """One imperative bullet for post-edit / tiered output."""
    if node.family == "CALLER":
        loc = f"{os.path.basename(node.file)}:{node.line}" if node.line else node.file
        return f"DO NOT change return type — {node.name}() at {loc} {node.summary}"
    if node.family == "IMPORT":
        return f"USE: {node.source_code}" if node.source_code else f"USE: {node.name} from {node.file}"
    if node.family == "SIBLING":
        return f"MATCH PATTERN: {node.summary}"
    if node.family == "TEST":
        if node.source_code:
            return f"VERIFY: {node.name} in {node.file} — {node.source_code[:120]}"
        return f"VERIFY: {node.name} in {node.file}"
    if node.family == "IMPACT":
        return f"CAUTION: {node.summary}"
    if node.family == "TYPE":
        return f"MUST return {target.return_type or node.summary}"
    if node.family == "PRECEDENT":
        return f"MATCH PATTERN: {node.summary}"
    return node.summary


def format_output(selected: list[EvidenceNode], target: GraphNode, root: str) -> str:
    """Tiered: high-confidence (score>=2) then additional context (score==1)."""
    high = [n for n in selected if n.score >= 2]
    low = [n for n in selected if n.score == 1]
    lines: list[str] = []

    if high:
        lines.append("\u26a0\ufe0f HIGH-CONFIDENCE CONSTRAINTS:")
        for node in high[:4]:
            lines.append(f"\u2022 {_evidence_constraint_bullet(node, target)}")
    if low:
        lines.append("ADDITIONAL CONTEXT (score=1):")
        for node in low[:2]:
            lines.append(f"\u2022 {_evidence_constraint_bullet(node, target)}")

    if not lines:
        return ""
    return "\n".join(lines)


def format_reminder(selected: list[EvidenceNode], target: GraphNode) -> str:
    """1-3 line post-edit reinforcement (short)."""
    if not selected:
        return ""
    lines = ["\u26a0\ufe0f REMINDER (GroundTruth):"]
    for node in selected[:2]:
        lines.append(f"\u2022 {_evidence_constraint_bullet(node, target)[:240]}")
    return "\n".join(lines)

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
    args = parser.parse_args()

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
            briefing = generate_enhanced_briefing(conn, args.root, identifiers)
            if briefing:
                print(briefing)
        conn.close()
        return

    # Briefing mode — extract identifiers from issue, query graph
    if args.briefing:
        issue_text = _issue_body()
        identifiers = extract_identifiers_from_issue(issue_text)
        if identifiers:
            briefing = generate_pretask_briefing(conn, args.root, identifiers)
            if briefing:
                print(briefing)
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
        # Silent — no output means no evidence
        conn.close()
        return

    # Compute evidence
    candidates = compute_evidence(conn, args.root, target)
    selected = rank_and_select(candidates)

    # Log evidence (always, even if suppressed)
    if args.log:
        log_evidence(candidates, selected, target, args.log, conn=conn)

    if not selected:
        conn.close()
        return

    # Format and print
    if args.reminder:
        out = format_reminder(selected, target)
        if out:
            print(out)
    else:
        output = format_output(selected, target, args.root)
        if output:
            print(output)

    conn.close()


if __name__ == "__main__":
    main()
