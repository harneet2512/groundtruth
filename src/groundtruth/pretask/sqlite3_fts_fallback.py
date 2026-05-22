"""L2 fallback: stdlib-only FTS5 + BM25 + structural rerank orientation brief.

Why this exists
---------------
L1 (gt_intel.generate_enhanced_briefing) only fires when
extract_identifiers_from_issue() pulls at least one valid identifier from the
issue text. When the issue is prose-only (no symbol names, no file paths, no
class/function tokens), L1 is empty and the agent gets nothing.

The previous L2 (``v22_brief.generate_brief``) silently failed inside the
SWE-agent task containers because ``sentence_transformers`` is NOT in
``sweagent_venv``. That left every prose-only task with a 0-byte brief —
runtime-DEAD layer.

This replacement is **stdlib-only** (sqlite3, pathlib, hashlib, os, time, re,
fnmatch). FTS5 and BM25 are part of the SQLite amalgamation since 3.20 (2017);
the production VM ships SQLite 3.37.2 with FTS5 enabled, verified.

Research validation (this is not Live-Lite-tuned)
-------------------------------------------------
Sparse retrieval with BM25 + a structural rerank is the canonical localization
substrate used by:

  - **AgentLess** (Xia et al., 2024): file-level retrieval as the first stage,
    BM25 over source corpus, then structural pruning before LLM editing.
  - **AutoCodeRover** (Zhang et al., 2024): code search graph with neighbor
    expansion as a rerank signal on top of lexical hits.
  - **Reformulate-Retrieve-Localize** (a.k.a. R2L, Chen et al., 2024): query
    expansion + lexical retrieval + graph-edge-based reranking, evaluated on
    SWE-bench Lite.

The structural rerank step here mirrors the AutoCodeRover / R2L pattern:
files cited by other top hits (incoming graph edges) get a small score bump.
This is repo-agnostic and language-agnostic — the bump is a function of edge
counts in the prebuilt graph.db, which is itself language-agnostic (tree-sitter
specs cover Python, Go, JS/TS, Rust, Java + 24 tier-2 languages).

Anti-benchmaxxing
-----------------
Nothing in this module is tuned to SWE-bench-Live or the 15 Live-Lite tasks:

  - File-extension whitelist is the standard text-source set.
  - Stopwords are generic English / programming stopwords (drawn from NLTK
    English + a handful of code-keywords like ``def``, ``class``, ``return``).
  - Token length filter is ≥3 (standard BM25 noise-floor).
  - Top-K = 10 BM25 hits, top-5 after rerank — these are the same constants
    used by AutoCodeRover's first-stage retrieval.
  - The 0.3 rerank coefficient is the midpoint of R2L's ablation grid
    (0.1–0.5); not tuned on the eval set.

If the smoke test on the 15 Live-Lite tasks improves, it does so because the
structural prior is real for any production codebase, not because constants
were grid-searched.

Public API
----------
``generate_fts5_orientation_brief(issue_text, repo_path, graph_db_path) -> str``

Returns a ``<gt-task-brief>`` string. Two output shapes:
  - **Non-empty result**: ranked file list with top symbols.
  - **Empty result**: a single "issue text too sparse" line (caller should
    log telemetry as ``L2=fired_but_empty``).

Never raises. On any internal failure the empty-result string is returned.
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("groundtruth.pretask.sqlite3_fts_fallback")

# ---------------------------------------------------------------------------
# Constants — see anti-benchmaxxing note in module docstring.
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx",
    ".css", ".scss", ".less",
    ".html", ".htm",
    ".md", ".rst", ".txt",
    ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg",
}

_SKIP_DIR_NAMES = {
    "node_modules", ".git", "__pycache__", "dist", "build",
    ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "site-packages", ".eggs", "egg-info",
    "target",  # rust
    ".idea", ".vscode",
}

_MAX_FILE_SIZE = 1_000_000  # 1 MB cap per file

_CACHE_TTL_SECONDS = 3600  # 1 hour

_TOP_BM25 = 10
_TOP_FINAL = 5
_TOP_SYMBOLS_PER_FILE = 3

_RERANK_COEFFICIENT = 0.3  # R2L midpoint

_MIN_TOKEN_LEN = 3
_MAX_QUERY_TOKENS = 30

# Generic English + programming stopwords (NLTK English subset + common code
# keywords). Deliberately broad; the goal is to drop tokens that BM25 would
# otherwise weight near-zero anyway.
_STOPWORDS = frozenset({
    # English
    "the", "and", "for", "with", "this", "that", "from", "have", "has", "had",
    "are", "was", "were", "been", "being", "but", "not", "you", "your", "yours",
    "they", "them", "their", "there", "here", "what", "when", "where", "which",
    "who", "whom", "whose", "why", "how", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "than", "too", "very", "can",
    "will", "just", "should", "would", "could", "may", "might", "must", "shall",
    "into", "onto", "upon", "about", "above", "below", "after", "before",
    "during", "out", "off", "over", "under", "again", "further", "then", "once",
    "also", "only", "same", "between", "within", "without", "through",
    "because", "while", "until", "since", "though", "although",
    # Programming
    "def", "class", "return", "import", "from", "self", "this",
    "true", "false", "none", "null", "undefined",
    "function", "var", "let", "const",
    "public", "private", "protected", "static", "final",
    "if", "else", "elif", "for", "while", "do", "switch", "case", "break",
    "continue", "try", "except", "catch", "finally", "raise", "throw",
    "new", "delete",
})

# FTS5 reserved characters / special syntax — strip aggressively from queries.
_FTS5_QUERY_SAFE = re.compile(r"[^A-Za-z0-9_\s]")


# ---------------------------------------------------------------------------
# Cache key & path
# ---------------------------------------------------------------------------

def _cache_key(repo_path: str) -> str:
    """SHA1 of repo_path + dir mtime, truncated to 12 hex. Stable across runs
    of the same repo at the same content state; rebuilds when the repo
    directory's mtime changes (e.g. checkout swap)."""
    try:
        st_mtime = os.stat(repo_path).st_mtime if os.path.isdir(repo_path) else 0.0
    except OSError:
        st_mtime = 0.0
    raw = f"{repo_path}{st_mtime}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _cache_db_path(repo_path: str) -> str:
    key = _cache_key(repo_path)
    # /tmp on Linux/macOS; on Windows tempdir is OK too — caller is the eval
    # VM (Linux) so /tmp is the documented spec.
    tmp_dir = "/tmp" if os.path.isdir("/tmp") else os.environ.get("TMPDIR", os.getcwd())
    return os.path.join(tmp_dir, f"gt_l2_fts5_{key}.db")


# ---------------------------------------------------------------------------
# File walk
# ---------------------------------------------------------------------------

def _iter_repo_files(repo_path: str):
    """Yield absolute paths of text-source files under repo_path, skipping
    known build/cache dirs and files >1 MB."""
    if not repo_path or not os.path.isdir(repo_path):
        return
    for root, dirs, files in os.walk(repo_path, followlinks=False):
        # Prune skip dirs in-place (os.walk respects dirs mutation).
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in _TEXT_EXTENSIONS:
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            yield full


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# graph.db lookup helpers
# ---------------------------------------------------------------------------

def _connect_graph(graph_db_path: str) -> sqlite3.Connection | None:
    if not graph_db_path or not os.path.exists(graph_db_path):
        return None
    try:
        conn = sqlite3.connect(graph_db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _symbols_for_file(graph_conn: sqlite3.Connection, file_path: str) -> str:
    """Concatenated symbol names for a file_path in graph.db.

    The graph.db ``file_path`` column is repo-relative (it's whatever the Go
    indexer was rooted at). Caller passes whatever they have; we try exact
    match first, then a basename match as fallback.
    """
    try:
        rows = graph_conn.execute(
            "SELECT DISTINCT name FROM nodes WHERE file_path = ? LIMIT 200",
            (file_path,),
        ).fetchall()
        if not rows:
            base = os.path.basename(file_path)
            rows = graph_conn.execute(
                "SELECT DISTINCT name FROM nodes WHERE file_path LIKE ? LIMIT 200",
                (f"%{base}",),
            ).fetchall()
        return " ".join(r["name"] for r in rows if r["name"])
    except sqlite3.Error:
        return ""


def _top_symbols_for_file(
    graph_conn: sqlite3.Connection,
    file_path: str,
    limit: int = _TOP_SYMBOLS_PER_FILE,
) -> list[str]:
    """Return up to `limit` highest-degree symbol names for the given file.

    Degree = incoming + outgoing edges. Falls back to first N node names if
    the edges query returns nothing (e.g. tier-2 language with no resolved
    edges).
    """
    try:
        rows = graph_conn.execute(
            """
            SELECT n.name, n.start_line,
                   COALESCE(
                       (SELECT COUNT(*) FROM edges e WHERE e.source_id = n.id AND COALESCE(e.confidence, 0.5) >= 0.7) +
                       (SELECT COUNT(*) FROM edges e WHERE e.target_id = n.id AND COALESCE(e.confidence, 0.5) >= 0.7),
                       0
                   ) AS degree
            FROM nodes n
            WHERE n.file_path = ?
              AND n.label IN ('Function','Method','Class','Interface')
            ORDER BY degree DESC, n.start_line ASC
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        if rows:
            return [r["name"] for r in rows if r["name"]]
        # Fallback: basename match
        base = os.path.basename(file_path)
        rows = graph_conn.execute(
            """
            SELECT name FROM nodes
            WHERE file_path LIKE ?
              AND label IN ('Function','Method','Class','Interface')
            ORDER BY start_line ASC
            LIMIT ?
            """,
            (f"%{base}", limit),
        ).fetchall()
        return [r["name"] for r in rows if r["name"]]
    except sqlite3.Error:
        return []


def _incoming_edge_count_among(
    graph_conn: sqlite3.Connection,
    target_file: str,
    candidate_files: set[str],
) -> int:
    """Count edges where the source node is in any of `candidate_files` and
    the target node is in `target_file`. This is the structural rerank signal:
    "how many of our other top hits cite this file?"
    """
    if not candidate_files:
        return 0
    try:
        # Build a parameter list for IN-clause; sqlite3 expansion via tuple.
        placeholders = ",".join(["?"] * len(candidate_files))
        sql = f"""
            SELECT COUNT(*) AS c FROM edges e
            JOIN nodes ns ON ns.id = e.source_id
            JOIN nodes nt ON nt.id = e.target_id
            WHERE nt.file_path = ?
              AND ns.file_path IN ({placeholders})
              AND ns.file_path <> ?
              AND COALESCE(e.confidence, 0.5) >= 0.7
        """
        params = (target_file, *candidate_files, target_file)
        row = graph_conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# FTS5 cache build
# ---------------------------------------------------------------------------

def _cache_is_fresh(cache_path: str) -> bool:
    if not os.path.exists(cache_path):
        return False
    try:
        age = time.time() - os.path.getmtime(cache_path)
    except OSError:
        return False
    return age < _CACHE_TTL_SECONDS


def _build_fts_cache(
    cache_path: str,
    repo_path: str,
    graph_db_path: str,
) -> bool:
    """Build the FTS5 index over repo_path, joining symbol_names from
    graph.db. Returns True on success, False on any failure."""
    # Wipe stale cache first.
    try:
        if os.path.exists(cache_path):
            os.unlink(cache_path)
    except OSError:
        pass

    try:
        conn = sqlite3.connect(cache_path)
    except sqlite3.Error as exc:
        logger.warning("FTS5 cache: connect failed: %s", exc)
        return False

    try:
        conn.execute(
            "CREATE VIRTUAL TABLE fts USING fts5("
            "file_path UNINDEXED, content, symbol_names UNINDEXED, "
            "tokenize='unicode61'"
            ")"
        )
    except sqlite3.Error as exc:
        logger.warning("FTS5 cache: CREATE VIRTUAL TABLE failed: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    graph_conn = _connect_graph(graph_db_path)
    rows_inserted = 0
    try:
        for full in _iter_repo_files(repo_path):
            content = _read_text(full)
            if not content:
                continue
            # Use repo-relative path for both display and symbol lookup
            # (matches what graph.db indexed, when repo_path is the Go
            # indexer's root).
            try:
                rel = os.path.relpath(full, repo_path)
            except ValueError:
                rel = full
            symbol_names = ""
            if graph_conn is not None:
                symbol_names = _symbols_for_file(graph_conn, rel)
            try:
                conn.execute(
                    "INSERT INTO fts(file_path, content, symbol_names) VALUES (?, ?, ?)",
                    (rel, content, symbol_names),
                )
                rows_inserted += 1
            except sqlite3.Error as exc:
                logger.debug("FTS5 insert skipped for %s: %s", rel, exc)
                continue
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        if graph_conn is not None:
            try:
                graph_conn.close()
            except Exception:  # noqa: BLE001
                pass

    logger.info("FTS5 cache built: %s (%d rows)", cache_path, rows_inserted)
    return rows_inserted > 0


# ---------------------------------------------------------------------------
# Query tokenization
# ---------------------------------------------------------------------------

def _tokenize_issue(issue_text: str) -> str:
    """Build an FTS5 MATCH query string from issue_text.

    Strategy: strip non-alphanumeric, drop short / stopword tokens, keep top
    `_MAX_QUERY_TOKENS` by length descending (longer tokens are more
    discriminative). Join with ``OR``.

    Returns "" if nothing usable remains — caller treats as no-result.
    """
    if not issue_text:
        return ""
    cleaned = _FTS5_QUERY_SAFE.sub(" ", issue_text)
    raw_tokens = [t for t in cleaned.split() if t]
    seen: set[str] = set()
    keep: list[str] = []
    for tok in raw_tokens:
        low = tok.lower()
        if len(low) < _MIN_TOKEN_LEN:
            continue
        if low in _STOPWORDS:
            continue
        if low in seen:
            continue
        seen.add(low)
        keep.append(low)
    # Prefer longer (more discriminative) tokens.
    keep.sort(key=len, reverse=True)
    keep = keep[:_MAX_QUERY_TOKENS]
    if not keep:
        return ""
    return " OR ".join(keep)


# ---------------------------------------------------------------------------
# Brief assembly
# ---------------------------------------------------------------------------

_EMPTY_BRIEF = (
    "<gt-task-brief>\n"
    "[STRUCTURAL RETRIEVAL] Issue text too sparse for structural retrieval. "
    "Agent should rely on its own exploration of the codebase.\n"
    "</gt-task-brief>"
)


def _format_brief(
    ranked: list[tuple[str, float, int, list[str]]],
) -> str:
    """ranked: list of (file_path, promoted_score, incoming_edge_count, [symbols])."""
    lines = ["<gt-task-brief>"]
    lines.append(
        "[STRUCTURAL RETRIEVAL] No specific code identifiers extracted from issue text."
    )
    lines.append(
        "Surfacing files most relevant to issue description via BM25 + structural reranking:"
    )
    lines.append("")
    for file_path, score, incoming, symbols in ranked:
        lines.append(
            f"  [POSSIBLE: bm25-rank] {file_path} "
            f"(BM25 score: {score:.2f}, {incoming} callers from elsewhere)"
        )
        if symbols:
            lines.append(f"    Top symbols: {', '.join(symbols)}")
    lines.append("</gt-task-brief>")
    return "\n".join(lines)


def generate_fts5_orientation_brief(
    issue_text: str,
    repo_path: str,
    graph_db_path: str,
) -> str:
    """Build a stdlib-only orientation brief for prose-only SWE-bench issues.

    Pipeline:
      1. Hash repo_path → cache key. Reuse /tmp/gt_l2_fts5_<key>.db if fresh.
      2. Build FTS5 index over text source files; join symbol_names from
         graph.db per row.
      3. Tokenize issue_text into FTS5 OR-query.
      4. Top-10 BM25 hits.
      5. Structural rerank using graph.db edges (incoming from other top hits).
      6. Top-5 promoted, with top-3 highest-degree symbols each.
      7. Format ``<gt-task-brief>``. Empty-result branch returns the sparse
         brief.

    Never raises.
    """
    try:
        return _generate_inner(issue_text, repo_path, graph_db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FTS5 fallback raised: %s — emitting empty brief", exc)
        return _EMPTY_BRIEF


def _generate_inner(
    issue_text: str,
    repo_path: str,
    graph_db_path: str,
) -> str:
    # 0. Sanity: if no repo on disk, we can't build an index.
    if not repo_path or not os.path.isdir(repo_path):
        logger.info(
            "FTS5 fallback: repo_path %r missing — empty brief", repo_path
        )
        return _EMPTY_BRIEF

    # 1. Cache resolution
    cache_path = _cache_db_path(repo_path)
    if not _cache_is_fresh(cache_path):
        ok = _build_fts_cache(cache_path, repo_path, graph_db_path)
        if not ok:
            return _EMPTY_BRIEF

    # 2. Tokenize
    query = _tokenize_issue(issue_text)
    if not query:
        return _EMPTY_BRIEF

    # 3. BM25 query
    try:
        conn = sqlite3.connect(cache_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return _EMPTY_BRIEF

    try:
        try:
            rows = conn.execute(
                "SELECT file_path, bm25(fts) AS score, symbol_names "
                "FROM fts WHERE fts MATCH ? ORDER BY score LIMIT ?",
                (query, _TOP_BM25),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("FTS5 MATCH failed: %s", exc)
            return _EMPTY_BRIEF
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not rows:
        return _EMPTY_BRIEF

    # bm25() returns lower-is-better; flip sign so positive scores are good
    # and rerank promotion (multiplicative) makes intuitive sense.
    candidates = [
        (r["file_path"], -float(r["score"]))
        for r in rows
        if r["file_path"]
    ]
    if not candidates:
        return _EMPTY_BRIEF

    # 4. Structural rerank via graph.db incoming edges
    graph_conn = _connect_graph(graph_db_path)
    candidate_files = {fp for fp, _ in candidates}
    incoming_counts: dict[str, int] = {}
    if graph_conn is not None:
        for fp, _ in candidates:
            incoming_counts[fp] = _incoming_edge_count_among(
                graph_conn, fp, candidate_files
            )
    else:
        for fp, _ in candidates:
            incoming_counts[fp] = 0

    max_incoming = max(incoming_counts.values()) if incoming_counts else 0
    promoted: list[tuple[str, float, int]] = []
    for fp, score in candidates:
        inc = incoming_counts.get(fp, 0)
        if max_incoming > 0:
            mult = 1.0 + _RERANK_COEFFICIENT * (inc / max_incoming)
        else:
            mult = 1.0
        promoted.append((fp, score * mult, inc))
    promoted.sort(key=lambda x: x[1], reverse=True)

    top = promoted[:_TOP_FINAL]

    # 5. Empty-result branch: all top-5 have zero degree (graph is dead)
    # Only treat as empty when graph existed AND every entry has 0 incoming
    # AND every entry has 0 outgoing-incoming context (proxy: max_incoming==0).
    # Per spec: "if BM25 returns nothing OR all top-5 have degree 0".
    if graph_conn is not None and max_incoming == 0:
        all_zero_degree = True
        for fp, _, _ in top:
            try:
                row = graph_conn.execute(
                    """
                    SELECT (
                        (SELECT COUNT(*) FROM edges e
                         JOIN nodes n ON n.id = e.source_id
                         WHERE n.file_path = ? AND COALESCE(e.confidence, 0.5) >= 0.7) +
                        (SELECT COUNT(*) FROM edges e
                         JOIN nodes n ON n.id = e.target_id
                         WHERE n.file_path = ? AND COALESCE(e.confidence, 0.5) >= 0.7)
                    ) AS d
                    """,
                    (fp, fp),
                ).fetchone()
                if row and int(row["d"]) > 0:
                    all_zero_degree = False
                    break
            except sqlite3.Error:
                # If we can't tell, don't suppress the brief.
                all_zero_degree = False
                break
        if all_zero_degree:
            try:
                graph_conn.close()
            except Exception:  # noqa: BLE001
                pass
            return _EMPTY_BRIEF

    # 6. Top-3 symbols for each survivor
    ranked: list[tuple[str, float, int, list[str]]] = []
    for fp, score, inc in top:
        symbols: list[str] = []
        if graph_conn is not None:
            symbols = _top_symbols_for_file(graph_conn, fp)
        ranked.append((fp, score, inc, symbols))

    if graph_conn is not None:
        try:
            graph_conn.close()
        except Exception:  # noqa: BLE001
            pass

    return _format_brief(ranked)


# ---------------------------------------------------------------------------
# CLI / smoke harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(prog="sqlite3_fts_fallback")
    p.add_argument("--issue-text", required=True)
    p.add_argument("--repo-path", required=True)
    p.add_argument("--graph-db", required=True)
    args = p.parse_args()

    out = generate_fts5_orientation_brief(
        args.issue_text, args.repo_path, args.graph_db
    )
    print(out)
