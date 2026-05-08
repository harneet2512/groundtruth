"""Deterministic git co-change clusters for v7 edit planning."""

from __future__ import annotations

import math
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CochangeHit:
    """One file historically changed with a primary candidate."""

    file: str
    score: float
    count: int
    primaries: tuple[str, ...] = field(default_factory=tuple)
    commits: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CochangeResult:
    """Co-change cluster plus auditable counters."""

    hits: list[CochangeHit]
    commits_examined: int
    commits_with_primary: int
    rejected_files: tuple[str, ...] = field(default_factory=tuple)
    abstain_reason: str = ""


_SOURCE_EXTS = {
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
    ".rst",
    ".md",
}
_REJECT_PARTS = {
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
_REJECT_SUFFIXES = {".lock", ".min.js", ".map"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _is_candidate_file(path: str) -> bool:
    norm = _norm(path)
    if not norm or any(part in _REJECT_PARTS for part in norm.split("/")):
        return False
    if any(norm.endswith(suffix) for suffix in _REJECT_SUFFIXES):
        return False
    return Path(norm).suffix.lower() in _SOURCE_EXTS


def _git_commits(repo_root: str, max_commits: int) -> list[tuple[str, str, list[str]]]:
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
    cur_subject = ""
    cur_files: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("__GT_COMMIT__"):
            if cur_hash:
                commits.append((cur_hash, cur_subject, cur_files))
            payload = line[len("__GT_COMMIT__") :]
            if "\x01" in payload:
                cur_hash, cur_subject = payload.split("\x01", 1)
            else:
                cur_hash, cur_subject = payload, ""
            cur_files = []
            continue
        cur_files.append(_norm(line))
    if cur_hash:
        commits.append((cur_hash, cur_subject, cur_files))
    return commits


def cochange_cluster(
    repo_root: str,
    primary_files: list[str],
    *,
    max_commits: int = 800,
    max_files: int = 8,
) -> CochangeResult:
    """Return files that historically change with ``primary_files``.

    Scores are recency-decayed co-occurrence counts. The primary files are
    included in the output so renderers can show one complete edit cluster.
    """
    primaries = [_norm(p) for p in primary_files if p]
    primary_set = set(primaries)
    if not repo_root or not primary_set:
        return CochangeResult([], 0, 0, abstain_reason="no_primary_files")

    commits = _git_commits(repo_root, max_commits)
    if not commits:
        return CochangeResult([], 0, 0, abstain_reason="no_git_history")

    scores: defaultdict[str, float] = defaultdict(float)
    counts: defaultdict[str, int] = defaultdict(int)
    seen_primaries: defaultdict[str, set[str]] = defaultdict(set)
    hit_commits: defaultdict[str, list[str]] = defaultdict(list)
    rejected: set[str] = set()
    commits_with_primary = 0

    for idx, (commit, _subject, files) in enumerate(commits):
        clean_files: list[str] = []
        for file_path in files:
            if _is_candidate_file(file_path):
                clean_files.append(file_path)
            elif file_path:
                rejected.add(file_path)
        touched_primary = primary_set & set(clean_files)
        if not touched_primary:
            continue
        commits_with_primary += 1
        decay = 1.0 / math.log(idx + 3)
        for file_path in clean_files:
            scores[file_path] += decay
            counts[file_path] += 1
            seen_primaries[file_path].update(touched_primary)
            if len(hit_commits[file_path]) < 3:
                hit_commits[file_path].append(commit[:7])

    for primary in primaries:
        if _is_candidate_file(primary):
            scores[primary] += 2.0
            counts[primary] = max(1, counts[primary])
            seen_primaries[primary].add(primary)

    hits = [
        CochangeHit(
            file=file_path,
            score=score,
            count=counts[file_path],
            primaries=tuple(sorted(seen_primaries[file_path])),
            commits=tuple(hit_commits[file_path]),
        )
        for file_path, score in scores.items()
        if score > 0.0
    ]
    hits.sort(
        key=lambda hit: (
            hit.file not in primary_set,
            -hit.score,
            hit.file,
        )
    )
    return CochangeResult(
        hits=hits[:max_files],
        commits_examined=len(commits),
        commits_with_primary=commits_with_primary,
        rejected_files=tuple(sorted(rejected)[:20]),
        abstain_reason="" if hits else "no_cochange_hits",
    )


def cochange_telemetry(
    result: CochangeResult,
    primary_files: list[str],
    wall_ms: int,
) -> dict[str, object]:
    """Convert a co-change result to the module_7_cochange telemetry block."""
    return {
        "wall_ms": wall_ms,
        "enabled": True,
        "primary_files": [_norm(p) for p in primary_files if p],
        "commits_examined": result.commits_examined,
        "commits_with_primary": result.commits_with_primary,
        "cluster_files": [
            {
                "file": hit.file,
                "score": round(hit.score, 6),
                "count": hit.count,
                "primaries": list(hit.primaries),
                "commits": list(hit.commits),
            }
            for hit in result.hits
        ],
        "rejected_files": list(result.rejected_files),
        "abstain_reason": result.abstain_reason,
    }
