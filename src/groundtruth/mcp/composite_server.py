"""FastMCP server exposing only the 3 v1.0.5 composite endpoints.

Run as a stdio MCP server alongside the OH agent. Caller passes ``--root``
(the in-container repo root, typically /testbed) and ``--db`` (graph.db
path, typically /tmp/graph.db).

Tools registered:
  gt_lookup(symbol, file_path="")
  gt_impact(target, file_path="")
  gt_check(file_path)

The implementations live in ``groundtruth.mcp.composite``; this module is
just the FastMCP transport wrapper. ``GT_INSTANCE_ID`` env (set by the OH
wrapper before launching this server) is consumed by the budget counter
and pre-submit gate logging path.
"""
from __future__ import annotations

import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP


def create_composite_server(root_path: str, db_path: str) -> FastMCP:
    app = FastMCP(name="groundtruth-composite")

    @app.tool()
    async def gt_lookup(symbol: str, file_path: str = "") -> str:
        """Callers + callees + tests + precedent + type for a symbol (cap=2/task)."""
        from groundtruth.mcp.composite import gt_lookup_impl

        return gt_lookup_impl(
            symbol,
            db_path=db_path,
            root_path=root_path,
            file_path=file_path,
        )

    @app.tool()
    async def gt_impact(target: str, file_path: str = "") -> str:
        """Blast radius + sibling norms for a symbol (cap=2/task)."""
        from groundtruth.mcp.composite import gt_impact_impl

        return gt_impact_impl(
            target,
            db_path=db_path,
            root_path=root_path,
            file_path=file_path,
        )

    @app.tool()
    async def gt_check(file_path: str) -> str:
        """File-level pre-submit check: validators + TEST + import shape (cap=3/task)."""
        from groundtruth.mcp.composite import gt_check_impl

        return gt_check_impl(
            file_path,
            db_path=db_path,
            root_path=root_path,
        )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="GT v1.0.5 composite MCP server")
    parser.add_argument("--root", required=True, help="In-container repo root (e.g. /testbed)")
    parser.add_argument("--db", required=True, help="graph.db path (e.g. /tmp/graph.db)")
    args = parser.parse_args()

    if not os.path.exists(args.root):
        print(f"composite_server: --root not found: {args.root}", file=sys.stderr)
        # Don't fail-hard — endpoints emit [INFO] gracefully
    if not os.path.exists(args.db):
        print(f"composite_server: --db not found: {args.db}", file=sys.stderr)

    app = create_composite_server(args.root, args.db)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
