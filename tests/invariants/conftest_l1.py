"""Helper functions for L1 localization invariant tests.

These functions extract issue-symbol-matched files and score edit-target
candidates from a graph.db, implementing the invariant that issue-named
functions must outrank high-caller hubs.
"""
import re
import sqlite3
from pathlib import Path


_COMMON_FN_PARTS = {
    "get", "set", "add", "remove", "update", "create",
    "delete", "find", "make", "check", "is", "has",
    "do", "run", "to", "from", "on", "in", "of", "by",
}

_STOP_WORDS = {
    "that", "this", "with", "from", "have", "been",
    "when", "then", "should", "would", "could",
    "file", "line", "code", "test", "error", "issue",
    "none", "true", "false", "self", "class",
    "return", "function", "method", "import", "raise",
    "except", "print", "string", "object", "value",
    "result", "data", "list", "dict", "type", "name",
    "path", "args", "kwargs", "super", "init", "call",
    "make", "using", "does", "work", "need", "want",
    "like", "also", "just", "some", "only", "more",
}


def create_graph_db(db_path: Path, nodes: list, edges: list) -> None:
    """Create a minimal graph.db with nodes and edges.

    nodes: list of (name, label, file_path, signature, start_line, end_line, is_exported)
    edges: list of (source_name, target_name, type, confidence)
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'python',
            parent_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT,
            trust_tier TEXT DEFAULT 'CERTIFIED',
            candidate_count INTEGER DEFAULT 1,
            evidence_type TEXT,
            verification_status TEXT DEFAULT 'verified'
        )
    """)

    name_to_id: dict[str, int] = {}
    for name, label, file_path, sig, start, end, is_exported in nodes:
        is_test = "test" in file_path.lower() or name.startswith("test_")
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, signature, start_line, end_line, is_exported, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (label, name, file_path, sig, start, end, int(is_exported), int(is_test)),
        )
        name_to_id[name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for src_name, tgt_name, edge_type, confidence in edges:
        src_id = name_to_id.get(src_name)
        tgt_id = name_to_id.get(tgt_name)
        if src_id and tgt_id:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, confidence, resolution_method) "
                "VALUES (?, ?, ?, ?, ?)",
                (src_id, tgt_id, edge_type, confidence, "import" if confidence >= 0.9 else "name_match"),
            )

    conn.commit()
    conn.close()


def extract_issue_symbol_files(graph_db: str, issue_text: str) -> list[str]:
    """Extract files containing functions whose names appear in issue text.

    Returns list of file paths, ordered by number of matched symbols descending.
    This implements L1-INV-1: issue-named symbols → their files must be in search space.
    """
    issue_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text))
    issue_lower = {t.lower() for t in issue_tokens if len(t) > 3 and t.lower() not in _STOP_WORDS}

    conn = sqlite3.connect(graph_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, file_path FROM nodes WHERE is_test = 0"
    ).fetchall()
    conn.close()

    matched: dict[str, list[str]] = {}
    for row in rows:
        name = row["name"]
        fpath = row["file_path"]
        if not name or not fpath:
            continue
        if name.lower() in issue_lower:
            matched.setdefault(fpath, []).append(name)

    ranked = sorted(matched.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [fp for fp, _ in ranked]


def score_edit_target_candidates(graph_db: str, issue_text: str) -> list[dict]:
    """Score all functions in graph.db against issue text.

    Returns sorted list of candidates with score, file, func, callers, direct flag.
    This implements L1-INV-2: issue-relevant functions outrank high-caller hubs.
    """
    issue_kws = {
        w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text)
        if len(w) > 3 and w.lower() not in _STOP_WORDS
    }

    conn = sqlite3.connect(graph_db)
    conn.row_factory = sqlite3.Row

    all_funcs = conn.execute(
        "SELECT id, name, file_path, signature, start_line FROM nodes "
        "WHERE is_test = 0 AND label IN ('Function', 'Method', 'Class')"
    ).fetchall()

    candidates = []
    for func in all_funcs:
        fn_parts = set(re.split(r"[_]|(?<=[a-z])(?=[A-Z])", func["name"]))
        fn_parts = {p.lower() for p in fn_parts if p and p.lower() not in _COMMON_FN_PARTS}
        kw_overlap = len(fn_parts & issue_kws)
        direct = func["name"].lower() in issue_text.lower()

        score = 0
        if direct:
            score += 1000
        score += kw_overlap * 10

        caller_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS' "
            "AND COALESCE(confidence, 0.5) >= 0.6",
            (func["id"],),
        ).fetchone()[0]
        score += min(caller_count, 5)

        candidates.append({
            "func": func["name"],
            "file": func["file_path"],
            "sig": func["signature"] or "",
            "callers": caller_count,
            "score": score,
            "direct": direct,
            "kw_overlap": kw_overlap,
        })

    conn.close()
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
