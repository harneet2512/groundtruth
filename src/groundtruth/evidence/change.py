"""Change evidence -- before/after AST diff on changed functions.

Detects: removed guards, broadened exceptions, swallowed exceptions,
return shape changes, removed validation. Pure stdlib ast.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field


@dataclass
class ChangeEvidence:
    """A detected change in function behavior."""
    kind: str  # guard_removed | exception_broadened | exception_swallowed | return_shape_changed | validation_removed
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "change"


def _get_original_source(root: str, file_path: str) -> str:
    """Get original file content from git HEAD."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{file_path}"],
            capture_output=True, text=True, cwd=root, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _parse_safe(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _find_function(tree: ast.Module, func_name: str) -> ast.FunctionDef | None:
    """Find a function/method by name in the AST."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node
    return None


def _get_guard_clauses(func: ast.FunctionDef) -> list[tuple[str, str]]:
    """Extract guard clauses (if-raise/if-return at function top)."""
    guards = []
    for stmt in func.body[:5]:  # only check first 5 statements
        if isinstance(stmt, ast.If):
            # Check if body is raise or return
            for sub in stmt.body:
                if isinstance(sub, ast.Raise):
                    cond = ast.dump(stmt.test)[:80]
                    guards.append(("raise", cond))
                    break
                elif isinstance(sub, ast.Return):
                    cond = ast.dump(stmt.test)[:80]
                    guards.append(("return", cond))
                    break
    return guards


def _get_except_handlers(func: ast.FunctionDef) -> list[str]:
    """Extract exception types from except clauses."""
    handlers = []
    for node in ast.walk(func):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                handlers.append("bare_except")
            elif isinstance(node.type, ast.Name):
                handlers.append(node.type.id)
            elif isinstance(node.type, ast.Tuple):
                for elt in node.type.elts:
                    if isinstance(elt, ast.Name):
                        handlers.append(elt.id)
    return handlers


def _is_swallowed(handler: ast.ExceptHandler) -> bool:
    """Check if an except handler swallows the exception."""
    if not handler.body:
        return True
    if len(handler.body) == 1:
        stmt = handler.body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return True  # bare expression like `...`
    return False


def _classify_return_shape(func: ast.FunctionDef) -> str:
    """Classify the dominant return shape of a function."""
    shapes = []
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            val = node.value
            if isinstance(val, ast.Tuple):
                shapes.append(f"tuple({len(val.elts)})")
            elif isinstance(val, ast.Dict):
                shapes.append("dict")
            elif isinstance(val, ast.List):
                shapes.append("list")
            elif isinstance(val, ast.Constant) and val.value is None:
                shapes.append("None")
            else:
                shapes.append("scalar")
    if not shapes:
        return "None"
    # Return most common
    from collections import Counter
    return Counter(shapes).most_common(1)[0][0]


def _get_raise_types(func: ast.FunctionDef) -> set[str]:
    """Get all exception types raised in a function."""
    types = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


def _parse_diff_changed_funcs(diff_text: str) -> list[tuple[str, str, int, int]]:
    """Parse diff to find (file_path, func_name_hint, start_line, end_line) of changes.

    Returns list of (file, None, start, end) tuples. func_name resolved later from AST.
    """
    results = []
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file and current_file.endswith(".py"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                results.append((current_file, None, start, start + count - 1))
    return results


class ChangeAnalyzer:
    """Analyze before/after AST diff for changed functions."""

    def analyze(self, root: str, diff_text: str) -> list[ChangeEvidence]:
        findings: list[ChangeEvidence] = []
        if not diff_text:
            return findings

        changes = _parse_diff_changed_funcs(diff_text)

        # Group by file
        files_seen: dict[str, list[tuple[int, int]]] = {}
        for fpath, _, start, end in changes:
            files_seen.setdefault(fpath, []).append((start, end))

        for fpath, line_ranges in files_seen.items():
            original_source = _get_original_source(root, fpath)
            current_path = os.path.join(root, fpath)
            try:
                with open(current_path, "r", errors="replace") as f:
                    current_source = f.read()
            except OSError:
                continue

            orig_tree = _parse_safe(original_source)
            curr_tree = _parse_safe(current_source)
            if not orig_tree or not curr_tree:
                continue

            # Find functions that overlap with changed lines
            changed_funcs = set()
            for node in ast.walk(curr_tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_start = node.lineno
                    func_end = getattr(node, "end_lineno", func_start + 50)
                    for ls, le in line_ranges:
                        if func_start <= le and ls <= func_end:
                            changed_funcs.add(node.name)
                            break

            for func_name in changed_funcs:
                orig_func = _find_function(orig_tree, func_name)
                curr_func = _find_function(curr_tree, func_name)
                if not orig_func or not curr_func:
                    continue  # new function or deleted — skip

                # 1. Guard clauses removed
                orig_guards = _get_guard_clauses(orig_func)
                curr_guards = _get_guard_clauses(curr_func)
                if len(orig_guards) > len(curr_guards):
                    removed = len(orig_guards) - len(curr_guards)
                    findings.append(ChangeEvidence(
                        kind="guard_removed",
                        file_path=fpath,
                        line=curr_func.lineno,
                        message=f"safety check removed -- original had {len(orig_guards)} guard(s), edit has {len(curr_guards)}",
                        confidence=0.8,
                    ))

                # 2. Exception handlers broadened
                orig_handlers = _get_except_handlers(orig_func)
                curr_handlers = _get_except_handlers(curr_func)
                broad_map = {"Exception": 1, "BaseException": 1, "bare_except": 1}
                for handler in curr_handlers:
                    if handler in broad_map and handler not in orig_handlers:
                        findings.append(ChangeEvidence(
                            kind="exception_broadened",
                            file_path=fpath,
                            line=curr_func.lineno,
                            message=f"exception catch broadened to {handler} -- original caught: {', '.join(orig_handlers) or 'nothing'}",
                            confidence=0.85,
                        ))
                        break

                # 3. Exception swallowed
                for node in ast.walk(curr_func):
                    if isinstance(node, ast.ExceptHandler) and _is_swallowed(node):
                        # Check if original had the same swallow
                        orig_had_swallow = False
                        for onode in ast.walk(orig_func):
                            if isinstance(onode, ast.ExceptHandler) and _is_swallowed(onode):
                                orig_had_swallow = True
                                break
                        if not orig_had_swallow:
                            exc_type = "bare except"
                            if node.type and isinstance(node.type, ast.Name):
                                exc_type = node.type.id
                            findings.append(ChangeEvidence(
                                kind="exception_swallowed",
                                file_path=fpath,
                                line=node.lineno,
                                message=f"exception silently swallowed ({exc_type}: pass/return None)",
                                confidence=0.9,
                            ))
                        break

                # 4. Return shape changed
                orig_shape = _classify_return_shape(orig_func)
                curr_shape = _classify_return_shape(curr_func)
                if orig_shape != curr_shape and orig_shape != "None":
                    findings.append(ChangeEvidence(
                        kind="return_shape_changed",
                        file_path=fpath,
                        line=curr_func.lineno,
                        message=f"return shape changed from {orig_shape} to {curr_shape}",
                        confidence=0.75,
                    ))

                # 5. Validation removed (raise/assert removed)
                orig_raises = _get_raise_types(orig_func)
                curr_raises = _get_raise_types(curr_func)
                removed_raises = orig_raises - curr_raises
                if removed_raises:
                    findings.append(ChangeEvidence(
                        kind="validation_removed",
                        file_path=fpath,
                        line=curr_func.lineno,
                        message=f"validation removed -- original raised {', '.join(sorted(removed_raises))}",
                        confidence=0.7,
                    ))

        return findings
