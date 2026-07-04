"""Configuration for the A/B code-gen agent (model, temperature, prompts)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """LLM and agent behavior for A/B code generation."""

    model: str = "gpt-4o-mini"
    temperature: float = 0.0
    max_tokens: int = 4096
    max_turns_with_mcp: int = 15

    # System prompts (overridable for experiments)
    no_mcp_system: str = field(
        default_factory=lambda: (
            "You are a precise code-generation assistant. You will be given a coding task and a file path. "
            "Respond with ONLY the requested code: the full file content for the given path, using correct "
            "imports and symbols that exist in the project. Do not include explanations or markdown."
        )
    )
    with_mcp_system: str = field(
        default_factory=lambda: (
            "You are a code-generation assistant with access to GroundTruth MCP tools for codebase intelligence. "
            "Workflow: (1) Use groundtruth_find_relevant to find relevant files for the task. "
            "(2) Use groundtruth_brief with your intent to get a briefing. "
            "(3) Generate the requested code. "
            "(4) Use groundtruth_validate on your proposed code to check for errors. "
            "Fix any reported errors and re-validate until valid. "
            "When done, respond with the final code only (no markdown, no explanation)."
        )
    )
