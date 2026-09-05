"""Deterministic hybrid localization signals for the pre-task brief.

This module intentionally stays simple and offline: no embeddings, no HTTP,
no model calls. It adds two signals that v5 was missing:

* lexical file retrieval over indexed repo files (BM25-style scoring);
* git-history memory from commit messages, touched files, and co-change.

The orchestrator fuses these ranked lists with graph PPR via reciprocal rank
fusion so weak single-signal guesses do not dominate the brief.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.traces import StackFrame


@dataclass(frozen=True)
class SignalHit:
    """One file hit emitted by one deterministic localization signal."""

    file: str
    score: float
    detail: str = ""


@dataclass
class FusedHit:
    """Final file-level score after rank fusion."""

    file: str
    score: float
    signals: list[tuple[str, str]] = field(default_factory=list)
    confidence: str = "low"


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SKIP_DIR_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
}
_SOURCE_EXTS = {
    # Source code
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cc",
    ".hh",
    ".cpp",
    ".hpp",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".scala",
    ".sh",
    # Config/data (enables GT to find config-file bugs)
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".dockerfile",
    ".tf",
    ".hcl",
    # Docs that might contain fix targets
    ".md",
    ".rst",
    ".txt",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "when",
    "where",
    "should",
    "could",
    "would",
    "error",
    "issue",
    "bug",
    "fail",
    "fails",
    "failed",
    "failure",
    "test",
    "tests",
    "file",
    "files",
    "function",
    "class",
    "method",
    "value",
    "result",
    "expected",
    "actual",
}


def query_terms(issue_text: str, anchors: IssueAnchors) -> list[str]:
    """Return stable query terms for lexical and memory retrieval."""
    terms: list[str] = []
    seen: set[str] = set()
    for source in (anchors.symbols, anchors.symbols_raw):
        for token in sorted(source):
            for part in token.replace(".", "_").split("_"):
                low = part.lower()
                if len(low) >= 3 and low not in _STOPWORDS and low not in seen:
                    seen.add(low)
                    terms.append(low)
    for match in _WORD_RE.finditer(issue_text or ""):
        low = match.group(0).lower()
        if len(low) < 4 or low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(low)
        if len(terms) >= 80:
            break
    return terms


def graph_file_paths(graph_db: str | None) -> list[str]:
    """List indexed file paths from graph.db."""
    if not graph_db or not os.path.exists(graph_db):
        return []
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL AND is_test = 0"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return sorted({str(r[0]) for r in rows if r and r[0]})


def _safe_source_file(repo_root: str, rel_path: str) -> Path | None:
    norm = rel_path.replace("\\", "/").lstrip("/")
    if not norm or any(part in _SKIP_DIR_PARTS for part in norm.split("/")):
        return None
    path = Path(repo_root) / norm
    if path.suffix.lower() not in _SOURCE_EXTS:
        return None
    try:
        resolved = path.resolve()
        root = Path(repo_root).resolve()
        if root not in resolved.parents and resolved != root:
            return None
    except OSError:
        return None
    return path


def _walk_text_files(repo_root: str, max_files: int = 2000) -> list[str]:
    """Walk repo for text files matching _SOURCE_EXTS (includes config/docs)."""
    results: list[str] = []
    root_path = Path(repo_root)
    try:
        for dirpath, dirnames, filenames in os.walk(repo_root):
            # Prune skipped directories in-place
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_PARTS]
            for fname in filenames:
                if Path(fname).suffix.lower() in _SOURCE_EXTS:
                    full = Path(dirpath) / fname
                    try:
                        rel = full.relative_to(root_path).as_posix()
                        results.append(rel)
                    except (ValueError, OSError):
                        pass
                if len(results) >= max_files:
                    return results
    except OSError:
        pass
    return results


def lexical_file_search(
    issue_text: str,
    repo_root: str,
    graph_db: str | None,
    anchors: IssueAnchors,
    *,
    max_files: int = 30,
    max_bytes: int = 400_000,
) -> list[SignalHit]:
    """Rank indexed files by deterministic BM25-style lexical overlap."""
    terms = query_terms(issue_text, anchors)
    if not terms or not repo_root:
        return []
    files = graph_file_paths(graph_db)
    # Also walk filesystem for text files not in graph.db (config, docs, etc.)
    walked = _walk_text_files(repo_root)
    graph_set = set(files)
    for wf in walked:
        if wf not in graph_set:
            files.append(wf)
    if not files:
        return []

    term_set = set(terms)
    docs: list[tuple[str, Counter[str], int]] = []
    df: Counter[str] = Counter()
    for rel_path in files:
        path = _safe_source_file(repo_root, rel_path)
        if path is None or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:max_bytes]
        except OSError:
            continue
        counts = Counter(
            tok.lower()
            for tok in _WORD_RE.findall(text)
            if tok.lower() in term_set
        )
        if not counts:
            continue
        for term in counts:
            df[term] += 1
        docs.append((rel_path, counts, max(1, len(text) // 6)))

    if not docs:
        return []

    n_docs = len(docs)
    avg_len = sum(length for _path, _counts, length in docs) / n_docs
    ranked: list[SignalHit] = []
    for rel_path, counts, length in docs:
        score = 0.0
        matched: list[str] = []
        for term, tf in counts.items():
            idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf + 1.2 * (1.0 - 0.75 + 0.75 * length / avg_len)
            score += idf * (tf * 2.2 / denom)
            matched.append(term)
        if score > 0.0:
            detail = ", ".join(sorted(matched)[:4])
            ranked.append(SignalHit(file=rel_path, score=score, detail=detail))

    ranked.sort(key=lambda hit: hit.score, reverse=True)
    return ranked[:max_files]


def _parse_git_history(repo_root: str, max_commits: int) -> list[tuple[str, str, list[str]]]:
    if not repo_root:
        return []
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "log",
                f"-n{max_commits}",
                "--name-only",
                "--pretty=format:__GT_COMMIT__%H%x01%s",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    commits: list[tuple[str, str, list[str]]] = []
    cur_hash = ""
    cur_msg = ""
    cur_files: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("__GT_COMMIT__"):
            if cur_hash:
                commits.append((cur_hash, cur_msg, cur_files))
            payload = line[len("__GT_COMMIT__") :]
            if "\x01" in payload:
                cur_hash, cur_msg = payload.split("\x01", 1)
            else:
                cur_hash, cur_msg = payload, ""
            cur_files = []
            continue
        if Path(line).suffix.lower() in _SOURCE_EXTS:
            cur_files.append(line.replace("\\", "/"))
    if cur_hash:
        commits.append((cur_hash, cur_msg, cur_files))
    return commits


def repository_memory_search(
    issue_text: str,
    repo_root: str,
    anchors: IssueAnchors,
    *,
    max_commits: int = 700,
    max_files: int = 30,
) -> tuple[list[SignalHit], dict[str, int]]:
    """Rank files by similar historical commits and co-change memory."""
    terms = set(query_terms(issue_text, anchors))
    commits = _parse_git_history(repo_root, max_commits)
    if not terms or not commits:
        return [], {"commits_examined": len(commits), "matching_commits": 0}

    explicit_paths = {p.replace("\\", "/") for p in anchors.paths}
    scores: defaultdict[str, float] = defaultdict(float)
    details: defaultdict[str, list[str]] = defaultdict(list)
    matching = 0

    for idx, (commit, message, files) in enumerate(commits):
        if not files:
            continue
        msg_terms = {t.lower() for t in _WORD_RE.findall(message)}
        overlap = terms & msg_terms
        cochanged = explicit_paths & set(files)
        if not overlap and not cochanged:
            continue
        matching += 1
        decay = 1.0 / math.log(idx + 3)
        base = (len(overlap) + 1.5 * len(cochanged)) * decay
        for file_path in files:
            scores[file_path] += base
            if len(details[file_path]) < 3:
                reason = ",".join(sorted(overlap)[:3]) if overlap else "cochange"
                details[file_path].append(f"{commit[:7]}:{reason}")

    ranked = [
        SignalHit(file=f, score=s, detail="; ".join(details[f]))
        for f, s in scores.items()
        if s > 0.0
    ]
    ranked.sort(key=lambda hit: hit.score, reverse=True)
    return ranked[:max_files], {
        "commits_examined": len(commits),
        "matching_commits": matching,
    }


def direct_path_hits(paths: Iterable[str]) -> list[SignalHit]:
    """Give explicit issue path mentions their own ranked list."""
    hits: list[SignalHit] = []
    for idx, path in enumerate(sorted({p.replace("\\", "/") for p in paths if p})):
        hits.append(SignalHit(file=path, score=1.0 / (idx + 1), detail="path-mention"))
    return hits


def stack_frame_hits(frames: Iterable[StackFrame]) -> list[SignalHit]:
    """Give parsed stack frames their own ranked list."""
    hits: list[SignalHit] = []
    seen: set[str] = set()
    for idx, fr in enumerate(frames):
        file_path = fr.file.replace("\\", "/")
        if file_path in seen:
            continue
        seen.add(file_path)
        hits.append(SignalHit(file=file_path, score=1.0 / (idx + 1), detail=f"line {fr.line}"))
    return hits


def ppr_hits(file_scores: dict[str, tuple[float, int]]) -> list[SignalHit]:
    """Convert PPR aggregate scores to a ranked signal list."""
    ranked = [
        SignalHit(file=f, score=score, detail=f"{n} nodes")
        for f, (score, n) in file_scores.items()
        if score > 0.0
    ]
    ranked.sort(key=lambda hit: hit.score, reverse=True)
    return ranked


def reciprocal_rank_fusion(
    signal_lists: dict[str, list[SignalHit]],
    *,
    k: int = 60,
    max_files: int = 50,
) -> list[FusedHit]:
    """Fuse multiple ranked file lists with deterministic RRF."""
    scores: defaultdict[str, float] = defaultdict(float)
    signals: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)

    for signal_name, hits in signal_lists.items():
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            if not hit.file or hit.file in seen:
                continue
            seen.add(hit.file)
            scores[hit.file] += 1.0 / (k + rank)
            signals[hit.file].append((signal_name, hit.detail))

    if not scores:
        return []

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best = ordered[0][1]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    gap = best - second

    out: list[FusedHit] = []
    for file_path, score in ordered[:max_files]:
        n_signals = len({name for name, _detail in signals[file_path]})
        ratio = score / best if best > 0 else 0.0
        if n_signals >= 3 or (n_signals >= 2 and ratio >= 0.85 and gap >= 0.002):
            conf = "high"
        elif n_signals >= 2 or ratio >= 0.75:
            conf = "medium"
        else:
            conf = "low"
        out.append(
            FusedHit(
                file=file_path,
                score=score,
                signals=signals[file_path],
                confidence=conf,
            )
        )
    return out
