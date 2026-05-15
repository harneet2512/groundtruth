"""Post-view hook — structural coupling enrichment for file reads.

Called by OpenHands PostToolUse hook on file_editor view operations.
Composes: PatternRoleClassifier + shared-state coupling detection.
Outputs 0-5 compact structural notes to stdout.

Usage:
    python -m groundtruth.hooks.post_view --root=/testbed --db=/tmp/gt_index.db --file=<path>
"""

from __future__ import annotations

import argparse
import ast
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from groundtruth.hooks.logger import log_hook

_GT_LOG = os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")


def _append_gt_log(event: str, detail: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}\tpost_view\t{event}"
    if detail:
        line += f"\t{detail}"
    try:
        with open(_GT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _status_line(kind: str, detail: str) -> str:
    return f"[GT_STATUS] {kind}:{detail}"


def _read_file(root: str, relpath: str) -> str:
    try:
        path = relpath if os.path.isabs(relpath) else os.path.join(root, relpath)
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_test_file(filepath: str) -> bool:
    fp = "/" + filepath.lower().replace("\\", "/")
    base = os.path.basename(fp)
    if base.startswith("test_"):
        return True
    return any(p in fp for p in ["/tests/", "/test/", "/testing/", "/fixtures/"])


def _classify_role(method_name: str, method_node: ast.FunctionDef) -> str:
    """Classify a method's role based on AST patterns."""
    if method_name == "__init__":
        return "stores"
    # Check for Store context on self.attrs
    written = set()
    for child in ast.walk(method_node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
            and isinstance(child.ctx, ast.Store)
        ):
            written.add(child.attr)
    if len(written) >= 2:
        return "stores"

    serialize_names = ("deconstruct", "serialize", "to_dict", "as_dict", "get_params")
    if any(s in method_name.lower() for s in serialize_names):
        return "serializes"

    if method_name in ("__eq__", "__ne__", "__hash__", "__lt__", "__le__", "__gt__", "__ge__"):
        return "compares"

    validate_names = ("validate", "check", "clean", "verify")
    if any(s in method_name.lower() for s in validate_names):
        return "validates"

    for child in ast.walk(method_node):
        if isinstance(child, ast.Raise):
            return "validates"

    return "reads"


def _get_role_label(role: str) -> str:
    return {
        "stores": "stores",
        "serializes": "serializes to kwargs",
        "compares": "compares",
        "validates": "checks",
        "reads": "reads",
    }.get(role, role)


def _load_issue_terms() -> set[str]:
    """Load issue keywords written during initialization for issue-aware navigation."""
    try:
        text = open("/tmp/gt_issue_terms.txt", encoding="utf-8").read()
        return set(text.strip().split("\n")) if text.strip() else set()
    except OSError:
        return set()


def _score_by_issue_relevance(
    files: list[tuple[str, int]], root: str, issue_terms: set[str],
) -> list[tuple[str, int, int]]:
    """Re-rank neighbor files by how many issue terms appear in their content."""
    if not issue_terms:
        return [(f, cnt, 0) for f, cnt in files]
    scored = []
    for fp, cnt in files:
        try:
            text = open(os.path.join(root, fp), encoding="utf-8", errors="ignore").read(200_000).lower()
            hits = sum(1 for t in issue_terms if t in text)
        except OSError:
            hits = 0
        scored.append((fp, cnt, hits))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


def _load_visited_files() -> set[str]:
    """Load already-viewed file paths from /tmp/gt_viewed.txt."""
    try:
        text = open("/tmp/gt_viewed.txt", encoding="utf-8").read()
        return {ln.strip() for ln in text.strip().split("\n") if ln.strip()}
    except OSError:
        return set()


def _load_brief_candidates() -> set[str]:
    """Load brief candidate file paths from /tmp/gt_brief_candidates.txt."""
    try:
        text = open("/tmp/gt_brief_candidates.txt", encoding="utf-8").read()
        return {ln.strip() for ln in text.strip().split("\n") if ln.strip()}
    except OSError:
        return set()


def _in_degree_for_file(cur: "sqlite3.Cursor", file_path: str) -> int:
    """Get total incoming edge count for a file (used for hub penalty)."""
    try:
        row = cur.execute(
            """
            SELECT COUNT(*) FROM edges e
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nt.file_path = ?
              AND e.type = 'CALLS'
            """,
            (file_path,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _top_functions_for_file(cur: "sqlite3.Cursor", file_path: str, limit: int = 2) -> list[tuple[str, int]]:
    """Get top functions in a file by reference count (name, ref_count)."""
    try:
        rows = cur.execute(
            """
            SELECT n.name, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY ref_count DESC, n.name
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]
    except Exception:
        return []


def graph_navigation(
    relpath: str, db_path: str, *, limit: int = 5, iteration_ratio: float = 0.0,
) -> tuple[list[str], int]:
    """Graph.db navigation context — callers, callees, importers.

    Issue-aware: ranks neighbors by relevance to the current issue so the
    agent sees connections that matter, not just high-edge-count hubs.

    Optimizations:
    1. Confidence filter (>= 0.5) on edge queries
    2. Suppress already-visited files
    3. Brief candidate annotation [CANDIDATE]
    4. Hub-penalized ranking: score = cnt * (1 - min(1, in_degree/50))
    5. Symbol-level hints: file::func1,func2 (Nx)
    """
    if not os.path.isfile(db_path):
        return [], 0
    needle = relpath.replace("\\", "/").lstrip("./")
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            return [], 0

    # Improvement 2: Load already-visited files for suppression
    visited_files = _load_visited_files()
    # Improvement 3: Load brief candidates for annotation
    brief_candidates = _load_brief_candidates()

    # Feature-flagged iteration-aware decay (Change 4)
    rebuild_l3b = os.environ.get("GT_REBUILD_L3B", "0") == "1"
    if rebuild_l3b and iteration_ratio >= 0.85:
        limit = 1
    elif rebuild_l3b and iteration_ratio >= 0.60:
        limit = max(2, limit // 2)

    # Progress tracking
    total_candidates = int(os.environ.get("GT_L3B_TOTAL_CANDIDATES", "0"))

    out: list[str] = []
    total_callers = 0
    try:
        cur = conn.cursor()

        # Callers: files that call functions in this file
        # Improvement 1: confidence filter >= 0.5
        cur.execute(
            """
            SELECT DISTINCT nsrc.file_path, COUNT(*) as cnt
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND COALESCE(e.confidence, 0.5) >= 0.5
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ?
              AND nsrc.file_path != ?
            GROUP BY nsrc.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (needle, needle, limit * 4),  # fetch more for filtering
        )
        callers = cur.fetchall()
        total_callers = len(callers)

        # Callees: files this file calls into
        # Improvement 1: confidence filter >= 0.5
        cur.execute(
            """
            SELECT DISTINCT nt.file_path, COUNT(*) as cnt
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
              AND COALESCE(e.confidence, 0.5) >= 0.5
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
            GROUP BY nt.file_path
            ORDER BY cnt DESC
            LIMIT 40
            """,
            (needle, needle),
        )
        callees = cur.fetchall()

        # Improvement 2: Suppress already-visited files
        if visited_files:
            callers = [(fp, cnt) for fp, cnt in callers if fp not in visited_files]
            callees = [(fp, cnt) for fp, cnt in callees if fp not in visited_files]

        # Re-rank both by issue relevance
        issue_terms = _load_issue_terms()
        root = os.environ.get("GT_REPO_ROOT", "/testbed")
        if issue_terms:
            ranked_callers = _score_by_issue_relevance(callers, root, issue_terms)
            ranked_callees = _score_by_issue_relevance(callees, root, issue_terms)
            top_callers = [(f, cnt) for f, cnt, _ in ranked_callers[:limit * 2]]
            top_callees = [(f, cnt) for f, cnt, _ in ranked_callees[:limit * 2]]
        else:
            top_callers = callers[:limit * 2]
            top_callees = callees[:limit * 2]

        # Improvement 4: Hub-penalized ranking (repo-relative hub scale)
        # Compute p90 in-degree once for this graph instead of hardcoded 50
        # Only count CALLS edges — EXTENDS/IMPLEMENTS are architectural, not hub indicators
        all_degrees = [r[0] for r in cur.execute(
            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' GROUP BY n.file_path ORDER BY 1"
        ).fetchall()]
        hub_scale = all_degrees[int(len(all_degrees) * 0.9)] if all_degrees else 50

        def _hub_penalized_score(fp: str, cnt: int) -> float:
            in_deg = _in_degree_for_file(cur, fp)
            return cnt * (1.0 - min(1.0, in_deg / float(hub_scale)))

        top_callers = sorted(top_callers, key=lambda x: _hub_penalized_score(x[0], x[1]), reverse=True)[:limit]
        top_callees = sorted(top_callees, key=lambda x: _hub_penalized_score(x[0], x[1]), reverse=True)[:limit]

        # Improvement 3 + 5: Brief candidate annotation + symbol-level hints
        def _format_neighbor(fp: str, cnt: int) -> str:
            funcs = _top_functions_for_file(cur, fp, limit=2)
            func_names = ",".join(name for name, _ in funcs) if funcs else ""
            max_ref = max((rc for _, rc in funcs), default=0) if funcs else 0
            suffix = ""
            if any(fp == c or fp.endswith("/" + c) or c.endswith("/" + fp) for c in brief_candidates):
                suffix = " [CANDIDATE]"
            if func_names:
                return f"{fp}::{func_names} ({cnt}x){suffix}"
            return f"{fp} ({cnt}x){suffix}"

        if top_callers:
            caller_files = [_format_neighbor(fp, cnt) for fp, cnt in top_callers]
            out.append(f"Called by: {', '.join(caller_files)}")
        if top_callees:
            callee_files = [_format_neighbor(fp, cnt) for fp, cnt in top_callees]
            out.append(f"Calls into: {', '.join(callee_files)}")

        # Importers: skip after 60% iteration (Change 4)
        if not (rebuild_l3b and iteration_ratio >= 0.60):
            cur.execute(
                """
                SELECT DISTINCT nsrc.file_path
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'IMPORTS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path = ?
                  AND nsrc.file_path != ?
                LIMIT ?
                """,
                (needle, needle, limit),
            )
            importers = [fp for (fp,) in cur.fetchall() if fp not in visited_files]
            if importers:
                out.append(f"Imported by: {', '.join(importers)}")

        # Progress tracking (Change 4)
        if rebuild_l3b and total_candidates > 0 and visited_files:
            out.insert(0, f"[Progress: visited {len(visited_files)}/{total_candidates} connected files]")

        # Late-phase focus tag (Change 4)
        if rebuild_l3b and iteration_ratio >= 0.85 and out:
            out.insert(0, "[FOCUS: late-phase, showing only top connection]")

    except Exception:
        return [], 0
    finally:
        conn.close()
    return out, total_callers


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-view enrichment hook")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--file", required=True, help="File path to enrich")
    parser.add_argument("--iteration-ratio", type=float, default=0.0)
    parser.add_argument("--total-candidates", type=int, default=0)
    args = parser.parse_args()

    start = time.time()
    _append_gt_log("fire", f"root={args.root} file={args.file} db={args.db}")
    log_entry = {
        "hook": "post_view",
        "endpoint": "understand",
        "file": args.file,
        "classes_found": 0,
        "coupled_classes": 0,
    }

    filepath = args.file
    if _is_test_file(filepath):
        status = _status_line("skipped", "test_file")
        print(status)
        _append_gt_log("status", status)
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    # Pass total_candidates via env for graph_navigation to pick up
    if args.total_candidates > 0:
        os.environ["GT_L3B_TOTAL_CANDIDATES"] = str(args.total_candidates)

    # Graph navigation is PRIMARY — shows the agent where this file
    # connects so agent + GT collaborate on localization
    nav_lines, total_callers = graph_navigation(
        filepath, args.db, iteration_ratio=args.iteration_ratio,
    )

    if nav_lines:
        print("\n".join(nav_lines))
        status = _status_line("success", f"{len(nav_lines)}_items")
        print(status)
        _append_gt_log("status", status)
    else:
        status = _status_line("no_evidence", "no_graph_edges")
        print(status)
        _append_gt_log("status", status)

    log_entry["output_lines"] = len(nav_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        status = _status_line("error", f"{type(exc).__name__}:{exc}")
        print(status)
        _append_gt_log("status", status)
