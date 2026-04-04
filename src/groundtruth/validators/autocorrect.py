"""Deterministic hallucination auto-correction engine.

Refactored from benchmarks/swebench/gt_autocorrect.py into a module that reads
from SQLite KB (SymbolStore) instead of JSON + AST re-parsing.

Checks modified code against the knowledge base and corrects hallucinated names:
imports, self.method, self.attr, kwargs, class refs, patch consistency.
"""

from __future__ import annotations

import ast
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from groundtruth.index.store import SymbolStore
from groundtruth.utils.levenshtein import levenshtein_distance, suggest_alternatives
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, Ok

log = get_logger("validators.autocorrect")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Correction:
    """A single correction to apply."""

    file: str
    line: int
    col_start: int
    col_end: int
    old_name: str
    new_name: str
    check_type: str  # import, method_call, attribute, kwarg, class_ref, consistency
    confidence: float
    reason: str


@dataclass
class Contradiction:
    """A structural contradiction detected in a patch."""

    file: str
    line: int
    kind: str       # stale_obligation | override_mismatch | caller_mismatch | impossible_field
    message: str    # one-line description
    evidence: str   # supporting detail from KB
    confidence: float


@dataclass
class AutoCorrectResult:
    """Result of check_patch or check_file."""

    corrections: list[Correction] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    files_checked: int = 0
    files_modified: int = 0
    corrected_diff: str | None = None
    by_type: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Levenshtein helper (matches gt_autocorrect.py find_closest semantics)
# ---------------------------------------------------------------------------


def _find_closest(name: str, candidates: set[str] | list[str], max_dist: int = 2) -> str | None:
    """Return single match if unambiguous, None if 0 or 2+ matches."""
    if not candidates or len(name) <= 3:
        return None
    matches = suggest_alternatives(name, list(candidates), max_distance=max_dist)
    # Filter out exact match
    matches = [(c, d) for c, d in matches if c != name]
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) >= 2 and matches[0][1] < matches[1][1]:
        return matches[0][0]
    return None


# ---------------------------------------------------------------------------
# KB building from SQLite store
# ---------------------------------------------------------------------------


def _filepath_to_module(filepath: str, repo_root: str) -> str:
    """Convert filepath to dotted module path (language-agnostic)."""
    rel = os.path.relpath(filepath, repo_root)
    # Strip known source extensions
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt",
                ".rs", ".cs", ".php", ".swift", ".rb", ".scala", ".lua"):
        if rel.endswith(ext):
            rel = rel[:-len(ext)]
            break
    # Strip index/init file markers
    for marker in ("__init__", "index", "mod"):
        if rel.endswith(marker):
            rel = rel[: -len(marker) - 1]
            break
    module = rel.replace(os.sep, ".").replace("/", ".")
    return module.rstrip(".")


def _build_kb_from_store(store: SymbolStore, repo_root: str) -> dict[str, Any]:
    """Build knowledge base from the SQLite store."""
    kb: dict[str, Any] = {
        "module_exports": {},
        "classes": {},
        "param_names": {},
        "all_class_names": set(),
        "file_modules": {},
    }

    # Get all files and symbols
    files_result = store.get_all_files()
    if isinstance(files_result, Err):
        return kb

    for file_path in files_result.value:
        module = _filepath_to_module(file_path, repo_root)
        kb["file_modules"][file_path] = module

        symbols_result = store.get_symbols_in_file(file_path)
        if isinstance(symbols_result, Err):
            continue

        exports: set[str] = set()
        for sym in symbols_result.value:
            exports.add(sym.name)

            if sym.kind in ("class", "Class"):
                kb["all_class_names"].add(sym.name)

                # Build class info from child symbols and attributes
                methods: set[str] = set()
                attrs: set[str] = set()

                # Get methods: symbols in same file that are methods of this class
                # (convention: methods are stored as children or with the class line range)
                if sym.line_number is not None and sym.end_line is not None:
                    children_result = store.get_symbols_in_line_range(
                        file_path, sym.line_number, sym.end_line
                    )
                    if isinstance(children_result, Ok):
                        for child in children_result.value:
                            if child.kind in ("method", "function") and child.id != sym.id:
                                methods.add(child.name)

                # Get attributes from the attributes table
                attrs_result = store.get_attributes_for_symbol(sym.id)
                if isinstance(attrs_result, Ok):
                    for attr_rec in attrs_result.value:
                        attrs.add(attr_rec["name"])

                if sym.name in kb["classes"]:
                    kb["classes"][sym.name]["methods"].update(methods)
                    kb["classes"][sym.name]["attrs"].update(attrs)
                else:
                    kb["classes"][sym.name] = {
                        "methods": methods,
                        "attrs": attrs,
                        "file": file_path,
                    }

            # Build param_names from signature
            if sym.signature and sym.kind in ("function", "method"):
                params = _parse_params_from_signature(sym.signature)
                if sym.kind == "method" and sym.name != "__init__":
                    # Try to find enclosing class
                    for cname, cinfo in kb["classes"].items():
                        if sym.name in cinfo.get("methods", set()):
                            kb["param_names"][f"{cname}.{sym.name}"] = params
                            break
                kb["param_names"][sym.name] = params

        if module in kb["module_exports"]:
            kb["module_exports"][module].update(exports)
        else:
            kb["module_exports"][module] = exports

    return kb


def _parse_params_from_signature(sig: str) -> list[str]:
    """Extract parameter names from a signature string like '(self, x: int, y: str = ...)'."""
    # Strip outer parens and return type
    sig = sig.strip()
    if sig.startswith("("):
        # Find matching closing paren
        depth = 0
        end = -1
        for i, ch in enumerate(sig):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > 0:
            sig = sig[1:end]
        else:
            sig = sig[1:]

    params: list[str] = []
    for part in sig.split(","):
        part = part.strip()
        if not part or part in ("/", "*"):
            continue
        # Get the name (before : or =)
        name = part.split(":")[0].split("=")[0].strip()
        name = name.lstrip("*")
        if name and name != "self":
            params.append(name)
    return params


# ---------------------------------------------------------------------------
# File checking — 6-phase analysis
# ---------------------------------------------------------------------------


def _get_modified_names(source: str) -> set[str]:
    """Get all names defined in source (to skip correcting new code)."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
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
                if node.module and _resolve_import_module(node.module, kb) is not None:
                    # Module is in our project KB → project-local
                    return True
                # Imported from external module → NOT project-local
                return False
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".")[-1]
                if imported_name == name:
                    if alias.name in kb.get("module_exports", {}):
                        return True
                    if _resolve_import_module(alias.name, kb) is not None:
                        return True
                    return False
    # Name not imported at all — could be builtin, stdlib, or defined elsewhere.
    # No positive evidence it's project-local.
    return False


def _check_file_against_kb(
    filepath: str,
    source: str,
    kb: dict[str, Any],
    modified_names: set[str],
    store: SymbolStore | None = None,
    repo: str = "",
) -> list[Correction]:
    """Check a file for hallucinated names against the KB."""
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    corrections: list[Correction] = []

    # Add parent references for context
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]

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
                if name in module_exports or name in modified_names:
                    continue

                # Check corrections table first
                corrected = _check_correction_table(store, repo, name)
                if corrected and corrected in module_exports:
                    corrections.append(
                        Correction(
                            file=filepath,
                            line=node.lineno,
                            col_start=0,
                            col_end=0,
                            old_name=name,
                            new_name=corrected,
                            check_type="import",
                            confidence=0.95,
                            reason=f"learned: '{name}' → '{corrected}' in {node.module}",
                        )
                    )
                    continue

                closest = _find_closest(name, module_exports)
                if closest and closest not in modified_names:
                    corrections.append(
                        Correction(
                            file=filepath,
                            line=node.lineno,
                            col_start=0,
                            col_end=0,
                            old_name=name,
                            new_name=closest,
                            check_type="import",
                            confidence=0.9,
                            reason=f"'{name}' not found in {node.module}, closest: '{closest}'",
                        )
                    )

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
                enclosing = self.class_stack[-1] if self.class_stack else None
                if enclosing and enclosing in kb["classes"]:
                    cinfo = kb["classes"][enclosing]
                    attr = node.attr
                    if len(attr) > 3:
                        all_names = cinfo["methods"] | cinfo["attrs"]
                        if attr not in all_names and attr not in modified_names:
                            corrected = _check_correction_table(store, repo, attr)
                            if corrected and corrected in all_names:
                                corrections.append(
                                    Correction(
                                        file=filepath,
                                        line=node.lineno,
                                        col_start=node.col_offset,
                                        col_end=node.end_col_offset or 0,
                                        old_name=attr,
                                        new_name=corrected,
                                        check_type="attribute",
                                        confidence=0.95,
                                        reason=f"learned: self.{attr} → self.{corrected}",
                                    )
                                )
                            else:
                                closest = _find_closest(attr, all_names)
                                if closest and closest not in modified_names:
                                    is_call = (
                                        isinstance(
                                            node._parent, ast.Call  # type: ignore[attr-defined]
                                        )
                                        and node._parent.func is node  # type: ignore[attr-defined]
                                    ) if hasattr(node, "_parent") else False
                                    check_type = "method_call" if is_call else "attribute"
                                    corrections.append(
                                        Correction(
                                            file=filepath,
                                            line=node.lineno,
                                            col_start=node.col_offset,
                                            col_end=node.end_col_offset or 0,
                                            old_name=attr,
                                            new_name=closest,
                                            check_type=check_type,
                                            confidence=0.85,
                                            reason=f"self.{attr} not in {enclosing}, closest: '{closest}'",
                                        )
                                    )

            # Check 5: ClassName.something
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in kb["classes"]
                and node.attr
                not in kb["classes"][node.value.id].get("methods", set())
                and node.attr not in kb["classes"][node.value.id].get("attrs", set())
            ):
                cinfo = kb["classes"][node.value.id]
                attr = node.attr
                if len(attr) > 3 and attr not in modified_names:
                    all_names = cinfo["methods"] | cinfo["attrs"]
                    closest = _find_closest(attr, all_names)
                    if closest and closest not in modified_names:
                        corrections.append(
                            Correction(
                                file=filepath,
                                line=node.lineno,
                                col_start=node.col_offset,
                                col_end=node.end_col_offset or 0,
                                old_name=attr,
                                new_name=closest,
                                check_type="attribute",
                                confidence=0.85,
                                reason=f"{node.value.id}.{attr} not found, closest: '{closest}'",
                            )
                        )

            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            # Check 4: keyword arguments
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
                            closest = _find_closest(kw.arg, valid_params)
                            if closest and closest not in modified_names:
                                corrections.append(
                                    Correction(
                                        file=filepath,
                                        line=kw.lineno if hasattr(kw, "lineno") else node.lineno,
                                        col_start=kw.col_offset
                                        if hasattr(kw, "col_offset")
                                        else 0,
                                        col_end=0,
                                        old_name=kw.arg,
                                        new_name=closest,
                                        check_type="kwarg",
                                        confidence=0.8,
                                        reason=f"kwarg '{kw.arg}' not in {func_name}, closest: '{closest}'",
                                    )
                                )

            # Check 5A: ClassName() — class instantiation
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if (
                    len(name) > 3
                    and name not in kb["all_class_names"]
                    and name not in modified_names
                    and name[0].isupper()
                    and _is_project_local_name(name, tree, kb)
                ):
                    closest = _find_closest(name, kb["all_class_names"])
                    if closest and closest not in modified_names:
                        corrections.append(
                            Correction(
                                file=filepath,
                                line=node.lineno,
                                col_start=node.func.col_offset,
                                col_end=node.func.end_col_offset or 0,
                                old_name=name,
                                new_name=closest,
                                check_type="class_ref",
                                confidence=0.8,
                                reason=f"class '{name}' not found, closest: '{closest}'",
                            )
                        )

            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            # Check 5B: bare ClassName references
            name = node.id
            if (
                len(name) > 3
                and name[0].isupper()
                and name not in kb["all_class_names"]
                and name not in modified_names
                and _is_project_local_name(name, tree, kb)
            ):
                closest = _find_closest(name, kb["all_class_names"])
                if closest and closest not in modified_names:
                    corrections.append(
                        Correction(
                            file=filepath,
                            line=node.lineno,
                            col_start=node.col_offset,
                            col_end=node.end_col_offset or 0,
                            old_name=name,
                            new_name=closest,
                            check_type="class_ref",
                            confidence=0.7,
                            reason=f"'{name}' not found, closest class: '{closest}'",
                        )
                    )
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)

    return corrections


def _resolve_import_module(module_str: str, kb: dict[str, Any]) -> set[str] | None:
    """Resolve an import module path to its exports from the KB."""
    if module_str in kb["module_exports"]:
        return kb["module_exports"][module_str]
    # Try partial match
    for mod_path, exports in kb["module_exports"].items():
        if mod_path.endswith(module_str) or module_str.endswith(mod_path):
            return exports
    return None


def _check_correction_table(
    store: SymbolStore | None, repo: str, name: str
) -> str | None:
    """Look up a previous correction in the learning table."""
    if store is None or not repo:
        return None
    result = store.get_correction(repo, name)
    if isinstance(result, Ok):
        return result.value
    return None


# ---------------------------------------------------------------------------
# Check 6: Patch consistency
# ---------------------------------------------------------------------------


def _check_patch_consistency(
    files_with_source: dict[str, str],
    modified_line_sets: dict[str, set[int]] | None = None,
) -> list[Correction]:
    """Check for minority-spelling self.attr names across modified files."""
    corrections: list[Correction] = []

    # Collect all self.X names across files
    self_attrs: dict[str, list[tuple[str, int, int, int, bool]]] = {}

    for fpath, source in files_with_source.items():
        try:
            tree = ast.parse(source, filename=fpath)
        except SyntaxError:
            continue
        file_mod_lines = modified_line_sets.get(fpath, set()) if modified_line_sets else set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                attr = node.attr
                if attr not in self_attrs:
                    self_attrs[attr] = []
                is_mod = node.lineno in file_mod_lines if file_mod_lines else True
                self_attrs[attr].append(
                    (fpath, node.lineno, node.col_offset, node.end_col_offset or 0, is_mod)
                )

    # Find near-duplicate attr names (distance 1 only)
    attr_names = list(self_attrs.keys())
    for i, a1 in enumerate(attr_names):
        if len(a1) <= 4:
            continue
        for a2 in attr_names[i + 1 :]:
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
                        corrections.append(
                            Correction(
                                file=fpath,
                                line=line,
                                col_start=col,
                                col_end=end_col,
                                old_name=a2,
                                new_name=a1,
                                check_type="consistency",
                                confidence=0.85,
                                reason=f"self.{a2} appears {count2}x vs self.{a1} {count1}x",
                            )
                        )
            elif count2 > count1 and count1 <= 2 and mod_count1 > 0:
                for fpath, line, col, end_col, is_mod in self_attrs[a1]:
                    if is_mod:
                        corrections.append(
                            Correction(
                                file=fpath,
                                line=line,
                                col_start=col,
                                col_end=end_col,
                                old_name=a1,
                                new_name=a2,
                                check_type="consistency",
                                confidence=0.85,
                                reason=f"self.{a1} appears {count1}x vs self.{a2} {count2}x",
                            )
                        )

    return corrections


# ---------------------------------------------------------------------------
# Apply corrections to source text
# ---------------------------------------------------------------------------


def _apply_corrections_to_source(source: str, corrections: list[Correction]) -> tuple[str, int]:
    """Apply corrections to source text. Returns (new_source, count_applied)."""
    if not corrections:
        return source, 0

    lines = source.splitlines(keepends=True)
    applied = 0

    # Group by line, process bottom-to-top
    by_line: dict[int, list[Correction]] = {}
    for c in corrections:
        idx = c.line - 1
        if idx not in by_line:
            by_line[idx] = []
        by_line[idx].append(c)

    for line_idx in sorted(by_line.keys(), reverse=True):
        if line_idx < 0 or line_idx >= len(lines):
            continue
        line = lines[line_idx]
        line_corrections = sorted(by_line[line_idx], key=lambda c: c.col_start, reverse=True)

        for c in line_corrections:
            if c.old_name == c.new_name:
                continue
            new_line = re.sub(r"\b" + re.escape(c.old_name) + r"\b", c.new_name, line, count=1)
            if new_line != line:
                lines[line_idx] = new_line
                line = new_line
                applied += 1

    return "".join(lines), applied


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def _parse_diff(diff_text: str) -> dict[str, set[int]]:
    """Parse unified diff to extract {file_path: set of added/modified line numbers}."""
    result: dict[str, set[int]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@ ") and current_file:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if current_file not in result:
                    result[current_file] = set()
                for i in range(start, start + count):
                    result[current_file].add(i)

    return result


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


def _check_contradictions(
    diff_text: str,
    obligation_engine: Any,
) -> list[Contradiction]:
    """Run obligation engine on the diff, convert unsatisfied obligations to contradictions.

    Logic:
    1. obligation_engine.infer_from_patch(diff_text) -> all obligations
    2. Parse diff to get set of changed files/lines
    3. For each obligation: is the obligated target ALSO changed in this diff?
    4. If NOT changed -> it's a contradiction (obligation unsatisfied)
    """
    try:
        obligations = obligation_engine.infer_from_patch(diff_text)
    except Exception:
        return []

    if not obligations:
        return []

    # Parse diff to find changed files
    changed_files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            changed_files.add(line[6:])
        elif line.startswith("+++ "):
            changed_files.add(line[4:].strip())

    contradictions: list[Contradiction] = []
    for ob in obligations:
        # If the obligated target file is NOT in the set of changed files,
        # this is a potential contradiction
        if ob.target_file not in changed_files:
            contradictions.append(Contradiction(
                file=ob.target_file,
                line=ob.target_line or 0,
                kind="stale_obligation",
                message=f"{ob.target} must change because {ob.source} changed, but is not in this patch",
                evidence=ob.reason,
                confidence=ob.confidence,
            ))

    return contradictions


class AutoCorrector:
    """Validates diffs and source files against the KB, corrects hallucinations."""

    def __init__(
        self,
        store: SymbolStore,
        repo_root: str,
        benchmark_safe: bool = False,
        graph: Any = None,
    ) -> None:
        self.store = store
        self.repo_root = repo_root
        self.benchmark_safe = benchmark_safe
        self._kb: dict[str, Any] | None = None
        self._repo_name = os.path.basename(os.path.abspath(repo_root))
        self._obligation_engine: Any = None
        if graph is not None:
            from groundtruth.validators.obligations import ObligationEngine
            self._obligation_engine = ObligationEngine(store, graph)

    @property
    def kb(self) -> dict[str, Any]:
        if self._kb is None:
            self._kb = _build_kb_from_store(self.store, self.repo_root)
        return self._kb

    def invalidate_kb(self) -> None:
        """Force KB rebuild on next access."""
        self._kb = None

    def check_patch(self, diff_text: str) -> AutoCorrectResult:
        """Validate a diff against the KB. THE KILLER FEATURE."""
        start_time = time.time()
        result = AutoCorrectResult()

        try:
            # 1. Parse diff into {file: modified_lines}
            modified_lines = _parse_diff(diff_text)
            if not modified_lines:
                return result

            # 2. Read each modified file and check
            files_with_source: dict[str, str] = {}
            all_corrections: list[Correction] = []

            for rel_path in modified_lines:
                abs_path = os.path.join(self.repo_root, rel_path)
                ext = os.path.splitext(rel_path)[1].lower()
                if ext not in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java",
                               ".kt", ".rs", ".cs", ".php", ".swift", ".rb") or not os.path.exists(abs_path):
                    continue

                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue

                files_with_source[rel_path] = source
                result.files_checked += 1

                modified_names = _get_modified_names(source)
                file_corrections = _check_file_against_kb(
                    filepath=rel_path,
                    source=source,
                    kb=self.kb,
                    modified_names=modified_names,
                    store=None if self.benchmark_safe else self.store,
                    repo=self._repo_name,
                )
                all_corrections.extend(file_corrections)

            # 3. Patch consistency check
            consistency = _check_patch_consistency(files_with_source, modified_lines)
            all_corrections.extend(consistency)

            # 4. Deduplicate
            seen: set[tuple[str, int, str]] = set()
            unique: list[Correction] = []
            for c in all_corrections:
                key = (c.file, c.line, c.old_name)
                if key not in seen:
                    seen.add(key)
                    unique.append(c)

            result.corrections = unique

            # 5. Log corrections to learning table (skip in benchmark-safe mode)
            if not self.benchmark_safe:
                for c in unique:
                    self.store.insert_correction(
                        repo=self._repo_name,
                        hallucinated_name=c.old_name,
                        corrected_to=c.new_name,
                        file=c.file,
                        context=c.reason,
                        check_type=c.check_type,
                        confidence=c.confidence,
                    )

            # 5b. Contradiction check (7th phase) — uses obligation engine
            if self._obligation_engine is not None:
                result.contradictions = _check_contradictions(
                    diff_text, self._obligation_engine
                )

            # 6. Build corrected diff (apply corrections to the diff text itself)
            corrected_diff = diff_text
            for c in unique:
                corrected_diff = re.sub(
                    r"\b" + re.escape(c.old_name) + r"\b",
                    c.new_name,
                    corrected_diff,
                )
            result.corrected_diff = corrected_diff

            # 7. Build summary
            by_type: dict[str, int] = {}
            for c in unique:
                by_type[c.check_type] = by_type.get(c.check_type, 0) + 1
            result.by_type = by_type

        except Exception as exc:
            result.errors.append(f"autocorrect error: {exc}")
            log.error("check_patch_error", error=str(exc), exc_info=True)

        elapsed = int((time.time() - start_time) * 1000)
        log.info(
            "check_patch_done",
            corrections=len(result.corrections),
            files_checked=result.files_checked,
            latency_ms=elapsed,
        )
        return result

    def check_file(self, file_path: str, source: str) -> list[Correction]:
        """Check a single file source against the KB."""
        modified_names = _get_modified_names(source)
        return _check_file_against_kb(
            filepath=file_path,
            source=source,
            kb=self.kb,
            modified_names=modified_names,
            store=self.store,
            repo=self._repo_name,
        )
