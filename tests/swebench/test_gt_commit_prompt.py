"""Tests for gt_commit_prompt.py — the TEG-1 commit-prompt hook.

Test strategy: we use a real git repo in tmp_path so `git status --porcelain`
runs cleanly. We DO NOT mock the subprocess — flaky-shaped behavior is the
risk we're testing against, so the test wires up real git state.

State and gold-paths are fed via real files; output is captured via io.StringIO.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

import gt_commit_prompt as hook


def _git(*args: str, cwd: Path) -> None:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with two seed files. Repo is in tmp_path/repo so
    auxiliary fixture files (gold_paths, state, log) live outside the repo
    and don't show up as untracked in `git status`."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git("init", "-q", cwd=repo_dir)
    _git("config", "commit.gpgsign", "false", cwd=repo_dir)
    _git("config", "core.autocrlf", "false", cwd=repo_dir)
    _git("config", "core.filemode", "false", cwd=repo_dir)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (repo_dir / "src" / "util.py").write_text("def util(): return 1\n", encoding="utf-8")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_main.py").write_text("def test_main(): pass\n", encoding="utf-8")
    _git("add", ".", cwd=repo_dir)
    _git("commit", "-q", "-m", "init", cwd=repo_dir)
    return repo_dir


@pytest.fixture
def aux_dir(tmp_path: Path) -> Path:
    """Sibling directory for state/log/gold-paths files — outside the git repo
    so they don't pollute `git status` in the workspace."""
    d = tmp_path / "aux"
    d.mkdir()
    return d


@pytest.fixture
def gold_paths_file(aux_dir: Path) -> Path:
    p = aux_dir / "gold_paths.txt"
    p.write_text("src/main.py\nsrc/util.py\n", encoding="utf-8")
    return p


@pytest.fixture
def state_file(aux_dir: Path) -> Path:
    return aux_dir / "state.json"


@pytest.fixture
def log_file(aux_dir: Path) -> Path:
    return aux_dir / "log.jsonl"


def _run(repo: Path, gold: Path, state: Path, log: Path, threshold: int = 4) -> tuple[int, str]:
    out = io.StringIO()
    rc = hook.run(
        root=str(repo),
        gold_paths_file=str(gold),
        state_file=str(state),
        log_file=str(log),
        threshold=threshold,
        out_stream=out,
    )
    return rc, out.getvalue()


def _read_state(state: Path) -> dict[str, Any]:
    return json.loads(state.read_text(encoding="utf-8"))


# ---------- Core fire-condition tests ----------


def test_does_not_fire_below_threshold(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """3 reads is below threshold=4, must not fire."""
    for _ in range(3):
        rc, out = _run(repo, gold_paths_file, state_file, log_file, threshold=4)
        assert rc == 0
        assert out == ""
    s = _read_state(state_file)
    assert s["reads_seen"] == 3
    assert s["has_fired"] is False


def test_fires_when_threshold_met_with_gold_read(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """4 reads + at least one gold file accessed => fire."""
    # Touch the gold file to update atime (simulate the agent reading it).
    (repo / "src" / "main.py").read_bytes()

    fired = False
    for i in range(4):
        # Touch gold file again at iteration 1 to ensure atime updates within window
        if i == 1:
            (repo / "src" / "main.py").read_bytes()
        rc, out = _run(repo, gold_paths_file, state_file, log_file, threshold=4)
        assert rc == 0
        if "<gt-commit-prompt>" in out:
            fired = True
    assert fired, "expected hook to fire by 4th invocation"
    s = _read_state(state_file)
    assert s["has_fired"] is True


def test_does_not_fire_after_gold_edit(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """If agent edited a gold file already, the prompt is unnecessary."""
    # Simulate: agent already edited src/main.py before any tool call.
    (repo / "src" / "main.py").write_text("def main(): return 42\n", encoding="utf-8")
    for _ in range(6):
        rc, out = _run(repo, gold_paths_file, state_file, log_file, threshold=4)
        assert "<gt-commit-prompt>" not in out
    s = _read_state(state_file)
    assert s["has_fired"] is False
    assert "src/main.py" in s["gold_files_edited"]


def test_fires_only_once_per_task(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """Once fired, subsequent invocations are no-ops."""
    (repo / "src" / "main.py").read_bytes()
    fire_count = 0
    for _ in range(10):
        rc, out = _run(repo, gold_paths_file, state_file, log_file, threshold=4)
        if "<gt-commit-prompt>" in out:
            fire_count += 1
    assert fire_count == 1, f"expected exactly 1 fire, got {fire_count}"


def test_does_not_fire_with_empty_gold_paths(repo: Path, aux_dir: Path, state_file: Path, log_file: Path) -> None:
    """If the V1R-map brief was empty (no gold paths), the hook should never fire."""
    empty = aux_dir / "empty_gold.txt"
    empty.write_text("", encoding="utf-8")
    for _ in range(20):
        rc, out = _run(repo, empty, state_file, log_file, threshold=4)
        assert "<gt-commit-prompt>" not in out


def test_does_not_fire_with_missing_gold_paths_file(repo: Path, aux_dir: Path, state_file: Path, log_file: Path) -> None:
    """Missing gold_paths.txt is treated as 'no candidates', no fire."""
    missing = aux_dir / "does_not_exist.txt"
    for _ in range(20):
        rc, out = _run(repo, missing, state_file, log_file, threshold=4)
        assert "<gt-commit-prompt>" not in out


# ---------- Edit detection ----------


def test_edit_detected_via_git_status(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """Agent writes a non-gold file; hook should classify as edit, not read."""
    rc, _ = _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert s["reads_seen"] == 1
    assert s["edits_seen"] == 0

    (repo / "tests" / "test_main.py").write_text("def test_main(): assert True\n", encoding="utf-8")
    rc, _ = _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert s["edits_seen"] == 1
    assert s["reads_seen"] == 1
    assert "tests/test_main.py" in s["files_modified_seen"]


def test_gold_edit_classified_separately(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """Edit to a gold path increments gold_files_edited."""
    (repo / "src" / "util.py").write_text("def util(): return 99\n", encoding="utf-8")
    _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert "src/util.py" in s["gold_files_edited"]
    assert s["edits_seen"] == 1


def test_repeated_edits_to_same_file_count_once_in_modified_set(
    repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path
) -> None:
    """Files in `git status` track distinct paths, not edit count."""
    (repo / "tests" / "test_main.py").write_text("v1\n", encoding="utf-8")
    _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert s["edits_seen"] == 1

    (repo / "tests" / "test_main.py").write_text("v2\n", encoding="utf-8")
    _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    # Same file already in modified set — no new edits counted, this is a "read" tick.
    assert s["edits_seen"] == 1
    assert len(s["files_modified_seen"]) == 1


# ---------- Path-matching robustness ----------


def test_is_gold_path_exact() -> None:
    assert hook._is_gold_path("src/main.py", ["src/main.py"]) is True


def test_is_gold_path_tail_match() -> None:
    """Brief says `pdm/auth.py`, agent edits `src/pdm/auth.py` — matches."""
    assert hook._is_gold_path("src/pdm/auth.py", ["pdm/auth.py"]) is True


def test_is_gold_path_no_false_positive_on_basename_collision() -> None:
    """Brief lists `src/main.py`, agent edits `tests/main.py` — must NOT match."""
    assert hook._is_gold_path("tests/main.py", ["src/main.py"]) is False


def test_is_gold_path_normalizes_backslashes() -> None:
    assert hook._is_gold_path("src\\main.py", ["src/main.py"]) is True


def test_is_gold_path_handles_empty_list() -> None:
    assert hook._is_gold_path("anything.py", []) is False


# ---------- Persistence ----------


def test_state_persists_across_invocations(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    for _ in range(2):
        _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert s["reads_seen"] == 2

    _run(repo, gold_paths_file, state_file, log_file)
    s = _read_state(state_file)
    assert s["reads_seen"] == 3


def test_corrupt_state_file_recovers(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    state_file.write_text("not valid json{", encoding="utf-8")
    rc, out = _run(repo, gold_paths_file, state_file, log_file)
    assert rc == 0
    s = _read_state(state_file)
    assert s["reads_seen"] == 1
    assert s["has_fired"] is False


# ---------- Failure modes ----------


def test_no_git_repo_does_not_crash(aux_dir: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """If /workspace is not a git repo, hook treats every call as a read and never fires gold-edit detection."""
    not_a_repo = aux_dir / "no_git"
    not_a_repo.mkdir()
    (not_a_repo / "src").mkdir()
    (not_a_repo / "src" / "main.py").write_text("x", encoding="utf-8")

    rc, _ = _run(not_a_repo, gold_paths_file, state_file, log_file)
    assert rc == 0
    s = _read_state(state_file)
    assert s["reads_seen"] == 1
    assert s["edits_seen"] == 0


def test_missing_workspace_root_does_not_crash(aux_dir: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    rc, _ = _run(aux_dir / "does_not_exist", gold_paths_file, state_file, log_file)
    assert rc == 0


# ---------- Threshold tunability ----------


def test_threshold_respected(repo: Path, gold_paths_file: Path, state_file: Path, log_file: Path) -> None:
    """threshold=2 fires earlier."""
    (repo / "src" / "main.py").read_bytes()
    fire_iter = None
    for i in range(5):
        if i == 0:
            (repo / "src" / "main.py").read_bytes()
        rc, out = _run(repo, gold_paths_file, state_file, log_file, threshold=2)
        if "<gt-commit-prompt>" in out and fire_iter is None:
            fire_iter = i
    assert fire_iter is not None
    assert fire_iter <= 2, f"with threshold=2, expected fire by iter 2, got {fire_iter}"
