"""Change evidence -- before/after diff on changed functions.

Detects: removed guards, broadened exceptions, swallowed exceptions,
return shape changes, removed validation.

v16: Language-agnostic. Uses graph.db properties for CURRENT version,
regex extraction for BEFORE version (from git show). Falls back to
Python AST for .py files when graph.db is unavailable.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from groundtruth.index.graph_store import GraphStore


@dataclass
class ChangeEvidence:
    """A detected change in function behavior."""
    kind: str  # guard_removed | exception_broadened | exception_swallowed | return_shape_changed | validation_removed
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "change"


def _git_env() -> dict:
    """Git environment that handles safe.directory in containers."""
    import copy
    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


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


# ── Language-agnostic regex extractors (for BEFORE version) ──────────────

# Patterns to find function boundaries across languages
_FUNC_START_PATTERNS = {
    "python": r"^(\s*)def\s+{name}\s*\(",
    "go": r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?{name}\s*\(",
    "javascript": r"(?:function\s+{name}|(?:const|let|var)\s+{name}\s*=\s*(?:async\s+)?(?:function|\())",
    "typescript": r"(?:function\s+{name}|(?:const|let|var)\s+{name}\s*=\s*(?:async\s+)?(?:function|\())",
    "java": r"(?:public|private|protected|static|\s)+\w+\s+{name}\s*\(",
    "kotlin": r"(?:fun|suspend\s+fun)\s+{name}\s*\(",
    "rust": r"(?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[<(]",
    "csharp": r"(?:public|private|protected|static|\s)+\w+\s+{name}\s*\(",
    "php": r"(?:public|private|protected|static|\s)*function\s+{name}\s*\(",
    "swift": r"func\s+{name}\s*\(",
    "ruby": r"def\s+{name}\b",
    "scala": r"def\s+{name}\s*[(\[]",
}

# Patterns indicating end of function body (approximate)
_BLOCK_END_INDICATORS = {
    "python": r"^(\S)",  # unindented line ends a Python function
    "ruby": r"^\s*end\b",
}


def _find_function_in_source(source: str, func_name: str, language: str = "") -> tuple[int, int, str]:
    """Find function boundaries in source text. Returns (start_line, end_line, func_body).

    Uses language-specific patterns for function start, then heuristics for end.
    """
    lines = source.splitlines()
    pattern_template = _FUNC_START_PATTERNS.get(language, r"(?:def|func|function|fn|fun)\s+{name}\s*[\(<]")
    pattern = pattern_template.format(name=re.escape(func_name))

    start_line = -1
    indent = 0
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            start_line = i
            # Measure indentation for block-end detection
            indent = len(line) - len(line.lstrip())
            break

    if start_line < 0:
        return (-1, -1, "")

    # Find end of function: use brace counting for C-like, indent for Python/Ruby
    if language in ("python",):
        # Indentation-based: function ends when we see a line at same or lower indent
        end_line = start_line
        for i in range(start_line + 1, min(start_line + 200, len(lines))):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                end_line = i
                continue
            line_indent = len(lines[i]) - len(lines[i].lstrip())
            if line_indent <= indent and stripped:
                break
            end_line = i
    elif language in ("ruby",):
        # end-keyword based
        end_line = start_line
        depth = 1
        for i in range(start_line + 1, min(start_line + 200, len(lines))):
            stripped = lines[i].strip()
            if re.match(r"(def|class|module|do|if|unless|while|until|for|case|begin)\b", stripped):
                depth += 1
            if stripped == "end":
                depth -= 1
                if depth <= 0:
                    end_line = i
                    break
            end_line = i
    else:
        # Brace-based (C, Go, Java, JS, TS, Rust, C#, PHP, Swift, Kotlin, Scala)
        end_line = start_line
        depth = 0
        found_open = False
        for i in range(start_line, min(start_line + 200, len(lines))):
            for ch in lines[i]:
                if ch == '{':
                    depth += 1
                    found_open = True
                elif ch == '}':
                    depth -= 1
                    if found_open and depth <= 0:
                        end_line = i
                        break
            if found_open and depth <= 0:
                break
            end_line = i

    body = "\n".join(lines[start_line:end_line + 1])
    return (start_line + 1, end_line + 1, body)  # 1-based


def _regex_extract_guards(func_body: str) -> list[tuple[str, str]]:
    """Extract guard clauses from function body text (language-agnostic).

    A guard clause is an if-statement near the top of a function whose body
    contains raise/throw/return/panic. The raise/throw may be on the same line
    as the if or on subsequent indented lines.
    """
    guards = []
    lines = func_body.splitlines()
    i = 1  # skip function signature line
    while i < min(len(lines), 15):
        stripped = lines[i].strip()
        i += 1
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        if not re.match(r"if\b", stripped):
            continue

        # Collect the if-block text (this line + next ~4 indented lines)
        block_text = stripped
        for j in range(i, min(i + 5, len(lines))):
            next_line = lines[j].strip()
            if next_line and not re.match(r"(if|elif|else|for|while|def|func|function)\b", next_line):
                block_text += " " + next_line
            else:
                break

        # Check if the block contains raise/throw/return/panic
        is_guard = False
        guard_type = ""
        for kw, gtype in [("raise ", "raise"), ("throw ", "raise"), ("panic(", "panic"),
                           ("return ", "return"), ("return;", "return"),
                           ("abort(", "panic"), ("Err(", "return")]:
            if kw in block_text:
                is_guard = True
                guard_type = gtype
                break

        if is_guard:
            # Extract condition from the if line
            cond = stripped
            for delim in ("{", ":", ")"):
                idx = cond.find(delim, 2)
                if idx > 0:
                    cond = cond[3:idx].strip()  # skip "if "
                    break
            else:
                cond = cond[3:].strip()
            guards.append((guard_type, cond[:80]))
    return guards


def _regex_extract_exceptions(func_body: str) -> list[str]:
    """Extract exception/error types from function body text (language-agnostic)."""
    exceptions = []
    for m in re.finditer(r'raise\s+(\w+)', func_body):
        exceptions.append(m.group(1))
    for m in re.finditer(r'throw\s+(?:new\s+)?(\w+)', func_body):
        exceptions.append(m.group(1))
    for m in re.finditer(r'panic\(', func_body):
        exceptions.append("panic")
    # Go error returns: return fmt.Errorf / errors.New
    for m in re.finditer(r'return\s+(?:fmt\.Errorf|errors\.New)', func_body):
        exceptions.append("error")
    return exceptions


def _regex_extract_catch_handlers(func_body: str) -> list[str]:
    """Extract caught exception types from function body text (language-agnostic)."""
    handlers = []
    # Python: except ValueError, except (A, B)
    for m in re.finditer(r'except\s+(?:\(([^)]+)\)|(\w+))', func_body):
        if m.group(1):
            for exc in m.group(1).split(","):
                handlers.append(exc.strip())
        elif m.group(2):
            handlers.append(m.group(2))
    # Bare except
    if re.search(r'except\s*:', func_body):
        handlers.append("bare_except")
    # Java/C#/Kotlin: catch (ExceptionType e)
    for m in re.finditer(r'catch\s*\(\s*(\w+)', func_body):
        handlers.append(m.group(1))
    # Go: if err != nil pattern (not really catch, but error handling)
    # Rust: .unwrap(), .expect() — not catch handlers
    return handlers


def _regex_detect_swallowed(func_body: str) -> bool:
    """Detect if any catch/except handler swallows the exception."""
    # Python: except ...: pass / except ...: return None
    if re.search(r'except[^:]*:\s*\n\s+pass\b', func_body):
        return True
    if re.search(r'except[^:]*:\s*\n\s+return\s+None\b', func_body):
        return True
    # Java/C#: catch (...) { } — empty catch block
    if re.search(r'catch\s*\([^)]*\)\s*\{\s*\}', func_body):
        return True
    return False


def _regex_classify_return_shape(func_body: str) -> str:
    """Classify return shape from function body text (language-agnostic)."""
    shapes: list[str] = []
    for m in re.finditer(r'return\s+(.+?)(?:;|\s*$)', func_body, re.MULTILINE):
        val = m.group(1).strip()
        if not val or val in ("None", "nil", "null", "undefined"):
            shapes.append("none")
        elif val.startswith("(") and "," in val:
            shapes.append("tuple")
        elif val.startswith("[") or val.startswith("{"):
            shapes.append("collection")
        elif "," in val and not val.startswith('"') and not val.startswith("'"):
            shapes.append("tuple")  # Go multi-return: return a, b
        else:
            shapes.append("value")
    if not shapes:
        return "none"
    return Counter(shapes).most_common(1)[0][0]


# ── Python AST helpers (fallback for .py files) ─────────────────────────

def _parse_safe(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _find_function_ast(tree: ast.Module, func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node
    return None


def _get_guard_clauses_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str]]:
    guards = []
    for stmt in func.body[:5]:
        if isinstance(stmt, ast.If):
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


def _get_except_handlers_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
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


def _is_swallowed_ast(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return True
    if len(handler.body) == 1:
        stmt = handler.body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return True
    return False


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
        return "None"
    return Counter(shapes).most_common(1)[0][0]


def _get_raise_types_ast(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    types = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
                types.add(node.exc.func.id)
            elif isinstance(node.exc, ast.Name):
                types.add(node.exc.id)
    return types


# ── Diff parser (language-agnostic) ──────────────────────────────────────

def _parse_diff_changed_funcs(diff_text: str) -> list[tuple[str, None, int, int]]:
    """Parse diff to find (file_path, None, start_line, end_line) of changes."""
    results: list[tuple[str, None, int, int]] = []
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                results.append((current_file, None, start, start + count - 1))
    return results


# ── Main analyzer ────────────────────────────────────────────────────────

class ChangeAnalyzer:
    """Analyze before/after diff for changed functions.

    Language-agnostic: uses graph.db properties for current version,
    regex for before version. Falls back to Python AST for .py files.
    """

    def __init__(self, store: GraphStore | None = None):
        self.store = store

    def analyze(self, root: str, diff_text: str) -> list[ChangeEvidence]:
        findings: list[ChangeEvidence] = []
        if not diff_text:
            return findings

        changes = _parse_diff_changed_funcs(diff_text)
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

            # Detect language from extension
            ext = os.path.splitext(fpath)[1].lower()
            language = _ext_to_language(ext)

            # Find changed functions and analyze them
            if self.store:
                # graph.db path (language-agnostic): use node positions to find changed functions
                file_funcs = self.store.get_functions_in_file(fpath)
                changed_func_ids = []
                for func in file_funcs:
                    fs, fe = func["start_line"], func["end_line"]
                    for ls, le in line_ranges:
                        if fs <= le and ls <= fe:
                            changed_func_ids.append(func)
                            break

                for func_info in changed_func_ids:
                    func_name = func_info["name"]
                    node_id = func_info["id"]
                    func_line = func_info["start_line"]

                    # CURRENT: properties from graph.db
                    curr_guards = self.store.get_properties(node_id, kind="guard_clause")
                    curr_exceptions = self.store.get_properties(node_id, kind="exception_type")
                    curr_shapes = self.store.get_properties(node_id, kind="return_shape")

                    # BEFORE: regex extraction from git show
                    _, _, before_body = _find_function_in_source(original_source, func_name, language)
                    if not before_body:
                        continue

                    orig_guards = _regex_extract_guards(before_body)
                    orig_exceptions = _regex_extract_exceptions(before_body)
                    orig_shape = _regex_classify_return_shape(before_body)

                    # Compare: guard removal
                    if len(orig_guards) > len(curr_guards):
                        findings.append(ChangeEvidence(
                            kind="guard_removed", file_path=fpath, line=func_line,
                            message=f"safety check removed -- original had {len(orig_guards)} guard(s), edit has {len(curr_guards)}",
                            confidence=0.8,
                        ))

                    # Compare: validation removed (exception types removed)
                    orig_exc_set = set(orig_exceptions)
                    curr_exc_set = {p["value"] for p in curr_exceptions}
                    removed = orig_exc_set - curr_exc_set
                    if removed:
                        findings.append(ChangeEvidence(
                            kind="validation_removed", file_path=fpath, line=func_line,
                            message=f"validation removed -- original raised {', '.join(sorted(removed))}",
                            confidence=0.7,
                        ))

                    # Compare: exception broadened (catch handlers)
                    # Need current function body for catch handler regex
                    _, _, curr_body = _find_function_in_source(current_source, func_name, language)
                    orig_handlers = _regex_extract_catch_handlers(before_body)
                    curr_handlers = _regex_extract_catch_handlers(curr_body) if curr_body else []
                    broad_types = {"Exception", "BaseException", "bare_except", "Throwable", "object"}
                    for handler in curr_handlers:
                        if handler in broad_types and handler not in orig_handlers:
                            findings.append(ChangeEvidence(
                                kind="exception_broadened", file_path=fpath, line=func_line,
                                message=f"exception catch broadened to {handler} -- original caught: {', '.join(orig_handlers) or 'nothing'}",
                                confidence=0.85,
                            ))
                            break

                    # Compare: exception swallowed
                    if curr_body and _regex_detect_swallowed(curr_body):
                        if not _regex_detect_swallowed(before_body):
                            findings.append(ChangeEvidence(
                                kind="exception_swallowed", file_path=fpath, line=func_line,
                                message="exception silently swallowed (empty catch/pass)",
                                confidence=0.85,
                            ))

                    # Compare: return shape changed (normalize vocabulary)
                    curr_shape_values = [p["value"] for p in curr_shapes]
                    curr_shape = _normalize_shape(curr_shape_values[0]) if curr_shape_values else "none"
                    orig_shape_norm = _normalize_shape(orig_shape)
                    if orig_shape_norm != curr_shape and orig_shape_norm != "none":
                        findings.append(ChangeEvidence(
                            kind="return_shape_changed", file_path=fpath, line=func_line,
                            message=f"return shape changed from {orig_shape_norm} to {curr_shape}",
                            confidence=0.75,
                        ))

                if changed_func_ids:
                    continue  # graph.db path handled this file

            # Fallback: Python AST (for .py files or when graph.db is unavailable)
            if language == "python":
                ast_findings = self._analyze_python_ast(
                    fpath, original_source, current_source, line_ranges
                )
                findings.extend(ast_findings)
            else:
                # Regex-only fallback for non-Python when no graph.db
                regex_findings = self._analyze_regex(
                    fpath, original_source, current_source, line_ranges, language
                )
                findings.extend(regex_findings)

        return findings

    def _analyze_python_ast(self, fpath: str, original_source: str,
                            current_source: str,
                            line_ranges: list[tuple[int, int]]) -> list[ChangeEvidence]:
        """Python AST-based analysis (original behavior, preserved as fallback)."""
        findings: list[ChangeEvidence] = []
        orig_tree = _parse_safe(original_source)
        curr_tree = _parse_safe(current_source)
        if not orig_tree or not curr_tree:
            return findings

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
            orig_func = _find_function_ast(orig_tree, func_name)
            curr_func = _find_function_ast(curr_tree, func_name)
            if not orig_func or not curr_func:
                continue

            orig_guards = _get_guard_clauses_ast(orig_func)
            curr_guards = _get_guard_clauses_ast(curr_func)
            if len(orig_guards) > len(curr_guards):
                findings.append(ChangeEvidence(
                    kind="guard_removed", file_path=fpath, line=curr_func.lineno,
                    message=f"safety check removed -- original had {len(orig_guards)} guard(s), edit has {len(curr_guards)}",
                    confidence=0.8,
                ))

            orig_handlers = _get_except_handlers_ast(orig_func)
            curr_handlers = _get_except_handlers_ast(curr_func)
            broad_map = {"Exception", "BaseException", "bare_except"}
            for handler in curr_handlers:
                if handler in broad_map and handler not in orig_handlers:
                    findings.append(ChangeEvidence(
                        kind="exception_broadened", file_path=fpath, line=curr_func.lineno,
                        message=f"exception catch broadened to {handler} -- original caught: {', '.join(orig_handlers) or 'nothing'}",
                        confidence=0.85,
                    ))
                    break

            for node in ast.walk(curr_func):
                if isinstance(node, ast.ExceptHandler) and _is_swallowed_ast(node):
                    orig_had_swallow = any(
                        isinstance(onode, ast.ExceptHandler) and _is_swallowed_ast(onode)
                        for onode in ast.walk(orig_func)
                    )
                    if not orig_had_swallow:
                        exc_type = "bare except"
                        if node.type and isinstance(node.type, ast.Name):
                            exc_type = node.type.id
                        findings.append(ChangeEvidence(
                            kind="exception_swallowed", file_path=fpath, line=node.lineno,
                            message=f"exception silently swallowed ({exc_type}: pass/return None)",
                            confidence=0.9,
                        ))
                    break

            orig_shape = _classify_return_shape_ast(orig_func)
            curr_shape = _classify_return_shape_ast(curr_func)
            if orig_shape != curr_shape and orig_shape != "None":
                findings.append(ChangeEvidence(
                    kind="return_shape_changed", file_path=fpath, line=curr_func.lineno,
                    message=f"return shape changed from {orig_shape} to {curr_shape}",
                    confidence=0.75,
                ))

            orig_raises = _get_raise_types_ast(orig_func)
            curr_raises = _get_raise_types_ast(curr_func)
            removed_raises = orig_raises - curr_raises
            if removed_raises:
                findings.append(ChangeEvidence(
                    kind="validation_removed", file_path=fpath, line=curr_func.lineno,
                    message=f"validation removed -- original raised {', '.join(sorted(removed_raises))}",
                    confidence=0.7,
                ))

        return findings

    def _analyze_regex(self, fpath: str, original_source: str,
                       current_source: str,
                       line_ranges: list[tuple[int, int]],
                       language: str) -> list[ChangeEvidence]:
        """Regex-based analysis for non-Python files when graph.db is unavailable."""
        findings: list[ChangeEvidence] = []

        # Find function names near changed lines (wider search window: 10 lines above)
        func_names: set[str] = set()
        lines = current_source.splitlines()
        func_pattern = re.compile(
            r'\s*(?:(?:pub(?:\s*\(crate\))?\s+)?(?:async\s+)?'
            r'(?:def|func|function|fn|fun|static\s+\w+|public\s+\w+|private\s+\w+|protected\s+\w+))\s+(\w+)'
        )
        for ls, le in line_ranges:
            for i in range(max(0, ls - 10), min(len(lines), le + 5)):
                m = func_pattern.match(lines[i] if i < len(lines) else "")
                if m:
                    func_names.add(m.group(1))

        for func_name in func_names:
            _, _, before_body = _find_function_in_source(original_source, func_name, language)
            _, _, after_body = _find_function_in_source(current_source, func_name, language)
            if not before_body or not after_body:
                continue

            orig_guards = _regex_extract_guards(before_body)
            curr_guards = _regex_extract_guards(after_body)
            if len(orig_guards) > len(curr_guards):
                findings.append(ChangeEvidence(
                    kind="guard_removed", file_path=fpath, line=1,
                    message=f"safety check removed -- original had {len(orig_guards)} guard(s), edit has {len(curr_guards)}",
                    confidence=0.7,
                ))

            orig_exc = set(_regex_extract_exceptions(before_body))
            curr_exc = set(_regex_extract_exceptions(after_body))
            removed = orig_exc - curr_exc
            if removed:
                findings.append(ChangeEvidence(
                    kind="validation_removed", file_path=fpath, line=1,
                    message=f"validation removed -- original raised {', '.join(sorted(removed))}",
                    confidence=0.6,
                ))

            orig_shape = _regex_classify_return_shape(before_body)
            curr_shape = _regex_classify_return_shape(after_body)
            if orig_shape != curr_shape and orig_shape != "none":
                findings.append(ChangeEvidence(
                    kind="return_shape_changed", file_path=fpath, line=1,
                    message=f"return shape changed from {orig_shape} to {curr_shape}",
                    confidence=0.65,
                ))

        return findings


def _normalize_shape(shape: str) -> str:
    """Normalize return shape names between regex and Go indexer vocabularies."""
    _map = {
        "scalar": "value", "None": "none", "nil": "none",
        "null": "none", "undefined": "none", "implicit_None": "none",
        "dict": "collection", "list": "collection", "array": "collection",
    }
    # Handle tuple(N) patterns
    if shape.startswith("tuple"):
        return "tuple"
    return _map.get(shape, shape)


def _ext_to_language(ext: str) -> str:
    """Map file extension to language name."""
    _map = {
        ".py": "python", ".go": "go", ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".rs": "rust", ".java": "java",
        ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".cs": "csharp",
        ".php": "php", ".swift": "swift", ".rb": "ruby", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".cc": "cpp", ".ex": "elixir", ".lua": "lua",
        ".ml": "ocaml", ".groovy": "groovy",
    }
    return _map.get(ext, "")
