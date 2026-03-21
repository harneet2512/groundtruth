"""AST-based Python symbol and import extraction.

Uses stdlib `ast` to extract symbols and imports from Python files instantly,
bypassing LSP which can timeout on large projects. Produces data structures
compatible with the existing store.insert_symbol() interface.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from groundtruth.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ASTSymbol:
    """A symbol extracted from a Python AST."""

    name: str
    kind: str  # "function" | "class" | "method" | "variable" | "property"
    line: int  # 0-indexed (to match LSP/store convention)
    end_line: int  # 0-indexed
    signature: str | None
    return_type: str | None
    is_exported: bool  # not name.startswith("_")
    documentation: str | None
    children: tuple[ASTSymbol, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ASTImport:
    """An import extracted from a Python AST."""

    module: str | None  # dotted module path
    name: str  # imported name
    alias: str | None  # "as" alias
    line: int  # 0-indexed
    is_from: bool  # from X import Y
    level: int  # relative import dots


def _build_signature(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a signature string from a function AST node."""
    parts: list[str] = []
    args = func.args

    # Positional-only args
    for i, arg in enumerate(args.posonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        # Defaults for posonlyargs come from the end of args.defaults
        # posonlyargs defaults are at the start of defaults list
        default_offset = len(args.defaults) - len(args.posonlyargs) - len(args.args)
        default_idx = default_offset + i
        if default_idx >= 0 and default_idx < len(args.defaults):
            part += " = ..."
        parts.append(part)
    if args.posonlyargs:
        parts.append("/")

    # Regular args
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    for i, arg in enumerate(args.args):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        # Defaults are right-aligned with args
        default_idx = i - (num_args - num_defaults)
        if default_idx >= 0:
            part += " = ..."
        parts.append(part)

    # *args
    if args.vararg:
        part = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            part += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(part)
    elif args.kwonlyargs:
        parts.append("*")

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            part += " = ..."
        parts.append(part)

    # **kwargs
    if args.kwarg:
        part = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            part += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(part)

    sig = f"({', '.join(parts)})"

    # Return type
    if func.returns:
        ret = ast.unparse(func.returns)
        sig += f" -> {ret}"

    return sig


def _get_return_type(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Extract return type annotation from a function."""
    if func.returns:
        return ast.unparse(func.returns)
    return None


def _get_docstring(node: ast.AST) -> str | None:
    """Extract first line of docstring, capped at 200 chars."""
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return None
    first_line = doc.split("\n")[0].strip()
    if len(first_line) > 200:
        return first_line[:200]
    return first_line


def _has_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Check if a function/method has a decorator with the given name."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == name:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == name:
            return True
    return False


def _extract_class_children(
    cls: ast.ClassDef,
) -> tuple[ASTSymbol, ...]:
    """Extract methods and properties from a class body."""
    children: list[ASTSymbol] = []
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _has_decorator(node, "property"):
                kind = "property"
            else:
                kind = "method"
            sig = _build_signature(node)
            children.append(
                ASTSymbol(
                    name=node.name,
                    kind=kind,
                    line=node.lineno - 1,
                    end_line=node.end_lineno - 1 if node.end_lineno else node.lineno - 1,
                    signature=sig,
                    return_type=_get_return_type(node),
                    is_exported=not node.name.startswith("_"),
                    documentation=_get_docstring(node),
                )
            )
    return tuple(children)


def parse_python_file(file_path: str) -> list[ASTSymbol]:
    """Parse a Python file and extract symbols.

    Returns an empty list on SyntaxError or file read failure.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as exc:
        logger.debug("ast_read_failed", file=file_path, error=str(exc))
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        logger.debug("ast_parse_failed", file=file_path, error=str(exc))
        return []

    symbols: list[ASTSymbol] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _build_signature(node)
            symbols.append(
                ASTSymbol(
                    name=node.name,
                    kind="function",
                    line=node.lineno - 1,
                    end_line=node.end_lineno - 1 if node.end_lineno else node.lineno - 1,
                    signature=sig,
                    return_type=_get_return_type(node),
                    is_exported=not node.name.startswith("_"),
                    documentation=_get_docstring(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            children = _extract_class_children(node)
            symbols.append(
                ASTSymbol(
                    name=node.name,
                    kind="class",
                    line=node.lineno - 1,
                    end_line=node.end_lineno - 1 if node.end_lineno else node.lineno - 1,
                    signature=None,
                    return_type=None,
                    is_exported=not node.name.startswith("_"),
                    documentation=_get_docstring(node),
                    children=children,
                )
            )
        elif isinstance(node, ast.Assign):
            # Only uppercase names (constants) at module level
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(
                        ASTSymbol(
                            name=target.id,
                            kind="variable",
                            line=node.lineno - 1,
                            end_line=node.end_lineno - 1 if node.end_lineno else node.lineno - 1,
                            signature=None,
                            return_type=None,
                            is_exported=not target.id.startswith("_"),
                            documentation=None,
                        )
                    )
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                ann = ast.unparse(node.annotation)
                symbols.append(
                    ASTSymbol(
                        name=node.target.id,
                        kind="variable",
                        line=node.lineno - 1,
                        end_line=node.end_lineno - 1 if node.end_lineno else node.lineno - 1,
                        signature=ann,
                        return_type=None,
                        is_exported=not node.target.id.startswith("_"),
                        documentation=None,
                    )
                )

    return symbols


@dataclass(frozen=True)
class ClassAttributeInfo:
    """A self.* attribute extracted from a class."""

    name: str
    setter_methods: tuple[str, ...]   # methods that assign self.name
    reader_methods: tuple[str, ...]   # methods that read self.name
    line: int


def extract_class_attributes(file_path: str) -> dict[str, list[ClassAttributeInfo]]:
    """Extract self.* attributes per class from a Python file.

    Returns {class_name: [ClassAttributeInfo, ...]}.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return {}

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return {}

    result: dict[str, list[ClassAttributeInfo]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        # Track per-attribute: which methods set it, which read it
        attr_setters: dict[str, set[str]] = {}
        attr_readers: dict[str, set[str]] = {}
        attr_lines: dict[str, int] = {}

        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_name = item.name

            for child in ast.walk(item):
                if not isinstance(child, ast.Attribute):
                    continue
                if not (isinstance(child.value, ast.Name) and child.value.id == "self"):
                    continue

                attr_name = child.attr

                # Record line
                if attr_name not in attr_lines:
                    attr_lines[attr_name] = child.lineno - 1

                # Determine if setter or reader by checking parent context
                # We check if this Attribute node is a target of assignment
                is_setter = False
                for parent in ast.walk(item):
                    if isinstance(parent, (ast.Assign, ast.AugAssign)):
                        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                        for t in targets:
                            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                                if t.value.id == "self" and t.attr == attr_name:
                                    is_setter = True
                    elif isinstance(parent, ast.AnnAssign) and parent.target is not None:
                        t = parent.target
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                            if t.value.id == "self" and t.attr == attr_name:
                                is_setter = True

                if is_setter:
                    attr_setters.setdefault(attr_name, set()).add(method_name)
                # All references are also readers
                attr_readers.setdefault(attr_name, set()).add(method_name)

        # Build ClassAttributeInfo list
        attrs: list[ClassAttributeInfo] = []
        for attr_name in sorted(set(list(attr_setters.keys()) + list(attr_readers.keys()))):
            attrs.append(ClassAttributeInfo(
                name=attr_name,
                setter_methods=tuple(sorted(attr_setters.get(attr_name, set()))),
                reader_methods=tuple(sorted(attr_readers.get(attr_name, set()))),
                line=attr_lines.get(attr_name, 0),
            ))

        if attrs:
            result[node.name] = attrs

    return result


def extract_base_classes(file_path: str) -> dict[str, list[str]]:
    """Extract base classes per class from a Python file.

    Returns {class_name: [base_class_name, ...]}.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return {}

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return {}

    _skip_bases = {"object", "ABC", "ABCMeta"}
    result: dict[str, list[str]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases: list[str] = []
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id not in _skip_bases:
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(base.attr)
        if bases:
            result[node.name] = bases

    return result


def parse_python_imports(file_path: str) -> list[ASTImport]:
    """Parse a Python file and extract import statements.

    Returns an empty list on SyntaxError or file read failure.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as exc:
        logger.debug("ast_read_failed", file=file_path, error=str(exc))
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        logger.debug("ast_parse_failed", file=file_path, error=str(exc))
        return []

    imports: list[ASTImport] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ASTImport(
                        module=alias.name,
                        name=alias.name.split(".")[-1],
                        alias=alias.asname,
                        line=node.lineno - 1,
                        is_from=False,
                        level=0,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.append(
                    ASTImport(
                        module=node.module,
                        name=alias.name,
                        alias=alias.asname,
                        line=node.lineno - 1,
                        is_from=True,
                        level=node.level or 0,
                    )
                )

    return imports
