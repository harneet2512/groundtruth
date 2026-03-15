"""LSP server lifecycle management."""

from __future__ import annotations

import os
from pathlib import Path

from typing import Any

from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.config import get_language_id, get_server_config
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

logger = get_logger(__name__)


class LSPManager:
    """Manages LSP server processes keyed by file extension."""

    def __init__(self, root_path: str, trace_dir: Path | None = None) -> None:
        self._root_path = root_path
        self._root_uri = Path(root_path).as_uri()
        self._clients: dict[str, LSPClient] = {}
        self._trace_dir = trace_dir
        self._crash_counts: dict[str, int] = {}

    async def ensure_server(self, ext: str) -> Result[LSPClient, GroundTruthError]:
        """Get or start an LSP server for the given file extension.

        Checks if cached client is still alive; if dead, auto-restarts.
        """
        if ext in self._clients:
            if self._clients[ext].is_running:
                return Ok(self._clients[ext])
            # Client died — remove and restart
            logger.warning("lsp_client_dead", ext=ext, msg="Cached client is dead, restarting")
            del self._clients[ext]

        config_result = get_server_config(ext)
        if isinstance(config_result, Err):
            return config_result

        config = config_result.value
        trace_path = (
            self._trace_dir / f"lsp-trace-{ext.lstrip('.')}.jsonl"
            if self._trace_dir is not None
            else None
        )
        client = LSPClient(
            server_command=config.command,
            root_uri=self._root_uri,
            trace_path=trace_path,
        )

        start_result = await client.start()
        if isinstance(start_result, Err):
            return Err(start_result.error)

        init_result = await self._initialize_client(client, ext, config.initialization_options)
        if isinstance(init_result, Err):
            await client.shutdown()
            return Err(init_result.error)

        self._clients[ext] = client
        return Ok(client)

    async def _initialize_client(
        self,
        client: LSPClient,
        ext: str,
        initialization_options: dict[str, Any] | None = None,
    ) -> Result[None, GroundTruthError]:
        """Send initialize/initialized handshake."""
        params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "hover": {
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    "publishDiagnostics": {"relatedInformation": True},
                    "signatureHelp": {
                        "signatureInformation": {
                            "parameterInformation": {"labelOffsetSupport": True},
                        },
                    },
                    "references": {},
                    "definition": {},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "symbol": {
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
            },
            "workspaceFolders": [
                {"uri": self._root_uri, "name": os.path.basename(self._root_path)},
            ],
        }
        if initialization_options is not None:
            params["initializationOptions"] = initialization_options

        result = await client.send_request("initialize", params)
        if isinstance(result, Err):
            return Err(result.error)

        notify_result = await client.send_notification("initialized", {})
        if isinstance(notify_result, Err):
            return Err(notify_result.error)

        # Drain startup burst (notifications, server requests). No probe_ready — avoids extra request and keeps _request_id predictable.
        await client.drain(timeout=2.0)

        # Wait for language server to finish initial project analysis (e.g. pyright background indexing).
        # This ensures subsequent queries hit the server's cache instead of triggering on-demand analysis.
        ready = await client.wait_for_progress_complete(timeout=120.0)
        if not ready:
            logger.warning(
                "lsp_progress_timeout",
                ext=ext,
                msg="Progress did not complete within 120s, proceeding anyway",
            )

        return Ok(None)

    def get_client(self, file_path: str) -> Result[LSPClient, GroundTruthError]:
        """Get the LSP client for a given file path, routed by extension."""
        ext = os.path.splitext(file_path)[1]
        lang_result = get_language_id(ext)
        if isinstance(lang_result, Err):
            return Err(lang_result.error)

        client = self._clients.get(ext)
        if client is None or not client.is_running:
            return Err(
                GroundTruthError(
                    code="lsp_not_started",
                    message=f"No LSP server running for {ext}. Call ensure_server() first.",
                )
            )
        return Ok(client)

    async def shutdown_all(self) -> None:
        """Shut down all managed LSP servers (error-isolated)."""
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.shutdown()
            except Exception:
                pass

    async def restart_server(self, ext: str) -> Result[LSPClient, GroundTruthError]:
        """Restart the LSP server for an extension."""
        if ext in self._clients:
            await self._clients[ext].shutdown()
            del self._clients[ext]
        return await self.ensure_server(ext)
