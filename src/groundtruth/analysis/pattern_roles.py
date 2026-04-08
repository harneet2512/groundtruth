"""Pattern role classification — HOW a method uses shared attributes.

Pure stdlib (ast module). No groundtruth imports. Works on raw source code strings.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Sequence

STORES_IN_STATE = "stores_in_state"
SERIALIZES_TO_KWARGS = "serializes_to_kwargs"
COMPARES_IN_EQ = "compares_in_eq"
VALIDATES_INPUT = "validates_input"
GUARDS_ON_STATE = "guards_on_state"
EMITS_TO_OUTPUT = "emits_to_output"
ALL_ROLES = (
    STORES_IN_STATE,
    SERIALIZES_TO_KWARGS,
    COMPARES_IN_EQ,
    VALIDATES_INPUT,
    GUARDS_ON_STATE,
    EMITS_TO_OUTPUT,
)


def _is_self_attr(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == attr
    )


def _subtree_has(node: ast.AST, attr: str) -> bool:
    return any(_is_self_attr(c, attr) for c in ast.walk(node))


def _find_method(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _wrap(body: Sequence[ast.stmt]) -> ast.Module:
    return ast.Module(body=list(body), type_ignores=[])


def _check_stores_in_state(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X as assignment target (Assign, AnnAssign, AugAssign)."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(t is not None and _is_self_attr(t, attr) for t in targets):
                return True
        elif isinstance(node, ast.AugAssign) and _is_self_attr(node.target, attr):
            return True
    return False


def _check_serializes(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X in a return statement."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, ast.Return) and node.value and _subtree_has(node.value, attr):
            return True
    return False


def _check_compares(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X in a comparison."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, ast.Compare):
            if _is_self_attr(node.left, attr):
                return True
            if any(_is_self_attr(c, attr) for c in node.comparators):
                return True
    return False


def _check_validates(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X passed as argument to a function call."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, ast.Call):
            if any(_is_self_attr(a, attr) for a in node.args):
                return True
            if any(kw.value is not None and _is_self_attr(kw.value, attr) for kw in node.keywords):
                return True
    return False


def _check_guards(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X in an if condition."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, ast.If) and _subtree_has(node.test, attr):
            return True
    return False


def _check_emits(body: Sequence[ast.stmt], attr: str) -> bool:
    """self.X in f-string or % formatting."""
    for node in ast.walk(_wrap(body)):
        if isinstance(node, ast.JoinedStr):
            for val in node.values:
                if isinstance(val, ast.FormattedValue) and _is_self_attr(val.value, attr):
                    return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if _subtree_has(node.right, attr):
                return True
    return False


_CHECKERS = [
    (STORES_IN_STATE, _check_stores_in_state),
    (SERIALIZES_TO_KWARGS, _check_serializes),
    (COMPARES_IN_EQ, _check_compares),
    (VALIDATES_INPUT, _check_validates),
    (GUARDS_ON_STATE, _check_guards),
    (EMITS_TO_OUTPUT, _check_emits),
]


def classify_method_role(source_code: str, method_name: str, attr_name: str) -> list[str]:
    """Classify HOW a method uses a shared attribute.

    Returns matching role names. Empty list if parsing fails or method not found.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []
    method = _find_method(tree, method_name)
    if method is None:
        return []
    return [name for name, check in _CHECKERS if check(method.body, attr_name)]


def classify_roles_for_obligation(
    source_code: str,
    target_method: str,
    shared_attrs: list[str],
) -> dict[str, list[str]]:
    """For each shared attr, classify the method's role. Returns {attr: [roles]}."""
    return {attr: classify_method_role(source_code, target_method, attr) for attr in shared_attrs}


# ---------------------------------------------------------------------------
# StateFlowGraph — maps all attribute × method × role relationships in a class
# ---------------------------------------------------------------------------


@dataclass
class StateFlowGraph:
    """Bipartite graph: attributes ↔ methods with role-labeled edges.

    Attributes:
        attr_to_methods: {attr_name: {method_name: [roles]}}
        method_to_attrs: {method_name: {attr_name: [roles]}}
    """

    attr_to_methods: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    method_to_attrs: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _find_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Find a class definition by name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _collect_self_attrs(cls_node: ast.ClassDef) -> set[str]:
    """Collect all self.X attribute names used in a class."""
    attrs: set[str] = set()
    for node in ast.walk(cls_node):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            attrs.add(node.attr)
    return attrs


def _get_methods(cls_node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Get all instance methods in a class (direct children only)."""
    return [
        item for item in cls_node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def build_state_flow(source_code: str, class_name: str) -> StateFlowGraph:
    """Build a complete state-flow graph for a class.

    Maps every (attribute, method) pair to the roles that method plays
    with respect to that attribute.
    """
    graph = StateFlowGraph()
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return graph

    cls_node = _find_class(tree, class_name)
    if cls_node is None:
        return graph

    attrs = _collect_self_attrs(cls_node)
    methods = _get_methods(cls_node)

    for attr in sorted(attrs):
        attr_methods: dict[str, list[str]] = {}
        for method in methods:
            roles = [name for name, check in _CHECKERS if check(method.body, attr)]
            if roles:
                attr_methods[method.name] = roles
                # Also populate method_to_attrs
                if method.name not in graph.method_to_attrs:
                    graph.method_to_attrs[method.name] = {}
                graph.method_to_attrs[method.name][attr] = roles
        if attr_methods:
            graph.attr_to_methods[attr] = attr_methods

    return graph
