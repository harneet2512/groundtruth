"""Return Usage Annotation -- classifies what a caller does with a function's return value.

Uses Python's ast.parse() to classify the usage pattern of a function call's
return value within a single caller line.

Feature flag: GT_RETURN_USAGE_ENABLED (env var, default "0" = OFF).
When OFF, all public functions return empty/no-op results.
"""

from __future__ import annotations

import ast
import os
from typing import Literal

ReturnUsageLabel = Literal[
    "CHECK_TRUTHINESS",
    "DESTRUCTURE",
    "DISCARD",
    "CAST",
    "COMPARE",
    "ASSIGN",
    "CHAIN_CALL",
    "CONDITIONAL",
    "UNKNOWN",
]


def _is_enabled() -> bool:
    return os.environ.get("GT_RETURN_USAGE_ENABLED", "0") == "1"


def _find_call(node: ast.AST, func_name: str) -> bool:
    """Check if an AST node contains a call to func_name."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == func_name:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
            return True
    for child in ast.iter_child_nodes(node):
        if _find_call(child, func_name):
            return True
    return False


def _node_is_call_to(node: ast.AST, func_name: str) -> bool:
    """Check if an AST node IS a direct call to func_name (top-level, not nested)."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name) and node.func.id == func_name:
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
        return True
    return False


def classify_return_usage(caller_line: str, func_name: str) -> ReturnUsageLabel:
    """Classify what a caller does with a function's return value.

    Args:
        caller_line: A single line of Python source containing a call to func_name.
        func_name: The name of the function being called.

    Returns:
        One of the ReturnUsageLabel values.
    """
    if not _is_enabled():
        return "UNKNOWN"

    line = caller_line.strip()

    # If the line ends with ':', it's likely an if/while/for header missing a body.
    # Add a dummy body so ast.parse succeeds.
    if line.endswith(":"):
        line = line + "\n  pass"

    try:
        tree = ast.parse(line, mode="exec")
    except SyntaxError:
        return "UNKNOWN"

    if not tree.body:
        return "UNKNOWN"

    stmt = tree.body[0]

    # --- DISCARD: bare expression statement with the call at top level ---
    if isinstance(stmt, ast.Expr):
        value = stmt.value
        # CHAIN_CALL: func().method() -- call is the object of an attribute access
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            obj = value.func.value
            if _node_is_call_to(obj, func_name):
                return "CHAIN_CALL"
        # Bare call at statement level
        if _node_is_call_to(value, func_name):
            return "DISCARD"
        # Could be a chain like func().x or nested -- check if func_name is in there
        if _find_call(value, func_name):
            # If the outer expression is an attribute call on our func, it's CHAIN_CALL
            if isinstance(value, ast.Attribute) and _node_is_call_to(value.value, func_name):
                return "CHAIN_CALL"
            return "UNKNOWN"
        return "UNKNOWN"

    # --- Assignments ---
    if isinstance(stmt, ast.Assign):
        value = stmt.value

        # DESTRUCTURE: a, b = func()
        if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Tuple):
            if _find_call(value, func_name):
                return "DESTRUCTURE"

        # CAST: int(func()), str(func()), etc.
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            builtin_casts = {"int", "float", "str", "bool", "list", "tuple", "dict", "set", "bytes"}
            if value.func.id in builtin_casts and value.args:
                if _find_call(value.args[0], func_name):
                    return "CAST"

        # CHAIN_CALL in assignment: x = func().method()
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            if _node_is_call_to(value.func.value, func_name):
                return "CHAIN_CALL"

        # COMPARE in assignment: x = func() == y  (or any comparison)
        if isinstance(value, ast.Compare):
            if _find_call(value.left, func_name):
                return "COMPARE"
            for comp in value.comparators:
                if _find_call(comp, func_name):
                    return "COMPARE"

        # CONDITIONAL in assignment: x = a if func() else b
        if isinstance(value, ast.IfExp):
            if _find_call(value.test, func_name):
                return "CONDITIONAL"

        # ASSIGN: x = func()
        if _find_call(value, func_name):
            return "ASSIGN"

    # --- If statement: CHECK_TRUTHINESS ---
    if isinstance(stmt, ast.If):
        if _find_call(stmt.test, func_name):
            # Could be a comparison inside if
            if isinstance(stmt.test, ast.Compare):
                return "COMPARE"
            return "CHECK_TRUTHINESS"

    # --- While statement: CHECK_TRUTHINESS ---
    if isinstance(stmt, ast.While):
        if _find_call(stmt.test, func_name):
            return "CHECK_TRUTHINESS"

    # --- For / other ---
    return "UNKNOWN"


def annotate_caller_lines(
    caller_lines: list[str], func_name: str
) -> list[dict[str, str]]:
    """Annotate multiple caller lines with return usage labels.

    Returns empty list when feature is disabled.

    Args:
        caller_lines: Lines of Python source each containing a call to func_name.
        func_name: The function name to classify usage for.

    Returns:
        List of dicts with keys "line" and "usage".
    """
    if not _is_enabled():
        return []

    results: list[dict[str, str]] = []
    for line in caller_lines:
        label = classify_return_usage(line, func_name)
        results.append({"line": line.strip(), "usage": label})
    return results
