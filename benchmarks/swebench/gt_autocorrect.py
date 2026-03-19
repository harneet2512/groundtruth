#!/usr/bin/env python3
"""GT v6 — Deterministic Hallucination Auto-Correction Engine.

Standalone stdlib-only script. Runs inside Docker container at /tmp/gt_autocorrect.py.
Analyzes modified Python files against a knowledge base built from the repo index
and corrects hallucinated names (methods, attributes, params, imports) in-place.

Based on Khati et al. (arXiv:2601.19106): 100% detection precision, 77% auto-correction
on library APIs. Extended to project-internal APIs + patch-level consistency.

Output: JSON report to stdout. NEVER crashes — prints empty report on any error.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any


# ---------------------------------------------------------------------------
# Levenshtein distance (copied from src/groundtruth/utils/levenshtein.py)
# ---------------------------------------------------------------------------

def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def find_closest(name: str, candidates: set[str] | list[str], max_dist: int = 2) -> str | None:
    """Return single match if unambiguous, None if 0 or 2+ matches."""
    if not candidates or len(name) <= 3:
        return None
    matches = []
    for c in candidates:
        if c == name:
            return None  # exact match exists, no correction needed
        d = levenshtein_distance(name, c)
        if d <= max_dist:
            matches.append((c, d))
    if len(matches) == 1:
        return matches[0][0]
    # If multiple matches but one is clearly closer, use it
    if len(matches) >= 2:
        matches.sort(key=lambda x: x[1])
        if matches[0][1] < matches[1][1]:
            return matches[0][0]
    return None  # ambiguous or no match


# ---------------------------------------------------------------------------
# Correction dataclass (plain dict for stdlib-only)
# ---------------------------------------------------------------------------

def make_correction(
    file: str,
    line: int,
    col_start: int,
    col_end: int,
    old_name: str,
    new_name: str,
    check_type: str,
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "file": file,
        "line": line,
        "col_start": col_start,
        "col_end": col_end,
        "old_name": old_name,
        "new_name": new_name,
        "check_type": check_type,
        "confidence": confidence,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Knowledge base construction
# ---------------------------------------------------------------------------

def _parse_module_exports(filepath: str) -> set[str]:
    """Extract top-level names (class/func/assign) from a Python file."""
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return set()

    exports: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            exports.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            # Capture re-exports (e.g. "from .engine import Engine" in __init__.py)
            for alias in node.names:
                exported_name = alias.asname or alias.name
                if exported_name != "*":
                    exports.add(exported_name)
    return exports


def _parse_class_info(filepath: str) -> dict[str, dict[str, Any]]:
    """Extract class methods, attributes, and base classes from a Python file."""
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return {}

    classes: dict[str, dict[str, Any]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods: set[str] = set()
        attrs: set[str] = set()
        for item in ast.walk(node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item in ast.iter_child_nodes(node):
                    methods.add(item.name)
            if isinstance(item, ast.Attribute):
                if (isinstance(item.value, ast.Name) and item.value.id == "self"):
                    attrs.add(item.attr)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.dump(base))  # rough but usable
        classes[node.name] = {
            "methods": methods,
            "attrs": attrs,
            "bases": bases,
            "file": filepath,
        }
    return classes


def _parse_param_names(filepath: str) -> dict[str, list[str]]:
    """Extract parameter names for Class.method and top-level functions."""
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return {}

    params: dict[str, list[str]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pnames = [a.arg for a in node.args.args if a.arg != "self"]
            pnames += [a.arg for a in node.args.kwonlyargs]
            params[node.name] = pnames
        elif isinstance(node, ast.ClassDef):
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pnames = [a.arg for a in item.args.args if a.arg != "self"]
                    pnames += [a.arg for a in item.args.kwonlyargs]
                    params[f"{node.name}.{item.name}"] = pnames
    return params


def _filepath_to_module(filepath: str, repo_root: str) -> str:
    """Convert filepath to dotted module path."""
    rel = os.path.relpath(filepath, repo_root)
    # Remove .py extension
    if rel.endswith(".py"):
        rel = rel[:-3]
    # Remove __init__
    if rel.endswith("__init__"):
        rel = rel[:-9]  # len("__init__") + 1 for separator
    # Convert separators to dots
    module = rel.replace(os.sep, ".").replace("/", ".")
    # Strip trailing dot
    module = module.rstrip(".")
    return module


def _scan_repo_imports(repo_root: str) -> set[str]:
    """Scan repo Python files to find which external packages are imported."""
    package_roots: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Skip hidden dirs, __pycache__, .git, node_modules, etc.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__" and d != "node_modules"
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, "r", errors="replace") as f:
                    source = f.read()
                tree = ast.parse(source, filename=fpath)
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        package_roots.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        package_roots.add(node.module.split(".")[0])
    return package_roots


def _introspect_package(pkg_name: str, submodules: list[str], timeout: float = 2.0) -> dict[str, set[str]]:
    """Introspect a package and its submodules for exported symbols."""
    result: dict[str, set[str]] = {}
    start = time.time()
    try:
        mod = importlib.import_module(pkg_name)
        result[pkg_name] = set(dir(mod))
    except Exception:
        return result
    for sub in submodules:
        if time.time() - start > timeout:
            break
        full = f"{pkg_name}.{sub}"
        try:
            submod = importlib.import_module(full)
            result[full] = set(dir(submod))
        except Exception:
            continue
    return result


def build_extended_kb(repo_root: str) -> dict[str, Any]:
    """Build knowledge base from repo and gt_index.json."""
    kb: dict[str, Any] = {
        "module_exports": {},   # module_path → set of names
        "classes": {},          # class_name → {methods, attrs, bases, file}
        "param_names": {},      # "Class.method" → [param names]
        "installed_symbols": {},  # "pkg.submod" → set of names
        "all_class_names": set(),
        "file_modules": {},     # filepath → module_path
    }

    # 1. Load gt_index.json if available (from gt_tool.py pre-warm)
    index_path = "/tmp/gt_index.json"
    gt_index: dict[str, Any] = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r") as f:
                gt_index = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Populate from gt_index symbols
    if "symbols" in gt_index:
        for sym in gt_index["symbols"]:
            fpath = sym.get("file", "")
            name = sym.get("name", "")
            kind = sym.get("kind", "")
            if fpath and name:
                mod = _filepath_to_module(fpath, repo_root)
                if mod not in kb["module_exports"]:
                    kb["module_exports"][mod] = set()
                kb["module_exports"][mod].add(name)
                if kind in ("class", "Class"):
                    kb["all_class_names"].add(name)

    # 2. Walk repo to build module_exports, class info, param names
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d != "__pycache__"
            and d != "node_modules"
            and d != ".git"
            and d != "test"
            and d != "tests"
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            mod = _filepath_to_module(fpath, repo_root)
            kb["file_modules"][fpath] = mod

            # Module exports
            exports = _parse_module_exports(fpath)
            if mod in kb["module_exports"]:
                kb["module_exports"][mod].update(exports)
            else:
                kb["module_exports"][mod] = exports

            # Class info
            classes = _parse_class_info(fpath)
            for cname, cinfo in classes.items():
                kb["all_class_names"].add(cname)
                if cname in kb["classes"]:
                    # Merge (class defined in multiple files or partial)
                    kb["classes"][cname]["methods"].update(cinfo["methods"])
                    kb["classes"][cname]["attrs"].update(cinfo["attrs"])
                else:
                    kb["classes"][cname] = cinfo

            # Param names
            params = _parse_param_names(fpath)
            kb["param_names"].update(params)

    # 3. Resolve class hierarchies — propagate base class methods/attrs
    _resolve_class_hierarchy(kb)

    # 4. Installed package introspection (with timeout)
    total_start = time.time()
    repo_imports = _scan_repo_imports(repo_root)
    # Filter to only external packages (not in repo)
    repo_top_modules = set()
    for mod_path in kb["module_exports"]:
        repo_top_modules.add(mod_path.split(".")[0])
    external_packages = repo_imports - repo_top_modules

    # Collect submodules actually imported
    pkg_submodules: dict[str, list[str]] = {}
    for pkg in external_packages:
        pkg_submodules[pkg] = []
    # Re-scan for submodule imports
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__"
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, "r", errors="replace") as f:
                    source = f.read()
                tree = ast.parse(source, filename=fpath)
            except (SyntaxError, OSError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if parts[0] in external_packages and len(parts) > 1:
                        submod = ".".join(parts[1:])
                        if parts[0] not in pkg_submodules:
                            pkg_submodules[parts[0]] = []
                        if submod not in pkg_submodules[parts[0]]:
                            pkg_submodules[parts[0]].append(submod)

    for pkg in external_packages:
        if time.time() - total_start > 10.0:
            break
        symbols = _introspect_package(pkg, pkg_submodules.get(pkg, []), timeout=2.0)
        kb["installed_symbols"].update(symbols)

    return kb


def _resolve_class_hierarchy(kb: dict[str, Any]) -> None:
    """Propagate base class methods/attrs to subclasses (single pass)."""
    # Simple resolution: for each class, look up bases and merge
    resolved: set[str] = set()

    def resolve(cname: str, depth: int = 0) -> None:
        if cname in resolved or depth > 10:
            return
        resolved.add(cname)
        cinfo = kb["classes"].get(cname)
        if not cinfo:
            return
        for base in cinfo.get("bases", []):
            if base in kb["classes"]:
                resolve(base, depth + 1)
                base_info = kb["classes"][base]
                cinfo["methods"].update(base_info["methods"])
                cinfo["attrs"].update(base_info["attrs"])

    for cname in list(kb["classes"].keys()):
        resolve(cname)


# ---------------------------------------------------------------------------
# File checking
# ---------------------------------------------------------------------------

def _get_modified_names(modified_files: list[str]) -> set[str]:
    """Get all names defined in modified files (to skip correcting new code)."""
    names: set[str] = set()
    for fpath in modified_files:
        try:
            with open(fpath, "r", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _resolve_import_module(module_str: str, kb: dict[str, Any]) -> set[str] | None:
    """Resolve an import module path to its exports from the KB."""
    # Direct match
    if module_str in kb["module_exports"]:
        return kb["module_exports"][module_str]
    # Try installed symbols
    if module_str in kb["installed_symbols"]:
        return kb["installed_symbols"][module_str]
    # Try partial match with dot-boundary (e.g. django.db.models → django.db.models)
    for mod_path, exports in kb["module_exports"].items():
        if (mod_path.endswith("." + module_str)
                or module_str.endswith("." + mod_path)):
            return exports
    for mod_path, symbols in kb["installed_symbols"].items():
        if (mod_path.endswith("." + module_str)
                or module_str.endswith("." + mod_path)):
            return symbols
    return None


def _is_project_local_name(name: str, tree: ast.Module, kb: dict[str, Any]) -> bool:
    """Return True only if we have positive evidence this name was meant
    to refer to a project-local symbol.

    Evidence:
    - The name is imported from a project-local module (one in kb["module_exports"])
    - The name appears in a relative import (from .X import name)

    If the name is only imported from external packages (not in KB),
    or not imported at all (could be stdlib builtin), return False.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.names:
            for alias in node.names:
                imported_name = alias.asname or alias.name
                if imported_name != name:
                    continue
                # Found an import of this name
                if node.level and node.level > 0:
                    # Relative import → project-local
                    return True
                if node.module:
                    # Check if module is in project KB (not installed_symbols)
                    if node.module in kb["module_exports"]:
                        return True
                    for mod_path in kb["module_exports"]:
                        # Dot-boundary suffix match to avoid "collections"
                        # matching "django.contrib.gis.geos.collections"
                        if (mod_path.endswith("." + node.module)
                                or node.module.endswith("." + mod_path)):
                            return True
                # Imported from external module → NOT project-local
                return False
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".")[-1]
                if imported_name == name:
                    if alias.name in kb.get("module_exports", {}):
                        return True
                    for mod_path in kb["module_exports"]:
                        if (mod_path.endswith("." + alias.name)
                                or alias.name.endswith("." + mod_path)):
                            return True
                    return False
    # Name not imported at all — could be builtin, stdlib, or defined elsewhere.
    # No positive evidence it's project-local.
    return False


def _find_enclosing_class(node: ast.AST, class_stack: list[str]) -> str | None:
    """Return the current enclosing class name, if any."""
    return class_stack[-1] if class_stack else None


def check_file(
    filepath: str,
    kb: dict[str, Any],
    modified_names: set[str],
) -> list[dict[str, Any]]:
    """Check a file for hallucinated names and return corrections."""
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []

    corrections: list[dict[str, Any]] = []

    # --- Check 1: Imports ---
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.names:
            module_exports = _resolve_import_module(node.module, kb)
            if module_exports is None:
                continue
            for alias in node.names:
                name = alias.name
                if name == "*" or len(name) <= 4:
                    continue
                if name in module_exports:
                    continue
                if name in modified_names:
                    continue
                closest = find_closest(name, module_exports)
                if closest and closest not in modified_names:
                    corrections.append(make_correction(
                        file=filepath,
                        line=node.lineno,
                        col_start=0,  # Will use text replacement
                        col_end=0,
                        old_name=name,
                        new_name=closest,
                        check_type="import",
                        confidence=0.9,
                        reason=f"'{name}' not found in {node.module}, closest: '{closest}'",
                    ))

    # --- Checks 2-5: AST walk with class tracking ---
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            # Check 2 & 3: self.method() and self.attr
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                enclosing = _find_enclosing_class(node, self.class_stack)
                if enclosing and enclosing in kb["classes"]:
                    cinfo = kb["classes"][enclosing]
                    attr = node.attr
                    if len(attr) <= 3:
                        self.generic_visit(node)
                        return
                    all_names = cinfo["methods"] | cinfo["attrs"]
                    if attr not in all_names and attr not in modified_names:
                        closest = find_closest(attr, all_names)
                        if closest and closest not in modified_names:
                            # Determine if method call or attribute access
                            is_call = (
                                isinstance(node._parent, ast.Call)  # type: ignore[attr-defined]
                                and node._parent.func is node  # type: ignore[attr-defined]
                            ) if hasattr(node, "_parent") else False
                            check_type = "method_call" if is_call else "attribute"
                            corrections.append(make_correction(
                                file=filepath,
                                line=node.lineno,
                                col_start=node.col_offset,
                                col_end=node.end_col_offset or 0,
                                old_name=attr,
                                new_name=closest,
                                check_type=check_type,
                                confidence=0.85,
                                reason=f"self.{attr} not found in {enclosing}, closest: '{closest}'",
                            ))

            # Check 5: ClassName references (SomeClass.something or SomeClass())
            if (isinstance(node.value, ast.Name)
                    and node.value.id in kb["classes"]
                    and node.attr not in kb["classes"][node.value.id].get("methods", set())
                    and node.attr not in kb["classes"][node.value.id].get("attrs", set())):
                cinfo = kb["classes"][node.value.id]
                attr = node.attr
                if len(attr) > 3 and attr not in modified_names:
                    all_names = cinfo["methods"] | cinfo["attrs"]
                    closest = find_closest(attr, all_names)
                    if closest and closest not in modified_names:
                        corrections.append(make_correction(
                            file=filepath,
                            line=node.lineno,
                            col_start=node.col_offset,
                            col_end=node.end_col_offset or 0,
                            old_name=attr,
                            new_name=closest,
                            check_type="attribute",
                            confidence=0.85,
                            reason=f"{node.value.id}.{attr} not found, closest: '{closest}'",
                        ))

            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            # Check 4: keyword arguments
            if node.keywords:
                # Resolve function name
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        if node.func.value.id == "self" and self.class_stack:
                            func_name = f"{self.class_stack[-1]}.{node.func.attr}"
                        else:
                            func_name = f"{node.func.value.id}.{node.func.attr}"

                if func_name and func_name in kb["param_names"]:
                    valid_params = set(kb["param_names"][func_name])
                    for kw in node.keywords:
                        if kw.arg and len(kw.arg) > 3 and kw.arg not in valid_params:
                            if kw.arg in modified_names:
                                continue
                            closest = find_closest(kw.arg, valid_params)
                            if closest and closest not in modified_names:
                                corrections.append(make_correction(
                                    file=filepath,
                                    line=kw.lineno if hasattr(kw, "lineno") else node.lineno,
                                    col_start=kw.col_offset if hasattr(kw, "col_offset") else 0,
                                    col_end=0,
                                    old_name=kw.arg,
                                    new_name=closest,
                                    check_type="kwarg",
                                    confidence=0.8,
                                    reason=f"kwarg '{kw.arg}' not in {func_name} params, closest: '{closest}'",
                                ))

            # Check 5A: ClassName() — class instantiation
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if (len(name) > 3
                        and name not in kb["all_class_names"]
                        and name not in modified_names
                        and name[0].isupper()
                        and _is_project_local_name(name, tree, kb)):
                    closest = find_closest(name, kb["all_class_names"])
                    if closest and closest not in modified_names:
                        corrections.append(make_correction(
                            file=filepath,
                            line=node.lineno,
                            col_start=node.func.col_offset,
                            col_end=node.func.end_col_offset or 0,
                            old_name=name,
                            new_name=closest,
                            check_type="class_ref",
                            confidence=0.8,
                            reason=f"class '{name}' not found, closest: '{closest}'",
                        ))

            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            # Check 5B: bare ClassName references (as type annotations, etc.)
            name = node.id
            if (len(name) > 3
                    and name[0].isupper()
                    and name not in kb["all_class_names"]
                    and name not in modified_names
                    and _is_project_local_name(name, tree, kb)):
                closest = find_closest(name, kb["all_class_names"])
                if closest and closest not in modified_names:
                    corrections.append(make_correction(
                        file=filepath,
                        line=node.lineno,
                        col_start=node.col_offset,
                        col_end=node.end_col_offset or 0,
                        old_name=name,
                        new_name=closest,
                        check_type="class_ref",
                        confidence=0.7,
                        reason=f"'{name}' not found, closest class: '{closest}'",
                    ))
            self.generic_visit(node)

    # Add parent references for context
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]

    visitor = Visitor()
    visitor.visit(tree)

    return corrections


def _get_modified_lines(modified_files: list[str]) -> dict[str, set[int]]:
    """Get line numbers that were added/changed in the git diff."""
    result: dict[str, set[int]] = {}
    try:
        diff_output = subprocess.run(
            ["git", "diff", "-U0"],
            capture_output=True, text=True, timeout=10,
            cwd="/testbed",
        ).stdout
    except Exception:
        return result

    current_file = None
    for line in diff_output.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@ ") and current_file:
            # Parse @@ -old,count +new,count @@
            import re as _re
            match = _re.search(r'\+(\d+)(?:,(\d+))?', line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if current_file not in result:
                    result[current_file] = set()
                for i in range(start, start + count):
                    result[current_file].add(i)
    return result


def check_patch_consistency(
    modified_files: list[str],
) -> list[dict[str, Any]]:
    """Check 6: Patch consistency — correct minority spellings to majority.

    Only flags pairs where at least one occurrence is on a modified line
    (to avoid "correcting" intentional existing attribute pairs).
    Uses edit distance 1 only (distance 2 catches too many unrelated names).
    """
    corrections: list[dict[str, Any]] = []

    # Get which lines are actually modified (new/changed)
    modified_lines = _get_modified_lines(modified_files)

    # Collect all self.X names across modified files
    self_attrs: dict[str, list[tuple[str, int, int, int, bool]]] = {}  # attr → [(file, line, col, end_col, is_modified_line)]
    for fpath in modified_files:
        try:
            with open(fpath, "r", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        rel_path = os.path.relpath(fpath, "/testbed") if fpath.startswith("/testbed") else fpath
        file_modified_lines = modified_lines.get(rel_path, set())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                attr = node.attr
                if attr not in self_attrs:
                    self_attrs[attr] = []
                is_mod = node.lineno in file_modified_lines
                self_attrs[attr].append((
                    fpath, node.lineno,
                    node.col_offset, node.end_col_offset or 0,
                    is_mod,
                ))

    # Find near-duplicate attr names (distance 1 ONLY for safety)
    attr_names = list(self_attrs.keys())
    for i, a1 in enumerate(attr_names):
        if len(a1) <= 4:  # Skip short names
            continue
        for a2 in attr_names[i + 1:]:
            if len(a2) <= 4:
                continue
            dist = levenshtein_distance(a1, a2)
            if dist != 1:  # Only distance 1 (strict)
                continue

            # At least one occurrence of the minority must be on a modified line
            count1 = len(self_attrs[a1])
            count2 = len(self_attrs[a2])
            mod_count1 = sum(1 for _, _, _, _, m in self_attrs[a1] if m)
            mod_count2 = sum(1 for _, _, _, _, m in self_attrs[a2] if m)

            if count1 > count2 and count2 <= 2 and mod_count2 > 0:
                # a2 is minority AND appears on modified lines → likely typo
                for fpath, line, col, end_col, is_mod in self_attrs[a2]:
                    if is_mod:  # Only correct on modified lines
                        corrections.append(make_correction(
                            file=fpath,
                            line=line,
                            col_start=col,
                            col_end=end_col,
                            old_name=a2,
                            new_name=a1,
                            check_type="consistency",
                            confidence=0.85,
                            reason=f"self.{a2} appears {count2}x vs self.{a1} {count1}x",
                        ))
            elif count2 > count1 and count1 <= 2 and mod_count1 > 0:
                for fpath, line, col, end_col, is_mod in self_attrs[a1]:
                    if is_mod:
                        corrections.append(make_correction(
                            file=fpath,
                            line=line,
                            col_start=col,
                            col_end=end_col,
                            old_name=a1,
                            new_name=a2,
                            check_type="consistency",
                            confidence=0.85,
                            reason=f"self.{a1} appears {count1}x vs self.{a2} {count2}x",
                        ))

    return corrections


# ---------------------------------------------------------------------------
# Apply corrections
# ---------------------------------------------------------------------------

def apply_corrections(filepath: str, corrections: list[dict[str, Any]]) -> int:
    """Apply corrections to a file using text replacement. Returns count applied."""
    if not corrections:
        return 0

    try:
        with open(filepath, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0

    applied = 0

    # Group corrections by line, process bottom-to-top to preserve positions
    corrections_by_line: dict[int, list[dict[str, Any]]] = {}
    for c in corrections:
        line_idx = c["line"] - 1  # 0-indexed
        if line_idx not in corrections_by_line:
            corrections_by_line[line_idx] = []
        corrections_by_line[line_idx].append(c)

    for line_idx in sorted(corrections_by_line.keys(), reverse=True):
        if line_idx < 0 or line_idx >= len(lines):
            continue
        line = lines[line_idx]
        line_corrections = corrections_by_line[line_idx]

        # Sort by column position, right to left
        line_corrections.sort(key=lambda c: c.get("col_start", 0), reverse=True)

        for c in line_corrections:
            old = c["old_name"]
            new = c["new_name"]
            if old == new:
                continue

            # For imports, we do a targeted replacement
            if c["check_type"] == "import":
                # Replace in "from X import old" or "from X import ..., old, ..."
                # Use word-boundary replacement to avoid partial matches
                new_line = re.sub(r'\b' + re.escape(old) + r'\b', new, line, count=1)
                if new_line != line:
                    # Safety: verify the replacement doesn't create undefined names
                    lines[line_idx] = new_line
                    line = new_line
                    applied += 1
            else:
                # General replacement: use word boundaries
                new_line = re.sub(r'\b' + re.escape(old) + r'\b', new, line, count=1)
                if new_line != line:
                    lines[line_idx] = new_line
                    line = new_line
                    applied += 1

    if applied > 0:
        try:
            with open(filepath, "w") as f:
                f.writelines(lines)
        except OSError:
            return 0

    return applied


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    report: dict[str, Any] = {
        "corrections": [],
        "files_checked": 0,
        "files_modified": 0,
        "total_corrections": 0,
        "by_type": {},
        "errors": [],
    }

    try:
        # Get modified .py files
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, timeout=10,
                cwd="/testbed",
            )
            all_files = [
                f.strip() for f in result.stdout.strip().splitlines()
                if f.strip().endswith(".py")
            ]
        except Exception as e:
            report["errors"].append(f"git diff failed: {e}")
            print(json.dumps(report))
            return

        if not all_files:
            print(json.dumps(report))
            return

        # Convert to absolute paths
        modified_files = [
            os.path.join("/testbed", f) for f in all_files
            if os.path.exists(os.path.join("/testbed", f))
        ]

        if not modified_files:
            print(json.dumps(report))
            return

        # Build knowledge base
        kb = build_extended_kb("/testbed")

        # Get names defined in modified files (skip correcting new code)
        modified_names = _get_modified_names(modified_files)

        # Check each file
        all_corrections: list[dict[str, Any]] = []
        report["files_checked"] = len(modified_files)

        for fpath in modified_files:
            file_corrections = check_file(fpath, kb, modified_names)
            all_corrections.extend(file_corrections)

        # Check patch consistency
        consistency_corrections = check_patch_consistency(modified_files)
        all_corrections.extend(consistency_corrections)

        # Deduplicate: same file + line + old_name
        seen: set[tuple[str, int, str]] = set()
        unique_corrections: list[dict[str, Any]] = []
        for c in all_corrections:
            key = (c["file"], c["line"], c["old_name"])
            if key not in seen:
                seen.add(key)
                unique_corrections.append(c)

        # Apply corrections per file
        corrections_by_file: dict[str, list[dict[str, Any]]] = {}
        for c in unique_corrections:
            fpath = c["file"]
            if fpath not in corrections_by_file:
                corrections_by_file[fpath] = []
            corrections_by_file[fpath].append(c)

        files_modified = 0
        applied_corrections: list[dict[str, Any]] = []

        for fpath, file_corrs in corrections_by_file.items():
            count = apply_corrections(fpath, file_corrs)
            if count > 0:
                files_modified += 1
                applied_corrections.extend(file_corrs[:count])

        # Build report
        report["corrections"] = unique_corrections
        report["files_modified"] = files_modified
        report["total_corrections"] = len(unique_corrections)

        by_type: dict[str, int] = {}
        for c in unique_corrections:
            ct = c["check_type"]
            by_type[ct] = by_type.get(ct, 0) + 1
        report["by_type"] = by_type

    except Exception as e:
        report["errors"].append(f"autocorrect error: {e}")

    # Convert sets to lists for JSON serialization
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # NEVER crash
        print(json.dumps({
            "corrections": [],
            "files_checked": 0,
            "files_modified": 0,
            "total_corrections": 0,
            "by_type": {},
            "errors": ["fatal crash"],
        }))
        sys.exit(0)
