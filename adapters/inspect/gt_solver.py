"""Full GT integration solver for Inspect AI.

Runs the SAME hooks as OH inside the Docker sandbox via sandbox().exec().
Mutates ChatMessageTool.content in-place (same as OH's append_observation).

Layers:
- L1: Brief from graph.db injected into first user message
- L3: post_edit.py runs in sandbox after every source edit
- L3b: post_view.py runs in sandbox after every file read
- L5: Scaffold trap monitoring
- L6: Incremental reindex before L3
- Router: first-per-file dedup, view/edit separate, budget cap
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from inspect_ai.agent import react, AgentState
from inspect_ai.model import ChatMessageTool
from inspect_ai.tool import bash_session, python, text_editor
from inspect_ai.util import sandbox


_TOOL_TIMEOUT = 210
_MAX_L3B_FIRES = 5
_MAX_L3_FIRES = 10
_MAX_EVIDENCE_CHARS = 1200
_SOURCE_EXTS = (".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".c", ".cpp", ".h")
_SCAFFOLD_RE = re.compile(r"(reproduce_|debug_|test_fix_|scratch_|temp_|\.tmp)")


class _GTState:
    def __init__(self) -> None:
        self.initialized = False
        self.graph_db = "/tmp/graph.db"
        self.viewed: set[str] = set()
        self.edited: set[str] = set()
        self.l3b_fires = 0
        self.l3_fires = 0
        self.action_count = 0
        self.last_msg_idx = 0


def _is_source(path: str) -> bool:
    return any(path.endswith(e) for e in _SOURCE_EXTS)


def _rel(path: str) -> str:
    for p in ("/testbed/", "/workspace/", "/tmp/repos/"):
        if path.startswith(p):
            return path[len(p):]
    return path.lstrip("/")


def _extract_file(msg: ChatMessageTool) -> tuple[str, str]:
    """Return (file_path, 'edit'|'view'|'') from a tool result."""
    content = str(getattr(msg, "content", ""))
    fn = getattr(msg, "function", "")

    if fn == "text_editor":
        if "has been edited" in content or "created file" in content:
            m = re.search(r"(?:file\s+)(/\S+\.\w+)", content[:300])
            if m and _is_source(m.group(1)):
                return m.group(1), "edit"
        m = re.search(r"cat -n[`]?\s+(?:on\s+)?(/\S+\.\w+)", content[:300])
        if m and _is_source(m.group(1)):
            return m.group(1), "view"
        m = re.search(r"(/testbed/\S+\.\w+)", content[:500])
        if m and _is_source(m.group(1)):
            return m.group(1), "edit" if "has been edited" in content else "view"

    elif fn in ("bash_session", "bash"):
        m = re.search(r"(/testbed/\S+\.\w+)", content[:500])
        if m and _is_source(m.group(1)):
            return m.group(1), "view"

    return "", ""


# ---------------------------------------------------------------------------
# Sandbox operations
# ---------------------------------------------------------------------------

async def _init_sandbox(st: _GTState) -> bool:
    """Upload gt-index + GT hooks, build graph.db."""
    sbx = sandbox()
    try:
        # gt-index binary
        check = await sbx.exec(["test", "-x", "/tmp/gt-index"], timeout=5)
        if check.returncode != 0:
            gt_bin = os.environ.get("GT_INDEX_BINARY", "/usr/local/bin/gt-index")
            if os.path.exists(gt_bin):
                await sbx.write_file("/tmp/gt-index", Path(gt_bin).read_bytes())
                await sbx.exec(["chmod", "+x", "/tmp/gt-index"], timeout=5)
                print("[GT_INIT] gt-index uploaded", flush=True)
            else:
                print(f"[GT_INIT] FAIL: no gt-index at {gt_bin}", flush=True)
                return False

        # Build graph.db
        check_db = await sbx.exec(["test", "-f", st.graph_db], timeout=5)
        if check_db.returncode != 0:
            r = await sbx.exec(
                ["/tmp/gt-index", "-root", "/testbed", "-output", st.graph_db],
                timeout=300,
            )
            if r.returncode != 0:
                print(f"[GT_INIT] gt-index FAILED: {r.stderr[:200]}", flush=True)
                return False
            v = await sbx.exec(
                ["python3", "-c",
                 f"import sqlite3; c=sqlite3.connect('{st.graph_db}');"
                 f"print(c.execute('SELECT COUNT(*) FROM nodes').fetchone()[0],"
                 f"'nodes',c.execute('SELECT COUNT(*) FROM edges').fetchone()[0],'edges')"],
                timeout=10,
            )
            print(f"[GT_INIT] graph.db: {v.stdout.strip()}", flush=True)

        # Upload GT hook files
        gt_src = None
        for candidate in [
            os.environ.get("GT_REPO", "") + "/src/groundtruth",
            "/home/ubuntu/Groundtruth/src/groundtruth",
            os.path.join(os.path.dirname(__file__), "..", "..", "src", "groundtruth"),
        ]:
            if os.path.isdir(candidate):
                gt_src = os.path.abspath(candidate)
                break

        if gt_src:
            await sbx.exec(["mkdir", "-p",
                            "/tmp/gt_hooks/groundtruth/hooks",
                            "/tmp/gt_hooks/groundtruth/pretask"], timeout=5)
            for sub in [
                "hooks/post_edit.py", "hooks/post_view.py",
                "hooks/semantic_check.py", "hooks/logger.py",
                "hooks/__init__.py", "pretask/__init__.py", "__init__.py",
            ]:
                local = os.path.join(gt_src, sub)
                if os.path.exists(local):
                    await sbx.write_file(
                        f"/tmp/gt_hooks/groundtruth/{sub}",
                        Path(local).read_bytes(),
                    )
            print(f"[GT_INIT] hooks uploaded from {gt_src}", flush=True)
        else:
            print("[GT_INIT] WARN: GT source not found, using graph.db queries only", flush=True)

        st.initialized = True
        return True
    except Exception as exc:
        print(f"[GT_INIT] ERROR: {exc}", flush=True)
        return False


async def _run_l3b(st: _GTState, file_path: str) -> str:
    """Run post_view.py in sandbox."""
    rel = _rel(file_path)
    try:
        # Try full hook first
        r = await sandbox().exec(
            ["python3", "-m", "groundtruth.hooks.post_view",
             "--root=/testbed", f"--db={st.graph_db}", f"--file={rel}"],
            env={"PYTHONPATH": "/tmp/gt_hooks"},
            timeout=30,
        )
        out = r.stdout.strip()
        if out and any(k in out for k in ("Called by:", "Calls into:", "Imported by:")):
            if len(out) > 500:
                out = out[:497] + "..."
            print(f"[GT_DELIVERY] L3b: {len(out)} chars file={rel} fire={st.l3b_fires+1}/{_MAX_L3B_FIRES}", flush=True)
            return f"\n\n[GT L3b] {rel}\n{out}\n"
    except Exception as exc:
        print(f"[GT_ERROR] L3b: {exc}", flush=True)

    # Fallback: simple SQL query
    try:
        r = await sandbox().exec(
            ["python3", "-c",
             f"import sqlite3; c=sqlite3.connect('{st.graph_db}'); c.row_factory=sqlite3.Row; "
             f"rows=c.execute(\"SELECT DISTINCT e.source_file,e.source_line,n.name FROM edges e "
             f"JOIN nodes n ON e.target_id=n.id WHERE n.file_path='{rel}' AND e.source_file!=n.file_path "
             f"AND e.confidence>=0.5 ORDER BY e.confidence DESC LIMIT 5\").fetchall(); "
             f"[print(f'Called by: {{r[\"source_file\"]}}:{{r[\"source_line\"]}} `{{r[\"name\"]}}`') for r in rows]"],
            timeout=10,
        )
        out = r.stdout.strip()
        if out:
            print(f"[GT_DELIVERY] L3b fallback: {len(out)} chars file={rel}", flush=True)
            return f"\n\n[GT L3b] {rel}\n{out}\n"
    except Exception:
        pass
    return ""


async def _run_l3(st: _GTState, file_path: str) -> str:
    """Run L6 reindex + L3 post_edit.py + semantic_check in sandbox."""
    rel = _rel(file_path)
    parts = []
    try:
        # L6: reindex
        await sandbox().exec(
            ["/tmp/gt-index", f"-file={file_path}", "-root=/testbed", f"-output={st.graph_db}"],
            timeout=60,
        )
        print(f"[GT_DELIVERY] L6 reindex: {rel}", flush=True)

        # L3: post_edit hook
        r = await sandbox().exec(
            ["python3", "-m", "groundtruth.hooks.post_edit",
             "--root=/testbed", f"--db={st.graph_db}", f"--file={rel}",
             "--quiet", "--max-items=3"],
            env={"PYTHONPATH": "/tmp/gt_hooks"},
            timeout=30,
        )
        out = r.stdout.strip()
        if out and any(k in out for k in (
            "CALLERS", "SIGNATURE", "MUST PRESERVE", "Called by:",
            "BEHAVIORAL CONTRACT", "TEST", "WARNING",
        )):
            if len(out) > _MAX_EVIDENCE_CHARS:
                out = out[:_MAX_EVIDENCE_CHARS - 3] + "..."
            parts.append(out)
            print(f"[GT_DELIVERY] L3 post_edit: {len(out)} chars file={rel} fire={st.l3_fires+1}/{_MAX_L3_FIRES}", flush=True)

        # Semantic check
        sem = await sandbox().exec(
            ["python3", "-m", "groundtruth.hooks.semantic_check",
             f"--file={rel}", "--workspace=/testbed"],
            env={"PYTHONPATH": "/tmp/gt_hooks"},
            timeout=15,
        )
        sem_out = sem.stdout.strip()
        if sem_out and any(k in sem_out for k in ("GUARD_ADDED", "GUARD_REMOVED", "RETURN_PATH")):
            parts.append(sem_out)
            print(f"[GT_DELIVERY] semantic_check: {sem_out[:100]}", flush=True)

    except Exception as exc:
        print(f"[GT_ERROR] L3: {exc}", flush=True)

    if parts:
        return f"\n\n[GT L3 post-edit] {rel}\n" + "\n".join(parts) + "\n"
    return ""


async def _generate_brief(st: _GTState) -> str:
    """L1 brief from graph.db."""
    try:
        r = await sandbox().exec(
            ["python3", "-c",
             f"import sqlite3\n"
             f"c=sqlite3.connect('{st.graph_db}')\n"
             f"c.row_factory=sqlite3.Row\n"
             f"for r in c.execute('SELECT n.file_path,COUNT(DISTINCT e.source_file) as cf,"
             f"GROUP_CONCAT(DISTINCT n.name) as s FROM nodes n JOIN edges e ON e.target_id=n.id "
             f"WHERE n.is_test=0 AND e.source_file!=n.file_path AND e.confidence>=0.5 "
             f"GROUP BY n.file_path ORDER BY cf DESC LIMIT 5').fetchall():\n"
             f"  print(f'  {{r[\"file_path\"]}} ({{r[\"cf\"]}} callers) -- {{r[\"s\"].split(\",\")[0]}}')\n"],
            timeout=15,
        )
        if r.stdout.strip():
            return f"[GT Task Brief] Top files:\n{r.stdout.strip()}"
    except Exception as exc:
        print(f"[GT_ERROR] brief: {exc}", flush=True)
    return ""


# ---------------------------------------------------------------------------
# on_continue: the main GT engine
# ---------------------------------------------------------------------------

def _make_gt_hook(st: _GTState):
    async def gt_on_continue(state: AgentState) -> bool | str | AgentState:
        # Init sandbox on first call
        if not st.initialized:
            ok = await _init_sandbox(st)
            if not ok:
                return True
            # L1: inject brief into first user message
            brief = await _generate_brief(st)
            if brief:
                for msg in state.messages:
                    if getattr(msg, "role", "") == "user":
                        orig = str(getattr(msg, "content", ""))
                        if "[GT Task Brief]" not in orig:
                            msg.content = f"{brief}\n\n{orig}"
                            print(f"[GT_DELIVERY] L1 brief: {len(brief)} chars", flush=True)
                        break

        st.action_count += 1

        # Find NEW tool messages since last check
        current = len(state.messages)
        if current <= st.last_msg_idx:
            return True
        new_msgs = state.messages[st.last_msg_idx:]
        st.last_msg_idx = current

        # Process tool results — MUTATE content in-place (same as OH append_observation)
        for msg in new_msgs:
            if not isinstance(msg, ChatMessageTool):
                continue
            if "groundtruth" in getattr(msg, "function", ""):
                continue

            file_path, action = _extract_file(msg)
            if not file_path or not _is_source(file_path):
                continue

            rel = _rel(file_path)

            if action == "edit":
                st.edited.add(rel)
                # L5: scaffold check
                if _SCAFFOLD_RE.search(os.path.basename(rel)):
                    has_source = any(not _SCAFFOLD_RE.search(os.path.basename(f)) for f in st.edited)
                    if not has_source:
                        msg.content = str(msg.content) + (
                            f"\n\n[GT L5] Creating scaffold ({rel}) without source edits. "
                            f"Edit source files first.\n"
                        )
                        print(f"[GT_DELIVERY] L5 scaffold: {rel}", flush=True)
                    continue

                if st.l3_fires >= _MAX_L3_FIRES:
                    continue
                evidence = await _run_l3(st, file_path)
                if evidence:
                    msg.content = str(msg.content) + evidence
                    st.l3_fires += 1

            elif action == "view":
                st.viewed.add(rel)
                if st.l3b_fires >= _MAX_L3B_FIRES:
                    continue
                view_key = f"view:{rel}"
                if view_key in st.viewed and st.l3b_fires > 0:
                    continue
                evidence = await _run_l3b(st, file_path)
                if evidence:
                    msg.content = str(msg.content) + evidence
                    st.l3b_fires += 1

        return True  # continue, no separate user message needed

    return gt_on_continue


GT_SYSTEM_PROMPT = """\
After every file edit, you will see [GT L3 post-edit] evidence showing callers, \
contracts, and behavioral obligations. After file reads, [GT L3b] shows graph \
connections and navigation hints. Use this evidence to avoid breaking callers \
and to navigate efficiently."""


def generate_l1_brief(db_path: str) -> str:
    """Generate L1 brief from local graph.db (host-side, for task.py)."""
    if not db_path or not os.path.exists(db_path):
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT n.file_path, COUNT(DISTINCT e.source_file) as cf, "
            "GROUP_CONCAT(DISTINCT n.name) as s FROM nodes n "
            "JOIN edges e ON e.target_id=n.id "
            "WHERE n.is_test=0 AND e.source_file!=n.file_path AND e.confidence>=0.5 "
            "GROUP BY n.file_path ORDER BY cf DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        parts = ["[GT Task Brief] Top files:"]
        for r in rows:
            parts.append(f"  {r['file_path']} ({r['cf']} callers) -- {r['s'].split(',')[0]}")
        return "\n".join(parts)
    except Exception:
        return ""


def create_gt_solver(db_path: str | None = None):
    """Create react agent with full GT — all layers firing via sandbox hooks."""
    from adapters.inspect.tools import gt_tools

    st = _GTState()
    return react(
        prompt=GT_SYSTEM_PROMPT,
        tools=[
            python(timeout=_TOOL_TIMEOUT),
            bash_session(timeout=_TOOL_TIMEOUT),
            text_editor(timeout=_TOOL_TIMEOUT),
            *gt_tools(),
        ],
        on_continue=_make_gt_hook(st),
    )
