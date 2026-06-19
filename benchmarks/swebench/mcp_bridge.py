"""Real MCP client for SWE-bench: spawns GroundTruth MCP server per task, records proof."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Substantive tools (proof requires at least one successful substantive call)
SUBSTANTIVE_TOOLS = frozenset({
    "groundtruth_find_relevant",
    "groundtruth_brief",
    "groundtruth_validate",
    "groundtruth_trace",
    "groundtruth_orient",
    "groundtruth_explain",
    "groundtruth_impact",
    "groundtruth_patterns",
    "groundtruth_symbols",
    "groundtruth_context",
    "groundtruth_dead_code",
    "groundtruth_unused_packages",
    "groundtruth_hotspots",
})


def _repo_root() -> Path:
    """Project root (GroundTruth repo)."""
    return Path(__file__).resolve().parent.parent.parent


class MCPProof:
    """Evidence that the run actually used the MCP server."""

    __slots__ = (
        "mcp_enabled", "connection_ok", "tools_discovered", "tool_calls",
        "successful_tool_calls", "failed_tool_calls", "substantive_tool_count",
        "valid", "invalid_run_reason", "mcp_server_command", "mcp_server_root",
        "worker_id", "shard_id", "model_name_exact",
    )

    def __init__(
        self,
        mcp_enabled: bool = True,
        connection_ok: bool = False,
        tools_discovered: list[str] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        successful_tool_calls: int = 0,
        failed_tool_calls: int = 0,
        substantive_tool_count: int = 0,
        valid: bool = False,
        invalid_run_reason: str | None = None,
        mcp_server_command: str = "",
        mcp_server_root: str = "",
        worker_id: int = 0,
        shard_id: int = 0,
        model_name_exact: str = "",
    ):
        self.mcp_enabled = mcp_enabled
        self.connection_ok = connection_ok
        self.tools_discovered = tools_discovered or []
        self.tool_calls = tool_calls or []
        self.successful_tool_calls = successful_tool_calls
        self.failed_tool_calls = failed_tool_calls
        self.substantive_tool_count = substantive_tool_count
        self.valid = valid
        self.invalid_run_reason = invalid_run_reason
        self.mcp_server_command = mcp_server_command
        self.mcp_server_root = mcp_server_root
        self.worker_id = worker_id
        self.shard_id = shard_id
        self.model_name_exact = model_name_exact

    def to_dict(self) -> dict[str, Any]:
        return {
            "mcp_enabled": self.mcp_enabled,
            "connection_ok": self.connection_ok,
            "tools_discovered": self.tools_discovered,
            "tools_called": [t.get("name") for t in self.tool_calls],
            "successful_tool_calls": self.successful_tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "substantive_tool_count": self.substantive_tool_count,
            "valid": self.valid,
            "invalid_run_reason": self.invalid_run_reason,
            "mcp_server_command": self.mcp_server_command,
            "mcp_server_root": self.mcp_server_root,
            "worker_id": self.worker_id,
            "shard_id": self.shard_id,
            "model_name_exact": self.model_name_exact,
        }


class MCPBridge:
    """
    Connects to a GroundTruth MCP server over stdio.
    Spawns the server with --root <repo_path> (server auto-indexes if no db).
    Records every tool call and produces MCPProof on shutdown.
    """

    def __init__(
        self,
        repo_path: str,
        *,
        db_path: str | None = None,
        no_auto_index: bool = False,
        project_root: Path | None = None,
        worker_id: int = 0,
        shard_id: int = 0,
        model_name_exact: str = "",
        proof_output_dir: str | None = None,
        instance_id: str = "",
    ):
        self.repo_path = os.path.abspath(repo_path)
        # Default to a real file path so auto-index persists and serve_cmd
        # doesn't endlessly re-index into a throwaway :memory: DB.
        self.db_path = db_path or os.path.join(self.repo_path, ".groundtruth", "index.db")
        self.no_auto_index = no_auto_index
        self.project_root = project_root or _repo_root()
        self.worker_id = worker_id
        self.shard_id = shard_id
        self.model_name_exact = model_name_exact
        self.proof_output_dir = proof_output_dir
        self.instance_id = instance_id

        self._session = None
        self._stdio_context = None
        self._server_process = None
        self._tool_calls_log: list[dict[str, Any]] = []
        self._proof = MCPProof(
            mcp_enabled=True,
            connection_ok=False,
            mcp_server_command=self._server_command(),
            mcp_server_root=self.repo_path,
            worker_id=worker_id,
            shard_id=shard_id,
            model_name_exact=model_name_exact,
        )

    def _server_command(self) -> str:
        parts = [
            sys.executable, "-m", "groundtruth.main", "serve",
            "--root", self.repo_path,
            "--db", self.db_path,
        ]
        if self.no_auto_index:
            parts.append("--no-auto-index")
        return " ".join(parts)

    async def connect(self) -> bool:
        """Spawn MCP server and establish session. Returns True on success."""
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            command = sys.executable
            args = [
                "-m", "groundtruth.main", "serve",
                "--root", self.repo_path,
                "--db", self.db_path,
            ]
            if self.no_auto_index:
                args.append("--no-auto-index")

            server_params = StdioServerParameters(
                command=command,
                args=args,
                cwd=str(self.project_root),
            )
            self._stdio_context = stdio_client(server_params)
            read_stream, write_stream = await self._stdio_context.__aenter__()
            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            await self._session.initialize()

            self._proof.connection_ok = True
            tools_result = await self._session.list_tools()
            if hasattr(tools_result, "tools") and tools_result.tools:
                self._proof.tools_discovered = [t.name for t in tools_result.tools]
            else:
                self._proof.tools_discovered = []
            logger.info(
                "MCP connected to %s, tools=%d",
                self.repo_path, len(self._proof.tools_discovered),
            )
            return True
        except Exception as e:
            logger.exception("MCP connect failed: %s", e)
            self._proof.connection_ok = False
            self._proof.invalid_run_reason = str(e)
            return False

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a GroundTruth tool by name. Returns JSON string result."""
        if self._session is None:
            return json.dumps({"error": "MCP not connected"})

        t0 = time.monotonic()
        success = False
        try:
            result = await self._session.call_tool(tool_name, arguments)
            success = not getattr(result, "isError", True)
            content = getattr(result, "content", []) or []
            if isinstance(content, list) and content:
                parts = [
                    getattr(p, "text", str(p))
                    for p in content
                    if hasattr(p, "text")
                ]
                text = "".join(parts) if parts else "{}"
            else:
                text = "{}"
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._tool_calls_log.append({
                "name": tool_name,
                "success": success,
                "latency_ms": round(elapsed_ms, 2),
                "args_hash": str(hash(json.dumps(arguments, sort_keys=True))),
            })
            if success:
                self._proof.successful_tool_calls += 1
                if tool_name in SUBSTANTIVE_TOOLS:
                    self._proof.substantive_tool_count += 1
            else:
                self._proof.failed_tool_calls += 1
            return text
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._tool_calls_log.append({
                "name": tool_name,
                "success": False,
                "latency_ms": round(elapsed_ms, 2),
                "error": str(e),
            })
            self._proof.failed_tool_calls += 1
            logger.exception("MCP tool %s failed", tool_name)
            return json.dumps({"error": str(e)})

    async def shutdown(self) -> None:
        """Close session, write proof artifacts, tear down server."""
        self._proof.tool_calls = self._tool_calls_log
        self._proof.valid = (
            self._proof.connection_ok and self._proof.substantive_tool_count >= 1
        )
        if self._proof.connection_ok and self._proof.substantive_tool_count == 0:
            self._proof.invalid_run_reason = "no_substantive_tool_calls"

        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._stdio_context is not None:
            try:
                await self._stdio_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._stdio_context = None

        if self.proof_output_dir:
            self._write_proof_artifacts()

    def _write_proof_artifacts(self) -> None:
        """Write mcp_usage.json and tool_calls.jsonl to proof_output_dir."""
        out = Path(self.proof_output_dir)
        if self.instance_id:
            out = out / self.instance_id.replace("/", "_").replace(" ", "_")
        out.mkdir(parents=True, exist_ok=True)
        (out / "mcp_usage.json").write_text(
            json.dumps(self._proof.to_dict(), indent=2),
            encoding="utf-8",
        )
        with open(out / "tool_calls.jsonl", "w", encoding="utf-8") as f:
            for entry in self._tool_calls_log:
                f.write(json.dumps(entry) + "\n")
        metadata = {
            "worker_id": self.worker_id,
            "shard_id": self.shard_id,
            "condition": "with_groundtruth_mcp",
            "model_name_exact": self.model_name_exact,
            "mcp_server_command": self._proof.mcp_server_command,
            "mcp_server_root": self._proof.mcp_server_root,
            "valid": self._proof.valid,
        }
        (out / "metadata.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )

    def get_proof(self) -> MCPProof:
        """Return the proof after shutdown."""
        return self._proof
