"""
Generic synchronous LSP client for GT edge resolution.

Speaks textDocument/definition to any language server via JSON-RPC 2.0 over stdio.
ZERO language-specific code — all language knowledge lives in servers.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from .servers import get_language_id


class LSPClient:
    """
    Synchronous LSP client. One client per language server.

    Usage:
        client = LSPClient("pyright-langserver", ["--stdio"], "/path/to/repo")
        client.start()
        client.open_file("src/main.py")
        result = client.goto_definition("src/main.py", 10, 4)
        # result: {"file": "src/utils.py", "line": 25} or None
        client.stop()
    """

    def __init__(self, server_cmd: str, server_args: list[str], workspace_root: str) -> None:
        self.workspace_root = os.path.abspath(workspace_root)
        self.server_cmd = server_cmd
        self.server_args = server_args
        self.process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._timeout = 30  # seconds per request

    def start(self) -> None:
        """Start the language server subprocess and send initialize handshake."""
        self.process = subprocess.Popen(
            [self.server_cmd] + self.server_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workspace_root,
        )
        self._initialize()

    def stop(self) -> None:
        """Send shutdown + exit and terminate the server process."""
        if self.process is None:
            return
        try:
            self._send_request("shutdown", {})
            self._send_notification("exit", None)
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None

    def open_file(self, file_path: str) -> None:
        """
        Send textDocument/didOpen notification.

        LSP requires didOpen before definition requests. Sends file content
        so the server can analyze it.
        """
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(
                f"LSP server died (exit code {self.process.returncode})"
            )
        abs_path = os.path.join(self.workspace_root, file_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return

        uri = self._path_to_uri(abs_path)
        language_id = get_language_id(file_path)

        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        })

    def goto_definition(self, file_path: str, line: int, col: int) -> dict[str, Any] | None:
        """
        Send textDocument/definition request.

        Same request for ALL languages: file:line:col in → file:line out.

        Args:
            file_path: Relative path from workspace root.
            line: 0-indexed line number.
            col: 0-indexed column number.

        Returns:
            {"file": "relative/path.py", "line": 25} or None if unresolved.
        """
        abs_path = os.path.join(self.workspace_root, file_path)
        uri = self._path_to_uri(abs_path)

        result = self._send_request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

        if result is None:
            return None

        # Normalize response: can be Location, list[Location], or LocationLink[]
        if isinstance(result, dict):
            locations = [result]
        elif isinstance(result, list):
            locations = result
        else:
            return None

        if not locations:
            return None

        loc = locations[0]
        target_uri = loc.get("uri") or loc.get("targetUri", "")
        target_range = loc.get("range") or loc.get("targetRange", {})
        target_line = target_range.get("start", {}).get("line", 0)

        # Convert URI to relative path
        target_path = self._uri_to_relative_path(target_uri)
        if target_path is None:
            return None

        return {"file": target_path, "line": target_line}

    # ── Private helpers ──────────────────────────────────────────────────

    def _initialize(self) -> None:
        """Send LSP initialize + initialized handshake."""
        root_uri = self._path_to_uri(self.workspace_root)
        result = self._send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "synchronization": {"didOpen": True},
                },
            },
            "workspaceFolders": [
                {"uri": root_uri, "name": os.path.basename(self.workspace_root)},
            ],
        })
        if result is None:
            raise RuntimeError("LSP server did not respond to initialize")
        self._send_notification("initialized", {})
        # Brief pause to let server process initialized notification
        time.sleep(1.0)

    def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and wait for the matching response."""
        self._request_id += 1
        request_id = self._request_id
        msg = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._write_message(msg)

        # Read responses until we get the one matching our ID
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            response = self._read_message(timeout=self._timeout)
            if response is None:
                return None
            # Skip notifications (no "id" field)
            if "id" not in response:
                continue
            if response.get("id") == request_id:
                if "error" in response:
                    return None
                return response.get("result")
        return None

    def _send_notification(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write_message(msg)

    def _write_message(self, message: dict[str, Any]) -> None:
        """Write a Content-Length framed JSON-RPC message to stdin."""
        assert self.process is not None and self.process.stdin is not None
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        data = header + body
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except OSError as e:
            # On Windows, "Invalid argument" can occur if the pipe buffer
            # hasn't been drained. Retry after a brief pause.
            import time as _time
            _time.sleep(0.1)
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _read_message(self, timeout: float = 30.0) -> dict[str, Any] | None:
        """Read one Content-Length framed JSON-RPC message from stdout."""
        assert self.process is not None and self.process.stdout is not None
        stdout = self.process.stdout

        # Read header bytes until we see double newline (\r\n\r\n or \n\n)
        header = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in header and b"\n\n" not in header:
            if time.time() > deadline:
                return None
            byte = stdout.read(1)
            if not byte:
                return None
            header += byte

        # Parse Content-Length (handle both \r\n and \n line endings)
        content_length = 0
        for line in header.decode("ascii", errors="replace").replace("\r\n", "\n").split("\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())

        if content_length == 0:
            return None

        # Read body
        body = stdout.read(content_length)
        if len(body) < content_length:
            return None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _path_to_uri(self, path: str) -> str:
        """Convert a filesystem path to a file:// URI."""
        # Normalize to forward slashes
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            # Windows: C:/foo → /C:/foo
            path = "/" + path
        return "file://" + path

    def _uri_to_relative_path(self, uri: str) -> str | None:
        """Convert a file:// URI to a path relative to workspace_root."""
        if not uri.startswith("file://"):
            return None
        # Strip file:// prefix
        path = uri[7:]
        # URL-decode percent-encoded characters (e.g., %3A → :)
        from urllib.parse import unquote
        path = unquote(path)
        # Handle Windows paths: /C:/foo → C:/foo
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        # Normalize separators
        path = path.replace("\\", "/")
        root = self.workspace_root.replace("\\", "/")
        if not root.endswith("/"):
            root += "/"
        if path.startswith(root):
            return path[len(root):]
        # Try case-insensitive match (Windows)
        if path.lower().startswith(root.lower()):
            return path[len(root):]
        return path
