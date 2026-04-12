"""Coupling Rules — detects non-code dependencies between files.

Types of coupling:
- config: source files reference keys defined in config files
- doc: function names appear in documentation files
- registry: symbols registered in __init__.py, plugin registries, URL configs
- build: build manifests reference source files

All edges are typed and confidence-scored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from groundtruth.repo_intel.models import CouplingEdge
from groundtruth.substrate.protocols import GraphReader


# File patterns for each coupling type
_CONFIG_PATTERNS = ("*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg", "*.env", "*.json")
_DOC_PATTERNS = ("*.md", "*.rst", "*.txt")
_REGISTRY_PATTERNS = ("__init__.py", "urls.py", "routes.*", "registry.*", "plugin.*")


class CouplingExtractor:
    """Extracts typed coupling edges between files."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    def extract(self, root: str) -> list[CouplingEdge]:
        """Extract all coupling edges for the repository."""
        edges: list[CouplingEdge] = []
        all_files = self._reader.get_file_paths()

        # Get all symbol names for matching
        source_files = [f for f in all_files if self._is_source_file(f)]
        symbol_names = self._get_symbol_names(source_files)

        root_path = Path(root)

        # Config coupling
        edges.extend(self._find_config_coupling(root_path, symbol_names, source_files))

        # Doc coupling
        edges.extend(self._find_doc_coupling(root_path, symbol_names, source_files))

        # Registry coupling
        edges.extend(self._find_registry_coupling(all_files, symbol_names))

        return edges

    def _find_config_coupling(
        self,
        root: Path,
        symbol_names: dict[str, str],
        source_files: list[str],
    ) -> list[CouplingEdge]:
        """Find config files that reference source symbols."""
        edges: list[CouplingEdge] = []

        for pattern in _CONFIG_PATTERNS:
            for config_path in root.rglob(pattern):
                if "node_modules" in str(config_path):
                    continue
                try:
                    content = config_path.read_text(errors="ignore")
                except OSError:
                    continue

                for name, source_file in symbol_names.items():
                    if len(name) < 4:  # Skip very short names (noise)
                        continue
                    if name in content:
                        edges.append(CouplingEdge(
                            source_file=str(config_path),
                            target_file=source_file,
                            coupling_type="config",
                            confidence=0.70,
                            detail=f"Config references '{name}'",
                        ))

        return edges[:50]  # Cap to avoid explosion

    def _find_doc_coupling(
        self,
        root: Path,
        symbol_names: dict[str, str],
        source_files: list[str],
    ) -> list[CouplingEdge]:
        """Find documentation files that mention source symbols."""
        edges: list[CouplingEdge] = []

        for pattern in _DOC_PATTERNS:
            for doc_path in root.rglob(pattern):
                if "node_modules" in str(doc_path):
                    continue
                try:
                    content = doc_path.read_text(errors="ignore")
                except OSError:
                    continue

                for name, source_file in symbol_names.items():
                    if len(name) < 5:  # Skip short names in docs
                        continue
                    # Look for backtick-quoted references or exact matches
                    if f"`{name}`" in content or f"``{name}``" in content:
                        edges.append(CouplingEdge(
                            source_file=str(doc_path),
                            target_file=source_file,
                            coupling_type="doc",
                            confidence=0.80,
                            detail=f"Docs reference `{name}`",
                        ))

        return edges[:50]

    def _find_registry_coupling(
        self,
        all_files: list[str],
        symbol_names: dict[str, str],
    ) -> list[CouplingEdge]:
        """Find registry files that register symbols."""
        edges: list[CouplingEdge] = []

        for file_path in all_files:
            basename = os.path.basename(file_path)
            if not any(
                basename == p or re.match(p.replace("*", ".*"), basename)
                for p in _REGISTRY_PATTERNS
            ):
                continue

            # Check what symbols this registry file imports/references
            nodes = self._reader.get_nodes_in_file(file_path)
            for node in nodes:
                callees = self._reader.get_callees(node.get("id", 0))
                for callee in callees:
                    target_file = callee.get("target_file_path", "")
                    if target_file and target_file != file_path:
                        edges.append(CouplingEdge(
                            source_file=file_path,
                            target_file=target_file,
                            coupling_type="registry",
                            confidence=0.85,
                            detail=f"Registry imports from {os.path.basename(target_file)}",
                        ))

        return edges[:50]

    def _get_symbol_names(self, source_files: list[str]) -> dict[str, str]:
        """Build a dict of exported symbol names → file paths."""
        symbols: dict[str, str] = {}
        for file_path in source_files[:200]:  # Cap for performance
            nodes = self._reader.get_nodes_in_file(file_path)
            for node in nodes:
                if node.get("is_exported") and node.get("label") in (
                    "Function", "Class", "Method"
                ):
                    symbols[node["name"]] = file_path
        return symbols

    def _is_source_file(self, file_path: str) -> bool:
        """Check if a file is source code (not config/docs/test)."""
        ext = os.path.splitext(file_path)[1].lower()
        return ext in (
            ".py", ".go", ".js", ".ts", ".jsx", ".tsx",
            ".java", ".kt", ".rs", ".rb", ".php", ".cs",
            ".swift", ".scala", ".c", ".cpp", ".h",
        )
