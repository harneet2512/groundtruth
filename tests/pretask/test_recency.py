"""Stage A unit tests for Module 4 (recency)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.recency import recent_commit_weight


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_recency_zero_for_unseen(tmp_path: Path) -> None:
    """A repo with one commit on file A: file B has weight 0 (i.e. absent)."""
    # init repo
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True
    )
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "first"], cwd=tmp_path, check=True
    )

    weights, total = recent_commit_weight(str(tmp_path), days=30)
    # a.py should be present, b.py absent.
    assert "a.py" in weights
    assert weights["a.py"] > 0.0
    assert "b.py" not in weights
    assert total >= 1


def test_recency_non_git_returns_empty(tmp_path: Path) -> None:
    """A directory that is not a git repo returns ``({}, 0)`` (no crash)."""
    out = recent_commit_weight(str(tmp_path), days=30)
    assert out == ({}, 0)


def test_recency_empty_input_returns_empty(tmp_path: Path) -> None:
    """Empty repo_root or non-positive days returns ``({}, 0)``."""
    assert recent_commit_weight("", days=30) == ({}, 0)
    assert recent_commit_weight(str(tmp_path), days=0) == ({}, 0)
