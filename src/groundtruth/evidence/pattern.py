"""Pattern evidence -- sibling analysis on N dimensions.

Compares a changed function against its siblings (same class or module)
on error types, return shapes, guard clauses, framework calls,
parameter patterns, and API access patterns. Emits evidence when the
edit is a statistical outlier.

v16: Language-agnostic. Uses graph.db properties for sibling comparison.
Falls back to Python AST when graph.db is unavailable.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from groundtruth.index.graph_store import GraphStore


@dataclass
class PatternEvidence:
    """A detected pattern deviation from siblings."""
    kind: str  # error_type_outlier | return_shape_outlier | missing_guard | missing_call | api_access_outlier | missing_docstring
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "pattern"


class SiblingAnalyzer:
    """Compare a changed function against its siblings.

    Language-agnostic: uses graph.db properties when available,
    falls back to Python AST for .py files.
    """

    def __init__(self, store: GraphStore | None = None):
        self.store = store

    def analyze(self, source: str, changed_func_name: str,
                file_path: str = "", node_id: int | None = None) -> list[PatternEvidence]:
        """Analyze the changed function against siblings in the same scope."""

        # Path 1: graph.db properties (language-agnostic)
        if self.store and node_id:
            findings = self._analyze_from_graph(node_id, changed_func_name, file_path)
            if findings is not None:  # None = graph.db query failed, fall through
                return findings

        # Path 2: Python AST fallback
        if file_path.endswith(".py") or not file_path:
            return self._analyze_python_ast(source, changed_func_name, file_path)

        # Path 3: No graph.db and non-Python — no evidence possible yet
        return []

    def _analyze_from_graph(self, node_id: int, changed_func_name: str,
                            file_path: str) -> list[PatternEvidence] | None:
        """Analyze using graph.db properties. Returns None if data is insufficient."""
        assert self.store is not None

        siblings = self.store.get_sibling_functions(node_id)
        if len(siblings) < 2:
            return None  # not enough siblings, fall through to AST

        # Get properties for the changed function
        target_props = self.store.get_properties(node_id)
        target_guards = [p for p in target_props if p["kind"] == "guard_clause"]
        target_exceptions = {p["value"] for p in target_props if p["kind"] == "exception_type"}
        target_shapes = [p["value"] for p in target_props if p["kind"] == "return_shape"]
        target_shape = target_shapes[0] if target_shapes else "none"

        # Get function line number
        line = 0
        try:
            if self.store.connection:
                cursor = self.store.connection.execute(
                    "SELECT start_line FROM nodes WHERE id = ?", (node_id,)
                )
                row = cursor.fetchone()
                if row:
                    line = row[0] or 0
        except Exception:
            pass

        findings: list[PatternEvidence] = []

        # Dimension 1: Exception types
        if target_exceptions:
            sibling_exc_counts: Counter[str] = Counter()
            total_with_exc = 0
            for sib in siblings:
                sib_exc = {p["value"] for p in sib["properties"] if p["kind"] == "exception_type"}
                if sib_exc:
                    total_with_exc += 1
                    for e in sib_exc:
                        sibling_exc_counts[e] += 1

            if total_with_exc >= 2:
                majority_exc, majority_count = sibling_exc_counts.most_common(1)[0]
                freq = majority_count / total_with_exc
                if freq >= 0.6 and majority_exc not in target_exceptions:
                    findings.append(PatternEvidence(
                        kind="error_type_outlier", file_path=file_path, line=line,
                        message=f"{majority_count}/{total_with_exc} siblings raise {majority_exc} -- edit raises {', '.join(sorted(target_exceptions))}",
                        confidence=freq,
                    ))

        # Dimension 2: Return shapes
        sibling_shapes = Counter(
            next((p["value"] for p in sib["properties"] if p["kind"] == "return_shape"), "none")
            for sib in siblings
        )
        if sibling_shapes and target_shape != "none":
            majority_shape, majority_count = sibling_shapes.most_common(1)[0]
            total = sum(sibling_shapes.values())
            freq = majority_count / total
            if freq >= 0.6 and target_shape != majority_shape and majority_shape != "none":
                findings.append(PatternEvidence(
                    kind="return_shape_outlier", file_path=file_path, line=line,
                    message=f"{majority_count}/{total} siblings return {majority_shape} -- edit returns {target_shape}",
                    confidence=freq,
                ))

        # Dimension 3: Guard clauses
        siblings_with_guard = sum(
            1 for sib in siblings
            if any(p["kind"] == "guard_clause" for p in sib["properties"])
        )
        guard_freq = siblings_with_guard / len(siblings) if siblings else 0
        if guard_freq >= 0.6 and not target_guards:
            findings.append(PatternEvidence(
                kind="missing_guard", file_path=file_path, line=line,
                message=f"{siblings_with_guard}/{len(siblings)} siblings have guard clauses -- edit does not",
                confidence=guard_freq,
            ))

        # Dimension 4: Missing framework calls (using CALLS edges from graph.db)
        try:
            if self.store.connection:
                # Get callees of the target function
                target_callees: set[str] = set()
                cursor = self.store.connection.execute(
                    "SELECT n.name FROM edges e JOIN nodes n ON e.target_id = n.id "
                    "WHERE e.source_id = ? AND e.type = 'CALLS'",
                    (node_id,),
                )
                for row in cursor.fetchall():
                    target_callees.add(row[0])

                # Get callees of each sibling
                sibling_callee_counts: Counter[str] = Counter()
                for sib in siblings:
                    sib_cursor = self.store.connection.execute(
                        "SELECT n.name FROM edges e JOIN nodes n ON e.target_id = n.id "
                        "WHERE e.source_id = ? AND e.type = 'CALLS'",
                        (sib["id"],),
                    )
                    for row in sib_cursor.fetchall():
                        sibling_callee_counts[row[0]] += 1

                for callee, count in sibling_callee_counts.most_common(3):
                    freq = count / len(siblings)
                    if freq >= 0.6 and callee not in target_callees:
                        findings.append(PatternEvidence(
                            kind="missing_call", file_path=file_path, line=line,
                            message=f"{count}/{len(siblings)} siblings call {callee}() -- edit does not",
                            confidence=freq,
                        ))
                        break
        except Exception:
            pass  # CALLS edges may not exist

        # Dimension 5: Docstrings (if most siblings have docstrings but target doesn't)
        target_has_doc = any(p["kind"] == "docstring" for p in target_props)
        siblings_with_doc = sum(
            1 for sib in siblings
            if any(p["kind"] == "docstring" for p in sib["properties"])
        )
        doc_freq = siblings_with_doc / len(siblings) if siblings else 0
        if doc_freq >= 0.7 and not target_has_doc:
            findings.append(PatternEvidence(
                kind="missing_docstring", file_path=file_path, line=line,
                message=f"{siblings_with_doc}/{len(siblings)} siblings have docstrings -- edit does not",
                confidence=doc_freq * 0.7,  # lower weight than structural issues
            ))

        return findings

    def _analyze_python_ast(self, source: str, changed_func_name: str,
                            file_path: str) -> list[PatternEvidence]:
        """Python AST-based analysis (original behavior, preserved as fallback)."""
        findings: list[PatternEvidence] = []
        tree = _parse_safe(source)
        if not tree:
            return findings

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
                    elif not item.name.startswith("__"):
                        class_methods.append(item)
            if target:
                changed_node = target
                siblings = class_methods
                break

        # Module-level functions
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
        edit_exc = _get_exception_types_ast(changed_node)
        if edit_exc:
            sibling_exc_counts: Counter[str] = Counter()
            total_with_exc = 0
            for sib in siblings:
                sib_exc = _get_exception_types_ast(sib)
                if sib_exc:
                    total_with_exc += 1
                    for e in sib_exc:
                        sibling_exc_counts[e] += 1
            if total_with_exc >= 2:
                majority_exc, majority_count = sibling_exc_counts.most_common(1)[0]
                freq = majority_count / total_with_exc
                if freq >= 0.6 and majority_exc not in edit_exc:
                    findings.append(PatternEvidence(
                        kind="error_type_outlier", file_path=file_path, line=line,
                        message=f"{majority_count}/{total_with_exc} siblings raise {majority_exc} -- edit raises {', '.join(sorted(edit_exc))}",
                        confidence=freq,
                    ))

        # Dimension 2: Return shapes
        edit_shape = _classify_return_shape_ast(changed_node)
        sibling_shapes = Counter(_classify_return_shape_ast(s) for s in siblings)
        if sibling_shapes and edit_shape != "implicit_None":
            majority_shape, majority_count = sibling_shapes.most_common(1)[0]
            total = sum(sibling_shapes.values())
            freq = majority_count / total
            if freq >= 0.6 and edit_shape != majority_shape and majority_shape != "implicit_None":
                findings.append(PatternEvidence(
                    kind="return_shape_outlier", file_path=file_path, line=line,
                    message=f"{majority_count}/{total} siblings return {majority_shape} -- edit returns {edit_shape}",
                    confidence=freq,
                ))

        # Dimension 3: Guard clauses
        edit_has_guard = _has_guard_clause_ast(changed_node)
        siblings_with_guard = sum(1 for s in siblings if _has_guard_clause_ast(s))
        guard_freq = siblings_with_guard / len(siblings) if siblings else 0
        if guard_freq >= 0.6 and not edit_has_guard:
            findings.append(PatternEvidence(
                kind="missing_guard", file_path=file_path, line=line,
                message=f"{siblings_with_guard}/{len(siblings)} siblings have guard clauses -- edit does not",
                confidence=guard_freq,
            ))

        # Dimension 4: Framework calls
        edit_calls = _get_framework_calls_ast(changed_node)
        sibling_call_counts: Counter[str] = Counter()
        for sib in siblings:
            for call in _get_framework_calls_ast(sib):
                sibling_call_counts[call] += 1
        for call, count in sibling_call_counts.most_common(3):
            freq = count / len(siblings)
            if freq >= 0.6 and call not in edit_calls:
                findings.append(PatternEvidence(
                    kind="missing_call", file_path=file_path, line=line,
                    message=f"{count}/{len(siblings)} siblings call {call} -- edit does not",
                    confidence=freq,
                ))
                break

        # Dimension 5: API access pattern for shared parameter names
        changed_params = {
            a.arg for a in changed_node.args.args
            if a.arg not in ("self", "cls")
        }
        for param_name in changed_params:
            access_counts: Counter[str] = Counter()
            siblings_with_param = 0
            for sib in siblings:
                sib_param_names = {
                    a.arg for a in sib.args.args
                    if a.arg not in ("self", "cls")
                }
                if param_name not in sib_param_names:
                    continue
                siblings_with_param += 1
                for node in ast.walk(sib):
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id == param_name):
                        access_counts[f"{param_name}.{node.attr}"] += 1
                    if isinstance(node, ast.Call):
                        for arg in node.args:
                            if (isinstance(arg, ast.Name)
                                    and arg.id == param_name
                                    and isinstance(node.func, ast.Name)):
                                access_counts[f"{node.func.id}({param_name})"] += 1

            if not access_counts or siblings_with_param < 2:
                continue

            edit_accesses: set[str] = set()
            for node in ast.walk(changed_node):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == param_name):
                    edit_accesses.add(f"{param_name}.{node.attr}")
                if isinstance(node, ast.Call):
                    for arg in node.args:
                        if (isinstance(arg, ast.Name)
                                and arg.id == param_name
                                and isinstance(node.func, ast.Name)):
                            edit_accesses.add(f"{node.func.id}({param_name})")

            if not edit_accesses:
                continue

            majority_pattern, majority_count = access_counts.most_common(1)[0]
            freq = majority_count / max(siblings_with_param, 1)
            if freq >= 0.6 and majority_pattern not in edit_accesses:
                findings.append(PatternEvidence(
                    kind="api_access_outlier", file_path=file_path, line=line,
                    message=(
                        f"{majority_count}/{siblings_with_param} siblings access "
                        f"{param_name} via {majority_pattern} -- edit uses different pattern"
                    ),
                    confidence=freq,
                ))

        return findings


# ── Python AST helpers (fallback) ────────────────────────────────────────

def _parse_safe(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _get_exception_types_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    types = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


def _classify_return_shape_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
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


def _has_guard_clause_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for stmt in func.body[:5]:
        if isinstance(stmt, ast.If):
            for sub in stmt.body:
                if isinstance(sub, (ast.Raise, ast.Return)):
                    return True
    return False


def _get_framework_calls_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    calls = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                prefix = node.func.value.id
                if prefix in ("self", "cls", "super"):
                    calls.add(f"self.{node.func.attr}()")
    return calls
