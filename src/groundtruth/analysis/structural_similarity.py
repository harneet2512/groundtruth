"""Structural similarity — zero-dependency method/class similarity via AST features.

Computes feature vectors from AST properties and uses Jaccard similarity
to find structurally similar methods and classes. No ML models, no embeddings,
no external dependencies. Pure stdlib.

This is the ML-free fallback for Phase 2 (Semantic Sidecar). If structural
similarity proves useful for convention clustering and pattern discovery,
it justifies upgrading to neural embeddings later.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

# Binary features extracted from method AST
FEATURE_NAMES = (
    "has_return",
    "has_return_value",
    "has_guard_clause",
    "has_raise",
    "has_loop",
    "has_yield",
    "has_try_except",
    "has_self_assign",
    "has_self_read",
    "has_comparison",
    "has_isinstance_check",
    "has_dict_literal",
    "has_list_literal",
    "has_fstring",
    "has_assert",
    "has_super_call",
    "has_decorator",
    "accepts_self",
    "accepts_args",
    "accepts_kwargs",
)


@dataclass(frozen=True)
class StructuralFeatures:
    """Binary feature vector for a method or function."""

    name: str
    file_path: str
    features: frozenset[str]  # set of present feature names
    param_count: int  # number of params (excluding self/cls)
    body_statement_count: int  # number of top-level statements in body

    def jaccard_similarity(self, other: StructuralFeatures) -> float:
        """Jaccard similarity between two feature sets (0.0 - 1.0)."""
        if not self.features and not other.features:
            return 1.0 if self.param_count == other.param_count else 0.0
        union = self.features | other.features
        if not union:
            return 0.0
        intersection = self.features & other.features
        return len(intersection) / len(union)


def _has_guard_clause(body: Sequence[ast.stmt]) -> bool:
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


def extract_method_features(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str = "",
) -> StructuralFeatures:
    """Extract structural features from a method/function AST node."""
    features: set[str] = set()
    body = func.body

    # Walk the body for pattern detection
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if isinstance(node, ast.Return):
            features.add("has_return")
            if node.value is not None:
                features.add("has_return_value")
        elif isinstance(node, ast.Raise):
            features.add("has_raise")
        elif isinstance(node, (ast.For, ast.While)):
            features.add("has_loop")
        elif isinstance(node, ast.Yield) or isinstance(node, ast.YieldFrom):
            features.add("has_yield")
        elif isinstance(node, ast.Try):
            features.add("has_try_except")
        elif isinstance(node, ast.Assert):
            features.add("has_assert")
        elif isinstance(node, ast.Compare):
            features.add("has_comparison")
        elif isinstance(node, ast.Dict):
            features.add("has_dict_literal")
        elif isinstance(node, ast.List):
            features.add("has_list_literal")
        elif isinstance(node, ast.JoinedStr):
            features.add("has_fstring")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                features.add("has_isinstance_check")
            if isinstance(node.func, ast.Call):
                pass
            if isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Call)
                        and isinstance(node.func.value.func, ast.Name)
                        and node.func.value.func.id == "super"):
                    features.add("has_super_call")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (t is not None and isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name) and t.value.id == "self"):
                    features.add("has_self_assign")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                features.add("has_self_read")

    # Guard clause detection
    if _has_guard_clause(body):
        features.add("has_guard_clause")

    # Decorators
    if func.decorator_list:
        features.add("has_decorator")

    # Parameter analysis
    args = func.args
    all_params = list(args.posonlyargs) + list(args.args)
    accepts_self = bool(all_params and all_params[0].arg in ("self", "cls"))
    if accepts_self:
        features.add("accepts_self")
        all_params = all_params[1:]
    if args.vararg:
        features.add("accepts_args")
    if args.kwarg:
        features.add("accepts_kwargs")

    param_count = len(all_params)
    body_stmt_count = len(body)

    return StructuralFeatures(
        name=func.name,
        file_path=file_path,
        features=frozenset(features),
        param_count=param_count,
        body_statement_count=body_stmt_count,
    )


def extract_features_from_source(
    source_code: str,
    file_path: str = "",
    class_name: str | None = None,
) -> list[StructuralFeatures]:
    """Extract structural features for all methods/functions in source code.

    If class_name is provided, only extract from that class.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    results: list[StructuralFeatures] = []

    if class_name is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        results.append(extract_method_features(item, file_path))
                break
    else:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                results.append(extract_method_features(node, file_path))

    return results


# ---------------------------------------------------------------------------
# Similarity search
# ---------------------------------------------------------------------------


@dataclass
class SimilarityResult:
    """A match from find_similar."""

    target: StructuralFeatures
    score: float  # 0.0 - 1.0


def find_similar(
    query: StructuralFeatures,
    candidates: list[StructuralFeatures],
    top_k: int = 5,
    min_score: float = 0.3,
) -> list[SimilarityResult]:
    """Find the most structurally similar methods to a query.

    Uses Jaccard similarity on binary AST features, with a small bonus
    for matching param count and body size.
    """
    results: list[SimilarityResult] = []

    for candidate in candidates:
        if candidate.name == query.name and candidate.file_path == query.file_path:
            continue  # skip self-match

        # Base: Jaccard on features
        score = query.jaccard_similarity(candidate)

        # Small bonus for matching param count (max +0.1)
        if query.param_count == candidate.param_count:
            score += 0.05

        # Small bonus for similar body size (max +0.05)
        size_diff = abs(query.body_statement_count - candidate.body_statement_count)
        if size_diff <= 2:
            score += 0.05

        score = min(1.0, score)

        if score >= min_score:
            results.append(SimilarityResult(target=candidate, score=round(score, 3)))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class MethodCluster:
    """A group of structurally similar methods."""

    representative: StructuralFeatures  # most central member
    members: list[StructuralFeatures] = field(default_factory=list)
    shared_features: frozenset[str] = frozenset()  # features ALL members share


def cluster_methods(
    features_list: list[StructuralFeatures],
    similarity_threshold: float = 0.5,
) -> list[MethodCluster]:
    """Simple single-linkage clustering of methods by structural similarity.

    Two methods are in the same cluster if their Jaccard similarity exceeds
    the threshold. Uses union-find for O(n²) worst case.
    """
    if not features_list:
        return []

    n = len(features_list)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Build clusters via pairwise similarity
    for i in range(n):
        for j in range(i + 1, n):
            if features_list[i].jaccard_similarity(features_list[j]) >= similarity_threshold:
                union(i, j)

    # Group by cluster root
    clusters_map: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters_map.setdefault(root, []).append(i)

    # Build cluster objects (skip singletons)
    clusters: list[MethodCluster] = []
    for indices in clusters_map.values():
        if len(indices) < 2:
            continue
        members = [features_list[i] for i in indices]
        # Shared features = intersection of all member feature sets
        shared = members[0].features
        for m in members[1:]:
            shared = shared & m.features
        # Representative = member with most features (most typical)
        representative = max(members, key=lambda m: len(m.features))
        clusters.append(MethodCluster(
            representative=representative,
            members=members,
            shared_features=shared,
        ))

    clusters.sort(key=lambda c: len(c.members), reverse=True)
    return clusters
