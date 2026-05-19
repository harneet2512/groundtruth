"""Full GT integration solver for Inspect AI.

Patches execute_tools() to inject GT evidence into tool results BEFORE
they reach the agent — same as OH's append_observation. Evidence appears
INSIDE the tool result message, same attention window.

Layers: L1 brief, L3 post-edit, L3b post-view, L5 scaffold, Router.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from inspect_ai.agent import react
from inspect_ai.model import ChatMessageTool
from inspect_ai.tool import bash_session, python, text_editor


_TOOL_TIMEOUT = 210
_SOURCE_EXTS = (".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".c", ".cpp", ".h")
_SCAFFOLD_RE = re.compile(r"(reproduce_|debug_|test_fix_|scratch_|temp_|\.tmp)")


def _is_source(p: str) -> bool:
    return any(p.endswith(e) for e in _SOURCE_EXTS)


def _rel(path: str) -> str:
    for p in ("/testbed/", "/workspace/", "/tmp/repos/"):
        if path.startswith(p):
            return path[len(p):]
    return path.lstrip("/")


def _log(msg: str) -> None:
    try:
        with open("/tmp/gt_solver_debug.log", "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Graph queries (host-side, from GT_GRAPH_DB)
# ---------------------------------------------------------------------------

def _callers(db: str, file_path: str, limit: int = 5) -> str:
    if not db or not os.path.exists(db):
        return ""
    try:
        fp = _rel(file_path)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT e.source_file, e.source_line, n.name "
            "FROM edges e JOIN nodes n ON e.target_id = n.id "
            "WHERE (n.file_path = ? OR n.file_path LIKE ?) "
            "AND e.source_file != n.file_path AND e.confidence >= 0.5 "
            "ORDER BY e.confidence DESC LIMIT ?",
            (fp, f"%{fp}", limit),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        return "\n".join(f"Called by: {r[0]}:{r[1]} `{r[2]}`" for r in rows)
    except Exception:
        return ""


def _symbols(db: str, file_path: str) -> str:
    if not db or not os.path.exists(db):
        return ""
    try:
        fp = _rel(file_path)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, label, signature, is_exported FROM nodes "
            "WHERE file_path = ? OR file_path LIKE ? LIMIT 8",
            (fp, f"%{fp}"),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        return "\n".join(
            f"  {r['label']} {r['signature'] or r['name']}"
            + (" [EXPORTED]" if r["is_exported"] else "")
            for r in rows
        )
    except Exception:
        return ""


def generate_l1_brief(db: str) -> str:
    if not db or not os.path.exists(db):
        return ""
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT n.file_path, COUNT(DISTINCT e.source_file) as cf, "
            "GROUP_CONCAT(DISTINCT n.name) as s FROM nodes n "
            "JOIN edges e ON e.target_id = n.id "
            "WHERE n.is_test = 0 AND e.source_file != n.file_path AND e.confidence >= 0.5 "
            "GROUP BY n.file_path ORDER BY cf DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        parts = ["[GT Task Brief] Top files by connectivity:"]
        for r in rows:
            parts.append(f"  {r['file_path']} ({r['cf']} callers) -- {r['s'].split(',')[0]}")
        return "\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class _GTRouter:
    def __init__(self, db: str) -> None:
        self.db = db
        self.seen_views: set[str] = set()
        self.seen_edits: set[str] = set()
        self.l3b_count = 0
        self.l3_count = 0
        self.edited: set[str] = set()

    def augment(self, msg: ChatMessageTool) -> None:
        """Augment a tool result message with GT evidence in-place."""
        content = str(getattr(msg, "content", ""))
        fn = getattr(msg, "function", "")

        if "groundtruth" in fn:
            return

        file_path, action = self._extract(fn, content)
        if not file_path or not _is_source(file_path):
            return

        rel = _rel(file_path)
        _log(f"augment: fn={fn} file={rel} action={action}")

        if action == "edit":
            self.edited.add(rel)
            if _SCAFFOLD_RE.search(os.path.basename(rel)):
                has_source = any(not _SCAFFOLD_RE.search(os.path.basename(f)) for f in self.edited)
                if not has_source:
                    msg.content = content + f"\n\n[GT L5] Creating scaffold ({rel}) without source edits.\n"
                    _log(f"L5 scaffold: {rel}")
                return

            key = f"edit:{rel}"
            if key in self.seen_edits or self.l3_count >= 10:
                return
            self.seen_edits.add(key)

            evidence = _callers(self.db, file_path)
            syms = _symbols(self.db, file_path)
            if evidence or syms:
                block = f"\n\n[GT L3 post-edit] {rel}\n"
                if evidence:
                    block += evidence + "\n"
                if syms:
                    block += f"Symbols:\n{syms}\n"
                msg.content = content + block
                self.l3_count += 1
                _log(f"L3 post-edit: {rel} ({len(block)} chars)")

        elif action == "view":
            key = f"view:{rel}"
            if key in self.seen_views or self.l3b_count >= 5:
                return
            self.seen_views.add(key)

            evidence = _callers(self.db, file_path)
            if evidence:
                msg.content = content + f"\n\n[GT L3b] {rel}\n{evidence}\n"
                self.l3b_count += 1
                _log(f"L3b: {rel} ({len(evidence)} chars)")

    def _extract(self, fn: str, content: str) -> tuple[str, str]:
        if fn == "text_editor":
            if "has been edited" in content or "created file" in content:
                m = re.search(r"(?:file\s+)(/\S+\.\w+)", content[:300])
                if m:
                    return m.group(1), "edit"
            m = re.search(r"cat -n[`]?\s+(?:on\s+)?(/\S+\.\w+)", content[:300])
            if m:
                return m.group(1), "view"
            m = re.search(r"(/testbed/\S+\.\w+)", content[:500])
            if m:
                return m.group(1), "edit" if "has been edited" in content else "view"
        elif fn in ("bash_session", "bash"):
            m = re.search(r"(/testbed/\S+\.\w+)", content[:500])
            if m:
                return m.group(1), "view"
        return "", ""


# ---------------------------------------------------------------------------
# Monkey-patch execute_tools
# ---------------------------------------------------------------------------

_original_execute_tools = None
_active_router: _GTRouter | None = None


async def _patched_execute_tools(messages, tools, **kwargs):
    """Call original execute_tools, then augment tool results with GT evidence."""
    result = await _original_execute_tools(messages, tools, **kwargs)

    if _active_router is None:
        return result

    # result is (messages, output) tuple
    new_messages, output = result
    for msg in new_messages:
        if isinstance(msg, ChatMessageTool):
            _active_router.augment(msg)

    return (new_messages, output)


def _patch_execute_tools(router: _GTRouter) -> None:
    """Install the monkey-patch on execute_tools."""
    global _original_execute_tools, _active_router
    _active_router = router

    if _original_execute_tools is not None:
        return  # already patched

    from inspect_ai.model._call_tools import execute_tools as orig
    _original_execute_tools = orig

    import inspect_ai.model._call_tools as call_tools_mod
    call_tools_mod.execute_tools = _patched_execute_tools

    # Also patch the import in the react module
    import inspect_ai.agent._react as react_mod
    react_mod.execute_tools = _patched_execute_tools

    _log("execute_tools patched")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

GT_SYSTEM_PROMPT = """\
After every file edit, you will see [GT L3 post-edit] evidence showing callers \
and contracts. After file reads, [GT L3b] shows graph connections. \
Use this evidence to avoid breaking callers and navigate efficiently."""


def create_gt_solver(db_path: str | None = None):
    """Create react agent with full GT — evidence injected into tool results."""
    from adapters.inspect.tools import gt_tools

    _db = db_path or os.environ.get("GT_GRAPH_DB", "")

    # L1 brief
    brief = generate_l1_brief(_db)
    prompt = GT_SYSTEM_PROMPT
    if brief:
        prompt = f"{brief}\n\n{prompt}"

    # Install the execute_tools patch
    router = _GTRouter(_db)
    _patch_execute_tools(router)

    return react(
        prompt=prompt,
        tools=[
            python(timeout=_TOOL_TIMEOUT),
            bash_session(timeout=_TOOL_TIMEOUT),
            text_editor(timeout=_TOOL_TIMEOUT),
            *gt_tools(),
        ],
    )
