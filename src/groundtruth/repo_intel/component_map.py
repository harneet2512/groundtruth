"""Component Map — clusters files into logical components.

Strategy:
1. Directory structure as strong prior (files in same dir = same component)
2. Import density as refinement (files that import each other heavily)
3. Package boundaries (each top-level package = one component)

Output: named components with file membership and confidence.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import PurePosixPath

from groundtruth.repo_intel.models import Component
from groundtruth.substrate.protocols import GraphReader


class ComponentMapExtractor:
    """Clusters repository files into logical components."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    def extract(self, root: str, max_depth: int = 2) -> list[Component]:
        """Extract component map for the repository.

        Args:
            root: Repository root path.
            max_depth: Maximum directory depth for component boundaries.

        Returns list of components with file assignments.
        """
        all_files = self._reader.get_file_paths()
        if not all_files:
            return []

        # Phase 1: Directory-based clustering
        dir_clusters = self._cluster_by_directory(all_files, root, max_depth)

        # Phase 2: Identify entry points per component
        components: list[Component] = []
        for dir_name, files in dir_clusters.items():
            if not files:
                continue

            entry_points = self._find_entry_points(files)
            confidence = self._compute_confidence(files)

            components.append(Component(
                name=dir_name,
                file_paths=tuple(sorted(files)),
                confidence=confidence,
                entry_points=tuple(entry_points),
            ))

        return components

    def _cluster_by_directory(
        self, files: list[str], root: str, max_depth: int
    ) -> dict[str, list[str]]:
        """Group files by directory path up to max_depth."""
        clusters: dict[str, list[str]] = defaultdict(list)
        root_normalized = root.replace("\\", "/").rstrip("/")

        for file_path in files:
            normalized = file_path.replace("\\", "/")

            # Make relative to root
            if normalized.startswith(root_normalized):
                relative = normalized[len(root_normalized):].lstrip("/")
            else:
                relative = normalized

            # Get directory at max_depth
            parts = PurePosixPath(relative).parts
            if len(parts) <= 1:
                component_name = "<root>"
            else:
                component_name = "/".join(parts[:max_depth])

            clusters[component_name].append(file_path)

        return dict(clusters)

    def _find_entry_points(self, files: list[str]) -> list[str]:
        """Find exported symbols that serve as entry points."""
        entry_points: list[str] = []

        for file_path in files[:20]:  # Cap to avoid excessive queries
            nodes = self._reader.get_nodes_in_file(file_path)
            for node in nodes:
                if node.get("is_exported") and node.get("label") in (
                    "Function", "Class", "Method"
                ):
                    entry_points.append(node["name"])

        return entry_points[:20]  # Cap total entry points

    def _compute_confidence(self, files: list[str]) -> float:
        """Compute confidence in this component assignment.

        Higher confidence when:
        - Files share many internal imports
        - Few cross-component references
        """
        if len(files) <= 1:
            return 0.9  # Single-file components are trivially correct

        # Simple heuristic: confidence based on directory cohesion
        # More files in same dir = higher confidence
        dirs = Counter(os.path.dirname(f) for f in files)
        if len(dirs) == 1:
            return 0.95  # All in same directory
        if len(dirs) <= 3:
            return 0.80
        return 0.60
