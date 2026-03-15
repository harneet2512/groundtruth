"""Tests for LSP protocol types."""

from __future__ import annotations

from groundtruth.lsp.protocol import (
    Diagnostic,
    DocumentSymbol,
    Hover,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    Location,
    MarkupContent,
    Position,
    Range,
    SymbolInformation,
    SymbolKind,
    TextDocumentIdentifier,
    TextDocumentItem,
)


class TestPosition:
    def test_creation(self) -> None:
        pos = Position(line=10, character=5)
        assert pos.line == 10
        assert pos.character == 5

    def test_serialization_camel_case(self) -> None:
        pos = Position(line=1, character=2)
        data = pos.model_dump(by_alias=True)
        assert data == {"line": 1, "character": 2}

    def test_roundtrip(self) -> None:
        data = {"line": 3, "character": 7}
        pos = Position.model_validate(data)
        assert pos.line == 3
        assert pos.character == 7


class TestRange:
    def test_creation(self) -> None:
        r = Range(
            start=Position(line=0, character=0),
            end=Position(line=0, character=10),
        )
        assert r.start.line == 0
        assert r.end.character == 10


class TestLocation:
    def test_creation(self) -> None:
        loc = Location(
            uri="file:///test.py",
            range=Range(
                start=Position(line=0, character=0),
                end=Position(line=0, character=5),
            ),
        )
        assert loc.uri == "file:///test.py"


class TestTextDocumentItem:
    def test_camel_case_serialization(self) -> None:
        item = TextDocumentItem(
            uri="file:///test.py",
            language_id="python",
            version=1,
            text="print('hello')",
        )
        data = item.model_dump(by_alias=True)
        assert "languageId" in data
        assert data["languageId"] == "python"

    def test_from_camel_case(self) -> None:
        data = {
            "uri": "file:///test.py",
            "languageId": "python",
            "version": 1,
            "text": "x = 1",
        }
        item = TextDocumentItem.model_validate(data)
        assert item.language_id == "python"


class TestTextDocumentIdentifier:
    def test_creation(self) -> None:
        doc = TextDocumentIdentifier(uri="file:///test.py")
        assert doc.uri == "file:///test.py"


class TestSymbolKind:
    def test_all_26_values(self) -> None:
        assert len(SymbolKind) == 26

    def test_specific_values(self) -> None:
        assert SymbolKind.FILE == 1
        assert SymbolKind.FUNCTION == 12
        assert SymbolKind.CLASS == 5
        assert SymbolKind.VARIABLE == 13
        assert SymbolKind.INTERFACE == 11
        assert SymbolKind.TYPE_PARAMETER == 26

    def test_from_int(self) -> None:
        kind = SymbolKind(12)
        assert kind == SymbolKind.FUNCTION


class TestDocumentSymbol:
    def test_basic(self) -> None:
        sym = DocumentSymbol(
            name="myFunc",
            kind=SymbolKind.FUNCTION,
            range=Range(
                start=Position(line=0, character=0),
                end=Position(line=5, character=0),
            ),
            selection_range=Range(
                start=Position(line=0, character=4),
                end=Position(line=0, character=10),
            ),
        )
        assert sym.name == "myFunc"
        assert sym.kind == SymbolKind.FUNCTION
        assert sym.children is None

    def test_nested_children(self) -> None:
        child = DocumentSymbol(
            name="inner",
            kind=SymbolKind.VARIABLE,
            range=Range(
                start=Position(line=1, character=0),
                end=Position(line=1, character=10),
            ),
            selection_range=Range(
                start=Position(line=1, character=4),
                end=Position(line=1, character=9),
            ),
        )
        parent = DocumentSymbol(
            name="outer",
            kind=SymbolKind.FUNCTION,
            range=Range(
                start=Position(line=0, character=0),
                end=Position(line=5, character=0),
            ),
            selection_range=Range(
                start=Position(line=0, character=4),
                end=Position(line=0, character=9),
            ),
            children=[child],
        )
        assert parent.children is not None
        assert len(parent.children) == 1
        assert parent.children[0].name == "inner"

    def test_camel_case_deserialization(self) -> None:
        data = {
            "name": "test",
            "kind": 12,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}},
            "selectionRange": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 4},
            },
        }
        sym = DocumentSymbol.model_validate(data)
        assert sym.name == "test"
        assert sym.selection_range.start.line == 0


class TestSymbolInformation:
    def test_creation(self) -> None:
        si = SymbolInformation(
            name="MyClass",
            kind=SymbolKind.CLASS,
            location=Location(
                uri="file:///test.py",
                range=Range(
                    start=Position(line=0, character=0),
                    end=Position(line=10, character=0),
                ),
            ),
        )
        assert si.name == "MyClass"
        assert si.container_name is None


class TestHover:
    def test_markup_content(self) -> None:
        hover = Hover(
            contents=MarkupContent(kind="markdown", value="**bold**"),
        )
        assert isinstance(hover.contents, MarkupContent)

    def test_string_content(self) -> None:
        hover = Hover(contents="simple text")
        assert hover.contents == "simple text"


class TestDiagnostic:
    def test_creation(self) -> None:
        diag = Diagnostic(
            range=Range(
                start=Position(line=5, character=0),
                end=Position(line=5, character=10),
            ),
            severity=1,
            message="Undefined variable 'x'",
        )
        assert diag.severity == 1
        assert diag.message == "Undefined variable 'x'"


class TestJsonRpc:
    def test_request_serialization(self) -> None:
        req = JsonRpcRequest(id=1, method="textDocument/hover", params={"key": "value"})
        data = req.model_dump()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["method"] == "textDocument/hover"

    def test_response_with_result(self) -> None:
        resp = JsonRpcResponse(id=1, result={"key": "value"})
        assert resp.error is None
        assert resp.result == {"key": "value"}

    def test_response_with_error(self) -> None:
        resp = JsonRpcResponse(id=1, error=JsonRpcError(code=-32600, message="Invalid request"))
        assert resp.error is not None
        assert resp.error.code == -32600

    def test_notification(self) -> None:
        notif = JsonRpcNotification(method="initialized", params={})
        data = notif.model_dump()
        assert "id" not in data or data.get("id") is None
        assert data["method"] == "initialized"

    def test_error_with_data(self) -> None:
        err = JsonRpcError(code=-32601, message="Method not found", data={"method": "foo"})
        assert err.data == {"method": "foo"}
