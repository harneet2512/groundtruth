"""Output format contract mining.

Deterministic, $0 AI. Mines expected return shape from callers and test
assertions. Surfaces: "Callers expect keys X, Y, Z" or "Tests assert
result has attribute A."

Used by L3 post-edit to warn when an edit might change the output format
in a way that breaks callers or tests.
"""

from __future__ import annotations

import os
import re
import sqlite3

MIN_EDGE_CONFIDENCE = 0.5


def mine_return_shape(
    db_path: str,
    file_path: str,
    func_name: str,
    repo_root: str = "",
    *,
    issue_terms: set[str] | None = None,
) -> list[str]:
    """Mine expected return shape from callers and test assertions.

    Returns human-readable contract lines, or empty list.

    TASK #47 relevance gate: [FORMAT] is a non-edge signal (it is derived from
    caller subscripts / test assertions, not from a graph edge to the edited
    function), so the categorical edge filter never sees it. Caller code and
    fixtures routinely subscript an unrelated dict on the same line the call
    appears (e.g. ``result = f(...); cfg["path"]; os.environ["SKIP_SLOW_TESTS"]``),
    which mines keys that have nothing to do with this function's contract.

    Each mined [FORMAT] line is therefore gated through
    ``passes_relevance_gate``: it renders only when at least one mined key/attr
    overlaps the edited function's identifier tokens OR the issue terms.
    Correct-or-quiet — when nothing overlaps, the line is dropped rather than
    injected as noise. ``issue_terms`` is keyword-only and optional so the
    existing 4-positional-arg call site keeps working; the function-name token
    anchor is always available, so the gate is effective even with no issue
    terms supplied.
    """
    if not os.path.isfile(db_path):
        return []

    from groundtruth.config.evidence_markers import (
        identifier_tokens,
        passes_relevance_gate,
    )

    norm = file_path.replace("\\", "/").lstrip("./").lstrip("/")
    fn_tokens = identifier_tokens(func_name)
    issue_terms = issue_terms or set()
    evidence: list[str] = []

    def _gate_keys(items: list[str]) -> list[str]:
        """Keep only mined keys/attrs that overlap the edit's relevance anchor."""
        return [
            it
            for it in items
            if passes_relevance_gate(str(it), issue_terms, fn_tokens)
        ]

    try:
        conn = sqlite3.connect(db_path)

        caller_keys = _mine_caller_subscripts(conn, norm, func_name, repo_root)
        relevant_keys = _gate_keys(list(caller_keys))
        if relevant_keys:
            keys_str = ", ".join(f'"{k}"' for k in relevant_keys[:6])
            evidence.append(f"[FORMAT] Callers access keys: {keys_str}")

        caller_attrs = _mine_caller_attributes(conn, norm, func_name, repo_root)
        relevant_attrs = _gate_keys(list(caller_attrs))
        if relevant_attrs:
            attrs_str = ", ".join(f".{a}" for a in relevant_attrs[:6])
            evidence.append(f"[FORMAT] Callers access attributes: {attrs_str}")

        test_keys = _mine_test_assertions(conn, norm, func_name, repo_root)
        relevant_test_keys = _gate_keys(list(test_keys))
        if relevant_test_keys:
            keys_str = ", ".join(f'"{k}"' for k in relevant_test_keys[:6])
            evidence.append(f"[FORMAT] Tests assert keys: {keys_str}")

        conn.close()
    except (sqlite3.Error, OSError):
        pass

    return evidence


def _confidence_clause(conn: sqlite3.Connection, alias: str = "e") -> str:
    try:
        cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    except sqlite3.Error:
        return ""
    if any(row[1] == "confidence" for row in cols):
        return f" AND COALESCE({alias}.confidence, {MIN_EDGE_CONFIDENCE}) >= {MIN_EDGE_CONFIDENCE}"
    return ""


def _mine_caller_subscripts(
    conn: sqlite3.Connection,
    norm_path: str,
    func_name: str,
    repo_root: str,
) -> set[str]:
    """Find dict keys callers access on the return value."""
    keys: set[str] = set()
    try:
        conf_clause = _confidence_clause(conn)
        rows = conn.execute(
            f"""
            SELECT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path LIKE ?
              AND nsrc.file_path NOT LIKE ?
              {conf_clause}
            LIMIT 10
            """,
            (func_name, f"%{norm_path}", f"%{norm_path}"),
        ).fetchall()
    except sqlite3.Error:
        return keys

    for caller_file, source_line in rows:
        full_path = os.path.join(repo_root, caller_file) if repo_root else caller_file
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
            start = max(0, (source_line or 1) - 2)
            end = min(len(lines), (source_line or 1) + 8)
            context = "".join(lines[start:end])
            for m in re.finditer(r'\["([a-zA-Z_]\w*)"\]', context):
                keys.add(m.group(1))
            for m in re.finditer(r"\.get\(['\"]([a-zA-Z_]\w*)['\"]\)", context):
                keys.add(m.group(1))
        except OSError:
            continue
    return keys


def _mine_caller_attributes(
    conn: sqlite3.Connection,
    norm_path: str,
    func_name: str,
    repo_root: str,
) -> set[str]:
    """Find attributes callers access on the return value."""
    attrs: set[str] = set()
    try:
        conf_clause = _confidence_clause(conn)
        rows = conn.execute(
            f"""
            SELECT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path LIKE ?
              AND nsrc.file_path NOT LIKE ?
              {conf_clause}
            LIMIT 10
            """,
            (func_name, f"%{norm_path}", f"%{norm_path}"),
        ).fetchall()
    except sqlite3.Error:
        return attrs

    var_pattern = re.compile(
        rf"(\w+)\s*=\s*.*\b{re.escape(func_name)}\b.*\n"
        rf".*\1\.(\w+)",
        re.MULTILINE,
    )

    for caller_file, source_line in rows:
        full_path = os.path.join(repo_root, caller_file) if repo_root else caller_file
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for m in var_pattern.finditer(content):
                attr = m.group(2)
                if attr not in _SKIP_ATTRS:
                    attrs.add(attr)
        except OSError:
            continue
    return attrs


def _mine_test_assertions(
    conn: sqlite3.Connection,
    norm_path: str,
    func_name: str,
    repo_root: str,
) -> set[str]:
    """Find keys/attributes asserted in test files."""
    keys: set[str] = set()
    try:
        conf_clause = _confidence_clause(conn)
        rows = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path LIKE ? AND nsrc.is_test = 1
              {conf_clause}
            LIMIT 5
            """,
            (f"%{norm_path}",),
        ).fetchall()
    except sqlite3.Error:
        return keys

    for (test_file,) in rows:
        full_path = os.path.join(repo_root, test_file) if repo_root else test_file
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for m in re.finditer(r'assert.*\["([a-zA-Z_]\w*)"\]', content):
                keys.add(m.group(1))
            for m in re.finditer(r'assert.*\.get\(["\']([a-zA-Z_]\w*)["\']', content):
                keys.add(m.group(1))
            for m in re.finditer(r'"([a-zA-Z_]\w*)"\s*in\s+\w+', content):
                keys.add(m.group(1))
        except OSError:
            continue
    return keys


_SKIP_ATTRS = frozenset({
    "items", "keys", "values", "get", "pop", "update", "copy",
    "append", "extend", "insert", "remove", "sort", "reverse",
    "strip", "split", "join", "replace", "lower", "upper",
    "encode", "decode", "format", "startswith", "endswith",
    "__init__", "__str__", "__repr__", "__eq__", "__hash__",
})
