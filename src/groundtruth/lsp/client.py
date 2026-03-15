"""Universal async LSP client over stdio using JSON-RPC."""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any, cast

from groundtruth.lsp.protocol import (
    Diagnostic,
    DocumentSymbol,
    Hover,
    Location,
    SignatureHelp,
    SymbolInformation,
    TextDocumentIdentifier,
    TextDocumentItem,
)
from groundtruth.utils.logger import get_logger
from groundtruth.utils.platform import resolve_command
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30.0

_TRACE_TRUNCATE_BYTES = 10 * 1024  # 10KB
_TRACE_MAX_FILES = 3


class LSPClient:
    """Async JSON-RPC client for communicating with an LSP server over stdio."""

    def __init__(
        self,
        server_command: list[str],
        root_uri: str,
        trace_path: Path | None = None,
    ) -> None:
        self._server_command = server_command
        self._root_uri = root_uri
        self._process: asyncio.subprocess.Process | None = None
        self._closed = False
        self._request_id = 0
        self._request_lock = asyncio.Lock()
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diagnostic_events: dict[str, asyncio.Event] = {}
        self._started = False
        self._trace_path = trace_path
        # Responses read during drain() so _request() can consume them (avoid dropping e.g. late documentSymbol)
        self._pending_responses: dict[int | str, dict[str, Any]] = {}
        self._progress_tokens: dict[str | int, bool] = {}  # token → completed

    @property
    def is_running(self) -> bool:
        """Whether the LSP server process is running."""
        if not self._started or self._process is None:
            return False
        return self._process.returncode is None

    async def start(self) -> Result[None, GroundTruthError]:
        """Start the LSP server subprocess."""
        try:
            if self._trace_path is not None:
                self._rotate_traces()
            resolved_cmd = resolve_command(self._server_command)
            self._process = await asyncio.create_subprocess_exec(
                *resolved_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._started = True
            return Ok(None)
        except (OSError, FileNotFoundError) as exc:
            return Err(
                GroundTruthError(
                    code="lsp_start_failed",
                    message=f"Failed to start LSP server: {exc}",
                    details={"command": self._server_command},
                )
            )

    async def _read_one_message(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Read one Content-Length framed JSON-RPC message from stdout."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            headers = b""
            while True:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
                if not line:
                    return None
                headers += line
                # Accept either CRLF or LF line endings (Windows LSP servers may send \n only)
                if headers.endswith(b"\r\n\r\n") or headers.endswith(b"\n\n"):
                    break

            content_length = 0
            # Split on either \r\n or \n for header lines
            header_text = headers.decode("ascii")
            for raw_line in header_text.replace("\r\n", "\n").split("\n"):
                header = raw_line.strip()
                if header.lower().startswith("content-length:"):
                    content_length = int(header.split(":")[1].strip())
                    break

            if content_length == 0:
                return None

            body = await asyncio.wait_for(
                self._process.stdout.readexactly(content_length),
                timeout=timeout,
            )
            parsed = cast(dict[str, Any], json.loads(body))
            self._trace_log("recv", parsed)
            return parsed
        except asyncio.TimeoutError:
            return None
        except (asyncio.IncompleteReadError, json.JSONDecodeError) as e:
            logger.warning("LSP read error: %s", e)
            return None

    def _trace_log(self, direction: str, message: dict[str, Any]) -> None:
        """Write a trace entry to the JSONL trace file."""
        if self._trace_path is None:
            return
        try:
            serialized = json.dumps(message)
            if len(serialized) > _TRACE_TRUNCATE_BYTES:
                serialized = serialized[:_TRACE_TRUNCATE_BYTES] + "...(truncated)"
            entry = json.dumps(
                {
                    "direction": direction,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "message": json.loads(serialized)
                    if len(serialized) <= _TRACE_TRUNCATE_BYTES
                    else serialized,
                }
            )
            with open(self._trace_path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def _rotate_traces(self) -> None:
        """Keep only the last N trace files in the trace directory."""
        if self._trace_path is None:
            return
        parent = self._trace_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            return
        trace_files = sorted(parent.glob("lsp-trace-*.jsonl"), key=lambda p: p.stat().st_mtime)
        while len(trace_files) >= _TRACE_MAX_FILES:
            oldest = trace_files.pop(0)
            try:
                oldest.unlink()
            except OSError:
                pass

    async def probe_ready(self, timeout: float = 5.0, interval: float = 1.0) -> bool:
        """Probe LSP server readiness by sending workspace/symbol queries.

        Returns True on any response (success or error = server alive).
        Returns False on full timeout (server never responded).
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            result = await self.send_request(
                "workspace/symbol", {"query": ""}, timeout=min(interval, deadline - loop.time())
            )
            if isinstance(result, Ok):
                return True
            # LSP error response still means server is alive
            if isinstance(result, Err) and result.error.code == "lsp_error":
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))
        return False

    async def _send_response(self, msg_id: int | str, result: object = None) -> None:
        """Send a response to a server-initiated request."""
        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        await self._write_message(response)

    def _handle_progress(self, params: dict[str, Any]) -> None:
        """Handle $/progress notification (track work done tokens)."""
        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind")
        if token is not None:
            if kind == "begin":
                self._progress_tokens[token] = False
            elif kind == "end":
                self._progress_tokens[token] = True

    async def wait_for_progress_complete(self, timeout: float = 60.0) -> bool:
        """Wait until all $/progress tokens reach 'end', or timeout.

        Returns True when all registered tokens are complete (or 5s with zero tokens).
        Returns False on full timeout.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        no_token_deadline = loop.time() + 5.0

        async with self._request_lock:
            while loop.time() < deadline:
                # If we have tokens and all are complete, we're done
                if self._progress_tokens and all(self._progress_tokens.values()):
                    return True
                # If no tokens registered and past the short deadline, proceed
                if not self._progress_tokens and loop.time() >= no_token_deadline:
                    return True

                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                msg = await self._read_one_message(timeout=min(0.5, remaining))
                if msg is None:
                    continue

                resp_method = msg.get("method")
                resp_id = msg.get("id")

                # Response — queue for _request()
                if (
                    resp_id is not None
                    and resp_method is None
                    and ("result" in msg or "error" in msg)
                ):
                    self._pending_responses[resp_id] = msg
                    continue

                # Server-initiated request
                if resp_method is not None and resp_id is not None:
                    # Handle workDoneProgress/create: register token
                    if resp_method == "window/workDoneProgress/create":
                        req_params = msg.get("params", {})
                        token = req_params.get("token")
                        if token is not None and token not in self._progress_tokens:
                            self._progress_tokens[token] = False
                    await self._send_response(resp_id)
                    continue

                # Notifications
                if resp_method is not None and resp_id is None:
                    if resp_method == "$/progress":
                        self._handle_progress(msg.get("params") or {})
                    elif resp_method == "textDocument/publishDiagnostics":
                        self._handle_diagnostics(msg.get("params") or {})

        return False

    def _handle_diagnostics(self, params: dict[str, Any]) -> None:
        """Handle textDocument/publishDiagnostics notification (cache + signal event)."""
        if not params:
            return
        uri = params.get("uri", "")
        raw_diags = params.get("diagnostics", [])
        self._diagnostics[uri] = [Diagnostic.model_validate(d) for d in raw_diags]
        event = self._diagnostic_events.get(uri)
        if event is not None:
            event.set()

    async def _request(
        self, method: str, params: dict[str, Any] | None, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        """Send a request and wait for the matching response.

        While waiting, handles notifications (skip / handle diagnostics) and
        server-initiated requests (respond with null). No background read loop.
        """
        self._request_id += 1
        request_id = self._request_id
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params if params is not None else {},
        }
        await self._write_message(msg)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            # Consume any response that arrived during a previous drain (e.g. late documentSymbol)
            response = self._pending_responses.pop(request_id, None)
            if response is not None:
                if "error" in response:
                    logger.warning("LSP error for %s: %s", method, response["error"])
                return response

            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("Request timed out after %.1fs: %s", timeout, method)
                return None

            await asyncio.sleep(0)  # Yield so subprocess pipe can deliver (Windows)
            response = await self._read_one_message(timeout=remaining)
            if response is None:
                return None

            resp_id = response.get("id")
            resp_method = response.get("method")

            # Server-initiated request — respond immediately
            if resp_method is not None and resp_id is not None:
                # Handle workDoneProgress/create: register token
                if resp_method == "window/workDoneProgress/create":
                    req_params = response.get("params", {})
                    token = req_params.get("token")
                    if token is not None and token not in self._progress_tokens:
                        self._progress_tokens[token] = False
                await self._send_response(resp_id)
                continue

            # Notification — skip or handle diagnostics/progress
            if resp_method is not None and resp_id is None:
                if resp_method == "textDocument/publishDiagnostics":
                    self._handle_diagnostics(response.get("params") or {})
                elif resp_method == "$/progress":
                    self._handle_progress(response.get("params") or {})
                continue

            # Response — check if it's ours
            if resp_id is not None:
                try:
                    rid = int(resp_id) if isinstance(resp_id, (int, float)) else int(str(resp_id))
                except (TypeError, ValueError):
                    rid = -1
                if rid == request_id:
                    if "error" in response:
                        logger.warning("LSP error for %s: %s", method, response["error"])
                    return response

            logger.warning("Unexpected response id=%s (waiting for %s)", resp_id, request_id)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write_message(msg)

    async def _write_message(self, body: dict[str, Any]) -> None:
        """Write a JSON-RPC message with Content-Length header."""
        assert self._process is not None
        assert self._process.stdin is not None
        self._trace_log("send", body)
        content = json.dumps(body)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        data = header.encode("utf-8") + content_bytes
        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except BrokenPipeError:
            logger.warning("lsp_broken_pipe", msg="stdin write failed — server likely crashed")

    async def drain(self, timeout: float = 2.0) -> None:
        """Read and process messages for up to timeout seconds (e.g. after initialize or didOpen)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        async with self._request_lock:
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                msg = await self._read_one_message(timeout=min(0.5, remaining))
                if msg is None:
                    continue
                resp_method = msg.get("method")
                resp_id = msg.get("id")
                # Response (id + result/error, no method): don't drop — queue for _request()
                if (
                    resp_id is not None
                    and resp_method is None
                    and ("result" in msg or "error" in msg)
                ):
                    self._pending_responses[resp_id] = msg
                    continue
                if resp_method is not None and resp_id is not None:
                    # Handle workDoneProgress/create: register token
                    if resp_method == "window/workDoneProgress/create":
                        req_params = msg.get("params", {})
                        token = req_params.get("token")
                        if token is not None and token not in self._progress_tokens:
                            self._progress_tokens[token] = False
                    await self._send_response(resp_id)
                elif resp_method == "textDocument/publishDiagnostics":
                    self._handle_diagnostics(msg.get("params") or {})
                elif resp_method == "$/progress":
                    self._handle_progress(msg.get("params") or {})

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Result[Any, GroundTruthError]:
        """Send a JSON-RPC request and wait for the response."""
        if not self.is_running:
            return Err(
                GroundTruthError(code="lsp_not_running", message="LSP server is not running")
            )

        async with self._request_lock:
            response = await self._request(method, params, timeout=timeout)

        if response is None:
            return Err(
                GroundTruthError(
                    code="lsp_timeout",
                    message=f"Request timed out or connection closed: {method}",
                )
            )
        if "error" in response:
            err = response["error"]
            code = err.get("code", -1)
            message = err.get("message", "Unknown error")
            return Err(
                GroundTruthError(
                    code="lsp_error",
                    message=f"LSP error ({code}): {message}",
                )
            )
        return Ok(response.get("result"))

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Result[None, GroundTruthError]:
        """Send a JSON-RPC notification (no response expected)."""
        if not self.is_running:
            return Err(
                GroundTruthError(code="lsp_not_running", message="LSP server is not running")
            )
        await self._notify(method, params)
        return Ok(None)

    # --- High-level LSP methods ---

    async def document_symbol(
        self, uri: str, timeout: float = DEFAULT_TIMEOUT
    ) -> Result[list[DocumentSymbol], GroundTruthError]:
        """Request document symbols for a file."""
        result = await self.send_request(
            "textDocument/documentSymbol", {"textDocument": {"uri": uri}}, timeout=timeout
        )
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok([])
        symbols = [DocumentSymbol.model_validate(s) for s in raw]
        return Ok(symbols)

    async def workspace_symbol(
        self, query: str = "", timeout: float = DEFAULT_TIMEOUT
    ) -> Result[list[SymbolInformation], GroundTruthError]:
        """Request workspace symbols matching a query."""
        result = await self.send_request("workspace/symbol", {"query": query}, timeout=timeout)
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok([])
        symbols = [SymbolInformation.model_validate(s) for s in raw]
        return Ok(symbols)

    async def references(
        self,
        uri: str,
        line: int,
        character: int,
        include_declaration: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Result[list[Location], GroundTruthError]:
        """Find all references to a symbol at the given position."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }
        result = await self.send_request("textDocument/references", params, timeout=timeout)
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok([])
        locations = [Location.model_validate(loc) for loc in raw]
        return Ok(locations)

    async def hover(
        self, uri: str, line: int, character: int, timeout: float = DEFAULT_TIMEOUT
    ) -> Result[Hover | None, GroundTruthError]:
        """Get hover information for a symbol at the given position."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        result = await self.send_request("textDocument/hover", params, timeout=timeout)
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok(None)
        return Ok(Hover.model_validate(raw))

    async def definition(
        self, uri: str, line: int, character: int
    ) -> Result[list[Location], GroundTruthError]:
        """Go to definition for a symbol at the given position."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        result = await self.send_request("textDocument/definition", params)
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok([])
        if isinstance(raw, dict):
            return Ok([Location.model_validate(raw)])
        locations = [Location.model_validate(loc) for loc in raw]
        return Ok(locations)

    async def did_open(self, uri: str, language_id: str, version: int, text: str) -> None:
        """Notify the server that a document was opened."""
        item = TextDocumentItem(uri=uri, language_id=language_id, version=version, text=text)
        await self.send_notification(
            "textDocument/didOpen",
            {"textDocument": item.model_dump(by_alias=True)},
        )

    async def did_change(self, uri: str, version: int, text: str) -> None:
        """Notify the server of a full document change."""
        await self.send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )

    async def did_close(self, uri: str) -> None:
        """Notify the server that a document was closed."""
        doc = TextDocumentIdentifier(uri=uri)
        await self.send_notification(
            "textDocument/didClose",
            {"textDocument": doc.model_dump(by_alias=True)},
        )

    async def get_diagnostics(self, uri: str, timeout: float = 5.0) -> list[Diagnostic]:
        """Read messages until diagnostics for uri arrive or timeout. Returns cached on timeout."""
        if uri not in self._diagnostic_events:
            self._diagnostic_events[uri] = asyncio.Event()

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        async with self._request_lock:
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                msg = await self._read_one_message(timeout=min(0.5, remaining))
                if msg is None:
                    break
                resp_method = msg.get("method")
                resp_id = msg.get("id")
                if (
                    resp_id is not None
                    and resp_method is None
                    and ("result" in msg or "error" in msg)
                ):
                    self._pending_responses[resp_id] = msg
                    continue
                if resp_method and resp_id is not None:
                    if resp_method == "window/workDoneProgress/create":
                        req_params = msg.get("params", {})
                        token = req_params.get("token")
                        if token is not None and token not in self._progress_tokens:
                            self._progress_tokens[token] = False
                    await self._send_response(resp_id)
                    continue
                if resp_method == "textDocument/publishDiagnostics":
                    self._handle_diagnostics(msg.get("params") or {})
                    params = msg.get("params") or {}
                    if params.get("uri") == uri:
                        return self._diagnostics.get(uri, [])
                elif resp_method == "$/progress":
                    self._handle_progress(msg.get("params") or {})

        return self._diagnostics.get(uri, [])

    async def open_and_get_diagnostics(
        self, uri: str, language_id: str, text: str, timeout: float = 5.0
    ) -> list[Diagnostic]:
        """Open a document and wait for diagnostics."""
        self._diagnostic_events[uri] = asyncio.Event()
        self._diagnostics.pop(uri, None)
        await self.did_open(uri, language_id, 1, text)
        return await self.get_diagnostics(uri, timeout)

    async def signature_help(
        self, uri: str, line: int, character: int
    ) -> Result[SignatureHelp | None, GroundTruthError]:
        """Get signature help at the given position."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        result = await self.send_request("textDocument/signatureHelp", params)
        if isinstance(result, Err):
            return result
        raw = result.value
        if raw is None:
            return Ok(None)
        return Ok(SignatureHelp.model_validate(raw))

    def clear_diagnostics(self, uri: str) -> None:
        """Remove cached diagnostics and event for a URI."""
        self._diagnostics.pop(uri, None)
        self._diagnostic_events.pop(uri, None)

    async def shutdown(self) -> None:
        """Shut down the LSP server gracefully.

        Bypasses _request_lock intentionally — this is a terminal operation
        and the lock may be held by a hung request.
        """
        if self._closed:
            return
        self._closed = True

        proc = self._process
        self._process = None
        self._started = False

        if proc is None or proc.returncode is not None:
            return

        # Write raw shutdown/exit JSON-RPC directly to stdin (bypass _request_lock)
        try:
            if proc.stdin is not None:
                shutdown_msg = json.dumps(
                    {"jsonrpc": "2.0", "id": 999999, "method": "shutdown", "params": {}}
                ).encode("utf-8")
                header = f"Content-Length: {len(shutdown_msg)}\r\n\r\n".encode("utf-8")
                proc.stdin.write(header + shutdown_msg)

                exit_msg = json.dumps({"jsonrpc": "2.0", "method": "exit"}).encode("utf-8")
                header2 = f"Content-Length: {len(exit_msg)}\r\n\r\n".encode("utf-8")
                proc.stdin.write(header2 + exit_msg)
                await proc.stdin.drain()
        except (BrokenPipeError, OSError, ConnectionResetError):
            pass

        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        except (OSError, ProcessLookupError):
            pass
