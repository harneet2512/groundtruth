"""Tests for MCP server creation."""

from __future__ import annotations

import os
import tempfile

from mcp.server.fastmcp import FastMCP

from groundtruth.mcp.server import create_server


class TestCreateServer:
    def test_returns_fastmcp_instance(self) -> None:
        tmpdir = tempfile.mkdtemp()
        app = create_server(tmpdir)
        assert isinstance(app, FastMCP)

    def test_seven_tools_registered(self) -> None:
        tmpdir = tempfile.mkdtemp()
        app = create_server(tmpdir)
        tool_names = set(app._tool_manager._tools.keys())
        expected = {
            "groundtruth_investigate",
            "groundtruth_orient_v2",
            "groundtruth_check_v2",
            "groundtruth_status_v2",
            "gt_plan",
            "gt_run_tests",
            "gt_contract",
        }
        assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"
        assert len(tool_names) == 7, f"Expected 7 tools, got {len(tool_names)}: {tool_names}"

    def test_creates_db_directory(self) -> None:
        tmpdir = tempfile.mkdtemp()
        create_server(tmpdir)
        db_dir = os.path.join(tmpdir, ".groundtruth")
        assert os.path.isdir(db_dir)
