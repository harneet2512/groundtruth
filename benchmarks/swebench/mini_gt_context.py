#!/usr/bin/env python3
"""
GroundTruth MCP — Change Surface Prediction (v3)
Runs inside SWE-bench Docker container. Stdlib only. 15s budget.

v3 changes from v2:
- Fix test class leakage: basename-level checks (test_*.py prefix, conftest.py)
- Fix ranking inflation: only keyword-matched classes scored, no unconditional bonus
- Single-letter class names skipped
- Coupling graph walk from entry points with per-method annotations
- Dynamic output via relevance cliff (not fixed 300-token budget)

Usage:
    python3 /tmp/gt_context.py /testbed /tmp/gt_problem.txt
    # Reads problem statement from file, outputs JSON to stdout
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys
import time

MAX_TIME = 15  # seconds
MAX_FILE_SIZE = 500_000  # bytes
MAX_CONTEXT_CHARS = 2000  # ~500 tokens safety cap
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tox", ".eggs", "venv", "env"}

# ─── Test / noise file detection ───

# Directory patterns (substring match on path)
SKIP_DIR_PATTERNS = [
    "/tests/", "/test/", "/__tests__/", "/testing/",
    "/docs/", "/doc/", "/examples/", "/example/",
    "/benchmarks/", "/bench/", "/fixtures/",
    "/migrations/",
]

# File-level patterns (substring match on path)
SKIP_FILE_PATTERNS = [
    "/test_", "_test.py", "_tests.py",
]


def is_test_file(filepath: str) -> bool:
    """Returns True if file is in a test/doc/example directory or is a test file."""
    fp_lower = filepath.lower().replace("\\", "/")

    # Directory-level patterns
    if any(pat in fp_lower for pat in SKIP_DIR_PATTERNS):
        return True

    # File-level substring patterns
    if any(pat in fp_lower for pat in SKIP_FILE_PATTERNS):
        return True

    # Basename-level checks (catches test_requests.py at repo root)
    basename = os.path.basename(fp_lower)
    if basename.startswith("test_"):
        return True
    if basename == "conftest.py":
        return True

    return False


# ─── AST Parsing ───


def parse_class_structure(tree: ast.AST, filepath: str) -> list:
    """
    Extract class structure: methods, signatures, and self.* attribute coupling.
    Returns list of class info dicts.
    """
    classes = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Skip single-letter class names (too ambiguous)
        if len(node.name) <= 1:
            continue

        # Get base class names
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                parts = []
                cur = base
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                bases.append(".".join(reversed(parts)))

        methods = {}
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            sig = _get_signature(item)

            # Extract self.* attribute accesses
            attrs = set()
            for child in ast.walk(item):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "self"):
                    attrs.add(child.attr)

            # Extract calls to self.method_name()
            calls = set()
            for child in ast.walk(item):
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "self"):
                    calls.add(child.func.attr)

            methods[item.name] = {
                "line": item.lineno,
                "signature": sig,
                "attrs": attrs,
                "calls": calls,
            }

        if not methods:
            continue

        # Build attribute -> methods coupling (attrs used in 2+ methods)
        attr_coupling = {}
        for method_name, info in methods.items():
            for attr in info["attrs"]:
                attr_coupling.setdefault(attr, []).append(method_name)

        coupled_attrs = {
            attr: sorted(meths)
            for attr, meths in attr_coupling.items()
            if len(meths) >= 2
        }

        classes.append({
            "name": node.name,
            "file": filepath,
            "line": node.lineno,
            "bases": bases,
            "methods": methods,
            "coupling": coupled_attrs,
        })

    return classes


def _get_signature(func_node: ast.FunctionDef) -> str:
    """Extract function signature as a string."""
    args = func_node.args
    parts = []

    num_defaults = len(args.defaults)
    num_args = len(args.args)
    for i, arg in enumerate(args.args):
        name = arg.arg
        if name == "self" or name == "cls":
            continue
        default_idx = i - (num_args - num_defaults)
        if 0 <= default_idx < len(args.defaults):
            default = _default_to_str(args.defaults[default_idx])
            parts.append(f"{name}={default}")
        else:
            parts.append(name)

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    for i, arg in enumerate(args.kwonlyargs):
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            default = _default_to_str(args.kw_defaults[i])
            parts.append(f"{arg.arg}={default}")
        else:
            parts.append(arg.arg)

    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return f"({', '.join(parts)})"


def _default_to_str(node: ast.AST) -> str:
    """Convert an AST default value node to a short string."""
    if isinstance(node, ast.Constant):
        r = repr(node.value)
        return r if len(r) < 20 else r[:17] + "..."
    elif isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, (ast.List, ast.Tuple)):
        return "[]" if isinstance(node, ast.List) else "()"
    elif isinstance(node, ast.Dict):
        return "{}"
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return f"{node.func.id}()"
        return "..."
    return "..."


# ─── Entry Point Finding (replaces rank_classes) ───


def find_entry_points(classes: list, keywords: set, max_entries: int = 3) -> list:
    """
    Find top entry-point classes based on keyword matches only.
    No unconditional bonuses — a class must match a keyword to score at all.
    Returns list of (score, class_dict) tuples, sorted by score descending.
    """
    scored = []
    for cls in classes:
        score = 0
        name_lower = cls["name"].lower()

        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower == name_lower:
                score += 15  # exact class name match
            elif kw_lower in name_lower or name_lower in kw_lower:
                score += 8  # substring match

            # Method name matches
            for method_name in cls["methods"]:
                if kw_lower == method_name.lower():
                    score += 5
                elif kw_lower in method_name.lower():
                    score += 2

        # Only include classes that actually matched something
        if score > 0:
            scored.append((score, cls))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:max_entries]


# ─── Change Surface Computation ───


def compute_change_surface(cls: dict) -> list:
    """
    From an entry-point class, compute which methods are most likely to need changes.

    Scores each method by:
    - Shared attribute count × 3 (methods sharing state are coupled)
    - Call coupling × 2 (methods calling each other)
    - Caller coupling × 2 (methods called by others in the class)

    Returns list of (method_name, score, reasons) sorted by score descending.
    """
    methods = cls["methods"]
    coupling = cls["coupling"]

    surface = []
    for method_name, info in methods.items():
        score = 0
        reasons = []

        # 1. Shared attributes: how many coupled attrs does this method touch?
        shared_attrs = []
        for attr, meths in coupling.items():
            if method_name in meths:
                shared_attrs.append(attr)

        if shared_attrs:
            score += len(shared_attrs) * 3
            reasons.append(f"shares self.{', self.'.join(sorted(shared_attrs[:5]))}")

        # 2. Call coupling: does this method call other methods in the class?
        calls_in_class = info["calls"] & set(methods.keys())
        if calls_in_class:
            score += len(calls_in_class) * 2
            reasons.append(f"calls {', '.join(sorted(calls_in_class)[:3])}")

        # 3. Caller coupling: is this method called by other methods?
        callers = []
        for other_name, other_info in methods.items():
            if other_name != method_name and method_name in other_info["calls"]:
                callers.append(other_name)
        if callers:
            score += len(callers) * 2
            reasons.append(f"called by {', '.join(sorted(callers)[:3])}")

        surface.append((method_name, score, reasons))

    surface.sort(key=lambda x: x[1], reverse=True)
    return surface


# ─── Keyword Extraction ───


def extract_keywords(problem_statement: str) -> set:
    """Extract likely class/function names from the problem statement."""
    # CamelCase words (likely class names)
    camel = set(re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", problem_statement))

    # Backtick-quoted identifiers
    backtick = set(re.findall(r"`(\w+)`", problem_statement))

    # snake_case identifiers that look like code
    snake = set(re.findall(r"\b([a-z_][a-z0-9_]{2,})\b", problem_statement))
    code_words = {
        "error", "the", "that", "this", "with", "from", "have", "been",
        "should", "would", "could", "which", "when", "where", "what",
        "into", "than", "then", "also", "only", "just", "like", "some",
        "other", "about", "because", "does", "not", "but", "for", "are",
        "was", "were", "will", "can", "all", "each", "they", "them",
        "their", "there", "here", "very", "still", "already", "however",
        "using", "used", "need", "make", "case", "work", "want", "look",
        "line", "file", "code", "test", "time", "type", "none", "true",
        "false", "self", "args", "kwargs", "return", "class", "import",
        "function", "method", "module", "value", "name", "list", "dict",
        "string", "result", "data", "seems", "instead", "expected",
        "actually", "think", "sure", "even", "same", "first", "last",
        "next", "following", "above", "below",
    }
    snake = {s for s in snake if s not in code_words and ("_" in s or s in backtick)}

    # PascalCase single words — only if backtick-quoted or 5+ chars
    pascal = set(re.findall(r"\b([A-Z][a-z]{3,}\w*)\b", problem_statement))
    pascal = {p for p in pascal if p in backtick or len(p) >= 5}

    return camel | backtick | snake | pascal


# ─── Context Formatting ───


def format_entry_context(cls: dict, surface: list) -> str:
    """
    Format an entry-point class with its change surface.
    Uses relevance cliff: include methods until score < 30% of top score.
    Each method block is complete — never truncated mid-block.
    """
    lines = []

    # Header
    bases_str = f" (extends {', '.join(cls['bases'])})" if cls["bases"] else ""
    lines.append(f"### {cls['name']}{bases_str}")
    lines.append(f"File: {cls['file']}:{cls['line']}")

    if not surface:
        return "\n".join(lines)

    # Relevance cliff: include methods until score < 30% of top
    top_score = surface[0][1] if surface else 0
    cliff_threshold = top_score * 0.3

    lines.append("Change surface:")
    methods_shown = 0
    for method_name, score, reasons in surface:
        if score < cliff_threshold and methods_shown > 0:
            break
        if methods_shown >= 15:
            break

        info = cls["methods"][method_name]
        sig = info["signature"]
        if len(sig) > 80:
            sig = sig[:77] + "..."

        method_line = f"  {method_name}{sig} -> line {info['line']}"
        if reasons:
            method_line += f"  # {'; '.join(reasons)}"
        lines.append(method_line)
        methods_shown += 1

    return "\n".join(lines)


def format_ambiguity_warnings(classes: list, keywords: set) -> str:
    """Warn about symbols that exist in multiple source files."""
    name_locations = {}
    for cls in classes:
        name_locations.setdefault(cls["name"], []).append(cls["file"])

    warnings = []
    for name, files in name_locations.items():
        if len(files) >= 2 and any(kw.lower() in name.lower() for kw in keywords):
            files_short = files[:3]
            warnings.append(f"Warning: {len(files)} different `{name}`: {', '.join(files_short)}")

    return "\n".join(warnings)


# ─── Main Entry Point ───


def generate_context(repo_path: str, problem_statement: str) -> dict:
    """
    Main function. Index repo, find entry points, compute change surface.
    Returns dict with 'context' (str), 'metrics', and 'debug' info.
    """
    start_time = time.time()

    metrics = {
        "files_scanned": 0,
        "files_parsed": 0,
        "files_skipped_test": 0,
        "files_skipped_size": 0,
        "files_parse_error": 0,
        "total_classes": 0,
        "source_classes": 0,
        "total_functions": 0,
        "source_functions": 0,
        "total_symbols": 0,
        "keywords_extracted": 0,
        "entry_points_found": 0,
        "surface_methods": 0,
        "context_chars": 0,
        "context_tokens_approx": 0,
        "index_time_seconds": 0,
        "context_generation_time_seconds": 0,
        "total_time_seconds": 0,
    }

    # Step 1: Extract keywords
    keywords = extract_keywords(problem_statement)
    metrics["keywords_extracted"] = len(keywords)

    # Step 2: Walk repo, parse Python files, extract class structures
    all_classes = []

    py_files = glob.glob(os.path.join(repo_path, "**", "*.py"), recursive=True)

    for filepath in py_files:
        rel = os.path.relpath(filepath, repo_path)
        rel_normalized = rel.replace("\\", "/")
        if any(skip in rel_normalized.split("/") for skip in SKIP_DIRS):
            continue

        metrics["files_scanned"] += 1

        try:
            size = os.path.getsize(filepath)
        except OSError:
            continue
        if size > MAX_FILE_SIZE:
            metrics["files_skipped_size"] += 1
            continue

        try:
            with open(filepath, "r", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, ValueError, RecursionError):
            metrics["files_parse_error"] += 1
            continue

        metrics["files_parsed"] += 1

        classes = parse_class_structure(tree, rel_normalized)
        metrics["total_classes"] += len(classes)

        # Count symbols
        for cls in classes:
            metrics["total_symbols"] += len(cls["methods"]) + 1

        if is_test_file(rel_normalized):
            metrics["files_skipped_test"] += 1
            # Don't add test classes at all
        else:
            metrics["source_classes"] += len(classes)
            all_classes.extend(classes)

        # Time budget check
        if time.time() - start_time > MAX_TIME - 2:
            break

    metrics["index_time_seconds"] = round(time.time() - start_time, 2)

    # Step 3: Find entry points (keyword-matched classes only)
    entry_points = find_entry_points(all_classes, keywords, max_entries=3)
    metrics["entry_points_found"] = len(entry_points)

    # Step 4: Compute change surface for each entry point and format context
    gen_start = time.time()
    context_parts = []
    chars_used = 0
    top_surface = []  # for debug output

    # Header
    header = (
        f"## GroundTruth Change Surface Analysis\n"
        f"Indexed: {metrics['files_parsed']} source files, "
        f"{metrics['source_classes']} classes, "
        f"{metrics['total_symbols']} symbols.\n"
    )
    context_parts.append(header)
    chars_used += len(header)

    for _score, cls in entry_points:
        surface = compute_change_surface(cls)
        block = format_entry_context(cls, surface)

        if chars_used + len(block) > MAX_CONTEXT_CHARS:
            break

        context_parts.append(block)
        chars_used += len(block)
        metrics["surface_methods"] += sum(1 for _, s, _ in surface if s >= (surface[0][1] * 0.3 if surface else 0))

        # Debug: record surface for this entry point
        top_surface.append({
            "class": cls["name"],
            "file": cls["file"],
            "entry_score": _score,
            "methods": [
                {"name": m, "score": s, "reasons": r}
                for m, s, r in surface[:10]
            ],
        })

    # Ambiguity warnings
    warnings = format_ambiguity_warnings(all_classes, keywords)
    if warnings and chars_used + len(warnings) < MAX_CONTEXT_CHARS:
        context_parts.append(warnings)
        chars_used += len(warnings)

    context = "\n\n".join(context_parts)

    metrics["context_chars"] = len(context)
    metrics["context_tokens_approx"] = len(context) // 4
    metrics["context_generation_time_seconds"] = round(time.time() - gen_start, 3)
    metrics["total_time_seconds"] = round(time.time() - start_time, 2)

    # Build debug info
    debug = {
        "keywords": sorted(keywords)[:20],
        "entry_points": [
            {"class": cls["name"], "file": cls["file"], "score": sc}
            for sc, cls in entry_points
        ],
        "top_surface": top_surface,
        "all_classes_found": len(all_classes),
    }

    has_content = metrics["entry_points_found"] > 0

    return {
        "context": context if has_content else "",
        "metrics": metrics,
        "debug": debug,
    }


if __name__ == "__main__":
    repo_path = sys.argv[1] if len(sys.argv) > 1 else "/testbed"
    problem_source = sys.argv[2] if len(sys.argv) > 2 else ""

    # If problem_source is a file path, read it; otherwise treat as inline text
    if problem_source and os.path.isfile(problem_source):
        with open(problem_source, errors="replace") as f:
            problem = f.read()
    else:
        problem = problem_source

    result = generate_context(repo_path, problem)

    # Output: JSON to stdout
    print(json.dumps(result))
