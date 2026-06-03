"""GT v2 Memory Surface — lifecycle-aware agent memory."""

from __future__ import annotations

MEMORY_AVAILABLE = True


def is_memory_available() -> bool:
    """Check if memory surface dependencies are installed."""
    return MEMORY_AVAILABLE
