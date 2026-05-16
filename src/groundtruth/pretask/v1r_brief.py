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
MAX_BRIEF_TOKENS = 400
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
    contract: str = ""


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

_NONE_CHECK_RE = _re.compile(r"\b(is none|is not none|not \w+|== none|!= none)\b", _re.IGNORECASE)
_ATTR_ACCESS_RE = _re.compile(r"\b\w+\.(\w+)")
_ITERATION_RE = _re.compile(r"\bfor\s+\w+\s+in\s+")
_RAISE_RE = _re.compile(r"\braise\b")
_INDEX_RE = _re.compile(r"\w+\[")

_TRIVIAL_ATTRS = frozenset({
    "append", "extend", "items", "keys", "values", "get",
    "strip", "split", "join", "format", "encode", "decode",
    "lower", "upper", "replace", "startswith", "endswith",
})

CALLER_CONFIDENCE_FLOOR = 0.9
MAX_CALLERS_PER_FILE = 5


def _caller_contract_for_file(
    graph_db: str,
    file_path: str,
    repo_root: str,
    func_names: list[str],
) -> str:
    """Extract a compact caller-usage contract for the top functions in a file.

    Queries cross-file callers at confidence >= 0.9, reads their source lines,
    and pattern-matches how they use the return value. Returns a one-line
    contract summary or empty string.
    """
    if not func_names:
        return ""

    try:
        conn = sqlite3.connect(graph_db)
        has_conf = _has_confidence(graph_db)
    except Exception:
        return ""

    none_checks = 0
    attr_accesses: dict[str, int] = {}
    iterations = 0
    raises_count = 0
    indexes = 0
    total_analyzed = 0

    try:
        for fname in func_names[:3]:
            conf_clause = f"AND e.confidence >= {CALLER_CONFIDENCE_FLOOR}" if has_conf else ""
            rows = conn.execute(
                f"""
                SELECT nsrc.file_path, e.source_line
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' {conf_clause}
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND nt.file_path = ?
                  AND nsrc.file_path != nt.file_path
                  AND e.source_line > 0
                ORDER BY e.source_line
                LIMIT ?
                """,
                (fname, file_path, MAX_CALLERS_PER_FILE),
            ).fetchall()

            for caller_file, source_line in rows:
                full_path = os.path.join(repo_root, caller_file)
                try:
                    with open(full_path, encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    if source_line <= 0 or source_line > len(lines):
                        continue
                    context = "".join(lines[source_line - 1:min(source_line + 2, len(lines))]).lower()
                except OSError:
                    continue

                total_analyzed += 1
                if _NONE_CHECK_RE.search(context):
                    none_checks += 1
                if _RAISE_RE.search(context):
                    raises_count += 1
                if _ITERATION_RE.search(context):
                    iterations += 1
                if _INDEX_RE.search(context):
                    indexes += 1
                for m in _ATTR_ACCESS_RE.finditer(context):
                    attr = m.group(1)
                    if attr not in _TRIVIAL_ATTRS:
                        attr_accesses[attr] = attr_accesses.get(attr, 0) + 1
    finally:
        conn.close()

    if total_analyzed == 0:
        return ""

    constraints: list[str] = []
    if none_checks > 0:
        constraints.append(f"{none_checks}/{total_analyzed} callers check None")
    if raises_count > 0:
        constraints.append(f"{raises_count}/{total_analyzed} raise on failure")
    if iterations > 0:
        constraints.append(f"{iterations}/{total_analyzed} iterate result")
    if indexes > 0:
        constraints.append(f"{indexes}/{total_analyzed} index into result")

    top_attrs = sorted(attr_accesses.items(), key=lambda x: -x[1])[:2]
    for attr, count in top_attrs:
        if count >= 2:
            constraints.append(f".{attr} used by {count}/{total_analyzed}")

    if not constraints:
        return ""
    return "; ".join(constraints[:3])


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
        elif " " in line and len(line.split()) == 2 and line[0] != " ":
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


def render_brief(files: list[FileEntry]) -> str:
    lines = ["<gt-task-brief>"]
    for i, f in enumerate(files, 1):
        funcs = ", ".join(f.functions) if f.functions else ""
        line = f"{i}. {f.path}"
        if funcs:
            line += f" ({funcs})"
        lines.append(line)
        if f.contract:
            lines.append(f"   Contract: {f.contract}")
        if f.callees:
            lines.append(f"   Calls: {', '.join(f.callees)}")
        if f.test_mappings:
            lines.append(f"   Tests: {', '.join(f.test_mappings)}")
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
    if weights is None and graph_db:
        try:
            _conn = sqlite3.connect(graph_db)
            _total_edges = _conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            _total_files = _conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[0]
            _conn.close()
            _edges_per_file = _total_edges / max(1, _total_files)
            if _edges_per_file < 2.0:
                weights = {"W_SEM": 0.0, "W_LEX": 0.70, "W_REACH": 0.0, "W_PROX": 0.0, "W_HUB": 0.0, "W_COMMIT": 0.30}
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

    # Adaptive K: include candidates while score gap is small
    scores = [r.get("score", 0.0) for r in v74.ranked_full]
    if len(scores) >= 2:
        gaps = [scores[i] - scores[i + 1] for i in range(min(len(scores) - 1, 10))]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.1
        # Include candidates until gap exceeds 2x median
        k = 1
        for i in range(1, min(len(scores), 8)):  # max 8
            if i < len(gaps) and gaps[i - 1] > median_gap * 2:
                break
            k = i + 1
        top_records = v74.ranked_full[:max(min(k, max_files), 3)]  # at least 3, at most max_files
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

    # Modulus gate: suppress brief if all top candidates are high-centrality hubs.
    # When brief is wrong, it's WORSE than no brief (agent trusts it, wastes iters).
    # Skip for small repos (< 50 indexed files) — hub vs non-hub is meaningless
    # when every file has high relative in-degree.
    _indexed_file_count = len(v74.ranked_full) if v74 else 0
    if top_records and graph_db and _indexed_file_count >= 50:
        try:
            conn = sqlite3.connect(graph_db)
            all_degrees = [r[0] for r in conn.execute(
                "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id GROUP BY n.file_path"
            ).fetchall()]
            conn.close()
            if all_degrees:
                p80 = sorted(all_degrees)[int(len(all_degrees) * 0.8)]
                if p80 > 0:
                    top_paths = [str(r.get("path", "")) for r in top_records[:3]]
                    conn = sqlite3.connect(graph_db)
                    top_degrees = []
                    for p in top_paths:
                        row = conn.execute(
                            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id WHERE n.file_path = ?", (p,)
                        ).fetchone()
                        top_degrees.append(row[0] if row else 0)
                    conn.close()
                    if all(d > p80 for d in top_degrees):
                        # All top candidates are hubs — suppress entirely
                        return V1RBriefResult(
                            files=[],
                            brief_text="",
                            token_estimate=0,
                            v74_result=v74,
                        )
                    # Demote hub candidates: if top-1 is a hub but others aren't,
                    # reorder so peripheral files come first (they're more likely fix targets)
                    if top_degrees and top_degrees[0] > p80 * 5:
                        # Top-1 is a massive hub — demote it behind peripheral candidates
                        hub_records = [r for r, d in zip(top_records[:3], top_degrees) if d > p80]
                        non_hub_records = [r for r, d in zip(top_records[:3], top_degrees) if d <= p80]
                        rest = top_records[3:]
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
        entries.append(FileEntry(
            path=path,
            score=score,
            functions=funcs,
            test_mappings=tests,
            callees=neighbors,
            contract=contract,
        ))

    brief_text = render_brief(entries)
    tok = _estimate_tokens(brief_text)

    while tok > max_brief_tokens and len(entries) > 1:
        entries = entries[:-1]
        brief_text = render_brief(entries)
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
