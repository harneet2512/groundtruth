"""Repository Intelligence — deterministic architectural map of repositories.

Provides build graph, test topology, component boundaries, and typed
coupling edges that agents can query for localization and navigation.
"""

from groundtruth.repo_intel.models import (
    BuildNode,
    Component,
    CouplingEdge,
    TestEdge,
)

__all__ = ["BuildNode", "Component", "CouplingEdge", "TestEdge"]
