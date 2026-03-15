"""Tests for ImportValidator (diagnostic-driven)."""

from __future__ import annotations

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic, Position, Range
from groundtruth.utils.result import Ok
from groundtruth.validators.import_validator import ImportValidator


def _r() -> Range:
    """Shorthand for a dummy range."""
    return Range(start=Position(line=0, character=0), end=Position(line=0, character=10))


def _setup_store(store: SymbolStore) -> None:
    """Populate store with test symbols and exports."""
    r = store.insert_symbol(
        name="hashPassword",
        kind="function",
        language="python",
        file_path="src/utils/crypto.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(password: str) -> str",
        params=None,
        return_type="str",
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(r, Ok)
    store.insert_export(r.value, "src/utils/crypto")

    r = store.insert_symbol(
        name="login",
        kind="function",
        language="python",
        file_path="src/auth/__init__.py",
        line_number=1,
        end_line=10,
        is_exported=True,
        signature="(user: str, pw: str) -> Token",
        params=None,
        return_type="Token",
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(r, Ok)
    store.insert_export(r.value, "src/auth")

    r = store.insert_symbol(
        name="User",
        kind="class",
        language="python",
        file_path="src/models.py",
        line_number=1,
        end_line=50,
        is_exported=True,
        signature=None,
        params=None,
        return_type=None,
        documentation=None,
        last_indexed_at=1000,
    )
    assert isinstance(r, Ok)
    store.insert_export(r.value, "src/models")


_PYRIGHT_CONFIG = DiagnosticCodeConfig(
    unresolved_import=["reportMissingImports", "reportMissingModuleSource"],
    wrong_arg_count=["reportCallIssue"],
    source="Pyright",
)


class TestImportValidator:
    def test_no_diagnostics_passes(self, in_memory_store: SymbolStore) -> None:
        """No diagnostics → no errors."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        result = validator.validate([], "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_wrong_module_path(self, in_memory_store: SymbolStore) -> None:
        """Symbol exists but in different module → wrong_module_path."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "auth.hashPassword" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        err = result.value[0]
        assert err.error_type == "wrong_module_path"
        assert "hashPassword" in err.message
        assert err.suggestion is not None
        assert "crypto" in err.suggestion

    def test_symbol_not_found(self, in_memory_store: SymbolStore) -> None:
        """Symbol doesn't exist anywhere → symbol_not_found."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "auth.nonExistentFunc" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        err = result.value[0]
        assert err.error_type == "symbol_not_found"
        assert "nonExistentFunc" in err.message

    def test_non_import_diagnostics_ignored(self, in_memory_store: SymbolStore) -> None:
        """Non-import diagnostics are skipped."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportCallIssue",
                source="Pyright",
                message="Expected 2 arguments but got 1",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_typescript_diagnostic(self, in_memory_store: SymbolStore) -> None:
        """TypeScript import diagnostic from tsserver."""
        r = in_memory_store.insert_symbol(
            name="UserService",
            kind="class",
            language="typescript",
            file_path="src/services/user.ts",
            line_number=1,
            end_line=50,
            is_exported=True,
            signature=None,
            params=None,
            return_type=None,
            documentation=None,
            last_indexed_at=1000,
        )
        assert isinstance(r, Ok)
        in_memory_store.insert_export(r.value, "./services/user")

        ts_config = DiagnosticCodeConfig(
            unresolved_import=[2307, 2305],
            wrong_arg_count=[2554, 2555],
            source="typescript",
        )

        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code=2307,
                source="typescript",
                message="Cannot find module './models/user' or its corresponding type declarations.",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.ts", "typescript", ts_config)
        assert isinstance(result, Ok)
        # "user" is the last segment — found as UserService? No, "user" != "UserService"
        # So it should be symbol_not_found
        assert len(result.value) == 1

    def test_multiple_errors(self, in_memory_store: SymbolStore) -> None:
        """Multiple import diagnostics produce multiple errors."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "auth.hashPassword" could not be resolved',
            ),
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "models.doesNotExist" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 2

    def test_fallback_no_config(self, in_memory_store: SymbolStore) -> None:
        """Without diagnostic config, severity=1 + keyword heuristic works."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="some_unknown_code",
                source="unknown-server",
                message='Could not be resolved: "auth.hashPassword"',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", None)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].error_type == "wrong_module_path"

    def test_fallback_skips_warnings(self, in_memory_store: SymbolStore) -> None:
        """Fallback path ignores non-Error severity diagnostics."""
        _setup_store(in_memory_store)
        validator = ImportValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=2,  # Warning
                code=None,
                source=None,
                message='Unresolved import "foo"',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", None)
        assert isinstance(result, Ok)
        assert len(result.value) == 0
