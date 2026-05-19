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

# Layer 2 (Agent-State Tracker) — FINAL_ARCH_V2 §3. Imported lazily inside
# functions where the in-process AgentState is passed; otherwise the loaders
# below fall back to the legacy /tmp files (subprocess compatibility).
from groundtruth.state.agent_state import (
    LEGACY_BRIEF_CANDIDATES_PATH,
    LEGACY_ISSUE_TERMS_PATH,
    LEGACY_VIEWED_PATH,
)

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


def _load_issue_terms(state: object | None = None) -> set[str]:
    """Issue keywords for issue-aware navigation.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_issue_terms.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        terms = getattr(state, "issue_terms", None)
        if terms:
            return set(terms)
    try:
        text = open(LEGACY_ISSUE_TERMS_PATH, encoding="utf-8").read()
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


def _load_visited_files(state: object | None = None) -> set[str]:
    """Already-viewed file paths.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_viewed.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        visited = getattr(state, "visited_files_set", None)
        if callable(visited):
            try:
                got = visited()
                if got:
                    return set(got)
            except Exception:
                pass
    try:
        text = open(LEGACY_VIEWED_PATH, encoding="utf-8").read()
        return {ln.strip() for ln in text.strip().split("\n") if ln.strip()}
    except OSError:
        return set()


def _load_brief_candidates(state: object | None = None) -> set[str]:
    """Brief candidate file paths.

    Prefers the in-process AgentState (FINAL_ARCH_V2 Layer 2) when provided;
    otherwise reads the legacy ``/tmp/gt_brief_candidates.txt`` mirror for the
    subprocess fallback.
    """
    if state is not None:
        cands = getattr(state, "brief_candidates", None)
        if cands:
            try:
                return {str(c) for c in cands}
            except TypeError:
                pass
    try:
        text = open(LEGACY_BRIEF_CANDIDATES_PATH, encoding="utf-8").read()
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
    _evidence_accumulator: list[dict] | None = None,
    state: object | None = None,
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
    visited_files = _load_visited_files(state)
    # Improvement 3: Load brief candidates for annotation
    brief_candidates = _load_brief_candidates(state)
    # Layer 2: record this view in AgentState if one was supplied
    if state is not None:
        record_view = getattr(state, "record_view", None)
        if callable(record_view):
            try:
                record_view(needle)
            except Exception:
                pass

    # Feature-flagged iteration-aware decay using telemetry constants
    rebuild_l3b = os.environ.get("GT_REBUILD_L3B", "0") == "1"
    _edge_limit_before = limit
    _decay_applied = False
    _iteration_band = "early_0_25"
    if rebuild_l3b:
        try:
            from groundtruth.telemetry.constants import L3B_EDGE_LIMITS, BAND_EARLY, BAND_MID, BAND_LATE, BAND_FINAL
            from groundtruth.telemetry.schemas import get_iteration_band
            _iteration_band = get_iteration_band(int(iteration_ratio * 100), 100)
            _configured_limit = L3B_EDGE_LIMITS.get(_iteration_band, limit)
            if _configured_limit < limit:
                limit = _configured_limit
                _decay_applied = True
        except ImportError:
            if iteration_ratio >= 0.85:
                limit = 0
                _decay_applied = True
            elif iteration_ratio >= 0.60:
                limit = max(1, limit // 2)
                _decay_applied = True

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
        callers = [(row[0], row[1]) for row in cur.fetchall()]
        # Get one representative source_line per caller file for code snippet
        _caller_source_lines: dict[str, int] = {}
        for caller_fp, _ in callers[:10]:
            row = cur.execute(
                """SELECT e.source_line FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path = ? AND nsrc.file_path = ? AND e.source_line > 0
                ORDER BY e.confidence DESC LIMIT 1""",
                (needle, caller_fp),
            ).fetchone()
            if row:
                _caller_source_lines[caller_fp] = row[0]
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
        issue_terms = _load_issue_terms(state)
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

        # Structured capture: decay metadata + edges
        _primary_edge_file: str | None = None
        _primary_edge_kind: str | None = None
        if _evidence_accumulator is not None:
            _evidence_accumulator.append({
                "kind": "l3b_decay_metadata",
                "decay_applied": _decay_applied,
                "edge_limit_before": _edge_limit_before,
                "edge_limit_after": limit,
                "iteration_band": _iteration_band,
                "broad_navigation_after_60pct": iteration_ratio >= 0.60 and not _decay_applied,
            })
        # Mark primary edge (top caller, or top callee if no caller)
        if top_callers:
            _primary_edge_file = top_callers[0][0]
            _primary_edge_kind = "READ_CALLER_CONTRACT"
        elif top_callees:
            _primary_edge_file = top_callees[0][0]
            _primary_edge_kind = "READ_CONSUMER"

        if _evidence_accumulator is not None:
            for i, (fp, cnt) in enumerate(top_callers):
                _evidence_accumulator.append({
                    "kind": "l3b_caller_edge", "file_path": fp,
                    "text": f"{cnt} calls", "source": "graph_db",
                    "reason": f"calls symbol in {needle}",
                    "primary_edge": i == 0,
                })
            for i, (fp, cnt) in enumerate(top_callees):
                _evidence_accumulator.append({
                    "kind": "l3b_callee_edge", "file_path": fp,
                    "text": f"{cnt} calls", "source": "graph_db",
                    "reason": f"called by symbol in {needle}",
                    "primary_edge": i == 0 and not top_callers,
                })

        # Primary-edge rendering (GT_L3B_PRIMARY_EDGE)
        _l3b_primary = os.environ.get("GT_L3B_PRIMARY_EDGE", "0") == "1"

        # Improvement 3 + 5: Brief candidate annotation + symbol-level hints
        def _format_neighbor(fp: str, cnt: int, source_line: int = 0) -> str:
            funcs = _top_functions_for_file(cur, fp, limit=2)
            func_names = ",".join(name for name, _ in funcs) if funcs else ""
            suffix = ""
            if any(fp == c or fp.endswith("/" + c) or c.endswith("/" + fp) for c in brief_candidates):
                suffix = " [CANDIDATE]"
            # Show actual caller code line (mechanism #1: consumption visibility)
            code_snippet = ""
            if source_line > 0 and root:
                try:
                    full_path = os.path.join(root, fp)
                    with open(full_path, encoding="utf-8", errors="ignore") as _cf:
                        lines = _cf.readlines()
                    if source_line <= len(lines):
                        code_snippet = lines[source_line - 1].strip()[:60]
                except OSError:
                    pass
            if code_snippet:
                return f"{fp}:{source_line} `{code_snippet}`{suffix}"
            if func_names:
                return f"{fp}::{func_names} ({cnt}x){suffix}"
            return f"{fp} ({cnt}x){suffix}"

        # Token caps per band (approx chars = tokens * 4)
        _char_caps = {"early_0_25": 1000, "mid_25_60": 640, "late_60_85": 320, "final_85_100": 0}
        _char_cap = _char_caps.get(_iteration_band, 1000) if _l3b_primary else 99999

        if _l3b_primary and iteration_ratio >= 0.25 and _primary_edge_file:
            # After early band: render ONLY primary edge
            primary_formatted = _format_neighbor(_primary_edge_file, top_callers[0][1] if top_callers else (top_callees[0][1] if top_callees else 0))
            label = "Called by" if top_callers else "Calls into"
            line = f"{label}: {primary_formatted}"
            if len(line) <= _char_cap:
                out.append(line)
        elif _l3b_primary and iteration_ratio >= 0.85:
            pass  # Final: silent unless tied to edit/failure
        else:
            # Early band or flag off: render all (original behavior)
            if top_callers:
                caller_files = [_format_neighbor(fp, cnt, _caller_source_lines.get(fp, 0)) for fp, cnt in top_callers]
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
                # Structured capture: importers
                if _evidence_accumulator is not None:
                    for fp in importers:
                        _evidence_accumulator.append({
                            "kind": "l3b_importer_edge", "file_path": fp,
                            "source": "graph_db", "reason": f"imports from {needle}",
                        })

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


def _file_function_spec(db_path: str, file_path: str, repo_root: str) -> str:
    """Show parallel patterns in the viewed file's main functions.

    Delivered at VIEW time = before the agent edits. This is the pre-edit
    specification surface that prevents incomplete fixes.
    """
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT name, start_line, end_line FROM nodes "
            "WHERE file_path = ? AND label IN ('Function','Method') AND is_test = 0 "
            "ORDER BY start_line LIMIT 5",
            (file_path,),
        ).fetchall()
        conn.close()
    except Exception:
        return ""

    if not rows:
        return ""

    from groundtruth.hooks.post_edit import _make_template

    full_path = os.path.join(repo_root, file_path)
    try:
        with open(full_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

    specs = []
    for name, start, end in rows:
        if not start or not end:
            continue
        func_lines = all_lines[max(0, start - 1):min(len(all_lines), end)]
        templates: dict[str, list[str]] = {}
        for line in func_lines:
            stripped = line.strip()
            if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
                continue
            tmpl = _make_template(stripped)
            if tmpl not in templates:
                templates[tmpl] = []
            templates[tmpl].append(stripped)

        groups = [(t, lns) for t, lns in templates.items() if 2 <= len(lns) <= 8]
        if groups:
            groups.sort(key=lambda x: -len(x[1]))
            cases = [ln if len(ln) <= 45 else ln[:42] + "..." for ln in groups[0][1][:4]]
            specs.append(f"{name} handles: {' | '.join(cases)}")

    if not specs:
        return ""
    return "Spec: " + specs[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-view enrichment hook")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--file", required=True, help="File path to enrich")
    parser.add_argument("--iteration-ratio", type=float, default=0.0)
    parser.add_argument("--total-candidates", type=int, default=0)
    parser.add_argument("--structured-output", action="store_true")
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
    _accum = [] if args.structured_output else None
    nav_lines, total_callers = graph_navigation(
        filepath, args.db, iteration_ratio=args.iteration_ratio,
        _evidence_accumulator=_accum,
    )

    # Function spec: show parallel patterns in viewed file's functions (pre-edit context)
    spec_line = _file_function_spec(args.db, filepath, args.root)
    if spec_line:
        nav_lines.append(spec_line)

    if nav_lines:
        print("\n".join(nav_lines))
        if args.structured_output and _accum:
            import json as _json
            print("__GT_STRUCTURED__")
            print(_json.dumps(_accum))
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
