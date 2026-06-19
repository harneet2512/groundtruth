"""Gold file + function extraction from SWE-Bench Lite unified-diff patches.

File-level extraction reuses the diff-walking approach in
scripts.swebench.compute_localization_metrics.gold_files. Function-level
extraction maps touched new-side line numbers onto Function/Method nodes
in a per-(repo, commit) graph.db.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


def _strip_git_prefix(path: str) -> str:
    if path.startswith("a/"):
        return path[2:]
    if path.startswith("b/"):
        return path[2:]
    return path


def gold_files_from_patch(patch: str) -> set[str]:
    """Return the set of files modified by a unified-diff patch."""
    files: set[str] = set()
    for ln in (patch or "").splitlines():
        if ln.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(ln)
            if m:
                files.add(_strip_git_prefix(m.group(2)))
                continue
            parts = ln.split()
            if len(parts) >= 3:
                files.add(_strip_git_prefix(parts[2]))
    return files


def _walk_hunks_new_side(
    patch: str,
) -> dict[str, set[int]]:
    """For each modified file, return the set of new-side line numbers
    that were added or were the target of a deletion (i.e. lines whose
    content in the new file is the result of an edit).

    A `+` line consumes one new-side line number and is recorded.
    A `-` line consumes a old-side line number; we record the current
    new-side cursor as the "anchor" line so that pure deletions still
    map to a hunk neighborhood in the new file.
    A context (` `) line consumes both sides and is not recorded.
    """
    touched: dict[str, set[int]] = {}
    current_file: str | None = None
    new_cursor = 0
    for ln in (patch or "").splitlines():
        if ln.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(ln)
            if m:
                current_file = _strip_git_prefix(m.group(2))
            else:
                parts = ln.split()
                current_file = _strip_git_prefix(parts[2]) if len(parts) >= 3 else None
            new_cursor = 0
            continue
        if ln.startswith("@@"):
            mh = _HUNK_HEADER_RE.match(ln)
            if mh and current_file is not None:
                new_start = int(mh.group(3))
                new_cursor = new_start
            continue
        if current_file is None:
            continue
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            touched.setdefault(current_file, set()).add(new_cursor)
            new_cursor += 1
        elif ln.startswith("-"):
            anchor = max(new_cursor - 1, 1)
            touched.setdefault(current_file, set()).add(anchor)
        elif ln.startswith(" "):
            new_cursor += 1
        else:
            continue
    return touched


def _query_function_for_line(
    conn: sqlite3.Connection, file_path: str, line: int
) -> list[str]:
    cur = conn.execute(
        "SELECT name FROM nodes "
        "WHERE file_path = ? AND label IN ('Function','Method') "
        "AND start_line <= ? AND end_line >= ?",
        (file_path, line, line),
    )
    return [row[0] for row in cur.fetchall() if row and row[0]]


def gold_functions_from_patch(
    patch: str,
    graph_db_path: str,
) -> set[tuple[str, str]]:
    """Map touched new-side lines onto Function/Method nodes in graph.db."""
    if not os.path.isfile(graph_db_path):
        print(
            f"gold_extractor: graph.db not found: {graph_db_path}",
            file=sys.stderr,
        )
        return set()
    touched = _walk_hunks_new_side(patch)
    if not touched:
        return set()
    out: set[tuple[str, str]] = set()
    try:
        conn = sqlite3.connect(graph_db_path)
    except sqlite3.Error as e:
        print(f"gold_extractor: sqlite open failed: {e}", file=sys.stderr)
        return set()
    try:
        for file_path, lines in touched.items():
            for line in sorted(lines):
                names = _query_function_for_line(conn, file_path, line)
                for name in names:
                    out.add((file_path, name))
    finally:
        conn.close()
    return out


def extract_gold(
    instance: dict[str, Any],
    graph_db_path: str | None,
) -> dict[str, Any]:
    patch = instance.get("patch") or ""
    files = gold_files_from_patch(patch)
    if graph_db_path is None:
        return {"files": files, "functions": set()}
    try:
        funcs = gold_functions_from_patch(patch, graph_db_path)
    except Exception as e:
        print(f"gold_extractor: function extraction failed: {e}", file=sys.stderr)
        funcs = set()
    return {"files": files, "functions": funcs}
