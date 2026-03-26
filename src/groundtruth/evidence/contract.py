"""Contract evidence -- what callers and tests expect from changed symbols.

Mines: caller usage patterns (destructuring, attribute access, iteration),
test assertion patterns (assertEqual, assertRaises, etc.). Pure stdlib ast.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass


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
    fp = "/" + filepath.lower().replace("\\", "/")
    if any(p in fp for p in ["/tests/", "/test/", "/testing/"]):
        return True
    basename = os.path.basename(fp)
    return basename.startswith("test_") or basename.endswith("_test.py")


class CallerUsageMiner:
    """Mine how callers use a symbol's return value."""

    def __init__(self, root: str):
        self.root = root

    def mine(self, symbol_name: str, caller_files: list[str]) -> list[CallerExpectation]:
        """Find how callers use the return value of symbol_name."""
        expectations: list[CallerExpectation] = []

        for fpath in caller_files[:10]:  # cap at 10 caller files
            source = _read_file(self.root, fpath)
            if not source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                # Check if this call is to our symbol
                call_name = ""
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr

                if call_name != symbol_name:
                    continue

                # Find parent context to determine usage
                # We need to walk the tree with parent tracking
                usage = self._classify_call_usage(tree, node, fpath)
                if usage:
                    expectations.append(usage)

        return expectations[:5]  # cap at 5

    def _classify_call_usage(self, tree: ast.Module, call_node: ast.Call,
                              file_path: str) -> CallerExpectation | None:
        """Classify how the return value of a call is used."""
        # Walk tree to find parent of call_node
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                if child is call_node:
                    return self._classify_parent(node, call_node, file_path)

                # Check assignments: x, y = func()
                if isinstance(node, ast.Assign):
                    if any(v is call_node for v in [node.value]):
                        return self._classify_assign_target(node, call_node, file_path)
        return None

    def _classify_assign_target(self, assign: ast.Assign, call: ast.Call,
                                 file_path: str) -> CallerExpectation | None:
        """Classify based on assignment target."""
        for target in assign.targets:
            if isinstance(target, ast.Tuple):
                n = len(target.elts)
                names = []
                for elt in target.elts[:4]:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
                detail = f"unpacks as ({', '.join(names)})" if names else f"destructures into {n} values"
                return CallerExpectation(
                    file_path=file_path,
                    line=assign.lineno,
                    usage_type="destructure_tuple",
                    detail=detail,
                    confidence=0.9,
                )
            elif isinstance(target, ast.Name):
                # Single assignment — check if result is used with attribute access later
                pass
        return None

    def _classify_parent(self, parent: ast.AST, call: ast.Call,
                          file_path: str) -> CallerExpectation | None:
        """Classify based on parent node type."""
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
    """Mine test assertions about a module's behavior."""

    def __init__(self, root: str):
        self.root = root

    def mine(self, changed_file: str, test_files: list[str]) -> list[TestExpectation]:
        """Find test assertions related to the changed module."""
        expectations: list[TestExpectation] = []

        # Get module name from file path for matching
        module_name = os.path.splitext(os.path.basename(changed_file))[0]

        for test_file in test_files[:5]:  # cap at 5 test files
            source = _read_file(self.root, test_file)
            if not source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test"):
                    continue

                # Search for assert patterns in test function
                for stmt in ast.walk(node):
                    exp = self._extract_assertion(stmt, test_file, node.name)
                    if exp:
                        expectations.append(exp)

        return expectations[:5]  # cap at 5

    def _extract_assertion(self, node: ast.AST, test_file: str,
                            test_func: str) -> TestExpectation | None:
        """Extract assertion from an AST node."""
        if not isinstance(node, ast.Call):
            return None

        # self.assertEqual(a, b) / self.assertRaises(ExcType)
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr

            if method == "assertEqual" and len(node.args) >= 2:
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
                needle = ast.dump(node.args[0])[:40]
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=node.lineno,
                    assertion_type="assertIn",
                    expected=needle,
                    confidence=0.8,
                )

            if method in ("assertTrue", "assertFalse") and len(node.args) >= 1:
                expr = ast.dump(node.args[0])[:60]
                return TestExpectation(
                    test_file=test_file,
                    test_func=test_func,
                    line=node.lineno,
                    assertion_type=method,
                    expected=expr,
                    confidence=0.7,
                )

        # assert a == b (plain assert)
        if isinstance(node, ast.Compare):
            # This is handled at the statement level, not here
            pass

        return None
