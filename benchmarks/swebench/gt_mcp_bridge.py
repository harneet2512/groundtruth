#!/usr/bin/env python3
"""
GroundTruth MCP Bridge — gt_check only, with observability.

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
  4. docker ps --filter name=openhands (OpenHands V1 agent-server)
  5. docker ps --latest (fallback)

gt_tool.py is expected to be baked into the container image.
If not found, falls back to docker cp from the host.

Observability:
  Every GT call is logged to GT_OBS_LOG (default: /tmp/gt_obs.jsonl).
  Each entry records: timestamp, tool, container, latency, response size,
  whether violations were found, and a response snippet.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

from mcp.server.fastmcp import FastMCP

app = FastMCP("groundtruth")

# ── Observability ────────────────────────────────────────────────────

GT_OBS_LOG = os.environ.get("GT_OBS_LOG", "/tmp/gt_obs.jsonl")

def _log_obs(entry: dict) -> None:
    """Append an observation entry to the JSONL log."""
    try:
        with open(GT_OBS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # observability must never break the tool

def _stderr(msg: str) -> None:
    """Write debug info to stderr (visible in logs, not in MCP stdio)."""
    print(f"[GT-bridge] {msg}", file=sys.stderr, flush=True)


# ── Container Discovery ──────────────────────────────────────────────

def _find_container() -> str | None:
    """Find the active SWE-bench container ID."""
    # 1. Explicit env var
    cid = os.environ.get("GT_CONTAINER_ID")
    if cid:
        _stderr(f"Container from GT_CONTAINER_ID: {cid.strip()[:12]}")
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
            _stderr(f"Container from ancestor={ancestor_filter}: {lines[0][:12]}")
            return lines[0]

    # 3. Name-based filter (OpenHands sandbox or agent-server)
    for name_filter in ["openhands-sandbox", "openhands"]:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name={name_filter}",
             "--filter", "status=running"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        if lines and lines[0]:
            _stderr(f"Container from name={name_filter}: {lines[0][:12]}")
            return lines[0]

    # 4. Fallback: most recently started running container
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "status=running", "--latest"],
        capture_output=True, text=True, timeout=10,
    )
    cid = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None
    if cid:
        _stderr(f"Container from fallback (latest): {cid[:12]}")
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
        _setup_done[container_id] = True
        _stderr("gt_tool.py already in container")
        return

    # Not baked in — copy from host
    gt_tool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gt_tool.py")
    if not os.path.exists(gt_tool_path):
        raise FileNotFoundError(f"gt_tool.py not found at {gt_tool_path}")

    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", gt_tool_path, f"{container_id}:/tmp/gt_tool.py",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_out = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"docker cp failed: {stderr_out.decode()}")

    _setup_done[container_id] = True
    _stderr("gt_tool.py copied into container")


# ── Workspace Discovery ──────────────────────────────────────────────

async def _find_workspace(container_id: str) -> str:
    """Find the repo working directory inside the container.

    OpenHands V1 copies /testbed to /workspace/<repo>/.
    Falls back to /testbed if /workspace has no repo subdirs.
    """
    # Check /workspace first (V1 layout)
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container_id,
        "bash", "-c", "ls -d /workspace/*/ 2>/dev/null | head -1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    ws_path = stdout.decode().strip()
    if ws_path and proc.returncode == 0:
        _stderr(f"Workspace: {ws_path}")
        return ws_path

    # Fallback to /testbed
    _stderr("Workspace: /testbed (fallback)")
    return "/testbed"


# ── Docker Exec ───────────────────────────────────────────────────────

async def _docker_exec(command: str, timeout: float = 120) -> str:
    """Execute a command inside the SWE-bench container."""
    container_id = _find_container()
    if not container_id:
        return "Error: No running SWE-bench container found. Set GT_CONTAINER_ID or start a container."

    await _ensure_setup(container_id)
    workdir = await _find_workspace(container_id)

    # Override REPO_ROOT in gt_tool.py via env var
    full_command = f"REPO_ROOT={workdir} {command}"

    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-w", workdir, container_id,
        "bash", "-c", full_command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr_out = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Error: Command timed out after {timeout}s"

    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 and not output.strip():
        err = stderr_out.decode("utf-8", errors="replace")
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
    t0 = time.time()
    _stderr("groundtruth_check called")

    result = await _docker_exec("python3 /tmp/gt_tool.py groundtruth_check")

    latency_ms = int((time.time() - t0) * 1000)

    # Parse result for observability
    has_violations = "STRUCTURAL VIOLATION" in result
    has_obligations = "obligation" in result.lower()
    line_count = result.count("\n")

    obs = {
        "ts": time.time(),
        "tool": "groundtruth_check",
        "latency_ms": latency_ms,
        "response_bytes": len(result),
        "response_lines": line_count,
        "has_violations": has_violations,
        "has_obligations": has_obligations,
        "is_error": result.startswith("Error"),
        "snippet": result[:500],
    }
    _log_obs(obs)
    _stderr(
        f"groundtruth_check done: {latency_ms}ms, {len(result)}B, "
        f"violations={has_violations}, error={result.startswith('Error')}"
    )

    return result


# ── Entry Point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    _stderr(f"Starting GT MCP bridge (obs log: {GT_OBS_LOG})")
    app.run(transport="stdio")
