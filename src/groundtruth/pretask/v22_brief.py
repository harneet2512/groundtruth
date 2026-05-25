"""v8.2.2 brief generator (host-side).

Runs v8.2.2 RRF ranker against a pre-built graph.db and renders a V1R-map
files block plus an appended ``<gt-focus-functions>`` block. Designed to be
called from the host (eval VM) where heavyweight deps (sentence-transformers,
v7.4) are available — *not* from inside SWE-Bench task containers.

The output is the inject-once first-turn brief; the caller writes the text
into the container's first-turn-text file (``/tmp/gt_first_turn_<id>.txt``)
and the OH harness consumes it via ``GT_FIRST_TURN_TEXT_PATH``.

Format (no prose, no constraints — map-only):

    <gt-task-brief>
    ## Focus files (top-5)
    1. path/to/foo.py
    2. path/to/bar.py
    ...

    <gt-focus-functions>
    1. path/to/foo.py:42 — handle_request
    2. path/to/foo.py:108 — _validate
    ...
    </gt-focus-functions>
    </gt-task-brief>
"""
from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from groundtruth.pretask.v2_types import RankedFile, RankedFunction

_TOP_FILES = 5
_TOP_FUNCS = 10

_V22_ENV_DEFAULTS = {
    "GT_V22_TIER1": "1",
    "GT_V22_MULTIHOP": "2",
    "GT_V22_GLOBAL_BM25": "1",
}


def _file_tier(rank: int) -> str:
    if rank < 3:
        return "[VERIFIED]"
    if rank < 5:
        return "[WARNING]"
    return "[INFO]"


def _func_tier(rank: int) -> str:
    if rank < 3:
        return "[VERIFIED]"
    if rank < 7:
        return "[WARNING]"
    return "[INFO]"


def _lookup_start_lines(
    graph_db_path: str, items: list[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Bulk-lookup start_line for (file_path, name) pairs from graph.db.

    Returns {(file_path, name): start_line_or_0}. A name may exist multiple
    times in a file (overloads); returns the smallest start_line.
    """
    if not items:
        return {}
    out: dict[tuple[str, str], int] = {}
    try:
        # RC-04: switch to URI mode + busy_timeout to avoid lock contention
        # with the gt-index writer; this lookup is read-only.
        conn = sqlite3.connect(
            f"file:{graph_db_path}?mode=ro", uri=True, timeout=10
        )
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error:
        return {}
    try:
        for fp, name in items:
            row = conn.execute(
                "SELECT MIN(start_line) FROM nodes "
                "WHERE file_path = ? AND name = ? "
                "AND label IN ('Function','Method')",
                (fp, name),
            ).fetchone()
            out[(fp, name)] = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _format_brief(
    files: "list[RankedFile]",
    funcs_with_lines: list[tuple["RankedFunction", int]],
) -> str:
    parts: list[str] = ["<gt-task-brief>"]
    parts.append("## Focus files (top-5)")
    if not files:
        parts.append("(no files ranked — graph.db empty or query produced no signal)")
    for i, f in enumerate(files[:_TOP_FILES]):
        parts.append(f"{i + 1}. {f.file}")
    parts.append("")
    parts.append("<gt-focus-functions>")
    if not funcs_with_lines:
        parts.append("(no functions ranked)")
    for i, (fn, line) in enumerate(funcs_with_lines[:_TOP_FUNCS]):
        line_token = str(line) if line > 0 else "?"
        parts.append(f"{i + 1}. {fn.file}:{line_token} — {fn.function}")
    parts.append("</gt-focus-functions>")
    parts.append("</gt-task-brief>")
    return "\n".join(parts)


def generate_brief(
    issue_text: str,
    repo_path: str,
    graph_db_path: str,
) -> str:
    """Render a v8.2.2 RRF brief for the given issue.

    Sets the ``GT_V22_*`` environment defaults required by the ranker before
    invoking it; if a caller has already set them, those values win. Returns
    a non-empty brief string on success, or an empty string on hard failure
    (caller decides whether to fall back).
    """
    if not issue_text or not issue_text.strip():
        return ""
    if not os.path.exists(graph_db_path):
        return ""

    for k, v in _V22_ENV_DEFAULTS.items():
        os.environ.setdefault(k, v)

    try:
        from groundtruth.pretask.query_preprocessor import preprocess
        from groundtruth.pretask.v2_ranker import rank_files, rank_functions
    except ImportError:
        return ""

    try:
        query = preprocess(issue_text)
    except Exception:
        return ""

    try:
        ranked_files = rank_files(query, repo_path, graph_db_path)
    except Exception:
        ranked_files = []
    if not ranked_files:
        return ""

    try:
        ranked_funcs = rank_functions(query, ranked_files, repo_path, graph_db_path)
    except Exception:
        ranked_funcs = []

    top_funcs = ranked_funcs[:_TOP_FUNCS]
    line_lookup = _lookup_start_lines(
        graph_db_path, [(fn.file, fn.function) for fn in top_funcs]
    )
    funcs_with_lines = [(fn, line_lookup.get((fn.file, fn.function), 0)) for fn in top_funcs]

    rendered = _format_brief(ranked_files, funcs_with_lines)

    # v1.0.5 telemetry — layer1 localization + layer2 brief.
    try:
        from groundtruth.runtime.v105_telemetry import log_localization, log_brief

        log_localization(
            files=[
                {"file": f.file, "score": float(f.score), "rank": i + 1, "tier": _file_tier(i)}
                for i, f in enumerate(ranked_files[:_TOP_FILES])
            ],
            functions=[
                {
                    "file": fn.file,
                    "function": fn.function,
                    "line": line,
                    "score": float(fn.score),
                    "rank": i + 1,
                    "tier": _func_tier(i),
                    "components": getattr(fn, "components", {}),
                }
                for i, (fn, line) in enumerate(funcs_with_lines)
            ],
        )
        log_brief(
            text=rendered,
            sections=["focus_files", "gt-focus-functions"],
            tier_counts={
                "files_verified": sum(1 for i, _ in enumerate(ranked_files[:_TOP_FILES]) if i < 3),
                "files_warning": sum(1 for i, _ in enumerate(ranked_files[:_TOP_FILES]) if 3 <= i < 5),
                "funcs_verified": sum(1 for i in range(len(funcs_with_lines)) if i < 3),
                "funcs_warning": sum(1 for i in range(len(funcs_with_lines)) if 3 <= i < 7),
                "funcs_info": sum(1 for i in range(len(funcs_with_lines)) if i >= 7),
            },
        )
    except Exception:
        # Telemetry is best-effort; never block the brief on a logging failure.
        pass

    return rendered
