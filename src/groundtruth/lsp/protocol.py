"""LSP protocol types as Pydantic models."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class LSPModel(BaseModel):
    """Base model with camelCase aliases for LSP compatibility."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


# --- Positions and Ranges ---


class Position(LSPModel):
    """A position in a text document (0-indexed line and character)."""

    line: int
    character: int


class Range(LSPModel):
    """A range in a text document."""

    start: Position
    end: Position


class Location(LSPModel):
    """A location in a document."""

    uri: str
    range: Range


# --- Text Documents ---


class TextDocumentIdentifier(LSPModel):
    """Identifies a text document by its URI."""

    uri: str


class TextDocumentItem(LSPModel):
    """An item to transfer a text document from client to server."""

    uri: str
    language_id: str
    version: int
    text: str


# --- Symbol Kinds ---


class SymbolKind(IntEnum):
    """LSP SymbolKind enum (all 26 values)."""

    FILE = 1
    MODULE = 2
    NAMESPACE = 3
    PACKAGE = 4
    CLASS = 5
    METHOD = 6
    PROPERTY = 7
    FIELD = 8
    CONSTRUCTOR = 9
    ENUM = 10
    INTERFACE = 11
    FUNCTION = 12
    VARIABLE = 13
    CONSTANT = 14
    STRING = 15
    NUMBER = 16
    BOOLEAN = 17
    ARRAY = 18
    OBJECT = 19
    KEY = 20
    NULL = 21
    ENUM_MEMBER = 22
    STRUCT = 23
    EVENT = 24
    OPERATOR = 25
    TYPE_PARAMETER = 26


# --- Symbols ---


class DocumentSymbol(LSPModel):
    """A symbol in a document (hierarchical)."""

    name: str
    detail: str | None = None
    kind: SymbolKind
    range: Range
    selection_range: Range
    children: list[DocumentSymbol] | None = None


class SymbolInformation(LSPModel):
    """Flat symbol information (legacy LSP response format)."""

    name: str
    kind: SymbolKind
    location: Location
    container_name: str | None = None


# --- Hover ---


class MarkupContent(LSPModel):
    """Markup content for hover responses."""

    kind: str  # "plaintext" | "markdown"
    value: str


class Hover(LSPModel):
    """Hover response from LSP."""

    contents: MarkupContent | str | list[str]
    range: Range | None = None


# --- Diagnostics ---


class Diagnostic(LSPModel):
    """A diagnostic (error/warning) from the language server."""

    range: Range
    severity: int | None = None  # 1=Error, 2=Warning, 3=Info, 4=Hint
    code: int | str | None = None
    source: str | None = None
    message: str


# --- Signature Help ---


class ParameterInformation(LSPModel):
    """Parameter information in a signature."""

    label: str | list[int]
    documentation: str | MarkupContent | None = None


class SignatureInformation(LSPModel):
    """Information about a callable signature."""

    label: str
    documentation: str | MarkupContent | None = None
    parameters: list[ParameterInformation] | None = None
    active_parameter: int | None = None


class SignatureHelp(LSPModel):
    """Signature help response from LSP."""

    signatures: list[SignatureInformation]
    active_signature: int | None = None
    active_parameter: int | None = None


# --- JSON-RPC ---


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    id: int
    method: str
    params: dict[str, Any] | list[Any] | None = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response."""

    jsonrpc: str = "2.0"
    id: int | None = None
    result: Any = None
    error: JsonRpcError | None = None


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


class JsonRpcNotification(BaseModel):
    """JSON-RPC 2.0 notification (no id)."""

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | list[Any] | None = None
