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

    def test_twelve_tools_registered(self) -> None:
        tmpdir = tempfile.mkdtemp()
        app = create_server(tmpdir)
        # Access the internal tool registry (dict of name -> Tool)
        tool_names = set(app._tool_manager._tools.keys())
        expected = {
            "groundtruth_find_relevant",
            "groundtruth_brief",
            "groundtruth_validate",
            "groundtruth_trace",
            "groundtruth_status",
            "groundtruth_dead_code",
            "groundtruth_unused_packages",
            "groundtruth_hotspots",
            "groundtruth_orient",
            "groundtruth_checkpoint",
            "groundtruth_symbols",
            "groundtruth_context",
        }
        assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"

    def test_creates_db_directory(self) -> None:
        tmpdir = tempfile.mkdtemp()
        create_server(tmpdir)
        db_dir = os.path.join(tmpdir, ".groundtruth")
        assert os.path.isdir(db_dir)
