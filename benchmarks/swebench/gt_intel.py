#!/usr/bin/env python3
"""GT Intelligence Layer v13 — reads graph.db from Go indexer, produces ranked evidence.

7 evidence families, scored 0-3:
  IMPORT:    correct import paths for callees in other files
  CALLER:    how cross-file callers use the target's return value
  SIBLING:   behavioral norms from sibling methods in the same class
  TEST:      assertions from test functions that reference the target
  IMPACT:    blast radius (caller count + critical path)
  TYPE:      return type contract from annotation + caller confirmation
  PRECEDENT: last git commit that touched the target function

v13: Default-deny admissibility gate. Only edges with verified resolution
(same_file, import) pass through. name_match edges are universally rejected.
Go indexer is single source of truth — no Python-side fallbacks.
Output: max 4 nodes, max 1/family, min 2 to show, max 20 lines.
Briefing: max 12 lines, case-insensitive matching.

Usage:
    python3 gt_intel.py --db=/tmp/gt_graph.db --file=src/model.py --root=/app
    python3 gt_intel.py --db=/tmp/gt_graph.db --file=src/model.py --root=/app --log=/tmp/ev.jsonl
    python3 gt_intel.py --db=/tmp/gt_graph.db --briefing --issue-text="fix do_encrypt" --root=/app
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
from dataclasses import dataclass, field

# ── v13: Admissibility gate ────────────────────────────────────────────────
# Only edges with verified resolution pass. Go indexer is single source of truth.
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import"})


def is_admissible(resolution_method: str) -> bool:
    """Universal admissibility gate. Default-deny: reject anything not verified."""
    return resolution_method in VERIFIED_RESOLUTIONS


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
    """Get cross-file callers of target. Returns (caller_node, call_line, source_file, resolution_method).
    v13: Only admissible edges (same_file, import) pass through."""
    cur = conn.cursor()
    cur.execute("""
        SELECT n.*, e.source_line, e.source_file, e.resolution_method
        FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND e.source_file != ?
          AND e.resolution_method IN ('same_file', 'import')
        LIMIT 10
    """, (target_id, target_file))

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
    """Get test functions that call the target.
    v13: Only admissible edges pass through."""
    cur = conn.cursor()
    cur.execute("""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
          AND e.resolution_method IN ('same_file', 'import')
        LIMIT 5
    """, (target_id,))
    return [_row_to_node(r) for r in cur.fetchall()]


def get_all_callers_count(conn: sqlite3.Connection, target_id: int) -> tuple[int, int]:
    """Returns (total_callers, unique_files). v13: Only counts verified edges."""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(DISTINCT source_file)
        FROM edges WHERE target_id=? AND type='CALLS'
          AND resolution_method IN ('same_file', 'import')
    """, (target_id,))
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


def generate_pretask_briefing(
    conn: sqlite3.Connection, root: str, identifiers: list[str], max_lines: int = 12,
) -> str:
    """Query graph.db for matching symbols + 1-hop connections. Returns compact briefing."""
    cur = conn.cursor()
    briefing_lines = ["=== GT PRE-TASK BRIEFING ===", ""]
    found_symbols = []

    for ident in identifiers:
        # Skip file paths — just note them
        if "/" in ident and "." in ident:
            briefing_lines.append(f"  File mentioned: {ident}")
            continue

        # For dotted refs like Module.method, search the last part
        search_name = ident.split(".")[-1] if "." in ident else ident

        rows = cur.execute("""
            SELECT id, label, name, qualified_name, file_path, start_line, end_line
            FROM nodes
            WHERE LOWER(name) = LOWER(?) AND is_test = 0
            LIMIT 3
        """, (search_name,)).fetchall()

        for row in rows:
            node_id, label, name, qname, fpath, sline, eline = row
            found_symbols.append(name)
            loc = f"{fpath}:{sline}" if sline else fpath
            briefing_lines.append(f"  {label}: {qname or name} ({loc})")

            # 1-hop callers — v13: only verified resolutions
            callers = cur.execute("""
                SELECT n.name, n.file_path, e.source_line
                FROM edges e JOIN nodes n ON e.source_id = n.id
                WHERE e.target_id = ? AND e.type = 'CALLS'
                  AND e.resolution_method IN ('same_file', 'import') AND n.is_test = 0
                LIMIT 3
            """, (node_id,)).fetchall()
            for cname, cfile, cline in callers:
                briefing_lines.append(f"    <- called by {cname} ({cfile}:{cline})")

            # 1-hop callees — v13: only verified resolutions
            callees = cur.execute("""
                SELECT n.name, n.file_path
                FROM edges e JOIN nodes n ON e.target_id = n.id
                WHERE e.source_id = ? AND e.type = 'CALLS'
                  AND e.resolution_method IN ('same_file', 'import')
                LIMIT 3
            """, (node_id,)).fetchall()
            for cname, cfile in callees:
                briefing_lines.append(f"    -> calls {cname} ({cfile})")

            # Tests — v13: only verified resolutions
            tests = cur.execute("""
                SELECT n.name, n.file_path
                FROM edges e JOIN nodes n ON e.source_id = n.id
                WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
                  AND e.resolution_method IN ('same_file', 'import')
                LIMIT 2
            """, (node_id,)).fetchall()
            for tname, tfile in tests:
                briefing_lines.append(f"    tested by {tname} ({tfile})")

            briefing_lines.append("")

        if len(briefing_lines) > max_lines + 2:
            break

    if not found_symbols:
        return ""  # no matches — don't show empty briefing

    # Trim to max_lines
    if len(briefing_lines) > max_lines + 2:
        briefing_lines = briefing_lines[:max_lines] + ["  ... (more connections available)", ""]

    briefing_lines.append("===")
    return "\n".join(briefing_lines)


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
    """Get functions that the target calls (outgoing CALLS edges).
    v13: Only admissible edges pass through."""
    cur = conn.cursor()
    cur.execute("""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.target_id
        WHERE e.source_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ('same_file', 'import')
        LIMIT 10
    """, (target_id,))
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
            from collections import Counter
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

def rank_and_select(candidates: list[EvidenceNode], max_nodes: int = 4) -> list[EvidenceNode]:
    """Select top evidence nodes. v13: max 4 nodes, max 1/family, min 2 to show."""
    qualified = [c for c in candidates if c.score >= 1]  # v13: lowered from 2 to 1 — import gate handles precision
    qualified.sort(key=lambda c: (-c.score, c.family))

    selected: list[EvidenceNode] = []
    family_counts: dict[str, int] = {}
    for c in qualified:
        if family_counts.get(c.family, 0) >= 1:  # v13: max 1 per family (force diversity)
            continue
        selected.append(c)
        family_counts[c.family] = family_counts.get(c.family, 0) + 1
        if len(selected) >= max_nodes:
            break

    return selected if len(selected) >= 1 else []  # v13: output gate — show if ≥1 admissible node

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
        "version": "v13",
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
        "v13_admissibility": {
            "edges_same_file": edge_counts.get("same_file", 0),
            "edges_import": edge_counts.get("import", 0),
            "edges_name_match_rejected": edge_counts.get("name_match", 0),
            "admissible_candidates": len(candidates),
            "output_gate_passed": len(selected) >= 2,
            "name_match_in_output": False,  # enforced by gate
        },
    }

    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # never fail the main pipeline for logging


# ── Output formatting ───────────────────────────────────────────────────────

def format_output(selected: list[EvidenceNode], target: GraphNode, root: str) -> str:
    """Format ranked evidence into the agent-readable output block."""
    lines = ["=== GT CODEBASE INTELLIGENCE ===", ""]

    # Target context (always show)
    target_code = read_lines(root, target.file_path, target.start_line,
                             min(target.end_line, target.start_line + 3))
    if target_code:
        lines.append(f"TARGET: {target.name} ({target.file_path}:{target.start_line})")
        for cl in target_code.split("\n")[:3]:  # v13: max 3 lines for target (was 5)
            lines.append(f"  {cl}")
        lines.append("")

    current_family = ""
    for node in selected:
        if node.family != current_family:
            headers = {
                "IMPORT": "--- IMPORTS (correct paths) ---",
                "CALLER": "--- CALLERS ---",
                "SIBLING": "--- SIBLING PATTERN ---",
                "TEST": "--- TESTS ---",
                "IMPACT": "--- IMPACT ---",
                "TYPE": "--- TYPE CONTRACT ---",
                "PRECEDENT": "--- GIT PRECEDENT ---",
            }
            lines.append(headers.get(node.family, f"--- {node.family} ---"))
            current_family = node.family

        if node.source_code:
            loc = f" ({os.path.basename(node.file)}:{node.line})" if node.line else ""
            lines.append(f"{node.name}{loc}")
            for cl in node.source_code.split("\n")[:4]:  # v13: max 4 lines per node (was 6)
                lines.append(f"  {cl}")
        if node.summary:
            lines.append(f"  → {node.summary}")
        lines.append("")

    # Trim trailing blanks
    while lines and not lines[-1].strip():
        lines.pop()

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
    parser.add_argument("--briefing", action="store_true", help="Pre-task briefing mode")
    parser.add_argument("--issue-text", default="", help="Issue text for briefing (or @file to read from file)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: graph.db not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)

    # Briefing mode — extract identifiers from issue, query graph
    if args.briefing:
        issue_text = args.issue_text
        if issue_text.startswith("@") and os.path.exists(issue_text[1:]):
            issue_text = open(issue_text[1:]).read()
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
        return  # suppressed — no qualified nodes

    # Format and print
    output = format_output(selected, target, args.root)
    print(output)

    conn.close()


if __name__ == "__main__":
    main()
