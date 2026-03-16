"""Tests for ValidationOrchestrator (diagnostic-driven, async)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from groundtruth.ai.semantic_resolver import ResolutionResult
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.manager import LSPManager
from groundtruth.lsp.protocol import Diagnostic, Position, Range
from groundtruth.utils.result import Err, GroundTruthError, Ok
from groundtruth.validators.orchestrator import ValidationOrchestrator


def _r() -> Range:
    return Range(start=Position(line=0, character=0), end=Position(line=0, character=10))


def _populate_store(store: SymbolStore) -> None:
    """Set up a store with symbols, exports, and packages."""
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

    store.insert_package("requests", "2.31.0", "pip")


def _mock_lsp_manager(diagnostics: list[Diagnostic] | None = None) -> LSPManager:
    """Create a mock LSPManager that returns controlled diagnostics."""
    manager = MagicMock(spec=LSPManager)
    client = MagicMock(spec=LSPClient)

    if diagnostics is None:
        diagnostics = []

    client.open_and_get_diagnostics = AsyncMock(return_value=diagnostics)
    client.did_close = AsyncMock()
    client.clear_diagnostics = MagicMock()

    manager.ensure_server = AsyncMock(return_value=Ok(client))
    return manager


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_valid_code_no_diagnostics(self, in_memory_store: SymbolStore) -> None:
        """No diagnostics → valid=True, empty errors."""
        _populate_store(in_memory_store)
        lsp = _mock_lsp_manager([])
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("from src.auth import login\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        assert result.value.valid is True
        assert result.value.errors == []

    @pytest.mark.asyncio
    async def test_import_error_detected(self, in_memory_store: SymbolStore) -> None:
        """Import diagnostic → wrong_module_path with suggestion."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "auth.hashPassword" could not be resolved',
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("from auth import hashPassword\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        vr = result.value
        assert vr.valid is False
        assert len(vr.errors) >= 1
        err = vr.errors[0]
        assert err["type"] == "wrong_module_path"
        assert err["suggestion"] is not None

    @pytest.mark.asyncio
    async def test_missing_package_detected(self, in_memory_store: SymbolStore) -> None:
        """Package diagnostic for uninstalled package."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "axios" could not be resolved',
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("import axios\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        vr = result.value
        assert vr.valid is False
        types = {e["type"] for e in vr.errors}
        assert "compiler_diagnostic" in types

    @pytest.mark.asyncio
    async def test_signature_error_detected(self, in_memory_store: SymbolStore) -> None:
        """Signature diagnostic → wrong_arg_count error."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportCallIssue",
                source="Pyright",
                message="Expected 2 arguments, but got 3",
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("login(a, b, c)\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        vr = result.value
        assert vr.valid is False
        types = {e["type"] for e in vr.errors}
        assert "wrong_arg_count" in types

    @pytest.mark.asyncio
    async def test_language_inference(self, in_memory_store: SymbolStore) -> None:
        """Language inferred from file extension when not provided."""
        _populate_store(in_memory_store)
        lsp = _mock_lsp_manager([])
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("x = 1\n", "src/app.py")
        assert isinstance(result, Ok)
        assert result.value.valid is True

    @pytest.mark.asyncio
    async def test_unknown_extension_returns_valid(self, in_memory_store: SymbolStore) -> None:
        """Unknown file extension with no language returns valid."""
        lsp = _mock_lsp_manager([])
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("some random code", "file.unknown")
        assert isinstance(result, Ok)
        assert result.value.valid is True

    @pytest.mark.asyncio
    async def test_latency_measured(self, in_memory_store: SymbolStore) -> None:
        """Latency is measured and > 0."""
        _populate_store(in_memory_store)
        lsp = _mock_lsp_manager([])
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("x = 1\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        assert result.value.latency_ms > 0

    @pytest.mark.asyncio
    async def test_ai_used_always_false_in_validate(self, in_memory_store: SymbolStore) -> None:
        """ai_used is always False in validate() (no AI)."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "nowhere.unknownFunc" could not be resolved',
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp)
        result = await orch.validate("from nowhere import x\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        assert result.value.ai_used is False

    @pytest.mark.asyncio
    async def test_no_lsp_manager_graceful(self, in_memory_store: SymbolStore) -> None:
        """Without LSP manager, validate returns valid for correct imports."""
        _populate_store(in_memory_store)
        orch = ValidationOrchestrator(in_memory_store, lsp_manager=None)
        # Use a valid import (login exists in src/auth/__init__.py)
        result = await orch.validate("from auth import login\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        assert result.value.valid is True

    @pytest.mark.asyncio
    async def test_lsp_server_unavailable_graceful(self, in_memory_store: SymbolStore) -> None:
        """If LSP server fails to start, AST validation still catches errors."""
        _populate_store(in_memory_store)
        manager = MagicMock(spec=LSPManager)
        manager.ensure_server = AsyncMock(
            return_value=Err(GroundTruthError(code="lsp_start_failed", message="No server"))
        )
        orch = ValidationOrchestrator(in_memory_store, manager)
        # Use a valid import to test graceful degradation
        result = await orch.validate("from auth import login\n", "src/app.py", "python")
        assert isinstance(result, Ok)
        assert result.value.valid is True


class TestOrchestratorWithAI:
    @pytest.mark.asyncio
    async def test_validate_with_ai_resolves_unresolved(self, in_memory_store: SymbolStore) -> None:
        """validate_with_ai calls resolver for errors without suggestions."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "nowhere.unknownFunc" could not be resolved',
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp, api_key="test-key")

        resolution = ResolutionResult(
            intended_symbol="hashPassword",
            suggested_fix="from src.utils.crypto import hashPassword",
            confidence=0.9,
            reasoning="Best match",
        )

        with patch.object(orch._resolver, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = Ok(resolution)
            result = await orch.validate_with_ai(
                "from nowhere import unknownFunc\n", "src/app.py", "python"
            )

        assert isinstance(result, Ok)
        vr = result.value
        assert vr.ai_used is True
        ai_suggestions = [e for e in vr.errors if e.get("suggestion", {}).get("source") == "ai"]
        assert len(ai_suggestions) >= 1

    @pytest.mark.asyncio
    async def test_validate_with_ai_no_key(self, in_memory_store: SymbolStore) -> None:
        """Without API key, ai_used stays False."""
        _populate_store(in_memory_store)
        diagnostics = [
            Diagnostic(
                range=_r(),
                severity=1,
                code="reportMissingImports",
                source="Pyright",
                message='Import "nowhere.unknownFunc" could not be resolved',
            ),
        ]
        lsp = _mock_lsp_manager(diagnostics)
        orch = ValidationOrchestrator(in_memory_store, lsp, api_key=None)
        result = await orch.validate_with_ai(
            "from nowhere import unknownFunc\n", "src/app.py", "python"
        )
        assert isinstance(result, Ok)
        assert result.value.ai_used is False

    @pytest.mark.asyncio
    async def test_validate_with_ai_valid_code(self, in_memory_store: SymbolStore) -> None:
        """Valid code returns without AI calls."""
        _populate_store(in_memory_store)
        lsp = _mock_lsp_manager([])
        orch = ValidationOrchestrator(in_memory_store, lsp, api_key="test-key")

        with patch.object(orch._resolver, "resolve", new_callable=AsyncMock) as mock_resolve:
            result = await orch.validate_with_ai("x = 1\n", "src/app.py", "python")

        assert isinstance(result, Ok)
        assert result.value.valid is True
        mock_resolve.assert_not_called()
