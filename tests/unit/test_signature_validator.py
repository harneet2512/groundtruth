"""Tests for SignatureValidator (diagnostic-driven)."""

from __future__ import annotations

import json

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic, Position, Range
from groundtruth.utils.result import Ok
from groundtruth.validators.signature_validator import SignatureValidator


def _r() -> Range:
    """Shorthand for a dummy range."""
    return Range(start=Position(line=0, character=0), end=Position(line=0, character=10))


def _insert_func(
    store: SymbolStore,
    name: str,
    params: list[dict[str, object]] | None = None,
    signature: str | None = None,
) -> int:
    """Insert a function symbol with optional params/signature."""
    params_json = json.dumps(params) if params is not None else None
    r = store.insert_symbol(
        name=name,
        kind="function",
        language="python",
        file_path="src/funcs.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature=signature,
        params=params_json,
        return_type=None,
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(r, Ok)
    return r.value


_PYRIGHT_CONFIG = DiagnosticCodeConfig(
    unresolved_import=["reportMissingImports"],
    wrong_arg_count=["reportCallIssue", "reportGeneralClassIssue"],
    source="Pyright",
)

_TS_CONFIG = DiagnosticCodeConfig(
    unresolved_import=[2307],
    wrong_arg_count=[2554, 2555],
    source="typescript",
)


class TestSignatureValidator:
    def test_no_diagnostics_passes(self, in_memory_store: SymbolStore) -> None:
        """No diagnostics → no errors."""
        validator = SignatureValidator(in_memory_store)
        result = validator.validate([], "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_too_many_args(self, in_memory_store: SymbolStore) -> None:
        """Diagnostic for too many arguments produces a SignatureError."""
        _insert_func(in_memory_store, "add", signature="(a: int, b: int) -> int")
        validator = SignatureValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportCallIssue",
                source="Pyright",
                message="Expected 2 arguments, but got 3",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        err = result.value[0]
        assert err.expected_params == 2
        assert err.actual_params == 3

    def test_too_few_args_typescript(self, in_memory_store: SymbolStore) -> None:
        """TypeScript diagnostic for too few arguments."""
        validator = SignatureValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code=2554,
                source="typescript",
                message="Expected 3 arguments, but got 1.",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.ts", "typescript", _TS_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        err = result.value[0]
        assert err.expected_params == 3
        assert err.actual_params == 1

    def test_non_signature_diagnostics_ignored(self, in_memory_store: SymbolStore) -> None:
        """Import diagnostics are not matched by signature validator."""
        validator = SignatureValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "foo" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_func_name_extracted_from_message(self, in_memory_store: SymbolStore) -> None:
        """Function name is extracted from quoted string in message."""
        _insert_func(in_memory_store, "greet", signature="(name: str) -> str")
        validator = SignatureValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportCallIssue",
                source="Pyright",
                message='No overloads for "greet" match the provided arguments. Expected 1 argument, but got 3',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].function_name == "greet"

    def test_fallback_no_config(self, in_memory_store: SymbolStore) -> None:
        """Without diagnostic config, severity + keyword heuristic works."""
        validator = SignatureValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="unknown",
                source="unknown-server",
                message="Expected 2 arguments, but got 5",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.rs", "rust", None)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].expected_params == 2
        assert result.value[0].actual_params == 5
