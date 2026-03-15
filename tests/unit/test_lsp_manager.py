"""Tests for the LSP manager."""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from groundtruth.lsp.manager import LSPManager
from groundtruth.utils.result import Err, Ok
from tests.conftest import MockStreamReader, make_jsonrpc_response, make_lsp_message

# Use a real temporary directory for Windows compatibility (Path.as_uri needs absolute path)
_TEST_ROOT = tempfile.gettempdir()


def _make_mock_process() -> AsyncMock:
    """Create a mock subprocess for LSP server."""
    mock_stdout = MockStreamReader()
    mock_stdin = AsyncMock()
    mock_stdin.write = lambda data: None
    mock_stdin.drain = AsyncMock()

    mock_proc = AsyncMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = AsyncMock()
    mock_proc.returncode = None
    mock_proc.terminate = lambda: None
    mock_proc.kill = lambda: None

    return mock_proc


class TestEnsureServer:
    @pytest.mark.asyncio
    async def test_starts_correct_command_for_python(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        mock_proc = _make_mock_process()

        async def feed_init_response() -> None:
            await asyncio.sleep(0.05)
            # Response for initialize request
            resp = make_jsonrpc_response(1, {"capabilities": {}})
            mock_proc.stdout.feed_data(make_lsp_message(resp))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            asyncio.create_task(feed_init_response())
            result = await manager.ensure_server(".py")

            assert isinstance(result, Ok)
            # Verify pyright-langserver was called (may be resolved to full path)
            call_args = mock_exec.call_args
            assert call_args is not None and any(
                "pyright-langserver" in str(a) for a in call_args[0]
            )

        mock_proc.stdout.feed_eof()
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_starts_correct_command_for_typescript(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        mock_proc = _make_mock_process()

        async def feed_init_response() -> None:
            await asyncio.sleep(0.05)
            resp = make_jsonrpc_response(1, {"capabilities": {}})
            mock_proc.stdout.feed_data(make_lsp_message(resp))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            asyncio.create_task(feed_init_response())
            result = await manager.ensure_server(".ts")

            assert isinstance(result, Ok)
            call_args = mock_exec.call_args
            assert call_args is not None and any(
                "typescript-language-server" in str(a) for a in call_args[0]
            )

        mock_proc.stdout.feed_eof()
        await manager.shutdown_all()

    @pytest.mark.asyncio
    async def test_unknown_extension_returns_err(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        result = await manager.ensure_server(".xyz")
        assert isinstance(result, Err)
        assert "unsupported_language" in result.error.code


class TestGetClient:
    @pytest.mark.asyncio
    async def test_routes_by_extension(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        mock_proc = _make_mock_process()

        async def feed_init_response() -> None:
            await asyncio.sleep(0.05)
            resp = make_jsonrpc_response(1, {"capabilities": {}})
            mock_proc.stdout.feed_data(make_lsp_message(resp))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            asyncio.create_task(feed_init_response())
            await manager.ensure_server(".py")

        result = manager.get_client("src/main.py")
        assert isinstance(result, Ok)

        mock_proc.stdout.feed_eof()
        await manager.shutdown_all()

    def test_no_server_running_returns_err(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        result = manager.get_client("src/main.py")
        assert isinstance(result, Err)

    def test_unknown_extension_returns_err(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        result = manager.get_client("file.xyz")
        assert isinstance(result, Err)


class TestServerReuse:
    @pytest.mark.asyncio
    async def test_same_extension_reuses_client(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        mock_proc = _make_mock_process()
        call_count = 0

        async def feed_init_responses() -> None:
            nonlocal call_count
            await asyncio.sleep(0.05)
            call_count += 1
            resp = make_jsonrpc_response(1, {"capabilities": {}})
            mock_proc.stdout.feed_data(make_lsp_message(resp))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            asyncio.create_task(feed_init_responses())
            result1 = await manager.ensure_server(".py")
            result2 = await manager.ensure_server(".py")

            assert isinstance(result1, Ok)
            assert isinstance(result2, Ok)
            # Should only have been called once
            assert mock_exec.call_count == 1

        mock_proc.stdout.feed_eof()
        await manager.shutdown_all()


class TestShutdownAllErrorIsolation:
    @pytest.mark.asyncio
    async def test_shutdown_all_error_isolation(self) -> None:
        """If one client fails shutdown, others still get shut down."""
        manager = LSPManager(_TEST_ROOT)

        # Create mock clients
        client1 = AsyncMock()
        client1.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        client1.is_running = True

        client2 = AsyncMock()
        client2.shutdown = AsyncMock()
        client2.is_running = True

        manager._clients = {".py": client1, ".ts": client2}

        # Should not raise despite client1 failing
        await manager.shutdown_all()

        client1.shutdown.assert_called_once()
        client2.shutdown.assert_called_once()
        assert len(manager._clients) == 0


class TestShutdownAll:
    @pytest.mark.asyncio
    async def test_shutdown_all_clears_clients(self) -> None:
        manager = LSPManager(_TEST_ROOT)
        mock_proc = _make_mock_process()

        async def feed_init_response() -> None:
            await asyncio.sleep(0.05)
            resp = make_jsonrpc_response(1, {"capabilities": {}})
            mock_proc.stdout.feed_data(make_lsp_message(resp))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            asyncio.create_task(feed_init_response())
            await manager.ensure_server(".py")

        mock_proc.stdout.feed_eof()
        await manager.shutdown_all()

        # After shutdown, get_client should fail
        result = manager.get_client("main.py")
        assert isinstance(result, Err)
