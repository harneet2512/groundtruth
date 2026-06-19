"""Post-edit mismatch detection.

Deterministic, $0 AI. After an agent edits a function, checks whether
tests or callers still reference OLD behavior that the edit changed.

Surfaces warnings like: "You edited set_url, but tests still assert old_url=."
"""

from __future__ import annotations

import builtins
import os
import re
import sqlite3
import sys

MIN_EDGE_CONFIDENCE = 0.5

# Names that are part of the language/stdlib surface, NOT agent-introduced
# symbols. A `-` diff line that drops one of these (e.g. `from datetime import
# timezone`, an `import subprocess`, or a `len(...)` call) is never a meaningful
# "the agent removed a symbol other code depends on" signal — a test importing
# `timezone` for its OWN use is not asserting on the edited function. Computed
# from the WHOLE stdlib (sys.stdlib_module_names) + every builtin, so it
# generalizes across the language and never hardcodes specific names.
_STDLIB_AND_BUILTINS: frozenset[str] = frozenset(
    set(getattr(sys, "stdlib_module_names", frozenset()))
    | set(dir(builtins))
)


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

    # Defense (2): only warn on identifiers that are GENUINELY GONE — absent
    # from the post-edit edited file content (the diff's '+' side already
    # filtered in _extract_removed_identifiers; here we also consult the live
    # file on disk so a name moved to another line of the same file, or still
    # imported/used elsewhere in it, is not mis-reported as removed). If we
    # cannot read the post-edit file we keep the diff-only result (the '+' side
    # was already honored) rather than fabricate a stronger claim.
    post_edit_content = _read_post_edit_content(file_path, repo_root)
    if post_edit_content is not None:
        removed_ids = [
            rid for rid in removed_ids
            if not _name_present(rid, post_edit_content)
        ]
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
    """Extract identifiers from removed lines (lines starting with -).

    Defense (1): drop language/stdlib/builtin names (computed from the whole
    stdlib + builtins, never a hardcoded list). A `-` line dropping `timezone`,
    `subprocess`, or `len` is not an agent-introduced removal worth warning on.
    """
    removed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            for m in re.finditer(r"\b([a-zA-Z_]\w{2,})\b", line):
                w = m.group(1)
                if w not in _COMMON_KEYWORDS and w not in _STDLIB_AND_BUILTINS:
                    removed.append(w)
    added: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for m in re.finditer(r"\b([a-zA-Z_]\w{2,})\b", line):
                added.append(m.group(1))
    added_set = set(added)
    return [r for r in dict.fromkeys(removed) if r not in added_set]


def _read_post_edit_content(file_path: str, repo_root: str) -> str | None:
    """Return the current (post-edit) content of the edited file, or None when
    it cannot be read. The diff's '+' side is the authoritative post-edit state
    for the changed hunk; the live file on disk is the authoritative post-edit
    state for the WHOLE file (a name moved elsewhere in the same file, or still
    imported there, has not left the file)."""
    if not file_path:
        return None
    norm = file_path.replace("\\", "/").lstrip("./").lstrip("/")
    candidate = os.path.join(repo_root, norm) if repo_root else norm
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return None


def _name_present(name: str, content: str) -> bool:
    """True if ``name`` appears as a whole identifier token in ``content``
    (word-boundary match, so a substring of a longer name does not count)."""
    return re.search(r"\b" + re.escape(name) + r"\b", content) is not None


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
            lines = content.splitlines()
            # Defense (3): require the removed id to appear on a line that ALSO
            # references the edited func_name, or is an assertion/mock line
            # within a small window of a line that does. A bare `rid in content`
            # match (e.g. the test's own `from datetime import timezone`, on a
            # line unrelated to the edited symbol) is not evidence the test
            # asserts on what the edit changed — correct-or-quiet: stay silent.
            for rid in removed_ids:
                line_no = _qualifying_test_line(lines, rid, func_name)
                if line_no is not None:
                    results.append((test_file, line_no, rid))
                    break
        except OSError:
            continue
    return results


def _qualifying_test_line(
    lines: list[str], rid: str, func_name: str
) -> int | None:
    """Return the 1-based line number where ``rid`` references the edited
    ``func_name`` (a genuine stale assertion/call on the changed symbol), or
    None when no such line exists.

    Qualifying = a line that contains ``rid`` (whole word) AND either also
    references ``func_name`` on that same line, or is an assertion/mock line
    sitting within a small window of a line that references ``func_name``
    (the assertion belongs to a test exercising the edited symbol).
    """
    _WINDOW = 3
    rid_re = re.compile(r"\b" + re.escape(rid) + r"\b")
    func_re = re.compile(r"\b" + re.escape(func_name) + r"\b") if func_name else None
    rid_lines = [i for i, ln in enumerate(lines) if rid_re.search(ln)]
    func_lines = (
        {i for i, ln in enumerate(lines) if func_re.search(ln)} if func_re else set()
    )
    for i in rid_lines:
        line = lines[i]
        # same-line reference to the edited symbol while passing the removed id
        if func_re and func_re.search(line):
            return i + 1
        # an assertion/mock on the removed id, near a use of the edited symbol
        low = line.lower()
        if ("assert" in low or "mock" in low) and any(
            abs(i - j) <= _WINDOW for j in func_lines
        ):
            return i + 1
    return None


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
    # Common method names that cause false positives when matched
    # across files (e.g., entry.get() flagging conftest.py's dict.get())
    "get", "pop", "keys", "values", "items", "format", "join",
    "split", "strip", "lower", "upper", "replace", "copy",
    "startswith", "endswith", "encode", "decode", "sort", "reverse",
    "insert", "remove", "count", "index", "find", "clear",
})
