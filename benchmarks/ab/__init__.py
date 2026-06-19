"""A/B benchmark harness: no_mcp vs with_groundtruth_mcp.

Single entrypoint, same task set and evaluation, with provable MCP usage
for the with_groundtruth_mcp condition.
"""

from benchmarks.ab.models import (
    ABCondition,
    ABReport,
    MCPProof,
    RunMetadata,
)

__all__ = [
    "ABCondition",
    "ABReport",
    "MCPProof",
    "RunMetadata",
]
