#!/usr/bin/env python3
"""Self-contained GT context generator for mini-swe-agent.

Designed to run INSIDE the SWE-bench Docker container at /testbed.
No external dependencies — uses only Python stdlib.

Usage:
    python3 /tmp/gt_context.py "problem statement text" > /tmp/gt_context.txt

The output is a markdown block that gets prepended to the problem statement.
"""
from __future__ import annotations

import ast
import glob
import os
import re
import sys
import time


# ---------------------------------------------------------------------------
# Minimal AST parser (extracted from groundtruth.index.ast_parser)
# ---------------------------------------------------------------------------

def parse_python_file(file_path: str) -> list[dict]:
    """Parse a Python file and extract symbols using stdlib ast."""
    try:
        with open(file_path, "r", errors="replace") as f:
            source = f.read()
        if len(source) > 500_000:  # skip huge files
            return []
        tree = ast.parse(source, filename=file_path)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return []

    symbols: list[dict] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _get_func_signature(node)
            symbols.append({
                "name": node.name,
                "kind": "function",
                "line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "signature": sig,
                "return_type": _get_annotation(node.returns),
                "is_exported": not node.name.startswith("_"),
                "documentation": ast.get_docstring(node) or "",
            })
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "name": node.name,
                "kind": "class",
                "line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "signature": "",
                "return_type": "",
                "is_exported": not node.name.startswith("_"),
                "documentation": ast.get_docstring(node) or "",
            })
            # Methods
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig = _get_func_signature(item)
                    symbols.append({
                        "name": item.name,
                        "kind": "method",
                        "line": item.lineno,
                        "end_line": item.end_lineno or item.lineno,
                        "signature": sig,
                        "return_type": _get_annotation(item.returns),
                        "is_exported": not item.name.startswith("_"),
                        "documentation": ast.get_docstring(item) or "",
                        "parent": node.name,
                    })
    return symbols


def _get_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract function signature as a string."""
    args = node.args
    parts: list[str] = []
    # Regular args
    for a in args.args:
        ann = _get_annotation(a.annotation)
        parts.append(f"{a.arg}: {ann}" if ann else a.arg)
    sig = ", ".join(parts)
    return sig


def _get_annotation(node: ast.expr | None) -> str:
    """Get type annotation as string."""
    if node is None:
        return ""
    return ast.unparse(node)


# ---------------------------------------------------------------------------
# In-memory symbol index
# ---------------------------------------------------------------------------

def build_index(repo_path: str, timeout: float = 15.0) -> list[dict]:
    """Index all Python files in repo_path. Returns list of symbol dicts."""
    py_files = glob.glob(os.path.join(repo_path, "**", "*.py"), recursive=True)
    all_symbols: list[dict] = []
    start = time.monotonic()

    for fpath in py_files:
        if "/.git/" in fpath or "/__pycache__/" in fpath:
            continue
        try:
            syms = parse_python_file(fpath)
            for s in syms:
                s["file_path"] = fpath
            all_symbols.extend(syms)
        except Exception:
            continue
        if time.monotonic() - start > timeout:
            break

    return all_symbols


# ---------------------------------------------------------------------------
# Context generation
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "the", "this", "that", "with", "from", "have", "has", "been", "being",
    "will", "would", "could", "should", "does", "doesn", "into", "when",
    "where", "which", "while", "about", "after", "before", "between", "each",
    "other", "some", "such", "than", "them", "then", "there", "these",
    "they", "through", "under", "very", "what", "only", "just", "also",
    "more", "most", "make", "like", "over", "even", "back", "still",
    "well", "here", "case", "most", "need", "both", "find", "give",
    "tell", "call", "come", "take", "want", "look", "line", "file",
    "code", "test", "work", "seem", "time", "type", "used", "using",
    "none", "true", "false", "self", "args", "kwargs", "return",
    "class", "import", "function", "method", "module", "error",
    "value", "name", "list", "dict", "string", "result", "data",
}


def extract_candidates(problem_statement: str) -> set[str]:
    """Extract likely symbol names from problem text."""
    candidates: set[str] = set()
    patterns = [
        r"`([a-zA-Z_]\w+(?:\.\w+)*)`",  # backtick-quoted
        r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b",  # CamelCase
        r"\b([a-z]+_[a-z_]+)\b",  # snake_case
        r"\b([A-Z][a-zA-Z]+)\b",  # PascalCase class names
    ]
    for pat in patterns:
        for m in re.finditer(pat, problem_statement):
            name = m.group(1)
            if len(name) >= 3 and name.lower() not in _STOP_WORDS:
                candidates.add(name)
                if "." in name:
                    candidates.add(name.rsplit(".", 1)[-1])
    return candidates


def short_path(file_path: str, repo_path: str) -> str:
    """Shorten a file path relative to repo root."""
    if file_path.startswith(repo_path):
        return file_path[len(repo_path):].lstrip("/").lstrip("\\")
    return os.path.basename(file_path)


def generate_context(
    problem_statement: str,
    repo_path: str = "/testbed",
    max_symbols: int = 10,
) -> str:
    """Generate GT context block for a problem statement."""
    # Index
    all_symbols = build_index(repo_path)
    if not all_symbols:
        return ""

    # Search for relevant symbols
    candidates = extract_candidates(problem_statement)
    if not candidates:
        return ""

    # Match candidates against index
    found: list[dict] = []
    seen: set[str] = set()
    for sym in all_symbols:
        name = sym["name"]
        if name in candidates or any(c in name for c in candidates):
            key = f"{name}:{sym['file_path']}:{sym['line']}"
            if key not in seen:
                seen.add(key)
                found.append(sym)

    if not found:
        return ""

    # Deduplicate by name+file, sort by relevance (exact match first)
    def sort_key(s: dict) -> tuple:
        exact = s["name"] in candidates
        return (not exact, s["name"])

    found.sort(key=sort_key)
    found = found[:max_symbols]

    # Build context block
    n_files = len({s["file_path"] for s in all_symbols})
    lines = [
        "",
        "## Codebase Context (auto-generated by GroundTruth)",
        f"Indexed: {n_files} Python files, {len(all_symbols)} symbols.",
        "",
        "### Relevant symbols:",
    ]

    for sym in found:
        sig = f"({sym['signature']})" if sym.get("signature") else "()"
        ret = f" -> {sym['return_type']}" if sym.get("return_type") else ""
        sp = short_path(sym["file_path"], repo_path)
        parent = f" [{sym['parent']}]" if sym.get("parent") else ""
        lines.append(f"- `{sym['name']}{sig}{ret}` in {sp}{parent}")

    # Warnings: ambiguous names
    name_files: dict[str, set[str]] = {}
    for sym in found:
        name_files.setdefault(sym["name"], set()).add(
            short_path(sym["file_path"], repo_path)
        )
    warnings = []
    for name, files in name_files.items():
        if len(files) > 1:
            warnings.append(
                f"{len(files)} different `{name}` exist: {', '.join(sorted(files)[:4])}"
            )
    if warnings:
        lines.append("")
        lines.append("### Warnings:")
        for w in warnings[:3]:
            lines.append(f"- {w}")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 gt_context.py 'problem statement'", file=sys.stderr)
        sys.exit(1)

    problem = sys.argv[1]
    repo = sys.argv[2] if len(sys.argv) > 2 else "/testbed"
    context = generate_context(problem, repo)
    print(context)
