"""Semantic check: compare guard clauses + return paths before/after an edit.

Runs inside the SWE-bench container. Standalone CLI so the wrapper can call it
without fragile shell-escaped one-liners.

Usage:
    python3 -m groundtruth.hooks.semantic_check \
        --file=loguru/_colorama.py --workspace=/workspace
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def extract_guards(text: str) -> set[str]:
    """Extract guard conditions from Python-like source text.

    A guard is an `if <condition>:` followed within 200 chars by
    return/raise/throw — i.e., an early-exit pattern.
    """
    guards: set[str] = set()
    for m in re.finditer(r"if\s+(.{3,80})\s*:", text):
        cond = m.group(1).strip()[:60]
        region = text[m.start() : m.start() + 200]
        if any(kw in region for kw in ("return", "raise", "throw")):
            guards.add(cond)
    return guards


def extract_return_paths(text: str) -> list[str]:
    """Extract all `return ...` statements."""
    paths = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("return ") or stripped == "return":
            paths.append(stripped[:60])
    return paths


def get_old_content(file_path: str, workspace: str) -> str:
    """Get the pre-edit version via git show HEAD:<file>."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{file_path}"],
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=10,
        )
        return result.stdout[:4000]
    except Exception:
        return ""


def get_new_content(file_path: str, workspace: str) -> str:
    """Read the current (post-edit) file."""
    full_path = Path(workspace) / file_path
    try:
        return full_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return ""


def run_check(file_path: str, workspace: str) -> list[str]:
    """Compare guards + returns before/after. Returns list of findings."""
    old = get_old_content(file_path, workspace)
    new = get_new_content(file_path, workspace)

    if not new:
        return []

    old_guards = extract_guards(old)
    new_guards = extract_guards(new)

    added = new_guards - old_guards
    removed = old_guards - new_guards

    lines: list[str] = []
    for g in sorted(added)[:3]:
        lines.append(f"GUARD_ADDED:{g}")
    for g in sorted(removed)[:3]:
        lines.append(f"GUARD_REMOVED:{g}")
    if added or removed:
        for r in extract_return_paths(new)[:5]:
            lines.append(f"RETURN_PATH:{r}")

    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Relative file path")
    parser.add_argument("--workspace", required=True, help="Repo root")
    args = parser.parse_args()

    findings = run_check(args.file, args.workspace)
    for line in findings:
        print(line)


if __name__ == "__main__":
    main()
