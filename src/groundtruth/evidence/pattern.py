"""Pattern evidence -- sibling analysis on N dimensions.

Compares a changed function against its siblings (same class or module)
on error types, return shapes, guard clauses, framework calls, and
parameter patterns. Emits evidence when the edit is a statistical outlier.
"""

from __future__ import annotations

import ast
import os
from collections import Counter
from dataclasses import dataclass


@dataclass
class PatternEvidence:
    """A detected pattern deviation from siblings."""
    kind: str  # error_type_outlier | return_shape_outlier | missing_guard | missing_call | param_mismatch
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "pattern"


def _parse_safe(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _get_exception_types(func: ast.FunctionDef) -> set[str]:
    """Get all exception types raised in a function."""
    types = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


def _classify_return_shape(func: ast.FunctionDef) -> str:
    """Classify dominant return shape."""
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
        return "implicit_None"
    return Counter(shapes).most_common(1)[0][0]


def _has_guard_clause(func: ast.FunctionDef) -> bool:
    """Check if function has guard clauses (if-raise/if-return at top)."""
    for stmt in func.body[:5]:
        if isinstance(stmt, ast.If):
            for sub in stmt.body:
                if isinstance(sub, (ast.Raise, ast.Return)):
                    return True
    return False


def _get_framework_calls(func: ast.FunctionDef) -> set[str]:
    """Get self.method() and module.func() calls."""
    calls = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                prefix = node.func.value.id
                if prefix in ("self", "cls", "super"):
                    calls.add(f"self.{node.func.attr}()")
    return calls


def _get_param_pattern(func: ast.FunctionDef) -> tuple[int, bool, bool]:
    """Get (positional_count, has_args, has_kwargs)."""
    args = func.args
    pos = len([a for a in args.args if a.arg not in ("self", "cls")])
    has_args = args.vararg is not None
    has_kwargs = args.kwarg is not None
    return (pos, has_args, has_kwargs)


class SiblingAnalyzer:
    """Compare a changed function against its siblings."""

    def analyze(self, source: str, changed_func_name: str,
                file_path: str = "") -> list[PatternEvidence]:
        """Analyze the changed function against siblings in the same scope."""
        findings: list[PatternEvidence] = []
        tree = _parse_safe(source)
        if not tree:
            return findings

        # Find the changed function and its siblings
        changed_node = None
        siblings: list[ast.FunctionDef] = []

        # Check class-level methods first
        for cls_node in ast.iter_child_nodes(tree):
            if not isinstance(cls_node, ast.ClassDef):
                continue
            class_methods = []
            target = None
            for item in cls_node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == changed_func_name:
                        target = item
                    else:
                        # Skip dunder methods as siblings
                        if not item.name.startswith("__"):
                            class_methods.append(item)
            if target:
                changed_node = target
                siblings = class_methods
                break

        # If not found in a class, check module-level functions
        if not changed_node:
            module_funcs = []
            for item in ast.iter_child_nodes(tree):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == changed_func_name:
                        changed_node = item
                    elif not item.name.startswith("_"):
                        module_funcs.append(item)
            if changed_node:
                siblings = module_funcs

        if not changed_node or len(siblings) < 2:
            return findings

        line = changed_node.lineno

        # Dimension 1: Error types
        edit_exc = _get_exception_types(changed_node)
        if edit_exc:
            sibling_exc_counts: Counter[str] = Counter()
            total_with_exc = 0
            for sib in siblings:
                sib_exc = _get_exception_types(sib)
                if sib_exc:
                    total_with_exc += 1
                    for e in sib_exc:
                        sibling_exc_counts[e] += 1

            if total_with_exc >= 2:
                # Find majority exception type
                majority_exc, majority_count = sibling_exc_counts.most_common(1)[0]
                freq = majority_count / total_with_exc
                if freq >= 0.6 and majority_exc not in edit_exc:
                    findings.append(PatternEvidence(
                        kind="error_type_outlier",
                        file_path=file_path,
                        line=line,
                        message=f"{majority_count}/{total_with_exc} siblings raise {majority_exc} -- edit raises {', '.join(sorted(edit_exc))}",
                        confidence=freq,
                    ))

        # Dimension 2: Return shapes
        edit_shape = _classify_return_shape(changed_node)
        sibling_shapes = Counter(_classify_return_shape(s) for s in siblings)
        if sibling_shapes and edit_shape != "implicit_None":
            majority_shape, majority_count = sibling_shapes.most_common(1)[0]
            total = sum(sibling_shapes.values())
            freq = majority_count / total
            if freq >= 0.6 and edit_shape != majority_shape and majority_shape != "implicit_None":
                findings.append(PatternEvidence(
                    kind="return_shape_outlier",
                    file_path=file_path,
                    line=line,
                    message=f"{majority_count}/{total} siblings return {majority_shape} -- edit returns {edit_shape}",
                    confidence=freq,
                ))

        # Dimension 3: Guard clauses
        edit_has_guard = _has_guard_clause(changed_node)
        siblings_with_guard = sum(1 for s in siblings if _has_guard_clause(s))
        guard_freq = siblings_with_guard / len(siblings) if siblings else 0
        if guard_freq >= 0.6 and not edit_has_guard:
            findings.append(PatternEvidence(
                kind="missing_guard",
                file_path=file_path,
                line=line,
                message=f"{siblings_with_guard}/{len(siblings)} siblings have guard clauses -- edit does not",
                confidence=guard_freq,
            ))

        # Dimension 4: Framework calls (self.validate(), self.clean(), etc.)
        edit_calls = _get_framework_calls(changed_node)
        sibling_call_counts: Counter[str] = Counter()
        for sib in siblings:
            for call in _get_framework_calls(sib):
                sibling_call_counts[call] += 1

        for call, count in sibling_call_counts.most_common(3):
            freq = count / len(siblings)
            if freq >= 0.6 and call not in edit_calls:
                findings.append(PatternEvidence(
                    kind="missing_call",
                    file_path=file_path,
                    line=line,
                    message=f"{count}/{len(siblings)} siblings call {call} -- edit does not",
                    confidence=freq,
                ))
                break  # only report first missing call

        return findings
