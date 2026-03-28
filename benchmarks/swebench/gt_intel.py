#!/usr/bin/env python3
"""GT Intelligence Layer — reads graph.db from Go indexer, produces ranked evidence.

5 evidence families, scored 0-3:
  CALLER:  how cross-file callers use the target's return value
  SIBLING: behavioral norms from sibling methods in the same class
  TEST:    assertions from test functions that reference the target
  IMPACT:  blast radius (caller count + critical path)
  TYPE:    return type contract from annotation + caller confirmation

Selection: only ≥2 shown, max 6 nodes, max 2 per family, suppress if <2 qualified.
Output: 25-40 lines of real source code + summaries.

Usage:
    python3 gt_intel.py --db=/tmp/gt_graph.db --file=src/model.py --root=/app
    python3 gt_intel.py --db=/tmp/gt_graph.db --function=delete --file=src/model.py --root=/app
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

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


def get_callers(conn: sqlite3.Connection, target_id: int, target_file: str) -> list[tuple[GraphNode, int, str]]:
    """Get cross-file callers of target. Returns (caller_node, call_line, source_file)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT n.*, e.source_line, e.source_file
        FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND e.source_file != ?
        LIMIT 10
    """, (target_id, target_file))

    results = []
    for row in cur.fetchall():
        node = _row_to_node(row[:-2])
        call_line = row[-2] or 0
        source_file = row[-1] or ""
        results.append((node, call_line, source_file))
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
    cur.execute("""
        SELECT n.* FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND n.is_test = 1
        LIMIT 5
    """, (target_id,))
    return [_row_to_node(r) for r in cur.fetchall()]


def get_all_callers_count(conn: sqlite3.Connection, target_id: int) -> tuple[int, int]:
    """Returns (total_callers, unique_files)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(DISTINCT source_file)
        FROM edges WHERE target_id=? AND type='CALLS'
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

# ── Evidence computation ────────────────────────────────────────────────────

def compute_evidence(conn: sqlite3.Connection, root: str, target: GraphNode) -> list[EvidenceNode]:
    """Compute ranked evidence for a target function."""
    candidates: list[EvidenceNode] = []

    # Family 1: CALLER
    callers = get_callers(conn, target.id, target.file_path)
    for caller_node, call_line, source_file in callers:
        score, summary = classify_caller_usage(root, source_file, call_line)
        if score >= 1:
            code = read_lines(root, source_file, max(1, call_line - 1), call_line + 2)
            candidates.append(EvidenceNode(
                family="CALLER", score=score,
                name=caller_node.name, file=source_file, line=call_line,
                source_code=code, summary=summary,
            ))

    # Family 2: SIBLING
    siblings = get_siblings(conn, target.id)
    if len(siblings) >= 3:
        # Check return type norm
        ret_types = [s.return_type for s in siblings if s.return_type]
        if ret_types:
            from collections import Counter
            common = Counter(ret_types).most_common(1)[0]
            if common[1] / len(siblings) >= 0.7:
                best = next((s for s in siblings if s.return_type == common[0]), siblings[0])
                code = read_lines(root, best.file_path, best.start_line,
                                  min(best.end_line, best.start_line + 8))
                candidates.append(EvidenceNode(
                    family="SIBLING", score=3,
                    name=best.name, file=best.file_path, line=best.start_line,
                    source_code=code,
                    summary=f"returns {common[0]} ({common[1]}/{len(siblings)} siblings agree)",
                ))

    # Family 3: TEST
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

    # Family 4: IMPACT
    total_callers, unique_files = get_all_callers_count(conn, target.id)
    critical = is_critical_path(target.file_path)
    if total_callers >= 5 or critical:
        candidates.append(EvidenceNode(
            family="IMPACT", score=2,
            name=target.name, file=target.file_path, line=0,
            source_code="",
            summary=f"{total_callers} callers in {unique_files} files" +
                    (" — CRITICAL PATH" if critical else ""),
        ))

    # Family 5: TYPE
    if target.return_type:
        score = 1
        if any(c.score >= 2 and "destruct" in c.summary for c in candidates if c.family == "CALLER"):
            score = 2
        candidates.append(EvidenceNode(
            family="TYPE", score=score,
            name=target.name, file=target.file_path, line=target.start_line,
            source_code="", summary=f"returns {target.return_type}",
        ))

    return candidates

# ── Ranking + selection ─────────────────────────────────────────────────────

def rank_and_select(candidates: list[EvidenceNode], max_nodes: int = 6) -> list[EvidenceNode]:
    """Select top evidence nodes: ≥2 score, max 2 per family, max 6 total."""
    qualified = [c for c in candidates if c.score >= 1]  # lowered from 2 to capture more evidence
    qualified.sort(key=lambda c: (-c.score, c.family))

    selected: list[EvidenceNode] = []
    family_counts: dict[str, int] = {}
    for c in qualified:
        if family_counts.get(c.family, 0) >= 2:
            continue
        selected.append(c)
        family_counts[c.family] = family_counts.get(c.family, 0) + 1
        if len(selected) >= max_nodes:
            break

    return selected if len(selected) >= 1 else []  # lowered from 2

# ── Output formatting ───────────────────────────────────────────────────────

def format_output(selected: list[EvidenceNode], target: GraphNode, root: str) -> str:
    """Format ranked evidence into the agent-readable output block."""
    lines = ["=== GT CODEBASE INTELLIGENCE ===", ""]

    # Target context (always show)
    target_code = read_lines(root, target.file_path, target.start_line,
                             min(target.end_line, target.start_line + 5))
    if target_code:
        lines.append(f"TARGET: {target.name} ({target.file_path}:{target.start_line})")
        for cl in target_code.split("\n")[:5]:
            lines.append(f"  {cl}")
        lines.append("")

    current_family = ""
    for node in selected:
        if node.family != current_family:
            headers = {
                "CALLER": "--- CALLERS ---",
                "SIBLING": "--- SIBLING PATTERN ---",
                "TEST": "--- TESTS ---",
                "IMPACT": "--- IMPACT ---",
                "TYPE": "--- TYPE CONTRACT ---",
            }
            lines.append(headers.get(node.family, f"--- {node.family} ---"))
            current_family = node.family

        if node.source_code:
            loc = f" ({os.path.basename(node.file)}:{node.line})" if node.line else ""
            lines.append(f"{node.name}{loc}")
            for cl in node.source_code.split("\n")[:6]:
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
    parser.add_argument("--file", required=True, help="Source file being edited (relative path)")
    parser.add_argument("--function", default="", help="Specific function name (optional)")
    parser.add_argument("--root", default="/testbed", help="Project root directory")
    parser.add_argument("--max-lines", type=int, default=40, help="Max output lines")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: graph.db not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)

    # Normalize file path
    file_path = args.file
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

    if not selected:
        conn.close()
        return  # suppressed — <2 qualified nodes

    # Format and print
    output = format_output(selected, target, args.root)
    print(output)

    conn.close()


if __name__ == "__main__":
    main()
