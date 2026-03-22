"""Feature flag infrastructure for incubator subsystems.

All flags default to OFF. Enable via environment variables:
    GT_ENABLE_CONTRADICTIONS=1
    GT_ENABLE_ABSTENTION=1
    etc.

Flag migration (Phase 5):
    GT_ENABLE_REPO_INTEL is DEPRECATED. Use the split flags:
    - GT_ENABLE_REPO_INTEL_LOGGING  (data collection)
    - GT_ENABLE_REPO_INTEL_DECISIONS (use data in responses, requires LOGGING)
    Old flag maps to LOGGING=on when split flags are unset.
"""

from __future__ import annotations

import os

import structlog

_log = structlog.get_logger("groundtruth.flags")
_repo_intel_deprecation_warned = False


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
    """DEPRECATED — compat alias. Maps to repo_intel_logging_enabled().

    Existing callers that check this flag continue to work. New code
    should use repo_intel_logging_enabled() / repo_intel_decisions_enabled().
    """
    return repo_intel_logging_enabled()


def repo_intel_logging_enabled() -> bool:
    """Append-only data collection to summary tables.

    Activated by GT_ENABLE_REPO_INTEL_LOGGING=1, OR by the deprecated
    GT_ENABLE_REPO_INTEL=1 when split flags are unset.
    """
    global _repo_intel_deprecation_warned  # noqa: PLW0603
    if is_enabled("REPO_INTEL_LOGGING"):
        return True
    # Compat: old umbrella flag maps to logging-only
    if is_enabled("REPO_INTEL"):
        if not _repo_intel_deprecation_warned:
            _repo_intel_deprecation_warned = True
            _log.warning(
                "deprecated_flag",
                flag="GT_ENABLE_REPO_INTEL",
                message="Use GT_ENABLE_REPO_INTEL_LOGGING instead. "
                "GT_ENABLE_REPO_INTEL will be removed in a future release.",
            )
        return True
    return False


def repo_intel_decisions_enabled() -> bool:
    """Use collected summary data in tool responses.

    Hard-depends on LOGGING — you can't use data you haven't collected.
    Requires GT_ENABLE_REPO_INTEL_DECISIONS=1 AND logging to be active.
    """
    return repo_intel_logging_enabled() and is_enabled("REPO_INTEL_DECISIONS")


def response_state_machine_enabled() -> bool:
    """Communication state machine framing in tool responses."""
    return is_enabled("RESPONSE_STATE_MACHINE")


def hnsw_enabled() -> bool:
    """Use HNSW backend for similarity queries (requires hnswlib installed)."""
    return is_enabled("HNSW")


def structural_similarity_enabled() -> bool:
    """Structural similarity search (AST feature vectors)."""
    return is_enabled("STRUCTURAL_SIMILARITY")


def treesitter_enabled() -> bool:
    """Tree-sitter parser backend (multi-language support)."""
    return is_enabled("TREESITTER")


def foundation_enabled() -> bool:
    """Foundation v2 pipeline (similarity + graph expansion + live indexing)."""
    return is_enabled("FOUNDATION")
