"""Graph endpoints must import without the MCP SDK's server API."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_endpoints_import_when_the_mcp_server_api_is_unavailable() -> None:
    # Simulate mcp 2.x, where mcp.server.fastmcp no longer exists: any import
    # of it raises. The route map and API impact endpoints must still load.
    probe = textwrap.dedent(
        """
        import importlib.abc, sys

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "mcp.server.fastmcp" or name.startswith("mcp.server.fastmcp."):
                    raise ModuleNotFoundError(name, name=name)
                return None

        sys.meta_path.insert(0, Block())
        from groundtruth.mcp.endpoints.route_map import run_route_map
        from groundtruth.mcp.endpoints import _graph_db
        import groundtruth.mcp as package
        assert "groundtruth.mcp.server" not in sys.modules
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_create_server_is_still_reachable_from_the_package() -> None:
    import groundtruth.mcp as package
    from groundtruth.mcp.server import create_server

    assert package.create_server is create_server
