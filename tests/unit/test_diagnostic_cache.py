"""Tests for LSPClient diagnostic caching."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.protocol import Diagnostic, Position, Range
from tests.conftest import MockStreamReader, make_lsp_message


def _r() -> Range:
    return Range(start=Position(line=0, character=0), end=Position(line=0, character=10))


@pytest.fixture
def client() -> LSPClient:
    return LSPClient(server_command=["fake-server", "--stdio"], root_uri="file:///project")


async def _start_client(client: LSPClient) -> MockStreamReader:
    """Start client with mock process, return the mock stdout."""
    mock_stdout = MockStreamReader()
    mock_stdin = AsyncMock()
    mock_stdin.write = lambda data: None
    mock_stdin.drain = AsyncMock()

    mock_proc = AsyncMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = AsyncMock()
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await client.start()

    return mock_stdout


class TestDiagnosticCache:
    @pytest.mark.asyncio
    async def test_on_publish_diagnostics_stores(self, client: LSPClient) -> None:
        """publishDiagnostics received during drain() is stored in _diagnostics."""
        mock_stdout = await _start_client(client)

        notification = {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": "file:///test.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                        "severity": 1,
                        "code": "reportMissingImports",
                        "source": "Pyright",
                        "message": 'Import "foo" could not be resolved',
                    },
                ],
            },
        }
        mock_stdout.feed_data(make_lsp_message(notification))
        await client.drain(timeout=0.5)

        diags = client._diagnostics.get("file:///test.py", [])
        assert len(diags) == 1
        assert diags[0].code == "reportMissingImports"
        assert "foo" in diags[0].message

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_get_diagnostics_waits_for_event(self, client: LSPClient) -> None:
        """get_diagnostics waits for event then returns cached diagnostics."""
        mock_stdout = await _start_client(client)

        async def feed_diags() -> None:
            await asyncio.sleep(0.05)
            notification = {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": "file:///test.py",
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 1,
                            "message": "Error",
                        },
                    ],
                },
            }
            mock_stdout.feed_data(make_lsp_message(notification))

        asyncio.create_task(feed_diags())
        result = await client.get_diagnostics("file:///test.py", timeout=2.0)
        assert len(result) == 1

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_get_diagnostics_timeout_returns_empty(self, client: LSPClient) -> None:
        """get_diagnostics returns empty list on timeout with no cached data."""
        mock_stdout = await _start_client(client)

        result = await client.get_diagnostics("file:///nonexistent.py", timeout=0.1)
        assert result == []

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_clear_diagnostics(self, client: LSPClient) -> None:
        """clear_diagnostics removes cached state."""
        uri = "file:///test.py"
        client._diagnostics[uri] = [
            Diagnostic(range=_r(), severity=1, message="Error"),
        ]
        client._diagnostic_events[uri] = asyncio.Event()

        client.clear_diagnostics(uri)

        assert uri not in client._diagnostics
        assert uri not in client._diagnostic_events

    @pytest.mark.asyncio
    async def test_multiple_uris_independent(self, client: LSPClient) -> None:
        """Multiple URIs tracked independently when received during drain()."""
        mock_stdout = await _start_client(client)

        for uri, msg in [("file:///a.py", "Error A"), ("file:///b.py", "Error B")]:
            notification = {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 1,
                            "message": msg,
                        },
                    ],
                },
            }
            mock_stdout.feed_data(make_lsp_message(notification))

        await client.drain(timeout=0.5)

        diags_a = client._diagnostics.get("file:///a.py", [])
        diags_b = client._diagnostics.get("file:///b.py", [])
        assert len(diags_a) == 1
        assert len(diags_b) == 1
        assert diags_a[0].message == "Error A"
        assert diags_b[0].message == "Error B"

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_open_and_get_diagnostics(self, client: LSPClient) -> None:
        """open_and_get_diagnostics opens document and returns diagnostics."""
        mock_stdout = await _start_client(client)

        uri = "file:///test.py"

        async def feed_diags() -> None:
            await asyncio.sleep(0.05)
            notification = {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 1,
                            "code": "reportMissingImports",
                            "source": "Pyright",
                            "message": 'Import "foo" could not be resolved',
                        },
                    ],
                },
            }
            mock_stdout.feed_data(make_lsp_message(notification))

        asyncio.create_task(feed_diags())
        result = await client.open_and_get_diagnostics(uri, "python", "import foo\n", timeout=2.0)

        assert len(result) == 1
        assert result[0].code == "reportMissingImports"

        mock_stdout.feed_eof()
        await client.shutdown()
