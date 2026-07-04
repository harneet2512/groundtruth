"""Unit tests for v7 git co-change clustering."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.cochange import cochange_cluster, cochange_telemetry


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=False)
        return True
    except OSError:
        return False


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_cochange_cluster_finds_historically_paired_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)

    _commit(repo, "initial", {"src/a.py": "a = 1\n"})
    _commit(
        repo,
        "fix auth flow",
        {
            "src/a.py": "a = 2\n",
            "src/b.py": "b = 2\n",
            "vendor/generated.py": "x = 1\n",
        },
    )

    result = cochange_cluster(str(repo), ["src/a.py"], max_files=5)
    files = [hit.file for hit in result.hits]

    assert result.commits_examined >= 2
    assert result.commits_with_primary >= 1
    assert files[0] == "src/a.py"
    assert "src/b.py" in files
    assert "vendor/generated.py" not in files

    telemetry = cochange_telemetry(result, ["src/a.py"], wall_ms=7)
    assert telemetry["enabled"] is True
    assert telemetry["wall_ms"] == 7
    assert telemetry["cluster_files"]


def test_cochange_cluster_abstains_without_git_history(tmp_path: Path) -> None:
    result = cochange_cluster(str(tmp_path), ["src/a.py"])
    assert result.hits == []
    assert result.abstain_reason == "no_git_history"
