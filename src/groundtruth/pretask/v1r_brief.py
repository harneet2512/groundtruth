"""V1R brief — map-only, inject-once, stay-silent.

Generates a minimal pre-task brief: ranked files + functions + test mappings.
No prose, no constraints, no behavioral nudges.

Uses v7.4 hybrid retrieval (sem + lex + reach + anchor_prox - hub_pen) to
rank candidates, then queries graph.db for top functions and test coverage.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from groundtruth.pretask.v7_4_brief import V74BriefResult, run_v74


MAX_FILES = 5
MAX_FUNCTIONS_PER_FILE = 3
MAX_BRIEF_TOKENS = 600
EDGE_CONFIDENCE_FLOOR = 0.7

_schema_cache: dict[str, bool] = {}


def _has_confidence(graph_db: str) -> bool:
    if graph_db in _schema_cache:
        return _schema_cache[graph_db]
    try:
        conn = sqlite3.connect(graph_db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        conn.close()
        result = "confidence" in cols
    except Exception:
        result = False
    _schema_cache[graph_db] = result
    return result


@dataclass(frozen=True)
class FileEntry:
    path: str
    score: float
    functions: list[str] = field(default_factory=list)
    test_mappings: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    co_changes: list[str] = field(default_factory=list)
    contract: str = ""
    pattern: str = ""
    spec: str = ""


@dataclass(frozen=True)
class V1RBriefResult:
    files: list[FileEntry]
    brief_text: str
    token_estimate: int
    v74_result: V74BriefResult | None = None


def _top_functions(graph_db: str, file_path: str, limit: int = MAX_FUNCTIONS_PER_FILE) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""
        rows = conn.execute(
            f"""
            SELECT n.name, n.signature, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id {conf_clause}
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
        return [row[1] if row[1] else row[0] for row in rows]
    except Exception:
        return []


def _top_function_names(
    graph_db: str, file_path: str, limit: int = MAX_FUNCTIONS_PER_FILE,
    issue_terms: set[str] | None = None,
) -> list[str]:
    """Return raw function NAMES (not signatures) for contract lookup.

    Prioritizes functions whose names appear in issue_terms (bug-relevant),
    then falls back to most-referenced functions.
    """
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""
        rows = conn.execute(
            f"""
            SELECT n.name, COUNT(e.id) AS ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id {conf_clause}
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
            GROUP BY n.id
            ORDER BY ref_count DESC, n.name
            LIMIT 20
            """,
            (file_path,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    if not rows:
        return []

    if issue_terms:
        terms_lower = {t.lower() for t in issue_terms}
        issue_matched = [r[0] for r in rows if r[0].lower() in terms_lower]
        others = [r[0] for r in rows if r[0].lower() not in terms_lower]
        return (issue_matched + others)[:limit]

    return [row[0] for row in rows[:limit]]


def _test_files_for(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""
        rows = conn.execute(
            f"""
            SELECT DISTINCT n2.file_path
            FROM nodes n1
            JOIN edges e ON e.target_id = n1.id {conf_clause}
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


def _issue_relevant_neighbors(
    graph_db: str,
    file_path: str,
    repo_root: str,
    issue_terms: set[str],
    limit: int = 3,
) -> list[str]:
    """Graph neighbors scored by issue relevance, not edge count.

    Queries both callees and callers, then ranks them by how many issue
    keywords appear in their file content.  The agent sees the connections
    most relevant to the current issue — dynamic, not static.
    """
    if not issue_terms:
        return _static_callees(graph_db, file_path, limit)
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ? AND nt.file_path != ? AND nt.is_test = 0
            UNION
            SELECT DISTINCT nsrc.file_path FROM nodes nt
            JOIN edges e ON e.target_id = nt.id {conf_clause}
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
            """,
            (file_path, file_path, file_path, file_path),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    scored: list[tuple[str, int]] = []
    for (neighbor,) in rows:
        fpath = os.path.join(repo_root, neighbor)
        try:
            text = open(fpath, encoding="utf-8", errors="ignore").read(200_000).lower()
            hits = sum(1 for t in issue_terms if t in text)
            scored.append((neighbor, hits))
        except OSError:
            scored.append((neighbor, 0))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [f for f, s in scored[:limit] if s > 0] or [f for f, _ in scored[:limit]]


def _static_callees(graph_db: str, file_path: str, limit: int = 3) -> list[str]:
    try:
        conn = sqlite3.connect(graph_db)
        conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_confidence(graph_db) else ""
        rows = conn.execute(
            f"""
            SELECT DISTINCT nt.file_path
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {conf_clause}
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ?
              AND nt.file_path != ?
              AND nt.is_test = 0
            LIMIT ?
            """,
            (file_path, file_path, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


import re as _re

CALLER_CONFIDENCE_FLOOR = 0.9
MAX_CALLERS_PER_FUNC = 2


def _caller_contract_for_file(
    graph_db: str,
    file_path: str,
    repo_root: str,
    func_names: list[str],
) -> str:
    """Show literal caller code lines for the top functions in a file.

    Queries cross-file callers at confidence >= 0.9, reads the actual source
    line where the call happens. Returns formatted caller evidence or empty.
    """
    if not func_names:
        return ""

    try:
        conn = sqlite3.connect(graph_db)
        has_conf = _has_confidence(graph_db)
    except Exception:
        return ""

    caller_lines: list[str] = []
    try:
        for fname in func_names[:2]:
            conf_clause = f"AND e.confidence >= {CALLER_CONFIDENCE_FLOOR}" if has_conf else ""
            _norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
            rows = conn.execute(
                f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' {conf_clause}
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND nt.file_path LIKE ?
                  AND nsrc.file_path != nt.file_path
                  AND nsrc.is_test = 0
                  AND e.source_line > 0
                ORDER BY e.confidence DESC, e.source_line
                LIMIT ?
                """,
                (fname, f"%{_norm_fp}", MAX_CALLERS_PER_FUNC),
            ).fetchall()

            for caller_file, source_line, _ in rows:
                full_path = os.path.join(repo_root, caller_file)
                try:
                    with open(full_path, encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    if source_line <= 0 or source_line > len(lines):
                        continue
                    code = lines[source_line - 1].strip()
                    if len(code) > 80:
                        code = code[:77] + "..."
                    caller_lines.append(f"{caller_file}:{source_line} `{code}`")
                except OSError:
                    continue

            if len(caller_lines) >= 3:
                break
    finally:
        conn.close()

    if not caller_lines:
        return ""
    return " | ".join(caller_lines[:3])


def _sibling_context(graph_db: str, file_path: str, func_names: list[str]) -> str:
    """Find sibling functions in the same class/module — parallel implementations.

    General mechanism: if the candidate has function X, show what OTHER functions
    exist at the same scope level. These are the patterns to follow.
    """
    if not func_names:
        return ""
    try:
        conn = sqlite3.connect(graph_db)
        rows = conn.execute(
            """
            SELECT DISTINCT n.name
            FROM nodes n
            WHERE n.file_path = ?
              AND n.label IN ('Function', 'Method')
              AND n.is_test = 0
              AND n.name NOT IN ({})
            ORDER BY n.start_line
            LIMIT 8
            """.format(",".join("?" * len(func_names))),
            (file_path, *func_names),
        ).fetchall()
        conn.close()
        names = [r[0] for r in rows if len(r[0]) > 2 and not r[0].startswith("_")]
        return ", ".join(names[:5]) if names else ""
    except Exception:
        return ""


def _function_spec(
    graph_db: str, file_path: str, func_name: str, repo_root: str,
) -> str:
    """Pre-edit specification: shows parallel patterns within a function.

    This surfaces the COMPLETE set of cases the function handles BEFORE the
    agent edits it. Prevents incomplete fixes (handling case A but missing B).
    Fires regardless of graph connectivity — purely syntactic.
    """
    try:
        conn = sqlite3.connect(graph_db)
        row = conn.execute(
            "SELECT start_line, end_line FROM nodes WHERE file_path = ? AND name = ? "
            "AND label IN ('Function','Method') LIMIT 1",
            (file_path, func_name),
        ).fetchone()
        conn.close()
        if not row or not row[0] or not row[1]:
            return ""
    except Exception:
        return ""

    full_path = os.path.join(repo_root, file_path)
    try:
        with open(full_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except OSError:
        return ""

    start = max(0, row[0] - 1)
    end = min(len(all_lines), row[1])
    func_lines = all_lines[start:end]

    from groundtruth.hooks.post_edit import _make_template
    templates: dict[str, list[str]] = {}
    for line in func_lines:
        stripped = line.strip()
        if len(stripped) < 15 or stripped.startswith("#") or stripped.startswith("//"):
            continue
        tmpl = _make_template(stripped)
        if tmpl not in templates:
            templates[tmpl] = []
        templates[tmpl].append(stripped)

    groups = [(t, lines) for t, lines in templates.items() if len(lines) >= 2 and len(lines) <= 8]
    if not groups:
        return ""

    groups.sort(key=lambda x: -len(x[1]))
    best = groups[0]
    cases = [ln if len(ln) <= 50 else ln[:47] + "..." for ln in best[1][:4]]
    return f"handles: {' | '.join(cases)}"


def _last_change(file_path: str, repo_root: str) -> str:
    """Get the last git commit message for this file — shows how the file evolves."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--", file_path],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            msg = result.stdout.strip()
            if len(msg) > 70:
                msg = msg[:67] + "..."
            return msg
    except Exception:
        pass
    return ""


def _co_change_files(file_path: str, repo_root: str, limit: int = 3) -> list[str]:
    """Find files that historically co-change with this file (git-based).

    Research: HAFixAgent (arXiv 2025) +56.6% from git history in repair loop.
    ESEM 2024: co-change + structural deps significantly improves impact prediction.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-20", "--", file_path],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    co_counts: dict[str, int] = {}
    current_commit_files: list[str] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            for f in current_commit_files:
                if f != file_path and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml")):
                    co_counts[f] = co_counts.get(f, 0) + 1
            current_commit_files = []
        else:
            current_commit_files.append(line)

    if current_commit_files:
        for f in current_commit_files:
            if f != file_path and not f.endswith((".md", ".rst", ".txt", ".yml", ".yaml")):
                co_counts[f] = co_counts.get(f, 0) + 1

    ranked = sorted(co_counts.items(), key=lambda x: -x[1])
    return [f for f, count in ranked[:limit] if count >= 2]


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


# --- Decision 26: Cross-Domain Bridging via Co-Change + Test Co-Import ---


def _detect_overconfident_convergence(top_records: list[dict], graph_db: str) -> bool:
    """Detect when all top candidates cluster in same module — symptom-not-cause risk."""
    if len(top_records) < 3:
        return False

    # Check directory concentration
    dirs = [os.path.dirname(r.get("path", "")) for r in top_records[:5]]
    unique_dirs = set(dirs)
    if len(unique_dirs) > 2:
        return False  # Spread across modules — not convergent

    # Check if BM25 dominates (lex component > 50% of total score for all top-5)
    bm25_dominant = all(
        r.get("components", {}).get("lex", 0) > 0.5 * r.get("score", 1)
        for r in top_records[:5]
        if r.get("score", 0) > 0
    )

    return bm25_dominant and len(unique_dirs) <= 2


def _expand_via_cochange(symptom_files: list[str], repo_root: str, max_expansion: int = 3) -> list[dict]:
    """Find files in other modules that co-changed with symptom files in git history."""
    symptom_dirs = {os.path.dirname(f) for f in symptom_files}
    cochange_counts: dict[str, int] = {}

    # Get last 100 commits
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--name-only", "-100"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    # Parse commits — each commit block starts with a hash line, followed by file paths
    current_files: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            # End of commit block — check for co-changes
            if current_files:
                symptom_in_commit = any(f in current_files for f in symptom_files)
                if symptom_in_commit:
                    for f in current_files:
                        if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                            cochange_counts[f] = cochange_counts.get(f, 0) + 1
            current_files = []
        elif _re.match(r'^[0-9a-f]{7,12}\s', line):
            # This is a commit hash line (e.g., "abc1234 Fix bug")
            # Process previous block
            if current_files:
                symptom_in_commit = any(f in current_files for f in symptom_files)
                if symptom_in_commit:
                    for f in current_files:
                        if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                            cochange_counts[f] = cochange_counts.get(f, 0) + 1
            current_files = []
        else:
            # This is a file path
            current_files.append(line)

    # Process final block
    if current_files:
        symptom_in_commit = any(f in current_files for f in symptom_files)
        if symptom_in_commit:
            for f in current_files:
                if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:
                    cochange_counts[f] = cochange_counts.get(f, 0) + 1

    # Rank by co-change frequency, require >= 2
    ranked = sorted(cochange_counts.items(), key=lambda x: -x[1])
    return [
        {"path": f, "score": 0.0, "components": {"cochange": count}, "entered_via": "cochange"}
        for f, count in ranked[:max_expansion]
        if count >= 2
    ]


def _expand_via_test_coimport(symptom_files: list[str], graph_db: str, max_expansion: int = 3) -> list[dict]:
    """Find cross-domain bridges via shared test importers."""
    symptom_dirs = {os.path.dirname(f) for f in symptom_files}

    try:
        conn = sqlite3.connect(graph_db)

        # Find test files that import any symptom file
        placeholders = ",".join("?" * len(symptom_files))
        test_importers = conn.execute(
            f"""
            SELECT DISTINCT nsrc.file_path
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type IN ('CALLS', 'IMPORTS')
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nt.file_path IN ({placeholders})
              AND nsrc.is_test = 1
            """,
            symptom_files,
        ).fetchall()

        test_files = [r[0] for r in test_importers]
        if not test_files:
            conn.close()
            return []

        # Find OTHER non-test files imported by those same test files
        test_placeholders = ",".join("?" * len(test_files))
        bridges = conn.execute(
            f"""
            SELECT nt.file_path, COUNT(*) as cnt
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type IN ('CALLS', 'IMPORTS')
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path IN ({test_placeholders})
              AND nt.is_test = 0
              AND nt.file_path NOT IN ({placeholders})
            GROUP BY nt.file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            test_files + symptom_files + [max_expansion * 3],
        ).fetchall()

        conn.close()

        # Filter to other modules only
        result: list[dict] = []
        for path, count in bridges:
            if os.path.dirname(path) not in symptom_dirs:
                result.append({
                    "path": path,
                    "score": 0.0,
                    "components": {"test_coimport": count},
                    "entered_via": "test_coimport",
                })
            if len(result) >= max_expansion:
                break
        return result
    except Exception:
        return []


def render_brief(
    files: list[FileEntry],
    *,
    scores: list[float] | None = None,
    scope_files: list[str] | None = None,
    scope_confidence: str = "low",
    issue_text: str = "",
) -> str:
    if not files:
        return "<gt-task-brief>\n</gt-task-brief>"

    # Confidence-gated framing: if top candidate clearly ahead, directive.
    # If scores are flat, exploratory. Based on score separation of #1 vs #2.
    high_confidence = False
    if scores and len(scores) >= 2 and scores[0] > 0:
        gap = (scores[0] - scores[1]) / scores[0]
        high_confidence = gap > 0.3  # top candidate 30%+ ahead of #2

    lines = ["<gt-task-brief>"]
    for i, f in enumerate(files, 1):
        funcs = ", ".join(f.functions) if f.functions else ""
        line = f"{i}. {f.path}"
        if funcs:
            line += f" ({funcs})"
        lines.append(line)
        if f.spec and issue_text:
            # Relevance gate: spec must overlap with issue terms to avoid red herrings
            _spec_lower = f.spec.lower()
            _issue_lower = issue_text.lower() if issue_text else ""
            _issue_terms = set(_issue_lower.split()) - {"the", "a", "an", "is", "to", "in", "of", "and", "or", "for", "this", "that", "with", "from", "by", "on", "at", "it", "be", "as", "not", "but", "if", "we", "i"}
            _spec_overlap = any(term in _spec_lower for term in _issue_terms if len(term) > 3)
            _func_overlap = any(fn.lower() in _spec_lower for fn in f.functions) if f.functions else False
            if _spec_overlap or _func_overlap:
                lines.append(f"   Spec: {f.spec}")
        elif f.spec and not issue_text:
            lines.append(f"   Spec: {f.spec}")
        if f.contract:
            lines.append(f"   Callers: {f.contract}")
        if f.pattern:
            lines.append(f"   Context: {f.pattern}")
        if f.co_changes:
            lines.append(f"   Also changes: {', '.join(f.co_changes)}")
        if f.callees:
            lines.append(f"   Calls: {', '.join(f.callees)}")
        if f.test_mappings:
            lines.append(f"   Tests: {', '.join(f.test_mappings)}")

    # Cross-file scope hint (Signal 1)
    if scope_files and scope_confidence in ("high", "medium"):
        scope_names = [os.path.basename(f) for f in scope_files[:3]]
        if scope_confidence == "high":
            lines.append(f"\nLikely multi-file scope: {', '.join(scope_names)}")
        else:
            lines.append(f"\nRelated files to inspect: {', '.join(scope_names)}")

    # Directive ending: confidence-gated
    top = files[0]
    if high_confidence:
        directive = f"\nEdit {top.path} first."
        if top.test_mappings:
            directive += f" Verify: pytest {top.test_mappings[0]}"
        lines.append(directive)
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
    # Density check: if graph is too sparse, graph signals are noise — use BM25 only
    _sparse_graph = False
    if weights is None and graph_db:
        try:
            _conn = sqlite3.connect(graph_db)
            _total_edges = _conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            _total_files = _conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[0]
            _conn.close()
            _edges_per_file = _total_edges / max(1, _total_files)
            if _edges_per_file < 2.0:
                _sparse_graph = True
                weights = {"W_SEM": 0.0, "W_LEX": 0.70, "W_REACH": 0.0, "W_PROX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.0, "W_PATH": 0.45}
        except Exception:
            pass

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
        min_confidence=EDGE_CONFIDENCE_FLOOR,
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

    # Adaptive K: include candidates while score gap is small.
    # Minimum recall guard: always return at least 5 candidates if available.
    # This prevents adaptive K from returning 1 wrong file when recall is low.
    scores = [r.get("score", 0.0) for r in v74.ranked_full]
    min_k = min(5, len(v74.ranked_full))  # minimum 5 (or all if fewer)
    if len(scores) >= 2:
        gaps = [scores[i] - scores[i + 1] for i in range(min(len(scores) - 1, 10))]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.1
        k = 1
        for i in range(1, min(len(scores), 8)):
            if i < len(gaps) and gaps[i - 1] > median_gap * 2:
                break
            k = i + 1
        top_records = v74.ranked_full[:max(min(k, max_files), min_k)]
    else:
        top_records = v74.ranked_full[:max_files]

    # Filter non-source files from candidates — changelogs, READMEs, configs, docs
    # rank high on BM25 keywords but are never edit targets
    _NON_SOURCE = {"CHANGELOG.md", "CHANGES.rst", "HISTORY.md", "README.md", "README.rst",
                   "CONTRIBUTING.md", "LICENSE", "LICENSE.md", "setup.py", "setup.cfg",
                   "pyproject.toml", "Makefile", "Dockerfile", ".gitignore"}
    _NON_SOURCE_EXTS = {".rst", ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
    top_records = [
        r for r in top_records
        if os.path.basename(r.get("path", "")) not in _NON_SOURCE
        and os.path.splitext(r.get("path", ""))[1].lower() not in _NON_SOURCE_EXTS
    ]
    if not top_records:
        top_records = v74.ranked_full[:max_files]  # fallback if all filtered

    # Path-match preservation: if a candidate has strong path-name match
    # (path component score ≥ 0.5) but didn't make it into top_records,
    # include it by replacing the lowest-scored entry. This prevents
    # BM25-dominant files from pushing out name-matched candidates.
    _top_paths_set = {r.get("path") for r in top_records}
    _path_rescued: list[dict] = []
    for r in v74.ranked_full:
        if r.get("path") in _top_paths_set:
            continue
        comps = r.get("components", {})
        if comps.get("path", 0.0) >= 0.5:
            bn = os.path.basename(r.get("path", ""))
            ext = os.path.splitext(bn)[1].lower()
            if bn not in _NON_SOURCE and ext not in _NON_SOURCE_EXTS:
                _path_rescued.append(r)
        if len(_path_rescued) >= 2:
            break
    if _path_rescued and len(top_records) >= max_files:
        for pr in _path_rescued:
            if len(top_records) < max_files:
                top_records.append(pr)
            else:
                top_records[-1] = pr

    # Graph neighbor expansion: callers/callees of top-ranked files become
    # candidates themselves. This is the core GT-agent collaboration: L1 gives
    # the NEIGHBORHOOD, not just the ranked list. The agent navigates from there.
    if graph_db and top_records:
        _existing_paths = {r.get("path") for r in top_records}
        _neighbor_candidates: list[dict] = []
        try:
            _nc = sqlite3.connect(graph_db)
            _has_conf = _has_confidence(graph_db)
            _conf_clause = f"AND e.confidence >= {EDGE_CONFIDENCE_FLOOR}" if _has_conf else ""
            for rec in top_records[:3]:
                fp = rec.get("path", "")
                if not fp:
                    continue
                # Get callers and callees (1-hop neighbors)
                rows = _nc.execute(
                    f"""
                    SELECT DISTINCT n2.file_path FROM nodes n1
                    JOIN edges e ON e.source_id = n1.id {_conf_clause}
                    JOIN nodes n2 ON e.target_id = n2.id
                    WHERE n1.file_path = ? AND n2.file_path != ? AND n2.is_test = 0
                    UNION
                    SELECT DISTINCT n1.file_path FROM nodes n2
                    JOIN edges e ON e.target_id = n2.id {_conf_clause}
                    JOIN nodes n1 ON e.source_id = n1.id
                    WHERE n2.file_path = ? AND n1.file_path != ? AND n1.is_test = 0
                    """,
                    (fp, fp, fp, fp),
                ).fetchall()
                for (neighbor,) in rows:
                    if neighbor in _existing_paths:
                        continue
                    bn = os.path.basename(neighbor)
                    ext = os.path.splitext(bn)[1].lower()
                    if bn in _NON_SOURCE or ext in _NON_SOURCE_EXTS:
                        continue
                    _neighbor_candidates.append({
                        "path": neighbor,
                        "score": rec.get("score", 0) * 0.8,
                        "components": {"path": 0.0},
                    })
                    _existing_paths.add(neighbor)
                    if len(_neighbor_candidates) >= 3:
                        break
                if len(_neighbor_candidates) >= 3:
                    break
            _nc.close()
        except Exception:
            pass
        # Insert neighbors after current top records (they'll be ranked 4-7ish)
        top_records.extend(_neighbor_candidates)

    # Cross-domain detection + expansion (Decision 26)
    if _detect_overconfident_convergence(top_records, graph_db):
        symptom_files = [r.get("path", "") for r in top_records[:5]]
        cochange_bridges = _expand_via_cochange(symptom_files, repo_root)
        test_bridges = _expand_via_test_coimport(symptom_files, graph_db)

        # Add bridges at lower score (60% of lowest top-5 score)
        if top_records:
            bridge_score = top_records[min(4, len(top_records) - 1)].get("score", 0) * 0.6
            for bridge in cochange_bridges + test_bridges:
                bridge["score"] = bridge_score
                if bridge["path"] not in {r.get("path") for r in top_records}:
                    top_records.append(bridge)

    # Decision 29: redundancy suppression removed. It killed briefs on too many tasks
    # (required all top-3 to enter via "both" paths), leaving agent with zero localization.
    # The modulus gate below handles the "all candidates are noise" case.

    # Hub demotion: reorder so peripheral files come before hubs.
    # NEVER suppress the brief entirely — an imperfect brief is better than none.
    _indexed_file_count = len(v74.ranked_full) if v74 else 0
    if top_records and graph_db and _indexed_file_count >= 50 and not _sparse_graph:
        try:
            conn = sqlite3.connect(graph_db)
            all_degrees = [r[0] for r in conn.execute(
                "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id GROUP BY n.file_path"
            ).fetchall()]
            conn.close()
            if all_degrees:
                p80 = sorted(all_degrees)[int(len(all_degrees) * 0.8)]
                if p80 > 0:
                    top_paths = [str(r.get("path", "")) for r in top_records[:5]]
                    conn = sqlite3.connect(graph_db)
                    top_degrees = []
                    for p in top_paths:
                        row = conn.execute(
                            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id WHERE n.file_path = ?", (p,)
                        ).fetchone()
                        top_degrees.append(row[0] if row else 0)
                    conn.close()
                    # Demote hubs behind peripheral candidates (never suppress)
                    hub_records = [r for r, d in zip(top_records[:5], top_degrees) if d > p80]
                    non_hub_records = [r for r, d in zip(top_records[:5], top_degrees) if d <= p80]
                    rest = top_records[5:]
                    if non_hub_records:
                        top_records = non_hub_records + hub_records + rest
        except Exception:
            pass

    _words = set(
        w.lower() for w in _re.findall(r"[A-Za-z_]\w{2,}", issue_text)
        if len(w) > 3
    )

    entries: list[FileEntry] = []
    for rec in top_records:
        path = str(rec.get("path", ""))
        score = float(rec.get("score", 0.0))
        funcs = _top_functions(graph_db, path)
        tests = _test_files_for(graph_db, path)
        neighbors = _issue_relevant_neighbors(
            graph_db, path, repo_root, _words,
        )
        func_names = _top_function_names(graph_db, path, issue_terms=_words)
        contract = _caller_contract_for_file(graph_db, path, repo_root, func_names)
        siblings = _sibling_context(graph_db, path, func_names)
        last_chg = _last_change(path, repo_root)
        co_changes = _co_change_files(path, repo_root)
        spec_parts = [_function_spec(graph_db, path, fn, repo_root) for fn in func_names[:2]]
        spec = " | ".join(s for s in spec_parts if s)
        pattern = f"{siblings}" if siblings else ""
        if last_chg:
            pattern = f"{pattern} | Last: {last_chg}" if pattern else f"Last: {last_chg}"
        entries.append(FileEntry(
            path=path,
            score=score,
            functions=funcs,
            test_mappings=tests,
            callees=neighbors,
            co_changes=co_changes,
            contract=contract,
            pattern=pattern,
            spec=spec,
        ))

    # Compute cross-file scope (Signal 1)
    _scope_files: list[str] = []
    _scope_confidence = "low"
    if graph_db and entries and not _sparse_graph:
        from groundtruth.config.signal_thresholds import (
            SCOPE_MIN_CALLER_FILES,
            SCOPE_MIN_EDGE_CONFIDENCE,
            SCOPE_HIGH_RESOLUTION_METHODS,
            SPARSE_GRAPH_THRESHOLD,
            log_threshold_use,
        )
        try:
            _sc = sqlite3.connect(graph_db)
            _top_path = entries[0].path
            _has_conf = _has_confidence(graph_db)
            if _has_conf:
                _scope_rows = _sc.execute(
                    """SELECT DISTINCT nsrc.file_path, e.resolution_method, e.confidence
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
                       ORDER BY e.confidence DESC LIMIT 10""",
                    (_top_path, _top_path),
                ).fetchall()
            else:
                _scope_rows = _sc.execute(
                    """SELECT DISTINCT nsrc.file_path, '' as res, 0.5 as conf
                       FROM nodes nt
                       JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                       JOIN nodes nsrc ON e.source_id = nsrc.id
                       WHERE nt.file_path = ? AND nsrc.file_path != ? AND nsrc.is_test = 0
                       LIMIT 10""",
                    (_top_path, _top_path),
                ).fetchall()
            _sc.close()

            _distinct_files = list(dict.fromkeys(r[0] for r in _scope_rows))
            _high_conf_files = [
                r[0] for r in _scope_rows
                if r[1] in SCOPE_HIGH_RESOLUTION_METHODS and float(r[2]) >= SCOPE_MIN_EDGE_CONFIDENCE
            ]
            _high_distinct = list(dict.fromkeys(_high_conf_files))

            if len(_high_distinct) >= SCOPE_MIN_CALLER_FILES:
                _scope_files = _high_distinct[:3]
                _scope_confidence = "high"
            elif len(_distinct_files) >= SCOPE_MIN_CALLER_FILES:
                _scope_files = _distinct_files[:3]
                _scope_confidence = "medium"

            log_threshold_use(
                "L1_SCOPE", _scope_confidence,
                f"top={_top_path} distinct={len(_distinct_files)} high={len(_high_distinct)}",
            )
        except Exception:
            pass

    _scores = [r.get("score", 0.0) for r in top_records[:len(entries)]]
    brief_text = render_brief(entries, scores=_scores, scope_files=_scope_files, scope_confidence=_scope_confidence, issue_text=issue_text)
    tok = _estimate_tokens(brief_text)

    while tok > max_brief_tokens and len(entries) > 1:
        entries = entries[:-1]
        _scores = _scores[:len(entries)]
        brief_text = render_brief(entries, scores=_scores, scope_files=_scope_files, scope_confidence=_scope_confidence, issue_text=issue_text)
        tok = _estimate_tokens(brief_text)

    result = V1RBriefResult(
        files=entries,
        brief_text=brief_text,
        token_estimate=tok,
        v74_result=v74,
    )

    # Structured telemetry: emit L1 candidates as JSON for wrapper to parse
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        try:
            import json as _json
            l1_items = []
            for entry in entries:
                l1_items.append({
                    "kind": "l1_candidate",
                    "file_path": entry.path,
                    "confidence": entry.score,
                    "source": "graph_db",
                    "reason": f"V1R score={entry.score:.3f}",
                    "text": ", ".join(entry.functions[:3]) if entry.functions else "",
                })
            structured = {
                "candidates": l1_items,
                "candidate_count": len(entries),
                "graph_edge_count": sum(1 for e in entries if e.callees),
                "test_edge_count": sum(1 for e in entries if e.test_mappings),
                "signature_count": sum(1 for e in entries if e.functions),
                "warnings": [],
                "abstain_reason": None,
            }
            if not entries:
                structured["abstain_reason"] = "no_candidates"
            with open("/tmp/gt_l1_structured.json", "w") as _f:
                _json.dump(structured, _f)
        except Exception:
            pass

    return result
