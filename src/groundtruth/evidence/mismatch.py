"""Post-edit mismatch detection.

Deterministic, $0 AI. After an agent edits a function, checks whether
tests or callers still reference OLD behavior that the edit changed.

Surfaces warnings like: "You edited set_url, but tests still assert old_url=."
"""

from __future__ import annotations

import os
import re
import sqlite3

MIN_EDGE_CONFIDENCE = 0.5


def detect_stale_references(
    db_path: str,
    file_path: str,
    func_name: str,
    diff_text: str,
    repo_root: str = "",
) -> list[str]:
    """Find test/caller references to identifiers REMOVED by the diff.

    Returns human-readable warning strings, or empty list if no mismatch.
    """
    if not diff_text or not os.path.isfile(db_path):
        return []

    removed_ids = _extract_removed_identifiers(diff_text)
    if not removed_ids:
        return []

    warnings: list[str] = []

    try:
        conn = sqlite3.connect(db_path)
        norm = file_path.replace("\\", "/").lstrip("./").lstrip("/")

        test_refs = _find_test_references(conn, norm, func_name, removed_ids, repo_root)
        for test_file, test_line, matched_id in test_refs[:3]:
            warnings.append(
                f"[MISMATCH] You removed `{matched_id}` but "
                f"{test_file}:{test_line} still references it"
            )

        caller_refs = _find_caller_references(conn, norm, func_name, removed_ids, repo_root)
        for caller_file, caller_line, matched_id in caller_refs[:3]:
            warnings.append(
                f"[MISMATCH] You removed `{matched_id}` but "
                f"caller {caller_file}:{caller_line} still passes it"
            )

        conn.close()
    except (sqlite3.Error, OSError):
        pass

    return warnings


def _confidence_clause(conn: sqlite3.Connection, alias: str = "e") -> str:
    try:
        cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    except sqlite3.Error:
        return ""
    if any(row[1] == "confidence" for row in cols):
        return f" AND COALESCE({alias}.confidence, {MIN_EDGE_CONFIDENCE}) >= {MIN_EDGE_CONFIDENCE}"
    return ""


def _extract_removed_identifiers(diff_text: str) -> list[str]:
    """Extract identifiers from removed lines (lines starting with -)."""
    removed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            for m in re.finditer(r"\b([a-zA-Z_]\w{2,})\b", line):
                w = m.group(1)
                if w not in _COMMON_KEYWORDS:
                    removed.append(w)
    added: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for m in re.finditer(r"\b([a-zA-Z_]\w{2,})\b", line):
                added.append(m.group(1))
    added_set = set(added)
    return [r for r in dict.fromkeys(removed) if r not in added_set]


def _find_test_references(
    conn: sqlite3.Connection,
    norm_path: str,
    func_name: str,
    removed_ids: list[str],
    repo_root: str,
) -> list[tuple[str, int, str]]:
    """Find test files that reference removed identifiers."""
    results: list[tuple[str, int, str]] = []
    try:
        conf_clause = _confidence_clause(conn)
        rows = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path LIKE ? AND nsrc.is_test = 1
              {conf_clause}
            LIMIT 10
            """,
            (f"%{norm_path}",),
        ).fetchall()
    except sqlite3.Error:
        return results

    for test_file, test_line in rows:
        full_path = os.path.join(repo_root, test_file) if repo_root else test_file
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for rid in removed_ids:
                if rid in content:
                    line_no = test_line or 0
                    for i, line in enumerate(content.splitlines(), 1):
                        if rid in line and ("assert" in line.lower() or "mock" in line.lower()):
                            line_no = i
                            break
                    results.append((test_file, line_no, rid))
                    break
        except OSError:
            continue
    return results


def _find_caller_references(
    conn: sqlite3.Connection,
    norm_path: str,
    func_name: str,
    removed_ids: list[str],
    repo_root: str,
) -> list[tuple[str, int, str]]:
    """Find caller files that pass removed identifiers as arguments."""
    results: list[tuple[str, int, str]] = []
    try:
        conf_clause = _confidence_clause(conn)
        rows = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path LIKE ?
              AND nsrc.file_path NOT LIKE ?
              AND nsrc.is_test = 0
              {conf_clause}
            LIMIT 10
            """,
            (func_name, f"%{norm_path}", f"%{norm_path}"),
        ).fetchall()
    except sqlite3.Error:
        return results

    for caller_file, source_line in rows:
        full_path = os.path.join(repo_root, caller_file) if repo_root else caller_file
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
            start = max(0, (source_line or 1) - 3)
            end = min(len(lines), (source_line or 1) + 3)
            context = "".join(lines[start:end])
            for rid in removed_ids:
                if rid in context:
                    results.append((caller_file, source_line or 0, rid))
                    break
        except OSError:
            continue
    return results


_COMMON_KEYWORDS = frozenset({
    "def", "class", "return", "import", "from", "self", "None", "True",
    "False", "not", "and", "for", "while", "with", "try", "except",
    "raise", "pass", "break", "continue", "yield", "lambda", "elif",
    "else", "finally", "assert", "del", "global", "nonlocal", "async",
    "await", "print", "len", "str", "int", "float", "bool", "list",
    "dict", "set", "tuple", "type", "isinstance", "super", "range",
    "open", "close", "read", "write", "append", "extend", "update",
})
