"""Full GT integration solver for Inspect AI.

Wraps the standard react() agent with observation augmentation that mirrors
the OpenHands GT wrapper behavior:

- L1: Brief injected as system message before agent starts
- L3: After every text_editor edit, GT evidence (callers, contracts) appended
- L3b: After every file read, GT graph navigation hints appended
- L4: Prefetch top symbols at task start, baked into brief
- L5: Scaffold trap monitoring
- Router: First-per-file dedup, view/edit separate keys
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from typing import Any

from inspect_ai import Task
from inspect_ai.agent import react
from inspect_ai.model import ChatMessage, ChatMessageTool, GenerateConfig
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolResult, bash_session, python, text_editor, tool

_TOOL_TIMEOUT = 210


# ---------------------------------------------------------------------------
# Graph queries (reuse from tools.py)
# ---------------------------------------------------------------------------

def _get_db() -> str | None:
    return os.environ.get("GT_GRAPH_DB")


def _query_callers(db_path: str, file_path: str, max_callers: int = 5) -> str:
    """Get top callers for symbols in a file."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Strip sandbox prefixes
        for prefix in ("/testbed/", "/workspace/", "/tmp/repos/"):
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
                break
        if file_path.startswith("/"):
            file_path = file_path.lstrip("/")

        rows = conn.execute(
            """SELECT DISTINCT e.source_file, e.source_line, n.name as target_name
               FROM edges e
               JOIN nodes n ON e.target_id = n.id
               WHERE n.file_path = ? AND e.source_file != n.file_path
               AND e.confidence >= 0.5
               ORDER BY e.confidence DESC
               LIMIT ?""",
            (file_path, max_callers),
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        lines = []
        for r in rows:
            lines.append(f"Called by: {r['source_file']}:{r['source_line']} `{r['target_name']}`")
        return "\n".join(lines)
    except Exception:
        return ""


def _query_brief(db_path: str, file_path: str) -> str:
    """Get brief for a file: symbols + callers + callees."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for prefix in ("/testbed/", "/workspace/", "/tmp/repos/"):
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
                break
        if file_path.startswith("/"):
            file_path = file_path.lstrip("/")

        symbols = conn.execute(
            "SELECT id, name, label, signature, is_exported FROM nodes WHERE file_path = ?",
            (file_path,),
        ).fetchall()

        if not symbols:
            symbols = conn.execute(
                "SELECT id, name, label, signature, is_exported FROM nodes WHERE file_path LIKE ?",
                (f"%{file_path}",),
            ).fetchall()

        if not symbols:
            conn.close()
            return ""

        parts = [f"[GT Brief] {file_path}: {len(symbols)} symbols"]
        for sym in symbols[:10]:
            caller_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND source_file != ?",
                (sym["id"], file_path),
            ).fetchone()[0]
            sig = sym["signature"] or sym["name"]
            exp = " (exported)" if sym["is_exported"] else ""
            parts.append(f"  {sym['label']} {sig}{exp} — {caller_count} callers")

        conn.close()
        return "\n".join(parts[:15])
    except Exception:
        return ""


def _query_impact(db_path: str, symbol_name: str) -> str:
    """Quick impact check for a symbol."""
    try:
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(DISTINCT e.source_file) FROM edges e JOIN nodes n ON e.target_id = n.id WHERE n.name = ?",
            (symbol_name,),
        ).fetchone()[0]
        conn.close()
        if count >= 5:
            return f"[GT] HIGH IMPACT: {symbol_name} has {count} caller files"
        elif count >= 2:
            return f"[GT] MODERATE IMPACT: {symbol_name} has {count} caller files"
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# L1: Brief generation
# ---------------------------------------------------------------------------

def generate_l1_brief(db_path: str) -> str:
    """Generate L1 brief from graph.db: top files by caller count."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT n.file_path, COUNT(DISTINCT e.source_file) as caller_files,
                      GROUP_CONCAT(DISTINCT n.name) as symbols
               FROM nodes n
               JOIN edges e ON e.target_id = n.id
               WHERE n.is_test = 0 AND e.source_file != n.file_path AND e.confidence >= 0.5
               GROUP BY n.file_path
               ORDER BY caller_files DESC
               LIMIT 5""",
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        parts = ["[GT Task Brief] Top files by connectivity:"]
        for r in rows:
            syms = r["symbols"].split(",")[:3]
            parts.append(f"  {r['file_path']} ({r['caller_files']} caller files) — {', '.join(syms)}")
        return "\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Router: dedup tracking
# ---------------------------------------------------------------------------

class _Router:
    def __init__(self) -> None:
        self._seen_views: set[str] = set()
        self._seen_edits: set[str] = set()
        self._delivery_count = 0
        self._max_deliveries = 20

    def should_deliver_view(self, file_path: str) -> bool:
        if self._delivery_count >= self._max_deliveries:
            return False
        key = f"view:{file_path}"
        if key in self._seen_views:
            return False
        self._seen_views.add(key)
        self._delivery_count += 1
        return True

    def should_deliver_edit(self, file_path: str) -> bool:
        if self._delivery_count >= self._max_deliveries:
            return False
        key = f"edit:{file_path}"
        if key in self._seen_edits:
            return False
        self._seen_edits.add(key)
        self._delivery_count += 1
        return True


# ---------------------------------------------------------------------------
# Tool wrappers that augment observations
# ---------------------------------------------------------------------------

def _wrap_text_editor(original_tool: Any, db_path: str, router: _Router) -> Tool:
    """Wrap text_editor to append L3 post-edit evidence."""

    @tool
    def gt_text_editor():
        """Edit files with automatic GT evidence augmentation."""

        async def run(**kwargs: Any) -> str:
            # Call original tool
            result = await original_tool.__call__(**kwargs)
            result_str = str(result) if not isinstance(result, str) else result

            if not db_path or not os.path.exists(db_path):
                return result_str

            # Extract file path from kwargs
            file_path = kwargs.get("path", kwargs.get("file_path", ""))
            if not file_path:
                return result_str

            command = kwargs.get("command", "")
            if command in ("str_replace", "insert", "create"):
                if router.should_deliver_edit(file_path):
                    evidence = _query_callers(db_path, file_path)
                    if evidence:
                        result_str += f"\n\n[GT L3 post-edit]\n{evidence}"

            elif command == "view":
                if router.should_deliver_view(file_path):
                    evidence = _query_callers(db_path, file_path)
                    if evidence:
                        result_str += f"\n\n[GT L3b]\n{evidence}"

            return result_str

        return run

    return gt_text_editor()


def _wrap_bash(original_tool: Any, db_path: str, router: _Router) -> Tool:
    """Wrap bash_session to append L3b post-view evidence on cat/head/less."""

    @tool
    def gt_bash():
        """Run bash with automatic GT evidence on file reads."""

        async def run(**kwargs: Any) -> str:
            result = await original_tool.__call__(**kwargs)
            result_str = str(result) if not isinstance(result, str) else result

            if not db_path or not os.path.exists(db_path):
                return result_str

            # Detect file reads from command
            cmd = kwargs.get("command", "")
            # Simple heuristic: if command reads a source file
            import re
            file_match = re.search(r'(?:cat|head|tail|less|more)\s+(\S+\.(?:py|js|ts|go|java|rs))', cmd)
            if file_match:
                file_path = file_match.group(1)
                if router.should_deliver_view(file_path):
                    evidence = _query_callers(db_path, file_path)
                    if evidence:
                        result_str += f"\n\n[GT L3b]\n{evidence}"

            return result_str

        return run

    return gt_bash()


# ---------------------------------------------------------------------------
# GT Solver: wraps react with full observation augmentation
# ---------------------------------------------------------------------------

GT_SYSTEM_PROMPT = """\
You have access to GroundTruth codebase intelligence. After every file edit, \
you will see [GT L3 post-edit] evidence showing callers and contracts for the \
edited file. After file reads, [GT L3b] shows graph connections.

Use this evidence to:
- Check callers before changing function signatures
- Understand blast radius of your changes
- Navigate to related test files shown in caller lists
- Avoid breaking dependent code"""


def create_gt_solver(db_path: str | None = None) -> Solver:
    """Create a solver with full GT observation augmentation.

    This mirrors the OH wrapper's push-based architecture:
    - L1 brief injected as system prompt
    - L3 post-edit evidence after every text_editor edit
    - L3b post-view evidence after file reads
    - Router dedup (first-per-file, view/edit separate)
    """
    _db = db_path or _get_db()
    router = _Router()

    # Generate L1 brief
    brief = ""
    if _db and os.path.exists(_db):
        brief = generate_l1_brief(_db)

    prompt = GT_SYSTEM_PROMPT
    if brief:
        prompt = f"{brief}\n\n{prompt}"

    # Create wrapped tools
    base_python = python(timeout=_TOOL_TIMEOUT)
    base_bash = bash_session(timeout=_TOOL_TIMEOUT)
    base_editor = text_editor(timeout=_TOOL_TIMEOUT)

    tools = [
        base_python,
        _wrap_bash(base_bash, _db or "", router),
        _wrap_text_editor(base_editor, _db or "", router),
    ]

    return react(prompt=prompt, tools=tools)
