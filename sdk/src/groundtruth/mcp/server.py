"""Thin MCP transport shim around ``GroundTruth``."""

from __future__ import annotations

# pyright: reportUnusedFunction=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportUnknownMemberType=false

import os
from typing import Literal, cast

from groundtruth.core import GroundTruth
from groundtruth.models import Direction

Transport = Literal["stdio", "sse", "streamable-http"]


def gt_briefing(gt: GroundTruth, symbol: str, family: str = "TARGET", max_results: int = 10) -> str:
    """Direct-callable MCP helper for tests."""
    return gt.briefing(symbol, family=family, max_results=max_results).evidence_text


def gt_check(gt: GroundTruth, path: str, diff: str = "") -> str:
    return gt.check(path, diff=diff or None).summary


def gt_context(
    gt: GroundTruth,
    symbol: str,
    direction: str = "callers",
    scope: str = "",
    depth: int = 2,
) -> str:
    return gt.context(
        symbol,
        direction=cast(Direction, direction),
        scope=scope or None,
        depth=int(depth),
    ).evidence


def serve(db_path: str | None = None, *, transport: Transport = "stdio") -> None:
    """Run skeleton MCP wrapper (requires ``[mcp]`` extra)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError("Install `[mcp]` extra: pip install groundtruth[mcp]") from exc

    resolved = db_path or os.environ.get("GT_DB", "graph.db")
    gt_instance = GroundTruth(resolved)

    server = FastMCP("groundtruth_sdk")

    @server.tool()
    async def sdk_gt_briefing(symbol: str, family: str = "TARGET", max_results: int = 10) -> str:
        return gt_briefing(gt_instance, symbol=symbol, family=family, max_results=max_results)

    @server.tool()
    async def sdk_gt_check(path: str, diff: str = "") -> str:
        return gt_check(gt_instance, path=path, diff=diff)

    @server.tool()
    async def sdk_gt_context(
        symbol: str,
        direction: str = "callers",
        scope: str = "",
        depth: int = 2,
    ) -> str:
        return gt_context(
            gt_instance,
            symbol=symbol,
            direction=direction,
            scope=scope,
            depth=depth,
        )

    server.run(transport=transport)


__all__ = ["serve", "gt_briefing", "gt_check", "gt_context"]
