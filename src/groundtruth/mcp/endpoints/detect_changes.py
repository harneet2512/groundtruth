"""gt_detect_changes — "what breaks if I commit this?"

When: before committing, with the working-tree diff (or a passed diff) in hand.

Pipeline:
  1. git diff of the working tree (``git diff HEAD``, falling back to
     ``git diff`` + ``git diff --cached`` on unborn-HEAD repos), or a
     caller-supplied unified diff.
  2. Map each ``@@`` hunk's new-side line range onto ``nodes`` symbol ranges
     (Function/Method/Class labels; overlap test).
  3. Join the changed symbols' stable ids through ``process_steps`` →
     ``processes`` — the test-witnessed interprocedural slices that traverse
     the change. Every affected process carries its witness (the assertion /
     test that certifies it), so "affected" means "a witnessed flow crosses
     the edit", never "looks related".

Abstention (typed, never fabricated):
  * graph tables absent          -> risk_level "unknown", degraded flag
  * git unavailable / diff fails -> risk_level "unknown", degraded flag
  * processes/process_steps absent -> affected_processes [] + degraded flag

Output shape:
    {"changed_count", "affected_count", "risk_level",
     "changed_symbols": [...], "affected_processes": [...],
     "partial", "truncated", "unmapped_files", "degraded"}
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from groundtruth.mcp.endpoints import _graph_db
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.utils.logger import get_logger

log: Any = get_logger("endpoints.detect_changes")

_MAX_CHANGED = 50
_MAX_PROCESSES = 25
_MAX_UNMAPPED = 20
_HOTSPOT_CALLERS = 10

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _get_working_tree_diff(root_path: str) -> str | None:
    """Unified diff of the working tree, or None when git can't answer.

    ``git diff HEAD`` covers staged+unstaged tracked changes in one shot. On
    an unborn-HEAD repository it fails; then ``git diff`` (unstaged) plus
    ``git diff --cached`` (staged) cover the same ground. A successful empty
    diff is a real answer ("nothing changed") and returns "" — it is NOT a
    failure. None is reserved for "git could not tell us".
    """
    try:
        head = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=root_path, timeout=15,
        )
        if head.returncode == 0:
            return head.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    parts: list[str] = []
    ran_ok = False
    for args in (["diff"], ["diff", "--cached"]):
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=root_path, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        if result.returncode == 0:
            ran_ok = True
            parts.append(result.stdout)
    if not ran_ok:
        return None
    return "\n".join(parts)


def _parse_diff_files(diff_text: str) -> tuple[dict[str, list[tuple[int, int]]], bool]:
    """Parse a unified diff into {new_path: [(hunk_start, hunk_end), ...]}.

    Returns (files, saw_diff_markers). ``saw_diff_markers`` distinguishes a
    structurally valid diff that touched no hunks (rename-only, binary,
    empty) from input that is not a diff at all — the latter is a parse
    failure, the former is a real "no symbol changes".
    """
    files: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    saw_markers = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            saw_markers = True
            continue
        if line.startswith("+++ "):
            saw_markers = True
            path = line[4:].strip().split("\t", 1)[0].strip()
            if path == "/dev/null":
                current = None  # deleted file: no new-side ranges exist
                continue
            if path.startswith("b/"):
                path = path[2:]
            current = path
            files.setdefault(path, [])
            continue
        if line.startswith("@@"):
            saw_markers = True
            m = _HUNK_RE.match(line)
            if m is None or current is None:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            # A +0,0 hunk (pure deletion) spans no new-side lines; record a
            # zero-width marker at the anchor so the symbol containing the
            # seam still counts as touched.
            end = start + max(count, 1) - 1
            files[current].append((start, end))
    return files, saw_markers


def _symbols_in_range(
    conn: Any, file_path: str, hunks: list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Symbols whose [start_line, end_line] range overlaps any hunk."""
    try:
        rows = conn.execute(
            "SELECT id, label, name, qualified_name, file_path, start_line, end_line "
            "FROM nodes WHERE file_path = ? "
            "AND label IN ('Function','Method','Class') "
            "AND start_line IS NOT NULL "
            "ORDER BY start_line, id",
            (file_path,),
        ).fetchall()
    except Exception as exc:
        log.debug("symbol_range_query_failed", file=file_path, error=str(exc))
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        s_start = row["start_line"]
        s_end = row["end_line"] if row["end_line"] is not None else s_start
        if any(s_start <= h_end and s_end >= h_start for h_start, h_end in hunks):
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "qualified_name": row["qualified_name"],
                    "file": row["file_path"],
                    "line": s_start,
                    "kind": row["label"],
                }
            )
    return out


def _caller_count(conn: Any, node_id: int) -> int:
    """Incoming-edge count at the >=0.5 floor (the verified-reach gate)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE target_id = ? AND COALESCE(confidence,0) >= 0.5",
            (node_id,),
        ).fetchone()
        return int(row["c"]) if row else 0
    except Exception:
        return 0


def _affected_processes(
    conn: Any, changed: list[dict[str, Any]], max_processes: int
) -> tuple[list[dict[str, Any]], bool]:
    """Join changed symbols → process_steps → processes (witnessed flows)."""
    node_ids = [c["id"] for c in changed]
    sid_map = _graph_db.stable_ids_for_nodes(conn, node_ids)
    sid_to_name: dict[str, str] = {}
    for c in changed:
        sid = sid_map.get(c["id"])
        if sid:
            sid_to_name[sid] = c["qualified_name"] or c["name"]
    sids = sorted(sid_to_name)
    if not sids:
        return [], False

    placeholders = ",".join("?" for _ in sids)
    try:
        pid_rows = conn.execute(
            f"SELECT DISTINCT process_id FROM process_steps WHERE stable_id IN ({placeholders})",
            tuple(sids),
        ).fetchall()
    except Exception as exc:
        log.debug("process_steps_join_failed", error=str(exc))
        return [], False
    pids = sorted(r["process_id"] for r in pid_rows)
    truncated = len(pids) > max_processes
    pids = pids[:max_processes]
    if not pids:
        return [], False

    pid_placeholders = ",".join("?" for _ in pids)
    try:
        proc_rows = conn.execute(
            "SELECT id, entry_stable_id, terminal_stable_id, witness_assertion_id, "
            "test_stable_id, kind, depth, trust_floor "
            f"FROM processes WHERE id IN ({pid_placeholders}) ORDER BY id",
            tuple(pids),
        ).fetchall()
        step_rows = conn.execute(
            f"SELECT process_id, stable_id FROM process_steps "
            f"WHERE process_id IN ({pid_placeholders}) ORDER BY process_id, ordinal",
            tuple(pids),
        ).fetchall()
    except Exception as exc:
        log.debug("processes_read_failed", error=str(exc))
        return [], truncated

    steps_by_pid: dict[str, list[str]] = {}
    for row in step_rows:
        steps_by_pid.setdefault(row["process_id"], []).append(row["stable_id"])

    # stable_id -> display name for entry/terminal/test.
    all_sids = sorted(
        {r["entry_stable_id"] for r in proc_rows}
        | {r["terminal_stable_id"] for r in proc_rows}
        | {r["test_stable_id"] for r in proc_rows}
    )
    names = _graph_db.symbol_names_for_stable_ids(conn, all_sids)

    affected: list[dict[str, Any]] = []
    for row in proc_rows:
        via = [sid_to_name[s] for s in steps_by_pid.get(row["id"], []) if s in sid_to_name]
        affected.append(
            {
                "id": row["id"],
                "entry": names.get(row["entry_stable_id"], row["entry_stable_id"]),
                "terminal": names.get(row["terminal_stable_id"], row["terminal_stable_id"]),
                "witnessed_by": names.get(row["test_stable_id"], row["test_stable_id"]),
                "witness_assertion_id": row["witness_assertion_id"],
                "kind": row["kind"],
                "depth": row["depth"],
                "trust_floor": row["trust_floor"],
                "via": via,
            }
        )
    return affected, truncated


def run_detect_changes(
    conn: Any,
    root_path: str,
    *,
    diff: str | None = None,
    max_changed: int = _MAX_CHANGED,
    max_processes: int = _MAX_PROCESSES,
) -> dict[str, Any]:
    """Sync core: map the diff to symbols and witnessed processes."""
    base: dict[str, Any] = {
        "changed_count": 0,
        "affected_count": 0,
        "risk_level": "unknown",
        "changed_symbols": [],
        "affected_processes": [],
        "partial": True,
        "truncated": False,
        "unmapped_files": [],
        "degraded": [],
    }
    if conn is None or not _graph_db.has_tables(conn, "nodes", "edges"):
        base["degraded"] = ["graph_tables_absent"]
        return base

    diff_text = diff if diff is not None else _get_working_tree_diff(root_path)
    if diff_text is None:
        base["degraded"] = ["git_diff_unavailable"]
        return base

    files, saw_markers = _parse_diff_files(diff_text)
    if diff_text.strip() and not saw_markers:
        # Non-empty input with no diff structure at all: we cannot see what
        # changed, so the risk level is honestly unknown.
        base["degraded"] = ["diff_parse_failed"]
        return base

    changed: list[dict[str, Any]] = []
    unmapped: list[str] = []
    seen_ids: set[int] = set()
    for file_path in sorted(files):
        matches = _symbols_in_range(conn, file_path, files[file_path])
        if not matches:
            unmapped.append(file_path)
            continue
        for m in matches:
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            changed.append(m)

    changed.sort(key=lambda c: (c["file"], c["line"] or 0))
    truncated = len(changed) > max_changed or len(unmapped) > _MAX_UNMAPPED
    changed = changed[:max_changed]

    degraded: list[str] = []
    affected: list[dict[str, Any]] = []
    if _graph_db.has_tables(conn, "processes", "process_steps"):
        affected, proc_trunc = _affected_processes(conn, changed, max_processes)
        truncated = truncated or proc_trunc
    else:
        degraded.append("processes")

    for c in changed:
        c["callers"] = _caller_count(conn, c["id"])
        c.pop("id", None)

    # Risk: a witnessed process crossing the change, or a changed hub symbol
    # (>= _HOTSPOT_CALLERS verified callers), is high. Symbols mapped but no
    # witnessed flow is moderate. A parsed diff touching no indexed symbol is
    # low. Failures above return "unknown" — never a guessed level.
    if affected or any(c["callers"] >= _HOTSPOT_CALLERS for c in changed):
        risk_level = "high"
    elif changed:
        risk_level = "moderate"
    else:
        risk_level = "low"

    return {
        "changed_count": len(changed),
        "affected_count": len(affected),
        "risk_level": risk_level,
        "changed_symbols": changed,
        "affected_processes": affected,
        "partial": bool(unmapped),
        "truncated": truncated,
        "unmapped_files": unmapped[:_MAX_UNMAPPED],
        "degraded": degraded,
    }


async def handle_gt_detect_changes(
    store: Any,
    graph: Any,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    diff: str | None = None,
) -> dict[str, Any]:
    """What breaks if I commit this — changed symbols + affected processes."""
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "gt_detect_changes",
        input_summary="diff -> symbols -> processes",
    ) as t:
        conn = _graph_db.store_connection(store)
        result = run_detect_changes(conn, root_path, diff=diff)
        t.log_component(
            "diff_to_processes",
            ComponentStatus.USED,
            output_summary=(
                f"{result['changed_count']} changed, "
                f"{result['affected_count']} affected processes, "
                f"risk={result['risk_level']}"
            ),
            item_count=result["affected_count"],
        )
        t.respond(
            response_type="detect_changes",
            verdict=result["risk_level"].upper(),
            output_summary=(
                f"risk {result['risk_level']}: {result['changed_count']} changed symbol(s), "
                f"{result['affected_count']} affected process(es)"
            ),
        )
        return result
