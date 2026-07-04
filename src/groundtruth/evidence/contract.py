"""Contract evidence -- what callers and tests expect from changed symbols.

Mines: caller usage patterns (destructuring, attribute access, iteration),
test assertion patterns (assertEqual, assertRaises, etc.).

v16: Language-agnostic. Reads from graph.db properties/assertions tables first,
falls back to Python AST for Python files when tables are empty.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from groundtruth.index.graph_store import GraphStore


@dataclass
class CallerExpectation:
    """How a caller uses a symbol's return value."""

    file_path: str
    line: int
    usage_type: str  # destructure_tuple | destructure_list | attr_access | iterated | boolean_check | exception_guard
    detail: str
    confidence: float
    family: str = "contract"


@dataclass
class TestExpectation:
    """What a test asserts about a symbol."""

    test_file: str
    test_func: str
    line: int
    assertion_type: str  # assertEqual | assertRaises | assertIn | assertTrue | assert_compare
    expected: str  # serialized expected value
    confidence: float
    family: str = "contract"


def _read_file(root: str, relpath: str) -> str:
    try:
        path = os.path.join(root, relpath)
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_test_file(filepath: str) -> bool:
    """Language-agnostic test file detection."""
    fp = "/" + filepath.replace("\\", "/")
    fp_lower = fp.lower()
    if any(p in fp_lower for p in ["/tests/", "/test/", "/testing/", "/spec/", "/__tests__/"]):
        return True
    basename = os.path.basename(fp)
    stem, ext = os.path.splitext(basename)
    stem_lower = stem.lower()
    # Python
    if basename.lower().startswith("test_") or stem_lower.endswith("_test"):
        return True
    # Go
    if basename.endswith("_test.go"):
        return True
    # JS/TS
    if ".test." in basename or ".spec." in basename:
        return True
    # JVM/C#/PHP/Swift (case-sensitive: UserTest, UserTests, UserSpec)
    if stem.endswith("Test") or stem.endswith("Tests") or stem.endswith("Spec"):
        return True
    # Ruby RSpec
    if stem_lower.endswith("_spec"):
        return True
    return False


class CallerUsageMiner:
    """Mine how callers use a symbol's return value.

    Strategy: try graph.db properties first (works for all languages),
    fall back to Python AST for .py files.
    """

    def __init__(self, root: str, store: GraphStore | None = None):
        self.root = root
        self.store = store

    def mine(
        self, symbol_name: str, caller_files: list[str], caller_node_ids: list[int] | None = None
    ) -> list[CallerExpectation]:
        """Find how callers use the return value of symbol_name."""
        expectations: list[CallerExpectation] = []

        # Path 1: graph.db properties (language-agnostic)
        if self.store and caller_node_ids:
            for node_id in caller_node_ids[:10]:
                props = self.store.get_properties(node_id)
                for prop in props:
                    # Direct usage properties
                    if prop["kind"] in (
                        "destructure_tuple",
                        "iterated",
                        "boolean_check",
                        "attr_access",
                        "exception_guard",
                    ):
                        expectations.append(
                            CallerExpectation(
                                file_path="",
                                line=prop.get("line", 0) or 0,
                                usage_type=prop["kind"],
                                detail=prop["value"],
                                confidence=prop.get("confidence", 0.8) or 0.8,
                            )
                        )
                    # caller_usage properties from Go indexer (format: "usage_type:callee_name")
                    elif prop["kind"] == "caller_usage":
                        value = prop["value"]
                        parts = value.split(":", 1)
                        usage_type = parts[0]
                        callee = parts[1] if len(parts) > 1 else ""
                        # Only include if this usage is about our target symbol
                        if not symbol_name or callee == symbol_name:
                            expectations.append(
                                CallerExpectation(
                                    file_path="",
                                    line=prop.get("line", 0) or 0,
                                    usage_type=usage_type,
                                    detail=f"{usage_type} of {callee}" if callee else usage_type,
                                    confidence=prop.get("confidence", 0.8) or 0.8,
                                )
                            )

        if expectations:
            return expectations[:5]

        # Path 2: Python AST fallback (for .py files or when properties table is empty)
        for fpath in caller_files[:10]:
            if not fpath.endswith(".py"):
                continue
            source = _read_file(self.root, fpath)
            if not source:
                continue
            exps = self._mine_python_ast(source, symbol_name, fpath)
            expectations.extend(exps)

        return expectations[:5]

    def _mine_python_ast(
        self, source: str, symbol_name: str, file_path: str
    ) -> list[CallerExpectation]:
        """Python-specific AST mining (fallback)."""
        expectations: list[CallerExpectation] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return expectations

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = ""
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            if call_name != symbol_name:
                continue
            usage = self._classify_call_usage(tree, node, file_path)
            if usage:
                expectations.append(usage)

        return expectations

    def _classify_call_usage(
        self, tree: ast.Module, call_node: ast.Call, file_path: str
    ) -> CallerExpectation | None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                if child is call_node:
                    return self._classify_parent(node, call_node, file_path)
                if isinstance(node, ast.Assign):
                    if any(v is call_node for v in [node.value]):
                        return self._classify_assign_target(node, call_node, file_path)
        return None

    def _classify_assign_target(
        self, assign: ast.Assign, call: ast.Call, file_path: str
    ) -> CallerExpectation | None:
        for target in assign.targets:
            if isinstance(target, ast.Tuple):
                n = len(target.elts)
                names = []
                for elt in target.elts[:4]:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
                detail = (
                    f"unpacks as ({', '.join(names)})" if names else f"destructures into {n} values"
                )
                return CallerExpectation(
                    file_path=file_path,
                    line=assign.lineno,
                    usage_type="destructure_tuple",
                    detail=detail,
                    confidence=0.9,
                )
            elif isinstance(target, ast.Name):
                pass
        return None

    def _classify_parent(
        self, parent: ast.AST, call: ast.Call, file_path: str
    ) -> CallerExpectation | None:
        if isinstance(parent, ast.For) and parent.iter is call:
            return CallerExpectation(
                file_path=file_path,
                line=parent.lineno,
                usage_type="iterated",
                detail="iterated over in for loop",
                confidence=0.85,
            )
        if isinstance(parent, ast.If) and parent.test is call:
            return CallerExpectation(
                file_path=file_path,
                line=parent.lineno,
                usage_type="boolean_check",
                detail="used as boolean condition",
                confidence=0.7,
            )
        return None


class TestAssertionMiner:
    """Mine test assertions about a module's behavior.

    Strategy: try graph.db assertions table first (works for all languages),
    fall back to Python AST for .py files.
    """

    def __init__(self, root: str, store: GraphStore | None = None):
        self.root = root
        self.store = store

    def mine(
        self, changed_file: str, test_files: list[str], symbol_name: str | None = None
    ) -> list[TestExpectation]:
        """Find test assertions related to the changed module.

        Strategy (all languages get the same path):
        1. graph.db assertions table — by target name match (confidence 0.85)
        2. graph.db assertions table — by test file path (confidence 0.85)
        3. Python AST fallback — for .py files when graph.db is empty
        4. Regex fallback — for non-Python files when graph.db is empty
        """
        expectations: list[TestExpectation] = []

        # Path 1a: graph.db assertions by target name (most precise)
        if self.store and symbol_name:
            db_assertions = self.store.get_assertions_for_target(symbol_name)
            for a in db_assertions[:8]:
                expectations.append(
                    TestExpectation(
                        test_file=a.get("file_path", ""),
                        test_func=a.get("test_name", ""),
                        line=a.get("line", 0) or 0,
                        assertion_type=a.get("kind", "assert"),
                        expected=a.get("expression", ""),
                        confidence=0.85,
                    )
                )

        # Path 1b: graph.db assertions by test file (broader, catches non-name-matched)
        if not expectations and self.store:
            for test_file in test_files[:5]:
                file_assertions = self.store.get_assertions_in_file(test_file)
                for a in file_assertions[:8]:
                    expectations.append(
                        TestExpectation(
                            test_file=test_file,
                            test_func=a.get("test_name", ""),
                            line=a.get("line", 0) or 0,
                            assertion_type=a.get("kind", "assert"),
                            expected=a.get("expression", ""),
                            confidence=0.85,
                        )
                    )
                if expectations:
                    break  # found assertions in at least one test file

        if expectations:
            return expectations[:8]

        # Path 2: Source-based fallback (Python AST or regex)
        for test_file in test_files[:5]:
            source = _read_file(self.root, test_file)
            if not source:
                continue

            # Python AST for .py files (highest quality fallback)
            if test_file.endswith(".py"):
                exps = self._mine_python_ast(source, test_file)
                expectations.extend(exps)
                continue

            # Regex for non-Python files
            exps = self._mine_regex(source, test_file)
            expectations.extend(exps)

        return expectations[:8]

    def _mine_python_ast(self, source: str, test_file: str) -> list[TestExpectation]:
        """Python-specific AST mining (fallback)."""
        expectations: list[TestExpectation] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return expectations

        source_lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            for stmt in ast.walk(node):
                exp = self._extract_assertion_ast(
                    stmt,
                    test_file,
                    node.name,
                    source_lines=source_lines,
                )
                if exp:
                    expectations.append(exp)

        return expectations

    def _mine_regex(self, source: str, test_file: str) -> list[TestExpectation]:
        """Regex-based assertion extraction for non-Python files."""
        import re

        expectations: list[TestExpectation] = []
        patterns = [
            (r"assert\w*\s*\((.{5,80})\)", "assert"),
            (r"expect\((.{5,80})\)", "expect"),
            (r"Assert\.\w+\((.{5,80})\)", "Assert"),
            (r"t\.\w+\((.{5,80})\)", "t_method"),
            (r"assert!\((.{5,80})\)", "assert_macro"),
            (r"assert_eq!\((.{5,80})\)", "assert_eq"),
            (r"XCTAssert\w*\((.{5,80})\)", "XCTAssert"),
        ]
        for line_no, line in enumerate(source.splitlines(), 1):
            for pat, kind in patterns:
                m = re.search(pat, line)
                if m:
                    expectations.append(
                        TestExpectation(
                            test_file=test_file,
                            test_func="",
                            line=line_no,
                            assertion_type=kind,
                            expected=m.group(0).strip()[:120],
                            confidence=0.6,
                        )
                    )
                    break  # one per line
            if len(expectations) >= 8:
                break
        return expectations

    def _extract_assertion_ast(
        self,
        node: ast.AST,
        test_file: str,
        test_func: str,
        source_lines: list[str] | None = None,
    ) -> TestExpectation | None:
        """Extract assertion from a Python AST node."""
        if isinstance(node, ast.Assert) and node.test is not None:
            try:
                expr = ast.unparse(node.test)[:120]
            except Exception:
                expr = ""
            if expr:
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=getattr(node, "lineno", 0),
                    assertion_type="assert",
                    expected=expr,
                    confidence=0.85,
                )

        if not isinstance(node, ast.Call):
            return None

        if isinstance(node.func, ast.Attribute):
            method = node.func.attr

            if method == "assertEqual" and len(node.args) >= 2:
                try:
                    lhs = ast.unparse(node.args[0])[:60]
                    rhs = ast.unparse(node.args[1])[:60]
                    expected = f"{lhs} == {rhs}"
                except Exception:
                    expected = ast.dump(node.args[1])[:60]
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=node.lineno,
                    assertion_type="assertEqual",
                    expected=expected,
                    confidence=0.85,
                )

            if method == "assertRaises" and len(node.args) >= 1:
                exc_type = ""
                if isinstance(node.args[0], ast.Name):
                    exc_type = node.args[0].id
                elif isinstance(node.args[0], ast.Attribute):
                    exc_type = node.args[0].attr
                if exc_type:
                    return TestExpectation(
                        test_file=test_file,
                        test_func=test_func,
                        line=node.lineno,
                        assertion_type="assertRaises",
                        expected=exc_type,
                        confidence=0.9,
                    )

            if method == "assertIn" and len(node.args) >= 2:
                try:
                    needle = ast.unparse(node.args[0])[:50]
                    haystack = ast.unparse(node.args[1])[:50]
                    expected = f"{needle} in {haystack}"
                except Exception:
                    expected = ast.dump(node.args[0])[:40]
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=node.lineno,
                    assertion_type="assertIn",
                    expected=expected,
                    confidence=0.8,
                )

            if method in ("assertTrue", "assertFalse") and len(node.args) >= 1:
                try:
                    expr = ast.unparse(node.args[0])[:80]
                except Exception:
                    expr = ast.dump(node.args[0])[:60]
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=node.lineno,
                    assertion_type=method,
                    expected=expr,
                    confidence=0.7,
                )

            if method.startswith("assert") and len(node.args) >= 1:
                try:
                    args_str = ", ".join(ast.unparse(a)[:40] for a in node.args[:3])
                    expected = f"{method}({args_str})"
                except Exception:
                    expected = method
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=getattr(node, "lineno", 0),
                    assertion_type=method,
                    expected=expected,
                    confidence=0.75,
                )

        # pytest.raises(ExcType)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "raises"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pytest"
            ):
                exc_type = ""
                if node.args and isinstance(node.args[0], ast.Name):
                    exc_type = node.args[0].id
                elif node.args and isinstance(node.args[0], ast.Attribute):
                    exc_type = node.args[0].attr
                if exc_type:
                    return TestExpectation(
                        test_file=test_file,
                        test_func=test_func,
                        line=getattr(node, "lineno", 0),
                        assertion_type="pytest.raises",
                        expected=exc_type,
                        confidence=0.9,
                    )

        return None
