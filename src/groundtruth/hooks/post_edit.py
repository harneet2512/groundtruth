"""Post-edit hook v5 -- graph.db-driven evidence with priority-ordered output.

Called by OpenHands PostToolUse hook on file_editor operations.
Priority order (stop when 300 tokens / ~1200 chars reached):
  1. Caller CODE lines (from graph.db edges.source_line -> read actual line from file)
  2. Sibling function pattern (from graph.db parent_id -> read sibling body snippet)
  3. Signature + return type (from graph.db nodes.signature)
  4. Test assertions (bonus only when available)

Falls back to legacy 5-family evidence when graph.db produces nothing.
Synced with L1 brief: briefed candidates get FULL evidence, 1-hop neighbors get
graph-aware evidence, unbriefed files get minimal (signature + nearest candidate).

Usage:
    python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from groundtruth.hooks.logger import log_hook

_GT_LOG = os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")


def _append_gt_log(event: str, detail: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}\tpost_edit\t{event}"
    if detail:
        line += f"\t{detail}"
    try:
        with open(_GT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _status_line(kind: str, detail: str) -> str:
    return f"[GT_STATUS] {kind}:{detail}"


# ---------------------------------------------------------------------------
# Improved L3 evidence: graph.db-driven, priority-ordered, code-first
# ---------------------------------------------------------------------------

_MAX_EVIDENCE_CHARS = 1200  # ~300 tokens
_BRIEF_CANDIDATES_PATH = "/tmp/gt_brief_candidates.txt"
_EDITED_FILES_PATH = "/tmp/gt_edited_files.txt"
_ISSUE_TERMS_PATH = "/tmp/gt_issue_terms.txt"


def _load_issue_terms() -> set[str]:
    """Load issue keywords written by wrapper at task start."""
    try:
        raw = open(_ISSUE_TERMS_PATH, encoding="utf-8").read().strip()
        if not raw:
            return set()
        return set(raw.lower().split("\n"))
    except OSError:
        return set()


def _compute_caller_relevance(caller: dict[str, str], issue_terms: set[str]) -> float:
    """Fraction of issue terms that appear in caller's file path + code."""
    if not issue_terms:
        return 0.5  # neutral when no issue terms available
    text = (caller.get("file", "") + " " + caller.get("code", "")).lower()
    hits = sum(1 for t in issue_terms if t in text)
    return hits / len(issue_terms)


def _annotate_evidence_header(
    callers: list[dict[str, str]],
    issue_terms: set[str],
    db_path: str = "",
    file_path: str = "",
) -> str:
    """Generate task-relevance annotation header for callers.

    Phase 4 (Contrastive Evidence): when keyword overlap is 0, query graph.db
    for connected files that DO have keyword overlap >= 2 with the issue.
    """
    if not callers or not issue_terms:
        return ""

    relevant_count = sum(
        1 for c in callers if _compute_caller_relevance(c, issue_terms) > 0
    )

    if relevant_count == 0:
        header = "[NOTE] Callers of this file show 0 keyword overlap with the issue.\n"

        # Phase 4: find connected files with keyword overlap
        if db_path and file_path and os.path.exists(db_path):
            try:
                import sqlite3 as _sq3

                conn = _sq3.connect(db_path)
                conn.row_factory = _sq3.Row
                norm_path = file_path.replace("\\", "/").lstrip("/")

                # Get files connected to the edited file (calls or called-by)
                connected_rows = conn.execute(
                    """SELECT DISTINCT n2.file_path
                       FROM nodes n1
                       JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                       JOIN nodes n2 ON (n2.id = e.source_id OR n2.id = e.target_id)
                       WHERE n1.file_path LIKE ? AND n2.file_path NOT LIKE ?
                         AND e.type = 'CALLS'
                       LIMIT 20""",
                    (f"%{norm_path}", f"%{norm_path}"),
                ).fetchall()
                conn.close()

                suggestions: list[str] = []
                for crow in connected_rows:
                    cf = crow["file_path"]
                    cf_lower = cf.lower()
                    overlap = sum(1 for t in issue_terms if t in cf_lower)
                    if overlap >= 2:
                        suggestions.append(f"Connected file {cf} has {overlap} keyword matches")
                    if len(suggestions) >= 2:
                        break

                if suggestions:
                    header += "\n".join(suggestions) + "\n"
            except Exception:
                pass

        return header
    return ""


def _extract_usage_contract(callers: list[dict[str, str]]) -> str:
    """Show literal caller code lines — the actual usage context.

    Takes already-captured caller dicts with 'code' field and formats them
    as literal evidence the agent can reason about directly.
    """
    if not callers:
        return ""

    lines: list[str] = []
    for c in callers[:3]:
        code = c.get("code", "")
        caller_file = c.get("file", "")
        line_num = c.get("line", "")
        if not code:
            continue
        code_clean = code.replace(" | ", " → ").strip()
        if len(code_clean) > 90:
            code_clean = code_clean[:87] + "..."
        if caller_file and line_num:
            lines.append(f"{caller_file}:{line_num} `{code_clean}`")
        elif code_clean:
            lines.append(f"`{code_clean}`")

    if not lines:
        return ""
    return "CALLERS: " + " | ".join(lines)


import re as _re

_TEMPLATE_SUBS = [
    (_re.compile(r'"[^"]*"'), 'STRING'),
    (_re.compile(r"'[^']*'"), 'STRING'),
    (_re.compile(r'\b\d+\b'), 'NUM'),
]


def _make_template(line: str) -> str:
    """Reduce a code line to its structural pattern by replacing literals."""
    t = line.strip()
    for pat, repl in _TEMPLATE_SUBS:
        t = pat.sub(repl, t)
    return t


def _detect_structural_twins(
    file_path: str,
    func_start: int,
    func_end: int,
) -> str:
    """Find structural twins within a function — lines sharing the same pattern.

    Detects when a function has multiple lines with identical structure but
    different values (e.g., multiple env var checks, multiple regex patterns,
    multiple elif branches). Shows them so the agent verifies consistency.
    """
    try:
        with open(file_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

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
        if tmpl not in templates:
            templates[tmpl] = []
        templates[tmpl].append((start + i + 1, stripped))

    twin_groups = [(tmpl, entries) for tmpl, entries in templates.items()
                   if len(entries) >= 2 and len(entries) <= 6]

    if not twin_groups:
        return ""

    twin_groups.sort(key=lambda x: -len(x[1]))
    best = twin_groups[0]
    entries = best[1]

    parts: list[str] = []
    for line_num, code in entries[:3]:
        code_short = code if len(code) <= 70 else code[:67] + "..."
        parts.append(f"L{line_num}: `{code_short}`")

    return "TWINS: " + " | ".join(parts)


def _detect_edit_propagation(
    db_path: str, file_path: str, func_name: str, repo_root: str,  # noqa: ARG001
) -> str:
    """Find call sites that may need updating after a function edit.

    Research: CodePlan (FSE 2024) — 5/7 repos pass with propagation vs 0/7 without.
    After editing a function, callers that pass specific args or destructure
    the return value may need corresponding updates.
    """
    try:
        import sqlite3 as _sql
        conn = _sql.connect(db_path)
        rows = conn.execute(
            """
            SELECT DISTINCT nsrc.file_path, e.source_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
              AND e.confidence >= 0.9
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.name = ? AND nt.file_path = ?
              AND nsrc.file_path != nt.file_path
              AND nsrc.is_test = 0
              AND e.source_line > 0
            ORDER BY e.source_line
            LIMIT 5
            """,
            (func_name, file_path),
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        sites: list[str] = []
        for caller_file, line_num in rows[:3]:
            sites.append(f"{caller_file}:{line_num}")

        if sites:
            return f"PROPAGATE: {len(rows)} call sites may need updating: {', '.join(sites)}"
    except Exception:
        pass
    return ""


def _co_change_reminder(file_path: str, repo_root: str, edited_files: list[str]) -> str:
    """Show files that historically co-change but haven't been edited yet.

    Research: HAFixAgent (arXiv 2025) +56.6% from git history context.
    ESEM 2024: co-change prediction with structural deps improves impact prediction.
    """
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-15", "--", file_path],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return ""
    except Exception:
        return ""

    co_counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        f = line.strip()
        if f and f != file_path and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml", ".toml")):
            co_counts[f] = co_counts.get(f, 0) + 1

    edited_set = set(edited_files)
    unedited_co = [(f, c) for f, c in co_counts.items() if f not in edited_set and c >= 2]
    unedited_co.sort(key=lambda x: -x[1])

    if not unedited_co:
        return ""

    top = unedited_co[:2]
    parts = [f"{f} ({c}x)" for f, c in top]
    return f"CO-CHANGE: typically also changes: {', '.join(parts)}"


def _scope_completeness(edited_files: list[str], file_path: str, repo_root: str) -> str:
    """Warn if edit scope seems incomplete based on historical patterns.

    Research: 60% of SWE-bench-Verified requires multi-component patches.
    Agents systematically under-edit (ASE 2025 multi-hunk study).
    """
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:COMMIT", "-30", "--", file_path],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return ""
    except Exception:
        return ""

    commit_file_counts: list[int] = []
    current_count = 0
    for line in result.stdout.splitlines():
        if line.strip() == "COMMIT":
            if current_count > 0:
                commit_file_counts.append(current_count)
            current_count = 0
        elif line.strip():
            current_count += 1
    if current_count > 0:
        commit_file_counts.append(current_count)

    if not commit_file_counts:
        return ""

    avg_files = sum(commit_file_counts) / len(commit_file_counts)
    current_edited = len(set(edited_files))

    if avg_files > 1.5 and current_edited == 1:
        return f"SCOPE: commits to this file typically touch {avg_files:.1f} files (you've edited {current_edited})"
    return ""


def _read_lines_file(path: str) -> list[str]:
    """Read a file containing one path per line. Returns [] on any error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []


def _read_source_line(full_path: str, line_no: int, extra_lines: int = 0, end_line: int = 0) -> str:
    """Read a source line + optional context lines after it. Returns '' on failure."""
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
                    if any(stripped.lstrip().startswith(kw) for kw in ("def ", "async def ", "class ", "func ", "function ", "fn ")):
                        break
                    lines_to_read.append(stripped)
                elif lines_to_read:
                    break
        return " | ".join(lines_to_read) if lines_to_read else ""
    except OSError:
        return ""


def _read_source_lines(full_path: str, start: int, end: int) -> str:
    """Read lines [start, end] from a source file. Returns '' on failure."""
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f, 1):
                if i >= start and i <= end:
                    lines.append(line.rstrip())
                if i > end:
                    break
            return "\n".join(lines)
    except OSError:
        return ""




def _get_callers_from_graph(
    db_path: str, file_path: str, function_name: str, repo_root: str,
    seen_files: list[str], limit: int = 5
) -> list[dict[str, str]]:
    """Query graph.db for cross-file callers with confidence >= 0.5.

    Returns list of dicts: {file, line, caller_name, code}
    Filters out callers from files the agent has already visited.
    """
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row

        # Check if confidence column exists
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        has_confidence = "confidence" in cols
        conf_filter = "AND e.confidence >= 0.5" if has_confidence else ""

        query = f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path LIKE ? AND nt.name = ?
              {conf_filter}
              AND nsrc.file_path != nt.file_path
            ORDER BY {"e.confidence DESC," if has_confidence else ""} e.source_line
            LIMIT ?
        """
        # Use LIKE with % suffix match for path flexibility
        norm_path = file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(query, (f"%{norm_path}", function_name, limit + 10)).fetchall()

        seen_norm = {s.replace("\\", "/").lstrip("/") for s in seen_files}

        for row in rows:
            caller_file = row["file_path"]
            source_line = row["source_line"]
            caller_name = row["name"]
            caller_norm = caller_file.replace("\\", "/").lstrip("/")

            # Mark whether agent has seen this file
            is_unseen = caller_norm not in seen_norm

            # Read the actual code line + 2 lines of context
            code = ""
            caller_end = row["end_line"] or 0
            if source_line and source_line > 0:
                full_path = os.path.join(repo_root, caller_file)
                code = _read_source_line(full_path, source_line, extra_lines=2, end_line=caller_end)

            results.append({
                "file": caller_file,
                "line": str(source_line or "?"),
                "caller_name": caller_name,
                "code": code,
                "unseen": "1" if is_unseen else "0",
            })

            if len(results) >= limit:
                break

        # Phase 2: Dynamic Hops — follow thin wrappers (max 2 hops total)
        # If only 1 caller exists, check if it's a thin wrapper (<3 callers itself)
        # and if so, append the wrapper's callers for additional context.
        if len(results) == 1:
            wrapper = results[0]
            wrapper_name = wrapper["caller_name"]
            wrapper_file = wrapper["file"]
            wrapper_norm = wrapper_file.replace("\\", "/").lstrip("/")

            hop2_query = f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, nsrc.end_line
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.file_path LIKE ? AND nt.name = ?
                  {conf_filter}
                  AND nsrc.file_path != nt.file_path
                ORDER BY {"e.confidence DESC," if has_confidence else ""} e.source_line
                LIMIT 5
            """
            hop2_rows = conn.execute(
                hop2_query, (f"%{wrapper_norm}", wrapper_name, )
            ).fetchall()

            # Only follow if the wrapper has <3 callers (thin wrapper pattern)
            if 0 < len(hop2_rows) < 3:
                for h2row in hop2_rows:
                    h2_file = h2row["file_path"]
                    h2_line = h2row["source_line"]
                    h2_name = h2row["name"]
                    h2_norm = h2_file.replace("\\", "/").lstrip("/")

                    is_unseen = h2_norm not in seen_norm

                    code = ""
                    h2_end = h2row["end_line"] or 0
                    if h2_line and h2_line > 0:
                        full_path = os.path.join(repo_root, h2_file)
                        code = _read_source_line(
                            full_path, h2_line, extra_lines=2, end_line=h2_end
                        )
                    if code:
                        code = f"[via wrapper] {code}"

                    results.append({
                        "file": h2_file,
                        "line": str(h2_line or "?"),
                        "caller_name": h2_name,
                        "code": code,
                        "unseen": "1" if is_unseen else "0",
                    })

                    if len(results) >= limit:
                        break

        conn.close()

    except Exception:
        pass

    return results


def _get_signature_from_graph(db_path: str, file_path: str, function_name: str) -> str:
    """Get function signature + return type from graph.db."""
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        norm_path = file_path.replace("\\", "/").lstrip("/")
        row = conn.execute(
            "SELECT signature, return_type FROM nodes "
            "WHERE file_path LIKE ? AND name = ? AND label IN ('Function', 'Method') LIMIT 1",
            (f"%{norm_path}", function_name),
        ).fetchone()
        conn.close()
        if row:
            sig = row["signature"] or ""
            ret = row["return_type"] or ""
            if sig:
                return sig if ret and ret in sig else f"{sig} -> {ret}" if ret else sig
            elif ret:
                return f"def {function_name}(...) -> {ret}"
        return ""
    except Exception:
        return ""


def _get_siblings_from_graph(
    db_path: str, file_path: str, function_name: str, repo_root: str
) -> list[dict[str, str]]:
    """Get sibling functions (same class/file) from graph.db with a body snippet."""
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        norm_path = file_path.replace("\\", "/").lstrip("/")

        # Find target node
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

        # Get siblings
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
            sib_name = sib["name"]
            sib_sig = sib["signature"] or ""
            sib_file = sib["file_path"]
            start = sib["start_line"] or 0
            end = sib["end_line"] or 0

            # Read first 2 lines of sibling body for pattern snippet
            snippet = ""
            if start > 0 and end > 0:
                full_path = os.path.join(repo_root, sib_file)
                body_start = start + 1  # skip def line
                body_end = min(start + 3, end)  # 2-3 lines max
                snippet = _read_source_lines(full_path, body_start, body_end)

            results.append({
                "name": sib_name,
                "signature": sib_sig,
                "snippet": snippet.strip(),
            })

    except Exception:
        pass

    return results


def _get_test_assertions_from_graph(
    db_path: str, file_path: str, function_name: str
) -> list[dict[str, str]]:
    """Get test assertions targeting this function from graph.db."""
    import sqlite3 as _sqlite3

    results: list[dict[str, str]] = []
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row

        # Check if assertions table exists
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "assertions" not in tables:
            conn.close()
            return []

        norm_path = file_path.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            """SELECT a.kind, a.expression, a.expected, a.line, n.name as test_name, n.file_path
               FROM assertions a
               JOIN nodes n ON a.test_node_id = n.id
               JOIN nodes target ON a.target_node_id = target.id
               WHERE target.file_path LIKE ? AND target.name = ?
               ORDER BY a.line LIMIT 3""",
            (f"%{norm_path}", function_name),
        ).fetchall()
        conn.close()

        for row in rows:
            results.append({
                "kind": row["kind"] or "",
                "expression": row["expression"] or "",
                "expected": row["expected"] or "",
                "test_name": row["test_name"] or "",
                "test_file": row["file_path"] or "",
            })
    except Exception:
        pass

    return results


def _find_nearest_candidate(
    file_path: str, brief_candidates: list[str], db_path: str
) -> str:
    """Find the nearest brief candidate connected to this file via graph.db edges."""
    import sqlite3 as _sqlite3

    if not brief_candidates:
        return ""
    try:
        conn = _sqlite3.connect(db_path)
        norm_path = file_path.replace("\\", "/").lstrip("/")

        for cand in brief_candidates:
            cand_norm = cand.replace("\\", "/").lstrip("/")
            # Check if there's an edge between this file and the candidate
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM edges e
                   JOIN nodes nsrc ON e.source_id = nsrc.id
                   JOIN nodes ntgt ON e.target_id = ntgt.id
                   WHERE (nsrc.file_path LIKE ? AND ntgt.file_path LIKE ?)
                      OR (nsrc.file_path LIKE ? AND ntgt.file_path LIKE ?)
                   LIMIT 1""",
                (f"%{norm_path}", f"%{cand_norm}", f"%{cand_norm}", f"%{norm_path}"),
            ).fetchone()
            if row and row[0] > 0:
                conn.close()
                return cand

        conn.close()
    except Exception:
        pass

    # If no graph connection found, return first candidate as reference
    return brief_candidates[0] if brief_candidates else ""


def _get_targeted_verification_suggestion(
    db_path: str, file_path: str, function_names: list[str],
) -> str:
    """Query graph.db for test file connected to edited function. Returns one suggestion or ''."""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        norm = file_path.replace("\\", "/").lstrip("/")
        for func_name in function_names[:2]:
            rows = conn.execute(
                """SELECT DISTINCT n2.file_path, n2.name
                   FROM nodes n1
                   JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
                   JOIN nodes n2 ON (
                       CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END = n2.id
                   )
                   WHERE n1.file_path LIKE ? AND n1.name = ? AND n2.is_test = 1
                   LIMIT 1""",
                (f"%{norm}", func_name),
            ).fetchall()
            if rows:
                test_file = rows[0][0]
                test_name = rows[0][1]
                conn.close()
                return f"[GT_VERIFY] Run: pytest {test_file}::{test_name}"
        conn.close()
    except Exception:
        pass
    return ""


def generate_improved_evidence(
    file_path: str,
    function_names: list[str],
    db_path: str,
    repo_root: str,
    *,
    mode: str = "post_edit",
    iteration_ratio: float = 0.0,
    _evidence_accumulator: list[dict] | None = None,
) -> str:
    """Generate priority-ordered evidence from graph.db.

    Priority order (stop at 1200 chars / ~300 tokens):
      1. Caller CODE lines (unseen by agent first)
      2. Sibling function pattern
      3. Signature + return type
      4. Test assertions (bonus)

    Decision 22 Fix 5: L3 fully decoupled from L1. Evidence depth is
    determined by the file's graph connectivity (edge confidence), not
    by whether L1 produced candidates. Files with high-confidence edges
    (≥0.5) get full evidence; files with only low-confidence or no edges
    get signature-only.

    Dynamic:
      - Tracks edited_files for unseen-caller prioritization
      - Decay: full on first 3 edits, lighter after
    """
    if not os.path.exists(db_path):
        return ""

    # Load trajectory state
    edited_files = _read_lines_file(_EDITED_FILES_PATH)
    edit_count = len(edited_files)

    # Load issue terms once for task-relevance annotation (Decision 25)
    issue_terms = _load_issue_terms()

    # Classify file by graph connectivity — decoupled from L1 brief.
    # Query: does this file have any edges with confidence >= 0.5?
    file_class = "minimal"
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cols = [d[0] for d in cur.execute("PRAGMA table_info(edges)").fetchall()]
        has_confidence = "confidence" in [c[1] if isinstance(c, (list, tuple)) else c for c in cols]
        if not has_confidence:
            cols_by_name = [row[1] for row in cur.execute("PRAGMA table_info(edges)").fetchall()]
            has_confidence = "confidence" in cols_by_name
        norm_file = file_path.replace("\\", "/").lstrip("/")
        edge_count = cur.execute(
            "SELECT COUNT(*) FROM edges e JOIN nodes n ON e.source_id = n.id "
            "WHERE n.file_path LIKE ? AND e.type = 'CALLS'"
            + (" AND e.confidence >= 0.5" if has_confidence else ""),
            (f"%{norm_file}",),
        ).fetchone()[0]
        conn.close()
        if edge_count > 0:
            file_class = "connected"
    except Exception:
        file_class = "connected"  # default to showing evidence on error

    # Decay: after 3 edits, reduce evidence density
    base_max = 3 if edit_count <= 3 else 2
    max_callers = base_max  # adjusted per-function below

    # Feature-flagged mode support (Change 3)
    rebuild_l3 = os.environ.get("GT_REBUILD_L3", "0") == "1"
    effective_mode = mode if rebuild_l3 else "post_edit"
    effective_ratio = iteration_ratio if rebuild_l3 else 0.0

    # Late-repair mode: reduced cap (Change 4)
    _LATE_REPAIR_MAX_CHARS = 600
    effective_max_chars = _LATE_REPAIR_MAX_CHARS if (effective_ratio >= 0.60 and effective_mode == "post_edit") else _MAX_EVIDENCE_CHARS

    output_parts: list[str] = []
    chars_used = 0

    # Post-failure mode header
    if effective_mode == "post_failure":
        output_parts.append("[GT L3: post_failure]")
        chars_used += 25

    for func_name in function_names[:3]:  # limit to 3 functions per edit
        func_parts: list[str] = []
        callers: list[dict[str, str]] = []
        total_callers = 0

        # --- Post-failure mode: test assertions first (Change 3) ---
        if effective_mode == "post_failure" and file_class == "connected" and chars_used < effective_max_chars - 150:
            assertions = _get_test_assertions_from_graph(db_path, file_path, func_name)
            if assertions:
                func_parts.append("TEST ASSERTIONS:")
                for a in assertions[:2]:
                    expr = a["expression"][:60] if a["expression"] else ""
                    expected = a["expected"][:30] if a["expected"] else ""
                    test_ref = f"{a['test_name']}" if a["test_name"] else "test"
                    if expr:
                        func_parts.append(f"  {test_ref} asserts {expr} == {expected}")

        # --- Late-repair: only signature + top 1 caller (Change 4) ---
        if effective_ratio >= 0.60 and effective_mode == "post_edit":
            sig = _get_signature_from_graph(db_path, file_path, func_name)
            if sig:
                func_parts.append(f"SIGNATURE: {sig}")
                if " -> " in sig:
                    ret_type = sig.split(" -> ")[-1].strip()
                    if ret_type and ret_type != "None":
                        func_parts.append(f"MUST PRESERVE: returns {ret_type}")
            callers = _get_callers_from_graph(
                db_path, file_path, func_name, repo_root,
                seen_files=edited_files, limit=3,
            )
            if callers:
                func_parts.append("TOP CALLER:")
                c = callers[0]
                code = c["code"]
                if code:
                    func_parts.append(f"  {c['file']}:{c['line']}  → {code}")
            # Skip full evidence pipeline for late repair
            if func_parts:
                block = "\n".join(func_parts)
                if chars_used + len(block) <= effective_max_chars:
                    output_parts.append(block)
                    chars_used += len(block) + 1
            continue

        # --- Priority 1: Caller CODE lines ---
        if file_class == "connected":
            callers = _get_callers_from_graph(
                db_path, file_path, func_name, repo_root,
                seen_files=edited_files,
                limit=base_max + 10,  # fetch extra for dynamic count + MUST PRESERVE
            )
            # Dynamic caller count: show more for lightly-called, fewer for hubs
            total_callers = len(callers)
            if total_callers <= 5:
                max_callers = total_callers
            elif total_callers <= 15:
                max_callers = base_max
            else:
                max_callers = max(base_max - 1, 2)

            unseen_callers = [c for c in callers if c["unseen"] == "1"]
            seen_callers = [c for c in callers if c["unseen"] == "0"]
            # Prioritize unseen
            ordered_callers = unseen_callers + seen_callers

            if ordered_callers:
                unseen_count = len(unseen_callers)
                label = f"CALLERS ({unseen_count} unseen)" if unseen_count > 0 else "CALLERS"
                # Decision 25: task-relevance annotation header
                annotation_header = _annotate_evidence_header(
                    ordered_callers, issue_terms,
                    db_path=db_path, file_path=file_path,
                )
                if annotation_header:
                    func_parts.append(annotation_header.rstrip())
                func_parts.append(f"{label}:")
                for c in ordered_callers[:max_callers]:
                    # Decision 29: removed [issue-relevant] inline tag — it drew
                    # agent attention to callers, encouraging cascading edits.
                    code = c["code"]
                    if code:
                        func_parts.append(f"  {c['file']}:{c['line']}  → {code}")
                    else:
                        func_parts.append(f"  {c['file']}:{c['line']}  ({c['caller_name']})")

            # Contract extraction from caller usage patterns (SYNFIX mechanism)
            contract_line = _extract_usage_contract(ordered_callers[:max_callers])
            if contract_line:
                func_parts.append(f"  {contract_line}")

        # --- Structural twin detection (edit consistency) ---
        if chars_used < effective_max_chars - 100:
            try:
                import sqlite3 as _sql3
                _tc = _sql3.connect(db_path)
                _frow = _tc.execute(
                    "SELECT start_line, end_line FROM nodes WHERE file_path = ? AND name = ? AND label IN ('Function','Method') LIMIT 1",
                    (file_path, func_name),
                ).fetchone()
                _tc.close()
                if _frow and _frow[0] and _frow[1]:
                    full_file = os.path.join(repo_root, file_path)
                    twin_line = _detect_structural_twins(full_file, _frow[0], _frow[1])
                    if twin_line:
                        func_parts.append(f"  {twin_line}")
            except Exception:
                pass

        # --- Edit propagation (CodePlan mechanism) ---
        if chars_used < effective_max_chars - 80:
            prop_line = _detect_edit_propagation(db_path, file_path, func_name, repo_root)
            if prop_line:
                func_parts.append(f"  {prop_line}")

        # --- Co-change reminder (HAFixAgent mechanism) ---
        if chars_used < effective_max_chars - 60:
            co_line = _co_change_reminder(file_path, repo_root, edited_files)
            if co_line:
                func_parts.append(f"  {co_line}")

        # --- Scope completeness (multi-hunk awareness) ---
        if chars_used < effective_max_chars - 60:
            scope_line = _scope_completeness(edited_files, file_path, repo_root)
            if scope_line:
                func_parts.append(f"  {scope_line}")

        # Structured capture: callers
        if _evidence_accumulator is not None and file_class == "connected" and callers:
            for c in callers[:5]:
                _evidence_accumulator.append({
                    "kind": "l3_caller_code", "file_path": c["file"],
                    "symbol": c.get("caller_name", ""),
                    "line_start": int(c.get("line", 0) or 0),
                    "text": c.get("code", ""), "source": "graph_db",
                    "reason": "calls edited function",
                    })

        # --- Blast Radius Warning (Phase 3) ---
        if total_callers > 5:
            func_parts.append(
                f"⚠ BLAST RADIUS: {total_callers} callers depend on this function"
            )

        # --- Priority 2: Test assertions (behavioral contract) ---
        if file_class == "connected" and chars_used < _MAX_EVIDENCE_CHARS - 150:
            assertions = _get_test_assertions_from_graph(db_path, file_path, func_name)
            if assertions:
                for a in assertions[:2]:
                    expr = a["expression"][:60] if a["expression"] else ""
                    expected = a["expected"][:30] if a["expected"] else ""
                    test_ref = f"{a['test_name']}" if a["test_name"] else "test"
                    if expr:
                        func_parts.append(f"TEST: {test_ref} asserts {expr} == {expected}")

            # Structured capture: test assertions
            if _evidence_accumulator is not None and assertions:
                for a in assertions[:2]:
                    _evidence_accumulator.append({
                        "kind": "l3_test_assertion", "file_path": a.get("test_file", ""),
                        "symbol": a.get("test_name", ""), "text": a.get("expression", ""),
                        "source": "graph_db",
                    })

        # --- Priority 3: Signature + return type ---
        sig = _get_signature_from_graph(db_path, file_path, func_name)
        if sig:
            func_parts.append(f"SIGNATURE: {sig}")
            # Structured capture: signature
            if _evidence_accumulator is not None:
                _evidence_accumulator.append({
                    "kind": "l3_signature", "file_path": file_path,
                    "symbol": func_name, "text": sig, "source": "graph_db",
                })

            # Add MUST PRESERVE if there are callers depending on return type
            if callers and " -> " in sig:
                ret_type = sig.split(" -> ")[-1].strip()
                if ret_type and ret_type != "None":
                    func_parts.append(
                        f"MUST PRESERVE: returns {ret_type} ({len(callers)} callers depend on this)"
                    )

        # --- Priority 4: Sibling pattern ---
        if file_class == "connected" and chars_used < _MAX_EVIDENCE_CHARS - 200:
            siblings = _get_siblings_from_graph(db_path, file_path, func_name, repo_root)
            if siblings:
                for sib in siblings:
                    if sib["snippet"]:
                        func_parts.append(f"SIBLING: {sib['name']} uses: {sib['snippet'][:80]}")
                        break
                else:
                    sib = siblings[0]
                    if sib["signature"]:
                        func_parts.append(f"SIBLING: {sib['name']}: {sib['signature'][:80]}")

            # Structured capture: siblings
            if _evidence_accumulator is not None and siblings:
                for sib in siblings[:2]:
                    _evidence_accumulator.append({
                        "kind": "l3_sibling_pattern", "file_path": file_path,
                        "symbol": sib.get("name", ""),
                        "text": sib.get("snippet", "") or sib.get("signature", ""),
                        "source": "graph_db",
                    })

        # (Removed: tiered unbriefed minimal evidence — all files now get full pipeline)

        # Accumulate
        if func_parts:
            block = "\n".join(func_parts)
            if chars_used + len(block) > _MAX_EVIDENCE_CHARS:
                # Truncate to fit
                remaining = _MAX_EVIDENCE_CHARS - chars_used
                if remaining > 50:
                    block = block[:remaining]
                    output_parts.append(block)
                break
            output_parts.append(block)
            chars_used += len(block) + 1  # +1 for separator newline

    if not output_parts:
        return ""

    # Targeted verification suggestion (Change 3): added in ALL modes
    if rebuild_l3 and chars_used < effective_max_chars - 80:
        verify_line = _get_targeted_verification_suggestion(db_path, file_path, function_names)
        if verify_line:
            output_parts.append(verify_line)
            if _evidence_accumulator is not None:
                _evidence_accumulator.append({
                    "kind": "l3_targeted_verification",
                    "text": verify_line, "source": "graph_db",
                    "reason": "targeted test for edited symbol",
                })

    # Wrap in structured format
    norm_path = file_path.replace("\\", "/").lstrip("/")
    mode_attr = f' mode="{effective_mode}"' if rebuild_l3 and effective_mode != "post_edit" else ""
    header = f'<gt-evidence trigger="post_edit:{norm_path}"{mode_attr}>'
    footer = "</gt-evidence>"
    body = "\n".join(output_parts)

    # Final cap check using effective max
    full_output = f"{header}\n{body}\n{footer}"
    if len(full_output) > effective_max_chars + 100:
        body = body[: effective_max_chars - len(header) - len(footer) - 5]
        full_output = f"{header}\n{body}\n{footer}"

    return full_output


def _git_env() -> dict[str, str]:
    """Git environment that handles safe.directory in containers."""
    import copy

    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


def _detect_workspace_root(provided_root: str) -> str:
    """Detect the actual workspace root dynamically.

    1. Try git rev-parse --show-toplevel from the provided root.
    2. If that fails, scan /workspace/*/ for a .git directory.
    3. Fall back to the provided root.
    """
    # Step 1: try git rev-parse from the provided root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=provided_root,
            timeout=5,
            env=_git_env(),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return toplevel
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, NotADirectoryError):
        pass

    # Step 2: scan /workspace/*/ for a .git directory
    try:
        workspace_dirs = _glob.glob("/workspace/*/")
        for candidate in sorted(workspace_dirs):
            if os.path.isdir(os.path.join(candidate, ".git")):
                return candidate.rstrip("/")
    except OSError:
        pass

    # Step 3: fall back to the provided root
    return provided_root


def _is_view_operation() -> bool:
    """Return True if the current hook invocation is for a view-only operation.

    OpenHands sets TOOL_INPUT or OPENHANDS_TOOL_INPUT to a JSON payload
    containing the tool arguments. If the payload has {"command": "view"}
    we skip all processing — no diff was produced.
    """
    for env_var in ("TOOL_INPUT", "OPENHANDS_TOOL_INPUT"):
        raw = os.environ.get(env_var, "")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("command") == "view":
                return True
        except (json.JSONDecodeError, ValueError):
            pass
    return False


_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".py",
        ".go",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".cs",
        ".php",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".rb",
        ".ex",
        ".exs",
        ".lua",
        ".ml",
        ".groovy",
        ".gradle",
        ".mjs",
        ".cjs",
    }
)


def _get_modified_files(root: str) -> list[str]:
    """Get modified source files from git diff (all supported languages)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return [
            f.strip()
            for f in result.stdout.strip().split("\n")
            if f.strip() and os.path.splitext(f.strip())[1].lower() in _SUPPORTED_EXTENSIONS
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _git_diff_path(root: str, relpath: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", relpath],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _is_untracked(root: str, relpath: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relpath],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
            env=_git_env(),
        )
        return result.returncode != 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True


def _synthetic_diff_new_file(relpath: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join("+" + ln for ln in lines)
    return (
        f"diff --git a/{relpath} b/{relpath}\nnew file\n--- /dev/null\n+++ b/{relpath}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def _read_file(root: str, relpath: str) -> str:
    try:
        with open(os.path.join(root, relpath), "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _read_text_file(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _git_show_head_file(root: str, relpath: str) -> str:
    if not relpath:
        return ""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{relpath}"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
            env=_git_env(),
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _reconstruct_old_content_from_diff(diff_text: str, relpath: str) -> str:
    """Rebuild old-side content from unified diff hunks for one file."""
    if not diff_text:
        return ""
    target = relpath.strip().replace("\\", "/").lstrip("/")
    if not target:
        return ""
    lines = diff_text.splitlines()
    in_file = False
    in_hunk = False
    old_lines: list[str] = []
    for line in lines:
        if line.startswith("+++ b/"):
            file_path = line[6:].strip().replace("\\", "/").lstrip("/")
            in_file = file_path == target
            in_hunk = False
            continue
        if not in_file:
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("---") or line.startswith("diff --git"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            old_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
    return "\n".join(old_lines).strip()


def _extract_diff_added_lines(diff_text: str, relpath: str) -> list[str]:
    target = relpath.strip().replace("\\", "/").lstrip("/")
    lines = diff_text.splitlines()
    in_file = False
    in_hunk = False
    added: list[str] = []
    for line in lines:
        if line.startswith("+++ b/"):
            file_path = line[6:].strip().replace("\\", "/").lstrip("/")
            in_file = file_path == target
            in_hunk = False
            continue
        if not in_file:
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


def _count_top_level_args(arg_blob: str) -> int:
    blob = arg_blob.strip()
    if not blob:
        return 0
    depth = 0
    count = 1
    for ch in blob:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


class _SimpleFinding:
    def __init__(self, family: str, message: str, confidence: float) -> None:
        self.family = family
        self.message = message
        self.confidence = confidence


def _sibling_pattern_fallback(source: str, diff_text: str, relpath: str) -> list[_SimpleFinding]:
    """Detect constructor-pattern drift in data-heavy files."""
    if not source or not diff_text or not relpath:
        return []
    call_re = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\(([^()\n]*)\)")
    all_calls = call_re.findall(source)
    if not all_calls:
        return []

    freq: dict[str, int] = {}
    arg_hist: dict[str, list[int]] = {}
    for ctor, args in all_calls:
        freq[ctor] = freq.get(ctor, 0) + 1
        arg_hist.setdefault(ctor, []).append(_count_top_level_args(args))

    repeated_ctors = {k for k, v in freq.items() if v >= 5}
    if not repeated_ctors:
        return []

    mode_args: dict[str, int] = {}
    for ctor in repeated_ctors:
        counts: dict[int, int] = {}
        for arg_count in arg_hist.get(ctor, []):
            counts[arg_count] = counts.get(arg_count, 0) + 1
        mode_args[ctor] = max(counts, key=counts.get) if counts else 0

    findings: list[_SimpleFinding] = []
    for line in _extract_diff_added_lines(diff_text, relpath):
        match = call_re.search(line)
        if not match:
            continue
        ctor, args_blob = match.group(1), match.group(2)
        if ctor not in repeated_ctors:
            continue
        observed = _count_top_level_args(args_blob)
        expected = mode_args.get(ctor, observed)
        if observed != expected:
            findings.append(
                _SimpleFinding(
                    family="pattern",
                    message=(
                        f"{ctor} constructor shape mismatch in sibling pattern "
                        f"(expected {expected} args, got {observed})"
                    ),
                    confidence=0.72,
                )
            )
    return findings


def _merge_modified_with_explicit(
    root: str, modified: list[str], explicit: str
) -> tuple[list[str], str]:
    """Merge wrapper-provided file path into modified list + diff (handles new/untracked files)."""

    diff_text = _get_diff_text(root)
    exp = explicit.strip().replace("\\", "/").lstrip("/")
    if not exp:
        return modified, diff_text

    join_path = os.path.join(root, exp)
    merged = list(modified)
    if exp not in merged and os.path.isfile(join_path):
        merged = [exp] + [f for f in merged if f != exp]

    if not os.path.isfile(join_path):
        return merged, diff_text

    p_diff = _git_diff_path(root, exp)
    file_marker = f"+++ b/{exp}"
    if p_diff.strip():
        if not diff_text.strip() or file_marker not in diff_text:
            diff_text = p_diff if not diff_text.strip() else diff_text + "\n" + p_diff
    elif _is_untracked(root, exp):
        synth = _synthetic_diff_new_file(exp, _read_file(root, exp))
        if not diff_text.strip() or file_marker not in diff_text:
            diff_text = synth if not diff_text.strip() else diff_text + "\n" + synth

    return merged, diff_text


def _extract_changed_func_names(diff_text: str) -> dict[str, list[str]]:
    """Parse diff to find changed function names per file.

    Returns dict: filepath -> list of function names in changed line ranges.
    """

    # Parse diff for file + line ranges
    changes: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif (
            line.startswith("@@")
            and current_file
            and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS
        ):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                changes.setdefault(current_file, []).append((start, start + count - 1))

    # Map line ranges to function names
    result: dict[str, list[str]] = {}
    for fpath, ranges in changes.items():
        # We'd need to parse the CURRENT file to find functions at those lines
        # This is done by the caller who has the AST
        result[fpath] = []  # Populated later when we have the source

    return result


def _find_funcs_at_lines(
    source: str, line_ranges: list[tuple[int, int]], file_path: str = "", store=None
) -> list[str]:
    """Find function/method names that overlap with given line ranges.

    Uses graph.db node positions when available, falls back to Python AST.
    """
    # Path 1: graph.db (language-agnostic)
    if store and file_path:
        try:
            funcs = store.get_functions_in_file(file_path)
            if funcs:
                names = []
                for func in funcs:
                    fs, fe = func["start_line"], func["end_line"]
                    for ls, le in line_ranges:
                        if fs <= le and ls <= fe:
                            names.append(func["name"])
                            break
                if names:
                    return names
        except Exception:
            pass

    # Path 2: Python AST (for .py files)
    if file_path.endswith(".py") or not file_path:
        import ast as _ast

        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return []
        func_names = []
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_start = node.lineno
                func_end = getattr(node, "end_lineno", func_start + 50)
                for ls, le in line_ranges:
                    if func_start <= le and ls <= func_end:
                        func_names.append(node.name)
                        break
        return func_names

    # Path 3: Regex fallback for non-Python without graph.db
    func_names = []
    lines = source.splitlines()
    func_pattern = re.compile(
        r"\s*(?:(?:pub\s+)?(?:async\s+)?(?:def|func|function|fn|fun)\s+)(\w+)"
    )
    for ls, le in line_ranges:
        for i in range(max(0, ls - 10), min(len(lines), le + 5)):
            m = func_pattern.match(lines[i] if i < len(lines) else "")
            if m and m.group(1) not in func_names:
                func_names.append(m.group(1))
    return func_names


def _apply_abstention(findings: list, min_confidence: float | None = None) -> list:
    """Universal abstention across all evidence families (Dynamic/Agnostic)."""
    if min_confidence is None:
        # SweRank-style: reduce abstention floor to allow more signal in sparse repos.
        # Fallback to 0.40 instead of 0.55 to prevent the 'hard funnel' failure mode.
        min_confidence = float(os.environ.get("GT_MIN_CONFIDENCE", "0.40"))

    passed = []
    for f in findings:
        conf = getattr(f, "confidence", 0)
        if conf < min_confidence:
            continue
        # Skip private methods
        msg = getattr(f, "message", "")
        if msg.startswith("_") and not msg.startswith("__init__"):
            continue
        passed.append(f)
    return passed


def _format_evidence(item) -> str:
    """Format a single evidence item as a compact one-liner."""
    family = getattr(item, "family", "?")
    family_tag = f"GT_{str(family).upper()}"

    # CallerExpectation: "3 callers destructure return as (x, y)"
    if hasattr(item, "usage_type"):
        detail = getattr(item, "detail", "")
        return f"GT: {detail} [{family_tag}]"

    # TestExpectation: "test_serialize:42 asserts format X"
    if hasattr(item, "assertion_type"):
        test_func = getattr(item, "test_func", "test")
        line = getattr(item, "line", "?")
        assertion = getattr(item, "assertion_type", "")
        expected = getattr(item, "expected", "")[:60]
        return f"GT: {test_func}:{line} {assertion} {expected} [{family_tag}]"

    # PatternEvidence, ChangeEvidence, StructuralEvidence: have "message"
    msg = getattr(item, "message", str(item))
    if len(msg) > 140:
        msg = msg[:137] + "..."
    return f"GT: {msg} [{family_tag}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook v4")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument(
        "--file",
        default="",
        help="Repo-relative path touched in this edit (fallback when git diff is empty)",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument("--diff", default="", help="Path to unified diff text")
    parser.add_argument("--old-content", default="", help="Path to previous file content")
    parser.add_argument("--mode", default="post_edit", choices=["post_edit", "post_failure", "late_repair"])
    parser.add_argument("--iteration-ratio", type=float, default=0.0)
    parser.add_argument("--structured-output", action="store_true")
    args = parser.parse_args()

    start = time.time()
    _append_gt_log("fire", f"root={args.root} file={args.file or '-'} db={args.db}")

    # Skip view operations immediately — no diff was produced
    if _is_view_operation():
        status = _status_line("skipped", "view_operation")
        print(status)
        _append_gt_log("status", status)
        return

    # Detect the actual workspace root (handles /testbed vs /workspace/django/ etc.)
    root = _detect_workspace_root(args.root)

    log_entry = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": root,
        "root_provided": args.root,
        "evidence": {},
    }

    modified_files = _get_modified_files(root)
    modified_files, diff_text = _merge_modified_with_explicit(root, modified_files, args.file)
    provided_diff_text = _read_text_file(args.diff)
    if provided_diff_text.strip():
        diff_text = provided_diff_text
        if args.file:
            explicit = args.file.strip().replace("\\", "/").lstrip("/")
            if explicit and explicit not in modified_files:
                modified_files = [explicit] + modified_files

    explicit_file = args.file.strip().replace("\\", "/").lstrip("/")
    old_content_source = "none"
    old_content_text = ""
    if args.old_content:
        old_content_text = _read_text_file(args.old_content)
        if old_content_text:
            old_content_source = "provided_old_content"
    if not old_content_text and explicit_file and diff_text:
        old_content_text = _reconstruct_old_content_from_diff(diff_text, explicit_file)
        if old_content_text:
            old_content_source = "reconstructed_from_diff"
    if not old_content_text and explicit_file:
        old_content_text = _git_show_head_file(root, explicit_file)
        if old_content_text:
            old_content_source = "git_show_head"
    log_entry["old_content_source"] = old_content_source
    if old_content_text:
        log_entry["old_content_bytes"] = len(old_content_text.encode("utf-8", errors="replace"))

    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        status = _status_line("no_evidence", "no_modified_files")
        print(status)
        _append_gt_log("status", status)
        return

    log_entry["files_changed"] = modified_files

    # Open GraphStore for language-agnostic evidence (v16+)
    graph_store = None
    try:
        from groundtruth.index.graph_store import GraphStore, is_graph_db

        if os.path.exists(args.db) and is_graph_db(args.db):
            graph_store = GraphStore(args.db)
            graph_store.initialize()
    except Exception:
        graph_store = None

    # Parse diff for changed line ranges per file
    diff_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif (
            line.startswith("@@")
            and current_file
            and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS
        ):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                s = int(match.group(1))
                c = int(match.group(2)) if match.group(2) else 1
                diff_ranges.setdefault(current_file, []).append((s, s + c - 1))

    # Find changed function names per file
    changed_funcs: dict[str, list[str]] = {}
    for fpath, ranges in diff_ranges.items():
        source = _read_file(root, fpath)
        if source:
            changed_funcs[fpath] = _find_funcs_at_lines(
                source, ranges, file_path=fpath, store=graph_store
            )

    # === IMPROVED L3: graph.db-driven priority-ordered evidence ===
    # Decision 22 Fix 5: L3 decoupled from L1 — gate on graph connectivity,
    # not on whether the brief produced candidates. Files with high-confidence
    # edges (≥0.5) in the graph get improved evidence regardless of L1 state.
    improved_output = ""
    if os.path.exists(args.db):
        try:
            all_func_names: list[str] = []
            primary_file = explicit_file or (modified_files[0] if modified_files else "")
            if primary_file and primary_file in changed_funcs:
                all_func_names = changed_funcs[primary_file]
            elif changed_funcs:
                for _fp, _fns in changed_funcs.items():
                    if _fns:
                        all_func_names = _fns
                        primary_file = _fp
                        break

            _accum: list[dict] | None = [] if args.structured_output else None
            if all_func_names and primary_file:
                improved_output = generate_improved_evidence(
                    file_path=primary_file,
                    function_names=all_func_names,
                    db_path=args.db,
                    repo_root=root,
                    mode=args.mode,
                    iteration_ratio=args.iteration_ratio,
                    _evidence_accumulator=_accum,
                )
        except Exception as e:
            _append_gt_log("improved_evidence_error", str(e))
            improved_output = ""

    if improved_output:
        # Improved evidence succeeded -- emit it and skip legacy families
        log_entry["evidence_source"] = "improved_l3"
        log_entry["output"] = improved_output
        log_entry["output_lines"] = len(improved_output.splitlines())
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        print(improved_output)
        if args.structured_output and _accum:
            print("__GT_STRUCTURED__")
            print(json.dumps(_accum))
        status = _status_line("success", "improved_l3")
        print(status)
        _append_gt_log("status", status)
        return

    # === LEGACY FALLBACK: 5 evidence families ===
    all_findings = []

    # === EVIDENCE FAMILY 1: CHANGE (before/after AST diff) ===
    change_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.change import ChangeAnalyzer

        analyzer = ChangeAnalyzer(store=graph_store)
        change_items = analyzer.analyze(root, diff_text)
        change_signal["ran"] = True
        change_signal["items_found"] = len(change_items)
        all_findings.extend(change_items)
    except Exception as e:
        import traceback

        change_signal["error"] = str(e)
        change_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["change"] = change_signal

    # === EVIDENCE FAMILY 2: CONTRACT (caller usage + test assertions) ===
    contract_signal = {
        "ran": False,
        "callers_analyzed": 0,
        "tests_analyzed": 0,
        "items_found": 0,
        "after_abstention": 0,
    }
    try:
        from groundtruth.evidence.contract import CallerUsageMiner, TestAssertionMiner

        caller_miner = CallerUsageMiner(root, store=graph_store)
        test_miner = TestAssertionMiner(root, store=graph_store)

        # Try to get caller info from index
        caller_files: list[str] = []
        test_files: list[str] = []
        try:
            from groundtruth.index.store import SymbolStore

            store = SymbolStore(args.db)
            store.initialize()
            for fpath in modified_files:
                result = store.get_importers_of_file(fpath)
                importers = getattr(result, "value", []) or []
                if importers:
                    for imp in importers:
                        if "test" in imp.lower():
                            test_files.append(imp)
                        else:
                            caller_files.append(imp)
        except Exception:
            pass

        contract_signal["callers_analyzed"] = len(caller_files)
        contract_signal["tests_analyzed"] = len(test_files)

        # Mine caller expectations for each changed function
        for fpath, funcs in changed_funcs.items():
            caller_node_ids = []
            if graph_store:
                try:
                    symbols_result = graph_store.get_symbols_in_file(fpath)
                    if hasattr(symbols_result, "value") and symbols_result.value:
                        caller_node_ids = [s.id for s in symbols_result.value if s.name in funcs]
                except Exception:
                    caller_node_ids = []
            for func_name in funcs:
                caller_items = caller_miner.mine(
                    func_name,
                    caller_files,
                    caller_node_ids=caller_node_ids,
                )
                all_findings.extend(caller_items)

        # Mine test assertions (pass function names for targeted graph.db queries)
        for fpath in modified_files:
            funcs = changed_funcs.get(fpath, [])
            for func_name in funcs or [None]:
                test_items = test_miner.mine(fpath, test_files, symbol_name=func_name)
                all_findings.extend(test_items)

        contract_signal["ran"] = True
        contract_items_count = sum(
            1 for f in all_findings if getattr(f, "family", "") == "contract"
        )
        contract_signal["items_found"] = contract_items_count
        if contract_items_count == 0:
            pattern_fallback_count = 0
            for fpath in modified_files:
                source = _read_file(root, fpath)
                fallback_items = _sibling_pattern_fallback(source, diff_text, fpath)
                pattern_fallback_count += len(fallback_items)
                all_findings.extend(fallback_items)
            if pattern_fallback_count:
                contract_signal["pattern_fallback_items"] = pattern_fallback_count
    except Exception as e:
        import traceback

        contract_signal["error"] = str(e)
        contract_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["contract"] = contract_signal

    # === EVIDENCE FAMILY 3: PATTERN (sibling analysis) ===
    pattern_signal = {"ran": False, "siblings_found": 0, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.pattern import SiblingAnalyzer

        sibling_analyzer = SiblingAnalyzer(store=graph_store)

        for fpath, funcs in changed_funcs.items():
            source = _read_file(root, fpath)
            if not source:
                continue
            for func_name in funcs:
                pattern_items = sibling_analyzer.analyze(source, func_name, file_path=fpath)
                all_findings.extend(pattern_items)

        pattern_signal["ran"] = True
        pattern_signal["items_found"] = sum(
            1 for f in all_findings if getattr(f, "family", "") == "pattern"
        )
    except Exception as e:
        pattern_signal["error"] = str(e)
    log_entry["evidence"]["pattern"] = pattern_signal

    # === EVIDENCE FAMILY 4: STRUCTURAL (obligations + contradictions + conventions) ===
    structural_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.structural import (
            run_obligations,
            run_contradictions,
            run_conventions,
        )

        store = None
        graph = None
        try:
            from groundtruth.index.store import SymbolStore
            from groundtruth.index.graph import ImportGraph

            store = SymbolStore(args.db)
            store.initialize()
            graph = ImportGraph(store)
        except Exception:
            pass

        struct_items = []
        if store and graph and diff_text:
            struct_items.extend(run_obligations(store, graph, diff_text))
        if store:
            struct_items.extend(run_contradictions(store, root, modified_files))
        struct_items.extend(run_conventions(root, modified_files))

        structural_signal["ran"] = True
        structural_signal["items_found"] = len(struct_items)
        all_findings.extend(struct_items)
    except Exception as e:
        structural_signal["error"] = str(e)
    log_entry["evidence"]["structural"] = structural_signal

    # === EVIDENCE FAMILY 5: SEMANTIC (call-site voting + arg affinity + guard consistency) ===
    semantic_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.semantic.call_site_voting import CallSiteVoter
        from groundtruth.evidence.semantic.argument_affinity import ArgumentAffinityChecker
        from groundtruth.evidence.semantic.guard_consistency import GuardConsistencyChecker

        voter = CallSiteVoter()
        affinity = ArgumentAffinityChecker()
        guard = GuardConsistencyChecker()

        semantic_items = []
        remaining_time = max(2.0, 8.0 - (time.time() - start))

        if diff_text:
            semantic_items.extend(voter.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(affinity.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(guard.analyze(root, diff_text, time_budget=remaining_time / 3))

        semantic_signal["ran"] = True
        semantic_signal["items_found"] = len(semantic_items)
        all_findings.extend(semantic_items)
    except Exception as e:
        semantic_signal["error"] = str(e)
    log_entry["evidence"]["semantic"] = semantic_signal

    # === ABSTENTION ===
    passed = _apply_abstention(all_findings)

    # Update after_abstention counts per family
    for family_name in ("change", "contract", "pattern", "structural", "semantic"):
        count = sum(1 for f in passed if getattr(f, "family", "") == family_name)
        log_entry["evidence"].get(family_name, {})["after_abstention"] = count

    log_entry["abstention_summary"] = {
        "total_raw": len(all_findings),
        "total_emitted": len(passed),
        "total_suppressed": len(all_findings) - len(passed),
    }

    # === FORMAT OUTPUT ===
    output_lines = []
    if passed:
        # Sort by confidence descending, take top N
        passed.sort(key=lambda f: -getattr(f, "confidence", 0))
        for item in passed[: args.max_items]:
            output_lines.append(_format_evidence(item))

    output = "\n".join(output_lines)
    log_entry["output"] = output
    log_entry["output_lines"] = len(output_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)

    if output:
        print(output)
        status = _status_line("success", f"{len(output_lines)}_items")
        print(status)
        _append_gt_log("status", status)
    else:
        status = _status_line("no_evidence", "abstention_filtered")
        print(status)
        _append_gt_log("status", status)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        status = _status_line("error", f"{type(exc).__name__}:{exc}")
        print(status)
        _append_gt_log("status", status)
