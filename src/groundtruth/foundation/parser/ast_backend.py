"""Python AST parser backend — wraps existing ast_parser.py behind the SymbolExtractor protocol.

This is the safe fallback when tree-sitter is not installed. It uses stdlib `ast`
and produces the same ExtractedSymbol output as the tree-sitter backend for Python files.
"""

from __future__ import annotations

import ast
import os

from groundtruth.foundation.parser.protocol import ExtractedSymbol, ParsedFile
from groundtruth.index.ast_parser import ASTSymbol, parse_python_file


def _ast_symbol_to_extracted(
    sym: ASTSymbol,
    language: str,
    source_lines: list[str],
    parent_class: str | None = None,
) -> ExtractedSymbol:
    """Convert an ASTSymbol to an ExtractedSymbol."""
    # Extract parameter names from signature
    params: list[str] = []
    if sym.signature and sym.kind in ("function", "method", "property"):
        params = _parse_param_names(sym.signature)

    # Extract raw text from source lines
    start = sym.line
    end = sym.end_line + 1
    if 0 <= start < len(source_lines) and end <= len(source_lines):
        raw_text = "\n".join(source_lines[start:end])
    else:
        raw_text = ""

    # Convert children recursively
    children = tuple(
        _ast_symbol_to_extracted(child, language, source_lines, parent_class=sym.name)
        for child in sym.children
    )

    return ExtractedSymbol(
        name=sym.name,
        kind=sym.kind,
        language=language,
        start_line=sym.line,
        end_line=sym.end_line,
        parameters=params,
        parent_class=parent_class,
        raw_text=raw_text,
        body_node=None,  # ast backend doesn't preserve nodes
        signature=sym.signature,
        return_type=sym.return_type,
        is_exported=sym.is_exported,
        documentation=sym.documentation,
        children=children,
    )


def _parse_param_names(signature: str) -> list[str]:
    """Extract parameter names from a signature string like '(self, x: int, y: str = ...) -> None'.

    Excludes self and cls.
    """
    # Strip return type
    sig = signature.split("->")[0].strip()
    # Strip parens
    if sig.startswith("(") and sig.endswith(")"):
        sig = sig[1:-1]
    elif sig.startswith("("):
        sig = sig[1:]

    if not sig.strip():
        return []

    names: list[str] = []
    # Split on commas, handling nested parens/brackets
    depth = 0
    current = ""
    for ch in sig:
        if ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            name = _extract_param_name(current.strip())
            if name:
                names.append(name)
            current = ""
        else:
            current += ch

    if current.strip():
        name = _extract_param_name(current.strip())
        if name:
            names.append(name)

    return names


def _extract_param_name(param: str) -> str | None:
    """Extract the name from a parameter like 'x: int = ...' or '*args' or '/'."""
    if not param or param == "/":
        return None

    # Handle *args, **kwargs
    if param.startswith("**"):
        name = param[2:].split(":")[0].strip()
        return f"**{name}" if name else None
    if param.startswith("*"):
        rest = param[1:].split(":")[0].strip()
        if not rest:
            return None  # bare * separator
        return f"*{rest}"

    # Regular param: take everything before ':' or '='
    name = param.split(":")[0].split("=")[0].strip()
    if name in ("self", "cls"):
        return None
    return name if name else None


class PythonASTExtractor:
    """Symbol extractor using Python's stdlib ast module.

    Wraps the existing parse_python_file() from index/ast_parser.py.
    Only supports Python.
    """

    @property
    def supported_languages(self) -> list[str]:
        return ["python"]

    def parse_file(self, file_path: str, content: bytes | None = None) -> ParsedFile:
        """Parse a Python file."""
        _, ext = os.path.splitext(file_path)
        if ext.lower() != ".py":
            return ParsedFile(
                file_path=file_path,
                language="unknown",
                source=content or b"",
                error=f"PythonASTExtractor only supports .py files, got {ext}",
            )

        if content is None:
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except OSError as exc:
                return ParsedFile(
                    file_path=file_path,
                    language="python",
                    source=b"",
                    error=str(exc),
                )

        # Try parsing to detect syntax errors
        try:
            ast.parse(content, filename=file_path)
        except SyntaxError as exc:
            return ParsedFile(
                file_path=file_path,
                language="python",
                source=content,
                error=str(exc),
            )

        return ParsedFile(
            file_path=file_path,
            language="python",
            source=content,
        )

    def extract_symbols(self, parsed: ParsedFile) -> list[ExtractedSymbol]:
        """Extract symbols using the existing ast_parser."""
        if parsed.error:
            return []

        # Use existing parse_python_file which reads from disk
        ast_symbols = parse_python_file(parsed.file_path)

        # Convert to ExtractedSymbol
        source_text = parsed.source.decode(errors="replace")
        source_lines = source_text.split("\n")

        return [
            _ast_symbol_to_extracted(sym, "python", source_lines)
            for sym in ast_symbols
        ]
