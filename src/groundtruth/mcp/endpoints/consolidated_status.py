"""groundtruth_status (consolidated) — Index health and session summary.

Absorbs: status, checkpoint, brief.
Agent asks: "What's the state?"
Token budget: 200. Always returns something (never silent).
"""

from __future__ import annotations

from groundtruth.index.graph_store import GraphStore
from groundtruth.schema.finding import enforce_budget
from groundtruth.utils.result import Err


TOKEN_BUDGET = 200


async def handle_status(
    store: GraphStore,
    session_calls: int = 0,
    session_findings: int = 0,
    session_fix_required: int = 0,
) -> str:
    stats_result = store.get_stats()
    if isinstance(stats_result, Err):
        return '<gt-evidence surface="status">\n[SKIP] No index found — run gt-index to create graph.db\n</gt-evidence>'

    stats = stats_result.value
    symbols = stats.get("symbols_count", 0)
    files = stats.get("files_count", 0)
    refs = stats.get("refs_count", 0)

    if symbols == 0:
        return '<gt-evidence surface="status">\n[SKIP] Index is empty — run gt-index first\n</gt-evidence>'

    try:
        langs_cursor = store.connection.execute(
            "SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL LIMIT 6"
        )
        langs = [row[0] for row in langs_cursor.fetchall()]
    except Exception:
        langs = []

    hc_ratio = store.get_high_confidence_edge_ratio()
    hc_pct = int(hc_ratio * 100)

    lang_str = ", ".join(langs[:5])
    if len(langs) > 5:
        lang_str += f" +{len(langs) - 5}"

    lines = ['<gt-evidence surface="status">']
    lines.append(
        f"INDEX: {symbols:,} symbols, {files:,} files, {refs:,} edges ({hc_pct}% high-confidence) | {lang_str}"
    )
    if session_calls > 0 or session_findings > 0:
        lines.append(
            f"SESSION: {session_calls} calls, {session_findings} findings emitted"
            + (f", {session_fix_required} FIX_REQUIRED pending" if session_fix_required > 0 else "")
        )
    lines.append("</gt-evidence>")

    text = "\n".join(lines)
    return enforce_budget(text, TOKEN_BUDGET)
