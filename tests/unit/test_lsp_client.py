"""Tests for the LSP client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from groundtruth.lsp.client import LSPClient
from groundtruth.utils.result import Err, Ok
from tests.conftest import (
    MockStreamReader,
    make_jsonrpc_error,
    make_jsonrpc_response,
    make_lsp_message,
)


@pytest.fixture
def client() -> LSPClient:
    """Create an LSP client for testing."""
    return LSPClient(server_command=["fake-server", "--stdio"], root_uri="file:///project")


class TestWireFormat:
    def test_make_lsp_message(self) -> None:
        body = {"jsonrpc": "2.0", "id": 1, "result": None}
        msg = make_lsp_message(body)
        content = json.dumps(body).encode("utf-8")
        expected = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8") + content
        assert msg == expected


class TestLSPClientStart:
    @pytest.mark.asyncio
    async def test_start_success(self, client: LSPClient) -> None:
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdout = MockStreamReader()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await client.start()

        assert isinstance(result, Ok)
        assert client.is_running

        # Clean up
        mock_proc.stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_start_failure(self, client: LSPClient) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("fake-server not found"),
        ):
            result = await client.start()

        assert isinstance(result, Err)
        assert "fake-server" in result.error.message


class TestLSPClientRequests:
    @pytest.mark.asyncio
    async def test_send_request_and_receive_response(self, client: LSPClient) -> None:
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

        # Feed a response after a small delay
        async def feed_response() -> None:
            await asyncio.sleep(0.05)
            response = make_jsonrpc_response(1, {"capabilities": {}})
            mock_stdout.feed_data(make_lsp_message(response))

        asyncio.create_task(feed_response())
        result = await client.send_request("initialize", {"processId": 1})

        assert isinstance(result, Ok)
        assert result.value == {"capabilities": {}}

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_request_timeout(self, client: LSPClient) -> None:
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

        # Don't feed any response — should timeout
        result = await client.send_request("test/method", timeout=0.1)

        assert isinstance(result, Err)
        assert "timed out" in result.error.message or "closed" in result.error.message

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_jsonrpc_error_response(self, client: LSPClient) -> None:
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

        async def feed_error() -> None:
            await asyncio.sleep(0.05)
            error_resp = make_jsonrpc_error(1, -32601, "Method not found")
            mock_stdout.feed_data(make_lsp_message(error_resp))

        asyncio.create_task(feed_error())
        result = await client.send_request("unknown/method")

        assert isinstance(result, Err)
        assert "Method not found" in result.error.message

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_not_running_returns_err(self, client: LSPClient) -> None:
        result = await client.send_request("test/method")
        assert isinstance(result, Err)
        assert result.error.code == "lsp_not_running"


class TestLSPClientNotifications:
    @pytest.mark.asyncio
    async def test_notification_dispatch(self, client: LSPClient) -> None:
        """publishDiagnostics received during a request is cached in _diagnostics."""
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

        # Feed notification then response so they are processed during send_request
        async def feed_messages() -> None:
            await asyncio.sleep(0.05)
            notification = {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": "file:///test.py", "diagnostics": []},
            }
            mock_stdout.feed_data(make_lsp_message(notification))
            await asyncio.sleep(0.02)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(1, {"capabilities": {}})))

        asyncio.create_task(feed_messages())
        result = await client.send_request("initialize", {"processId": 1})
        assert isinstance(result, Ok)

        assert "file:///test.py" in client._diagnostics
        assert client._diagnostics["file:///test.py"] == []

        mock_stdout.feed_eof()
        await client.shutdown()


class TestLSPClientServerCrash:
    @pytest.mark.asyncio
    async def test_eof_cancels_pending(self, client: LSPClient) -> None:
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

        # Feed EOF after a short delay to simulate crash
        async def crash() -> None:
            await asyncio.sleep(0.05)
            mock_stdout.feed_eof()

        asyncio.create_task(crash())
        result = await client.send_request("test/method", timeout=1.0)

        assert isinstance(result, Err)
        await client.shutdown()


class TestLSPClientConcurrentRequests:
    @pytest.mark.asyncio
    async def test_multiple_concurrent_requests(self, client: LSPClient) -> None:
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

        # With inline read + lock, requests run one after another; feed responses in request order
        async def feed_responses() -> None:
            await asyncio.sleep(0.05)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(1, "result_1")))
            await asyncio.sleep(0.02)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(2, "result_2")))

        asyncio.create_task(feed_responses())

        result1_task = asyncio.create_task(client.send_request("method1"))
        result2_task = asyncio.create_task(client.send_request("method2"))

        result1 = await result1_task
        result2 = await result2_task

        assert isinstance(result1, Ok)
        assert result1.value == "result_1"
        assert isinstance(result2, Ok)
        assert result2.value == "result_2"

        mock_stdout.feed_eof()
        await client.shutdown()


class TestLSPClientServerRequests:
    """Tests for handling server-initiated requests (e.g., window/workDoneProgress/create)."""

    @pytest.mark.asyncio
    async def test_server_request_gets_response(self, client: LSPClient) -> None:
        mock_stdout = MockStreamReader()
        mock_stdin = AsyncMock()
        written_data: list[bytes] = []
        mock_stdin.write = lambda data: written_data.append(data)
        mock_stdin.drain = AsyncMock()

        mock_proc = AsyncMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await client.start()

        # Feed server request then our response so client reads both during send_request
        async def feed_messages() -> None:
            await asyncio.sleep(0.05)
            server_request = {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "window/workDoneProgress/create",
                "params": {"token": "some-token"},
            }
            mock_stdout.feed_data(make_lsp_message(server_request))
            await asyncio.sleep(0.02)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(1, {"capabilities": {}})))

        asyncio.create_task(feed_messages())
        result = await client.send_request("initialize", {"processId": 1})
        assert isinstance(result, Ok)

        # written_data[0] = initialize request; [1] = response to server request (id=99)
        assert len(written_data) >= 2
        body_start = written_data[1].index(b"\r\n\r\n") + 4
        body = json.loads(written_data[1][body_start:])
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 99
        assert body["result"] is None

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_server_request_does_not_block_subsequent_requests(
        self, client: LSPClient
    ) -> None:
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

        # Feed: server request, then a response to our request
        async def feed_messages() -> None:
            await asyncio.sleep(0.05)
            # Server-initiated request
            server_req = {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "client/registerCapability",
                "params": {"registrations": []},
            }
            mock_stdout.feed_data(make_lsp_message(server_req))
            await asyncio.sleep(0.05)
            # Response to our request (id=1)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(1, {"ok": True})))

        asyncio.create_task(feed_messages())
        result = await client.send_request("initialize", {"processId": 1})

        assert isinstance(result, Ok)
        assert result.value == {"ok": True}

        mock_stdout.feed_eof()
        await client.shutdown()


class TestLSPClientStderrSurfacing:
    """A server that dies at launch must surface its exit code + stderr in the client
    error — never a blind 'Request timed out or connection closed'.

    This is the gopls regression (5-lang smoke run 27240730335): `gopls serve -stdio`
    hit an undefined flag, gopls printed usage to stderr and exited(2) BEFORE the LSP
    handshake; the client read EOF on stdout and reported a bare timeout with the
    die-reason sitting unread in the stderr pipe. These tests use a real subprocess
    that reproduces that exact shape (stderr + instant exit). Red before the
    stderr-capture fix, green after.
    """

    @pytest.mark.asyncio
    async def test_dead_server_surfaces_stderr_and_exit_code(self) -> None:
        import sys

        script = (
            "import sys; "
            "sys.stderr.write('flag provided but not defined: -stdio\\n'); "
            "sys.stderr.write('Usage: fake-gopls serve [flags]\\n'); "
            "sys.exit(2)"
        )
        client = LSPClient(
            server_command=[sys.executable, "-c", script],
            root_uri="file:///project",
        )
        start = await client.start()
        assert isinstance(start, Ok)
        result = await client.send_request("initialize", {"processId": 1}, timeout=10.0)
        assert isinstance(result, Err)
        # Either branch (lsp_timeout via stdout-EOF, or lsp_not_running if the death
        # was reaped first) must carry the server's die-reason + exit code.
        assert "flag provided but not defined: -stdio" in result.error.message
        assert "server exited with code 2" in result.error.message
        assert result.error.details is not None
        assert result.error.details["server_returncode"] == 2
        assert "Usage: fake-gopls serve" in str(result.error.details["server_stderr"])
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_stderr_excerpt_keeps_first_lines_bounded(self) -> None:
        import sys

        script = (
            "import sys\n"
            "for i in range(200):\n"
            "    sys.stderr.write(f'line-{i}\\n')\n"
            "sys.exit(3)\n"
        )
        client = LSPClient(
            server_command=[sys.executable, "-c", script],
            root_uri="file:///project",
        )
        await client.start()
        result = await client.send_request("initialize", {}, timeout=10.0)
        assert isinstance(result, Err)
        excerpt = client.stderr_excerpt()
        lines = excerpt.splitlines()
        assert lines[0] == "line-0"  # FIRST lines retained (the die-reason), not last
        assert len(lines) <= 10  # excerpt bounded
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_alive_silent_server_reports_plain_timeout(self) -> None:
        """A server that is alive but slow must NOT gain a bogus exit-code suffix —
        the timeout/death disambiguation must not misclassify genuine timeouts."""
        import sys

        script = "import time; time.sleep(30)"
        client = LSPClient(
            server_command=[sys.executable, "-c", script],
            root_uri="file:///project",
        )
        await client.start()
        result = await client.send_request("initialize", {}, timeout=0.5)
        assert isinstance(result, Err)
        assert "Request timed out or connection closed: initialize" in result.error.message
        assert "server exited with code" not in result.error.message
        await client.shutdown()


class TestLSPClientShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_double_safe(self, client: LSPClient) -> None:
        """Two shutdown calls should not raise."""
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

        mock_stdout.feed_eof()
        await client.shutdown()
        await client.shutdown()  # Second call should be a no-op

    @pytest.mark.asyncio
    async def test_shutdown_no_deadlock(self, client: LSPClient) -> None:
        """Shutdown should not deadlock when request_lock is held."""
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

        # Acquire the lock to simulate a hung request
        async with client._request_lock:
            # Shutdown should still work — it bypasses the lock
            mock_stdout.feed_eof()
            await client.shutdown()


class TestLSPClientHighLevel:
    @pytest.mark.asyncio
    async def test_document_symbol(self, client: LSPClient) -> None:
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

        symbols_data = [
            {
                "name": "myFunc",
                "kind": 12,
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 0}},
                "selectionRange": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 10},
                },
            }
        ]

        async def feed_response() -> None:
            await asyncio.sleep(0.05)
            mock_stdout.feed_data(make_lsp_message(make_jsonrpc_response(1, symbols_data)))

        asyncio.create_task(feed_response())
        result = await client.document_symbol("file:///test.py")

        assert isinstance(result, Ok)
        assert len(result.value) == 1
        assert result.value[0].name == "myFunc"

        mock_stdout.feed_eof()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_did_open(self, client: LSPClient) -> None:
        mock_stdout = MockStreamReader()
        mock_stdin = AsyncMock()
        written_data: list[bytes] = []
        mock_stdin.write = lambda data: written_data.append(data)
        mock_stdin.drain = AsyncMock()

        mock_proc = AsyncMock()
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await client.start()

        await client.did_open("file:///test.py", "python", 1, "x = 1")

        # Verify notification was sent (checking written data)
        assert len(written_data) > 0
        # Parse the written message
        raw = b"".join(written_data)
        body_start = raw.index(b"\r\n\r\n") + 4
        body = json.loads(raw[body_start:])
        assert body["method"] == "textDocument/didOpen"
        assert body["params"]["textDocument"]["languageId"] == "python"

        mock_stdout.feed_eof()
        await client.shutdown()
