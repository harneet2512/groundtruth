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
    if any(p in fp for p in ["/tests/", "/test/", "/testing/"]):
        return True
    basename = os.path.basename(fp)
    return basename.startswith("test_") or basename.endswith("_test.py")


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


def _apply_abstention(findings: list, min_confidence: float = 0.65) -> list:
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


def _format_evidence(item: object) -> str:
    """Format a single evidence item as a compact one-liner."""
    family = getattr(item, "family", "?")

    # CallerExpectation
    if hasattr(item, "usage_type"):
        detail = getattr(item, "detail", "")
        return f"GT: {detail} [{family}]"

    # TestExpectation
    if hasattr(item, "assertion_type"):
        test_func = getattr(item, "test_func", "test")
        line = getattr(item, "line", "?")
        assertion = getattr(item, "assertion_type", "")
        expected = getattr(item, "expected", "")[:60]
        return f"GT: {test_func}:{line} {assertion} {expected} [{family}]"

    # PatternEvidence, ChangeEvidence, StructuralEvidence, SemanticEvidence: have "message"
    msg = getattr(item, "message", str(item))
    if len(msg) > 140:
        msg = msg[:137] + "..."
    return f"GT: {msg} [{family}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook v4")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-items", type=int, default=3)
    args = parser.parse_args()

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

    # === FORMAT OUTPUT ===
    output_lines = []
    if passed:
        passed.sort(key=lambda f: -getattr(f, "confidence", 0))
        for item in passed[:args.max_items]:
            output_lines.append(_format_evidence(item))

    output = "\n".join(output_lines)
    log_entry["output"] = output
    log_entry["output_lines"] = len(output_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)

    if output:
        print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
