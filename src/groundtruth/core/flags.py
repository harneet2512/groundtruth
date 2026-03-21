"""Feature flag infrastructure for incubator subsystems.

All flags default to OFF. Enable via environment variables:
    GT_ENABLE_CONTRADICTIONS=1
    GT_ENABLE_ABSTENTION=1
    etc.
"""

from __future__ import annotations

import os


def is_enabled(flag: str) -> bool:
    """Check GT_ENABLE_{flag}. Returns False if unset or '0'."""
    val = os.environ.get(f"GT_ENABLE_{flag.upper()}", "")
    return val.lower() in ("1", "true", "yes")


def contradictions_enabled() -> bool:
    """Contradiction output in consolidated check."""
    return is_enabled("CONTRADICTIONS")


def abstention_enabled() -> bool:
    """AbstentionPolicy filtering of findings."""
    return is_enabled("ABSTENTION")


def communication_enabled() -> bool:
    """CommunicationPolicy framing in MCP responses."""
    return is_enabled("COMMUNICATION")


def state_flow_enabled() -> bool:
    """StateFlowGraph in obligation output."""
    return is_enabled("STATE_FLOW")


def convention_fingerprint_enabled() -> bool:
    """Per-class convention fingerprints."""
    return is_enabled("CONVENTION_FINGERPRINT")


def content_hash_enabled() -> bool:
    """Content-hash based incremental indexing."""
    return is_enabled("CONTENT_HASH")


def repo_intel_enabled() -> bool:
    """Accumulated repo intelligence logging."""
    return is_enabled("REPO_INTEL")


def structural_similarity_enabled() -> bool:
    """Structural similarity search (AST feature vectors)."""
    return is_enabled("STRUCTURAL_SIMILARITY")
