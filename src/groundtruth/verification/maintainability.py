"""Maintainability Checker — structural legality and code quality heuristics.

Soft concerns that should not veto a patch on their own, but contribute
to the overall score and may generate warnings.
"""

from __future__ import annotations

import re

from groundtruth.verification.models import PatchCandidate, ViolationRecord


class MaintainabilityChecker:
    """Checks maintainability heuristics on a candidate patch."""

    def check(
        self, candidate: PatchCandidate
    ) -> tuple[float, list[ViolationRecord]]:
        """Check maintainability heuristics.

        Returns (score, violations) where all violations are 'soft' severity.
        These should NEVER veto a patch on their own.
        """
        violations: list[ViolationRecord] = []
        deductions = 0.0

        # Check 1: Large function additions (>50 lines in a single function)
        if self._has_large_function(candidate.diff):
            violations.append(ViolationRecord(
                contract_id=0,
                contract_type="maintainability",
                predicate="Functions should be <50 lines",
                severity="soft",
                explanation="Patch introduces a function >50 lines",
            ))
            deductions += 0.1

        # Check 2: Deep nesting (>4 levels)
        if self._has_deep_nesting(candidate.diff):
            violations.append(ViolationRecord(
                contract_id=0,
                contract_type="maintainability",
                predicate="Nesting depth should be ≤4",
                severity="soft",
                explanation="Patch introduces deeply nested code (>4 levels)",
            ))
            deductions += 0.1

        # Check 3: Removed exports with potential callers
        removed_exports = self._count_removed_exports(candidate.diff)
        if removed_exports > 0:
            violations.append(ViolationRecord(
                contract_id=0,
                contract_type="maintainability",
                predicate="Exported symbols should not be removed without migration",
                severity="soft",
                explanation=f"Removed {removed_exports} exported symbol(s)",
            ))
            deductions += min(0.2, removed_exports * 0.05)

        score = max(0.0, 1.0 - deductions)
        return score, violations

    def _has_large_function(self, diff: str) -> bool:
        """Check if the diff adds a function with >50 lines."""
        added_lines = [
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]

        # Simple heuristic: count consecutive added lines after a def
        in_function = False
        function_lines = 0

        for line in added_lines:
            if re.match(r"\s*def\s+", line):
                in_function = True
                function_lines = 0
            elif in_function:
                if line.strip() == "" or re.match(r"\s*def\s+", line):
                    if function_lines > 50:
                        return True
                    if re.match(r"\s*def\s+", line):
                        function_lines = 0
                    else:
                        in_function = False
                else:
                    function_lines += 1

        return function_lines > 50

    def _has_deep_nesting(self, diff: str) -> bool:
        """Check if the diff adds code with >4 indent levels."""
        added_lines = [
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]

        for line in added_lines:
            stripped = line.lstrip()
            if not stripped:
                continue
            indent = len(line) - len(stripped)
            # Assume 4-space indent = 1 level
            levels = indent // 4
            if levels > 4:
                return True

        return False

    def _count_removed_exports(self, diff: str) -> int:
        """Count exported symbols removed by the diff."""
        removed_lines = [
            line[1:] for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]

        count = 0
        for line in removed_lines:
            # Python: def/class at module level (no indent)
            if re.match(r"^(def|class)\s+[A-Z]", line):
                count += 1
            # Go: exported (capitalized) function
            elif re.match(r"^func\s+[A-Z]", line):
                count += 1

        return count
