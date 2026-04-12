"""Test Topology — maps test files to source modules they exercise.

Builds a directed graph: test_file → exercises → [source_files/symbols].
Answers: "given this changed file, which tests should run?"

Strategy:
1. Import analysis: test file imports from source → direct edge
2. Naming conventions: test_foo.py → foo.py
3. Directory structure: tests/unit/test_X.py → src/X.py
"""

from __future__ import annotations

import os
import re

from groundtruth.repo_intel.models import TestEdge
from groundtruth.substrate.protocols import GraphReader


class TestTopologyExtractor:
    """Extracts test → source mappings from the code graph."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    def extract(self, root: str) -> list[TestEdge]:
        """Extract test topology for the repository."""
        edges: list[TestEdge] = []
        all_files = self._reader.get_file_paths()

        # Separate test files from source files
        test_files = [f for f in all_files if self._is_test_file(f)]
        source_files = [f for f in all_files if not self._is_test_file(f)]

        # Build source file lookup by basename
        source_by_name: dict[str, list[str]] = {}
        for sf in source_files:
            basename = os.path.basename(sf).rsplit(".", 1)[0]
            source_by_name.setdefault(basename, []).append(sf)

        for test_file in test_files:
            matched = self._match_test_to_source(
                test_file, source_files, source_by_name
            )
            for source_file, confidence, relationship in matched:
                # Get symbols from both files
                test_nodes = self._reader.get_nodes_in_file(test_file)
                source_nodes = self._reader.get_nodes_in_file(source_file)

                test_symbols = tuple(
                    n["name"] for n in test_nodes if n.get("is_test")
                )
                source_symbols = tuple(
                    n["name"] for n in source_nodes
                    if n.get("label") in ("Function", "Method", "Class")
                )

                edges.append(TestEdge(
                    test_file=test_file,
                    source_file=source_file,
                    test_symbols=test_symbols[:10],  # Cap for sanity
                    source_symbols=source_symbols[:10],
                    confidence=confidence,
                    relationship=relationship,
                ))

        return edges

    def _match_test_to_source(
        self,
        test_file: str,
        source_files: list[str],
        source_by_name: dict[str, list[str]],
    ) -> list[tuple[str, float, str]]:
        """Match a test file to source files it exercises.

        Returns list of (source_file, confidence, relationship_type).
        """
        matches: list[tuple[str, float, str]] = []

        # Strategy 1: Import analysis (highest confidence)
        test_nodes = self._reader.get_nodes_in_file(test_file)
        for node in test_nodes:
            callees = self._reader.get_callees(node.get("id", 0))
            for callee in callees:
                target_file = callee.get("target_file_path", "")
                if target_file and not self._is_test_file(target_file):
                    matches.append((target_file, 0.95, "direct"))

        # Strategy 2: Naming conventions
        basename = os.path.basename(test_file).rsplit(".", 1)[0]
        # test_foo.py → foo
        source_name = re.sub(r"^test_?", "", basename)
        if source_name in source_by_name:
            for sf in source_by_name[source_name]:
                if (sf, 0.95, "direct") not in matches:
                    matches.append((sf, 0.80, "naming_convention"))

        # Deduplicate by source_file (keep highest confidence)
        seen: dict[str, tuple[float, str]] = {}
        for sf, conf, rel in matches:
            if sf not in seen or conf > seen[sf][0]:
                seen[sf] = (conf, rel)

        return [(sf, conf, rel) for sf, (conf, rel) in seen.items()]

    def _is_test_file(self, file_path: str) -> bool:
        """Determine if a file is a test file."""
        basename = os.path.basename(file_path).lower()
        parts = file_path.replace("\\", "/").lower().split("/")

        # Common test directory patterns
        if any(p in ("test", "tests", "spec", "specs", "__tests__") for p in parts):
            return True

        # Common test file patterns
        if basename.startswith("test_") or basename.endswith("_test.py"):
            return True
        if basename.endswith(".test.ts") or basename.endswith(".test.js"):
            return True
        if basename.endswith("_test.go"):
            return True
        if basename.endswith("Test.java") or basename.endswith("Test.kt"):
            return True

        return False
