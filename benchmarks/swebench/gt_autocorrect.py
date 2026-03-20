#!/usr/bin/env python3
"""GT vNext — Certainty-Layered Hallucination Auto-Correction Engine.

Standalone stdlib-only script. Runs inside Docker container at /tmp/gt_autocorrect.py.
Analyzes modified Python files against a knowledge base built from the repo index
and corrects hallucinated names ONLY when operating on closed-world (green-lane) facts.

Key change from v6: Checks 5A, 5B, 5C (class_ref, bare ClassName, func_call) are
DISABLED — they operate on open sets and produce ~98% false positives.
Only corrections on `+` lines that don't touch agent-defined or renamed names are applied.

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
# PatchOverlay (inlined — stdlib-only)
# ---------------------------------------------------------------------------

_DEF_RE = re.compile(
    r"^(?:\s*)"
    r"(?:(?:class|def|async\s+def)\s+([A-Za-z_]\w*)"
    r"|([A-Z]\w*)\s*=)"
)
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(\S+)\s+import\s+(.+)|import\s+(.+))"
)


def _extract_definitions(lines: list[str]) -> set[str]:
    defs: set[str] = set()
    for line in lines:
        m = _DEF_RE.match(line)
        if m:
            name = m.group(1) or m.group(2)
            if name:
                defs.add(name)
    return defs


def _extract_imports(lines: list[str]) -> set[str]:
    imports: set[str] = set()
    for line in lines:
        m = _IMPORT_RE.match(line)
        if not m:
            continue
        if m.group(1):
            names_part = m.group(2)
            for name in names_part.split(","):
                name = name.strip()
                if " as " in name:
                    name = name.split(" as ")[-1].strip()
                if name and name != "*":
                    imports.add(name)
        elif m.group(3):
            for name in m.group(3).split(","):
                name = name.strip()
                if " as " in name:
                    name = name.split(" as ")[-1].strip()
                if name:
                    imports.add(name.split(".")[-1])
    return imports


def _detect_renames(added: set[str], removed: set[str], max_dist: int = 3) -> dict[str, str]:
    renames: dict[str, str] = {}
    used_removed: set[str] = set()
    for new_name in added:
        best: str | None = None
        best_dist = max_dist + 1
        for old_name in removed:
            if old_name in used_removed:
                continue
            d = levenshtein_distance(new_name, old_name)
            if d <= max_dist and d < best_dist:
                best = old_name
                best_dist = d
        if best is not None:
            renames[new_name] = best
            used_removed.add(best)
    return renames


def build_patch_overlay(repo_root: str = "/testbed") -> dict[str, Any]:
    """Build patch overlay from current git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "-U0"],
            capture_output=True, text=True, timeout=10,
            cwd=repo_root,
        )
        diff_text = result.stdout
    except Exception:
        return {
            "added_definitions": set(),
            "removed_definitions": set(),
            "renames": {},
            "added_imports": set(),
            "removed_imports": set(),
            "added_lines": {},
            "changed_files": [],
        }

    added_source_lines: list[str] = []
    removed_source_lines: list[str] = []
    added_import_lines: list[str] = []
    added_lines: dict[str, set[int]] = {}
    changed_files: list[str] = []

    current_file: str | None = None
    current_new_line = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            if current_file not in changed_files:
                changed_files.append(current_file)
        elif raw_line.startswith("@@ ") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            if m:
                current_new_line = int(m.group(1))
            else:
                current_new_line = 0
        elif current_file and raw_line.startswith("+") and not raw_line.startswith("+++"):
            content = raw_line[1:]
            added_source_lines.append(content)
            if _IMPORT_RE.match(content.strip()):
                added_import_lines.append(content)
            if current_file not in added_lines:
                added_lines[current_file] = set()
            added_lines[current_file].add(current_new_line)
            current_new_line += 1
        elif current_file and raw_line.startswith("-") and not raw_line.startswith("---"):
            content = raw_line[1:]
            removed_source_lines.append(content)

    added_defs = _extract_definitions(added_source_lines)
    removed_defs = _extract_definitions(removed_source_lines)
    renames = _detect_renames(added_defs, removed_defs)

    return {
        "added_definitions": added_defs,
        "removed_definitions": removed_defs,
        "renames": renames,
        "added_imports": _extract_imports(added_import_lines),
        "removed_imports": set(),
        "added_lines": added_lines,
        "changed_files": changed_files,
    }


# ---------------------------------------------------------------------------
# Pyright integration (optional)
# ---------------------------------------------------------------------------

def _try_pyright(files: list[str], cwd: str = "/testbed") -> list[dict[str, Any]]:
    """Run Pyright on files, return green-lane diagnostics. Graceful degradation."""
    _GREEN_RULES = {
        "reportAttributeAccessIssue",
        "reportMissingImports",
        "reportUndefinedVariable",
        "reportGeneralTypeIssues",
    }
    try:
        result = subprocess.run(
            ["pyright", "--outputjson"] + files,
            capture_output=True, text=True, timeout=30,
            cwd=cwd,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError, ValueError):
        return []

    diagnostics: list[dict[str, Any]] = []
    for diag in data.get("generalDiagnostics", []):
        rule = diag.get("rule", "")
        if rule not in _GREEN_RULES:
            continue
        diagnostics.append({
            "file": diag.get("file", ""),
            "line": diag.get("range", {}).get("start", {}).get("line", 0),
            "rule": rule,
            "message": diag.get("message", ""),
        })
    return diagnostics


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
# Green-lane gate
# ---------------------------------------------------------------------------

def _should_correct(
    correction: dict[str, Any],
    overlay: dict[str, Any],
) -> bool:
    """Gate function: only allow corrections that pass green-lane checks.

    A correction is allowed only if:
    1. The corrected line is a `+` line (added by the agent)
    2. The old name is NOT something the agent explicitly defined
    3. The old name is NOT the target of a rename
    """
    file_path = correction["file"]
    line_num = correction["line"]
    old_name = correction["old_name"]

    # 1. Must be on an added line
    file_added_lines = overlay.get("added_lines", {}).get(file_path, set())
    # Also check with /testbed prefix stripped
    if not file_added_lines:
        rel_path = file_path
        if rel_path.startswith("/testbed/"):
            rel_path = rel_path[len("/testbed/"):]
        file_added_lines = overlay.get("added_lines", {}).get(rel_path, set())
    if line_num not in file_added_lines:
        return False

    # 2. Not agent-defined
    if old_name in overlay.get("added_definitions", set()):
        return False

    # 3. Not a rename target
    if old_name in overlay.get("renames", {}).values():
        return False

    return True


# ---------------------------------------------------------------------------
# Knowledge base construction
# ---------------------------------------------------------------------------

def _parse_module_exports(filepath: str) -> set[str]:
    """Extract top-level names (class/func/assign) from a Python file.

    Handles star imports from relative modules by resolving them to sibling files.
    """
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return set()

    exports: set[str] = set()
    star_sources: list[str] = []

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
            has_star = False
            for alias in node.names:
                exported_name = alias.asname or alias.name
                if exported_name == "*":
                    has_star = True
                else:
                    exports.add(exported_name)
            if has_star and node.module and node.level and node.level > 0:
                star_sources.append(node.module)

    if star_sources:
        dir_path = os.path.dirname(filepath)
        for mod_name in star_sources:
            candidates = [
                os.path.join(dir_path, mod_name + ".py"),
                os.path.join(dir_path, mod_name, "__init__.py"),
            ]
            for candidate in candidates:
                if os.path.exists(candidate):
                    try:
                        with open(candidate, "r", errors="replace") as f:
                            sub_source = f.read()
                        sub_tree = ast.parse(sub_source, filename=candidate)
                        for sub_node in ast.iter_child_nodes(sub_tree):
                            if isinstance(sub_node, ast.Assign):
                                for target in sub_node.targets:
                                    if isinstance(target, ast.Name) and target.id == "__all__":
                                        if isinstance(sub_node.value, (ast.List, ast.Tuple)):
                                            for elt in sub_node.value.elts:
                                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                                    exports.add(elt.value)
                            elif isinstance(sub_node, ast.ClassDef):
                                exports.add(sub_node.name)
                            elif isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                exports.add(sub_node.name)
                    except (SyntaxError, OSError, UnicodeDecodeError):
                        pass
                    break

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
        for item in ast.iter_child_nodes(node):
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attrs.add(target.id)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attrs.add(item.target.id)
            elif isinstance(item, ast.ClassDef):
                attrs.add(item.name)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.dump(base))
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
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("__init__"):
        rel = rel[:-9]
    module = rel.replace(os.sep, ".").replace("/", ".")
    module = module.rstrip(".")
    return module


def _scan_repo_imports(repo_root: str) -> set[str]:
    """Scan repo Python files to find which external packages are imported."""
    package_roots: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_root):
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
        "module_exports": {},
        "classes": {},
        "param_names": {},
        "installed_symbols": {},
        "all_class_names": set(),
        "file_modules": {},
    }

    # 1. Load gt_index.json if available
    index_path = "/tmp/gt_index.json"
    gt_index: dict[str, Any] = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r") as f:
                gt_index = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

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

    # 2. Walk repo
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

            exports = _parse_module_exports(fpath)
            if mod in kb["module_exports"]:
                kb["module_exports"][mod].update(exports)
            else:
                kb["module_exports"][mod] = exports

            classes = _parse_class_info(fpath)
            for cname, cinfo in classes.items():
                kb["all_class_names"].add(cname)
                if cname in kb["classes"]:
                    kb["classes"][cname]["methods"].update(cinfo["methods"])
                    kb["classes"][cname]["attrs"].update(cinfo["attrs"])
                else:
                    kb["classes"][cname] = cinfo

            params = _parse_param_names(fpath)
            kb["param_names"].update(params)

    # 3. Resolve class hierarchies
    _resolve_class_hierarchy(kb)

    # 3.5. Merge runtime introspection KB (ground truth from dir()/inspect)
    _merge_runtime_kb(kb)

    # 4. Installed package introspection
    total_start = time.time()
    repo_imports = _scan_repo_imports(repo_root)
    repo_top_modules = set()
    for mod_path in kb["module_exports"]:
        repo_top_modules.add(mod_path.split(".")[0])
    external_packages = repo_imports - repo_top_modules

    pkg_submodules: dict[str, list[str]] = {}
    for pkg in external_packages:
        pkg_submodules[pkg] = []
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


def _merge_runtime_kb(kb: dict[str, Any]) -> None:
    """Merge runtime introspection KB into the AST-built KB.

    The runtime KB (built by gt_runtime_kb.py) uses dir()/inspect to find all
    class members including metaclass-injected, mixin-provided, and descriptor
    methods that AST parsing fundamentally misses.

    This is the key fix for false positives: when the runtime KB says a method
    exists on a class, we trust it — Python's own runtime is the oracle.
    """
    runtime_kb_path = "/tmp/gt_runtime_kb.json"
    if not os.path.exists(runtime_kb_path):
        return

    try:
        with open(runtime_kb_path, "r") as f:
            runtime_kb = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    runtime_classes = runtime_kb.get("classes", {})
    if not runtime_classes:
        return

    for class_name, runtime_info in runtime_classes.items():
        runtime_methods = set(runtime_info.get("methods", []))
        runtime_attrs = set(runtime_info.get("attrs", []))
        runtime_params = runtime_info.get("params", {})

        if class_name in kb["classes"]:
            # Merge: runtime KB ADDS members that AST missed (never removes)
            kb["classes"][class_name]["methods"].update(runtime_methods)
            kb["classes"][class_name]["attrs"].update(runtime_attrs)
        else:
            # New class only known from runtime (metaclass-generated, etc.)
            kb["classes"][class_name] = {
                "methods": runtime_methods,
                "attrs": runtime_attrs,
                "bases": runtime_info.get("bases", []),
                "file": runtime_info.get("file", ""),
                "source": "runtime",
            }
        kb["all_class_names"].add(class_name)

        # Merge param names from runtime signatures
        for method_name, params in runtime_params.items():
            qualified = f"{class_name}.{method_name}"
            if qualified not in kb["param_names"]:
                kb["param_names"][qualified] = params


def _resolve_class_hierarchy(kb: dict[str, Any]) -> None:
    """Propagate base class methods/attrs to subclasses (single pass)."""
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
# Import resolution
# ---------------------------------------------------------------------------

def _resolve_import_module(module_str: str, kb: dict[str, Any]) -> set[str] | None:
    """Resolve an import module path to its exports from the KB."""
    if module_str in kb["module_exports"]:
        return kb["module_exports"][module_str]
    if module_str in kb["installed_symbols"]:
        return kb["installed_symbols"][module_str]
    suffix = "." + module_str
    for mod_path, exports in kb["module_exports"].items():
        if mod_path.endswith(suffix) or mod_path == module_str.split(".")[-1]:
            return exports
    return None


# ---------------------------------------------------------------------------
# File checking — GREEN-LANE ONLY
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


def _find_enclosing_class(node: ast.AST, class_stack: list[str]) -> str | None:
    """Return the current enclosing class name, if any."""
    return class_stack[-1] if class_stack else None


def check_file_green_only(
    filepath: str,
    kb: dict[str, Any],
    modified_names: set[str],
) -> list[dict[str, Any]]:
    """Check a file for hallucinated names — GREEN-LANE CHECKS ONLY.

    ENABLED:  Check 1 (imports), Check 2 (self.method), Check 3 (self.attr), Check 4 (kwargs)
    DISABLED: Check 5A (ClassName()), Check 5B (bare ClassName), Check 5C (func_call())
    Check 6 (consistency) is handled separately.
    """
    try:
        with open(filepath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []

    corrections: list[dict[str, Any]] = []

    # --- Check 1: Imports (GREEN: module exports are a closed set) ---
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.names:
            module_exports = _resolve_import_module(node.module, kb)
            if module_exports is None:
                continue
            for alias in node.names:
                name = alias.name
                if name == "*" or len(name) <= 2:
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
                        col_start=0,
                        col_end=0,
                        old_name=name,
                        new_name=closest,
                        check_type="import",
                        confidence=0.9,
                        reason=f"'{name}' not found in {node.module}, closest: '{closest}'",
                    ))

    # --- Checks 2-4: AST walk with class tracking ---
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            # Check 2 & 3: self.method() and self.attr (GREEN: class members are closed)
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

            # NOTE: Check 5 (ClassName.something) is DISABLED — open set
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            # Check 4: keyword arguments (GREEN: param names are closed)
            if node.keywords:
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

            # NOTE: Checks 5A, 5B, 5C are DISABLED — open set, ~98% false positives
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
            match = re.search(r'\+(\d+)(?:,(\d+))?', line)
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

    Only flags pairs where at least one occurrence is on a modified line.
    Uses edit distance 1 only.
    """
    corrections: list[dict[str, Any]] = []
    modified_lines = _get_modified_lines(modified_files)

    self_attrs: dict[str, list[tuple[str, int, int, int, bool]]] = {}
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

    attr_names = list(self_attrs.keys())
    for i, a1 in enumerate(attr_names):
        if len(a1) <= 4:
            continue
        for a2 in attr_names[i + 1:]:
            if len(a2) <= 4:
                continue
            dist = levenshtein_distance(a1, a2)
            if dist != 1:
                continue

            count1 = len(self_attrs[a1])
            count2 = len(self_attrs[a2])
            mod_count1 = sum(1 for _, _, _, _, m in self_attrs[a1] if m)
            mod_count2 = sum(1 for _, _, _, _, m in self_attrs[a2] if m)

            if count1 > count2 and count2 <= 2 and mod_count2 > 0:
                for fpath, line, col, end_col, is_mod in self_attrs[a2]:
                    if is_mod:
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
    corrections_by_line: dict[int, list[dict[str, Any]]] = {}
    for c in corrections:
        line_idx = c["line"] - 1
        if line_idx not in corrections_by_line:
            corrections_by_line[line_idx] = []
        corrections_by_line[line_idx].append(c)

    for line_idx in sorted(corrections_by_line.keys(), reverse=True):
        if line_idx < 0 or line_idx >= len(lines):
            continue
        line = lines[line_idx]
        line_corrections = corrections_by_line[line_idx]
        line_corrections.sort(key=lambda c: c.get("col_start", 0), reverse=True)

        for c in line_corrections:
            old = c["old_name"]
            new = c["new_name"]
            if old == new:
                continue
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
        "green_lane": True,
        "gated_out": 0,
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

        # Build patch overlay for green-lane gating
        overlay = build_patch_overlay("/testbed")

        # Build knowledge base
        kb = build_extended_kb("/testbed")

        # Get names defined in modified files
        modified_names = _get_modified_names(modified_files)

        # Check each file — GREEN-LANE ONLY
        all_corrections: list[dict[str, Any]] = []
        report["files_checked"] = len(modified_files)

        for fpath in modified_files:
            file_corrections = check_file_green_only(fpath, kb, modified_names)
            all_corrections.extend(file_corrections)

        # Check patch consistency (Check 6)
        consistency_corrections = check_patch_consistency(modified_files)
        all_corrections.extend(consistency_corrections)

        # Optionally run Pyright on changed files
        pyright_files = [os.path.join("/testbed", f) for f in overlay["changed_files"]
                         if f.endswith(".py")]
        if pyright_files:
            pyright_diags = _try_pyright(pyright_files)
            if pyright_diags:
                report["pyright_diagnostics"] = len(pyright_diags)

        # Deduplicate: same file + line + old_name
        seen: set[tuple[str, int, str]] = set()
        unique_corrections: list[dict[str, Any]] = []
        for c in all_corrections:
            key = (c["file"], c["line"], c["old_name"])
            if key not in seen:
                seen.add(key)
                unique_corrections.append(c)

        # Apply overlay gate to all corrections
        gated_corrections: list[dict[str, Any]] = []
        gated_out = 0
        for c in unique_corrections:
            if _should_correct(c, overlay):
                gated_corrections.append(c)
            else:
                gated_out += 1

        report["gated_out"] = gated_out

        # Apply corrections per file
        corrections_by_file: dict[str, list[dict[str, Any]]] = {}
        for c in gated_corrections:
            fpath = c["file"]
            if fpath not in corrections_by_file:
                corrections_by_file[fpath] = []
            corrections_by_file[fpath].append(c)

        files_modified = 0
        for fpath, file_corrs in corrections_by_file.items():
            count = apply_corrections(fpath, file_corrs)
            if count > 0:
                files_modified += 1

        # Build report
        report["corrections"] = gated_corrections
        report["files_modified"] = files_modified
        report["total_corrections"] = len(gated_corrections)

        by_type: dict[str, int] = {}
        for c in gated_corrections:
            ct = c["check_type"]
            by_type[ct] = by_type.get(ct, 0) + 1
        report["by_type"] = by_type

    except Exception as e:
        report["errors"].append(f"autocorrect error: {e}")

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
            "green_lane": True,
        }))
        sys.exit(0)
