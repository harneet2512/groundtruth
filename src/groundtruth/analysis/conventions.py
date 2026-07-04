"""Convention detectors that mine patterns from Python source code.

Analyzes actual code to discover repo-local conventions like guard clauses,
consistent error types, and return shapes. Pure stdlib (ast module).
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field

CONVENTION_THRESHOLD = 0.7


@dataclass
class Convention:
    """A mined convention from source code."""

    kind: str  # guard_clause | error_type | return_shape
    scope: str  # class name or module path
    pattern: str  # human-readable description
    frequency: float  # 0.0-1.0 (fraction of methods matching)
    confidence: float  # 0.0-1.0
    examples: list[str] = field(default_factory=list)


def _is_guard_clause(stmt: ast.stmt) -> bool:
    """Check if a statement is an if ... raise or if not ... raise pattern."""
    if not isinstance(stmt, ast.If):
        return False
    # Check the if-body for a raise as first statement
    for body_stmt in stmt.body:
        if isinstance(body_stmt, ast.Raise):
            return True
        # Allow a single-line expression (like assert) followed by raise
        break
    return False


def _get_public_methods(
    tree: ast.AST, class_name: str | None = None
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Get public methods/functions from the given scope."""
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    if class_name is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not item.name.startswith("_"):
                            methods.append(item)
                break
    else:
        # Module-level functions only (not nested in classes)
        if isinstance(tree, ast.Module):
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        methods.append(node)

    return methods


def detect_guard_clauses(source_code: str, class_name: str | None = None) -> list[Convention]:
    """Detect guard clause conventions in methods/functions.

    A guard clause is when the first statement of a method body is
    ``if ... raise`` or ``if not ... raise``.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    methods = _get_public_methods(tree, class_name)
    if not methods:
        return []

    matching: list[str] = []
    for method in methods:
        # Skip past docstring if present
        body = method.body
        start_idx = 0
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            start_idx = 1

        if start_idx < len(body) and _is_guard_clause(body[start_idx]):
            matching.append(method.name)

    freq = len(matching) / len(methods)
    if freq < CONVENTION_THRESHOLD:
        return []

    scope = class_name if class_name else "<module>"
    return [
        Convention(
            kind="guard_clause",
            scope=scope,
            pattern=f"{len(matching)}/{len(methods)} public methods start with a guard clause",
            frequency=round(freq, 2),
            confidence=round(min(1.0, freq + 0.1), 2),
            examples=matching,
        )
    ]


def detect_error_types(source_code: str, scope: str | None = None) -> list[Convention]:
    """Detect dominant exception types in raise statements.

    If >70% of raise statements use the same exception type, reports it.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    # Collect raise statements from the appropriate scope
    raise_types: list[str] = []

    if scope is not None:
        # Scope is a class name
        target: ast.AST | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == scope:
                target = node
                break
        if target is None:
            return []
        search_root = target
    else:
        search_root = tree

    for node in ast.walk(search_root):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        if exc is None:
            continue
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name):
            raise_types.append(exc.id)
        elif isinstance(exc, ast.Attribute):
            raise_types.append(exc.attr)

    if not raise_types:
        return []

    counter = Counter(raise_types)
    most_common_type, most_common_count = counter.most_common(1)[0]
    freq = most_common_count / len(raise_types)

    if freq < CONVENTION_THRESHOLD:
        return []

    scope_label = scope if scope else "<module>"
    return [
        Convention(
            kind="error_type",
            scope=scope_label,
            pattern=f"{most_common_count}/{len(raise_types)} raise statements use {most_common_type}",
            frequency=round(freq, 2),
            confidence=round(min(1.0, freq), 2),
            examples=[most_common_type],
        )
    ]


def _classify_return(node: ast.Return) -> str | None:
    """Classify a return statement's shape."""
    val = node.value
    if val is None:
        return "None"
    if isinstance(val, ast.Dict):
        return "dict"
    if isinstance(val, ast.Tuple):
        return "tuple"
    if isinstance(val, ast.List):
        return "list"
    if isinstance(val, ast.Call):
        func = val.func
        # dict(...), list(...), tuple(...)
        if isinstance(func, ast.Name):
            if func.id in ("dict", "list", "tuple"):
                return func.id
        # self — returning self.method() doesn't count as "self"
        return "call"
    if isinstance(val, ast.Constant):
        if val.value is None:
            return "None"
        if isinstance(val.value, bool):
            return "bool"
        if isinstance(val.value, (int, float)):
            return "number"
        if isinstance(val.value, str):
            return "str"
    # Check for `return self`
    if isinstance(val, ast.Name) and val.id == "self":
        return "self"
    return "other"


def detect_return_shapes(source_code: str, class_name: str | None = None) -> list[Convention]:
    """Detect dominant return shape conventions in public methods.

    If >70% of public methods return the same shape, reports it.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    methods = _get_public_methods(tree, class_name)
    if not methods:
        return []

    shape_per_method: dict[str, str] = {}
    for method in methods:
        returns: list[str] = []
        for node in ast.walk(method):
            if isinstance(node, ast.Return):
                shape = _classify_return(node)
                if shape is not None:
                    returns.append(shape)
        if returns:
            # Use the most common return shape in the method
            counter = Counter(returns)
            shape_per_method[method.name] = counter.most_common(1)[0][0]

    if not shape_per_method:
        return []

    shape_counter = Counter(shape_per_method.values())
    dominant_shape, dominant_count = shape_counter.most_common(1)[0]
    freq = dominant_count / len(methods)

    if freq < CONVENTION_THRESHOLD:
        return []

    matching_methods = [name for name, shape in shape_per_method.items() if shape == dominant_shape]

    scope = class_name if class_name else "<module>"
    return [
        Convention(
            kind="return_shape",
            scope=scope,
            pattern=f"{dominant_count}/{len(methods)} public methods return {dominant_shape}",
            frequency=round(freq, 2),
            confidence=round(min(1.0, freq), 2),
            examples=matching_methods,
        )
    ]


def detect_all(source_code: str, scope: str | None = None) -> list[Convention]:
    """Run all convention detectors and return combined results."""
    results: list[Convention] = []
    results.extend(detect_guard_clauses(source_code, class_name=scope))
    results.extend(detect_error_types(source_code, scope=scope))
    results.extend(detect_return_shapes(source_code, class_name=scope))
    return results


# ---------------------------------------------------------------------------
# Convention fingerprint — hashable summary of conventions for a class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConventionFingerprint:
    """Deterministic, hashable summary of a class's conventions.

    Two classes with the same fingerprint follow the same coding conventions.
    """

    guard_clause_freq: float  # 0.0 - 1.0
    error_type: str | None  # dominant exception type, or None
    error_type_freq: float  # 0.0 - 1.0
    return_shape: str | None  # dominant return shape, or None
    return_shape_freq: float  # 0.0 - 1.0

    def __hash__(self) -> int:
        return hash(
            (
                round(self.guard_clause_freq, 2),
                self.error_type,
                round(self.error_type_freq, 2),
                self.return_shape,
                round(self.return_shape_freq, 2),
            )
        )


def fingerprint_class(source_code: str, class_name: str) -> ConventionFingerprint:
    """Compute a convention fingerprint for a class.

    Runs all convention detectors and summarizes their findings into
    a deterministic, hashable fingerprint.
    """
    convs = detect_all(source_code, scope=class_name)

    guard_freq = 0.0
    error_type: str | None = None
    error_freq = 0.0
    return_shape: str | None = None
    return_freq = 0.0

    for c in convs:
        if c.kind == "guard_clause":
            guard_freq = c.frequency
        elif c.kind == "error_type":
            error_freq = c.frequency
            error_type = c.examples[0] if c.examples else None
        elif c.kind == "return_shape":
            return_freq = c.frequency
            # Extract shape from pattern like "3/4 public methods return dict"
            parts = c.pattern.rsplit(" return ", 1)
            return_shape = parts[1] if len(parts) == 2 else None

    return ConventionFingerprint(
        guard_clause_freq=guard_freq,
        error_type=error_type,
        error_type_freq=error_freq,
        return_shape=return_shape,
        return_shape_freq=return_freq,
    )
