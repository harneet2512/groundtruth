"""MCP tools package — re-exports all legacy and consolidated handlers.

Backward compatibility: all legacy handler names are available at the
``groundtruth.mcp.tools`` import path, so ``server.py`` needs no changes.
"""

# Legacy handlers — full backward compatibility
from groundtruth.mcp.tools.legacy_tools import (  # noqa: F401
    _build_dependency_chain,
    _check_path,
    _detect_operation,
    _extract_function_source,
    _find_pattern_example,
    _read_source_lines,
    handle_brief,
    handle_check_patch,
    handle_checkpoint,
    handle_confusions,
    handle_context,
    handle_dead_code,
    handle_do,
    handle_explain,
    handle_find_relevant,
    handle_hotspots,
    handle_impact,
    handle_obligations,
    handle_orient,
    handle_patterns,
    handle_scope,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)

# Consolidated handlers — new high-signal surface
from groundtruth.mcp.tools.core_tools import (  # noqa: F401
    handle_consolidated_check,
    handle_consolidated_impact,
    handle_consolidated_orient,
    handle_consolidated_references,
    handle_consolidated_search,
)

__all__ = [
    # Legacy
    "handle_brief",
    "handle_check_patch",
    "handle_checkpoint",
    "handle_confusions",
    "handle_context",
    "handle_dead_code",
    "handle_do",
    "handle_explain",
    "handle_find_relevant",
    "handle_hotspots",
    "handle_impact",
    "handle_obligations",
    "handle_orient",
    "handle_patterns",
    "handle_scope",
    "handle_status",
    "handle_symbols",
    "handle_trace",
    "handle_unused_packages",
    "handle_validate",
    # Consolidated
    "handle_consolidated_check",
    "handle_consolidated_impact",
    "handle_consolidated_orient",
    "handle_consolidated_references",
    "handle_consolidated_search",
]
