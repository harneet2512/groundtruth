"""GroundTruth post-edit hook — amalgamated single-file version for SWE-bench containers.

This file combines all evidence modules and the post-edit hook into a single
stdlib-only script that can be injected into Docker containers without any
package installation.

Usage:
    python3 /tmp/gt_hook.py --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3

Evidence families:
    CHANGE     -- before/after AST diff on changed functions
    CONTRACT   -- caller usage patterns + test assertions
    PATTERN    -- sibling analysis across N dimensions
    STRUCTURAL -- obligation / contradiction / convention checks (thin wrapper,
                  gracefully no-ops if groundtruth package is absent)
    SEMANTIC   -- call-site voting, argument affinity, guard consistency

All groundtruth.* imports in the STRUCTURAL section are wrapped in try/except
and will silently do nothing in containers where the full package is not present.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# STDLIB IMPORTS (all merged)
# ---------------------------------------------------------------------------

import argparse
import ast
import copy
import glob as _glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# SHARED UTILS
# ---------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    """Git environment that handles safe.directory in containers."""
    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


def _read_file(root: str, relpath: str) -> str:
    try:
        path = os.path.join(root, relpath)
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_test_file(filepath: str) -> bool:
    fp = "/" + filepath.lower().replace("\\", "/")
    if any(p in fp for p in ["/tests/", "/test/", "/testing/", "/__tests__/", "/spec/", "/specs/"]):
        return True
    basename = os.path.basename(fp)
    return (
        basename.startswith("test_")
        or basename.endswith("_test.py")
        or basename.endswith("_test.go")
        or basename.endswith(".test.js")
        or basename.endswith(".test.ts")
        or basename.endswith(".test.tsx")
        or basename.endswith(".spec.js")
        or basename.endswith(".spec.ts")
        or basename.endswith(".spec.tsx")
        or basename.endswith("Test.java")
        or basename.endswith("_test.rb")
    )


def _parse_safe(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


# ---------------------------------------------------------------------------
# LOGGER
# ---------------------------------------------------------------------------

HOOK_LOG = os.path.join(tempfile.gettempdir(), "gt_hook_log.jsonl")



def log_hook(entry: dict) -> None:
    """Append one JSON line to the hook log. Never raises."""
    try:
        entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(HOOK_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def get_logger(name: str) -> logging.Logger:
    """Get a stdlib logger (structlog-free for container compatibility)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
    return logger


# ---------------------------------------------------------------------------
# CHANGE EVIDENCE
# ---------------------------------------------------------------------------

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
            env=_git_env(),
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _find_function(tree: ast.Module, func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a function/method by name in the AST."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node
    return None


def _get_guard_clauses(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str]]:
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


def _get_except_handlers(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
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


def _classify_return_shape(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
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
    return Counter(shapes).most_common(1)[0][0]


def _get_raise_types(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get all exception types raised in a function."""
    types = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


def _parse_diff_changed_funcs(diff_text: str) -> list[tuple[str, None, int, int]]:
    """Parse diff to find (file_path, None, start_line, end_line) of changes."""
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
            changed_funcs: set[str] = set()
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


# ---------------------------------------------------------------------------
# CONTRACT EVIDENCE
# ---------------------------------------------------------------------------

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

                usage = self._classify_call_usage(tree, node, fpath)
                if usage:
                    expectations.append(usage)

        return expectations[:5]  # cap at 5

    def _classify_call_usage(self, tree: ast.Module, call_node: ast.Call,
                              file_path: str) -> CallerExpectation | None:
        """Classify how the return value of a call is used."""
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                if child is call_node:
                    return self._classify_parent(node, call_node, file_path)

                if isinstance(node, ast.Assign):
                    if any(v is call_node for v in [node.value]):
                        return self._classify_assign_target(node, call_node, file_path)
        return None

    def _classify_assign_target(self, assign: ast.Assign, _call: ast.Call,
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

    def mine(self, _changed_file: str, test_files: list[str]) -> list[TestExpectation]:
        """Find test assertions related to the changed module."""
        expectations: list[TestExpectation] = []

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

        return None


class RegexTestAssertionMiner:
    """Mine test assertions from non-Python files using regex patterns."""

    # JS/TS assertion patterns
    _JS_PATTERNS = [
        (r'expect\((.+?)\)\.toBe\((.+?)\)', "toBe", 0.85),
        (r'expect\((.+?)\)\.toEqual\((.+?)\)', "toEqual", 0.85),
        (r'expect\((.+?)\)\.toThrow\((.+?)\)', "toThrow", 0.90),
        (r'expect\((.+?)\)\.toHaveBeenCalled', "toHaveBeenCalled", 0.80),
        (r'expect\((.+?)\)\.toBeTruthy', "toBeTruthy", 0.70),
        (r'expect\((.+?)\)\.toBeFalsy', "toBeFalsy", 0.70),
        (r'expect\((.+?)\)\.toContain\((.+?)\)', "toContain", 0.80),
        (r'assert\.equal\((.+?),\s*(.+?)\)', "assertEqual", 0.85),
        (r'assert\.deepEqual\((.+?),\s*(.+?)\)', "assertDeepEqual", 0.85),
        (r'assert\.throws\((.+?)\)', "assertThrows", 0.90),
        (r'assert\.ok\((.+?)\)', "assertTrue", 0.70),
        (r'assert\((.+?)\)', "assert", 0.70),
    ]

    # Go assertion patterns
    _GO_PATTERNS = [
        (r'if\s+(.+?)\s*!=\s*(.+?)\s*\{', "notEqual", 0.80),
        (r't\.(?:Error|Fatal|Fail)f?\((.+?)\)', "tError", 0.85),
        (r'assert\.Equal\(t,\s*(.+?),\s*(.+?)\)', "assertEqual", 0.85),
        (r'assert\.NoError\(t,\s*(.+?)\)', "assertNoError", 0.85),
        (r'require\.Equal\(t,\s*(.+?),\s*(.+?)\)', "assertEqual", 0.90),
    ]

    # Test function patterns
    _JS_TEST_FUNC = re.compile(r'(?:it|test|describe)\s*\(\s*[\'"](.+?)[\'"]', re.MULTILINE)
    _GO_TEST_FUNC = re.compile(r'func\s+(Test\w+)\s*\(', re.MULTILINE)

    def __init__(self, root: str):
        self.root = root

    def mine(self, _changed_file: str, test_files: list[str]) -> list[TestExpectation]:
        """Extract assertions from non-Python test files using regex."""
        expectations: list[TestExpectation] = []

        for test_file in test_files[:5]:
            source = _read_file(self.root, test_file)
            if not source:
                continue

            ext = os.path.splitext(test_file)[1]
            if ext in (".js", ".ts", ".jsx", ".tsx"):
                patterns = self._JS_PATTERNS
                func_pattern = self._JS_TEST_FUNC
            elif ext == ".go":
                patterns = self._GO_PATTERNS
                func_pattern = self._GO_TEST_FUNC
            else:
                continue

            # Find test function names
            test_funcs = []
            for m in func_pattern.finditer(source):
                test_funcs.append((m.group(1), m.start()))

            # Find assertions
            for pat_str, atype, confidence in patterns:
                for m in re.finditer(pat_str, source):
                    line = source[:m.start()].count("\n") + 1
                    expected = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
                    if len(expected) > 60:
                        expected = expected[:57] + "..."

                    # Find enclosing test function
                    tfunc = "?"
                    for fname, fstart in reversed(test_funcs):
                        if fstart < m.start():
                            tfunc = fname
                            break

                    expectations.append(TestExpectation(
                        test_file=test_file,
                        test_func=tfunc,
                        line=line,
                        assertion_type=atype,
                        expected=expected,
                        confidence=confidence,
                    ))

        return expectations[:5]


# ---------------------------------------------------------------------------
# PATTERN EVIDENCE
# ---------------------------------------------------------------------------

@dataclass
class PatternEvidence:
    """A detected pattern deviation from siblings."""
    kind: str  # error_type_outlier | return_shape_outlier | missing_guard | missing_call | param_mismatch
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "pattern"


def _get_exception_types(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get all exception types raised in a function."""
    types: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


def _classify_return_shape_pattern(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Classify dominant return shape (pattern variant — returns implicit_None for empty)."""
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


def _has_guard_clause(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has guard clauses (if-raise/if-return at top)."""
    for stmt in func.body[:5]:
        if isinstance(stmt, ast.If):
            for sub in stmt.body:
                if isinstance(sub, (ast.Raise, ast.Return)):
                    return True
    return False


def _get_framework_calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get self.method() and module.func() calls."""
    calls: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                prefix = node.func.value.id
                if prefix in ("self", "cls", "super"):
                    calls.add(f"self.{node.func.attr}()")
    return calls


class SiblingAnalyzer:
    """Compare a changed function against its siblings."""

    def analyze(self, source: str, changed_func_name: str,
                file_path: str = "") -> list[PatternEvidence]:
        """Analyze the changed function against siblings in the same scope."""
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
                    else:
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
        edit_shape = _classify_return_shape_pattern(changed_node)
        sibling_shapes = Counter(_classify_return_shape_pattern(s) for s in siblings)
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
                    kind="api_access_outlier",
                    file_path=file_path,
                    line=line,
                    message=(
                        f"{majority_count}/{siblings_with_param} siblings access "
                        f"{param_name} via {majority_pattern} -- edit uses different pattern"
                    ),
                    confidence=freq,
                ))

        return findings


# ---------------------------------------------------------------------------
# STRUCTURAL EVIDENCE
# ---------------------------------------------------------------------------

@dataclass
class StructuralEvidence:
    """A structural finding from existing validators."""
    kind: str  # obligation | contradiction | convention
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "structural"


def run_obligations(store: Any, graph: Any, diff_text: str) -> list[StructuralEvidence]:
    """Run ObligationEngine and convert to evidence items."""
    try:
        from groundtruth.validators.obligations import ObligationEngine  # type: ignore[import]
        engine = ObligationEngine(store, graph)
        obligations = engine.infer_from_patch(diff_text)
        return [
            StructuralEvidence(
                kind="obligation",
                file_path=ob.target_file,
                line=ob.target_line or 0,
                message=f"{ob.target} -- {ob.reason}",
                confidence=ob.confidence,
            )
            for ob in obligations
        ]
    except Exception:
        return []


def run_contradictions(store: Any, root: str, modified_files: list[str]) -> list[StructuralEvidence]:
    """Run ContradictionDetector and convert to evidence items."""
    try:
        from groundtruth.validators.contradictions import ContradictionDetector  # type: ignore[import]
        detector = ContradictionDetector(store)
        results = []
        for fpath in modified_files[:5]:
            try:
                with open(os.path.join(root, fpath), "r", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            for c in detector.check_file(fpath, source):
                results.append(StructuralEvidence(
                    kind="contradiction",
                    file_path=c.file_path,
                    line=c.line or 0,
                    message=c.message,
                    confidence=c.confidence,
                ))
        return results
    except Exception:
        return []


def run_conventions(root: str, modified_files: list[str]) -> list[StructuralEvidence]:
    """Run ConventionChecker and convert to evidence items."""
    try:
        from groundtruth.analysis.conventions import detect_all  # type: ignore[import]
        results = []
        for fpath in modified_files[:5]:
            try:
                with open(os.path.join(root, fpath), "r", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            for conv in detect_all(source, scope=fpath):
                if conv.frequency < 1.0 and conv.confidence >= 0.6:
                    results.append(StructuralEvidence(
                        kind="convention",
                        file_path=fpath,
                        line=0,
                        message=conv.pattern,
                        confidence=conv.confidence,
                    ))
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# BEHAVIORAL INTELLIGENCE (v6 understand pipeline)
# ---------------------------------------------------------------------------

@dataclass
class BehavioralFingerprint:
    """What a function DOES, not what it IS."""
    name: str
    line: int
    reads_self: list  # self.X attribute loads
    reads_params: list  # parameter name references
    writes_self: list  # self.X = ... stores
    return_shape: str  # scalar/tuple/dict/list/None-possible/conditional
    raises: list  # exception type names
    guard_conditions: list  # first if-raise/if-return patterns
    calls: list  # func() and self.method() calls
    is_abstract: bool  # raises NotImplementedError only


class BehavioralFingerprinter:
    """Extract behavioral contract summaries from functions using AST."""

    def fingerprint_function(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> BehavioralFingerprint:
        reads_self, reads_params = self._extract_reads(func)
        return BehavioralFingerprint(
            name=func.name,
            line=func.lineno,
            reads_self=reads_self,
            reads_params=reads_params,
            writes_self=self._extract_writes(func),
            return_shape=_classify_return_shape(func),
            raises=sorted(_get_raise_types(func)),
            guard_conditions=[g[1][:60] for g in _get_guard_clauses(func)],
            calls=self._extract_calls(func),
            is_abstract=self._is_abstract(func),
        )

    def fingerprint_class(self, cls: ast.ClassDef) -> list[BehavioralFingerprint]:
        fps = []
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip very short functions (< 3 statements)
                if len(node.body) < 3:
                    # Still fingerprint if it has meaningful content (not just pass/return/...)
                    if len(node.body) == 1 and isinstance(node.body[0], (ast.Pass, ast.Expr)):
                        continue
                fps.append(self.fingerprint_function(node))
        return fps

    def _extract_reads(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list, list]:
        """Extract self.X loads and parameter references."""
        self_reads: list[str] = []
        param_names: set[str] = set()

        # Collect parameter names (skip 'self' and 'cls')
        for arg in func.args.args:
            name = arg.arg
            if name not in ("self", "cls"):
                param_names.add(name)

        param_reads: set[str] = set()

        for node in ast.walk(func):
            # self.X attribute loads
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    attr = f"self.{node.attr}"
                    if attr not in self_reads:
                        self_reads.append(attr)
            # Parameter name references
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in param_names:
                    param_reads.add(node.id)

        return self_reads, sorted(param_reads)

    def _extract_writes(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """Extract self.X stores."""
        writes: list[str] = []
        for node in ast.walk(func):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                if isinstance(node.value, ast.Name) and node.value.id == "self":
                    attr = f"self.{node.attr}"
                    if attr not in writes:
                        writes.append(attr)
        return writes

    def _extract_calls(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """Extract function/method calls."""
        calls: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                call_name = self._call_name(node.func)
                if call_name and call_name not in seen:
                    seen.add(call_name)
                    calls.append(call_name)
        return calls[:15]  # cap to avoid noise

    def _call_name(self, node: ast.expr) -> str:
        """Extract readable name from a Call's func node."""
        if isinstance(node, ast.Name):
            return f"{node.id}()"
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                return f"{node.value.id}.{node.attr}()"
            # self.method() or cls.method()
            if isinstance(node.value, ast.Attribute):
                # e.g., user.__class__.get_email_field_name()
                inner = self._call_name(node.value)
                if inner:
                    return f"{inner.rstrip('()')}.{node.attr}()"
            return f"?.{node.attr}()"
        return ""

    def _is_abstract(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Check if function only raises NotImplementedError."""
        body = func.body
        # Filter docstrings
        stmts = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if len(stmts) != 1:
            return False
        stmt = stmts[0]
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            if isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name):
                return stmt.exc.func.id == "NotImplementedError"
            if isinstance(stmt.exc, ast.Name):
                return stmt.exc.id == "NotImplementedError"
        return False


@dataclass
class MinedRule:
    """An implicit rule mined from behavioral contract patterns."""
    dimension: str  # parameter_access, exception_type, return_shape, guard_clause, call_pattern
    pattern: str  # human-readable pattern description
    frequency: str  # "6/6", "8/9"
    confidence: float  # 0.0-1.0
    evidence_methods: list


class RuleMiner:
    """Mine implicit rules from behavioral contract patterns across a class/module."""

    MIN_METHODS = 3
    THRESHOLD = 0.8

    def mine(self, fingerprints: list[BehavioralFingerprint]) -> list[MinedRule]:
        """Mine rules from a list of fingerprints (typically from one class)."""
        # Filter abstract methods
        fps = [fp for fp in fingerprints if not fp.is_abstract]
        if len(fps) < self.MIN_METHODS:
            return []

        rules: list[MinedRule] = []
        r = self._mine_return_shapes(fps)
        if r:
            rules.append(r)
        r = self._mine_raises(fps)
        if r:
            rules.append(r)
        r = self._mine_guards(fps)
        if r:
            rules.append(r)
        r = self._mine_no_writes(fps)
        if r:
            rules.append(r)
        rules.extend(self._mine_param_access(fps))
        r = self._mine_call_patterns(fps)
        if r:
            rules.append(r)
        return rules

    def _mine_return_shapes(self, fps: list[BehavioralFingerprint]) -> MinedRule | None:
        """Check if most methods share the same return shape."""
        shapes = Counter(fp.return_shape for fp in fps)
        top_shape, count = shapes.most_common(1)[0]
        ratio = count / len(fps)
        if ratio >= self.THRESHOLD and top_shape != "None":
            return MinedRule(
                dimension="return_shape",
                pattern=f"returns {top_shape}",
                frequency=f"{count}/{len(fps)}",
                confidence=ratio,
                evidence_methods=[fp.name for fp in fps if fp.return_shape == top_shape],
            )
        return None

    def _mine_raises(self, fps: list[BehavioralFingerprint]) -> MinedRule | None:
        """Check if most methods raise the same exception type."""
        fps_with_raises = [fp for fp in fps if fp.raises]
        if len(fps_with_raises) < self.MIN_METHODS:
            return None
        all_types: Counter[str] = Counter()
        for fp in fps_with_raises:
            for t in fp.raises:
                all_types[t] += 1
        if not all_types:
            return None
        top_type, count = all_types.most_common(1)[0]
        ratio = count / len(fps_with_raises)
        if ratio >= self.THRESHOLD:
            return MinedRule(
                dimension="exception_type",
                pattern=f"raises {top_type}",
                frequency=f"{count}/{len(fps_with_raises)}",
                confidence=ratio,
                evidence_methods=[fp.name for fp in fps_with_raises if top_type in fp.raises],
            )
        return None

    def _mine_guards(self, fps: list[BehavioralFingerprint]) -> MinedRule | None:
        """Check if most methods have guard clauses."""
        with_guards = [fp for fp in fps if fp.guard_conditions]
        ratio = len(with_guards) / len(fps)
        if ratio >= self.THRESHOLD and len(with_guards) >= self.MIN_METHODS:
            return MinedRule(
                dimension="guard_clause",
                pattern="has guard clauses (if-raise/if-return at top)",
                frequency=f"{len(with_guards)}/{len(fps)}",
                confidence=ratio,
                evidence_methods=[fp.name for fp in with_guards],
            )
        return None

    def _mine_no_writes(self, fps: list[BehavioralFingerprint]) -> MinedRule | None:
        """Check if no methods write to self (immutable pattern)."""
        without_writes = [fp for fp in fps if not fp.writes_self]
        ratio = len(without_writes) / len(fps)
        if ratio >= 1.0 and len(fps) >= self.MIN_METHODS:
            return MinedRule(
                dimension="state_mutation",
                pattern="no methods write to self.* (stateless/immutable)",
                frequency=f"{len(without_writes)}/{len(fps)}",
                confidence=1.0,
                evidence_methods=[fp.name for fp in without_writes],
            )
        return None

    def _mine_param_access(self, fps: list[BehavioralFingerprint]) -> list[MinedRule]:
        """Mine parameter access pattern rules (e.g., 'user accessed via user.__class__')."""
        rules: list[MinedRule] = []
        # Collect all self.reads that look like param.something patterns
        # Look for common attribute access patterns on self
        attr_counter: Counter[str] = Counter()
        for fp in fps:
            for read in fp.reads_self:
                attr_counter[read] += 1

        for attr, count in attr_counter.most_common(10):
            ratio = count / len(fps)
            if ratio >= self.THRESHOLD and count >= self.MIN_METHODS:
                rules.append(MinedRule(
                    dimension="parameter_access",
                    pattern=f"reads {attr}",
                    frequency=f"{count}/{len(fps)}",
                    confidence=ratio,
                    evidence_methods=[fp.name for fp in fps if attr in fp.reads_self],
                ))

        # Look for common call patterns that indicate access conventions
        # e.g., user.__class__.get_email_field_name() across methods
        call_counter: Counter[str] = Counter()
        for fp in fps:
            for call in fp.calls:
                if ".__" in call or "." in call:
                    call_counter[call] += 1

        for call, count in call_counter.most_common(5):
            ratio = count / len(fps)
            if ratio >= self.THRESHOLD and count >= self.MIN_METHODS:
                rules.append(MinedRule(
                    dimension="parameter_access",
                    pattern=f"calls {call}",
                    frequency=f"{count}/{len(fps)}",
                    confidence=ratio,
                    evidence_methods=[fp.name for fp in fps if call in fp.calls],
                ))

        return rules

    def _mine_call_patterns(self, fps: list[BehavioralFingerprint]) -> MinedRule | None:
        """Check if most methods call a common function before writes."""
        # Find calls common to ≥80% of methods
        call_counter: Counter[str] = Counter()
        for fp in fps:
            for call in set(fp.calls):  # dedupe per-function
                call_counter[call] += 1

        for call, count in call_counter.most_common(3):
            ratio = count / len(fps)
            if ratio >= self.THRESHOLD and count >= self.MIN_METHODS:
                # Skip very common builtins
                if call in ("str()", "int()", "len()", "bool()", "type()", "isinstance()",
                            "super()", "getattr()", "setattr()", "hasattr()"):
                    continue
                return MinedRule(
                    dimension="call_pattern",
                    pattern=f"all methods call {call}",
                    frequency=f"{count}/{len(fps)}",
                    confidence=ratio,
                    evidence_methods=[fp.name for fp in fps if call in fp.calls],
                )
        return None


@dataclass
class SystemShape:
    """System-level context for a symbol."""
    callers_in_file: list  # [{name, usage_type}]
    git_churn: int | None  # changes in 6 months, None if unavailable
    criticality: str  # high/medium/low
    criticality_reason: str


class SystemShapeAnalyzer:
    """Compute system-level context for a symbol."""

    CRITICAL_PATHS = frozenset({
        "auth", "security", "session", "password", "token",
        "permission", "payment", "crypto", "login", "credential",
    })

    def analyze(self, func_name: str, source: str, filepath: str, root: str) -> SystemShape:
        tree = _parse_safe(source)
        callers = self._find_callers_in_file(func_name, tree) if tree else []
        churn = self._git_churn(filepath, root)
        criticality, reason = self._assess_criticality(filepath, callers)
        return SystemShape(
            callers_in_file=callers,
            git_churn=churn,
            criticality=criticality,
            criticality_reason=reason,
        )

    def _find_callers_in_file(self, func_name: str, tree: ast.Module | None) -> list[dict]:
        """Find functions/methods in the same file that call func_name."""
        if tree is None:
            return []
        callers: list[dict] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == func_name:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    name = ""
                    if isinstance(child.func, ast.Name):
                        name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        name = child.func.attr
                    if name == func_name:
                        # Classify how the return value is used
                        usage = self._classify_usage(child, node)
                        callers.append({"name": node.name, "usage_type": usage})
                        break
        return callers

    def _classify_usage(self, call_node: ast.Call, parent_func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """Classify how a call's return value is used in context."""
        # Walk the parent function to find the statement containing this call
        for node in ast.walk(parent_func):
            if isinstance(node, ast.Assign):
                if any(self._contains_call(v, call_node) for v in [node.value]):
                    return "stores_result"
            if isinstance(node, ast.If):
                if self._contains_call(node.test, call_node):
                    return "uses_as_condition"
            if isinstance(node, ast.Return):
                if node.value and self._contains_call(node.value, call_node):
                    return "returns_result"
        return "calls"

    def _contains_call(self, node: ast.AST, target: ast.Call) -> bool:
        """Check if node contains the target call (by identity)."""
        for child in ast.walk(node):
            if child is target:
                return True
        return False

    def _git_churn(self, filepath: str, root: str) -> int | None:
        """Count changes to file in last 6 months."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--since=6 months ago", "--", filepath],
                capture_output=True, text=True, cwd=root, timeout=5,
                env=_git_env(),
            )
            if result.returncode == 0:
                lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                return len(lines)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _assess_criticality(self, filepath: str, callers: list[dict]) -> tuple[str, str]:
        """Assess criticality based on file path and caller context."""
        fp_lower = filepath.lower().replace("\\", "/")
        for keyword in self.CRITICAL_PATHS:
            if keyword in fp_lower:
                return "high", f"{keyword} path"
        if len(callers) >= 5:
            return "medium", f"{len(callers)} in-file callers"
        return "low", ""


# ---------------------------------------------------------------------------
# CROSS-FILE CONSTRAINT MAP (v7)
# ---------------------------------------------------------------------------

_INDEX_CACHE_PATH = os.path.join(tempfile.gettempdir(), "gt_index.json")
_INDEX_BUILD_TIMEOUT = 25  # seconds
_INDEX_MAX_FILES = 5000
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".tox", ".eggs",
                         ".mypy_cache", ".pytest_cache", "dist", "build", ".venv", "venv"})


def _classify_reference_usage(call_node: ast.Call, parent_stmt: ast.AST) -> str:
    """Classify HOW a call's return value is used by examining the parent statement."""
    if isinstance(parent_stmt, ast.Assign):
        targets = parent_stmt.targets
        if len(targets) == 1:
            t = targets[0]
            if isinstance(t, ast.Tuple):
                return f"destructure_tuple({len(t.elts)})"
            if isinstance(t, ast.List):
                return f"destructure_list({len(t.elts)})"
            if isinstance(t, ast.Attribute):
                return "attr_store"
        return "assigned_to_var"
    if isinstance(parent_stmt, ast.Return):
        return "returned"
    if isinstance(parent_stmt, ast.If):
        return "boolean_test"
    if isinstance(parent_stmt, ast.For):
        return "iteration"
    if isinstance(parent_stmt, ast.Assert):
        return "assertion"
    if isinstance(parent_stmt, ast.Expr):
        # bare call — result discarded
        return "discarded"
    if isinstance(parent_stmt, (ast.Compare,)):
        return "comparison"
    # Check if it's an argument to another call
    if isinstance(parent_stmt, ast.Call):
        return "passed_as_arg"
    return "other"


def _find_enclosing_func(file_tree: ast.Module, lineno: int) -> str:
    """Find the function/method name that encloses a given line number."""
    best_name = "<module>"
    best_start = 0
    for node in ast.walk(file_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno + 500)
            if node.lineno <= lineno <= end and node.lineno > best_start:
                best_name = node.name
                best_start = node.lineno
    return best_name


_LANG_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb"}


def _detect_repo_language(root: str) -> str:
    """Detect dominant language in repo by file count (fast sample)."""
    counts: dict[str, int] = {}
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext in _LANG_EXTS:
                counts[ext] = counts.get(ext, 0) + 1
                checked += 1
        if checked > 500:
            break
    if not counts:
        return "python"
    return max(counts, key=counts.get)  # type: ignore[arg-type]


def _walk_source_files(root: str, exts: set[str] | None = None,
                       max_files: int = _INDEX_MAX_FILES) -> list[tuple[str, str]]:
    """Walk repo and return list of (relpath, abspath) for source files."""
    if exts is None:
        exts = _LANG_EXTS
    results: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext not in exts:
                continue
            abspath = os.path.join(dirpath, fname)
            relpath = os.path.relpath(abspath, root).replace("\\", "/")
            results.append((relpath, abspath))
            if len(results) >= max_files:
                return results
    return results


def _walk_py_files(root: str, max_files: int = _INDEX_MAX_FILES) -> list[tuple[str, str]]:
    """Walk repo and return list of (relpath, abspath) for .py files."""
    return _walk_source_files(root, exts={".py"}, max_files=max_files)


# ── Regex-based symbol extraction for non-Python languages ──

_JS_TS_FUNC_RE = re.compile(
    r'(?:export\s+)?(?:async\s+)?function\s+(\w+)|'
    r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?|'
    r'(?:export\s+)?class\s+(\w+)|'
    r'(\w+)\s*\([^)]*\)\s*\{',
    re.MULTILINE,
)

_GO_FUNC_RE = re.compile(
    r'func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(|'
    r'type\s+(\w+)\s+struct',
    re.MULTILINE,
)

_IS_TEST_FILE_MULTI = re.compile(
    r'(?:test[_.]|[_.]test\.|[_.]spec\.|__tests__|/tests?/|/testing/|_test\.go$|Test\.java$)',
    re.IGNORECASE,
)


def _extract_symbols_regex(source: str, ext: str) -> list[tuple[str, int]]:
    """Extract (symbol_name, line_number) from source using regex. For non-Python files."""
    symbols: list[tuple[str, int]] = []
    seen: set[str] = set()

    if ext in (".js", ".ts", ".jsx", ".tsx"):
        pattern = _JS_TS_FUNC_RE
    elif ext == ".go":
        pattern = _GO_FUNC_RE
    else:
        return symbols

    for m in pattern.finditer(source):
        name = next((g for g in m.groups() if g), None)
        if not name or name in seen or len(name) < 2:
            continue
        if name[0].islower() and name in ("if", "for", "while", "return", "switch", "case", "var", "let", "const"):
            continue
        seen.add(name)
        line = source[:m.start()].count("\n") + 1
        symbols.append((name, line))

    return symbols


def _build_regex_index(root: str, deadline: float) -> dict:
    """Build a basic index for non-Python repos using regex.

    Provides enough structure for ego-graph (symbol_defs, callers, file_symbols, test_files).
    """
    index: dict[str, Any] = {
        "meta": {"root": root, "build_timestamp": time.time(), "file_count": 0, "symbol_count": 0, "indexer": "regex"},
        "file_symbols": {},
        "file_classes": {},
        "fingerprints": {},
        "norms": {},
        "callers": {},
        "system": {},
        "test_files": {},
        "symbol_defs": {},
    }

    dom_ext = _detect_repo_language(root)
    exts = {dom_ext} | {".py"}  # always include .py if any exist
    files = _walk_source_files(root, exts=exts)
    index["meta"]["file_count"] = len(files)

    # Pass 1: extract symbols
    known_symbols: set[str] = set()
    parsed_sources: dict[str, str] = {}

    for relpath, abspath in files:
        if time.time() > deadline:
            break
        ext = os.path.splitext(relpath)[1]
        try:
            with open(abspath, "r", errors="replace") as f:
                source = f.read()
            if len(source) > 500_000:
                continue
        except OSError:
            continue

        parsed_sources[relpath] = source

        if ext == ".py":
            # Use AST for Python files
            tree = _parse_safe(source)
            if tree:
                file_syms: list[str] = []
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        known_symbols.add(node.name)
                        file_syms.append(node.name)
                        index["symbol_defs"].setdefault(node.name, []).append({"file": relpath, "line": node.lineno})
                        for item in node.body:
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                known_symbols.add(item.name)
                                file_syms.append(item.name)
                                index["symbol_defs"].setdefault(item.name, []).append({"file": relpath, "line": item.lineno})
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        known_symbols.add(node.name)
                        file_syms.append(node.name)
                        index["symbol_defs"].setdefault(node.name, []).append({"file": relpath, "line": node.lineno})
                if file_syms:
                    index["file_symbols"][relpath] = file_syms
        else:
            # Use regex for other languages
            syms = _extract_symbols_regex(source, ext)
            file_syms_list = []
            for name, line in syms:
                known_symbols.add(name)
                file_syms_list.append(name)
                index["symbol_defs"].setdefault(name, []).append({"file": relpath, "line": line})
            if file_syms_list:
                index["file_symbols"][relpath] = file_syms_list

    index["meta"]["symbol_count"] = len(known_symbols)

    # Pass 2: find callers via simple text search (regex grep)
    for relpath, source in parsed_sources.items():
        if time.time() > deadline:
            break
        is_test = bool(_IS_TEST_FILE_MULTI.search(relpath))

        for sym in known_symbols:
            if len(sym) < 3:
                continue
            # Quick check: is symbol mentioned in this file?
            if sym not in source:
                continue
            # Find lines that reference the symbol (not its definition)
            def_files = [d["file"] for d in index.get("symbol_defs", {}).get(sym, [])]
            if relpath in def_files:
                continue  # skip self-references

            if is_test:
                index["test_files"].setdefault(sym, [])
                if relpath not in index["test_files"][sym]:
                    index["test_files"][sym].append(relpath)
            else:
                callers_list = index["callers"].setdefault(sym, [])
                if len(callers_list) < 30:
                    # Find the line
                    for i, line_text in enumerate(source.split("\n"), 1):
                        if sym in line_text:
                            callers_list.append({"file": relpath, "func": "?", "usage": "reference", "line": i})
                            break

    # System context
    for sym_name in known_symbols:
        callers = index["callers"].get(sym_name, [])
        if callers:
            files_set = {c["file"] for c in callers}
            index["system"][sym_name] = {
                "caller_count": len(callers),
                "caller_files": len(files_set),
            }

    # Cache
    build_ms = int((time.time() - index["meta"]["build_timestamp"]) * 1000)
    index["meta"]["build_time_ms"] = build_ms
    try:
        with open(_INDEX_CACHE_PATH, "w") as f:
            json.dump(index, f, separators=(",", ":"))
    except OSError:
        pass

    return index


def _annotate_parents(tree: ast.Module) -> None:
    """Walk AST and annotate each Call node with its enclosing statement."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for stmt in ast.walk(node):
                if isinstance(stmt, (ast.Assign, ast.Return, ast.If, ast.For,
                                     ast.Assert, ast.Expr, ast.AugAssign)):
                    for child in ast.walk(stmt):
                        if isinstance(child, ast.Call):
                            child._gt_parent_stmt = stmt  # type: ignore[attr-defined]


def build_index(root: str) -> dict:
    """Build cross-file constraint map for the entire repo.

    For Python-dominant repos: full AST-based index with fingerprints and norms.
    For non-Python repos: regex-based index with symbol defs, callers, test files.

    Returns the index dict. Also caches to /tmp/gt_index.json.
    """
    t0 = time.time()
    deadline = t0 + _INDEX_BUILD_TIMEOUT

    # Detect dominant language — use regex indexer for non-Python repos
    dom_lang = _detect_repo_language(root)
    if dom_lang != ".py":
        return _build_regex_index(root, deadline)

    fingerprinter = BehavioralFingerprinter()
    rule_miner = RuleMiner()

    # Index structure
    index: dict[str, Any] = {
        "meta": {"root": root, "build_timestamp": t0, "file_count": 0, "symbol_count": 0},
        "file_symbols": {},    # relpath -> [symbol_names]
        "file_classes": {},    # relpath -> [{name, methods, bases}]
        "fingerprints": {},    # "relpath::func_name" -> {reads, writes, returns, ...}
        "norms": {},           # "ClassName" -> {dimension: {pattern, freq, confidence}}
        "callers": {},         # "symbol_name" -> [{file, func, usage, line}]
        "system": {},          # "symbol_name" -> {caller_count, caller_files, usage, critical}
        "test_files": {},      # "symbol_name" -> [test_file_paths]
        "symbol_defs": {},     # "symbol_name" -> [{file, line}]  (v10: for ego-graph code retrieval)
    }

    py_files = _walk_py_files(root)
    index["meta"]["file_count"] = len(py_files)

    # Parsed trees cache for pass 2
    parsed_trees: dict[str, ast.Module] = {}
    known_symbols: set[str] = set()
    symbol_to_files: dict[str, list[str]] = {}  # symbol_name -> [defining files]

    # ---- PASS 1: symbols, fingerprints, imports ----
    for relpath, abspath in py_files:
        if time.time() > deadline:
            break
        try:
            with open(abspath, "r", errors="replace") as f:
                source = f.read()
            if len(source) > 500_000:  # skip huge files
                continue
            tree = ast.parse(source)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue

        parsed_trees[relpath] = tree
        file_syms: list[str] = []

        # Extract classes
        classes_in_file: list[dict] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(item.name)
                        sym_key = f"{relpath}::{item.name}"
                        fp = fingerprinter.fingerprint_function(item)
                        if not fp.is_abstract:
                            index["fingerprints"][sym_key] = {
                                "reads": fp.reads_self[:6] + [f"param:{p}" for p in fp.reads_params[:3]],
                                "writes": fp.writes_self[:4],
                                "returns": fp.return_shape,
                                "raises": fp.raises[:3],
                                "calls": fp.calls[:6],
                                "guards": [g[:40] for g in fp.guard_conditions[:2]],
                            }
                        known_symbols.add(item.name)
                        file_syms.append(item.name)
                        symbol_to_files.setdefault(item.name, []).append(relpath)

                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases.append(base.attr)
                classes_in_file.append({
                    "name": node.name,
                    "methods": methods,
                    "bases": bases,
                    "line": node.lineno,
                })
                known_symbols.add(node.name)
                file_syms.append(node.name)
                symbol_to_files.setdefault(node.name, []).append(relpath)
                # v10: store definition location for ego-graph
                index["symbol_defs"].setdefault(node.name, []).append({"file": relpath, "line": node.lineno})
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index["symbol_defs"].setdefault(item.name, []).append({"file": relpath, "line": item.lineno})

                # Fingerprint class methods for norm mining
                fps = fingerprinter.fingerprint_class(node)
                if len(fps) >= 3:
                    rules = rule_miner.mine(fps)
                    if rules:
                        norm_dict: dict[str, Any] = {}
                        for rule in rules:
                            norm_dict[rule.dimension] = {
                                "pattern": rule.pattern,
                                "freq": rule.frequency,
                                "confidence": rule.confidence,
                            }
                        if norm_dict:
                            index["norms"][node.name] = norm_dict

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fp = fingerprinter.fingerprint_function(node)
                sym_key = f"{relpath}::{node.name}"
                if not fp.is_abstract:
                    index["fingerprints"][sym_key] = {
                        "reads": fp.reads_self[:6] + [f"param:{p}" for p in fp.reads_params[:3]],
                        "writes": fp.writes_self[:4],
                        "returns": fp.return_shape,
                        "raises": fp.raises[:3],
                        "calls": fp.calls[:6],
                        "guards": [g[:40] for g in fp.guard_conditions[:2]],
                    }
                known_symbols.add(node.name)
                file_syms.append(node.name)
                symbol_to_files.setdefault(node.name, []).append(relpath)
                index["symbol_defs"].setdefault(node.name, []).append({"file": relpath, "line": node.lineno})

        if file_syms:
            index["file_symbols"][relpath] = file_syms
        if classes_in_file:
            index["file_classes"][relpath] = classes_in_file

    index["meta"]["symbol_count"] = len(known_symbols)

    # ---- PASS 2: cross-file references with usage classification ----
    for relpath, tree in parsed_trees.items():
        if time.time() > deadline:
            break
        _annotate_parents(tree)
        is_test = _is_test_file(relpath)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Extract call target name
            call_name = None
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr

            if not call_name or call_name not in known_symbols:
                continue

            # Get defining files for this symbol
            def_files = symbol_to_files.get(call_name, [])
            if not def_files:
                continue

            # Skip self-references (calls within the same file to the same symbol)
            # But still record them for in-file caller tracking
            caller_func = _find_enclosing_func(tree, getattr(node, "lineno", 0))
            parent_stmt = getattr(node, "_gt_parent_stmt", None)
            usage = _classify_reference_usage(node, parent_stmt) if parent_stmt else "other"

            ref_entry = {
                "file": relpath,
                "func": caller_func,
                "usage": usage,
                "line": getattr(node, "lineno", 0),
            }

            # Record as test file reference or caller reference
            if is_test:
                index["test_files"].setdefault(call_name, [])
                if relpath not in index["test_files"][call_name]:
                    index["test_files"][call_name].append(relpath)
            else:
                callers_list = index["callers"].setdefault(call_name, [])
                # Cap callers per symbol to prevent bloat
                if len(callers_list) < 50:
                    callers_list.append(ref_entry)

    # ---- POST-WALK: compute system context per symbol ----
    critical_keywords = {"auth", "security", "session", "password", "token",
                         "permission", "payment", "crypto", "login", "credential"}

    for sym_name, callers in index["callers"].items():
        def_files = set(symbol_to_files.get(sym_name, []))
        # Cross-file callers only
        xfile_callers = [c for c in callers if c["file"] not in def_files]
        if not xfile_callers:
            continue

        caller_files = set(c["file"] for c in xfile_callers)
        usage_summary: Counter[str] = Counter()
        for c in xfile_callers:
            usage_summary[c["usage"]] += 1

        critical = any(
            any(kw in f.lower() for kw in critical_keywords)
            for f in caller_files
        )

        index["system"][sym_name] = {
            "caller_count": len(xfile_callers),
            "caller_files": len(caller_files),
            "usage": dict(usage_summary.most_common(5)),
            "critical": critical,
        }

    # ---- Finalize ----
    build_ms = int((time.time() - t0) * 1000)
    index["meta"]["build_time_ms"] = build_ms

    # Cache
    try:
        with open(_INDEX_CACHE_PATH, "w") as f:
            json.dump(index, f, separators=(",", ":"))
    except OSError:
        pass

    return index


def _load_or_build_index(root: str) -> tuple[dict, int, int]:
    """Load cached index or build fresh. Returns (index, load_ms, build_ms)."""
    t0 = time.time()

    # Try loading cache
    try:
        if os.path.exists(_INDEX_CACHE_PATH):
            with open(_INDEX_CACHE_PATH, "r") as f:
                index = json.load(f)
            cached_root = index.get("meta", {}).get("root", "")
            # Validate cache is for same root
            if os.path.normpath(cached_root) == os.path.normpath(root):
                load_ms = int((time.time() - t0) * 1000)
                return index, load_ms, 0
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    # Build fresh
    load_ms = int((time.time() - t0) * 1000)
    index = build_index(root)
    build_ms = index.get("meta", {}).get("build_time_ms", 0)
    return index, load_ms, build_ms


def _git_cochange(root: str, target_file: str, max_commits: int = 50) -> list[dict]:
    """Find files that frequently co-change with target_file in git history."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:%H", "-n", str(max_commits),
             "--", target_file],
            capture_output=True, text=True, cwd=root, timeout=8,
            env=_git_env(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        # Parse: commits are separated by blank lines, files follow each hash
        cochange: Counter[str] = Counter()
        total_target_commits = 0
        current_files: list[str] = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                # End of a commit block
                if current_files:
                    total_target_commits += 1
                    for f in current_files:
                        if f != target_file and f.endswith(".py") and "/tests/" not in f:
                            cochange[f] += 1
                current_files = []
            elif len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                current_files = []  # new commit hash
            else:
                current_files.append(line)
        # Last block
        if current_files:
            total_target_commits += 1
            for f in current_files:
                if f != target_file and f.endswith(".py") and "/tests/" not in f:
                    cochange[f] += 1

        if total_target_commits == 0:
            return []

        # Return top pairs with coupling strength
        pairs = []
        for f, count in cochange.most_common(5):
            if count >= 2:  # at least 2 co-changes
                pairs.append({
                    "file": f,
                    "commits_together": count,
                    "total_target_commits": total_target_commits,
                    "coupling": round(count / total_target_commits, 2),
                })
        return pairs
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _git_recent_changes(root: str, target_file: str) -> list[str]:
    """Get recent commit subjects for the target file."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-n", "3", "--", target_file],
            capture_output=True, text=True, cwd=root, timeout=5,
            env=_git_env(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return [l.strip() for l in result.stdout.strip().split("\n") if l.strip()][:3]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def _find_precedent(index: dict, rel_path: str, file_syms: list[str]) -> dict | None:
    """Find the most similar function in the same file/module as a template."""
    fps = index.get("fingerprints", {})
    target_fps = []
    for sym in file_syms:
        key = f"{rel_path}::{sym}"
        fp = fps.get(key)
        if fp:
            target_fps.append((sym, fp))

    if len(target_fps) < 2:
        return None

    # Find pairs with similar parameter count and return type
    best = None
    best_score = 0.0
    for i, (sym_a, fp_a) in enumerate(target_fps):
        for sym_b, fp_b in target_fps[i + 1:]:
            score = 0.0
            # Same return type
            if fp_a.get("returns") == fp_b.get("returns"):
                score += 0.3
            # Similar param count
            pa = len(fp_a.get("reads", []))
            pb = len(fp_b.get("reads", []))
            if pa > 0 and pb > 0 and abs(pa - pb) <= 1:
                score += 0.3
            # Similar calls
            ca = set(fp_a.get("calls", []))
            cb = set(fp_b.get("calls", []))
            if ca and cb and len(ca & cb) > 0:
                score += 0.4
            if score > best_score:
                best_score = score
                best = {"function": sym_b, "similar_to": sym_a, "score": score}

    return best if best and best_score >= 0.5 else None


# ── v10: Ego-Graph Functions ──────────────────────────────────────────────────


def _read_source_lines(root: str, rel_path: str, start_line: int, max_lines: int = 6) -> str:
    """Read actual source code from a file at a given line range."""
    abs_path = os.path.join(root, rel_path)
    try:
        with open(abs_path, "r", errors="replace") as f:
            lines = f.readlines()
        end = min(start_line + max_lines, len(lines) + 1)
        chunk = lines[max(0, start_line - 1):end - 1]
        # Dedent to minimum indent
        if chunk:
            min_indent = min((len(l) - len(l.lstrip()) for l in chunk if l.strip()), default=0)
            chunk = [l[min_indent:] if len(l) > min_indent else l for l in chunk]
        return "".join(chunk).rstrip()
    except (OSError, IndexError):
        return ""


def _get_ego_graph(
    index: dict, root: str, symbol_name: str, rel_path: str, max_nodes: int = 8
) -> list[tuple[str, str, str, str]]:
    """Build 1-hop ego-graph: (relation, symbol, file, source_code).

    Retrieves TARGET + callees + callers + key references with real source code.
    """
    nodes: list[tuple[str, str, str, str]] = []

    # 1. TARGET: the symbol itself
    defs = index.get("symbol_defs", {}).get(symbol_name, [])
    target_def = None
    for d in defs:
        if d["file"] == rel_path:
            target_def = d
            break
    if not target_def and defs:
        target_def = defs[0]

    if target_def:
        code = _read_source_lines(root, target_def["file"], target_def["line"], max_lines=6)
        if code:
            nodes.append(("TARGET", symbol_name, f"{target_def['file']}:{target_def['line']}", code))

    # 2. CALLS: what does the target call? (from fingerprint)
    fp_key = f"{rel_path}::{symbol_name}"
    fp = index.get("fingerprints", {}).get(fp_key, {})
    callees = fp.get("calls", [])
    for callee_name in callees[:4]:
        # Strip method calls: self.foo() → foo, obj.bar() → bar
        clean_name = callee_name.split(".")[-1].rstrip("()")
        callee_defs = index.get("symbol_defs", {}).get(clean_name, [])
        if not callee_defs:
            continue
        # Prefer same-file definition, then first definition
        cd = next((d for d in callee_defs if d["file"] == rel_path), callee_defs[0])
        code = _read_source_lines(root, cd["file"], cd["line"], max_lines=4)
        if code and len(nodes) < max_nodes:
            nodes.append(("CALLS", clean_name, f"{cd['file']}:{cd['line']}", code))

    # 3. CALLED BY: who calls the target? (cross-file callers only)
    callers = index.get("callers", {}).get(symbol_name, [])
    def_files = {d["file"] for d in defs} if defs else {rel_path}
    xfile_callers = [c for c in callers if c["file"] not in def_files]
    for caller in xfile_callers[:3]:
        caller_line = caller.get("line", 0)
        if caller_line > 0:
            # Show 3 lines around the call site
            code = _read_source_lines(root, caller["file"], max(1, caller_line - 1), max_lines=3)
            if code and len(nodes) < max_nodes:
                caller_label = caller.get("func", "?")
                nodes.append(("CALLED BY", caller_label, f"{caller['file']}:{caller_line}", code))

    return nodes


def _get_obligations(index: dict, symbol_name: str, rel_path: str) -> list[str]:
    """Extract behavioral obligations from the constraint map."""
    obligations: list[str] = []

    # 1. Caller usage contract
    sys_ctx = index.get("system", {}).get(symbol_name, {})
    if sys_ctx:
        count = sys_ctx.get("caller_count", 0)
        n_files = sys_ctx.get("caller_files", 0)
        usage = sys_ctx.get("usage", {})
        if count > 0:
            obligations.append(f"CALLERS: {count} callers in {n_files} files")
        if usage:
            dominant = max(usage.items(), key=lambda x: x[1])
            if dominant[1] > count * 0.6 and count >= 2:
                obligations.append(f"CONTRACT: {dominant[1]}/{count} callers {dominant[0]} — preserve this interface")
        if sys_ctx.get("critical"):
            obligations.append(f"CRITICAL: on security/auth critical path")

    # 2. Class norms
    classes = index.get("file_classes", {}).get(rel_path, [])
    for cls_info in classes:
        norms = index.get("norms", {}).get(cls_info["name"], {})
        for _dim, norm in norms.items():
            if norm.get("confidence", 0) >= 0.85:
                obligations.append(f"NORM: {norm['pattern']} ({norm.get('freq', '?')} methods)")
                break  # one norm is enough

    # 3. Test coverage
    test_files = index.get("test_files", {}).get(symbol_name, [])
    if test_files:
        obligations.append(f"TEST: covered by {', '.join(test_files[:2])}")

    return obligations[:5]


def _format_ego_output(
    nodes: list[tuple[str, str, str, str]],
    obligations: list[str],
) -> str:
    """Format ego-graph + obligations as readable output."""
    if len(nodes) < 2:
        return ""  # suppress if no connected nodes

    lines: list[str] = []
    lines.append("--- CONNECTED CODE ---")

    for relation, name, location, code in nodes:
        if relation == "TARGET":
            lines.append(f"TARGET: {name} ({location})")
        elif relation == "CALLS":
            lines.append(f"CALLS → {name} ({location})")
        elif relation == "CALLED BY":
            lines.append(f"CALLED BY → {name} ({location})")
        elif relation == "REFERENCES":
            lines.append(f"REFERENCES → {name} ({location})")

        # Indent code
        for cl in code.split("\n")[:6]:
            lines.append(f"  {cl}")

    if obligations:
        lines.append("")
        lines.append("--- OBLIGATIONS ---")
        lines.extend(obligations)

    return "\n".join(lines)


class UnderstandEndpoint:
    """v10: Ego-graph with real code + behavioral obligations."""

    def run(self, filepath: str, root: str, max_lines: int = 35) -> tuple[str, dict]:
        """v10: Return ego-graph with real code + behavioral obligations."""
        log_data: dict[str, Any] = {}

        # Normalize filepath
        rel_path = filepath.replace("\\", "/")
        if os.path.isabs(rel_path):
            rel_path = os.path.relpath(rel_path, root).replace("\\", "/")

        # Load or build index
        index, load_ms, build_ms = _load_or_build_index(root)
        log_data["index_load_ms"] = load_ms
        log_data["index_build_ms"] = build_ms

        # Find symbols in this file
        file_syms = index.get("file_symbols", {}).get(rel_path, [])
        if not file_syms:
            for key in index.get("file_symbols", {}):
                if key.endswith("/" + rel_path) or rel_path.endswith("/" + key):
                    file_syms = index["file_symbols"][key]
                    rel_path = key
                    break

        if not file_syms:
            log_data["error"] = f"no symbols found for {rel_path}"
            return "", log_data

        # Pick the primary symbol (most callers, or first class, or first function)
        primary_sym = file_syms[0]
        best_callers = 0
        for sym in file_syms:
            sys_ctx = index.get("system", {}).get(sym, {})
            cc = sys_ctx.get("caller_count", 0)
            if cc > best_callers:
                best_callers = cc
                primary_sym = sym

        # ── Build ego-graph ──
        nodes = _get_ego_graph(index, root, primary_sym, rel_path, max_nodes=8)
        obligations = _get_obligations(index, primary_sym, rel_path)

        # Format output
        output = _format_ego_output(nodes, obligations)

        if not output:
            log_data["suppressed"] = True
            log_data["suppressed_reason"] = f"ego-graph has <2 nodes for {primary_sym}"
            return "", log_data

        # ── Log ──
        log_data["primary_symbol"] = primary_sym
        log_data["ego_graph"] = {
            "total_nodes": len(nodes),
            "cross_file_nodes": sum(1 for r, _, loc, _ in nodes if rel_path not in loc),
            "relations": [r for r, _, _, _ in nodes],
        }
        log_data["obligations_count"] = len(obligations)
        log_data["output_lines"] = output.count("\n") + 1
        log_data["index_meta"] = index.get("meta", {})

        return output, log_data

    # v9: No fallback. If no cross-file data, stay silent.


# ---------------------------------------------------------------------------
# v10 ANALYZE: Combined 3-signal output (test assertions + ego-graph + sibling)
# ---------------------------------------------------------------------------


def _jaccard(a: list | set, b: list | set) -> float:
    """Jaccard similarity between two collections."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _find_best_sibling(
    index: dict, root: str, symbol_name: str, rel_path: str, max_lines: int = 10
) -> tuple[str, str, str, float] | None:
    """Find the most similar sibling in the same class.

    Returns (name, location, source_code, similarity_score) or None.
    """
    # Find which class contains symbol_name
    classes = index.get("file_classes", {}).get(rel_path, [])
    target_class = None
    for cls_info in classes:
        if symbol_name in cls_info.get("methods", []):
            target_class = cls_info
            break

    if not target_class:
        return None

    siblings = [m for m in target_class["methods"]
                if m != symbol_name and not m.startswith("__")]
    if not siblings:
        return None

    target_fp_key = f"{rel_path}::{symbol_name}"
    target_fp = index.get("fingerprints", {}).get(target_fp_key, {})
    if not target_fp:
        return None

    target_calls = target_fp.get("calls", [])
    target_reads = [r for r in target_fp.get("reads", []) if not r.startswith("param:")]
    target_params = [r.replace("param:", "") for r in target_fp.get("reads", []) if r.startswith("param:")]
    target_returns = target_fp.get("returns", "")

    best_name = ""
    best_score = 0.0

    for sib_name in siblings:
        sib_fp_key = f"{rel_path}::{sib_name}"
        sib_fp = index.get("fingerprints", {}).get(sib_fp_key, {})
        if not sib_fp:
            continue

        sib_calls = sib_fp.get("calls", [])
        sib_reads = [r for r in sib_fp.get("reads", []) if not r.startswith("param:")]
        sib_params = [r.replace("param:", "") for r in sib_fp.get("reads", []) if r.startswith("param:")]
        sib_returns = sib_fp.get("returns", "")

        # Score dimensions
        # Param count match
        pc_diff = abs(len(target_params) - len(sib_params))
        param_count_score = 1.0 if pc_diff == 0 else (0.5 if pc_diff == 1 else 0.0)

        # Param name overlap (Jaccard)
        param_name_score = _jaccard(target_params, sib_params)

        # Return shape match
        returns_score = 1.0 if target_returns == sib_returns else 0.0

        # Call overlap (Jaccard)
        call_score = _jaccard(target_calls, sib_calls)

        # Reads overlap (Jaccard)
        reads_score = _jaccard(target_reads, sib_reads)

        # Weighted sum
        score = (
            0.15 * param_count_score
            + 0.20 * param_name_score
            + 0.15 * returns_score
            + 0.30 * call_score
            + 0.20 * reads_score
        )

        if score > best_score:
            best_score = score
            best_name = sib_name

    if best_score < 0.3 or not best_name:
        return None

    # Find the sibling's line number from symbol_defs
    sib_defs = index.get("symbol_defs", {}).get(best_name, [])
    sib_line = 0
    for d in sib_defs:
        if d["file"] == rel_path:
            sib_line = d["line"]
            break
    if not sib_line and sib_defs:
        sib_line = sib_defs[0].get("line", 0)

    if not sib_line:
        return None

    source = _read_source_lines(root, rel_path, sib_line, max_lines=max_lines)
    if not source:
        return None

    location = f"{rel_path}:{sib_line}"
    return (best_name, location, source, best_score)


def _format_test_section(
    symbol_name: str, expectations: list, max_items: int = 5
) -> list[str]:
    """Format test assertions into output lines."""
    if not expectations:
        return []

    # Group by test file for the header
    test_files_seen: set[str] = set()
    for exp in expectations:
        test_files_seen.add(os.path.basename(getattr(exp, "test_file", "")))

    lines: list[str] = ["--- TESTS ---"]
    tf_display = ", ".join(sorted(test_files_seen)[:2])
    lines.append(f"TESTS FOR: {symbol_name} (from {tf_display})")

    for exp in expectations[:max_items]:
        atype = getattr(exp, "assertion_type", "")
        expected = getattr(exp, "expected", "")
        tfunc = getattr(exp, "test_func", "")
        # Clean up AST dump artifacts for readability
        expected_clean = expected.replace("Constant(value=", "").rstrip(")")
        if len(expected_clean) > 60:
            expected_clean = expected_clean[:57] + "..."
        lines.append(f"  {tfunc}: {atype} -> {expected_clean}")

    return lines


def _format_sibling_section(
    name: str, location: str, source: str, score: float
) -> list[str]:
    """Format sibling template into output lines."""
    lines: list[str] = ["--- SIMILAR ---"]
    pct = int(score * 100)
    lines.append(f"SIMILAR: {name} (same class, line {location.split(':')[-1]}, {pct}% similar)")
    for cl in source.split("\n")[:8]:
        lines.append(f"  {cl}")
    return lines


def _format_analyze_output(
    test_lines: list[str],
    ego_output: str,
    sibling_lines: list[str],
    obligations: list[str],
) -> str:
    """Combine all signal sections into a single output with line budget."""
    all_lines: list[str] = ["=== GT CODEBASE INTELLIGENCE ===", ""]

    # Tests section (up to 7 lines)
    if test_lines:
        all_lines.extend(test_lines[:7])
        all_lines.append("")

    # Connected code section (ego-graph, already formatted)
    if ego_output:
        all_lines.extend(ego_output.split("\n")[:20])
        all_lines.append("")

    # Similar section (up to 10 lines)
    if sibling_lines:
        all_lines.extend(sibling_lines[:10])
        all_lines.append("")

    # Obligations (up to 5 lines, only if not already in ego_output)
    if obligations and "--- OBLIGATIONS ---" not in ego_output:
        all_lines.append("--- OBLIGATIONS ---")
        all_lines.extend(obligations[:4])

    # Trim trailing blanks
    while all_lines and not all_lines[-1].strip():
        all_lines.pop()

    return "\n".join(all_lines)


def main_analyze(args: argparse.Namespace) -> None:
    """v10 analyze: combined test assertions + ego-graph + sibling template."""
    import sys as _sys
    start = time.time()

    root = _detect_workspace_root(args.root)
    filepath = args.filepath

    # Resolve filepath
    if os.path.isabs(filepath):
        rel_path = os.path.relpath(filepath, root).replace("\\", "/")
    else:
        rel_path = filepath.replace("\\", "/")

    log_data: dict[str, Any] = {"hook": "post_edit", "endpoint": "analyze", "root": root, "file": rel_path}

    # 1. Load/build index
    index, load_ms, build_ms = _load_or_build_index(root)
    log_data["index_load_ms"] = load_ms
    log_data["index_build_ms"] = build_ms

    # 2. Find primary symbol (same logic as UnderstandEndpoint)
    file_syms = index.get("file_symbols", {}).get(rel_path, [])
    if not file_syms:
        for key in index.get("file_symbols", {}):
            if key.endswith("/" + rel_path) or rel_path.endswith("/" + key):
                file_syms = index["file_symbols"][key]
                rel_path = key
                break

    if not file_syms:
        log_data["error"] = f"no symbols found for {rel_path}"
        log_data["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_data)
        if not args.quiet:
            print(f"GT: no symbols found for {rel_path}", file=_sys.stderr)
        return

    primary_sym = file_syms[0]
    best_callers = 0
    for sym in file_syms:
        sys_ctx = index.get("system", {}).get(sym, {})
        cc = sys_ctx.get("caller_count", 0)
        if cc > best_callers:
            best_callers = cc
            primary_sym = sym

    log_data["primary_symbol"] = primary_sym

    # 3. Signal A: Test assertions (PRIMARY)
    test_expectations: list = []
    test_signal: dict = {"test_files_found": 0, "assertions_extracted": 0}
    try:
        test_file_list = index.get("test_files", {}).get(primary_sym, [])
        test_signal["test_files_found"] = len(test_file_list)
        if test_file_list:
            ext = os.path.splitext(rel_path)[1]
            if ext == ".py":
                miner: TestAssertionMiner | RegexTestAssertionMiner = TestAssertionMiner(root)
            else:
                miner = RegexTestAssertionMiner(root)
            test_expectations = miner.mine(rel_path, test_file_list)
            test_signal["assertions_extracted"] = len(test_expectations)
    except Exception as e:
        test_signal["error"] = str(e)
    log_data["test_assertions"] = test_signal

    # 4. Signal B: Ego-graph with real code
    ego_signal: dict = {"total_nodes": 0, "cross_file_nodes": 0}
    nodes = _get_ego_graph(index, root, primary_sym, rel_path, max_nodes=8)
    obligations = _get_obligations(index, primary_sym, rel_path)
    ego_output = _format_ego_output(nodes, obligations)
    ego_signal["total_nodes"] = len(nodes)
    ego_signal["cross_file_nodes"] = sum(1 for r, _, loc, _ in nodes if rel_path not in loc)
    ego_signal["relations"] = [r for r, _, _, _ in nodes]
    log_data["ego_graph"] = ego_signal

    # 5. Signal C: Best sibling template
    sibling_signal: dict = {"found": False}
    sibling_result = _find_best_sibling(index, root, primary_sym, rel_path, max_lines=10)
    if sibling_result:
        sibling_signal = {"found": True, "name": sibling_result[0], "score": round(sibling_result[3], 2)}
    log_data["sibling"] = sibling_signal

    # 6. Suppression check: skip if no useful data
    has_tests = len(test_expectations) > 0
    has_ego = len(nodes) >= 2
    has_sibling = sibling_result is not None
    if not has_tests and not has_ego and not has_sibling:
        log_data["suppressed"] = True
        log_data["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_data)
        return

    # 7. Format combined output
    test_lines = _format_test_section(primary_sym, test_expectations)
    sibling_lines: list[str] = []
    if sibling_result:
        sib_name, sib_loc, sib_source, sib_score = sibling_result
        sibling_lines = _format_sibling_section(sib_name, sib_loc, sib_source, sib_score)
    # Strip obligations from ego_output since we include them separately
    ego_for_combine = ego_output
    obligations_standalone = obligations if "--- OBLIGATIONS ---" not in ego_output else []

    output = _format_analyze_output(test_lines, ego_for_combine, sibling_lines, obligations_standalone)

    log_data["output_lines"] = output.count("\n") + 1
    log_data["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_data)

    if output:
        print(output)


# ---------------------------------------------------------------------------
# SEMANTIC EVIDENCE (shared dataclass + shared helpers from call_site_voting)
# ---------------------------------------------------------------------------

@dataclass
class SemanticEvidence:
    """Evidence item emitted by a semantic signal."""
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "semantic"


def _extract_arg_name(node: ast.expr) -> str | None:
    """Extract a simple string name from an AST argument node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _parse_call_args_from_line(line_text: str, func_name: str) -> list[str | None] | None:
    """Parse argument names from a single source line containing a call to func_name."""
    stripped = line_text.strip()
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError:
        try:
            tree = ast.parse(f"_={stripped}", mode="eval")
        except SyntaxError:
            return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_id = None
        if isinstance(node.func, ast.Name):
            func_id = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_id = node.func.attr
        if func_id != func_name:
            continue
        return [_extract_arg_name(a) for a in node.args]
    return None


@dataclass
class _CallRecord:
    """One sampled call site."""
    file_path: str
    line_no: int
    args: list[str | None]


def _git_grep_call_sites(
    root: str,
    func_name: str,
    exclude_file: str,
    max_sites: int = 20,
    deadline: float = 0.0,
) -> list[_CallRecord]:
    """Find call sites of func_name via git grep."""
    records: list[_CallRecord] = []
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "--", f"{func_name}("],
            capture_output=True, text=True, cwd=root, timeout=8,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return records

    rel_exclude = os.path.relpath(exclude_file, root) if os.path.isabs(exclude_file) else exclude_file

    for raw_line in result.stdout.splitlines():
        if deadline and time.time() > deadline:
            break
        if len(records) >= max_sites:
            break

        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_path, lineno_str, content = parts[0], parts[1], parts[2]

        if rel_path == rel_exclude:
            continue
        if _is_test_file(rel_path):
            continue
        if not rel_path.endswith(".py"):
            continue

        try:
            line_no = int(lineno_str)
        except ValueError:
            continue

        parsed = _parse_call_args_from_line(content, func_name)
        if parsed is None or len(parsed) < 2:
            continue

        records.append(_CallRecord(file_path=rel_path, line_no=line_no, args=parsed))

    return records


def _extract_diff_calls(diff_text: str) -> list[tuple[str, int, str, list[str | None]]]:
    """Extract function calls from added lines of a diff.

    Returns list of (file_path, line_no, func_name, [arg_names]).
    """
    results: list[tuple[str, int, str, list[str | None]]] = []
    current_file = ""
    current_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            current_line = 0
        elif raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            content = raw[1:]
            if not current_file.endswith(".py"):
                continue
            try:
                tree = ast.parse(content.strip(), mode="eval")
            except SyntaxError:
                try:
                    tree = ast.parse(f"_={content.strip()}", mode="eval")
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func_id = None
                if isinstance(node.func, ast.Name):
                    func_id = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_id = node.func.attr
                if not func_id:
                    continue
                args = [_extract_arg_name(a) for a in node.args]
                if len(args) >= 2 and any(a is not None for a in args):
                    results.append((current_file, current_line, func_id, args))
        elif not raw.startswith("-"):
            current_line += 1

    return results


def _levenshtein_similarity(a: str, b: str) -> float:
    """Return similarity in [0, 1] based on Levenshtein distance."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    dist = prev[n]
    return 1.0 - dist / max(m, n)


# ---------------------------------------------------------------------------
# SEMANTIC: CALL SITE VOTER
# ---------------------------------------------------------------------------

class CallSiteVoter:
    """Compare argument patterns at each position against sampled call sites."""

    MIN_SITES = 3
    MAJORITY_THRESHOLD = 0.70
    CONFIDENCE_FLOOR = 0.65

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        for file_path, line_no, func_name, edit_args in diff_calls:
            if time.time() > deadline:
                break

            abs_file = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path
            sites = _git_grep_call_sites(
                root, func_name, abs_file,
                max_sites=20, deadline=deadline,
            )
            if len(sites) < self.MIN_SITES:
                continue

            total = len(sites)

            max_pos = max(len(s.args) for s in sites)
            for pos in range(min(len(edit_args), max_pos)):
                edit_arg = edit_args[pos]
                if edit_arg is None:
                    continue
                pos_counter: Counter[str] = Counter()
                for site in sites:
                    if pos < len(site.args) and site.args[pos] is not None:
                        pos_counter[site.args[pos]] += 1  # type: ignore[arg-type]

                if not pos_counter:
                    continue
                majority_arg, majority_count = pos_counter.most_common(1)[0]
                freq = majority_count / total
                if freq >= self.MAJORITY_THRESHOLD and majority_arg != edit_arg:
                    confidence = freq * (1.0 - _levenshtein_similarity(edit_arg, majority_arg))
                    if confidence >= self.CONFIDENCE_FLOOR:
                        findings.append(SemanticEvidence(
                            kind="call_site_voting",
                            file_path=file_path,
                            line=line_no,
                            message=(
                                f"{majority_count}/{total} call sites of {func_name}() "
                                f"pass {majority_arg} at pos {pos + 1} -- edit passes {edit_arg}"
                            ),
                            confidence=min(confidence, 0.95),
                        ))

            # Detect suspected argument swaps (only 2-arg calls for now)
            if len(edit_args) == 2:
                a0, a1 = edit_args[0], edit_args[1]
                if a0 is None or a1 is None:
                    continue
                swap_count = sum(
                    1 for s in sites
                    if len(s.args) == 2
                    and s.args[0] == a1
                    and s.args[1] == a0
                )
                match_count = sum(
                    1 for s in sites
                    if len(s.args) == 2
                    and s.args[0] == a0
                    and s.args[1] == a1
                )
                two_arg_total = swap_count + match_count
                if two_arg_total >= self.MIN_SITES and swap_count > match_count:
                    freq = swap_count / two_arg_total
                    if freq >= self.MAJORITY_THRESHOLD:
                        confidence = freq * 0.9
                        if confidence >= self.CONFIDENCE_FLOOR:
                            findings.append(SemanticEvidence(
                                kind="call_site_swap",
                                file_path=file_path,
                                line=line_no,
                                message=(
                                    f"suspected arg swap at {func_name}({a0}, {a1}) -- "
                                    f"majority passes ({a1}, {a0})"
                                ),
                                confidence=min(confidence, 0.92),
                            ))

        return findings


# ---------------------------------------------------------------------------
# SEMANTIC: ARGUMENT AFFINITY
# ---------------------------------------------------------------------------

def _edit_distance(a: str, b: str) -> int:
    """Standard Levenshtein distance."""
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def _greedy_optimal_assignment(args: list[str], params: list[str]) -> list[int]:
    """Greedy min-cost bipartite matching: returns param index for each arg position."""
    k = min(len(args), len(params))
    used_params: set[int] = set()
    assignment: list[int] = [-1] * k

    costs = [
        [_edit_distance(args[i], params[j]) for j in range(len(params))]
        for i in range(k)
    ]

    for _ in range(k):
        best_cost = 10 ** 9
        best_i = best_j = -1
        for i in range(k):
            if assignment[i] != -1:
                continue
            for j in range(len(params)):
                if j in used_params:
                    continue
                if costs[i][j] < best_cost:
                    best_cost = costs[i][j]
                    best_i, best_j = i, j
        if best_i == -1:
            break
        assignment[best_i] = best_j
        used_params.add(best_j)

    return assignment


def _identity_cost(args: list[str], params: list[str]) -> int:
    """Cost of using args in the same order as params (identity mapping)."""
    k = min(len(args), len(params))
    return sum(_edit_distance(args[i], params[i]) for i in range(k))


def _optimal_cost(args: list[str], params: list[str]) -> tuple[int, list[int]]:
    """Return (optimal_cost, optimal_assignment) via greedy matching."""
    assignment = _greedy_optimal_assignment(args, params)
    k = min(len(args), len(params))
    cost = sum(
        _edit_distance(args[i], params[assignment[i]])
        for i in range(k)
        if assignment[i] != -1
    )
    return cost, assignment


def _find_function_def(root: str, func_name: str, deadline: float) -> list[str] | None:
    """Return parameter names for func_name found anywhere in the repo."""
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "--", f"def {func_name}("],
            capture_output=True, text=True, cwd=root, timeout=5,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    for raw_line in result.stdout.splitlines():
        if time.time() > deadline:
            break
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_path, _, content = parts
        if not rel_path.endswith(".py"):
            continue

        stub = content.strip()
        if not stub.startswith("def "):
            continue
        try:
            tree = ast.parse(stub + "\n    pass", mode="exec")
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != func_name:
                continue
            params = [
                a.arg for a in node.args.args
                if a.arg not in ("self", "cls")
            ]
            if params:
                return params

    return None


class ArgumentAffinityChecker:
    """Detect mismatched argument-parameter ordering via edit distance."""

    MIN_IMPROVEMENT_FRACTION = 0.25
    CONFIDENCE_CAP = 0.90
    CONFIDENCE_FLOOR = 0.65

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        seen_funcs: dict[str, list[str] | None] = {}

        for file_path, line_no, func_name, raw_edit_args in diff_calls:
            if time.time() > deadline:
                break

            edit_args = [a for a in raw_edit_args if a is not None]
            if len(edit_args) < 2:
                continue

            if func_name not in seen_funcs:
                seen_funcs[func_name] = _find_function_def(root, func_name, deadline)
            params = seen_funcs[func_name]
            if not params or len(params) < 2:
                continue

            k = min(len(edit_args), len(params))
            if k < 2:
                continue

            args_k = edit_args[:k]
            params_k = params[:k]

            id_cost = _identity_cost(args_k, params_k)
            opt_cost, assignment = _optimal_cost(args_k, params_k)

            if id_cost == 0 or opt_cost >= id_cost:
                continue

            improvement = (id_cost - opt_cost) / id_cost
            if improvement < self.MIN_IMPROVEMENT_FRACTION:
                continue

            if all(assignment[i] == i for i in range(k)):
                continue

            suggested_order = [args_k[assignment.index(j)] if j in assignment else "?" for j in range(k)]

            confidence = min(improvement * 0.9, self.CONFIDENCE_CAP)
            if confidence < self.CONFIDENCE_FLOOR:
                continue

            findings.append(SemanticEvidence(
                kind="arg_affinity",
                file_path=file_path,
                line=line_no,
                message=(
                    f"arg order may be wrong in {func_name}({', '.join(args_k)}) -- "
                    f"parameter names suggest ({', '.join(suggested_order)})"
                ),
                confidence=confidence,
            ))

        return findings


# ---------------------------------------------------------------------------
# SEMANTIC: GUARD CONSISTENCY
# ---------------------------------------------------------------------------

_CONTEXT_LINES = 3


def _line_has_guard(line_text: str) -> bool:
    """Return True if the line or its assignment target is guarded."""
    guard_patterns = [
        r"\bif\s+not\s+\w+\b",
        r"\bif\s+\w+\s+is\s+None\b",
        r"\bif\s+\w+\s+is\s+not\s+None\b",
        r"\bif\s+\w+\s*==\s*None\b",
        r"\bif\s+\w+\s*!=\s*None\b",
        r"\bor\s+None\b",
        r"\bif\s+\w+\b",
    ]
    for pat in guard_patterns:
        if re.search(pat, line_text):
            return True
    return False


def _assignment_target(line_text: str, func_name: str) -> str | None:
    """Return the variable name that receives the result of func_name()."""
    m = re.match(r"^\s*(\w+)\s*=\s*.*\b" + re.escape(func_name) + r"\s*\(", line_text)
    if m:
        return m.group(1)
    return None


def _sample_call_sites(
    root: str,
    func_name: str,
    exclude_file: str,
    max_sites: int = 20,
    deadline: float = 0.0,
) -> list[dict]:
    """Return list of {file, line, guarded, assignment_target} dicts."""
    results: list[dict] = []
    try:
        proc = subprocess.run(
            ["git", "grep", "-n", "-A", str(_CONTEXT_LINES), "--", f"{func_name}("],
            capture_output=True, text=True, cwd=root, timeout=8,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return results

    rel_exclude = (
        os.path.relpath(exclude_file, root)
        if os.path.isabs(exclude_file)
        else exclude_file
    )

    current_hit: dict | None = None
    context_lines_buf: list[str] = []

    for raw in proc.stdout.splitlines():
        if deadline and time.time() > deadline:
            break
        if len(results) >= max_sites:
            break

        if raw == "--":
            if current_hit is not None:
                _finalize_hit(current_hit, context_lines_buf, results)
            current_hit = None
            context_lines_buf = []
            continue

        m = re.match(r"^([^:]+):(\d+):(.*)", raw)
        if m:
            rel_path, lineno_str, content = m.group(1), m.group(2), m.group(3)
            if rel_path == rel_exclude or _is_test_file(rel_path) or not rel_path.endswith(".py"):
                current_hit = None
                context_lines_buf = []
                continue

            if f"{func_name}(" in content:
                if current_hit is not None:
                    _finalize_hit(current_hit, context_lines_buf, results)
                current_hit = {
                    "file": rel_path,
                    "line": int(lineno_str),
                    "call_line": content,
                    "target": _assignment_target(content, func_name),
                }
                context_lines_buf = [content]
            elif current_hit is not None:
                context_lines_buf.append(content)
        else:
            m2 = re.match(r"^([^-]+)-(\d+)-(.*)", raw)
            if m2 and current_hit is not None:
                context_lines_buf.append(m2.group(3))

    if current_hit is not None:
        _finalize_hit(current_hit, context_lines_buf, results)

    return results


def _finalize_hit(hit: dict, context_lines: list[str], results: list[dict]) -> None:
    """Determine whether the call site is guarded and append to results."""
    target = hit.get("target")

    guarded = False
    if target:
        for ctx_line in context_lines[1:]:
            if re.search(r"\b" + re.escape(target) + r"\b", ctx_line):
                if _line_has_guard(ctx_line):
                    guarded = True
                    break
        if not guarded and _line_has_guard(hit["call_line"]):
            guarded = True
    else:
        all_text = "\n".join(context_lines)
        if _line_has_guard(all_text):
            guarded = True

    results.append({
        "file": hit["file"],
        "line": hit["line"],
        "guarded": guarded,
        "target": target,
    })


def _edit_has_guard(diff_text: str, func_name: str, call_file: str, call_line: int) -> bool:
    """Check whether the edit's call site has a guard in the diff context."""
    in_file = False
    current_line = 0
    call_line_content = ""
    post_lines: list[str] = []
    collecting_post = False

    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            in_file = raw[6:] == call_file
            current_line = 0
            collecting_post = False
            post_lines = []
        elif in_file and raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
            collecting_post = False
        elif in_file and raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            content = raw[1:]
            if current_line == call_line:
                call_line_content = content
                collecting_post = True
            elif collecting_post:
                post_lines.append(content)
                if len(post_lines) >= _CONTEXT_LINES:
                    break
        elif in_file and not raw.startswith("-"):
            current_line += 1
            if collecting_post:
                post_lines.append(raw)
                if len(post_lines) >= _CONTEXT_LINES:
                    break

    if not call_line_content:
        return False

    target = _assignment_target(call_line_content, func_name)
    if target:
        for line in post_lines:
            if re.search(r"\b" + re.escape(target) + r"\b", line):
                if _line_has_guard(line):
                    return True
    return _line_has_guard(call_line_content)


class GuardConsistencyChecker:
    """Flag call sites that don't guard return values when most callers do."""

    GUARD_RATE_THRESHOLD = 0.75
    CONFIDENCE_CAP = 0.85
    CONFIDENCE_FLOOR = 0.65
    MIN_SITES = 3

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        seen_funcs: set[str] = set()

        for file_path, line_no, func_name, _ in diff_calls:
            if time.time() > deadline:
                break
            if func_name in seen_funcs:
                continue
            seen_funcs.add(func_name)

            abs_file = (
                os.path.join(root, file_path)
                if not os.path.isabs(file_path)
                else file_path
            )

            sites = _sample_call_sites(
                root, func_name, abs_file,
                max_sites=20, deadline=deadline,
            )
            if len(sites) < self.MIN_SITES:
                continue

            guarded_count = sum(1 for s in sites if s["guarded"])
            total = len(sites)
            guard_rate = guarded_count / total

            if guard_rate < self.GUARD_RATE_THRESHOLD:
                continue

            if _edit_has_guard(diff_text, func_name, file_path, line_no):
                continue

            confidence = min(guard_rate * self.CONFIDENCE_CAP, self.CONFIDENCE_CAP)
            if confidence < self.CONFIDENCE_FLOOR:
                continue

            findings.append(SemanticEvidence(
                kind="guard_consistency",
                file_path=file_path,
                line=line_no,
                message=(
                    f"{guarded_count}/{total} call sites guard {func_name}() "
                    f"against None -- edit does not check return value"
                ),
                confidence=confidence,
            ))

        return findings


# ---------------------------------------------------------------------------
# MAIN HOOK
# ---------------------------------------------------------------------------

def _detect_workspace_root(provided_root: str) -> str:
    """Detect the actual workspace root dynamically.

    1. Try git rev-parse --show-toplevel from the provided root.
    2. If that fails, scan /workspace/*/ for a .git directory.
    3. Fall back to the provided root.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=provided_root, timeout=5,
            env=_git_env(),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return toplevel
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, NotADirectoryError):
        pass

    try:
        workspace_dirs = _glob.glob("/workspace/*/")
        for candidate in sorted(workspace_dirs):
            if os.path.isdir(os.path.join(candidate, ".git")):
                return candidate.rstrip("/")
    except OSError:
        pass

    return provided_root


def _is_view_operation() -> bool:
    """Return True if the current hook invocation is for a view-only operation."""
    for env_var in ("TOOL_INPUT", "OPENHANDS_TOOL_INPUT"):
        raw = os.environ.get(env_var, "")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("command") == "view":
                return True
        except (json.JSONDecodeError, ValueError):
            pass
    return False


def _get_modified_files(root: str) -> list[str]:
    """Get modified .py files from git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=root, timeout=10,
            env=_git_env(),
        )
        return [f.strip() for f in result.stdout.strip().split("\n")
                if f.strip().endswith(".py")]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, cwd=root, timeout=10,
            env=_git_env(),
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _find_funcs_at_lines(source: str, line_ranges: list[tuple[int, int]]) -> list[str]:
    """Find function/method names that overlap with given line ranges."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    func_names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_start = node.lineno
            func_end = getattr(node, "end_lineno", func_start + 50)
            for ls, le in line_ranges:
                if func_start <= le and ls <= func_end:
                    func_names.append(node.name)
                    break
    return func_names


def check_staleness(index_path: str, evidence_files: list[str]) -> int:
    """Return count of source files modified after the index was last built.

    Returns -1 if the index file doesn't exist.
    """
    try:
        index_mtime = os.path.getmtime(index_path)
    except OSError:
        return -1

    stale_count = 0
    for fpath in evidence_files:
        try:
            if os.path.getmtime(fpath) > index_mtime:
                stale_count += 1
        except OSError:
            continue
    return stale_count


def _apply_abstention(findings: list, min_confidence: float = 0.60) -> list:
    """Universal abstention across all evidence families."""
    passed = []
    for f in findings:
        conf = getattr(f, "confidence", 0)
        if conf < min_confidence:
            continue
        msg = getattr(f, "message", "")
        if msg.startswith("_") and not msg.startswith("__init__"):
            continue
        passed.append(f)
    return passed


def _confidence_tier(item: object) -> str:
    """Map evidence confidence to a human-readable tier tag."""
    conf = getattr(item, "confidence", 0.0)
    if conf >= 0.80:
        return "VERIFIED"
    if conf >= 0.60:
        return "WARNING"
    return "INFO"


def _to_imperative(item: object) -> str:
    """Transform evidence message to imperative voice where possible.

    Converts descriptive messages ("exception silently swallowed") to
    imperative commands ("DO NOT swallow this exception"). Falls back
    to the original message when no template matches.
    """
    kind = getattr(item, "kind", "")
    msg = getattr(item, "message", "")

    # CallerExpectation — already a factual statement, make imperative
    if hasattr(item, "usage_type"):
        detail = getattr(item, "detail", "")
        usage = getattr(item, "usage_type", "")
        if usage == "exception_guard":
            return f"PRESERVE exception behavior — {detail}"
        if usage in ("destructure_tuple", "destructure_list"):
            return f"RETURN same shape — {detail}"
        if usage == "attr_access":
            return f"PRESERVE attribute access — {detail}"
        if usage == "iterated":
            return f"RETURN iterable — {detail}"
        if usage == "boolean_check":
            return f"PRESERVE truthiness contract — {detail}"
        return f"PRESERVE caller contract — {detail}"

    # TestExpectation — make test reference imperative
    if hasattr(item, "assertion_type"):
        test_func = getattr(item, "test_func", "test")
        line = getattr(item, "line", "?")
        assertion = getattr(item, "assertion_type", "")
        expected = getattr(item, "expected", "")[:60]
        return f"MATCH test expectation — {test_func}:{line} {assertion} {expected}"

    # ChangeEvidence — imperative rewrites for each kind
    if kind == "exception_swallowed":
        return f"DO NOT swallow exception — {msg}"
    if kind == "guard_removed":
        return f"DO NOT remove safety check — {msg}"
    if kind == "exception_broadened":
        return f"DO NOT broaden exception handling — {msg}"
    if kind == "return_shape_changed":
        return f"PRESERVE return shape — {msg}"
    if kind == "validation_removed":
        return f"DO NOT remove validation — {msg}"

    # PatternEvidence — make sibling comparisons imperative
    if kind == "error_type_outlier":
        return f"RAISE the expected exception type — {msg}"
    if kind == "return_shape_outlier":
        return f"RETURN the expected shape — {msg}"
    if kind == "missing_guard":
        return f"ADD guard clause — {msg}"
    if kind == "missing_call":
        return f"ADD missing call — {msg}"
    if kind == "param_mismatch":
        return f"MATCH parameter access pattern — {msg}"

    # StructuralEvidence — obligation/contradiction/convention
    if kind == "obligation":
        return f"UPDATE required — {msg}"
    if kind == "contradiction":
        return f"RESOLVE contradiction — {msg}"
    if kind == "convention":
        return f"FOLLOW convention — {msg}"

    # Fallback: use message as-is (already informational, but at least tagged)
    if msg:
        if len(msg) > 140:
            msg = msg[:137] + "..."
        return msg
    return str(item)[:140]


def _format_evidence_item(item: object) -> str:
    """Format a single evidence item with tier and confidence."""
    tier = _confidence_tier(item)
    conf = getattr(item, "confidence", 0.0)
    imperative = _to_imperative(item)
    return f"[{tier}] {imperative} ({conf:.2f})"


def format_gt_evidence(
    evidence_items: list,
    suppressed_count: int = 0,
    stale_files: int = 0,
    error: str | None = None,
) -> str:
    """Single formatting function for ALL GT output paths.

    Returns the complete <gt-evidence> block as a string.
    Every delivery path (gt_hook.py, gt_v2_hooks.py, MCP server)
    must call this function and use its return value directly.
    """
    lines: list[str] = []

    # Error case — GT failed to run
    if error:
        lines.append(f"[SKIP] GT could not analyze: {error}")
        return "<gt-evidence>\n" + "\n".join(lines) + "\n</gt-evidence>"

    # Staleness warning
    if stale_files > 0:
        lines.append(f"[STALE] {stale_files} file(s) modified since last index — evidence may be outdated.")

    # Evidence items
    if evidence_items:
        for item in evidence_items:
            lines.append(_format_evidence_item(item))
    elif suppressed_count > 0:
        lines.append(f"[OK] No high-confidence findings. {suppressed_count} item(s) below threshold suppressed.")
    else:
        lines.append("[OK] No findings for this edit.")

    stale_attr = ' stale="true"' if stale_files > 0 else ""
    return f"<gt-evidence{stale_attr}>\n" + "\n".join(lines) + "\n</gt-evidence>"


# Keep old function as thin wrapper for any remaining callers
def _format_evidence(item: object) -> str:
    """Legacy format — delegates to new unified formatter."""
    return _format_evidence_item(item)


def main_verify(args: argparse.Namespace) -> None:
    """Post-edit verify pipeline (v4/v5 behavior)."""
    start = time.time()

    # Skip view operations immediately — no diff was produced
    if _is_view_operation():
        return

    # Detect the actual workspace root (handles /testbed vs /workspace/django/ etc.)
    root = _detect_workspace_root(args.root)

    log_entry: dict = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": root,
        "root_provided": args.root,
        "evidence": {},
    }

    modified_files = _get_modified_files(root)
    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        return

    log_entry["files_changed"] = modified_files
    diff_text = _get_diff_text(root)

    # Parse diff for changed line ranges per file
    diff_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file and current_file.endswith(".py"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                s = int(match.group(1))
                c = int(match.group(2)) if match.group(2) else 1
                diff_ranges.setdefault(current_file, []).append((s, s + c - 1))

    # Find changed function names per file
    changed_funcs: dict[str, list[str]] = {}
    for fpath, ranges in diff_ranges.items():
        source = _read_file(root, fpath)
        if source:
            changed_funcs[fpath] = _find_funcs_at_lines(source, ranges)

    all_findings: list = []

    # === EVIDENCE FAMILY 1: CHANGE (before/after AST diff) ===
    change_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        analyzer = ChangeAnalyzer()
        change_items = analyzer.analyze(root, diff_text)
        change_signal["ran"] = True
        change_signal["items_found"] = len(change_items)
        all_findings.extend(change_items)
    except Exception as e:
        import traceback
        change_signal["error"] = str(e)
        change_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["change"] = change_signal

    # === EVIDENCE FAMILY 2: CONTRACT (caller usage + test assertions) ===
    contract_signal: dict = {"ran": False, "callers_analyzed": 0, "tests_analyzed": 0, "items_found": 0, "after_abstention": 0}
    try:
        caller_miner = CallerUsageMiner(root)
        test_miner = TestAssertionMiner(root)

        caller_files: list[str] = []
        test_files: list[str] = []
        try:
            from groundtruth.index.store import SymbolStore  # type: ignore[import]
            store = SymbolStore(args.db)
            store.initialize()
            for fpath in modified_files:
                result = store.get_importers_of_file(fpath)
                importers = getattr(result, "value", []) or []
                if importers:
                    for imp in importers:
                        if "test" in imp.lower():
                            test_files.append(imp)
                        else:
                            caller_files.append(imp)
        except Exception:
            pass

        contract_signal["callers_analyzed"] = len(caller_files)
        contract_signal["tests_analyzed"] = len(test_files)

        for fpath, funcs in changed_funcs.items():
            for func_name in funcs:
                caller_items = caller_miner.mine(func_name, caller_files)
                all_findings.extend(caller_items)

        for fpath in modified_files:
            test_items = test_miner.mine(fpath, test_files)
            all_findings.extend(test_items)

        contract_signal["ran"] = True
        contract_signal["items_found"] = sum(1 for f in all_findings if getattr(f, "family", "") == "contract")
    except Exception as e:
        import traceback
        contract_signal["error"] = str(e)
        contract_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["contract"] = contract_signal

    # === EVIDENCE FAMILY 3: PATTERN (sibling analysis) ===
    pattern_signal: dict = {"ran": False, "siblings_found": 0, "items_found": 0, "after_abstention": 0}
    try:
        sibling_analyzer = SiblingAnalyzer()

        for fpath, funcs in changed_funcs.items():
            source = _read_file(root, fpath)
            if not source:
                continue
            for func_name in funcs:
                pattern_items = sibling_analyzer.analyze(source, func_name, file_path=fpath)
                all_findings.extend(pattern_items)

        pattern_signal["ran"] = True
        pattern_signal["items_found"] = sum(1 for f in all_findings if getattr(f, "family", "") == "pattern")
    except Exception as e:
        pattern_signal["error"] = str(e)
    log_entry["evidence"]["pattern"] = pattern_signal

    # === EVIDENCE FAMILY 4: STRUCTURAL (obligations + contradictions + conventions) ===
    structural_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        store_obj = None
        graph_obj = None
        try:
            from groundtruth.index.store import SymbolStore  # type: ignore[import]
            from groundtruth.index.graph import ImportGraph  # type: ignore[import]
            store_obj = SymbolStore(args.db)
            store_obj.initialize()
            graph_obj = ImportGraph(store_obj)
        except Exception:
            pass

        struct_items: list = []
        if store_obj and graph_obj and diff_text:
            struct_items.extend(run_obligations(store_obj, graph_obj, diff_text))
        if store_obj:
            struct_items.extend(run_contradictions(store_obj, root, modified_files))
        struct_items.extend(run_conventions(root, modified_files))

        structural_signal["ran"] = True
        structural_signal["items_found"] = len(struct_items)
        all_findings.extend(struct_items)
    except Exception as e:
        structural_signal["error"] = str(e)
    log_entry["evidence"]["structural"] = structural_signal

    # === EVIDENCE FAMILY 5: SEMANTIC (call-site voting + arg affinity + guard consistency) ===
    semantic_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        voter = CallSiteVoter()
        affinity = ArgumentAffinityChecker()
        guard = GuardConsistencyChecker()

        semantic_items: list = []
        remaining_time = max(2.0, 8.0 - (time.time() - start))

        if diff_text:
            semantic_items.extend(voter.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(affinity.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(guard.analyze(root, diff_text, time_budget=remaining_time / 3))

        semantic_signal["ran"] = True
        semantic_signal["items_found"] = len(semantic_items)
        all_findings.extend(semantic_items)
    except Exception as e:
        semantic_signal["error"] = str(e)
    log_entry["evidence"]["semantic"] = semantic_signal

    # === ABSTENTION ===
    passed = _apply_abstention(all_findings)

    for family_name in ("change", "contract", "pattern", "structural", "semantic"):
        count = sum(1 for f in passed if getattr(f, "family", "") == family_name)
        log_entry["evidence"].get(family_name, {})["after_abstention"] = count

    log_entry["abstention_summary"] = {
        "total_raw": len(all_findings),
        "total_emitted": len(passed),
        "total_suppressed": len(all_findings) - len(passed),
    }

    # === STALENESS CHECK ===
    stale_count = 0
    if args.db and os.path.exists(args.db):
        stale_count = check_staleness(args.db, [
            os.path.join(root, f) for f in modified_files
        ])

    # === FORMAT OUTPUT ===
    suppressed_count = len(all_findings) - len(passed)

    if passed:
        passed.sort(key=lambda f: -getattr(f, "confidence", 0))
        display_items = passed[:args.max_items]
    else:
        display_items = []

    output = format_gt_evidence(
        evidence_items=display_items,
        suppressed_count=suppressed_count,
        stale_files=max(0, stale_count),
    )
    log_entry["output"] = output
    log_entry["output_lines"] = len(display_items)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_entry["stale_files"] = stale_count
    log_hook(log_entry)

    # Always print — format_gt_evidence never returns empty
    print(output)


def main_understand(args: argparse.Namespace) -> None:
    """Pre-edit intelligence pipeline (v6)."""
    import sys as _sys
    start = time.time()

    root = _detect_workspace_root(args.root)
    filepath = args.filepath

    # Resolve filepath: if absolute, make relative to root for logging
    if os.path.isabs(filepath):
        rel_path = os.path.relpath(filepath, root)
    else:
        rel_path = filepath

    endpoint = UnderstandEndpoint()
    output, log_data = endpoint.run(rel_path, root, max_lines=args.max_lines)

    log_entry: dict[str, Any] = {
        "hook": "pre_edit",
        "endpoint": "understand",
        "root": root,
        "file": rel_path,
        **log_data,
        "output": output,
        "output_lines": len(output.strip().splitlines()) if output else 0,
        "wall_time_ms": int((time.time() - start) * 1000),
    }
    log_hook(log_entry)

    if output:
        print(output)
    elif not args.quiet:
        print(f"GT: no behavioral context available for {rel_path}", file=_sys.stderr)


def main() -> None:
    """Route to understand, analyze, or verify subcommand."""
    import sys as _sys

    # Detect subcommand: first positional arg that isn't a flag
    command = "verify"  # default for backward compat
    if len(_sys.argv) > 1 and _sys.argv[1] in ("understand", "verify", "analyze"):
        command = _sys.argv[1]
        _sys.argv = [_sys.argv[0]] + _sys.argv[2:]

    if command == "understand":
        parser = argparse.ArgumentParser(description="GT understand — pre-edit behavioral intelligence")
        parser.add_argument("filepath", help="File to analyze (relative to --root)")
        parser.add_argument("--root", default="/testbed")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--max-lines", type=int, default=10)
        args = parser.parse_args()
        main_understand(args)
    elif command == "analyze":
        parser = argparse.ArgumentParser(description="GT analyze — v10 combined signals (tests + ego-graph + sibling)")
        parser.add_argument("filepath", help="File to analyze (relative to --root)")
        parser.add_argument("--root", default="/testbed")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--max-lines", type=int, default=35)
        args = parser.parse_args()
        main_analyze(args)
    else:
        parser = argparse.ArgumentParser(description="GT post-edit verify hook v4")
        parser.add_argument("--root", default="/testbed")
        parser.add_argument("--db", default="/tmp/gt_index.db")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--max-items", type=int, default=3)
        args = parser.parse_args()
        main_verify(args)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
