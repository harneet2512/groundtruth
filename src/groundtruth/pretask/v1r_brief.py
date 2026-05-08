"""V1R brief — map-only, inject-once, stay-silent.

Generates a minimal pre-task brief: ranked files + functions + test mappings.
No prose, no constraints, no behavioral nudges.

Uses v7.4 hybrid retrieval (sem + lex + reach + anchor_prox - hub_pen) to
rank candidates, then queries graph.db for top functions and test coverage.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from groundtruth.pretask.v7_4_brief import V74BriefResult, run_v74


MAX_FILES = 5
MAX_FUNCTIONS_PER_FILE = 3
MAX_BRIEF_TOKENS = 400


@dataclass(frozen=True)
class FileEntry:
    path: str
    score: float
    functions: list[str] = field(default_factory=list)
    test_mappings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class V1RBriefResult:
    files: list[FileEntry]
    brief_text: str
    token_estimate: int
    v74_result: V74BriefResult | None = None


def _top_functions(graph_db: str, file_path: str, limit: int = MAX_FUNCTIONS_PER_FILE) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        rows = conn.execute(
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
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


def _test_files_for(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        rows = conn.execute(
            """
            SELECT DISTINCT n2.file_path
            FROM nodes n1
            JOIN edges e ON e.target_id = n1.id
            JOIN nodes n2 ON e.source_id = n2.id
            WHERE n1.file_path = ?
              AND n2.is_test = 1
              AND n2.file_path != n1.file_path
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def render_brief(files: list[FileEntry]) -> str:
    lines = ["<gt-task-brief>"]
    for i, f in enumerate(files, 1):
        funcs = ", ".join(f.functions) if f.functions else ""
        line = f"{i}. {f.path}"
        if funcs:
            line += f" — {funcs}"
        lines.append(line)
        for t in f.test_mappings:
            lines.append(f"   Tests: {t}")
    lines.append("</gt-task-brief>")
    return "\n".join(lines)


def generate_v1r_brief(
    issue_text: str,
    repo_root: str,
    graph_db: str,
    *,
    bug_id: str = "unknown",
    repo: str = "unknown",
    gold_files: list[str] | None = None,
    max_files: int = MAX_FILES,
    max_brief_tokens: int = MAX_BRIEF_TOKENS,
    weights: dict[str, float] | None = None,
) -> V1RBriefResult:
    v74 = run_v74(
        issue_text,
        repo_root,
        graph_db,
        bug_id=bug_id,
        repo=repo,
        gold_files=gold_files,
        ablation="C",
        k_anchor=3,
        k_sem_top=10,
        tau_anchor=0.20,
        max_depth=3,
        min_confidence=0.5,
        weights=weights,
        focus_size=max_files,
    )

    if not v74.ranked_full:
        return V1RBriefResult(
            files=[],
            brief_text="<gt-task-brief>\n</gt-task-brief>",
            token_estimate=4,
            v74_result=v74,
        )

    top_records = v74.ranked_full[:max_files]

    entries: list[FileEntry] = []
    for rec in top_records:
        path = str(rec.get("path", ""))
        score = float(rec.get("score", 0.0))
        funcs = _top_functions(graph_db, path)
        tests = _test_files_for(graph_db, path)
        entries.append(FileEntry(
            path=path,
            score=score,
            functions=funcs,
            test_mappings=tests,
        ))

    brief_text = render_brief(entries)
    tok = _estimate_tokens(brief_text)

    while tok > max_brief_tokens and len(entries) > 1:
        entries = entries[:-1]
        brief_text = render_brief(entries)
        tok = _estimate_tokens(brief_text)

    return V1RBriefResult(
        files=entries,
        brief_text=brief_text,
        token_estimate=tok,
        v74_result=v74,
    )
