"""Shared test fixtures."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from groundtruth.index.store import SymbolStore


def make_lsp_message(body: dict[str, Any]) -> bytes:
    """Frame a JSON-RPC body as an LSP wire-format message."""
    content = json.dumps(body).encode("utf-8")
    header = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8")
    return header + content


def make_jsonrpc_response(request_id: int, result: Any = None) -> dict[str, Any]:
    """Create a JSON-RPC response dict."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_jsonrpc_error(
    request_id: int, code: int = -32600, message: str = "Error"
) -> dict[str, Any]:
    """Create a JSON-RPC error response dict."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class MockStreamReader:
    """A mock asyncio.StreamReader that serves pre-loaded LSP messages."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._eof = False

    def feed_data(self, data: bytes) -> None:
        """Add data to the buffer."""
        self._buffer.extend(data)

    def feed_eof(self) -> None:
        """Signal end of stream."""
        self._eof = True

    async def readline(self) -> bytes:
        """Read a line from the buffer."""
        while b"\n" not in self._buffer:
            if self._eof:
                # Return remaining data or empty
                data = bytes(self._buffer)
                self._buffer.clear()
                return data
            await asyncio.sleep(0.001)

        idx = self._buffer.index(b"\n")
        line = bytes(self._buffer[: idx + 1])
        del self._buffer[: idx + 1]
        return line

    async def readexactly(self, n: int) -> bytes:
        """Read exactly n bytes."""
        while len(self._buffer) < n:
            if self._eof:
                raise asyncio.IncompleteReadError(bytes(self._buffer), n)
            await asyncio.sleep(0.001)

        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data


@pytest.fixture
def mock_subprocess() -> tuple[AsyncMock, MockStreamReader, MagicMock]:
    """Create a mock subprocess with controllable stdin/stdout."""
    mock_stdout = MockStreamReader()

    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()

    mock_proc = AsyncMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = AsyncMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    return mock_proc, mock_stdout, mock_stdin


@pytest.fixture
def in_memory_store() -> SymbolStore:
    """Create an in-memory SymbolStore with initialized schema."""
    store = SymbolStore(":memory:")
    store.initialize()
    return store
