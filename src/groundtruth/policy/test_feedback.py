"""Bounded test-feedback policy — find relevant tests for changed files.

Given changed files, identify the most relevant test file(s) and produce a
workflow nudge. This is a POLICY module: it suggests tests, never executes them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath

_TRIVIAL_DIRS = {"src", "tests", "test", "unit", "lib"}


@dataclass(frozen=True)
class TestTarget:
    """A test file matched to a changed source file."""

    test_file: str       # path to the test file
    source_file: str     # the changed file it corresponds to
    match_type: str      # "name_convention" | "import_match" | "directory_match"
    confidence: float    # 0.0-1.0


def _stem(path: str) -> str:
    return PurePosixPath(path).stem


def _normalize(path: str) -> str:
    return path.replace(os.sep, "/")


def _parts(path: str) -> list[str]:
    return _normalize(path).split("/")


class TestFeedbackPolicy:
    """Suggest which tests to run for a set of changed files.

    All matching is deterministic and stdlib-only.
    """

    def __init__(
        self,
        max_test_files: int = 1,
        max_repair_attempts: int = 1,
        output_truncate_lines: int = 50,
    ) -> None:
        self.max_test_files = max_test_files
        self.max_repair_attempts = max_repair_attempts
        self.output_truncate_lines = output_truncate_lines

    def find_relevant_tests(
        self,
        changed_files: list[str],
        available_test_files: list[str],
    ) -> list[TestTarget]:
        """Given changed files, find the most relevant test files."""
        targets: list[TestTarget] = []
        available_norm = {_normalize(t): t for t in available_test_files}

        for src in changed_files:
            src_norm = _normalize(src)
            stem = _stem(src_norm)
            src_parts = _parts(src_norm)
            best: TestTarget | None = None

            for test_norm, test_orig in available_norm.items():
                test_stem = _stem(test_norm)
                test_parts = _parts(test_norm)
                shared = set(src_parts[:-1]) & set(test_parts[:-1])

                # Priority 1: name convention — test_<stem>.py
                if test_stem == f"test_{stem}":
                    conf = 0.95 if shared - _TRIVIAL_DIRS else 0.9
                    candidate = TestTarget(test_orig, src, "name_convention", conf)
                    if best is None or candidate.confidence > best.confidence:
                        best = candidate
                # Priority 2: directory match — shared meaningful subdir
                elif best is None:
                    meaningful = shared - _TRIVIAL_DIRS
                    if meaningful:
                        best = TestTarget(test_orig, src, "directory_match", 0.5)

            if best is not None:
                targets.append(best)

        targets.sort(key=lambda t: t.confidence, reverse=True)
        return targets[: self.max_test_files]

    def format_test_nudge(self, targets: list[TestTarget]) -> str | None:
        """Format a workflow nudge suggesting which tests to run.
        Returns None if no tests found.
        """
        if not targets:
            return None
        return "\n".join(
            f"\u2192 Run: pytest {t.test_file} -x (tests {t.source_file})"
            for t in targets
        )

    def truncate_output(self, output: str) -> str:
        """Truncate test output to configured line limit."""
        lines = output.split("\n")
        limit = self.output_truncate_lines
        if len(lines) <= limit:
            return output
        head_count = 10
        tail_count = limit - head_count
        skipped = len(lines) - head_count - tail_count
        return "\n".join(
            lines[:head_count]
            + [f"... (truncated {skipped} lines) ..."]
            + lines[-tail_count:]
        )
