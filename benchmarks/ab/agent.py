"""LLM code-gen agent for A/B benchmark: no_mcp (no tools) vs with_mcp (GroundTruth MCP tools)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Project root and path setup
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from benchmarks.ab.agent_config import AgentConfig
from benchmarks.ab.models import MCPProof


# Minimal GroundTruth tool definitions for the agent (OpenAI function-calling format).
# Only the tools needed for code-gen: find_relevant, brief, validate.
GT_TOOLS_FOR_AGENT: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "groundtruth_find_relevant",
            "description": "Find files relevant to a task description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "entry_symbols": {"type": "array", "items": {"type": "string"}},
                    "max_files": {"type": "integer"},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "groundtruth_brief",
            "description": "Get a briefing before writing code: symbols, imports, patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "target_file": {"type": "string"},
                },
                "required": ["intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "groundtruth_validate",
            "description": "Validate proposed code against the codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_code": {"type": "string"},
                    "file_path": {"type": "string"},
                    "language": {"type": "string"},
                },
                "required": ["proposed_code", "file_path"],
            },
        },
    },
]

SUBSTANTIVE_TOOLS = {"groundtruth_validate", "groundtruth_find_relevant", "groundtruth_brief"}


def _extract_code_from_content(content: str | None, file_path: str) -> str | None:
    """Extract code block from LLM response. Prefer ```file_path or ```lang blocks."""
    if not content or not content.strip():
        return None
    # Try marked code block with path or language
    pattern = r"```(?:\w*)\s*\n(.*?)```"
    matches = list(re.finditer(pattern, content, re.DOTALL))
    if matches:
        return matches[-1].group(1).strip()
    # No backticks: treat whole content as code if it looks like code
    if "def " in content or "import " in content or "function " in content or "class " in content:
        return content.strip()
    return None


class CodeGenAgent:
    """Generates code for a task; supports no_mcp (no tools) and with_mcp (MCP tools)."""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()

    def generate_no_mcp(self, task_description: str, file_path: str, language: str) -> str | None:
        """
        Generate code using the LLM only (no MCP tools).
        Returns the generated code string, or None if parsing failed.
        """
        try:
            from openai import OpenAI
        except ImportError:
            return None
        client = OpenAI()
        user_msg = (
            f"Task: {task_description}\n\n"
            f"Produce the full file content for: {file_path}\n"
            f"Language: {language}. Use correct imports and existing project symbols only."
        )
        response = client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": self.config.no_mcp_system},
                {"role": "user", "content": user_msg},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        content = response.choices[0].message.content if response.choices else None
        return _extract_code_from_content(content, file_path)

    async def generate_with_mcp(
        self,
        task_description: str,
        file_path: str,
        language: str,
        session_factory: Any,  # async context manager yielding (read_stream, write_stream)
        run_id: str | None = None,
    ) -> tuple[str | None, MCPProof]:
        """
        Generate code using the LLM with GroundTruth MCP tools.
        session_factory: async context manager that yields (read_stream, write_stream)
        after connecting to the MCP server (e.g. stdio_client(server_params)).
        Returns (generated_code, mcp_proof).
        """
        import anyio
        from mcp import ClientSession

        proof = MCPProof(mcp_enabled=True, connection_ok=False, valid=False, run_id=run_id)
        tool_calls_log: list[dict[str, Any]] = []
        generated_code: str | None = None

        try:
            from openai import OpenAI
        except ImportError:
            return None, proof

        if run_id:
            os.environ["GROUNDTRUTH_RUN_ID"] = run_id

        try:
            async with session_factory() as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    proof.connection_ok = True
                    tools_result = await session.list_tools()
                    if hasattr(tools_result, "tools") and tools_result.tools:
                        proof.tools_discovered = [t.name for t in tools_result.tools]

                    client = OpenAI()
                    user_msg = (
                        f"Task: {task_description}\n\n"
                        f"Produce the full file content for: {file_path}\n"
                        f"Language: {language}. Use GroundTruth tools to find relevant files, get a briefing, "
                        f"then validate your code before returning it."
                    )
                    messages: list[dict[str, Any]] = [
                        {"role": "system", "content": self.config.with_mcp_system},
                        {"role": "user", "content": user_msg},
                    ]

                    for _turn in range(self.config.max_turns_with_mcp):
                        response = client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            tools=GT_TOOLS_FOR_AGENT,
                            tool_choice="auto",
                            temperature=self.config.temperature,
                            max_tokens=self.config.max_tokens,
                        )
                        choice = response.choices[0]
                        msg = choice.message
                        messages.append({
                            "role": "assistant",
                            "content": msg.content,
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                                }
                                for tc in (msg.tool_calls or [])
                            ],
                        })

                        if not msg.tool_calls:
                            generated_code = _extract_code_from_content(msg.content, file_path)
                            break

                        for tc in msg.tool_calls:
                            name = tc.function.name
                            try:
                                args = json.loads(tc.function.arguments)
                            except json.JSONDecodeError:
                                args = {}
                            result = await session.call_tool(name, args)
                            success = not getattr(result, "isError", True)
                            tool_calls_log.append({"name": name, "success": success})
                            content = getattr(result, "content", []) or []
                            text_parts = [
                                getattr(p, "text", str(p))
                                for p in content
                                if hasattr(p, "text")
                            ]
                            result_text = "".join(text_parts) if text_parts else "{}"
                            if len(result_text) > 8000:
                                result_text = result_text[:4000] + "\n...(truncated)...\n" + result_text[-4000:]
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result_text,
                            })
                            if name == "groundtruth_validate" and not generated_code:
                                try:
                                    data = json.loads(result_text)
                                    if "valid" in data and data.get("valid"):
                                        proposed = data.get("proposed_code") or (args.get("proposed_code") if isinstance(args, dict) else None)
                                        if proposed:
                                            generated_code = proposed
                                except (json.JSONDecodeError, TypeError):
                                    pass

            proof.tool_calls = tool_calls_log
            proof.substantive_tool_count = sum(
                1 for t in tool_calls_log if t.get("name") in SUBSTANTIVE_TOOLS
            )
            proof.valid = proof.connection_ok and proof.substantive_tool_count >= 1
        except Exception as e:
            proof.connection_ok = False
            proof.tool_calls = [{"name": "error", "success": False, "error": str(e)}]
            generated_code = None

        return generated_code, proof
