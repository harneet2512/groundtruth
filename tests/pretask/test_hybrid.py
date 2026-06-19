"""Unit tests for deterministic hybrid localization signals."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.hybrid import (
    lexical_file_search,
    reciprocal_rank_fusion,
    repository_memory_search,
)


def test_hybrid_lexical_search_uses_repo_files(
    tiny_graph_db: str, tmp_path: Path
) -> None:
    """Lexical retrieval ranks indexed source files by issue term overlap."""
    repo = tmp_path / "repo"
    (repo / "patroni").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "patroni" / "watchdog.py").write_text(
        "class SafeWatchdog:\n    def activate(self):\n        self._fd.write(b'x')\n",
        encoding="utf-8",
    )
    (repo / "patroni" / "postmaster.py").write_text(
        "class Postmaster:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_watchdog.py").write_text(
        "def test_watchdog_fires():\n    pass\n",
        encoding="utf-8",
    )
    issue = "SafeWatchdog activate fails when _fd is closed"
    anchors = extract_issue_anchors(issue, tiny_graph_db)

    hits = lexical_file_search(issue, str(repo), tiny_graph_db, anchors)
    assert hits
    assert hits[0].file == "patroni/watchdog.py"
    assert "activate" in hits[0].detail or "safewatchdog" in hits[0].detail


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=False)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_repository_memory_search_uses_commit_messages(tmp_path: Path) -> None:
    """Commit history memory ranks files touched by similar past fixes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "auth.py").write_text("def refresh_token():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "auth.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fix refresh token expiry"],
        cwd=repo,
        check=True,
    )

    anchors = extract_issue_anchors("refresh token expiry broken", None)
    hits, stats = repository_memory_search(
        "refresh token expiry broken", str(repo), anchors
    )
    assert stats["commits_examined"] >= 1
    assert stats["matching_commits"] >= 1
    assert hits[0].file == "auth.py"


def test_rrf_confidence_increases_with_signal_agreement() -> None:
    """Fusion marks candidates stronger when independent signals agree."""
    fused = reciprocal_rank_fusion(
        {
            "graph-ppr": [],
            "lexical-match": [],
            "repo-memory": [],
        }
    )
    assert fused == []

    from groundtruth.pretask.hybrid import SignalHit

    fused = reciprocal_rank_fusion(
        {
            "graph-ppr": [SignalHit("a.py", 1.0, "node")],
            "lexical-match": [SignalHit("a.py", 1.0, "term")],
            "repo-memory": [SignalHit("a.py", 1.0, "commit")],
        }
    )
    assert fused[0].file == "a.py"
    assert fused[0].confidence == "high"
