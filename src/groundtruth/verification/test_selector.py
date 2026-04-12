"""Test Selector — chooses targeted tests for a change.

Given changed symbols, queries the graph to find tests that exercise
those symbols. Ranks by relevance: direct > transitive > sibling.
"""

from __future__ import annotations

from groundtruth.substrate.protocols import GraphReader


class TestSelector:
    """Selects targeted tests for changed symbols."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    def select(
        self,
        changed_symbols: list[str],
        changed_files: list[str],
        max_tests: int = 10,
    ) -> list[str]:
        """Select test files relevant to the change.

        Strategy:
        1. Direct tests: test nodes that call changed symbols
        2. File-level tests: test files that import changed files
        3. Sibling tests: tests in same directory as changed files

        Returns test file paths ranked by relevance, up to max_tests.
        """
        ranked: list[tuple[str, int]] = []  # (file_path, relevance_score)
        seen: set[str] = set()

        # 1. Direct tests for changed symbols
        for symbol_name in changed_symbols:
            node = self._reader.get_node_by_name(symbol_name)
            if not node:
                continue

            tests = [
                test for test in self._reader.get_tests_for(node["id"])
                if test.get("_resolution") in {"call_graph", "assertion_target"}
            ]
            for test in tests:
                file_path = test.get("file_path", "")
                if file_path and file_path not in seen:
                    ranked.append((file_path, 3))  # Highest relevance
                    seen.add(file_path)

        # 2. File-level: tests in the same file or importing changed files
        for file_path in changed_files:
            nodes = self._reader.get_nodes_in_file(file_path)
            for node in nodes:
                if not node.get("is_test"):
                    continue
                test_file = node.get("file_path", "")
                if test_file and test_file not in seen:
                    ranked.append((test_file, 2))
                    seen.add(test_file)

        # 3. Sibling test files (same directory, test_* prefix)
        for file_path in changed_files:
            dir_path = "/".join(file_path.replace("\\", "/").split("/")[:-1])
            if not dir_path:
                continue
            # Look for test files in the graph that share the directory
            all_files = self._reader.get_file_paths()
            for f in all_files:
                normalized = f.replace("\\", "/")
                if (
                    normalized.startswith(dir_path)
                    and "/test" in normalized
                    and f not in seen
                ):
                    ranked.append((f, 1))
                    seen.add(f)

        # Sort by relevance (descending) and return top N
        ranked.sort(key=lambda x: -x[1])
        return [path for path, _ in ranked[:max_tests]]
