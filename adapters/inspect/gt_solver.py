"""Full GT integration solver for Inspect AI using on_continue hook.

Architecture: Uses react()'s on_continue callback to augment observations
AFTER every tool execution — same push-based pattern as the OH wrapper.

- L1: Brief injected as system prompt (top files by connectivity)
- L3: After text_editor edits, caller/contract evidence added via user message
- L3b: After file reads, graph navigation hints added via user message
- Router: First-per-file dedup, view/edit separate, budget cap
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from inspect_ai.agent import react, AgentState
from inspect_ai.model import ChatMessageTool


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

_SANDBOX_PREFIXES = ("/testbed/", "/workspace/", "/tmp/repos/")


def _normalize_path(file_path: str) -> str:
    for prefix in _SANDBOX_PREFIXES:
        if file_path.startswith(prefix):
            return file_path[len(prefix):]
    if file_path.startswith("/"):
        return file_path.lstrip("/")
    return file_path


# ---------------------------------------------------------------------------
# Graph queries
# ---------------------------------------------------------------------------

def _query_callers(db_path: str, file_path: str, max_callers: int = 5) -> str:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        fp = _normalize_path(file_path)

        rows = conn.execute(
            """SELECT DISTINCT e.source_file, e.source_line, n.name as target_name
               FROM edges e
               JOIN nodes n ON e.target_id = n.id
               WHERE n.file_path = ? AND e.source_file != n.file_path
               AND e.confidence >= 0.5
               ORDER BY e.confidence DESC
               LIMIT ?""",
            (fp, max_callers),
        ).fetchall()

        if not rows:
            rows = conn.execute(
                """SELECT DISTINCT e.source_file, e.source_line, n.name as target_name
                   FROM edges e
                   JOIN nodes n ON e.target_id = n.id
                   WHERE n.file_path LIKE ? AND e.source_file != n.file_path
                   AND e.confidence >= 0.5
                   ORDER BY e.confidence DESC
                   LIMIT ?""",
                (f"%{fp}", max_callers),
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


def _query_symbols(db_path: str, file_path: str) -> str:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        fp = _normalize_path(file_path)
        rows = conn.execute(
            "SELECT name, label, signature, is_exported FROM nodes WHERE file_path = ? OR file_path LIKE ?",
            (fp, f"%{fp}"),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        parts = []
        for r in rows[:8]:
            sig = r["signature"] or r["name"]
            parts.append(f"  {r['label']} {sig}")
        return "\n".join(parts)
    except Exception:
        return ""


def generate_l1_brief(db_path: str) -> str:
    """Generate L1 brief: top files by caller connectivity."""
    if not db_path or not os.path.exists(db_path):
        return ""
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
# Router
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
        key = f"view:{_normalize_path(file_path)}"
        if key in self._seen_views:
            return False
        self._seen_views.add(key)
        self._delivery_count += 1
        return True

    def should_deliver_edit(self, file_path: str) -> bool:
        if self._delivery_count >= self._max_deliveries:
            return False
        key = f"edit:{_normalize_path(file_path)}"
        if key in self._seen_edits:
            return False
        self._seen_edits.add(key)
        self._delivery_count += 1
        return True


# ---------------------------------------------------------------------------
# on_continue hook: the GT augmentation engine
# ---------------------------------------------------------------------------

def _make_gt_hook(db_path: str, router: _Router):
    """Create the on_continue callback that augments observations with GT evidence."""

    async def gt_on_continue(state: AgentState) -> bool | str | AgentState:
        """Inspect latest tool results and inject GT evidence if relevant."""

        if not db_path or not os.path.exists(db_path):
            return True  # continue without GT

        # Find the last tool result messages
        evidence_parts: list[str] = []

        for msg in reversed(state.messages[-10:]):
            if not isinstance(msg, ChatMessageTool):
                continue

            content = str(getattr(msg, "content", ""))

            # L3: detect text_editor edits (str_replace, insert, create)
            if msg.function in ("text_editor", "gt_text_editor"):
                # Extract file path from the tool call arguments
                file_path = _extract_file_from_editor_result(content)
                if file_path and _is_edit_result(content):
                    if router.should_deliver_edit(file_path):
                        callers = _query_callers(db_path, file_path)
                        if callers:
                            evidence_parts.append(f"[GT L3 post-edit] {file_path}\n{callers}")

                # L3b: detect text_editor view
                elif file_path and _is_view_result(content):
                    if router.should_deliver_view(file_path):
                        callers = _query_callers(db_path, file_path)
                        if callers:
                            evidence_parts.append(f"[GT L3b] {file_path}\n{callers}")

            # L3b: detect bash file reads (cat, head, grep on source files)
            elif msg.function in ("bash_session", "gt_bash", "bash"):
                file_path = _extract_file_from_bash(content)
                if file_path:
                    if router.should_deliver_view(file_path):
                        callers = _query_callers(db_path, file_path)
                        if callers:
                            evidence_parts.append(f"[GT L3b] {file_path}\n{callers}")

            # Only process the most recent tool results (break after first batch)
            break

        if evidence_parts:
            return "\n\n".join(evidence_parts)

        return True  # continue without injecting

    return gt_on_continue


def _extract_file_from_editor_result(content: str) -> str:
    """Extract file path from text_editor tool result."""
    # Patterns: "File: /testbed/foo.py" or "/testbed/foo.py" at start
    m = re.search(r'(?:File:\s*|path[=:]\s*)(\/\S+\.(?:py|js|ts|go|java|rs))', content)
    if m:
        return m.group(1)
    m = re.search(r'(\/testbed\/\S+\.(?:py|js|ts|go|java|rs))', content[:200])
    if m:
        return m.group(1)
    return ""


def _is_edit_result(content: str) -> bool:
    return any(kw in content for kw in ["has been edited", "successfully", "str_replace", "insert", "created"])


def _is_view_result(content: str) -> bool:
    return "def " in content or "class " in content or "import " in content


def _extract_file_from_bash(content: str) -> str:
    """Extract source file path from bash output (cat/head/grep results)."""
    m = re.search(r'(\/testbed\/\S+\.(?:py|js|ts|go|java|rs))', content[:300])
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_TOOL_TIMEOUT = 210

GT_SYSTEM_PROMPT = """\
You have access to 4 GroundTruth codebase intelligence tools that query a \
pre-built code graph. They are instant and free (no LLM calls).

BEFORE editing any file, call groundtruth_brief on it to understand its \
callers, callees, contracts, and high-impact symbols.
Use groundtruth_trace to find callers/callees before changing function signatures.
Use groundtruth_impact to assess blast radius before modifying high-impact functions.
Use groundtruth_validate after making changes to check for broken imports or caller-blind edits.

After edits and file reads, you will also receive [GT L3] and [GT L3b] evidence \
showing callers and graph connections automatically."""


def create_gt_solver(db_path: str | None = None):
    """Create a react agent with full GT observation augmentation.

    - L1: brief injected as system prompt
    - L3/L3b: on_continue hook augments after tool calls
    - 4 GT tools available for pull-based queries
    - Router: first-per-file dedup
    """
    from inspect_ai.tool import bash_session, python, text_editor
    from adapters.inspect.tools import gt_tools

    _db = db_path or os.environ.get("GT_GRAPH_DB", "")
    router = _Router()

    brief = generate_l1_brief(_db)
    prompt = GT_SYSTEM_PROMPT
    if brief:
        prompt = f"{brief}\n\n{prompt}"

    return react(
        prompt=prompt,
        tools=[
            python(timeout=_TOOL_TIMEOUT),
            bash_session(timeout=_TOOL_TIMEOUT),
            text_editor(timeout=_TOOL_TIMEOUT),
            *gt_tools(),
        ],
        on_continue=_make_gt_hook(_db, router),
    )
