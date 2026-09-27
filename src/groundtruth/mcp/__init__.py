"""MCP server and tool definitions.

``create_server`` is resolved lazily. Importing any submodule - the graph
endpoints under ``groundtruth.mcp.endpoints`` most of all - runs this file
first, and an eager ``from groundtruth.mcp.server import create_server`` made
every endpoint import depend on the MCP SDK's server API. That API moved in
mcp 2.x (``mcp.server.fastmcp`` became ``mcp.server.mcpserver``), so an
environment that pins mcp 2 for another tool could not even import the
route map, although no endpoint touches the server.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_server"]


def __getattr__(name: str) -> Any:
    if name == "create_server":
        from groundtruth.mcp.server import create_server

        return create_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
