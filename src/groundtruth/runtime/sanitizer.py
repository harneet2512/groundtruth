"""Sanitize every agent-visible GT message.

Strips hidden diagnostic prefixes, enforces character caps,
and validates that only allowed markers reach the agent.
Shared between OH adapter and MCP product face.
"""
from __future__ import annotations

_HIDDEN_PREFIXES = (
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]",
    "[GT_DELIVERY]", "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]",
)


def is_hidden_line(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in _HIDDEN_PREFIXES)


def sanitize(text: str, *, max_chars: int = 2000) -> str:
    """Remove hidden lines and enforce character cap."""
    lines = [ln for ln in text.splitlines() if not is_hidden_line(ln)]
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars - 3] + "..."
    return cleaned


def has_leak(text: str) -> bool:
    """True if text contains any hidden diagnostic prefix."""
    return any(p in text for p in _HIDDEN_PREFIXES)
