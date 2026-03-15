"""Tests for PackageValidator (diagnostic-driven)."""

from __future__ import annotations

from groundtruth.index.store import SymbolStore
from groundtruth.lsp.config import DiagnosticCodeConfig
from groundtruth.lsp.protocol import Diagnostic, Position, Range
from groundtruth.utils.result import Ok
from groundtruth.validators.package_validator import PackageValidator


def _r() -> Range:
    """Shorthand for a dummy range."""
    return Range(start=Position(line=0, character=0), end=Position(line=0, character=10))


_PYRIGHT_CONFIG = DiagnosticCodeConfig(
    unresolved_import=["reportMissingImports", "reportMissingModuleSource"],
    wrong_arg_count=["reportCallIssue"],
    source="Pyright",
)

_TS_CONFIG = DiagnosticCodeConfig(
    unresolved_import=[2307, 2305],
    wrong_arg_count=[2554, 2555],
    source="typescript",
)


class TestPackageValidator:
    def test_installed_package_passes(self, in_memory_store: SymbolStore) -> None:
        """Installed package import passes — diagnostic for it means it's not resolved
        by LSP but IS in our packages table, so no error."""
        in_memory_store.insert_package("requests", "2.31.0", "pip")
        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code="reportMissingImports", source="Pyright",
                message='Import "requests" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_missing_package_caught(self, in_memory_store: SymbolStore) -> None:
        """Uninstalled package triggers error."""
        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code="reportMissingImports", source="Pyright",
                message='Import "requests" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].package_name == "requests"

    def test_no_diagnostics_passes(self, in_memory_store: SymbolStore) -> None:
        """No diagnostics → no package errors."""
        validator = PackageValidator(in_memory_store)
        result = validator.validate([], "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_local_module_skipped(self, in_memory_store: SymbolStore) -> None:
        """Local modules with exports in the store are skipped."""
        r = in_memory_store.insert_symbol(
            name="helper", kind="function", language="python",
            file_path="src/utils.py", line_number=1, end_line=5,
            is_exported=True, signature=None, params=None,
            return_type=None, documentation=None, last_indexed_at=1000,
        )
        assert isinstance(r, Ok)
        in_memory_store.insert_export(r.value, "src/utils")

        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code="reportMissingImports", source="Pyright",
                message='Import "src.utils" could not be resolved',
            ),
        ]
        result = validator.validate(diagnostics, "src/app.py", "python", _PYRIGHT_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_typescript_package(self, in_memory_store: SymbolStore) -> None:
        """TypeScript npm package validation."""
        in_memory_store.insert_package("axios", "1.6.0", "npm")
        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code=2307, source="typescript",
                message="Cannot find module 'axios' or its corresponding type declarations.",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.ts", "typescript", _TS_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_typescript_missing_package(self, in_memory_store: SymbolStore) -> None:
        """Missing TypeScript npm package caught."""
        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code=2307, source="typescript",
                message="Cannot find module 'axios' or its corresponding type declarations.",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.ts", "typescript", _TS_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].package_name == "axios"

    def test_scoped_npm_package(self, in_memory_store: SymbolStore) -> None:
        """Scoped npm package @scope/pkg handled correctly."""
        in_memory_store.insert_package("@angular/core", "17.0.0", "npm")
        validator = PackageValidator(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(), severity=1,
                code=2307, source="typescript",
                message="Cannot find module '@angular/core' or its corresponding type declarations.",
            ),
        ]
        result = validator.validate(diagnostics, "src/app.ts", "typescript", _TS_CONFIG)
        assert isinstance(result, Ok)
        assert len(result.value) == 0
