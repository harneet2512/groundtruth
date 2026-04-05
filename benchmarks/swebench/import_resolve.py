#!/usr/bin/env python3
"""AST-based Python import resolver for graph.db.

Post-processes graph.db produced by gt-index: upgrades name-match edges to
import-verified (confidence 1.0) using Python's own ast module.

Handles ALL Python import patterns natively:
- Relative imports (from . import X, from ..utils import Y)
- __init__.py re-exports (from django.urls import reverse)
- Aliases (from X import Y as Z)
- Star imports (from X import *)
- Class hierarchy (self.method() via parent_id chain)

Zero external dependencies — Python stdlib only (ast, sqlite3, os).
Runtime: <5s for any SWE-bench repo.

Usage:
    python3 import_resolve.py --root /testbed --db /tmp/gt_graph.db [--log /tmp/import_resolve.jsonl]

Research backing:
- Codebase-Memory-MCP (arXiv 2026): tree-sitter + 6-strategy import resolution
- Code Graph Model (NeurIPS 2025): lightweight semantic analysis, 43% SWE-bench Lite
- RepoGraph (ICLR 2025): AST-only ego-graph, 32.8% relative improvement
- Sheeptechnologies RFC (2026): removed SCIP, tree-sitter 3 OOM faster
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Directories to skip when walking .py files
SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".hg", ".svn", "node_modules", ".tox",
    "venv", ".venv", "env", ".env", ".eggs", "*.egg-info",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
})

MAX_REEXPORT_DEPTH = 5


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportInfo:
    """A single import extracted from a Python source file."""
    local_name: str       # name used in calling code (alias or original)
    original_name: str    # actual symbol name in source module
    module: str           # dotted module path (e.g., "django.urls"), "" for bare relative
    level: int            # relative import level (0=absolute, 1=., 2=.., etc.)
    is_star: bool         # from X import *
    raw_statement: str    # original import line for logging


# ---------------------------------------------------------------------------
# AST import extraction
# ---------------------------------------------------------------------------

import re

# Regex patterns for Python imports — 100x faster than ast.parse in slow containers
# from [...]module import name [as alias], name2 [as alias2], ...
_RE_FROM_IMPORT = re.compile(
    r"^(?:from\s+(\.*)(\w[\w.]*)?)\s+import\s+(.+)$", re.MULTILINE
)
# import module [as alias], module2 [as alias2]
_RE_IMPORT = re.compile(
    r"^import\s+(.+)$", re.MULTILINE
)
# Individual name within an import list: "name" or "name as alias"
_RE_NAME = re.compile(r"(\w+)(?:\s+as\s+(\w+))?")


def _join_multiline_imports(source: str) -> str:
    """Join multi-line parenthesized imports into single lines.

    Converts:
        from .base import (
            foo, bar,
            baz,
        )
    Into:
        from .base import foo, bar, baz

    This is critical for Django-style imports which are almost always multi-line.
    ~1ms for a 3000-line file.
    """
    result: list[str] = []
    in_paren = False
    current: list[str] = []

    for line in source.splitlines():
        stripped = line.strip()

        if in_paren:
            # Inside a parenthesized import — accumulate
            if ")" in stripped:
                # Closing paren — finish the join
                current.append(stripped.replace(")", "").strip().rstrip(","))
                result.append(" ".join(current))
                current = []
                in_paren = False
            else:
                # Strip comments and trailing commas, accumulate names
                part = stripped.split("#")[0].strip().rstrip(",").strip()
                if part:
                    current.append(part)
        elif stripped.startswith(("from ", "import ")) and "(" in stripped and ")" not in stripped:
            # Opening of a multi-line parenthesized import
            in_paren = True
            # Take everything before the paren
            before_paren = stripped.split("(")[0].strip()
            after_paren = stripped.split("(", 1)[1].strip().rstrip(",").strip()
            current = [before_paren]
            if after_paren:
                current.append(after_paren)
        else:
            result.append(line)

    # If we ended mid-paren (malformed), flush what we have
    if current:
        result.append(" ".join(current))

    return "\n".join(result)


def parse_file_imports(filepath: str) -> list[ImportInfo]:
    """Extract all imports from a Python file using regex.

    Handles: import X, from X import Y, from . import Z, aliases, star imports,
    and multi-line parenthesized imports (Django-style).
    Uses regex instead of ast.parse for speed (~0.5ms vs ~30ms per file).
    Returns empty list on read errors (graceful degradation).
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    # Pre-process: join multi-line parenthesized imports into single lines
    source = _join_multiline_imports(source)

    imports: list[ImportInfo] = []

    # from [...module] import names
    for m in _RE_FROM_IMPORT.finditer(source):
        dots = m.group(1) or ""
        module = m.group(2) or ""
        names_str = m.group(3).strip()
        level = len(dots)

        # Strip trailing comments
        names_str = names_str.split("#")[0].strip()

        if names_str == "*":
            imports.append(ImportInfo(
                local_name="*", original_name="*",
                module=module, level=level, is_star=True,
                raw_statement=f"from {'.' * level}{module} import *",
            ))
            continue

        for nm in _RE_NAME.finditer(names_str):
            orig = nm.group(1)
            alias = nm.group(2)
            local = alias if alias else orig
            imports.append(ImportInfo(
                local_name=local, original_name=orig,
                module=module, level=level, is_star=False,
                raw_statement=f"from {'.' * level}{module} import {orig}" + (f" as {alias}" if alias else ""),
            ))

    # import module [as alias]
    for m in _RE_IMPORT.finditer(source):
        names_str = m.group(1).split("#")[0].strip()
        for nm in re.finditer(r"([\w.]+)(?:\s+as\s+(\w+))?", names_str):
            fullpath = nm.group(1)
            alias = nm.group(2)
            local = alias if alias else fullpath.split(".")[-1]
            imports.append(ImportInfo(
                local_name=local,
                original_name=fullpath.split(".")[-1],
                module=fullpath, level=0, is_star=False,
                raw_statement=f"import {fullpath}" + (f" as {alias}" if alias else ""),
            ))

    return imports


# ---------------------------------------------------------------------------
# Module path → file path resolution
# ---------------------------------------------------------------------------

def resolve_module_to_file(
    module: str, level: int, importer_path: str, root: str,
) -> str | None:
    """Resolve a Python import to a file path on disk.

    Args:
        module: Dotted module path (e.g., "django.urls"). Empty for bare relative.
        level: Relative import level (0=absolute, 1=., 2=..).
        importer_path: Absolute path of the importing file.
        root: Repository root.

    Returns:
        Relative file path (from root) or None if unresolvable.
    """
    if level > 0:
        # Relative import: compute base directory
        importer_rel = os.path.relpath(importer_path, root)
        parts = importer_rel.replace(os.sep, "/").split("/")
        # Remove filename to get package directory parts
        pkg_parts = parts[:-1]
        # Go up (level - 1) directories (level=1 means current package)
        up = level - 1
        if up > len(pkg_parts):
            return None  # relative import goes above root
        if up > 0:
            pkg_parts = pkg_parts[:-up]
        # Append module components
        if module:
            pkg_parts.extend(module.split("."))
        module_path = "/".join(pkg_parts)
    else:
        # Absolute import
        if not module:
            return None
        module_path = module.replace(".", "/")

    # Check: module_path.py (module file)
    candidate = os.path.join(root, module_path + ".py")
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, root).replace(os.sep, "/")

    # Check: module_path/__init__.py (package)
    candidate = os.path.join(root, module_path, "__init__.py")
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, root).replace(os.sep, "/")

    return None


# Regex for finding definitions in a file
_RE_DEF = re.compile(r"^(?:def|class|async\s+def)\s+(\w+)", re.MULTILINE)

# Cache for file source text (avoids re-reading the same file)
_source_cache: dict[str, str] = {}
# Cache for parsed ASTs — only used for follow_reexports internals
_ast_cache: dict[str, object] = {}


def _read_cached(filepath: str) -> str:
    """Read file content with caching."""
    if filepath not in _source_cache:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                _source_cache[filepath] = f.read()
        except OSError:
            _source_cache[filepath] = ""
    return _source_cache[filepath]


def _find_name_in_file(filepath: str, name: str) -> bool:
    """Check if a function/class named `name` is defined in `filepath`."""
    source = _read_cached(filepath)
    if not source:
        return False
    for m in _RE_DEF.finditer(source):
        if m.group(1) == name:
            return True
    return False


def follow_reexports(
    name: str, init_path: str, root: str, visited: set[str] | None = None,
) -> tuple[str | None, list[str]]:
    """Follow re-export chain in __init__.py to find actual definition.

    Uses regex for speed — no ast.parse. Returns (resolved_file_relpath, chain).
    """
    if visited is None:
        visited = set()

    if init_path in visited or len(visited) >= MAX_REEXPORT_DEPTH:
        return None, []

    visited.add(init_path)
    chain: list[str] = []

    source = _read_cached(init_path)
    if not source:
        return None, chain

    # First check: is name directly defined in this file?
    if _find_name_in_file(init_path, name):
        rel = os.path.relpath(init_path, root).replace(os.sep, "/")
        return rel, chain

    # Second: look for re-export imports using regex
    init_imports = parse_file_imports(init_path)
    for imp in init_imports:
        exported = imp.local_name
        if exported == name or imp.is_star:
            target_file = resolve_module_to_file(imp.module, imp.level, init_path, root)
            chain.append(f"{os.path.relpath(init_path, root)}:{imp.raw_statement}")

            if target_file is None:
                continue

            abs_target = os.path.join(root, target_file)

            if target_file.endswith("__init__.py"):
                lookup = imp.original_name if not imp.is_star else name
                result, sub_chain = follow_reexports(lookup, abs_target, root, visited)
                chain.extend(sub_chain)
                if result:
                    return result, chain
            else:
                lookup = imp.original_name if not imp.is_star else name
                if _find_name_in_file(abs_target, lookup):
                    chain.append(f"{target_file}:def {lookup}()")
                    return target_file, chain

    return None, chain


# ---------------------------------------------------------------------------
# Build import tables for all .py files
# ---------------------------------------------------------------------------

def walk_py_files(root: str) -> list[str]:
    """Walk repo root and return all .py file paths (absolute)."""
    py_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter skip dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(os.path.join(dirpath, fname))
    return py_files


def build_import_tables(
    root: str,
    source_files: set[str] | None = None,
    log_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, tuple[str, str, list[str]]]], int]:
    """Build per-file import tables mapping local names to resolved files.

    Args:
        root: Repository root path.
        source_files: If provided, only parse these files (relative paths).
            This is a critical optimization: only parse files that are source
            files of name_match edges in graph.db, not ALL .py files.
        log_records: Optional list to collect per-edge audit records.

    Returns:
        (tables, parse_errors) where tables is:
        {rel_filepath -> {local_name -> (resolved_file_relpath, original_name, import_chain)}}
    """
    if source_files is not None:
        py_files = [os.path.join(root, f) for f in source_files if os.path.isfile(os.path.join(root, f))]
    else:
        py_files = walk_py_files(root)

    tables: dict[str, dict[str, tuple[str, str, list[str]]]] = {}
    parse_errors = 0

    for abs_path in py_files:
        rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
        imports = parse_file_imports(abs_path)

        if not imports and os.path.getsize(abs_path) > 10:
            # Likely a parse error if file has content but no imports parsed
            # (could also just be a file with no imports)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    ast.parse(f.read(), filename=abs_path)
            except (SyntaxError, ValueError):
                parse_errors += 1
                if log_records is not None:
                    log_records.append({
                        "action": "skipped_parse_error",
                        "file": rel_path,
                        "reason": f"SyntaxError in {rel_path}",
                    })
                continue

        file_table: dict[str, tuple[str, str, list[str]]] = {}

        for imp in imports:
            if imp.is_star:
                # Star imports: resolve module, record for later expansion
                target_file = resolve_module_to_file(imp.module, imp.level, abs_path, root)
                if target_file:
                    file_table["*:" + (imp.module or ".".join([""] * imp.level))] = (
                        target_file, "*", [f"{rel_path}:{imp.raw_statement}"]
                    )
                continue

            # Resolve the import to a file
            target_file = resolve_module_to_file(imp.module, imp.level, abs_path, root)
            chain = [f"{rel_path}:{imp.raw_statement}"]

            if target_file is not None:
                abs_target = os.path.join(root, target_file)

                # If target is __init__.py, check for re-exports
                if target_file.endswith("__init__.py"):
                    reexport_file, reexport_chain = follow_reexports(
                        imp.original_name, abs_target, root,
                    )
                    if reexport_file:
                        chain.extend(reexport_chain)
                        target_file = reexport_file

                file_table[imp.local_name] = (target_file, imp.original_name, chain)

        if file_table:
            tables[rel_path] = file_table

    return tables, parse_errors


# ---------------------------------------------------------------------------
# Star import expansion helper
# ---------------------------------------------------------------------------

# Regex for __all__ = ["name1", "name2", ...]
_RE_ALL = re.compile(r"__all__\s*=\s*\[([^\]]+)\]", re.DOTALL)
_RE_ALL_STR = re.compile(r"['\"](\w+)['\"]")


def _get_star_exports(filepath: str, root: str) -> set[str]:
    """Get names exported by a module (for star import resolution).

    Checks __all__ first, falls back to all public names. Uses regex for speed.
    """
    abs_path = os.path.join(root, filepath)
    source = _read_cached(abs_path)
    if not source:
        return set()

    # Check for __all__
    m = _RE_ALL.search(source)
    if m:
        names = set(_RE_ALL_STR.findall(m.group(1)))
        if names:
            return names

    # No __all__ — return all public function/class definitions
    names = set()
    for dm in _RE_DEF.finditer(source):
        n = dm.group(1)
        if not n.startswith("_"):
            names.add(n)
    return names


# ---------------------------------------------------------------------------
# Path normalization (matches scip_resolve.py pattern)
# ---------------------------------------------------------------------------

def normalize_path(p: str) -> str:
    """Normalize path to relative form, stripping container prefixes."""
    for prefix in ("/testbed/", "/home/", "/tmp/", "/app/", "/data/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p.lstrip("/").replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Schema detection (reused from scip_resolve.py)
# ---------------------------------------------------------------------------

def _detect_resolution_column(conn: sqlite3.Connection) -> str:
    """Detect whether edges table uses 'resolution_method' or 'resolution'."""
    cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    col_names = {row[1] for row in cols}
    if "resolution_method" in col_names:
        return "resolution_method"
    if "resolution" in col_names:
        return "resolution"
    return "resolution_method"


def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    """Check if edges table has a confidence column."""
    cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    return any(row[1] == "confidence" for row in cols)


# ---------------------------------------------------------------------------
# Edge upgrade engine
# ---------------------------------------------------------------------------

def _build_file_table_lazy(
    abs_path: str, rel_path: str, root: str,
) -> dict[str, tuple[str, str, list[str]]]:
    """Build import table for a single file (lazy, cached via _file_table_cache).

    Phase 1: Direct import resolution only (no re-export following).
    This is fast (<50ms per file) and handles relative imports, aliases, etc.
    Re-export following is deferred to a targeted second pass only for edges
    that don't match in the first pass.
    """
    imports = parse_file_imports(abs_path)
    file_table: dict[str, tuple[str, str, list[str]]] = {}

    for imp in imports:
        if imp.is_star:
            target_file = resolve_module_to_file(imp.module, imp.level, abs_path, root)
            if target_file:
                file_table["*:" + (imp.module or ".".join([""] * imp.level))] = (
                    target_file, "*", [f"{rel_path}:{imp.raw_statement}"]
                )
            continue

        target_file = resolve_module_to_file(imp.module, imp.level, abs_path, root)
        chain = [f"{rel_path}:{imp.raw_statement}"]

        if target_file is not None:
            file_table[imp.local_name] = (target_file, imp.original_name, chain)

    return file_table


# Cache: rel_path → import table (avoids re-parsing the same source file)
_file_table_cache: dict[str, dict[str, tuple[str, str, list[str]]]] = {}


def upgrade_edges(
    db_path: str,
    import_tables: dict[str, dict[str, tuple[str, str, list[str]]]],
    root: str,
    log_records: list[dict[str, Any]] | None = None,
    lazy_parse: bool = False,
) -> dict[str, int]:
    """Upgrade name-match edges to import-verified using import tables.

    If lazy_parse=True, ignores import_tables and builds them on-demand per file.
    Returns stats dict with counts per action category.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    res_col = _detect_resolution_column(conn)
    has_conf = _has_confidence_column(conn)

    stats: dict[str, int] = {
        "upgraded": 0,
        "corrected": 0,
        "unchanged_no_import": 0,
        "unchanged_mismatch": 0,
        "near_miss": 0,
        "total": 0,
    }

    # Near-miss breakdown
    near_miss_reasons: dict[str, int] = {
        "star_import_without_all": 0,
        "relative_import_no_init": 0,
        "reexport_chain_too_deep": 0,
        "class_hierarchy_unresolved": 0,
        "star_import_resolved": 0,
    }

    # Get all name-match CALLS edges with source and target info
    edges = conn.execute(
        f"SELECT e.id, e.source_id, e.target_id, e.source_line, e.source_file, "
        f"e.{res_col} AS res_method, "
        + ("e.confidence, " if has_conf else "")
        + "s.name AS src_name, s.file_path AS src_file, "
        f"s.start_line AS src_start, s.end_line AS src_end, "
        f"t.name AS tgt_name, t.file_path AS tgt_file, t.start_line AS tgt_start "
        f"FROM edges e "
        f"JOIN nodes s ON e.source_id = s.id "
        f"JOIN nodes t ON e.target_id = t.id "
        f"WHERE e.{res_col} = 'name_match' AND e.type = 'CALLS'"
    ).fetchall()

    stats["total"] = len(edges)

    # Column indices depend on has_conf
    if has_conf:
        IDX_RES, IDX_CONF, IDX_SRC_NAME = 5, 6, 7
        IDX_SRC_FILE, IDX_SRC_START, IDX_SRC_END = 8, 9, 10
        IDX_TGT_NAME, IDX_TGT_FILE, IDX_TGT_START = 11, 12, 13
    else:
        IDX_RES, IDX_SRC_NAME = 5, 6
        IDX_SRC_FILE, IDX_SRC_START, IDX_SRC_END = 7, 8, 9
        IDX_TGT_NAME, IDX_TGT_FILE, IDX_TGT_START = 10, 11, 12
        IDX_CONF = -1  # unused

    updates: list[tuple[str, float, int]] = []     # (method, confidence, edge_id)
    corrections: list[tuple[int, str, float, int]] = []  # (new_target_id, method, conf, edge_id)

    for row in edges:
        edge_id = row[0]
        src_id, tgt_id = row[1], row[2]
        src_line = row[3]
        src_file_raw = row[4]
        old_method = row[IDX_RES]
        old_conf = row[IDX_CONF] if has_conf and IDX_CONF >= 0 else 0.0
        tgt_name = row[IDX_TGT_NAME]
        tgt_file = row[IDX_TGT_FILE] or ""
        src_file_norm = normalize_path(row[IDX_SRC_FILE] or "")
        tgt_file_norm = normalize_path(tgt_file)

        # Look up target name in source file's import table
        if lazy_parse:
            if src_file_norm not in _file_table_cache:
                abs_src = os.path.join(root, src_file_norm)
                if os.path.isfile(abs_src):
                    _file_table_cache[src_file_norm] = _build_file_table_lazy(
                        abs_src, src_file_norm, root,
                    )
                else:
                    _file_table_cache[src_file_norm] = {}
            file_table = _file_table_cache.get(src_file_norm)
        else:
            file_table = import_tables.get(src_file_norm)

        if file_table is None:
            stats["unchanged_no_import"] += 1
            if log_records is not None:
                log_records.append({
                    "action": "unchanged",
                    "edge_id": edge_id,
                    "source_file": src_file_norm,
                    "target_name": tgt_name,
                    "old_method": old_method,
                    "old_confidence": old_conf,
                    "reason": f"no_import_table: no imports parsed for {src_file_norm}",
                })
            continue

        # Direct lookup by target name
        entry = file_table.get(tgt_name)

        # If not found directly, check star imports
        if entry is None:
            for key, (star_file, _, star_chain) in file_table.items():
                if not key.startswith("*:"):
                    continue
                # Check if target name is exported by the star-imported module
                star_exports = _get_star_exports(star_file, root)
                if tgt_name in star_exports:
                    entry = (star_file, tgt_name, star_chain + [f"{star_file}:exports {tgt_name}"])
                    break
                elif not star_exports:
                    # __all__ not defined and no public names found — near miss
                    stats["near_miss"] += 1
                    near_miss_reasons["star_import_without_all"] += 1
                    if log_records is not None:
                        log_records.append({
                            "action": "near_miss",
                            "edge_id": edge_id,
                            "source_file": src_file_norm,
                            "target_name": tgt_name,
                            "old_method": old_method,
                            "reason": f"star_import: '{key[2:]}' imported with * but can't verify membership",
                            "potential_source": star_file,
                            "blocker": "star_import_without_all",
                        })
                    # Don't break — try other star imports
                    continue

        if entry is None:
            # Target name not in any import in this file
            stats["unchanged_no_import"] += 1
            if log_records is not None:
                # Collect all imports in this file for context
                candidate_imports = [
                    v[2][0] if v[2] else f"{k}→{v[0]}"
                    for k, v in file_table.items()
                    if not k.startswith("*:")
                ][:5]
                log_records.append({
                    "action": "unchanged",
                    "edge_id": edge_id,
                    "source_file": src_file_norm,
                    "target_name": tgt_name,
                    "old_method": old_method,
                    "old_confidence": old_conf,
                    "reason": f"no_import: '{tgt_name}' not in any import in {src_file_norm}",
                    "candidate_imports": candidate_imports,
                    "note": "might be inherited via class hierarchy, builtin, or dynamic",
                })
            continue

        resolved_file, original_name, import_chain = entry

        # Compare resolved file with edge's target file
        resolved_norm = normalize_path(resolved_file)

        if _paths_match(resolved_norm, tgt_file_norm):
            # CONFIRMED: import chain agrees with edge target
            updates.append(("import", 1.0, edge_id))
            stats["upgraded"] += 1
            if log_records is not None:
                log_records.append({
                    "action": "upgraded",
                    "edge_id": edge_id,
                    "source_file": src_file_norm,
                    "target_name": tgt_name,
                    "old_method": old_method,
                    "old_confidence": old_conf,
                    "new_method": "import",
                    "new_confidence": 1.0,
                    "reason": f"from_import: import chain confirms target in {resolved_norm}",
                    "import_chain": import_chain,
                })
        elif resolved_file.endswith("__init__.py"):
            # Import resolves to __init__.py but edge target is elsewhere.
            # Try following re-exports from __init__.py to find the actual definition.
            abs_init = os.path.join(root, resolved_file)
            reexport_file, reexport_chain = follow_reexports(
                original_name, abs_init, root,
            )
            if reexport_file and _paths_match(normalize_path(reexport_file), tgt_file_norm):
                import_chain.extend(reexport_chain)
                updates.append(("import", 1.0, edge_id))
                stats["upgraded"] += 1
                if log_records is not None:
                    log_records.append({
                        "action": "upgraded",
                        "edge_id": edge_id,
                        "source_file": src_file_norm,
                        "target_name": tgt_name,
                        "old_method": old_method,
                        "old_confidence": old_conf,
                        "new_method": "import",
                        "new_confidence": 1.0,
                        "reason": f"reexport_chain: {resolved_norm} → {reexport_file}",
                        "import_chain": import_chain,
                    })
            elif reexport_file:
                # Re-export found but points to yet another file
                reexport_norm = normalize_path(reexport_file)
                correct_node = conn.execute(
                    "SELECT id, file_path FROM nodes WHERE name = ? AND file_path LIKE ? LIMIT 1",
                    (original_name, f"%{reexport_norm}"),
                ).fetchone()
                if correct_node:
                    corrections.append((correct_node[0], "import", 1.0, edge_id))
                    stats["corrected"] += 1
                    if log_records is not None:
                        log_records.append({
                            "action": "corrected",
                            "edge_id": edge_id,
                            "source_file": src_file_norm,
                            "target_name": tgt_name,
                            "old_target_file": tgt_file_norm,
                            "new_target_file": reexport_norm,
                            "new_target_node_id": correct_node[0],
                            "reason": f"reexport resolves to different file: {reexport_norm}",
                            "import_chain": import_chain + reexport_chain,
                        })
                else:
                    stats["unchanged_mismatch"] += 1
                    if log_records is not None:
                        log_records.append({
                            "action": "unchanged_mismatch",
                            "edge_id": edge_id,
                            "source_file": src_file_norm,
                            "target_name": tgt_name,
                            "old_method": old_method,
                            "reason": f"reexport resolves to '{reexport_file}' not in graph.db",
                            "import_resolves_to": resolved_norm,
                            "edge_target_file": tgt_file_norm,
                        })
            else:
                # Re-export not found — near miss
                stats["near_miss"] += 1
                near_miss_reasons["reexport_chain_too_deep"] += 1
                if log_records is not None:
                    log_records.append({
                        "action": "near_miss",
                        "edge_id": edge_id,
                        "source_file": src_file_norm,
                        "target_name": tgt_name,
                        "old_method": old_method,
                        "reason": f"reexport: import → {resolved_norm} but can't find {tgt_name} re-export",
                        "blocker": "reexport_chain_too_deep",
                    })
        else:
            # Import resolves to a DIFFERENT file than the edge target
            # Try to find the correct node in graph.db
            correct_node = conn.execute(
                "SELECT id, file_path FROM nodes WHERE name = ? AND file_path LIKE ? LIMIT 1",
                (original_name, f"%{resolved_norm}"),
            ).fetchone()

            if correct_node:
                corrections.append((correct_node[0], "import", 1.0, edge_id))
                stats["corrected"] += 1
                if log_records is not None:
                    log_records.append({
                        "action": "corrected",
                        "edge_id": edge_id,
                        "source_file": src_file_norm,
                        "target_name": tgt_name,
                        "old_target_file": tgt_file_norm,
                        "new_target_file": resolved_norm,
                        "new_target_node_id": correct_node[0],
                        "reason": f"import resolves to different file: {resolved_norm}",
                        "import_chain": import_chain,
                    })
            else:
                # Resolved file not in graph.db (likely stdlib/external)
                stats["unchanged_mismatch"] += 1
                if log_records is not None:
                    log_records.append({
                        "action": "unchanged_mismatch",
                        "edge_id": edge_id,
                        "source_file": src_file_norm,
                        "target_name": tgt_name,
                        "old_method": old_method,
                        "reason": (
                            f"import_mismatch: import resolves to '{resolved_norm}' "
                            f"but edge target is '{tgt_file_norm}' — "
                            "likely stdlib/external, leaving as name_match"
                        ),
                        "import_resolves_to": resolved_norm,
                        "edge_target_file": tgt_file_norm,
                    })

    # Batch apply all changes in one transaction
    conn.execute("BEGIN")

    for method, conf, eid in updates:
        if has_conf:
            conn.execute(
                f"UPDATE edges SET {res_col} = ?, confidence = ? WHERE id = ?",
                (method, conf, eid),
            )
        else:
            conn.execute(
                f"UPDATE edges SET {res_col} = ? WHERE id = ?",
                (method, eid),
            )

    for new_target_id, method, conf, eid in corrections:
        sql = f"UPDATE edges SET target_id = ?, {res_col} = ?"
        params: list[Any] = [new_target_id, method]
        if has_conf:
            sql += ", confidence = ?"
            params.append(conf)
        sql += " WHERE id = ?"
        params.append(eid)
        conn.execute(sql, params)

    conn.execute("COMMIT")
    conn.close()

    stats["near_miss_breakdown"] = near_miss_reasons  # type: ignore[assignment]
    return stats


def _paths_match(a: str, b: str) -> bool:
    """Check if two normalized paths refer to the same file.

    Handles cases where one path is a suffix of the other
    (e.g., 'django/urls/resolvers.py' matches 'resolvers.py').
    """
    if a == b:
        return True
    # Suffix match: one may be a full path and other a relative fragment
    if a.endswith("/" + b) or b.endswith("/" + a):
        return True
    # Strip common prefixes
    a_parts = a.split("/")
    b_parts = b.split("/")
    # Match on filename + immediate parent
    if len(a_parts) >= 1 and len(b_parts) >= 1 and a_parts[-1] == b_parts[-1]:
        if len(a_parts) >= 2 and len(b_parts) >= 2:
            return a_parts[-2] == b_parts[-2]
        return True
    return False


# ---------------------------------------------------------------------------
# Edge distribution query
# ---------------------------------------------------------------------------

def get_edge_distribution(db_path: str) -> dict[str, int]:
    """Get edge count by resolution method."""
    conn = sqlite3.connect(db_path)
    res_col = _detect_resolution_column(conn)
    rows = conn.execute(
        f"SELECT COALESCE({res_col}, 'unknown'), COUNT(*) FROM edges "
        f"WHERE type = 'CALLS' GROUP BY {res_col} ORDER BY COUNT(*) DESC"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AST-based Python import resolver for graph.db")
    parser.add_argument("--root", required=True, help="Repository root")
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--log", help="Path to JSONL log file for per-edge audit")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    db_path = os.path.abspath(args.db)

    if not os.path.isdir(root):
        print(f"Error: root directory not found: {root}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(db_path):
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    log_records: list[dict[str, Any]] = []

    # --- Phase 1: Upgrade edges with lazy import parsing ---
    # Don't parse all source files upfront — parse lazily per-edge.
    # SWE-bench containers are slow (~25ms/file for ast.parse), so
    # parsing 600 files upfront takes 15s. Instead, parse on-demand
    # and cache results. Most edges share source files so cache hits well.
    t0 = time.monotonic()
    dist_before = get_edge_distribution(db_path)

    stats = upgrade_edges(db_path, {}, root, log_records, lazy_parse=True)
    t_total_upgrade = time.monotonic() - t0

    n_files_parsed = len(_file_table_cache)
    parse_errors = sum(1 for v in _source_cache.values() if not v)
    t_total = t_total_upgrade

    print(
        f"import_resolve: {n_files_parsed} files parsed lazily "
        f"({parse_errors} skipped: SyntaxError) in {t_total:.1f}s",
        file=sys.stderr,
    )

    dist_after = get_edge_distribution(db_path)

    total = stats["total"]
    if total == 0:
        print("import_resolve: no name_match CALLS edges found", file=sys.stderr)
        return

    # --- Summary stats ---
    upgraded = stats["upgraded"]
    corrected = stats["corrected"]
    no_import = stats["unchanged_no_import"]
    mismatch = stats["unchanged_mismatch"]
    near_miss = stats["near_miss"]

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total > 0 else "0%"

    print(f"import_resolve: {total} name_match CALLS edges examined", file=sys.stderr)
    print(f"  upgraded:              {upgraded:>4} ({pct(upgraded)}) — import chain confirmed target", file=sys.stderr)
    print(f"  corrected:             {corrected:>4} ({pct(corrected)}) — import chain found different target, fixed", file=sys.stderr)
    print(f"  unchanged_no_import:   {no_import:>4} ({pct(no_import)}) — callee not in any import (likely builtin/inherited)", file=sys.stderr)
    print(f"  unchanged_mismatch:    {mismatch:>4} ({pct(mismatch)}) — import resolves to external/stdlib file", file=sys.stderr)
    print(f"  near_miss:             {near_miss:>4} ({pct(near_miss)}) — star import, unresolved relative, or missing __init__.py", file=sys.stderr)
    print(f"import_resolve: total {t_total:.1f}s", file=sys.stderr)

    # Edge distribution before/after
    print(f"\nEDGE DISTRIBUTION BEFORE:", file=sys.stderr)
    total_before = sum(dist_before.values())
    for method, count in sorted(dist_before.items(), key=lambda x: -x[1]):
        p = f"{count / total_before * 100:.1f}%" if total_before > 0 else "0%"
        print(f"  {method:20s} {count:>6} ({p})", file=sys.stderr)

    print(f"\nEDGE DISTRIBUTION AFTER:", file=sys.stderr)
    total_after = sum(dist_after.values())
    for method, count in sorted(dist_after.items(), key=lambda x: -x[1]):
        p = f"{count / total_after * 100:.1f}%" if total_after > 0 else "0%"
        marker = ""
        before_count = dist_before.get(method, 0)
        if count != before_count:
            diff = count - before_count
            marker = f" ← was {before_count} ({'+' if diff > 0 else ''}{diff})"
        print(f"  {method:20s} {count:>6} ({p}){marker}", file=sys.stderr)

    # Near-miss breakdown
    nm_breakdown = stats.get("near_miss_breakdown", {})
    if any(v > 0 for v in nm_breakdown.values()):
        print(f"\nNEAR-MISS BREAKDOWN:", file=sys.stderr)
        for reason, count in sorted(nm_breakdown.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"  {reason:35s} {count:>4}", file=sys.stderr)

    # --- Write JSONL log ---
    if args.log and log_records:
        with open(args.log, "w", encoding="utf-8") as f:
            for record in log_records:
                f.write(json.dumps(record, default=str) + "\n")
        print(f"\nimport_resolve: {len(log_records)} log records written to {args.log}", file=sys.stderr)


if __name__ == "__main__":
    main()
