"""StructuralVectorExtractor — 32-dimensional structural feature vector (astvec_v1).

32 float32 features across 5 buckets:
- Bucket 1: Statement types (8 dims)
- Bucket 2: Control flow (6 dims)
- Bucket 3: Expression patterns (6 dims)
- Bucket 4: Shape metrics (6 dims, normalized)
- Bucket 5: Structural patterns (6 dims)

Distance = 1.0 - cosine_similarity.
"""

from __future__ import annotations

import ast
import hashlib
import math
import struct

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.registry import register_extractor

VECTOR_DIM = 32
VECTOR_BYTES = VECTOR_DIM * 4  # 128 bytes


def _parse_body(raw_text: str) -> ast.Module | None:
    """Parse raw_text into an AST module, returning None on failure."""
    if not raw_text.strip():
        return None
    try:
        return ast.parse(raw_text)
    except SyntaxError:
        return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _has_guard_clause(body: list[ast.stmt]) -> bool:
    """Check if first non-docstring statement is if ... raise."""
    start = 0
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        start = 1
    if start >= len(body):
        return False
    stmt = body[start]
    if not isinstance(stmt, ast.If):
        return False
    for s in stmt.body:
        if isinstance(s, ast.Raise):
            return True
        break
    return False


def _has_early_return(body: list[ast.stmt]) -> bool:
    """Check if there's a return before the last statement."""
    for stmt in body[:-1]:
        if isinstance(stmt, ast.Return):
            return True
        if isinstance(stmt, ast.If):
            for s in stmt.body:
                if isinstance(s, ast.Return):
                    return True
    return False


def _max_depth(node: ast.AST, current: int = 0) -> int:
    """Compute max nesting depth of a node."""
    depth_increase_types = (
        ast.If, ast.For, ast.While, ast.Try, ast.With,
        ast.FunctionDef, ast.AsyncFunctionDef,
    )
    max_d = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, depth_increase_types):
            max_d = max(max_d, _max_depth(child, current + 1))
        else:
            max_d = max(max_d, _max_depth(child, current))
    return max_d


def extract_astvec_features(raw_text: str, param_count: int = 0) -> list[float]:
    """Extract 32-dimensional feature vector from raw Python source text.

    Reuses detection patterns from structural_similarity where applicable,
    extended to the full 32 dimensions.
    """
    tree = _parse_body(raw_text)

    if tree is None:
        return [0.0] * VECTOR_DIM

    features: list[float] = []

    # Collect all nodes for multi-pass analysis
    all_nodes = list(ast.walk(tree))

    # State accumulators
    has_return = False
    has_yield = False
    has_raise = False
    has_assignment = False
    has_augmented_assign = False
    has_delete = False
    has_assert = False
    has_pass = False

    has_if = False
    has_for = False
    has_while = False
    has_try_except = False
    has_with = False
    has_match_case = False

    has_comparison = False
    has_boolean_op = False
    has_string_format = False
    has_dict_literal = False
    has_list_literal = False
    has_call_chain = False

    num_self_refs = 0
    num_calls = 0
    num_assignments = 0

    has_loop_with_break = False
    has_nested_function = False
    has_decorator = False
    has_comprehension = False

    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None

    for node in all_nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if func_node is None:
                func_node = node
            else:
                has_nested_function = True
            if node.decorator_list:
                has_decorator = True

        elif isinstance(node, ast.Return):
            has_return = True
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            has_yield = True
        elif isinstance(node, ast.Raise):
            has_raise = True
        elif isinstance(node, ast.Assign):
            has_assignment = True
            num_assignments += 1
        elif isinstance(node, ast.AnnAssign):
            has_assignment = True
            num_assignments += 1
        elif isinstance(node, ast.AugAssign):
            has_augmented_assign = True
            num_assignments += 1
        elif isinstance(node, ast.Delete):
            has_delete = True
        elif isinstance(node, ast.Assert):
            has_assert = True
        elif isinstance(node, ast.Pass):
            has_pass = True

        elif isinstance(node, ast.If):
            has_if = True
        elif isinstance(node, ast.For):
            has_for = True
            # Check for break in loop body
            for child in ast.walk(node):
                if isinstance(child, ast.Break):
                    has_loop_with_break = True
                    break
        elif isinstance(node, ast.While):
            has_while = True
            for child in ast.walk(node):
                if isinstance(child, ast.Break):
                    has_loop_with_break = True
                    break
        elif isinstance(node, ast.Try):
            has_try_except = True
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            has_with = True

        elif isinstance(node, ast.Compare):
            has_comparison = True
        elif isinstance(node, ast.BoolOp):
            has_boolean_op = True
        elif isinstance(node, ast.JoinedStr):
            has_string_format = True
        elif isinstance(node, ast.Dict):
            has_dict_literal = True
        elif isinstance(node, ast.List):
            has_list_literal = True
        elif isinstance(node, ast.Call):
            num_calls += 1
            # Check for call chain: a.b().c()
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Call):
                    has_call_chain = True

        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                num_self_refs += 1

        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            has_comprehension = True

    # Check for match/case (Python 3.10+)
    if hasattr(ast, "Match"):
        for node in all_nodes:
            if isinstance(node, ast.Match):  # type: ignore[attr-defined]
                has_match_case = True
                break

    # Check for string format via % or .format()
    if not has_string_format:
        for node in all_nodes:
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                    has_string_format = True
                    break
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "format":
                    has_string_format = True
                    break

    # Body info
    body_stmts = tree.body
    if func_node is not None:
        body_stmts = func_node.body

    body_length = len(body_stmts)
    body_depth = _max_depth(tree)

    has_guard = _has_guard_clause(body_stmts)
    has_early_ret = _has_early_return(body_stmts)

    # ---- Bucket 1: Statement types (8 dims) ----
    features.append(float(has_return))
    features.append(float(has_yield))
    features.append(float(has_raise))
    features.append(float(has_assignment))
    features.append(float(has_augmented_assign))
    features.append(float(has_delete))
    features.append(float(has_assert))
    features.append(float(has_pass))

    # ---- Bucket 2: Control flow (6 dims) ----
    features.append(float(has_if))
    features.append(float(has_for))
    features.append(float(has_while))
    features.append(float(has_try_except))
    features.append(float(has_with))
    features.append(float(has_match_case))

    # ---- Bucket 3: Expression patterns (6 dims) ----
    features.append(float(has_comparison))
    features.append(float(has_boolean_op))
    features.append(float(has_string_format))
    features.append(float(has_dict_literal))
    features.append(float(has_list_literal))
    features.append(float(has_call_chain))

    # ---- Bucket 4: Shape metrics (6 dims, normalized) ----
    features.append(_clamp(param_count / 10.0))
    features.append(_clamp(num_self_refs / 20.0))
    features.append(_clamp(num_calls / 15.0))
    features.append(_clamp(num_assignments / 15.0))
    features.append(_clamp(body_depth / 6.0))
    features.append(_clamp(body_length / 30.0))

    # ---- Bucket 5: Structural patterns (6 dims) ----
    features.append(float(has_guard))
    features.append(float(has_early_ret))
    features.append(float(has_loop_with_break))
    features.append(float(has_nested_function))
    features.append(float(has_decorator))
    features.append(float(has_comprehension))

    return features


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class StructuralVectorExtractor:
    """Extracts a 32-dimensional structural feature vector from a symbol."""

    @property
    def rep_type(self) -> str:
        return "astvec_v1"

    @property
    def rep_version(self) -> str:
        return "1.0"

    @property
    def dimension(self) -> int | None:
        return VECTOR_DIM

    @property
    def supported_languages(self) -> list[str]:
        return ["python"]

    def extract(self, symbol: ExtractedSymbol) -> bytes:
        """Extract feature vector and pack as 32 float32 values."""
        features = extract_astvec_features(
            symbol.raw_text,
            param_count=len(symbol.parameters),
        )
        return struct.pack(f"{VECTOR_DIM}f", *features)

    def distance(self, a: bytes, b: bytes) -> float:
        """Distance = 1.0 - cosine_similarity."""
        vec_a = list(struct.unpack(f"{VECTOR_DIM}f", a))
        vec_b = list(struct.unpack(f"{VECTOR_DIM}f", b))
        return 1.0 - _cosine_similarity(vec_a, vec_b)

    def invalidation_key(self, file_path: str, content: str) -> str:
        """SHA-256 of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Auto-register
_instance = StructuralVectorExtractor()
register_extractor(_instance)
