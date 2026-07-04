"""Standardized tool response builder."""

from __future__ import annotations

import time
from typing import Any


class ToolResponse:
    """Builder for standardized MCP tool responses."""

    def __init__(self) -> None:
        self._start_ns = time.monotonic_ns()
        self._data: dict[str, Any] = {}
        self._guidance_parts: list[str] = []

    def set(self, key: str, value: Any) -> "ToolResponse":
        """Set a response field."""
        self._data[key] = value
        return self

    def add_guidance(self, text: str) -> "ToolResponse":
        """Add a reasoning guidance line."""
        if text:
            self._guidance_parts.append(text)
        return self

    def error(self, message: str) -> dict[str, Any]:
        """Return an error response."""
        return {"error": message}

    def build(self) -> dict[str, Any]:
        """Build the final response dict with timing and guidance."""
        elapsed_ms = max(1, (time.monotonic_ns() - self._start_ns) // 1_000_000)
        result = dict(self._data)
        result["latency_ms"] = elapsed_ms
        if self._guidance_parts:
            result["reasoning_guidance"] = " ".join(self._guidance_parts)
        return result
