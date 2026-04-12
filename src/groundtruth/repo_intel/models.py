"""Repository Intelligence domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Component:
    """A logical component — a cluster of files with high cohesion."""

    name: str
    """Component name (derived from directory or dominant package)."""

    file_paths: tuple[str, ...]
    """Files belonging to this component."""

    confidence: float
    """0.0-1.0: how confident we are in this clustering."""

    entry_points: tuple[str, ...] = ()
    """Public entry points (exported functions/classes)."""


@dataclass(frozen=True)
class BuildNode:
    """A node in the build graph (package, target, or entry point)."""

    name: str
    """Package or target name."""

    kind: Literal["package", "entry_point", "build_target", "config"]
    """Node type."""

    file_path: str
    """Manifest or source file defining this node."""

    dependencies: tuple[str, ...] = ()
    """Names of nodes this depends on."""


@dataclass(frozen=True)
class TestEdge:
    """Relationship between a test and the code it exercises."""

    test_file: str
    """Path to the test file."""

    source_file: str
    """Path to the source file being tested."""

    test_symbols: tuple[str, ...]
    """Test function/method names."""

    source_symbols: tuple[str, ...]
    """Source symbols exercised."""

    confidence: float
    """0.0-1.0: how confident in this mapping."""

    relationship: Literal["direct", "transitive", "naming_convention"] = "direct"
    """How the relationship was determined."""


@dataclass(frozen=True)
class CouplingEdge:
    """A typed coupling relationship between files.

    Coupling edges capture non-code dependencies: config references,
    documentation mentions, registry entries, build declarations.
    """

    source_file: str
    """File containing the reference."""

    target_file: str
    """File being referenced."""

    coupling_type: Literal["config", "doc", "registry", "build", "test"]
    """Type of coupling."""

    confidence: float
    """0.0-1.0."""

    detail: str = ""
    """Additional context (e.g., the config key, doc section)."""


@dataclass
class RepoIntelResult:
    """Complete repository intelligence output."""

    components: list[Component] = field(default_factory=list)
    build_nodes: list[BuildNode] = field(default_factory=list)
    test_edges: list[TestEdge] = field(default_factory=list)
    coupling_edges: list[CouplingEdge] = field(default_factory=list)
