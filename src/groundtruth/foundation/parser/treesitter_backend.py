"""Tree-sitter parser backend — language-agnostic symbol extraction.

Uses tree-sitter to parse source files into concrete syntax trees and extract
symbols. Supports any language with a tree-sitter grammar installed.
"""

from __future__ import annotations

import os
from typing import Any

from groundtruth.foundation.parser.protocol import ExtractedSymbol, ParsedFile

# Conditional imports — tree-sitter is optional
try:
    from tree_sitter import Language, Parser

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

# Grammar registry: language name → (module_name, Language object)
_GRAMMARS: dict[str, Language] = {}
_GRAMMAR_LOADED = False

# Extension → language name mapping
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
}

# Grammar package names for each language
_LANG_TO_PACKAGE: dict[str, str] = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "java": "tree_sitter_java",
    "ruby": "tree_sitter_ruby",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "c_sharp": "tree_sitter_c_sharp",
}

# Node type names for function/method definitions per language
_FUNC_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "python": ("function_definition",),
    "javascript": ("function_declaration", "method_definition", "arrow_function"),
    "typescript": ("function_declaration", "method_definition", "arrow_function"),
    "go": ("function_declaration", "method_declaration"),
    "rust": ("function_item",),
    "java": ("method_declaration", "constructor_declaration"),
}

_CLASS_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "python": ("class_definition",),
    "javascript": ("class_declaration",),
    "typescript": ("class_declaration",),
    "go": (),  # Go doesn't have classes
    "rust": ("struct_item", "impl_item"),
    "java": ("class_declaration", "interface_declaration"),
}


def _load_grammars() -> None:
    """Discover and load available tree-sitter grammars."""
    global _GRAMMAR_LOADED
    if _GRAMMAR_LOADED:
        return
    _GRAMMAR_LOADED = True

    if not HAS_TREE_SITTER:
        return

    for lang_name, pkg_name in _LANG_TO_PACKAGE.items():
        try:
            mod = __import__(pkg_name)
            capsule = mod.language()
            _GRAMMARS[lang_name] = Language(capsule)
        except (ImportError, AttributeError, Exception):
            pass  # Grammar not installed — skip


def _get_parser(language: str) -> Parser | None:
    """Get a tree-sitter Parser for the given language, or None."""
    if not HAS_TREE_SITTER:
        return None
    _load_grammars()
    lang_obj = _GRAMMARS.get(language)
    if lang_obj is None:
        return None
    return Parser(lang_obj)


def _language_for_file(file_path: str) -> str | None:
    """Determine language from file extension."""
    _, ext = os.path.splitext(file_path)
    return _EXT_TO_LANG.get(ext.lower())


def _find_nodes(node: Any, type_names: tuple[str, ...]) -> list[Any]:
    """Recursively find all nodes matching any of the given type names."""
    results = []
    if node.type in type_names:
        results.append(node)
    for child in node.children:
        results.extend(_find_nodes(child, type_names))
    return results


def _extract_python_params(params_node: Any) -> list[str]:
    """Extract parameter names from a Python parameters node, excluding self/cls."""
    if params_node is None:
        return []
    names = []
    for child in params_node.children:
        if child.type == "identifier":
            name = child.text.decode()
            if name not in ("self", "cls"):
                names.append(name)
        elif child.type in ("default_parameter", "typed_default_parameter", "typed_parameter"):
            id_node = child.child_by_field_name("name")
            if id_node is None:
                # Try first identifier child
                for c in child.children:
                    if c.type == "identifier":
                        id_node = c
                        break
            if id_node:
                name = id_node.text.decode()
                if name not in ("self", "cls"):
                    names.append(name)
        elif child.type == "list_splat_pattern":
            for c in child.children:
                if c.type == "identifier":
                    names.append("*" + c.text.decode())
        elif child.type == "dictionary_splat_pattern":
            for c in child.children:
                if c.type == "identifier":
                    names.append("**" + c.text.decode())
    return names


def _get_docstring(node: Any) -> str | None:
    """Extract docstring from a function or class body (Python)."""
    body = node.child_by_field_name("body")
    if body is None:
        return None
    # First statement in body
    for child in body.children:
        if child.type == "expression_statement":
            for expr in child.children:
                if expr.type == "string":
                    text = expr.text.decode()
                    # Strip triple quotes
                    for q in ('"""', "'''", '"', "'"):
                        if text.startswith(q) and text.endswith(q):
                            text = text[len(q):-len(q)]
                            break
                    first_line = text.strip().split("\n")[0].strip()
                    return first_line[:200] if len(first_line) > 200 else first_line
            break
        elif child.type != "comment":
            break
    return None


def _get_return_type_python(node: Any) -> str | None:
    """Extract return type annotation from Python function."""
    ret = node.child_by_field_name("return_type")
    if ret is None:
        return None
    # return_type includes the '-> ' prefix in some tree-sitter versions
    text = ret.text.decode().strip()
    if text.startswith("->"):
        text = text[2:].strip()
    return text


def _build_signature_python(node: Any) -> str | None:
    """Build a signature string from a Python function node."""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return None
    sig = params_node.text.decode()
    ret = _get_return_type_python(node)
    if ret:
        sig += f" -> {ret}"
    return sig


def _has_decorator_python(node: Any, name: str) -> bool:
    """Check if a Python function has a specific decorator."""
    for child in node.children:
        if child.type == "decorator":
            text = child.text.decode()
            if f"@{name}" in text:
                return True
    return False


def _extract_python_symbols(
    root: Any, source: bytes, file_path: str
) -> list[ExtractedSymbol]:
    """Extract symbols from a Python parse tree."""
    symbols: list[ExtractedSymbol] = []
    func_types = _FUNC_NODE_TYPES.get("python", ())
    class_types = _CLASS_NODE_TYPES.get("python", ())

    for node in root.children:
        if not node.is_named:
            continue

        if node.type in func_types:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode()
            params_node = node.child_by_field_name("parameters")
            params = _extract_python_params(params_node)
            raw = source[node.start_byte:node.end_byte].decode(errors="replace")

            symbols.append(ExtractedSymbol(
                name=name,
                kind="function",
                language="python",
                start_line=node.start_point[0],
                end_line=node.end_point[0],
                parameters=params,
                parent_class=None,
                raw_text=raw,
                body_node=node,
                signature=_build_signature_python(node),
                return_type=_get_return_type_python(node),
                is_exported=not name.startswith("_"),
                documentation=_get_docstring(node),
            ))

        elif node.type in class_types:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            class_name = name_node.text.decode()
            raw = source[node.start_byte:node.end_byte].decode(errors="replace")

            # Extract methods as children
            children: list[ExtractedSymbol] = []
            body = node.child_by_field_name("body")
            if body:
                for child_node in body.children:
                    # Handle decorated definitions (e.g., @property)
                    actual_func = child_node
                    is_decorated = child_node.type == "decorated_definition"
                    if is_decorated:
                        # The actual function is inside the decorated_definition
                        for sub in child_node.children:
                            if sub.type in func_types:
                                actual_func = sub
                                break
                        else:
                            continue  # No function found inside decorator

                    if actual_func.type in func_types:
                        method_name_node = actual_func.child_by_field_name("name")
                        if method_name_node is None:
                            continue
                        method_name = method_name_node.text.decode()
                        method_params_node = actual_func.child_by_field_name("parameters")
                        method_params = _extract_python_params(method_params_node)
                        # Use child_node for byte range (includes decorators)
                        # but actual_func for signature/return_type/docstring
                        method_raw = source[child_node.start_byte:child_node.end_byte].decode(
                            errors="replace"
                        )

                        # Check decorators on the decorated_definition wrapper
                        decorator_source = child_node if is_decorated else actual_func
                        if _has_decorator_python(decorator_source, "property"):
                            method_kind = "property"
                        else:
                            method_kind = "method"

                        children.append(ExtractedSymbol(
                            name=method_name,
                            kind=method_kind,
                            language="python",
                            start_line=actual_func.start_point[0],
                            end_line=actual_func.end_point[0],
                            parameters=method_params,
                            parent_class=class_name,
                            raw_text=method_raw,
                            body_node=actual_func,
                            signature=_build_signature_python(actual_func),
                            return_type=_get_return_type_python(actual_func),
                            is_exported=not method_name.startswith("_"),
                            documentation=_get_docstring(actual_func),
                        ))

            symbols.append(ExtractedSymbol(
                name=class_name,
                kind="class",
                language="python",
                start_line=node.start_point[0],
                end_line=node.end_point[0],
                parameters=[],
                parent_class=None,
                raw_text=raw,
                body_node=node,
                signature=None,
                return_type=None,
                is_exported=not class_name.startswith("_"),
                documentation=_get_docstring(node),
                children=tuple(children),
            ))

        elif node.type == "expression_statement":
            # Module-level assignments: NAME = ...
            for child in node.children:
                if child.type == "assignment":
                    left = child.child_by_field_name("left")
                    if left and left.type == "identifier":
                        var_name = left.text.decode()
                        if var_name.isupper():
                            symbols.append(ExtractedSymbol(
                                name=var_name,
                                kind="variable",
                                language="python",
                                start_line=node.start_point[0],
                                end_line=node.end_point[0],
                                parameters=[],
                                raw_text=source[node.start_byte:node.end_byte].decode(
                                    errors="replace"
                                ),
                                body_node=node,
                                is_exported=not var_name.startswith("_"),
                            ))

    return symbols


class TreeSitterExtractor:
    """Symbol extractor using tree-sitter for language-agnostic parsing."""

    def __init__(self) -> None:
        _load_grammars()

    @property
    def supported_languages(self) -> list[str]:
        """Languages with installed tree-sitter grammars."""
        _load_grammars()
        return list(_GRAMMARS.keys())

    def parse_file(self, file_path: str, content: bytes | None = None) -> ParsedFile:
        """Parse a source file using tree-sitter."""
        language = _language_for_file(file_path)
        if language is None:
            return ParsedFile(
                file_path=file_path,
                language="unknown",
                source=content or b"",
                error=f"Unsupported file extension: {file_path}",
            )

        parser = _get_parser(language)
        if parser is None:
            return ParsedFile(
                file_path=file_path,
                language=language,
                source=content or b"",
                error=f"No tree-sitter grammar for {language}",
            )

        if content is None:
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except OSError as exc:
                return ParsedFile(
                    file_path=file_path,
                    language=language,
                    source=b"",
                    error=str(exc),
                )

        tree = parser.parse(content)
        return ParsedFile(
            file_path=file_path,
            language=language,
            source=content,
            tree=tree,
        )

    def extract_symbols(self, parsed: ParsedFile) -> list[ExtractedSymbol]:
        """Extract symbols from a parsed file."""
        if parsed.error or parsed.tree is None:
            return []

        root = parsed.tree.root_node

        if parsed.language == "python":
            return _extract_python_symbols(root, parsed.source, parsed.file_path)

        # Generic extraction for other languages — find functions and classes
        return self._extract_generic(root, parsed.source, parsed.file_path, parsed.language)

    def _extract_generic(
        self, root: Any, source: bytes, file_path: str, language: str
    ) -> list[ExtractedSymbol]:
        """Generic symbol extraction for languages without specialized handlers."""
        symbols: list[ExtractedSymbol] = []
        func_types = _FUNC_NODE_TYPES.get(language, ())
        class_types = _CLASS_NODE_TYPES.get(language, ())

        for fn_node in _find_nodes(root, func_types):
            name_node = fn_node.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode()
            raw = source[fn_node.start_byte:fn_node.end_byte].decode(errors="replace")
            symbols.append(ExtractedSymbol(
                name=name,
                kind="function",
                language=language,
                start_line=fn_node.start_point[0],
                end_line=fn_node.end_point[0],
                raw_text=raw,
                body_node=fn_node,
                is_exported=not name.startswith("_"),
            ))

        for cls_node in _find_nodes(root, class_types):
            name_node = cls_node.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode()
            raw = source[cls_node.start_byte:cls_node.end_byte].decode(errors="replace")
            symbols.append(ExtractedSymbol(
                name=name,
                kind="class",
                language=language,
                start_line=cls_node.start_point[0],
                end_line=cls_node.end_point[0],
                raw_text=raw,
                body_node=cls_node,
                is_exported=not name.startswith("_"),
            ))

        return symbols
