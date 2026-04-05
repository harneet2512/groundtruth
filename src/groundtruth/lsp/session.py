"""
LSP session manager for GT.

Manages running LSP servers across multiple queries within a task.
Starts servers lazily (on first file touch), keeps them alive for reuse,
shuts down on task completion.
"""

from __future__ import annotations

import shutil
from typing import Any

from .servers import LSP_SERVERS, get_language_id
from .sync_client import LSPClient


class LSPSession:
    """
    Manages running LSP servers for a workspace.
    One server per language, started on demand, reused across queries.

    Usage:
        session = LSPSession("/path/to/repo")
        client = session.get_client("src/main.py")  # starts pyright if needed
        if client:
            client.open_file("src/main.py")
            result = client.goto_definition("src/main.py", 10, 4)
        session.shutdown()
    """

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self._clients: dict[str, LSPClient | None] = {}
        self._opened_files: set[str] = set()

    def get_client(self, file_path: str) -> LSPClient | None:
        """
        Get or create an LSP client for this file's language.
        Returns None if no server is available (not installed or not configured).
        """
        ext = _get_ext(file_path)
        if ext in self._clients:
            return self._clients[ext]

        config = LSP_SERVERS.get(ext)
        if config is None:
            self._clients[ext] = None
            return None

        cmd = str(config["cmd"])
        if not shutil.which(cmd):
            self._clients[ext] = None
            return None

        args = list(config.get("args", []))  # type: ignore[arg-type]
        try:
            client = LSPClient(cmd, args, self.workspace_root)
            client.start()
            self._clients[ext] = client
            return client
        except Exception:
            self._clients[ext] = None
            return None

    def ensure_file_open(self, file_path: str) -> None:
        """Open file in LSP server if not already opened."""
        if file_path in self._opened_files:
            return
        client = self.get_client(file_path)
        if client is None:
            return
        try:
            client.open_file(file_path)
            self._opened_files.add(file_path)
        except Exception:
            pass

    def shutdown(self) -> None:
        """Stop all running LSP servers."""
        for client in self._clients.values():
            if client is not None:
                try:
                    client.stop()
                except Exception:
                    pass
        self._clients.clear()
        self._opened_files.clear()


def _get_ext(file_path: str) -> str:
    """Extract file extension (lowercase, with dot)."""
    import os
    _, ext = os.path.splitext(file_path)
    return ext.lower()
