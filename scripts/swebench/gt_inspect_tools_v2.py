"""GroundTruth live tools V2 — with call cap, pre-indexing, and tighter output.

V2 changes from V1:
1. CALL CAP: Max 3 GT calls per task. After 3, returns guidance to proceed.
2. PRE-INDEX: First _ensure_gt_installed also runs indexing upfront.
3. TIGHTER OUTPUT: Max 2000 chars (down from 3000) to save context.
4. BETTER LOGGING: Includes call count and whether cap was hit.
"""

import json
import os
import time
from pathlib import Path

from inspect_ai.tool import Tool, tool
from inspect_ai.util import sandbox as get_sandbox
from inspect_ai.util._sandbox.environment import SandboxEnvironment

# Path to gt_tool.py on the HOST
GT_TOOL_HOST_PATH = os.environ.get(
    "GT_TOOL_PATH",
    os.path.expanduser("~/groundtruth/benchmarks/swebench/gt_tool.py"),
)

# Tighter output cap (V2: 2000, was 3000)
GT_MAX_OUTPUT = 2000

# JSONL log
GT_LOG_PATH = os.environ.get("GT_LOG_PATH", "/tmp/gt_v2_tool_calls.jsonl")

# V2: Call cap per task — data shows 1-3 calls help, 5+ hurt
GT_MAX_CALLS_PER_TASK = int(os.environ.get("GT_MAX_CALLS", "3"))

# Per-sandbox call counter (reset per task via sandbox identity)
_call_counts: dict[int, int] = {}


def _get_call_count(sb: SandboxEnvironment) -> int:
    """Get current GT call count for this sandbox (task)."""
    sb_id = id(sb)
    return _call_counts.get(sb_id, 0)


def _increment_call_count(sb: SandboxEnvironment) -> int:
    """Increment and return new count."""
    sb_id = id(sb)
    _call_counts[sb_id] = _call_counts.get(sb_id, 0) + 1
    return _call_counts[sb_id]


async def _ensure_gt_installed(sb: SandboxEnvironment) -> bool:
    """Install gt_tool.py and PRE-INDEX the repo (V2: index upfront)."""
    result = await sb.exec(["test", "-f", "/tmp/gt_tool.py"])
    if result.returncode == 0:
        return True

    # Copy gt_tool.py into container
    gt_code = Path(GT_TOOL_HOST_PATH).read_text()
    await sb.write_file("/tmp/gt_tool.py", gt_code)
    await sb.exec(["chmod", "+x", "/tmp/gt_tool.py"])

    # V2: Pre-index the repo so first real call is fast
    await sb.exec(
        ["python3", "/tmp/gt_tool.py", "groundtruth_summary"],
        timeout=60,
    )
    return True


async def _run_gt(sb: SandboxEnvironment, command: str, arg: str = "") -> str:
    """Run a gt_tool.py command inside the sandbox."""
    await _ensure_gt_installed(sb)

    cmd = ["python3", "/tmp/gt_tool.py", command]
    if arg:
        cmd.append(arg)

    result = await sb.exec(cmd, timeout=30)

    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr:
        output += f"\n[stderr: {result.stderr.strip()[:300]}]"

    # V2: Tighter cap
    if len(output) > GT_MAX_OUTPUT:
        output = output[:GT_MAX_OUTPUT] + f"\n... [truncated, {len(output)} total chars]"

    return output if output else "[no output]"


def _log_call(
    tool_name: str, args: dict, result: str, duration: float,
    call_number: int, capped: bool,
) -> None:
    """Log GT tool call with V2 metadata."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool_name,
        "args": args,
        "result_length": len(result),
        "result_preview": result[:500],
        "duration_seconds": round(duration, 2),
        "call_number": call_number,
        "capped": capped,
        "version": "v2",
    }
    with open(GT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


CAP_MESSAGE = (
    "GT analysis complete (3/3 calls used). You have enough context. "
    "Proceed with your code changes now. Use bash and text_editor to "
    "implement the fix, then submit."
)


@tool
def gt_impact() -> Tool:
    async def run(symbol: str) -> str:
        """Show what must change when a symbol is modified — obligation sites, shared state, conventions. Call ONCE before editing a class. Do not call repeatedly.

        Args:
            symbol: The class or function name to analyze (e.g. "QuerySet", "get_user")
        """
        sb = get_sandbox()
        count = _get_call_count(sb)
        if count >= GT_MAX_CALLS_PER_TASK:
            _log_call("gt_impact", {"symbol": symbol}, CAP_MESSAGE, 0.0, count + 1, True)
            return CAP_MESSAGE

        start = time.time()
        result = await _run_gt(sb, "groundtruth_impact", symbol)
        duration = time.time() - start
        new_count = _increment_call_count(sb)
        remaining = GT_MAX_CALLS_PER_TASK - new_count
        if remaining > 0:
            result += f"\n\n[GT: {new_count}/{GT_MAX_CALLS_PER_TASK} calls used, {remaining} remaining]"
        else:
            result += f"\n\n[GT: All {GT_MAX_CALLS_PER_TASK} calls used. Proceed with changes.]"
        _log_call("gt_impact", {"symbol": symbol}, result, duration, new_count, False)
        return result

    return run


@tool
def gt_references() -> Tool:
    async def run(symbol: str) -> str:
        """Find where a symbol is defined and all files that use it. Call ONCE to find dependents. Do not call repeatedly — use bash grep for follow-up searches.

        Args:
            symbol: The symbol to search for (e.g. "QuerySet", "parse_datetime")
        """
        sb = get_sandbox()
        count = _get_call_count(sb)
        if count >= GT_MAX_CALLS_PER_TASK:
            _log_call("gt_references", {"symbol": symbol}, CAP_MESSAGE, 0.0, count + 1, True)
            return CAP_MESSAGE

        start = time.time()
        result = await _run_gt(sb, "groundtruth_references", symbol)
        duration = time.time() - start
        new_count = _increment_call_count(sb)
        remaining = GT_MAX_CALLS_PER_TASK - new_count
        if remaining > 0:
            result += f"\n\n[GT: {new_count}/{GT_MAX_CALLS_PER_TASK} calls used, {remaining} remaining]"
        else:
            result += f"\n\n[GT: All {GT_MAX_CALLS_PER_TASK} calls used. Proceed with changes.]"
        _log_call("gt_references", {"symbol": symbol}, result, duration, new_count, False)
        return result

    return run


@tool
def gt_check() -> Tool:
    async def run() -> str:
        """Verify patch completeness — checks if your edits cover all obligation sites. Call ONCE after finishing edits, before submitting. Do not call repeatedly."""
        sb = get_sandbox()
        count = _get_call_count(sb)
        if count >= GT_MAX_CALLS_PER_TASK:
            _log_call("gt_check", {}, CAP_MESSAGE, 0.0, count + 1, True)
            return CAP_MESSAGE

        start = time.time()
        result = await _run_gt(sb, "groundtruth_check")
        duration = time.time() - start
        new_count = _increment_call_count(sb)
        remaining = GT_MAX_CALLS_PER_TASK - new_count
        if remaining > 0:
            result += f"\n\n[GT: {new_count}/{GT_MAX_CALLS_PER_TASK} calls used, {remaining} remaining]"
        else:
            result += f"\n\n[GT: All {GT_MAX_CALLS_PER_TASK} calls used. Proceed with changes.]"
        _log_call("gt_check", {}, result, duration, new_count, False)
        return result

    return run
