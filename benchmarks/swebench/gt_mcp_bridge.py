#!/usr/bin/env python3
"""
GroundTruth MCP Bridge — gt_check only.

Architecture:
  OpenHands Agent → MCP tool call (free, no iteration cost)
  → gt_mcp_bridge.py (this file, stdio MCP server)
  → docker exec into running SWE-bench container
  → gt_tool.py at /tmp/gt_tool.py returns text
  → Bridge returns response to agent

Container discovery order:
  1. GT_CONTAINER_ID env var (explicit)
  2. docker ps --filter ancestor=sweb.eval (SWE-bench standard)
  3. docker ps --filter name=openhands-sandbox (OpenHands sandbox)
  4. docker ps --latest (fallback)

gt_tool.py is expected to be baked into the container image.
If not found, falls back to docker cp from the host.
"""

import asyncio
import os
import subprocess

from mcp.server.fastmcp import FastMCP

app = FastMCP("groundtruth")

# ── Container Discovery ──────────────────────────────────────────────

def _find_container() -> str | None:
    """Find the active SWE-bench container ID."""
    # 1. Explicit env var
    cid = os.environ.get("GT_CONTAINER_ID")
    if cid:
        return cid.strip()

    # 2. SWE-bench eval containers (ancestor filter)
    for ancestor_filter in ["sweb.eval", "swe-bench"]:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"ancestor={ancestor_filter}",
             "--filter", "status=running"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        if lines and lines[0]:
            return lines[0]

    # 3. Name-based filter (OpenHands uses openhands-sandbox-*)
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=openhands-sandbox",
         "--filter", "status=running"],
        capture_output=True, text=True, timeout=10,
    )
    lines = result.stdout.strip().split("\n")
    if lines and lines[0]:
        return lines[0]

    # 4. Fallback: most recently started running container
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "status=running", "--latest"],
        capture_output=True, text=True, timeout=10,
    )
    cid = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None
    return cid


# ── Lazy Setup ────────────────────────────────────────────────────────

_setup_done: dict[str, bool] = {}

async def _ensure_setup(container_id: str) -> None:
    """Ensure gt_tool.py exists in container. Skip docker cp if baked into image."""
    if container_id in _setup_done:
        return

    # Check if gt_tool.py is already baked into the image
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container_id, "test", "-f", "/tmp/gt_tool.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)

    if proc.returncode == 0:
        # Already baked in — skip docker cp
        _setup_done[container_id] = True
        return

    # Not baked in — copy from host
    gt_tool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt_tool.py")
    if not os.path.exists(gt_tool_path):
        raise FileNotFoundError(f"gt_tool.py not found at {gt_tool_path}")

    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", gt_tool_path, f"{container_id}:/tmp/gt_tool.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"docker cp failed: {stderr.decode()}")

    _setup_done[container_id] = True


# ── Docker Exec ───────────────────────────────────────────────────────

async def _docker_exec(command: str, timeout: float = 120) -> str:
    """Execute a command inside the SWE-bench container."""
    container_id = _find_container()
    if not container_id:
        return "Error: No running SWE-bench container found. Set GT_CONTAINER_ID or start a container."

    await _ensure_setup(container_id)

    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-w", "/testbed", container_id,
        "bash", "-c", command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: Command timed out after {timeout}s"

    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 and not output.strip():
        err = stderr.decode("utf-8", errors="replace")
        return f"Error (exit {proc.returncode}): {err[:2000]}"

    return output


# ── MCP Tools ─────────────────────────────────────────────────────────

@app.tool(
    description=(
        "Verify your patch covers all structurally coupled obligation sites. "
        "Call after finishing all code edits, before submitting. Takes no arguments — "
        "reads the current git diff automatically and re-indexes changed files."
    )
)
async def groundtruth_check() -> str:
    """Verify patch completeness against obligation sites."""
    return await _docker_exec("python3 /tmp/gt_tool.py groundtruth_check")


# ── Entry Point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(transport="stdio")
