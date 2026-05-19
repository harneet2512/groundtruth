"""Layer 4 evidence providers (extracted from ``post_edit.py``).

Pure functions that, given a file/function/db, return one slice of evidence.
The router composes them. No tmp-file I/O, no env-var reads, no AgentState.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Sequence


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerCodeRecord:
    """A cross-file caller of the target function."""

    file: str
    line: int  # 0 == unknown
    caller_name: str
    code: str  # actual source line(s); may be empty
    unseen: bool  # True if ``file`` is NOT in ``seen_files``
    via_wrapper: bool = False  # set by the 2-hop wrapper follow


@dataclass(frozen=True)
class SiblingFunction:
    """A sibling function in the same class/file as the target."""

    name: str
    signature: str
    snippet: str  # first 2-3 body lines


@dataclass(frozen=True)
class Contract:
    """Signature + return type contract for a function."""

    signature: str
    return_type: str


@dataclass(frozen=True)
class TestAssertion:
    """One assertion in a test file pointing at the target function."""

    kind: str
    expression: str
    expected: str
    test_name: str
    test_file: str


@dataclass(frozen=True)
class TwinGroup:
    """Lines inside one function that share a structural template."""

    template: str
    entries: tuple[tuple[int, str], ...]  # (line_no, source) pairs


@dataclass(frozen=True)
class CoChangeFile:
    """A file that historically co-changes with the target."""

    file_path: str
    cooccurrence_count: int


@dataclass(frozen=True)
class EditPropagation:
    """A call site that may need updating after an edit."""

    caller_file: str
    line: int


# ---------------------------------------------------------------------------
# Shared internals (copied verbatim from post_edit so parity is byte-for-byte)
# ---------------------------------------------------------------------------


_TEMPLATE_SUBS = [
    (re.compile(r'"[^"]*"'), "STRING"),
    (re.compile(r"'[^']*'"), "STRING"),
    (re.compile(r"\b\d+\b"), "NUM"),
]


def _make_template(line: str) -> str:
    t = line.strip()
    for pat, repl in _TEMPLATE_SUBS:
        t = pat.sub(repl, t)
    return t


def _read_source_line(
    full_path: str, line_no: int, extra_lines: int = 0, end_line: int = 0
) -> str:
    """Read a source line + optional context lines after it (post_edit logic)."""
    try:
        lines_to_read: list[str] = []
        base_indent = -1
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i == line_no:
                    lines_to_read.append(line.rstrip())
                    base_indent = len(line) - len(line.lstrip())
                elif lines_to_read and len(lines_to_read) <= extra_lines:
                    if end_line and i > end_line:
                        break
                    stripped = line.rstrip()
                    if not stripped:
                        break
                    cur_indent = len(line) - len(line.lstrip())
                    if cur_indent < base_indent:
                        break
                    if any(
                        stripped.lstrip().startswith(kw)
                        for kw in ("def ", "async def ", "class ", "func ", "function ", "fn ")
                    ):
                        break
                    lines_to_read.append(stripped)
                elif lines_to_read:
                    break
        return " | ".join(lines_to_read) if lines_to_read else ""
    except OSError:
        return ""


def _read_source_lines(full_path: str, start: int, end: int) -> str:
    """Read lines [start, end] inclusive."""
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines: list[str] = []
            for i, line in enumerate(f, 1):
                if start <= i <= end:
                    lines.append(line.rstrip())
                if i > end:
                    break
            return "\n".join(lines)
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def caller_code_provider(
    db_path: str,
    file_path: str,
    function_name: str,
    repo_root: str,
    *,
    seen_files: Sequence[str] | None = None,
    limit: int = 5,
    follow_thin_wrapper: bool = True,
) -> list[CallerCodeRecord]:
    """Cross-file callers of ``function_name`` defined in ``file_path``.

    Mirrors ``post_edit._get_callers_from_graph`` byte-for-byte (verified by
    parity tests). The 2-hop "thin wrapper" follow can be disabled with
    ``follow_thin_wrapper=False``; default matches the live hook.

    ``seen_files`` is data, not state: it tells the provider which caller
    files the agent has already visited so the ``unseen`` flag is correct. The
    provider does NOT decide what to do with that information.
    """
    if not os.path.exists(db_path):
        return []
    seen: set[str] = set()
    if seen_files:
        seen = {s.replace("\\", "/").lstrip("/") for s in seen_files}
    results: list[CallerCodeRecord] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        has_confidence = "confidence" in cols
        conf_filter = "AND e.confidence >= 0.5" if has_confidence else ""
        order_clause = "e.confidence DESC, " if has_confidence else ""
        query = f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path LIKE ? AND nt.name = ?
              {conf_filter}
              AND nsrc.file_path != nt.file_path
            ORDER BY {order_clause}e.source_line
            LIMIT ?
        """
        norm_path = file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(query, (f"%{norm_path}", function_name, limit + 10)).fetchall()

        for row in rows:
            caller_file = row["file_path"]
            source_line = row["source_line"] or 0
            caller_name = row["name"]
            caller_norm = caller_file.replace("\\", "/").lstrip("/")
            is_unseen = caller_norm not in seen
            code = ""
            caller_end = row["end_line"] or 0
            if source_line and source_line > 0:
                code = _read_source_line(
                    os.path.join(repo_root, caller_file),
                    source_line,
                    extra_lines=2,
                    end_line=caller_end,
                )
            results.append(
                CallerCodeRecord(
                    file=caller_file,
                    line=int(source_line or 0),
                    caller_name=caller_name,
                    code=code,
                    unseen=is_unseen,
                )
            )
            if len(results) >= limit:
                break

        # 2-hop thin-wrapper follow (kept verbatim from post_edit)
        if follow_thin_wrapper and len(results) == 1:
            wrapper = results[0]
            wrapper_norm = wrapper.file.replace("\\", "/").lstrip("/")
            hop2_query = f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path LIKE ? AND nt.name = ?
                  {conf_filter}
                  AND nsrc.file_path != nt.file_path
                ORDER BY {order_clause}e.source_line
                LIMIT 5
            """
            hop2_rows = conn.execute(
                hop2_query, (f"%{wrapper_norm}", wrapper.caller_name)
            ).fetchall()
            if 0 < len(hop2_rows) < 3:
                for h2 in hop2_rows:
                    h2_file = h2["file_path"]
                    h2_line = h2["source_line"] or 0
                    h2_name = h2["name"]
                    h2_norm = h2_file.replace("\\", "/").lstrip("/")
                    is_unseen = h2_norm not in seen
                    code = ""
                    h2_end = h2["end_line"] or 0
                    if h2_line and h2_line > 0:
                        code = _read_source_line(
                            os.path.join(repo_root, h2_file),
                            h2_line,
                            extra_lines=2,
                            end_line=h2_end,
                        )
                    if code:
                        code = f"[via wrapper] {code}"
                    results.append(
                        CallerCodeRecord(
                            file=h2_file,
                            line=int(h2_line),
                            caller_name=h2_name,
                            code=code,
                            unseen=is_unseen,
                            via_wrapper=True,
                        )
                    )
                    if len(results) >= limit:
                        break
        conn.close()
    except sqlite3.Error:
        pass
    return results


def contract_provider(
    db_path: str, file_path: str, function_name: str
) -> Contract | None:
    """Signature + return type for ``function_name`` (post_edit parity)."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        norm_path = file_path.replace("\\", "/").lstrip("/")
        row = conn.execute(
            "SELECT signature, return_type FROM nodes "
            "WHERE file_path LIKE ? AND name = ? AND label IN ('Function', 'Method') LIMIT 1",
            (f"%{norm_path}", function_name),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    sig = row["signature"] or ""
    ret = row["return_type"] or ""
    if not sig and not ret:
        return None
    return Contract(signature=sig, return_type=ret)


def sibling_twin_provider(
    db_path: str, file_path: str, function_name: str, repo_root: str
) -> list[SiblingFunction]:
    """Sibling functions sharing the same parent (or same-file when no parent)."""
    if not os.path.exists(db_path):
        return []
    results: list[SiblingFunction] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        norm_path = file_path.replace("\\", "/").lstrip("/")
        target = conn.execute(
            "SELECT id, parent_id FROM nodes "
            "WHERE file_path LIKE ? AND name = ? AND label IN ('Function', 'Method') LIMIT 1",
            (f"%{norm_path}", function_name),
        ).fetchone()
        if not target:
            conn.close()
            return []
        node_id = target["id"]
        parent_id = target["parent_id"]
        if parent_id and parent_id > 0:
            siblings = conn.execute(
                "SELECT name, start_line, end_line, signature, file_path FROM nodes "
                "WHERE parent_id = ? AND id != ? AND label IN ('Function', 'Method') "
                "ORDER BY start_line LIMIT 3",
                (parent_id, node_id),
            ).fetchall()
        else:
            siblings = conn.execute(
                "SELECT name, start_line, end_line, signature, file_path FROM nodes "
                "WHERE file_path LIKE ? AND id != ? AND label IN ('Function', 'Method') "
                "AND (parent_id IS NULL OR parent_id = 0) "
                "ORDER BY start_line LIMIT 3",
                (f"%{norm_path}", node_id),
            ).fetchall()
        conn.close()
        for sib in siblings:
            start = sib["start_line"] or 0
            end = sib["end_line"] or 0
            snippet = ""
            if start > 0 and end > 0:
                body_start = start + 1
                body_end = min(start + 3, end)
                snippet = _read_source_lines(
                    os.path.join(repo_root, sib["file_path"]), body_start, body_end
                ).strip()
            results.append(
                SiblingFunction(
                    name=sib["name"],
                    signature=sib["signature"] or "",
                    snippet=snippet,
                )
            )
    except sqlite3.Error:
        pass
    return results


def test_provider(
    db_path: str, file_path: str, function_name: str
) -> list[TestAssertion]:
    """Test assertions targeting ``function_name`` (requires ``assertions`` table)."""
    if not os.path.exists(db_path):
        return []
    results: list[TestAssertion] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "assertions" not in tables:
            conn.close()
            return []
        norm_path = file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            """SELECT a.kind, a.expression, a.expected, a.line,
                      n.name AS test_name, n.file_path
               FROM assertions a
               JOIN nodes n ON a.test_node_id = n.id
               JOIN nodes target ON a.target_node_id = target.id
               WHERE target.file_path LIKE ? AND target.name = ?
               ORDER BY a.line LIMIT 3""",
            (f"%{norm_path}", function_name),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    for r in rows:
        results.append(
            TestAssertion(
                kind=r["kind"] or "",
                expression=r["expression"] or "",
                expected=r["expected"] or "",
                test_name=r["test_name"] or "",
                test_file=r["file_path"] or "",
            )
        )
    return results


def structural_twin_in_function_provider(
    full_file_path: str, func_start: int, func_end: int
) -> list[TwinGroup]:
    """AST-style twin detection within a function body (post_edit parity).

    Returns groups of >=2 lines sharing the same template (literals normalized).
    Sorted by group size descending. Mirrors
    ``post_edit._detect_structural_twins`` minus the caller-side string framing.
    """
    try:
        with open(full_file_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return []
    start = max(0, func_start - 1)
    end = min(len(all_lines), func_end)
    func_lines = all_lines[start:end]
    templates: dict[str, list[tuple[int, str]]] = {}
    for i, line in enumerate(func_lines):
        stripped = line.strip()
        if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if stripped in ("pass", "else:", "try:", "finally:", "except:", "break", "continue"):
            continue
        tmpl = _make_template(stripped)
        templates.setdefault(tmpl, []).append((start + i + 1, stripped))
    groups = [
        TwinGroup(template=tmpl, entries=tuple(entries))
        for tmpl, entries in templates.items()
        if 2 <= len(entries) <= 6
    ]
    groups.sort(key=lambda g: -len(g.entries))
    return groups


def co_change_provider(
    repo_root: str,
    file_path: str,
    *,
    edited_files: Sequence[str] | None = None,
    history_depth: int = 15,
    min_cooccurrence: int = 2,
    limit: int = 2,
) -> list[CoChangeFile]:
    """Files that historically co-change with ``file_path`` but aren't edited.

    Mirrors ``post_edit._co_change_reminder`` (git log -name-only).
    """
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", f"-{history_depth}", "--", file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []
    co_counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        f = line.strip()
        if (
            f
            and f != file_path
            and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml", ".toml"))
        ):
            co_counts[f] = co_counts.get(f, 0) + 1
    edited_set = set(edited_files or [])
    unedited_co = [
        (f, c) for f, c in co_counts.items() if f not in edited_set and c >= min_cooccurrence
    ]
    unedited_co.sort(key=lambda x: -x[1])
    return [CoChangeFile(file_path=f, cooccurrence_count=c) for f, c in unedited_co[:limit]]


def edit_propagation_provider(
    db_path: str,
    file_path: str,
    function_name: str,
    *,
    min_confidence: float = 0.9,
    limit: int = 5,
) -> list[EditPropagation]:
    """Call sites that may need updating after editing ``function_name``."""
    if not os.path.exists(db_path):
        return []
    results: list[EditPropagation] = []
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT DISTINCT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND e.confidence >= ?
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path = ?
              AND nsrc.file_path != nt.file_path
              AND nsrc.is_test = 0
              AND e.source_line > 0
            ORDER BY e.source_line
            LIMIT ?
            """,
            (min_confidence, function_name, file_path, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    for caller_file, line in rows:
        results.append(EditPropagation(caller_file=caller_file, line=int(line)))
    return results


__all__ = [
    "CallerCodeRecord",
    "CoChangeFile",
    "Contract",
    "EditPropagation",
    "SiblingFunction",
    "TestAssertion",
    "TwinGroup",
    "caller_code_provider",
    "co_change_provider",
    "contract_provider",
    "edit_propagation_provider",
    "sibling_twin_provider",
    "structural_twin_in_function_provider",
    "test_provider",
]
