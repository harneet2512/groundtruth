#!/usr/bin/env python3
"""Run the LLM code-gen agent in no_mcp or with_groundtruth_mcp mode.

This is the entry point for the agent-based A/B benchmark (LLM generates code
with vs without GroundTruth MCP tools). Requires OPENAI_API_KEY.

Usage:
  python -m benchmarks.ab.agent_harness --condition no_mcp --task "Add password hashing to user creation" --file src/routes/users.py --language python
  python -m benchmarks.ab.agent_harness --condition with_groundtruth_mcp --task "..." --file ... --language python
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from benchmarks.ab.agent import CodeGenAgent
from benchmarks.ab.agent_config import AgentConfig
from benchmarks.ab.mcp_client_runner import _prepare_server_index
from benchmarks.ab.models import MCPProof


def _run_no_mcp(task: str, file_path: str, language: str, config: AgentConfig) -> str | None:
    """Run agent without MCP; return generated code."""
    agent = CodeGenAgent(config)
    return agent.generate_no_mcp(task, file_path, language)


async def _run_with_mcp(
    task: str,
    file_path: str,
    language: str,
    config: AgentConfig,
    run_id: str | None,
) -> tuple[str | None, MCPProof]:
    """Run agent with MCP server; return (code, proof)."""
    from mcp.client.stdio import StdioServerParameters, stdio_client

    agent = CodeGenAgent(config)
    with tempfile.TemporaryDirectory(prefix="groundtruth_agent_") as tmp:
        db_dir = os.path.join(tmp, ".groundtruth")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "index.db")
        _prepare_server_index(tmp, db_path)
        if run_id:
            os.environ["GROUNDTRUTH_RUN_ID"] = run_id
        command = sys.executable
        args = [
            "-m", "groundtruth.main", "serve",
            "--root", tmp, "--db", db_path, "--no-auto-index",
        ]
        server_params = StdioServerParameters(command=command, args=args, cwd=str(_ROOT))

        def session_factory():
            return stdio_client(server_params)

        return await agent.generate_with_mcp(
            task_description=task,
            file_path=file_path,
            language=language,
            session_factory=session_factory,
            run_id=run_id,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B code-gen agent (no_mcp vs with_groundtruth_mcp)")
    parser.add_argument("--condition", required=True, choices=["no_mcp", "with_groundtruth_mcp"])
    parser.add_argument("--task", required=True, help="Coding task description")
    parser.add_argument("--file", required=True, help="Target file path (e.g. src/routes/users.py)")
    parser.add_argument("--language", default="python", choices=["python", "typescript", "go"])
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    if args.condition == "with_groundtruth_mcp" and not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY required for with_groundtruth_mcp", file=sys.stderr)
        return 1
    if args.condition == "no_mcp" and not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY required for no_mcp (LLM call)", file=sys.stderr)
        return 1

    config = AgentConfig(
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    run_id = str(uuid.uuid4())

    if args.condition == "no_mcp":
        code = _run_no_mcp(args.task, args.file, args.language, config)
        if code:
            print("Generated code length:", len(code))
            print("---")
            print(code[:2000] + ("..." if len(code) > 2000 else ""))
        else:
            print("No code generated.")
        return 0 if code else 1

    code, proof = asyncio.run(_run_with_mcp(args.task, args.file, args.language, config, run_id))
    print("MCP proof: connection_ok=%s, substantive_calls=%s, valid=%s" % (
        proof.connection_ok, proof.substantive_tool_count, proof.valid,
    ))
    if code:
        print("Generated code length:", len(code))
        print("---")
        print(code[:2000] + ("..." if len(code) > 2000 else ""))
    else:
        print("No code generated.")
    return 0 if (code and proof.valid) else 1


if __name__ == "__main__":
    sys.exit(main())
