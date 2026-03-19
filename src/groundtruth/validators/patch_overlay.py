"""PatchOverlayBuilder — production version with Pydantic dataclasses.

Parses unified diffs into structured overlays for certainty-lane gating.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from groundtruth.utils.levenshtein import levenshtein_distance


# Patterns for extracting definitions
_DEF_RE = re.compile(
    r"^(?:\s*)"
    r"(?:(?:class|def|async\s+def)\s+([A-Za-z_]\w*)"
    r"|([A-Z]\w*)\s*=)"
)
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(\S+)\s+import\s+(.+)|import\s+(.+))"
)


@dataclass
class PatchOverlay:
    """Structured representation of what a diff introduces and removes."""

    added_definitions: set[str] = field(default_factory=set)
    removed_definitions: set[str] = field(default_factory=set)
    renames: dict[str, str] = field(default_factory=dict)  # {new_name: old_name}
    added_imports: set[str] = field(default_factory=set)
    removed_imports: set[str] = field(default_factory=set)
    added_lines: dict[str, set[int]] = field(default_factory=dict)  # {file: {line_numbers}}
    changed_files: list[str] = field(default_factory=list)


def _extract_definitions(lines: list[str]) -> set[str]:
    """Extract names defined in a set of source lines."""
    defs: set[str] = set()
    for line in lines:
        m = _DEF_RE.match(line)
        if m:
            name = m.group(1) or m.group(2)
            if name:
                defs.add(name)
    return defs


def _extract_imports(lines: list[str]) -> set[str]:
    """Extract imported names from source lines."""
    imports: set[str] = set()
    for line in lines:
        m = _IMPORT_RE.match(line)
        if not m:
            continue
        if m.group(1):  # from X import Y, Z
            names_part = m.group(2)
            for name in names_part.split(","):
                name = name.strip()
                if " as " in name:
                    name = name.split(" as ")[-1].strip()
                if name and name != "*":
                    imports.add(name)
        elif m.group(3):  # import X, Y
            for name in m.group(3).split(","):
                name = name.strip()
                if " as " in name:
                    name = name.split(" as ")[-1].strip()
                if name:
                    imports.add(name.split(".")[-1])
    return imports


def _detect_renames(
    added: set[str], removed: set[str], max_dist: int = 3
) -> dict[str, str]:
    """Detect renames: {new_name: old_name} via Levenshtein ≤ max_dist."""
    renames: dict[str, str] = {}
    used_removed: set[str] = set()
    for new_name in added:
        best: str | None = None
        best_dist = max_dist + 1
        for old_name in removed:
            if old_name in used_removed:
                continue
            d = levenshtein_distance(new_name, old_name)
            if d <= max_dist and d < best_dist:
                best = old_name
                best_dist = d
        if best is not None:
            renames[new_name] = best
            used_removed.add(best)
    return renames


class PatchOverlayBuilder:
    """Builds a PatchOverlay from unified diff text."""

    @staticmethod
    def build(diff_text: str) -> PatchOverlay:
        """Parse a unified diff into a PatchOverlay."""
        added_source_lines: list[str] = []
        removed_source_lines: list[str] = []
        added_import_lines: list[str] = []
        removed_import_lines: list[str] = []
        added_lines: dict[str, set[int]] = {}
        changed_files: list[str] = []

        current_file: str | None = None
        current_new_line = 0

        for raw_line in diff_text.splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = raw_line[6:]
                if current_file not in changed_files:
                    changed_files.append(current_file)
            elif raw_line.startswith("@@ ") and current_file:
                m = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
                if m:
                    current_new_line = int(m.group(1))
                else:
                    current_new_line = 0
            elif current_file and raw_line.startswith("+") and not raw_line.startswith("+++"):
                content = raw_line[1:]
                added_source_lines.append(content)
                if _IMPORT_RE.match(content.strip()):
                    added_import_lines.append(content)
                if current_file not in added_lines:
                    added_lines[current_file] = set()
                added_lines[current_file].add(current_new_line)
                current_new_line += 1
            elif current_file and raw_line.startswith("-") and not raw_line.startswith("---"):
                content = raw_line[1:]
                removed_source_lines.append(content)
                if _IMPORT_RE.match(content.strip()):
                    removed_import_lines.append(content)
            elif not raw_line.startswith("\\"):
                if (
                    current_file
                    and not raw_line.startswith("diff ")
                    and not raw_line.startswith("index ")
                ):
                    current_new_line += 1

        added_defs = _extract_definitions(added_source_lines)
        removed_defs = _extract_definitions(removed_source_lines)
        renames = _detect_renames(added_defs, removed_defs)

        return PatchOverlay(
            added_definitions=added_defs,
            removed_definitions=removed_defs,
            renames=renames,
            added_imports=_extract_imports(added_import_lines),
            removed_imports=_extract_imports(removed_import_lines),
            added_lines=added_lines,
            changed_files=changed_files,
        )
