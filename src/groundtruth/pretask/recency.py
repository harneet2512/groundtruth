"""Module 4 — Recent-commit weighting.

Runs ``git log --since=<N>days --name-only --pretty=format:`` against the
target repo and converts file-mention counts into a normalized [0, 1]
boost map. Files never touched in the window get weight ``0.0``.

Failure modes handled:
    - ``repo_root`` is not a git repo → returns ``{}``.
    - Detached HEAD or shallow clone → ``git log`` still works; output is
      whatever HEAD reaches, no crash.
    - Subprocess timeout → ``{}``.

The boost is applied multiplicatively by the orchestrator (max 1.5x), so
this module does NOT do the multiplication itself — it only returns the
normalized weights.
"""

from __future__ import annotations

import math
import subprocess
from collections import Counter


def recent_commit_weight(
    repo_root: str,
    days: int = 30,
    timeout_sec: float = 10.0,
) -> tuple[dict[str, float], int]:
    """Return per-file weight map and raw commit-line count.

    Args:
        repo_root: Filesystem path to the repository.
        days: Window for the ``--since`` filter.
        timeout_sec: Max wall time for the subprocess.

    Returns:
        Tuple of (weight_map, total_commit_lines).
        weight_map: ``{file_path: log(commit_count + 1) / log(max_count + 1)}``.
        total_commit_lines: sum of all per-file mention counts (i.e. raw
        ``--name-only`` line count in the window). 0 if non-git or empty.
    """
    if not repo_root or days <= 0:
        return {}, 0

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "log",
                f"--since={days}.days",
                "--name-only",
                "--pretty=format:",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}, 0

    if proc.returncode != 0:
        return {}, 0

    counts: Counter[str] = Counter()
    for line in (proc.stdout or "").splitlines():
        path = line.strip()
        if not path:
            continue
        counts[path] += 1

    if not counts:
        return {}, 0

    total = sum(counts.values())
    max_log = math.log(max(counts.values()) + 1)
    if max_log == 0.0:
        return {}, total

    return {path: math.log(c + 1) / max_log for path, c in counts.items()}, total
