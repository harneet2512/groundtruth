#!/usr/bin/env python3
"""
GroundTruth MCP — Smart Context Generator (v2)
Runs inside SWE-bench Docker container. Stdlib only. 15s budget.

Generates class structure maps with method signatures and attribute coupling
for the classes most relevant to the issue being solved.

v2 changes from v1:
- Filters out test/doc/example files (noise sources)
- Outputs class structure maps with method signatures + self.* coupling
- Returns JSON with context + full observability metrics
- Targets <300 tokens of dense structural information

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
MAX_CONTEXT_CHARS = 1200  # ~300 tokens
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tox", ".eggs", "venv", "env"}

# ─── Directories to EXCLUDE from symbol search (noise sources) ───
TEST_PATTERNS = [
    "/tests/", "/test/", "/__tests__/", "/testing/",
    "/test_", "_test.py", "_tests.py",
    "/docs/", "/doc/", "/examples/", "/example/",
    "/benchmarks/", "/bench/", "/fixtures/",
    "/conftest.py",
]


def is_test_file(filepath: str) -> bool:
    """Returns True if file is in a test/doc/example directory."""
    fp_lower = filepath.lower().replace("\\", "/")
    return any(pat in fp_lower for pat in TEST_PATTERNS)


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

        # Get base class names
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                # e.g. models.Model -> "models.Model"
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

            # Extract signature
            sig = _get_signature(item)

            # Extract self.* attribute accesses in this method body
            attrs = set()
            for child in ast.walk(item):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "self"):
                    attrs.add(child.attr)

            methods[item.name] = {
                "line": item.lineno,
                "signature": sig,
                "attrs": attrs,
            }

        if not methods:
            continue

        # Build attribute -> methods coupling (only attrs used in 2+ methods)
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
            "methods": {name: {"line": info["line"], "sig": info["signature"]}
                        for name, info in methods.items()},
            "coupling": coupled_attrs,
        })

    return classes


def _get_signature(func_node: ast.FunctionDef) -> str:
    """Extract function signature as a string."""
    args = func_node.args
    parts = []

    # Regular args
    num_defaults = len(args.defaults)
    num_args = len(args.args)
    for i, arg in enumerate(args.args):
        name = arg.arg
        if name == "self" or name == "cls":
            continue
        # Check if this arg has a default
        default_idx = i - (num_args - num_defaults)
        if 0 <= default_idx < len(args.defaults):
            default = _default_to_str(args.defaults[default_idx])
            parts.append(f"{name}={default}")
        else:
            parts.append(name)

    # *args
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            default = _default_to_str(args.kw_defaults[i])
            parts.append(f"{arg.arg}={default}")
        else:
            parts.append(arg.arg)

    # **kwargs
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


# ─── Also extract top-level functions for completeness ───


def parse_top_level_functions(tree: ast.AST, filepath: str) -> list:
    """Extract top-level function names and signatures (not in classes)."""
    funcs = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _get_signature(node)
            funcs.append({
                "name": node.name,
                "file": filepath,
                "line": node.lineno,
                "signature": sig,
            })
    return funcs


# ─── Symbol Search ───


def extract_keywords(problem_statement: str) -> set:
    """Extract likely class/function names from the problem statement."""
    # CamelCase words (likely class names)
    camel = set(re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", problem_statement))

    # Backtick-quoted identifiers
    backtick = set(re.findall(r"`(\w+)`", problem_statement))

    # snake_case identifiers that look like code
    snake = set(re.findall(r"\b([a-z_][a-z0-9_]{2,})\b", problem_statement))
    # Filter snake_case to only likely code identifiers
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

    # PascalCase single words (e.g. "Model", "Field") — only if backtick-quoted or 5+ chars
    pascal = set(re.findall(r"\b([A-Z][a-z]{3,}\w*)\b", problem_statement))
    pascal = {p for p in pascal if p in backtick or len(p) >= 5}

    return camel | backtick | snake | pascal


def rank_classes(classes: list, keywords: set) -> list:
    """Rank classes by relevance to the problem keywords."""
    scored = []
    for cls in classes:
        score = 0
        name_lower = cls["name"].lower()

        # Class name matches a keyword
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower == name_lower:
                score += 15  # exact match
            elif kw_lower in name_lower or name_lower in kw_lower:
                score += 8  # substring match
            # Method names match keywords
            for method_name in cls["methods"]:
                if kw_lower == method_name.lower():
                    score += 5  # exact method match
                elif kw_lower in method_name.lower():
                    score += 2  # substring method match

        # Bonus for classes with coupling (more methods sharing state = more complex)
        score += min(len(cls["coupling"]), 5) * 2

        # Bonus for classes with more methods (bigger API surface)
        score += min(len(cls["methods"]), 10)

        if score > 0:
            scored.append((score, cls))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [cls for _, cls in scored]


def rank_functions(funcs: list, keywords: set) -> list:
    """Rank top-level functions by relevance to keywords."""
    scored = []
    for func in funcs:
        score = 0
        name_lower = func["name"].lower()
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower == name_lower:
                score += 15
            elif kw_lower in name_lower or name_lower in kw_lower:
                score += 5
        if score > 0:
            scored.append((score, func))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored]


# ─── Context Formatting ───


def format_class_context(cls: dict, budget: int = 600) -> str:
    """Format a single class structure as a concise context block."""
    lines = []

    # Header: class name, file, line, inheritance
    bases_str = f" (extends {', '.join(cls['bases'])})" if cls["bases"] else ""
    lines.append(f"### {cls['name']}{bases_str}")
    lines.append(f"File: {cls['file']}:{cls['line']}")

    # Methods with signatures and line numbers
    lines.append("Methods:")
    for name, info in sorted(cls["methods"].items(), key=lambda x: x[1]["line"]):
        sig = info["sig"]
        # Truncate very long signatures
        if len(sig) > 80:
            sig = sig[:77] + "..."
        lines.append(f"  {name}{sig} -> line {info['line']}")

    # Attribute coupling (the key insight)
    if cls["coupling"]:
        lines.append("Shared state:")
        for attr, meths in sorted(cls["coupling"].items(),
                                   key=lambda x: len(x[1]), reverse=True):
            if len(meths) > 5:
                meths_str = ", ".join(meths[:5]) + f" (+{len(meths)-5} more)"
            else:
                meths_str = ", ".join(meths)
            lines.append(f"  self.{attr} -> {meths_str}")

    result = "\n".join(lines)
    if len(result) > budget:
        result = result[:budget - 3] + "..."
    return result


def format_function_context(funcs: list, budget: int = 200) -> str:
    """Format relevant top-level functions."""
    if not funcs:
        return ""
    lines = ["### Relevant functions:"]
    chars = len(lines[0])
    for func in funcs:
        sig = func["signature"]
        if len(sig) > 60:
            sig = sig[:57] + "..."
        line = f"  {func['name']}{sig} -> {func['file']}:{func['line']}"
        if chars + len(line) + 1 > budget:
            break
        lines.append(line)
        chars += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


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
    Main function. Index repo, find relevant classes, generate context.
    Returns dict with 'context' (str) and observability metrics.
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
        "classes_matched": 0,
        "functions_matched": 0,
        "classes_in_context": 0,
        "context_chars": 0,
        "context_tokens_approx": 0,
        "index_time_seconds": 0,
        "context_generation_time_seconds": 0,
        "total_time_seconds": 0,
    }

    # Step 1: Extract keywords from problem statement
    keywords = extract_keywords(problem_statement)
    metrics["keywords_extracted"] = len(keywords)

    # Step 2: Walk repo, parse Python files, extract class structures
    all_classes = []  # from source files
    all_test_classes = []  # from test files (tracked for metrics, not used in context)
    all_functions = []  # source top-level functions
    all_test_functions = []  # test top-level functions

    py_files = glob.glob(os.path.join(repo_path, "**", "*.py"), recursive=True)

    for filepath in py_files:
        # Skip excluded directories
        rel = os.path.relpath(filepath, repo_path)
        # Normalize path separators for matching
        rel_normalized = rel.replace("\\", "/")
        if any(skip in rel_normalized.split("/") for skip in SKIP_DIRS):
            continue

        metrics["files_scanned"] += 1

        # Skip large files
        try:
            size = os.path.getsize(filepath)
        except OSError:
            continue
        if size > MAX_FILE_SIZE:
            metrics["files_skipped_size"] += 1
            continue

        # Parse
        try:
            with open(filepath, "r", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, ValueError, RecursionError):
            metrics["files_parse_error"] += 1
            continue

        metrics["files_parsed"] += 1

        # Extract class structures
        classes = parse_class_structure(tree, rel_normalized)
        funcs = parse_top_level_functions(tree, rel_normalized)
        metrics["total_classes"] += len(classes)
        metrics["total_functions"] += len(funcs)

        for cls in classes:
            metrics["total_symbols"] += len(cls["methods"]) + 1  # +1 for class itself
        metrics["total_symbols"] += len(funcs)

        if is_test_file(rel_normalized):
            metrics["files_skipped_test"] += 1
            all_test_classes.extend(classes)
            all_test_functions.extend(funcs)
        else:
            metrics["source_classes"] += len(classes)
            metrics["source_functions"] += len(funcs)
            all_classes.extend(classes)
            all_functions.extend(funcs)

        # Time budget check
        if time.time() - start_time > MAX_TIME - 2:
            break

    metrics["index_time_seconds"] = round(time.time() - start_time, 2)

    # Step 3: Rank source classes by relevance to the issue
    ranked_classes = rank_classes(all_classes, keywords)
    metrics["classes_matched"] = len(ranked_classes)

    ranked_functions = rank_functions(all_functions, keywords)
    metrics["functions_matched"] = len(ranked_functions)

    # Step 4: Format context for top 1-2 classes + relevant functions
    gen_start = time.time()
    context_parts = []
    chars_used = 0

    # Header
    header = (
        f"## GroundTruth Codebase Analysis\n"
        f"Indexed: {metrics['files_parsed']} source files, "
        f"{metrics['source_classes']} classes, "
        f"{metrics['total_symbols']} symbols.\n"
    )
    context_parts.append(header)
    chars_used += len(header)

    for cls in ranked_classes[:2]:  # Top 2 classes max
        budget = MAX_CONTEXT_CHARS - chars_used - 150  # reserve for warnings + functions
        if budget < 100:
            break
        block = format_class_context(cls, budget=budget)
        context_parts.append(block)
        chars_used += len(block)
        metrics["classes_in_context"] += 1

    # Add relevant top-level functions if budget allows
    func_budget = MAX_CONTEXT_CHARS - chars_used - 100  # reserve for warnings
    if func_budget > 50 and ranked_functions:
        func_block = format_function_context(ranked_functions[:5], budget=func_budget)
        if func_block:
            context_parts.append(func_block)
            chars_used += len(func_block)

    # Ambiguity warnings (check across source + test classes)
    all_for_warnings = all_classes + all_test_classes
    warnings = format_ambiguity_warnings(all_for_warnings, keywords)
    if warnings:
        context_parts.append(warnings)
        chars_used += len(warnings)

    context = "\n\n".join(context_parts)

    metrics["context_chars"] = len(context)
    metrics["context_tokens_approx"] = len(context) // 4
    metrics["context_generation_time_seconds"] = round(time.time() - gen_start, 3)
    metrics["total_time_seconds"] = round(time.time() - start_time, 2)

    return {
        "context": context if metrics["classes_in_context"] > 0 or metrics["functions_matched"] > 0 else "",
        "metrics": metrics,
        "keywords": sorted(keywords)[:20],  # save top 20 for debugging
        "top_classes": [c["name"] for c in ranked_classes[:5]],  # save top 5 names
        "top_functions": [f["name"] for f in ranked_functions[:5]],
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
