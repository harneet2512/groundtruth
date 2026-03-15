"""Token usage tracking for MCP responses."""

from __future__ import annotations


class TokenTracker:
    """Estimates and tracks token usage across MCP tool calls."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, int]] = []

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate token count from text (≈4 chars per token)."""
        return len(text) // 4

    def track(self, tool_name: str, response_text: str) -> int:
        """Record token usage for a tool call. Returns estimated tokens."""
        tokens = self.estimate_tokens(response_text)
        self._entries.append((tool_name, tokens))
        return tokens

    def get_session_total(self) -> int:
        """Total estimated tokens across all tracked calls."""
        return sum(t for _, t in self._entries)

    def get_breakdown(self) -> dict[str, int]:
        """Token usage grouped by tool name."""
        breakdown: dict[str, int] = {}
        for tool_name, tokens in self._entries:
            breakdown[tool_name] = breakdown.get(tool_name, 0) + tokens
        return breakdown

    def get_footprint(self, tool_name: str, call_tokens: int) -> dict[str, object]:
        """Build a footprint dict for inclusion in responses."""
        return {
            "this_call_tokens": call_tokens,
            "session_total_tokens": self.get_session_total(),
            "breakdown": self.get_breakdown(),
        }
