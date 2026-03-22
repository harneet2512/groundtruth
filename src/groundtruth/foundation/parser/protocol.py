"""Protocol and data types for language-agnostic symbol extraction.

Every parser backend (tree-sitter, Python ast) implements the SymbolExtractor
protocol. Downstream components (fingerprints, structural vectors, token sketches)
depend ONLY on these types — never on a specific parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExtractedSymbol:
    """A symbol extracted from source code — language-agnostic."""

    name: str
    kind: str  # "function" | "method" | "class" | "variable" | "property"
    language: str  # "python" | "typescript" | "go" | "rust" | ...
    start_line: int  # 0-indexed
    end_line: int  # 0-indexed
    parameters: list[str] = field(default_factory=list)  # param names (excl self/cls)
    parent_class: str | None = None  # for methods: enclosing class name
    raw_text: str = ""  # source text of the symbol body
    body_node: Any = field(default=None, repr=False)  # parser-specific node for deeper analysis
    signature: str | None = None
    return_type: str | None = None
    is_exported: bool = True
    documentation: str | None = None
    children: tuple[ExtractedSymbol, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParsedFile:
    """Result of parsing a single source file."""

    file_path: str
    language: str
    source: bytes  # raw file content
    tree: Any = field(default=None, repr=False)  # parser-specific tree object
    error: str | None = None  # set if parsing failed


@runtime_checkable
class SymbolExtractor(Protocol):
    """Protocol for language-agnostic symbol extraction."""

    @property
    def supported_languages(self) -> list[str]:
        """Languages this extractor can handle."""
        ...

    def parse_file(self, file_path: str, content: bytes | None = None) -> ParsedFile:
        """Parse a source file into a ParsedFile.

        If content is provided, use it instead of reading from disk.
        """
        ...

    def extract_symbols(self, parsed: ParsedFile) -> list[ExtractedSymbol]:
        """Extract all symbols from a parsed file."""
        ...
