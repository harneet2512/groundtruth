"""RED-first coverage for replay snapshot reset-before-setup (WIDE-10)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.swebench import ss_replay_oracle as oracle


def _git_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Replay Test"], check=True)
    (root / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True)
    return root


def test_reset_snapshot_cleans_crash_dirt_before_setup(tmp_path: Path) -> None:
    """MUTATION W10-A: omitting pre-setup reset replays crash residue."""
    snapshot = _git_snapshot(tmp_path)
    (snapshot / "tracked.txt").write_text("crash residue", encoding="utf-8")
    (snapshot / "untracked.py").write_text("left by crashed replay", encoding="utf-8")

    oracle.TaskMirrors("task", tmp_path, tmp_path)._reset_snapshot()

    assert (snapshot / "tracked.txt").read_text(encoding="utf-8") == "base"
    assert not (snapshot / "untracked.py").exists()
    status = subprocess.run(
        ["git", "-C", str(snapshot), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    assert status.stdout == ""


def test_reset_snapshot_fails_closed_when_checkout_fails(tmp_path: Path, monkeypatch) -> None:
    """MUTATION W10-B: ignoring reset errors would proceed on an unknown snapshot."""
    _git_snapshot(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(cmd)
        return 1

    monkeypatch.setattr(oracle, "_run_quiet", fake_run)
    with pytest.raises(oracle.SeamReplayBlocked, match="reset checkout failed"):
        oracle.TaskMirrors("task", tmp_path, tmp_path)._reset_snapshot()
    assert calls and calls[0][-3:] == ["checkout", "--", "."]


def test_setup_resets_before_mounting_mirror(tmp_path: Path, monkeypatch) -> None:
    """The setup seam must reset before it can expose /testbed to the child."""
    _git_snapshot(tmp_path)
    events: list[str] = []
    mirrors = oracle.TaskMirrors("task", tmp_path, tmp_path)
    monkeypatch.setattr(mirrors, "_reset_snapshot", lambda: events.append("reset"))
    monkeypatch.setattr(oracle, "_make_junction", lambda *_args: events.append("mount") or False)
    with pytest.raises(oracle.SeamReplayBlocked, match="could not junction"):
        mirrors.setup()
    assert events == ["reset", "mount"]
