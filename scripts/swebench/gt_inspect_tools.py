"""GroundTruth live tools for Inspect AI SWE-bench evaluation.

Three tools that wrap gt_tool.py inside the Docker sandbox:
- gt_impact(symbol): What must change if I change this symbol?
- gt_references(symbol): Where is this symbol used?
- gt_check(): Is my patch structurally complete?

GT is lazily initialized: first tool call copies gt_tool.py into the
container and indexes the repo. Index is cached for the task duration.
"""

import json
import os
import time
from pathlib import Path

from inspect_ai.tool import Tool, ToolResult, tool
from inspect_ai.util import sandbox as get_sandbox
from inspect_ai.util._sandbox.environment import SandboxEnvironment

# Path to gt_tool.py on the HOST (will be copied into container)
GT_TOOL_HOST_PATH = os.environ.get(
    "GT_TOOL_PATH",
    os.path.expanduser("~/groundtruth/benchmarks/swebench/gt_tool.py"),
)

# Max characters in GT output to avoid context bloat
GT_MAX_OUTPUT = 3000

# JSONL log for observability
GT_LOG_PATH = os.environ.get("GT_LOG_PATH", "/tmp/gt_tool_calls.jsonl")


async def _ensure_gt_installed(sb: SandboxEnvironment) -> bool:
    """Lazily install and index gt_tool.py in the container."""
    # Check if already installed
    result = await sb.exec(["test", "-f", "/tmp/gt_tool.py"])
    if result.returncode == 0:
        return True

    # Copy gt_tool.py into container
    gt_code = Path(GT_TOOL_HOST_PATH).read_text()
    await sb.write_file("/tmp/gt_tool.py", gt_code)
    await sb.exec(["chmod", "+x", "/tmp/gt_tool.py"])
    return True


async def _run_gt(sb: SandboxEnvironment, command: str, arg: str = "") -> str:
    """Run a gt_tool.py command inside the sandbox and return output."""
    await _ensure_gt_installed(sb)

    cmd = ["python3", "/tmp/gt_tool.py", command]
    if arg:
        cmd.append(arg)

    result = await sb.exec(cmd, timeout=30)

    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr:
        output += f"\n[stderr: {result.stderr.strip()[:500]}]"

    # Cap output to avoid context bloat
    if len(output) > GT_MAX_OUTPUT:
        output = output[:GT_MAX_OUTPUT] + f"\n... [truncated, {len(output)} total chars]"

    return output if output else "[no output]"


def _log_call(tool_name: str, args: dict, result: str, duration: float) -> None:
    """Log GT tool call to JSONL for observability."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool_name,
        "args": args,
        "result_length": len(result),
        "result_preview": result[:500],
        "duration_seconds": round(duration, 2),
    }
    with open(GT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


@tool
def gt_impact() -> Tool:
    """Analyze what must change if a symbol is modified.

    Shows obligation sites (methods sharing state), class conventions,
    and subclass overrides. Call this BEFORE making changes to understand
    coupling and avoid incomplete patches.

    Args:
        symbol: The class or function name to analyze (e.g. "QuerySet", "get_user")
    """

    async def run(symbol: str) -> ToolResult:
        start = time.time()
        sb = get_sandbox()
        result = await _run_gt(sb, "groundtruth_impact", symbol)
        duration = time.time() - start
        _log_call("gt_impact", {"symbol": symbol}, result, duration)
        return ToolResult(content=result)

    return run


@tool
def gt_references() -> Tool:
    """Find all references to a symbol across the codebase.

    Shows where a class, function, or method is defined and every file/line
    where it is used. Call this to find all dependents before making changes.

    Args:
        symbol: The symbol to search for (e.g. "QuerySet", "parse_datetime")
    """

    async def run(symbol: str) -> ToolResult:
        start = time.time()
        sb = get_sandbox()
        result = await _run_gt(sb, "groundtruth_references", symbol)
        duration = time.time() - start
        _log_call("gt_references", {"symbol": symbol}, result, duration)
        return ToolResult(content=result)

    return run


@tool
def gt_check() -> Tool:
    """Check if the current patch covers all obligation sites.

    Runs after editing to verify completeness. Parses git diff and maps
    changes to obligation groups, showing which sites were modified and
    which were missed.
    """

    async def run() -> ToolResult:
        start = time.time()
        sb = get_sandbox()
        result = await _run_gt(sb, "groundtruth_check")
        duration = time.time() - start
        _log_call("gt_check", {}, result, duration)
        return ToolResult(content=result)

    return run
