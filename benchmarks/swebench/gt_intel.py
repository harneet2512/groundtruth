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
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass

# ── v17: Staleness detection ───────────────────────────────────────────────

def check_staleness(db_path: str, source_file: str, root: str) -> str | None:
    """Return a warning string if graph.db is older than the source file,
    or if the source file no longer exists (M8 fix: detect deleted files)."""
    try:
        db_mtime = os.path.getmtime(db_path)
        src_path = os.path.join(root, source_file) if not os.path.isabs(source_file) else source_file
        if not os.path.exists(src_path):
            return f"{os.path.basename(source_file)} no longer exists — evidence may reference deleted code"
        if os.path.getmtime(src_path) > db_mtime:
            return f"graph.db is behind {os.path.basename(source_file)} — evidence may be stale"
    except OSError:
        pass
    return None


# ── v15: Admissibility gate ────────────────────────────────────────────────
# Edges with verified resolution pass (Go indexer is source of truth).
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "class_hierarchy", "fqn", "name_match"}
)
# Note: name_match stays in VERIFIED_RESOLUTIONS for evidence queries (callers/tests).
# The ego-graph uses verified_only with fallback separately.


def _resolution_sql_in() -> tuple[str, tuple[str, ...]]:
    """SQL IN clause placeholders and bound values for current VERIFIED_RESOLUTIONS."""
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    return ",".join("?" * len(methods)), methods


# Minimum confidence threshold for evidence inclusion.
# Edges below this are excluded from callers/callees/tests queries.
MIN_CONFIDENCE = 0.5
MAX_EVIDENCE_LINES = 6  # v22: file + skeleton + test command


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
            VERIFIED_RESOLUTIONS = frozenset({"import", "class_hierarchy", "fqn", "name_match"}
            )
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
    conf_clause = _confidence_clause(_has_confidence_column(conn))
    cur.execute(f"""
        SELECT n.*, e.source_line, e.source_file, e.resolution_method
        FROM edges e
        JOIN nodes n ON n.id = e.source_id
        WHERE e.target_id = ? AND e.type = 'CALLS' AND e.source_file != ?
          AND e.resolution_method IN ({ph}){conf_clause}
        ORDER BY e.source_line, n.id
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
        ORDER BY n.name, n.id
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


def get_callers_by_resolution(conn: sqlite3.Connection, target_id: int) -> dict[str, int]:
    """v21-definitive: Count callers grouped by resolution_method. Language-agnostic.
    Returns {'same_file': N, 'import': N, 'name_match': N}."""
    cur = conn.cursor()
    conf_clause = _confidence_clause(_has_confidence_column(conn), alias="edges")
    rows = cur.execute(f"""
        SELECT resolution_method, COUNT(*) FROM edges
        WHERE target_id = ? AND type = 'CALLS'{conf_clause}
        GROUP BY resolution_method
    """, (target_id,)).fetchall()
    return {method: count for method, count in rows if method}


def format_impact_verdict(
    total: int, files: int, resolution_counts: dict[str, int], critical: bool,
) -> tuple[str, int]:
    """v21-definitive: Returns (verdict_string, evidence_score). Language-agnostic.

    Converts raw caller counts into decision-resolving verdicts with provenance.
    Research: ReAct (Yao et al., 2022) — tools help most when they improve ACTION SELECTION.
    '12 callers' is data. 'HIGH RISK — 12 callers will break' is action-changing.
    """
    verified = resolution_counts.get("same_file", 0) + resolution_counts.get("import", 0)
    speculative = resolution_counts.get("name_match", 0)
    provenance = f"({verified} verified, {speculative} name-match)" if speculative else "(all verified)"

    if total > 20 or (critical and total > 5):
        return f"IMPACT: {total} callers across {files} files — CRITICAL {provenance}", 3
    elif total > 5:
        return f"IMPACT: {total} callers across {files} files — HIGH RISK {provenance}", 2
    elif total > 2:
        return f"IMPACT: {total} callers across {files} files — MODERATE RISK {provenance}", 1
    elif total > 0:
        return f"IMPACT: {total} callers in {files} files — LOW RISK {provenance}", 1
    return "", 0


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
                "SELECT kind, expression FROM assertions WHERE test_node_id = ? ORDER BY line, kind LIMIT 8",
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


def get_test_command(test_node: GraphNode) -> str | None:
    """v21-definitive: Build test runner command for any language. Returns None if unsupported.

    Language-agnostic: supports Python, Go, JS/TS, Java, Rust, C#, Ruby, Kotlin.
    Uses the most common runner for each language (Guardrail 4).
    Falls back to None for unknown languages — never emits a wrong command.
    """
    lang = test_node.language.lower()
    fpath = test_node.file_path
    func = test_node.name
    qname = test_node.qualified_name or ""

    if lang == "python":
        if qname and "." in qname:
            parts = qname.rsplit(".", 1)
            selector = f"{fpath}::{parts[0]}::{parts[1]}"
        else:
            selector = f"{fpath}::{func}"
        return f"python -m pytest {selector} -xvs"

    elif lang == "go":
        pkg_dir = os.path.dirname(fpath)
        if func:
            return f"go test -v -run {func} ./{pkg_dir}/..."
        return f"go test -v ./{pkg_dir}/..."

    elif lang in ("javascript", "typescript"):
        if func:
            return f"npx jest {fpath} -t \"{func}\""
        return f"npx jest {fpath}"

    elif lang in ("java", "groovy"):
        class_name = os.path.splitext(os.path.basename(fpath))[0]
        if func:
            return f"mvn test -Dtest={class_name}#{func}"
        return f"mvn test -Dtest={class_name}"

    elif lang == "rust":
        if func:
            return f"cargo test {func} -- --exact"
        return "cargo test"

    elif lang == "csharp":
        if func:
            return f"dotnet test --filter {func}"
        return "dotnet test"

    elif lang == "ruby":
        if func:
            return f"bundle exec rspec {fpath} -e \"{func}\""
        return f"bundle exec rspec {fpath}"

    elif lang == "kotlin":
        class_name = os.path.splitext(os.path.basename(fpath))[0]
        if func:
            return f"./gradlew test --tests {class_name}.{func}"
        return f"./gradlew test --tests {class_name}"

    # Unknown language — stay silent (Guardrail 6: graceful degradation)
    return None


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
    for ident in sorted(filtered, key=lambda x: (-len(x), x)):
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
    # Split on camelCase boundaries: getUserById → get User By Id
    raw = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Split on acronym boundaries: HTMLOutputter → HTML Outputter, XMLParser → XML Parser
    raw = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', raw)
    for part in re.split(r'[\s/._\-:,;(){}[\]"\'`<>]+', raw.lower()):
        if len(part) >= 3 and part not in _NOISE_WORDS:
            tokens.add(part)
    return tokens


def extract_compound_terms(identifiers: list[str]) -> dict[str, float]:
    """v21-definitive: Extract compound terms with weights from issue identifiers.

    Compound terms (multi-word identifiers) get 3x weight because they're highly
    specific — `translate_url` matching one file is 3x stronger than `url` matching 50.

    Language-agnostic: handles snake_case, CamelCase, PascalCase, dot.paths,
    SCREAMING_CASE, scope::resolution across all 31 supported languages.

    Returns dict mapping term (lowered) → weight (3.0 compound, 1.0 sub-token).
    """
    terms: dict[str, float] = {}
    for ident in identifiers:
        is_compound = False

        # snake_case: translate_url, get_user_by_id (Python, Rust, Ruby, C, Go)
        if '_' in ident:
            is_compound = True
        # dot.paths: astropy.io.ascii, com.myapp.UserService (Java, Python, C#)
        elif '.' in ident:
            is_compound = True
        # scope::resolution: std::vector, crate::module (C++, Rust)
        elif '::' in ident:
            is_compound = True
        # camelCase: getUserById (JS, TS, Java, Kotlin, Swift, Go)
        elif re.search(r'[a-z][A-Z]', ident):
            is_compound = True
        # PascalCase / mixed-case with 2+ humps: UserService, HTMLOutputter
        # Detected by tokenizer producing 2+ sub-tokens from a single identifier
        elif len(_tokenize_text(ident)) >= 2:
            is_compound = True
        # SCREAMING_CASE: MAX_RETRIES, DEFAULT_TIMEOUT (constants, any language)
        elif re.match(r'^[A-Z][A-Z_]{2,}$', ident):
            is_compound = True

        if is_compound:
            terms[ident.lower()] = max(terms.get(ident.lower(), 0), 3.0)

        # Always add sub-tokens at weight 1.0 (standard BM25 behavior)
        for sub in _tokenize_text(ident):
            if sub not in terms:
                terms[sub] = 1.0

    return terms


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
    results.sort(key=lambda x: (-x[1], x[0].id))

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
            ORDER BY id
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
                ORDER BY id
                LIMIT 5
            """, (f"%{ident}%",)).fetchall()
            if rows:
                candidates = [_row_to_node(r) for r in rows]
                scored = _resolution_confidence(candidates, issue_tokens, conn)
                if scored and scored[0][2] != "abstain":
                    targets.append((scored[0][0], scored[0][2]))
                    break

    return targets[:max_targets]


# ── v22: Graph-boosted file scoring ──────────────────────────────────────


def graph_boosted_file_scores(
    bm25_scores: dict[str, float], conn: sqlite3.Connection, max_hops: int = 3,
) -> dict[str, float]:
    """v22: Boost file scores using call graph edges from graph.db.

    BM25 finds files matching terms. Graph boost finds files STRUCTURALLY
    CONNECTED to those files. The gold fix is often in the connected file,
    not the term-matching file.

    Research: LocAgent (ACL 2025) achieved 92.7% file accuracy with graph traversal.
    Language agnostic: operates on graph.db edges, not source code.
    """
    boosted = dict(bm25_scores)

    # Only propagate from top BM25 files (avoid noise from low-scoring files)
    top_files = sorted(bm25_scores.items(), key=lambda x: (-x[1], x[0]))[:5]

    decay_factors = [1.0, 0.5, 0.25, 0.12]  # Score decays per hop

    for source_file, source_score in top_files:
        if source_score <= 0:
            continue

        # BFS through graph edges up to max_hops
        visited = {source_file}
        frontier: list[tuple[str, float, int]] = [(source_file, source_score, 0)]

        while frontier:
            current_file, current_score, hops = frontier.pop(0)

            if hops >= max_hops:
                continue

            # Find all files connected to current_file via edges
            connected = conn.execute("""
                SELECT DISTINCT
                    CASE WHEN s.file_path = ? THEN t.file_path
                         ELSE s.file_path END AS neighbor_file,
                    AVG(COALESCE(e.confidence, 0.5)) as avg_confidence
                FROM edges e
                JOIN nodes s ON e.source_id = s.id
                JOIN nodes t ON e.target_id = t.id
                WHERE (s.file_path = ? OR t.file_path = ?)
                AND s.file_path != t.file_path
                AND s.is_test = 0 AND t.is_test = 0
                GROUP BY neighbor_file
                ORDER BY avg_confidence DESC
                LIMIT 20
            """, (current_file, current_file, current_file)).fetchall()

            decay = decay_factors[min(hops, len(decay_factors) - 1)]

            for neighbor_file, avg_conf in connected:
                if neighbor_file in visited:
                    continue
                visited.add(neighbor_file)

                boost = current_score * avg_conf * decay
                boosted[neighbor_file] = boosted.get(neighbor_file, 0) + boost

                if hops + 1 < max_hops:
                    frontier.append((neighbor_file, boost, hops + 1))

    return boosted


def build_file_skeleton(
    filepath: str, conn: sqlite3.Connection,
) -> str | None:
    """v22: List all functions/methods in a file with line numbers and cross-file caller counts.

    The agent reads the skeleton and decides which function to edit.
    GT provides the map, the agent navigates.

    Research: Agentless (2024) showed function pinning accuracy is ~51%.
    File-level is 78%. Let the agent pick the function from the skeleton.
    Language agnostic — uses graph.db nodes table.
    """
    funcs = conn.execute("""
        SELECT n.name, n.start_line, n.label,
            (SELECT COUNT(DISTINCT e.source_id) FROM edges e
             JOIN nodes caller ON e.source_id = caller.id
             WHERE e.target_id = n.id AND e.type = 'CALLS'
             AND caller.file_path != n.file_path) as cross_file_callers
        FROM nodes n
        WHERE n.file_path = ?
        AND n.label IN ('Function', 'Method')
        AND n.is_test = 0
        ORDER BY n.start_line
        LIMIT 15
    """, (filepath,)).fetchall()

    if not funcs:
        return None

    parts: list[str] = []
    for name, line, label, callers in funcs:
        if callers > 5:
            parts.append(f"  {name}:{line} ! {callers} cross-file callers")
        elif callers > 0:
            parts.append(f"  {name}:{line} ({callers} callers)")
        else:
            parts.append(f"  {name}:{line}")

    return "\n".join(parts)


def filter_also_files(
    target_file: str, also_candidates: list[tuple[str, float]],
    conn: sqlite3.Connection,
) -> list[tuple[str, float]]:
    """v22: Remove ALSO files with no structural connection to the target.

    If a file has zero edges to the target file, it's BM25 noise.
    Only show files that are structurally connected via the call graph.
    """
    connected: list[tuple[str, float]] = []
    for also_file, score in also_candidates:
        if also_file == target_file:
            continue
        edge_count = conn.execute("""
            SELECT COUNT(*) FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE (s.file_path = ? AND t.file_path = ?)
            OR (s.file_path = ? AND t.file_path = ?)
        """, (target_file, also_file, also_file, target_file)).fetchone()[0]

        if edge_count > 0:
            connected.append((also_file, score))

    return connected


def annotate_related_files(
    target_file: str, candidates: list[tuple[str, float]],
    conn: sqlite3.Connection,
) -> list[str]:
    """v1.0.4: Replace flat ALSO with directed edge annotations.

    For each candidate, determine its relationship to the target via edge direction:
    - IMPORTS: target imports from candidate (target depends on it)
    - CALLED BY: candidate calls functions in target (impact if target changes)
    - TESTED BY: candidate is a test file for target
    """
    annotations: list[str] = []
    for cand_file, _ in candidates:
        if cand_file == target_file:
            continue

        # Check if candidate is a test file
        is_test = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE file_path = ? AND is_test = 1",
            (cand_file,),
        ).fetchone()[0] > 0

        if is_test:
            # Test file — extract expectations
            expectations = _extract_test_expectations(conn, cand_file, target_file)
            if expectations:
                exp_str = ", ".join(f"{e}()" for e in expectations[:5])
                annotations.append(f"TESTED BY: {cand_file} → expects {exp_str}")
            else:
                annotations.append(f"TESTED BY: {cand_file}")
            continue

        # Target imports FROM candidate (target depends on candidate)
        target_imports = conn.execute("""
            SELECT DISTINCT tgt.name FROM edges e
            JOIN nodes src ON e.source_id = src.id
            JOIN nodes tgt ON e.target_id = tgt.id
            WHERE src.file_path = ? AND tgt.file_path = ?
              AND (e.type IN ('IMPORTS', 'CALLS') OR e.resolution_method = 'import')
            LIMIT 3
        """, (target_file, cand_file)).fetchall()
        if target_imports:
            symbols = ", ".join(r[0] for r in target_imports)
            annotations.append(f"IMPORTS: {cand_file}:{symbols}")
            continue

        # Candidate calls functions in target (impact direction)
        cand_calls = conn.execute("""
            SELECT DISTINCT tgt.name FROM edges e
            JOIN nodes src ON e.source_id = src.id
            JOIN nodes tgt ON e.target_id = tgt.id
            WHERE src.file_path = ? AND tgt.file_path = ?
              AND (e.type IN ('CALLS', 'IMPORTS') OR e.resolution_method = 'import')
            LIMIT 3
        """, (cand_file, target_file)).fetchall()
        if cand_calls:
            symbols = ", ".join(r[0] for r in cand_calls)
            annotations.append(f"CALLED BY: {cand_file}:{symbols}")

    return annotations


# ── v1.0.4: Definition-likeness scoring ──────────────────────────────────


def _build_definition_likeness_cache(
    conn: sqlite3.Connection,
) -> dict[str, float]:
    """v1.0.4: Score files by how much they DEFINE vs USE.

    Source files that define things have more incoming IMPORTS edges.
    Test/consumer files have more outgoing IMPORTS edges.
    Returns a dict of file_path -> multiplier in [0.5, 1.5].
    """
    rows = conn.execute("""
        SELECT file_path,
            SUM(CASE WHEN direction = 'incoming' THEN cnt ELSE 0 END) as incoming,
            SUM(CASE WHEN direction = 'outgoing' THEN cnt ELSE 0 END) as outgoing
        FROM (
            SELECT tgt.file_path as file_path, 'incoming' as direction, COUNT(*) as cnt
            FROM edges e
            JOIN nodes src ON e.source_id = src.id
            JOIN nodes tgt ON e.target_id = tgt.id
            WHERE (e.type = 'IMPORTS' OR e.resolution_method = 'import')
            AND src.file_path != tgt.file_path
            GROUP BY tgt.file_path
            UNION ALL
            SELECT src.file_path, 'outgoing', COUNT(*)
            FROM edges e
            JOIN nodes src ON e.source_id = src.id
            JOIN nodes tgt ON e.target_id = tgt.id
            WHERE (e.type = 'IMPORTS' OR e.resolution_method = 'import')
            AND src.file_path != tgt.file_path
            GROUP BY src.file_path
        ) sub
        GROUP BY file_path
    """).fetchall()

    cache: dict[str, float] = {}
    for fpath, incoming, outgoing in rows:
        ratio = (incoming + 1) / (outgoing + 1)
        cache[fpath] = min(1.5, max(0.5, 0.5 + ratio * 0.5))
    return cache


# ── v21: File-first localization ──────────────────────────────────────────


def resolve_file_targets(
    conn: sqlite3.Connection, identifiers: list[str], max_files: int = 5,
) -> tuple[list[tuple[str, list[GraphNode], float]], list[float]]:
    """v21-definitive: BM25 file scoring with compound term weighting + z-score support.

    Returns (file_results, all_scores) where:
    - file_results: [(file_path, [ranked_functions], bm25_score), ...] sorted by score desc
    - all_scores: [float, ...] all non-zero BM25 scores for z-score computation
    BM25 weights rare terms higher (IDF), compound terms 3x, penalizes non-source dirs.
    """
    # v21-definitive: Build weighted issue terms (compound terms get 3x)
    issue_terms = extract_compound_terms(identifiers)
    if not issue_terms:
        return [], []

    cur = conn.cursor()

    # Get all non-test files (cap at 500 for perf)
    file_rows = cur.execute(
        "SELECT DISTINCT file_path FROM nodes WHERE is_test = 0 ORDER BY file_path LIMIT 500"
    ).fetchall()
    all_files = [r[0] for r in file_rows]
    N = len(all_files)
    if N == 0:
        return [], []

    # Precompute document frequency (df) for each issue token
    df: dict[str, int] = {}
    for token in issue_terms:
        count = cur.execute(
            "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE is_test = 0 AND (LOWER(file_path) LIKE ? OR LOWER(name) = ?)",
            (f"%{token}%", token),
        ).fetchone()[0]
        df[token] = count

    # Get all symbol names grouped by file (single query, efficient)
    symbol_rows = cur.execute(
        "SELECT file_path, LOWER(name) FROM nodes WHERE is_test = 0"
    ).fetchall()
    file_symbols: dict[str, set[str]] = {}
    for fpath, name in symbol_rows:
        file_symbols.setdefault(fpath, set()).add(name)

    # BM25 parameters
    k1, b = 1.2, 0.75
    # Average "document length" = path tokens + symbol count
    total_len = sum(len(_tokenize_text(f)) + len(file_symbols.get(f, set())) for f in all_files)
    avg_len = total_len / max(N, 1)

    # Infrastructure penalty files — never the bug location
    INFRA_BASENAMES = frozenset({
        "conftest.py", "setup.py", "setup.cfg", "__init__.py",
        "manage.py", "wsgi.py", "asgi.py",
    })
    # v21-definitive: Non-source directory penalty — all ecosystems covered
    # Python: build/dist/egg-info/venv/.tox  JS: node_modules/.next  Go: vendor
    # Java: target/build  Rust: target  C++: cmake-build/obj  C#: bin/obj
    NON_SOURCE_DIRS = frozenset({
        # Documentation & examples (any language)
        "examples", "example", "demo", "demos", "samples", "sample",
        "doc", "docs", "documentation",
        # Build & distribution (any language)
        "build", "dist", "out", "target", "bin", "obj",
        # External dependencies (any language)
        "extern", "external", "third_party", "thirdparty", "vendor",
        "node_modules", "bower_components",
        # Infrastructure (any language)
        "scripts", "tools", "benchmarks", "fixtures",
        "migrations", "contrib", ".git",
        # Python-specific
        "venv", ".tox", "__pycache__", ".mypy_cache", ".pytest_cache",
        # JS-specific
        ".next", ".nuxt", ".jest", ".nyc_output",
        # Java-specific
        ".gradle", ".m2",
    })

    # Score each file with BM25
    file_scores: list[tuple[str, float]] = []
    for fpath in all_files:
        fpath_lower = fpath.lower()
        symbols = file_symbols.get(fpath, set())
        file_len = len(_tokenize_text(fpath)) + len(symbols)

        score = 0.0
        for token, weight in issue_terms.items():
            token_df = df.get(token, 0)
            if token_df == 0:
                continue
            # v21-definitive: tf weighted by compound term weight (3x for compounds)
            # tf: 2 for path match (structural), 1 per symbol match
            raw_tf = (2 if token in fpath_lower else 0) + (1 if token in symbols else 0)
            if raw_tf == 0:
                continue
            tf = weight * raw_tf
            idf = math.log((N - token_df + 0.5) / (token_df + 0.5) + 1)
            bm25_tf = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * file_len / max(avg_len, 1)))
            score += idf * bm25_tf

        # Infrastructure penalty
        basename = os.path.basename(fpath)
        if basename in INFRA_BASENAMES:
            score *= 0.1

        # Non-source directory penalty
        dir_parts = set(fpath.replace("\\", "/").split("/"))
        if dir_parts & NON_SOURCE_DIRS:
            score *= 0.1

        if score > 0:
            file_scores.append((fpath, score))

    if not file_scores:
        return [], []

    # v1.0.4: Definition-likeness re-ranking — source files over test/consumer files
    def_cache = _build_definition_likeness_cache(conn)
    file_scores = [(f, s * def_cache.get(f, 1.0)) for f, s in file_scores]

    # v22: Graph-boosted file scoring — propagate BM25 through call graph edges
    bm25_dict = {f: s for f, s in file_scores}
    boosted = graph_boosted_file_scores(bm25_dict, conn, max_hops=3)
    file_scores = [(f, s) for f, s in boosted.items() if s > 0]

    file_scores.sort(key=lambda x: (-x[1], x[0]))

    # Return ALL scores for z-score confidence calculation
    all_scores = [s for _, s in file_scores]

    top_files = file_scores[:max_files]

    # For each top file, rank functions with multi-signal scoring
    # Use the sub-tokens (weight 1.0) for function-level matching
    issue_tokens = {t for t, w in issue_terms.items() if w == 1.0} | set(issue_terms.keys())
    results: list[tuple[str, list[GraphNode], float]] = []
    for fpath, score in top_files:
        funcs = _rank_functions_in_file(conn, fpath, issue_tokens)
        results.append((fpath, funcs, score))

    return results, all_scores


def _rank_functions_in_file(
    conn: sqlite3.Connection, fpath: str, issue_tokens: set[str],
) -> list[GraphNode]:
    """Multi-signal function ranking within a file (CombineFL-style)."""
    func_rows = conn.execute("""
        SELECT * FROM nodes WHERE file_path = ? AND is_test = 0
          AND label IN ('Function', 'Method', 'Class')
        ORDER BY start_line
    """, (fpath,)).fetchall()
    funcs = [_row_to_node(r) for r in func_rows]
    if not funcs:
        return []

    scored: list[tuple[GraphNode, float]] = []
    for node in funcs:
        # Signal 1: Name overlap (0.40 weight)
        name_tokens = _tokenize_text(node.name) | _tokenize_text(node.qualified_name or "")
        name_score = len(issue_tokens & name_tokens) / max(len(issue_tokens), 1)

        # Signal 2: Specificity — unique name = high signal (0.25 weight)
        name_count = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE LOWER(name) = LOWER(?)",
            (node.name,),
        ).fetchone()[0]
        specificity = 1.0 / max(name_count, 1)

        # Signal 3: Anti-centrality — fewer callers = more specific (0.15 weight)
        caller_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
            (node.id,),
        ).fetchone()[0]
        anti_centrality = 1.0 / max(1, math.log(caller_count + 1))

        # Signal 4: Label penalty (0.10 weight)
        label_pen = 0.5 if node.label == "Class" else (0.3 if node.name in ("__init__", "__new__") else 1.0)

        # Unused 0.10 weight reserved for test_proximity (added later if needed)
        total = (name_score * 0.40) + (specificity * 0.25) + (anti_centrality * 0.15) + (label_pen * 0.10)
        scored.append((node, total))

    scored.sort(key=lambda x: (-x[1], x[0].id))
    return [node for node, _ in scored]


def localization_confidence_zscore(
    all_scores: list[float],
) -> tuple[float, float]:
    """v21-definitive: Z-score based confidence — self-calibrating per repo.

    Returns (z_score, confidence) where:
    - z_score: how many standard deviations the top file is above the mean
    - confidence: normalized to 0-0.95 for display

    Z-score tiering (standard statistical thresholds):
    - z >= 2.5 → HIGH confidence (statistical outlier)
    - z >= 1.5 → MEDIUM confidence (unusual)
    - z > 0    → LOW confidence (some signal)
    - z <= 0   → SILENT (no signal)

    This self-calibrates: same thresholds work for 50-file repos and 5000-file repos.
    No static BM25 score thresholds that break across repo sizes.
    """
    if len(all_scores) < 2 or all_scores[0] <= 0:
        return 0.0, 0.0

    n = len(all_scores)
    if n >= 3:
        mean = sum(all_scores) / n
        variance = sum((s - mean) ** 2 for s in all_scores) / n
        std = variance ** 0.5
        if std < 0.001:
            return 0.0, 0.0  # all scores identical → no signal
        z = (all_scores[0] - mean) / std
    elif n == 2:
        # Two files: use gap ratio
        z = (all_scores[0] - all_scores[1]) / max(all_scores[1], 0.001)
    else:
        return 0.0, 0.0  # only 1 file scored — can't compute z-score

    # Confidence from z-score (normalized to 0-0.95, never 1.0)
    confidence = min(z / 4.0, 0.95) if z > 0 else 0.0
    return z, confidence


# ── v21-definitive: Diff-aware post-edit validation ──────────────────────────
# Two-layer design: Layer 1 uses graph.db stored signatures (ALL 31 languages),
# Layer 2 uses Python ast for richer additive-vs-breaking analysis.

import ast as _ast  # imported here to avoid polluting top-level namespace


def _looks_like_declaration(line: str, func_name: str) -> bool:
    """Heuristic: does this line look like a function declaration, not a call?
    Language-agnostic — checks for common declaration patterns."""
    decl_keywords = {
        'def ', 'func ', 'function ', 'fn ', 'fun ', 'sub ', 'proc ',
        'public ', 'private ', 'protected ', 'static ', 'async ',
        'override ', 'virtual ', 'abstract ', 'final ',
    }
    line_lower = line.lower().lstrip()
    for kw in decl_keywords:
        if kw in line_lower:
            return True
    # Common declaration patterns: name( followed by line ending with : { -> =>
    if re.search(rf'\b{re.escape(func_name)}\s*\(', line):
        end = line.rstrip()
        if end and end[-1] in (':', '{') or end.endswith('->') or end.endswith('=>'):
            return True
    return False


def _find_function_in_source(
    source_lines: list[str], func_name: str, old_signature: str,
) -> str | None:
    """Find a function's current signature line in source code. Language-agnostic.
    Uses the function name to locate, returns the line containing it.
    Returns None if function not found."""
    for line in source_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or stripped.startswith('//'):
            continue
        if re.search(rf'\b{re.escape(func_name)}\b', stripped):
            if _looks_like_declaration(stripped, func_name):
                return stripped
    return None


def _extract_python_sigs(source: str) -> dict[str, dict] | None:
    """Layer 2 (Python only): Extract function signatures using ast.parse().
    Returns {name: {'params': [str], 'return_type': str|None}} or None on failure."""
    try:
        tree = _ast.parse(source)
    except (SyntaxError, IndentationError, UnicodeDecodeError, ValueError):
        return None

    sigs: dict[str, dict] = {}
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            params = [a.arg for a in node.args.args if a.arg not in ('self', 'cls')]
            ret = _ast.dump(node.returns) if node.returns else None
            sigs[node.name] = {"params": params, "return_type": ret}
    return sigs


def _count_params_from_sig(sig: str) -> int:
    """Count parameters from a stored signature string. Rough heuristic."""
    m = re.search(r'\(([^)]*)\)', sig)
    if not m:
        return 0
    params_str = m.group(1).strip()
    if not params_str:
        return 0
    params = [p.strip() for p in params_str.split(',')]
    params = [p for p in params if p and p not in ('self', 'cls')]
    return len(params)


def diff_aware_validation(
    filepath: str, root: str, conn: sqlite3.Connection,
) -> list[str] | None:
    """v21-definitive: Compare agent's edit against graph.db signatures.

    Layer 1 (all 31 languages): Compare graph.db stored signature strings vs new file.
    Layer 2 (Python only): Use ast.parse() for additive-vs-breaking distinction.

    Returns list of verdict strings (max 2), or None if nothing to report.
    Guardrail 6: returns None on any parse failure — never crashes the pipeline.
    """
    # Query OLD signatures from graph.db
    old_funcs = conn.execute(
        "SELECT id, name, signature FROM nodes "
        "WHERE file_path = ? AND label IN ('Function', 'Method') ORDER BY start_line",
        (filepath,)
    ).fetchall()

    if not old_funcs:
        return None  # no functions in graph.db for this file

    # Read NEW file from disk
    abs_path = os.path.join(root, filepath) if not os.path.isabs(filepath) else filepath
    try:
        with open(abs_path, encoding='utf-8', errors='replace') as f:
            new_source = f.read()
    except (FileNotFoundError, IOError, PermissionError):
        # File deleted — report functions with callers
        changes: list[tuple[str, str, int]] = []
        for func_id, func_name, _ in old_funcs:
            callers = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
                (func_id,),
            ).fetchone()[0]
            if callers > 0:
                changes.append(("DELETED", f"DELETED: {func_name}() removed — {callers} callers will break", callers))
        return [c[1] for c in changes[:2]] if changes else None

    new_lines = new_source.splitlines()
    ext = os.path.splitext(filepath)[1].lower()

    # Layer 2 (Python enhancement): try ast.parse for richer analysis
    python_sigs = None
    if ext == '.py':
        python_sigs = _extract_python_sigs(new_source)

    changes = []
    for func_id, func_name, old_sig in old_funcs:
        if not old_sig:  # empty signature in graph.db — skip (Guardrail 6)
            continue

        callers = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
            (func_id,),
        ).fetchone()[0]

        if callers == 0:
            continue  # no callers — signature changes don't matter

        # Layer 1 (all languages): find function in new source by name
        new_sig_line = _find_function_in_source(new_lines, func_name, old_sig)

        if new_sig_line is None:
            # Function not found — possibly deleted or renamed
            if python_sigs is not None and func_name not in python_sigs:
                changes.append(("DELETED", f"DELETED: {func_name}() removed — {callers} callers will break", callers))
            elif python_sigs is None:
                # Non-Python or AST failed: check with regex
                if not re.search(rf'\b{re.escape(func_name)}\b', new_source):
                    changes.append(("DELETED", f"DELETED: {func_name}() removed — {callers} callers will break", callers))
            continue

        # Function found — compare signatures
        old_sig_norm = old_sig.strip()
        new_sig_norm = new_sig_line.strip()

        if old_sig_norm != new_sig_norm:
            # Signature changed — use Python AST for additive detection if available
            if python_sigs and func_name in python_sigs and ext == '.py':
                old_params = _count_params_from_sig(old_sig)
                new_params = len(python_sigs[func_name]["params"])
                if new_params > old_params:
                    changes.append(("SAFE", f"SAFE: {func_name}() params added (additive) — {callers} callers OK", 0))
                else:
                    changes.append(("BREAKING", f"BREAKING: {func_name}() signature changed — {callers} callers affected", callers))
            else:
                # Non-Python: conservative report
                changes.append(("BREAKING", f"YOUR EDIT: {func_name}() signature changed — {callers} callers to verify", callers))

    if not changes:
        return None

    # Sort by severity: DELETED > BREAKING > SAFE, then by caller count desc
    severity_order = {"DELETED": 0, "BREAKING": 1, "SAFE": 2}
    changes.sort(key=lambda x: (severity_order.get(x[0], 3), -x[2]))
    return [c[1] for c in changes[:2]]  # max 2 lines for validation


def _find_test_for_file(
    conn: sqlite3.Connection, target_file: str,
) -> GraphNode | None:
    """v1.0.4: Multi-strategy test discovery — import-edge-first.

    Strategy 1: Import-based — test files with CALLS/IMPORTS edges to target (structural)
    Strategy 2: Language-aware naming convention (fallback)
    Language-agnostic: covers Python, Go, JS/TS, Java, Rust, C#, Ruby, Kotlin.
    """
    # Strategy 1: Import-based — find test FILES that have edges pointing to target
    # This is structurally grounded: the test actually imports/calls the target.
    test_file_rows = conn.execute("""
        SELECT DISTINCT n.file_path, COUNT(DISTINCT e.id) as edge_count
        FROM nodes n
        JOIN edges e ON e.source_id = n.id
        JOIN nodes target ON e.target_id = target.id
        WHERE n.is_test = 1 AND target.file_path = ?
          AND (e.type IN ('CALLS', 'IMPORTS') OR e.resolution_method = 'import')
        GROUP BY n.file_path
        ORDER BY edge_count DESC
        LIMIT 3
    """, (target_file,)).fetchall()
    if test_file_rows:
        # Pick a representative node from the best test file for backward compat
        best_test_file = test_file_rows[0][0]
        node_rows = conn.execute("""
            SELECT * FROM nodes WHERE file_path = ? AND is_test = 1
              AND label IN ('Function', 'Method')
            ORDER BY start_line, id LIMIT 1
        """, (best_test_file,)).fetchall()
        if node_rows:
            return _row_to_node(node_rows[0])

    # Strategy 2: Language-aware naming convention fallback
    stem = os.path.splitext(os.path.basename(target_file))[0]
    if len(stem) < 3:
        return None
    ext = os.path.splitext(target_file)[1].lower()
    dir_parts = os.path.dirname(target_file).replace("\\", "/")

    convention_patterns = [
        f"%tests/test_{stem.lower()}%",
        f"%test_{stem.lower()}%",
    ]
    if dir_parts:
        convention_patterns.insert(0, f"%{dir_parts}%test%{stem.lower()}%")

    if ext == '.go':
        convention_patterns.insert(0, f"%{stem.lower()}_test.go")
    elif ext in ('.js', '.jsx', '.ts', '.tsx', '.mjs'):
        convention_patterns.extend([
            f"%{stem.lower()}.test.%",
            f"%{stem.lower()}.spec.%",
            f"%__tests__%{stem.lower()}%",
        ])
    elif ext == '.java':
        convention_patterns.extend([
            f"%{stem}Test.java",
            f"%{stem}Tests.java",
        ])
    elif ext == '.rs':
        convention_patterns.extend([
            f"%tests/{stem.lower()}%",
            f"%tests%{stem.lower()}%",
        ])
    elif ext == '.cs':
        convention_patterns.extend([
            f"%{stem}Test.cs",
            f"%{stem}Tests.cs",
        ])
    elif ext == '.rb':
        convention_patterns.extend([
            f"%spec/{stem.lower()}_spec.rb",
            f"%{stem.lower()}_spec.rb",
        ])
    elif ext == '.kt':
        convention_patterns.extend([
            f"%{stem}Test.kt",
            f"%{stem}Tests.kt",
        ])

    for pattern in convention_patterns:
        rows = conn.execute("""
            SELECT * FROM nodes WHERE is_test = 1
              AND LOWER(file_path) LIKE ?
              AND label IN ('Function', 'Method')
            ORDER BY start_line, id LIMIT 1
        """, (pattern,)).fetchall()
        if rows:
            return _row_to_node(rows[0])

    return None


def _extract_test_expectations(
    conn: sqlite3.Connection, test_file: str, target_file: str,
) -> list[str]:
    """v1.0.4: Find function names the test file expects from the target module.

    Queries CALLS/IMPORTS edges from test_file nodes to target_file nodes.
    Returns list of target function/method names the test calls.
    """
    rows = conn.execute("""
        SELECT DISTINCT target_node.name
        FROM edges e
        JOIN nodes src ON e.source_id = src.id
        JOIN nodes target_node ON e.target_id = target_node.id
        WHERE src.file_path = ? AND target_node.file_path = ?
          AND src.is_test = 1
          AND (e.type IN ('CALLS', 'IMPORTS') OR e.resolution_method = 'import')
          AND target_node.label IN ('Function', 'Method')
        ORDER BY target_node.name
        LIMIT 10
    """, (test_file, target_file)).fetchall()
    return [r[0] for r in rows]


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


def _identifier_specificity(identifiers: list[str]) -> float:
    """v1.0.4: Score how specific the extracted identifiers are.

    Specific identifiers -> high confidence in localization.
    Generic identifiers -> low confidence even with high z-score.
    Returns multiplier in [0.6, 1.3].
    """
    if not identifiers:
        return 0.6

    specific_count = 0.0
    for ident in identifiers:
        if '/' in ident and '.' in ident:  # file path
            specific_count += 3
        elif re.search(r'[A-Z][a-z]+[A-Z]', ident):  # CamelCase multi-hump
            specific_count += 3
        elif '.' in ident:  # dotted reference
            specific_count += 2
        elif '_' in ident and len(ident) > 6:  # compound snake_case
            specific_count += 2
        else:
            specific_count += 0.5

    specificity_ratio = specific_count / max(len(identifiers) * 3, 1)
    return min(1.3, 0.6 + specificity_ratio * 0.7)


def generate_enhanced_briefing(
    conn: sqlite3.Connection, root: str, identifiers: list[str], max_lines: int = 5,
) -> str:
    """v1.0.4: Edge-directed localization with confidence calibration.

    Z-score tiering (standard statistical thresholds, modulated by identifier specificity):
    z >= 2.5 -> HIGH:   TARGET FILE + skeleton + directed annotations
    z >= 1.5 -> MEDIUM: LIKELY FILES with mini-skeletons
    z > 0    -> LOW:    SCOPE (file list)
    z <= 0   -> SILENT: no signal

    v1.0.4 changes vs v22:
    - Definition-likeness re-ranking (source files over test files)
    - Directed edge annotations (replaces flat ALSO)
    - Import-first test discovery with expectations
    - Identifier specificity calibration on z-score
    """
    # Score all files first — graph-boosted BM25 + definition-likeness
    file_results, all_scores = resolve_file_targets(conn, identifiers, max_files=5)
    if not file_results:
        return ""

    # v1.0.4: Calibrate z-score by identifier specificity
    z_raw, _conf_raw = localization_confidence_zscore(all_scores)
    spec_mult = _identifier_specificity(identifiers)
    z = z_raw * spec_mult
    conf = min(z / 4.0, 0.95) if z > 0 else 0.0

    if z <= 0:
        return ""

    lines: list[str] = []

    # ── HIGH: z >= 2.5 — one file dominates. Show skeleton, not function pin. ──
    if z >= 2.5:
        top_file, top_funcs, top_score = file_results[0]

        lines.append(f"TARGET FILE: {top_file} ({conf:.2f})")

        # v22: File skeleton — agent picks the function
        skeleton = build_file_skeleton(top_file, conn)
        if skeleton:
            # Show up to 4 lines of skeleton
            skel_lines = skeleton.splitlines()[:4]
            lines.append("SKELETON:")
            lines.extend(skel_lines)

        # v1.0.4: Directed edge annotations (replaces flat ALSO)
        also_candidates = [(f, s) for f, _, s in file_results[1:4] if s >= top_score * 0.3]
        annotations = annotate_related_files(top_file, also_candidates, conn)

        # v1.0.4: Test discovery + expectations
        test_node = _find_test_for_file(conn, top_file)
        if test_node:
            # Add TESTED BY with expectations if not already in annotations
            has_tested_by = any(a.startswith("TESTED BY:") for a in annotations)
            if not has_tested_by:
                expectations = _extract_test_expectations(conn, test_node.file_path, top_file)
                if expectations:
                    exp_str = ", ".join(f"{e}()" for e in expectations[:5])
                    annotations.append(f"TESTED BY: {test_node.file_path} → expects {exp_str}")
            cmd = get_test_command(test_node)
            if cmd:
                lines.append(f"RUN: {cmd}")

        lines.extend(annotations[:2])  # max 2 annotation lines

    # ── MEDIUM: z >= 1.5 — top 2-3 files with mini-skeletons ──
    elif z >= 1.5:
        top_score = file_results[0][2]
        cutoff = top_score * 0.5
        candidates_files = [(f, funcs, s) for f, funcs, s in file_results if s >= cutoff][:3]

        lines.append("LIKELY FILES:")
        for fpath, funcs, score in candidates_files:
            # Mini-skeleton: top 3 functions with caller counts
            mini_skel = build_file_skeleton(fpath, conn)
            if mini_skel:
                top_funcs_str = ", ".join(mini_skel.splitlines()[:3])
                lines.append(f"  {fpath} ({score:.1f}) -> {top_funcs_str.strip()}")
            else:
                lines.append(f"  {fpath} ({score:.1f})")
            if len(lines) >= MAX_EVIDENCE_LINES:
                break

    # ── LOW: z > 0 — file list only ──
    else:
        top_score = file_results[0][2]
        cutoff = top_score * 0.3
        candidates_files_low = [(f, s) for f, _, s in file_results if s >= cutoff][:5]

        lines.append(f"SCOPE: {len(candidates_files_low)} candidate files:")
        for fpath, _score in candidates_files_low:
            lines.append(f"  {fpath}")
            if len(lines) >= MAX_EVIDENCE_LINES:
                break

    return format_gt_output(
        lines[:MAX_EVIDENCE_LINES],
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
            ORDER BY id
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
                ORDER BY n.id
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
                ORDER BY n.id
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
                ORDER BY id
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
        ORDER BY n.name, n.id
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
            common = sorted(Counter(ret_types).items(), key=lambda x: (-x[1], x[0]))[0]
            if common[1] / max(len(siblings), 1) >= 0.7:
                candidates[-1].score = 3
                candidates[-1].summary = f"returns {common[0]} ({common[1]}/{len(siblings)} siblings agree)"

    # Family 3: TEST — test functions with assertions + RUN command (v21)
    tests = get_tests(conn, target.id)
    for test_node in tests:
        # v21: Add RUN command (highest priority — score 3)
        run_cmd = get_test_command(test_node)
        if run_cmd:
            candidates.append(EvidenceNode(
                family="TEST_RUN", score=3,
                name=test_node.name, file=test_node.file_path, line=test_node.start_line,
                source_code="", summary=f"RUN: {run_cmd}",
            ))

        # v20: Extract best assertion for EXPECTS line
        assertions = extract_assertions(root, test_node)
        if assertions:
            best = assertions[0][:120]
            candidates.append(EvidenceNode(
                family="TEST", score=2,
                name=test_node.name, file=test_node.file_path, line=test_node.start_line,
                source_code="", summary=f"EXPECTS: {best}",
            ))
        else:
            candidates.append(EvidenceNode(
                family="TEST", score=1,
                name=test_node.name, file=test_node.file_path, line=test_node.start_line,
                source_code="", summary=f"test function references {target.name}",
            ))

    # Family 4: IMPACT — decision-resolving verdict with provenance
    total_callers, unique_files = get_all_callers_count(conn, target.id)
    critical = is_critical_path(target.file_path)
    resolution_counts = get_callers_by_resolution(conn, target.id)
    if total_callers >= 1 or critical:
        verdict, impact_score = format_impact_verdict(
            total_callers, unique_files, resolution_counts, critical,
        )
        if verdict and impact_score > 0:
            candidates.append(EvidenceNode(
                family="IMPACT", score=impact_score,
                name=target.name, file=target.file_path, line=0,
                source_code="", summary=verdict,
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
    # v21-definitive: VERIFIED — PRECEDENT stays suppressed (score=0, filtered by MMR).
    # Reason: sympy-15976 proved agent followed prior commit pattern instead of structural rewrite.
    # Kept for future MCP tool use; MMR filters score=0 at line ~1717.
    precedent = get_git_precedent(root, target.file_path, target.start_line, target.end_line)
    if precedent:
        candidates.append(EvidenceNode(
            family="PRECEDENT", score=0,
            name=target.name, file=target.file_path, line=target.start_line,
            source_code="", summary=precedent,
        ))

    return candidates


# ── Ranking + selection ─────────────────────────────────────────────────────

def _estimate_tokens(node: EvidenceNode) -> int:
    """Rough token estimate for an evidence node (1 token ≈ 4 chars)."""
    text = f"{node.family} {node.name} {node.summary} {node.source_code}"
    return max(5, len(text) // 4)


def rank_and_select(
    candidates: list[EvidenceNode],
    max_items: int = 4,
) -> list[EvidenceNode]:
    """v21+QoD: MMR evidence selection — maximize diversity within budget.

    Maximal Marginal Relevance (Carbonell & Goldstein, 1998): after selecting
    the highest-scored item, penalize remaining items from the same family,
    then pick the next highest. Produces diverse evidence within 5-line budget.
    """
    # v21-final: filter out score=0 items (e.g. suppressed PRECEDENT) before selection
    candidates = [c for c in candidates if c.score > 0]

    # Boost negative specs (constraint violations are highest value)
    for c in candidates:
        if c.family == "TEST" and any(kw in c.summary.lower() for kw in ("raises", "error", "exception")):
            c.score = max(c.score, 3)

    family_priority = {"TEST_RUN": 0, "TEST": 1, "CALLER": 2, "IMPORT": 3,
                       "PRECEDENT": 4, "IMPACT": 5, "TYPE": 6, "SIBLING": 7}
    # Initial sort for determinism
    candidates.sort(key=lambda c: (-c.score, family_priority.get(c.family, 9), c.name or '', c.file or ''))

    selected: list[EvidenceNode] = []
    remaining = list(candidates)

    while len(selected) < max_items and remaining:
        families_shown = {s.family for s in selected}
        # MMR: penalize same-family items (0.3x score if family already shown)
        for item in remaining:
            item._adjusted = item.score * (0.3 if item.family in families_shown else 1.0)  # type: ignore[attr-defined]
        remaining.sort(key=lambda c: (
            -getattr(c, '_adjusted', c.score),
            family_priority.get(c.family, 9),
            c.name or '', c.file or '',
        ))
        selected.append(remaining.pop(0))

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
    if node.family == "TEST_RUN":
        return node.summary  # "RUN: python -m pytest ..."
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
        # v21-definitive: verdict already includes IMPACT prefix + risk level + provenance
        return node.summary
    if node.family == "TYPE":
        return f"MUST return {target.return_type or node.summary}"
    if node.family == "PRECEDENT":
        return f"MATCH PATTERN: {node.summary}"
    return node.summary


def format_output(selected: list[EvidenceNode], target: GraphNode, root: str) -> str:
    """v21: Hard 5-line cap. TARGET not VERIFIED — candidates, not verdicts."""
    lines = [f"TARGET: {target.name}() at {target.file_path}:{target.start_line}"]
    for node in selected[:MAX_EVIDENCE_LINES - 1]:
        tier = _score_to_tier(node)
        bullet = _evidence_constraint_bullet(node, target)[:200].replace("\n", " ").strip()
        conf = f"{node.score / 3:.2f}"
        lines.append(f"[{tier}] {bullet} ({conf})")
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


def format_test_reminder(selected: list[EvidenceNode]) -> str:
    """v21: Post-edit reminder with just the test command. One line, ~15 tokens."""
    for node in selected:
        if node.family == "TEST_RUN":
            cmd = node.summary.replace("RUN: ", "")
            return format_gt_output([f"[SPEC] VERIFY YOUR EDIT: {cmd}"])
    return ""  # no test command available — stay silent


# ── v21-definitive: Combined post-edit hook ──────────────────────────────────

def on_edit_hook(
    filepath: str, root: str, conn: sqlite3.Connection,
    first_edit: bool = True,
) -> str | None:
    """v21-definitive: Combined post-edit validation + test command + cross-file callers.
    All GT computation, zero agent cost. Max 4 lines.

    Priority order:
    1. Diff-aware validation (what the edit broke/kept safe) — max 2 lines
    2. Test command (verification path) — 1 line
    3. Cross-file callers with risk verdict (first edit only) — 1 line
    """
    lines: list[str] = []

    # 1. Diff-aware validation — checks agent's OWN edit
    diff_results = diff_aware_validation(filepath, root, conn)
    if diff_results:
        lines.extend(diff_results[:2])

    # 2. Test command
    test_node = _find_test_for_file(conn, filepath)
    if test_node:
        cmd = get_test_command(test_node)
        if cmd:
            lines.append(f"RUN: {cmd}")

    # 3. Cross-file callers (first edit to this file only — no repeat)
    if first_edit and len(lines) < 4:
        target = get_target_node(conn, filepath, "")
        if target:
            total_callers, unique_files = get_all_callers_count(conn, target.id)
            if total_callers >= 2:
                resolution_counts = get_callers_by_resolution(conn, target.id)
                verdict, _ = format_impact_verdict(
                    total_callers, unique_files, resolution_counts,
                    is_critical_path(filepath),
                )
                if verdict:
                    lines.append(verdict)

    lines = lines[:4]  # hard cap

    if not lines:
        return None

    return format_gt_output(lines)


# ── v1.0.5: Structural-key + Jaccard dedup cache ──────────────────────────────
# Two-stage dedup for context injection:
#   Stage 1: O(1) structural key (family::file::symbol) — fast path catches 95%
#   Stage 2: On key collision, Jaccard similarity on 3-shingles — detects evolved evidence
#
# Keying rules:
#   - Different family, same file → always emit (TESTED BY after BREAKING = new info)
#   - Same key, same content → suppress
#   - Same key, content evolved (Jaccard < threshold) → emit

_JACCARD_SUPPRESS_THRESHOLD = 0.85  # above this = same evidence, suppress


def _shingles(text: str, k: int = 3) -> set[str]:
    """3-character shingles for Jaccard similarity."""
    text = text.strip()
    if len(text) < k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


class _EvidenceDedup:
    """Rolling dedup cache keyed by (family, file, symbol).

    - First-time key → always emit, store shingles.
    - Collision, Jaccard >= threshold → suppress (same evidence).
    - Collision, Jaccard <  threshold → emit, update stored shingles (evidence evolved).
    - Different family, same file/symbol → always emit (independent evidence type).
    """

    def __init__(self) -> None:
        self._cache: dict[str, set[str]] = {}  # key -> shingle set of last emitted content

    def should_emit(self, family: str, file_path: str, symbol: str, content: str) -> bool:
        key = f"{family}::{file_path}::{symbol}"
        new_shingles = _shingles(content)
        if key not in self._cache:
            self._cache[key] = new_shingles
            return True
        sim = _jaccard(self._cache[key], new_shingles)
        if sim >= _JACCARD_SUPPRESS_THRESHOLD:
            return False  # same evidence, suppress
        self._cache[key] = new_shingles  # evidence evolved, update and emit
        return True

    def reset(self) -> None:
        self._cache.clear()


_dedup = _EvidenceDedup()



# ── v1.0.1: Ego-graph based briefing + edit hook ─────────────────────────────

def ego_graph_briefing(
    conn: sqlite3.Connection, root: str, identifiers: list[str],
) -> str:
    """v1.0.1: Ego-graph structural map briefing.

    Replaces generate_enhanced_briefing() with:
    1. Extract seed names from identifiers
    2. Find matching nodes in graph.db
    3. BFS through verified edges → structural map
    4. Find test command for seeds

    Empty ego-graph = empty string (GT stays silent).
    """
    # Import ego_graph module — lives in src/groundtruth/
    # When running in Docker/benchmark: it's copied alongside gt_intel.py
    try:
        from groundtruth.ego_graph import (
            extract_ego_graph,
            find_seeds_by_name,
            find_test_for_seeds,
            format_verdict,
        )
    except ImportError:
        # Fallback: try relative import for when copied to /tmp
        import importlib.util
        ego_path = os.path.join(os.path.dirname(__file__), "ego_graph.py")
        if not os.path.exists(ego_path):
            # Can't find ego_graph module — fall through to legacy
            return ""
        spec = importlib.util.spec_from_file_location("ego_graph", ego_path)
        if spec is None or spec.loader is None:
            return ""
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        extract_ego_graph = mod.extract_ego_graph
        find_seeds_by_name = mod.find_seeds_by_name
        find_test_for_seeds = mod.find_test_for_seeds
        format_verdict = mod.format_verdict

    # Extract seed names from identifiers (reuse existing compound term extraction)
    compound_terms = extract_compound_terms(identifiers)
    seed_names = list(compound_terms.keys()) + identifiers

    # Find matching node IDs
    seed_node_ids = find_seeds_by_name(seed_names, conn)
    if not seed_node_ids:
        return ""

    # Extract ego-graph — prefer verified edges, fallback to name-match
    # Try verified-only first (import, same_file, class_hierarchy, fqn)
    ego_edges = extract_ego_graph(seed_node_ids, conn, root=root, verified_only=True)
    if not ego_edges:
        # Fallback: include name-match edges with tighter hops
        ego_edges = extract_ego_graph(seed_node_ids, conn, root=root, max_hops=2)
    if not ego_edges:
        return ""

    # Format verdict (cross-file impact, not raw edge dump)
    verdict = format_verdict(ego_edges, seed_names=seed_names[:3])
    if not verdict:
        return ""

    lines: list[str] = verdict.splitlines()

    # v1.0.5: Find the primary target file from ego-graph seed nodes
    seed_files = conn.execute(
        "SELECT DISTINCT file_path FROM nodes WHERE id IN ({}) AND is_test = 0".format(
            ",".join("?" for _ in seed_node_ids)
        ), seed_node_ids,
    ).fetchall()
    primary_file = seed_files[0][0] if seed_files else None

    # v1.0.5: Annotate related files discovered by the ego-graph.
    # This is the architectural fix: annotate_related_files() was previously only
    # called in the generate_enhanced_briefing fallback path.  We now call it here
    # so IMPORTS / CALLED BY annotations are present in the ego-graph briefing path.
    if primary_file:
        # Collect unique non-test files reachable via ego-graph edges (cross-file only)
        ego_related_files: list[str] = list(dict.fromkeys(
            e["to"]["file"]
            for e in ego_edges
            if e["to"]["file"] != primary_file and not e["to"].get("is_test", False)
        ))
        if ego_related_files:
            candidates = [(f, 1.0) for f in ego_related_files[:4]]
            annotations = annotate_related_files(primary_file, candidates, conn)
            lines.extend(annotations[:2])  # max 2 annotation lines, same budget as briefing path

    # v1.0.5: Import-edge-first test discovery + expectations
    if primary_file:
        test_node = _find_test_for_file(conn, primary_file)
        if test_node:
            has_tested_by = any(l.startswith("TESTED BY:") for l in lines)
            if not has_tested_by:
                expectations = _extract_test_expectations(conn, test_node.file_path, primary_file)
                if expectations:
                    exp_str = ", ".join(f"{e}()" for e in expectations[:5])
                    lines.append(f"TESTED BY: {test_node.file_path} -> expects {exp_str}")
            cmd = get_test_command(test_node)
            if cmd:
                lines.append(f"RUN: {cmd}")
        else:
            # Fallback to ego-graph test discovery
            test_cmd = find_test_for_seeds(seed_node_ids, conn)
            if test_cmd:
                lines.append(f"RUN: {test_cmd}")
    else:
        test_cmd = find_test_for_seeds(seed_node_ids, conn)
        if test_cmd:
            lines.append(f"RUN: {test_cmd}")

    return format_gt_output(lines[:MAX_EVIDENCE_LINES])


def ego_graph_edit_hook(
    filepath: str, root: str, conn: sqlite3.Connection,
    first_edit: bool = True,
) -> str | None:
    """v1.0.3: Ego-graph based edit consequence map.

    1. Detect changed/deleted functions (reuse diff_aware_validation)
    2. Look up changed function node IDs (with filepath normalization)
    3. Extract ego-graph (1 hop — immediate dependents)
    4. Format as verdict (cross-file callers + risk + test)
    """
    lines: list[str] = []

    # v1.0.3: Normalize filepath — strip repo root for graph.db matching
    for prefix in [root + "/", "/testbed/", "/app/", "/home/"]:
        if filepath.startswith(prefix):
            filepath = filepath[len(prefix):]
            break
    filepath = filepath.lstrip("/")

    # 1. Diff-aware validation — keeps structural change detection
    diff_results = diff_aware_validation(filepath, root, conn)
    if diff_results:
        lines.extend(diff_results[:2])

    # 2. Try ego-graph for cross-file impact (first edit only)
    if first_edit and len(lines) < 4:
        try:
            from groundtruth.ego_graph import (
                extract_ego_graph,
                format_verdict,
            )
        except ImportError:
            # Fall through to legacy callers logic below
            pass
        else:
            # Find node IDs for functions in the edited file
            nodes = conn.execute(
                "SELECT id, name FROM nodes WHERE file_path = ? "
                "AND label IN ('Function', 'Method')",
                (filepath,),
            ).fetchall()
            # v1.0.3: suffix match fallback if exact match fails
            if not nodes:
                nodes = conn.execute(
                    "SELECT id, name FROM nodes WHERE file_path LIKE ? "
                    "AND label IN ('Function', 'Method') LIMIT 10",
                    (f"%{filepath}",),
                ).fetchall()
            if nodes:
                node_ids = [row[0] for row in nodes[:5]]
                ego_edges = extract_ego_graph(node_ids, conn, root=root, max_hops=1, verified_only=True)
                if not ego_edges:
                    ego_edges = extract_ego_graph(node_ids, conn, root=root, max_hops=1)
                # Filter to only cross-file edges
                cross_file = [e for e in ego_edges if e["from"]["file"] != e["to"]["file"]]
                if cross_file:
                    seed_names = [row[1] for row in nodes[:5]]
                    verdict = format_verdict(cross_file, seed_names=seed_names)
                    if verdict:
                        for line in verdict.splitlines():
                            if len(lines) < 3:
                                lines.append(line.strip())

    # 3. Test command
    test_node = _find_test_for_file(conn, filepath)
    if test_node:
        cmd = get_test_command(test_node)
        if cmd and len(lines) < 4:
            lines.append(f"RUN: {cmd}")

    # 4. Cross-file callers fallback (if ego-graph didn't fire)
    if first_edit and len(lines) < 4:
        target = get_target_node(conn, filepath, "")
        if target:
            total_callers, unique_files = get_all_callers_count(conn, target.id)
            if total_callers >= 2:
                resolution_counts = get_callers_by_resolution(conn, target.id)
                verdict, _ = format_impact_verdict(
                    total_callers, unique_files, resolution_counts,
                    is_critical_path(filepath),
                )
                if verdict:
                    lines.append(verdict)

    lines = lines[:4]  # hard cap

    if not lines:
        return None

    # v1.0.4b: Two-stage dedup — structural key + Jaccard similarity
    # Extract family and symbol from the first evidence line for keying
    first_line = lines[0] if lines else ""
    family = "EDIT"
    symbol = ""
    if first_line.startswith("BREAKING"):
        family = "BREAKING"
    elif first_line.startswith("RUN:"):
        family = "RUN"
    elif first_line.startswith("IMPACT"):
        family = "IMPACT"
    # Extract symbol name if present (pattern: "name() signature" or "name:")
    for token in first_line.split():
        if "(" in token or token.endswith(":"):
            symbol = token.rstrip("():,")
            break

    content = "\n".join(lines)
    if not _dedup.should_emit(family, filepath, symbol, content):
        return None

    return format_gt_output(lines)


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
    parser.add_argument("--test-command", action="store_true", help="v21: Output only the test RUN command for post-edit verification")
    parser.add_argument("--edit-hook", action="store_true", help="v21-definitive: Combined post-edit hook (validation + test + callers)")
    parser.add_argument("--first-edit", action="store_true", help="v21-definitive: First meaningful edit to this file")
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
    # v1.0.1: Try ego-graph first, fall back to legacy
    if args.enhanced_briefing:
        issue_text = _issue_body()
        identifiers = extract_identifiers_from_issue(issue_text)
        if identifiers:
            try:
                result = ego_graph_briefing(conn, args.root, identifiers)
                if result:
                    print(result)
                    conn.close()
                    return
            except Exception as e:
                print(f"[ego-graph failed: {e}]", file=sys.stderr)
            # Fallback to legacy briefing
            print(generate_enhanced_briefing(conn, args.root, identifiers))
        else:
            print(format_gt_output([], fallback_ok="No identifiers extracted from issue."))
        conn.close()
        return

    # Briefing mode — extract identifiers from issue, query graph
    if args.briefing:
        issue_text = _issue_body()
        identifiers = extract_identifiers_from_issue(issue_text)
        if identifiers:
            print(generate_pretask_briefing(conn, args.root, identifiers))
        else:
            print(format_gt_output([], fallback_ok="No identifiers extracted from issue."))
        conn.close()
        return

    # v21-definitive: edit-hook mode — combined post-edit validation
    # v1.0.1: Try ego-graph edit hook first, fall back to legacy
    if args.edit_hook and args.file:
        file_path = args.file
        if os.path.isabs(file_path):
            file_path = os.path.relpath(file_path, args.root)
        file_path = file_path.replace("\\", "/")
        try:
            result = ego_graph_edit_hook(file_path, args.root, conn, first_edit=args.first_edit)
        except Exception as e:
            print(f"[ego-graph edit hook failed: {e}]", file=sys.stderr)
            result = on_edit_hook(file_path, args.root, conn, first_edit=args.first_edit)
        if result:
            print(result)
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

    # v17: staleness detection
    staleness = check_staleness(args.db, target.file_path, args.root)

    # Compute evidence
    candidates = compute_evidence(conn, args.root, target)
    selected = rank_and_select(candidates)

    # Log evidence (always, even if suppressed)
    if args.log:
        log_evidence(candidates, selected, target, args.log, conn=conn)

    # Format and print (never silent)
    if args.test_command:
        # v21: post-edit reminder — just the test command, one line
        output = format_test_reminder(selected)
        if output:
            print(output)
        # else: no test command available — stay silent (don't waste context)
    elif args.reminder:
        print(format_reminder(selected, target, staleness_warning=staleness))
    else:
        if selected:
            print(format_output(selected, target, args.root))
        else:
            print(format_gt_output([], staleness_warning=staleness,
                                   fallback_ok="No ranked evidence for this target."))

    conn.close()


if __name__ == "__main__":
    main()
