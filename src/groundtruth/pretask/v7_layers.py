"""Behavioral fingerprints, caller evidence, and recent-edits layers for v7 briefs.

Three deterministic content layers, each gracefully empty when the source data is
absent:

  - extract_contract_properties: behavioral fingerprints from the ``properties``
    table populated by gt-index v16+ (return_shape, guard_clause, exception_type,
    docstring, caller_usage).
  - extract_caller_evidence: cross-file callers per top focus function with the
    call-site usage line.
  - extract_recent_edits: last commit touching each top focus function, with
    before/after lines from the diff hunk that overlaps the function range.

All three read graph.db with sqlite3 directly. No HTTP, no embeddings, no model
calls.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from typing import Any

# Confidence floor for caller-edge admissibility. Matches gt_intel.py's gate so
# v7 brief callers stay parity with the post-edit evidence engine.
MIN_EDGE_CONFIDENCE = 0.7

PROPERTY_KINDS = (
    "return_shape",
    "guard_clause",
    "exception_type",
    "docstring",
    "caller_usage",
)

DEFAULT_MAX_FUNCTIONS_PER_FILE = 1
DEFAULT_MAX_FOCUS_FUNCTIONS = 3
DEFAULT_MAX_CALLERS_PER_FN = 2
DEFAULT_MAX_PROPERTY_LINES = 3
DEFAULT_GIT_TIMEOUT_SEC = 5


@dataclass(frozen=True)
class FocusFunction:
    """A function selected for behavioral surfacing.

    ``incoming`` is the count of admissible incoming CALLS edges; used to pick
    the most-referenced function per file.
    """

    node_id: int
    name: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    incoming: int


@dataclass(frozen=True)
class ContractFingerprint:
    """Compact behavioral fingerprint for one focus function."""

    function: FocusFunction
    return_shape: str = ""
    guard_clause: str = ""
    exception_type: str = ""
    docstring: str = ""
    caller_usage: str = ""

    def lines(self, *, max_lines: int = DEFAULT_MAX_PROPERTY_LINES) -> list[str]:
        out: list[str] = []
        if self.return_shape:
            out.append(f"returns: {self.return_shape}")
        if self.guard_clause:
            out.append(f"guards: {self.guard_clause}")
        if self.exception_type:
            out.append(f"raises: {self.exception_type}")
        if self.docstring:
            out.append(f"doc: {self.docstring}")
        if self.caller_usage:
            out.append(f"used as: {self.caller_usage}")
        return out[:max_lines]


@dataclass(frozen=True)
class CallerHit:
    """A single cross-file caller of a focus function."""

    caller_name: str
    caller_file: str
    call_line: int
    call_text: str = ""


@dataclass(frozen=True)
class CallerEvidenceEntry:
    function: FocusFunction
    callers: tuple[CallerHit, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecentEdit:
    function: FocusFunction
    commit_hash: str
    commit_msg: str
    before: tuple[str, ...] = field(default_factory=tuple)
    after: tuple[str, ...] = field(default_factory=tuple)


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        return "confidence" in cols
    except sqlite3.Error:
        return False


def _confidence_clause(conn: sqlite3.Connection, alias: str = "e") -> str:
    if _has_confidence_column(conn):
        return f" AND {alias}.confidence >= {MIN_EDGE_CONFIDENCE}"
    return ""


def _select_focus_functions(
    conn: sqlite3.Connection,
    focus_files: list[str],
    *,
    max_total: int = DEFAULT_MAX_FOCUS_FUNCTIONS,
    max_per_file: int = DEFAULT_MAX_FUNCTIONS_PER_FILE,
) -> list[FocusFunction]:
    """Pick top focus functions across files by incoming-edge count.

    Edge confidence is gated when the column exists; otherwise plain count.
    Test-marked nodes are excluded — they would not be the edit target.
    """
    if not focus_files:
        return []
    conf = _confidence_clause(conn)
    placeholders = ",".join("?" for _ in focus_files)
    try:
        sql = (
            "SELECT n.id, n.name, n.file_path, n.start_line, n.end_line, "
            "n.language, COALESCE(COUNT(e.id), 0) AS incoming "
            "FROM nodes n "
            f"LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'{conf} "
            f"WHERE n.file_path IN ({placeholders}) "
            "AND n.label IN ('Function', 'Method') "
            "AND COALESCE(n.is_test, 0) = 0 "
            "GROUP BY n.id "
            "ORDER BY incoming DESC, n.start_line ASC"
        )
        rows = conn.execute(sql, tuple(focus_files)).fetchall()
    except sqlite3.Error:
        return []

    per_file: dict[str, int] = {}
    out: list[FocusFunction] = []
    for r in rows:
        file_path = r[2]
        if per_file.get(file_path, 0) >= max_per_file:
            continue
        per_file[file_path] = per_file.get(file_path, 0) + 1
        out.append(
            FocusFunction(
                node_id=r[0],
                name=r[1] or "",
                file_path=file_path or "",
                start_line=r[3] or 0,
                end_line=r[4] or 0,
                language=r[5] or "",
                incoming=r[6] or 0,
            )
        )
        if len(out) >= max_total:
            break
    return out


def _truncate(value: str, limit: int = 100) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _fetch_property_for_node(
    conn: sqlite3.Connection, node_id: int, kind: str
) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM properties WHERE node_id = ? AND kind = ? "
            "ORDER BY confidence DESC, line ASC LIMIT 1",
            (node_id, kind),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return _truncate(row[0] if row and row[0] else "")


def extract_contract_properties(
    graph_db: str | None,
    focus_functions: list[FocusFunction],
) -> list[ContractFingerprint]:
    """Per focus function, fetch one value per known property kind."""
    if not graph_db or not os.path.exists(graph_db) or not focus_functions:
        return []
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return []
    try:
        if not _has_table(conn, "properties"):
            return []
        out: list[ContractFingerprint] = []
        for fn in focus_functions:
            kwargs: dict[str, str] = {}
            for kind in PROPERTY_KINDS:
                value = _fetch_property_for_node(conn, fn.node_id, kind)
                if value:
                    kwargs[kind] = value
            if kwargs:
                out.append(ContractFingerprint(function=fn, **kwargs))
        return out
    finally:
        conn.close()


def extract_caller_evidence(
    graph_db: str | None,
    repo_root: str,
    focus_functions: list[FocusFunction],
    *,
    max_per_fn: int = DEFAULT_MAX_CALLERS_PER_FN,
) -> list[CallerEvidenceEntry]:
    """Collect cross-file CALLS edges per focus function with the call-site line.

    Empty list when graph.db has no admissible cross-file callers.
    """
    if not graph_db or not os.path.exists(graph_db) or not focus_functions:
        return []
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return []
    try:
        conf = _confidence_clause(conn)
        out: list[CallerEvidenceEntry] = []
        for fn in focus_functions:
            try:
                rows = conn.execute(
                    "SELECT n.name, e.source_file, e.source_line "
                    "FROM edges e JOIN nodes n ON n.id = e.source_id "
                    "WHERE e.target_id = ? AND e.type = 'CALLS' "
                    "AND e.source_file IS NOT NULL "
                    f"AND e.source_file != ?{conf} "
                    "ORDER BY e.source_line ASC LIMIT ?",
                    (fn.node_id, fn.file_path, max_per_fn),
                ).fetchall()
            except sqlite3.Error:
                continue
            hits: list[CallerHit] = []
            for caller_name, source_file, source_line in rows:
                call_text = _read_call_line(repo_root, source_file or "", source_line or 0)
                hits.append(
                    CallerHit(
                        caller_name=caller_name or "",
                        caller_file=_norm(source_file or ""),
                        call_line=source_line or 0,
                        call_text=call_text,
                    )
                )
            if hits:
                out.append(CallerEvidenceEntry(function=fn, callers=tuple(hits)))
        return out
    finally:
        conn.close()


def _read_call_line(repo_root: str, file_path: str, line: int) -> str:
    if not repo_root or not file_path or line <= 0:
        return ""
    abs_path = file_path
    if not os.path.isabs(abs_path):
        abs_path = os.path.join(repo_root, file_path)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            for idx, raw in enumerate(fh, start=1):
                if idx == line:
                    return _truncate(raw.strip(), limit=120)
                if idx > line:
                    break
    except OSError:
        return ""
    return ""


_HUNK_RE = re.compile(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def extract_recent_edits(
    repo_root: str,
    focus_functions: list[FocusFunction],
    *,
    timeout_sec: int = DEFAULT_GIT_TIMEOUT_SEC,
) -> list[RecentEdit]:
    """For each focus function, return the most recent commit that touched its
    line range, with a small slice of before/after diff lines.

    Time-bounded: never invokes git for more than ``DEFAULT_MAX_FOCUS_FUNCTIONS``
    functions, and each git call is wall-bounded by ``timeout_sec``.
    """
    if not repo_root or not focus_functions:
        return []
    if not os.path.isdir(os.path.join(repo_root, ".git")):
        return []
    out: list[RecentEdit] = []
    for fn in focus_functions[:DEFAULT_MAX_FOCUS_FUNCTIONS]:
        edit = _git_recent_edit(repo_root, fn, timeout_sec=timeout_sec)
        if edit is not None:
            out.append(edit)
    return out


def _git_recent_edit(
    repo_root: str, fn: FocusFunction, *, timeout_sec: int
) -> RecentEdit | None:
    if not fn.file_path:
        return None
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-5", "--follow", "--", fn.file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if log.returncode != 0 or not log.stdout.strip():
        return None
    for commit_line in log.stdout.strip().splitlines()[:3]:
        parts = commit_line.split(maxsplit=1)
        if not parts:
            continue
        commit_hash = parts[0]
        commit_msg = parts[1] if len(parts) > 1 else ""
        try:
            diff = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}", "--", fn.file_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        if diff.returncode != 0 or not diff.stdout:
            continue
        before, after = _slice_hunk(diff.stdout, fn.start_line, fn.end_line)
        if not before and not after:
            continue
        return RecentEdit(
            function=fn,
            commit_hash=commit_hash[:7],
            commit_msg=_truncate(commit_msg, limit=70),
            before=before,
            after=after,
        )
    return None


def _slice_hunk(
    diff_text: str, start_line: int, end_line: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    before: list[str] = []
    after: list[str] = []
    in_relevant = False
    for raw in diff_text.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            hunk_start = int(match.group(1))
            in_relevant = start_line - 10 <= hunk_start <= end_line + 10
            continue
        if not in_relevant:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        stripped = raw[1:].strip() if raw else ""
        if not stripped:
            continue
        if raw.startswith("-"):
            before.append(_truncate(stripped, limit=100))
        elif raw.startswith("+"):
            after.append(_truncate(stripped, limit=100))
        if len(before) >= 2 and len(after) >= 2:
            break
    return tuple(before[:2]), tuple(after[:2])


# ----------------------------------------------------------------- public API


def collect_v7_layers(
    graph_db: str | None,
    repo_root: str,
    focus_files: list[str],
    *,
    max_focus: int = DEFAULT_MAX_FOCUS_FUNCTIONS,
) -> dict[str, Any]:
    """One-shot helper used by v7_brief.py.

    Returns a dict with keys ``focus_functions``, ``contract``, ``callers``,
    ``recent_edits``. Each value is a list (possibly empty). Caller decides
    what to render — empty values render no header.
    """
    if not graph_db or not os.path.exists(graph_db):
        return {"focus_functions": [], "contract": [], "callers": [], "recent_edits": []}
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return {"focus_functions": [], "contract": [], "callers": [], "recent_edits": []}
    try:
        focus_fns = _select_focus_functions(conn, focus_files, max_total=max_focus)
    finally:
        conn.close()
    if not focus_fns:
        return {"focus_functions": [], "contract": [], "callers": [], "recent_edits": []}
    contract = extract_contract_properties(graph_db, focus_fns)
    callers = extract_caller_evidence(graph_db, repo_root, focus_fns)
    edits = extract_recent_edits(repo_root, focus_fns)
    return {
        "focus_functions": focus_fns,
        "contract": contract,
        "callers": callers,
        "recent_edits": edits,
    }
