from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_replay_toolchain as srt  # noqa: E402


_META = _REPO / "tests" / "fixtures" / "ss_replay" / "toolchains.json"
_GEO = "geopandas__geopandas-3471"


def _inspect_payload(record: dict[str, str]) -> str:
    return json.dumps({
        "Id": "sha256:" + record["image_config_sha256"],
        "RepoDigests": [record["image"]],
        "Config": {
            "WorkingDir": record["repo_root"],
            "Env": [
                f"PYTHON_VERSION={record['python_version']}",
                f"PYTHON_SHA256={record['python_env_sha256']}",
            ],
        },
        "Architecture": "amd64",
        "Os": "linux",
    })


def _workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    test_file = repo / "geopandas" / "tools" / "tests" / "test_random.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    return repo


def test_verification_executor_runs_covering_argv_in_pinned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The unit runner sees the edited checkout, never a host pytest fallback."""
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    repo = _workspace(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")
        return subprocess.CompletedProcess(cmd, 1, "1 failed\n", "native stderr")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_verification_executor(record, repo)
    assert executor is not None

    command = ["pytest", "geopandas/tools/tests/test_random.py"]
    assert executor(command, record["repo_root"], 20) == (
        1, "1 failed\n", "native stderr")

    run = calls[1]
    assert run[:5] == ["docker", "run", "--rm", "--pull", "never"]
    assert run[run.index("--network") + 1] == "none"
    assert "--read-only" in run
    assert run[run.index("--platform") + 1] == record["platform"]
    assert run[run.index("--workdir") + 1] == record["repo_root"]
    mount = run[run.index("--mount") + 1]
    assert f"source={repo.resolve()}" in mount
    assert f"target={record['repo_root']}" in mount
    assert "readonly" not in mount.lower(), "the edited repo bind must be RW"
    assert run[-2:] == command
    assert record["image"] in run


def test_verification_executor_rejects_unowned_commands_and_unconfined_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    repo = _workspace(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_verification_executor(record, repo)
    assert executor is not None

    rc, out, err = executor(
        ["python", "-c", "__import__('os').system('echo unowned')"],
        record["repo_root"], 10)
    assert rc is None and out == "" and "unsupported_command" in err

    rc, out, err = executor(
        ["pytest", "geopandas/tools/tests/test_random.py"],
        "/outside", 10)
    assert rc is None and out == "" and "unsupported_cwd" in err
    assert len(calls) == 1, "only immutable-image inspection may execute"


def test_verification_executor_admits_only_exact_single_file_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Pytest options or a second file are outside the bounded replay contract."""
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    repo = _workspace(tmp_path)
    second = repo / "geopandas" / "tools" / "tests" / "test_second.py"
    second.write_text("def test_second():\n    assert True\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_verification_executor(record, repo)
    assert executor is not None

    for command in (
        ["pytest", "geopandas/tools/tests/test_random.py", "--rootdir=/"],
        ["pytest", "geopandas/tools/tests/test_random.py",
         "geopandas/tools/tests/test_second.py"],
    ):
        rc, out, err = executor(command, record["repo_root"], 20)
        assert rc is None and out == ""
        assert "unsupported_command" in err

    assert len(calls) == 1, "rejected argv must stop after immutable-image inspection"


def test_verification_executor_image_mismatch_fails_closed_without_docker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    repo = _workspace(tmp_path)
    payload = json.loads(_inspect_payload(record))
    payload["Id"] = "sha256:" + "0" * 64
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)

    assert srt.build_replay_verification_executor(record, repo) is None
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


class _FakeSeam:
    @staticmethod
    def _build_env_executor():
        return "live-env-executor"

    @staticmethod
    def _build_edit_check_executor():
        return "parse-only-executor"

    @staticmethod
    def _build_verification_executor():
        return "uninstalled-verification-executor"


def test_verification_installer_is_separate_and_enables_only_ready_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _workspace(tmp_path)
    sentinel = object()
    monkeypatch.setattr(
        srt, "build_replay_verification_executor",
        lambda _record, _workspace: sentinel,
    )
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "0")
    seam = _FakeSeam()
    live_factory = seam._build_env_executor
    edit_factory = seam._build_edit_check_executor

    ready = srt.install_replay_verification_executor(seam, _GEO, _META, repo)

    assert ready is True
    assert os.environ["GT_VERIFY_EXECUTE"] == "1"
    assert seam._build_env_executor == live_factory
    assert seam._build_edit_check_executor == edit_factory
    assert seam._build_verification_executor() is sentinel


def test_verification_installer_keeps_flag_off_when_metadata_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _workspace(tmp_path)

    def unavailable(record, _workspace):
        assert record is None
        return None

    monkeypatch.setattr(
        srt, "build_replay_verification_executor",
        unavailable,
    )
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    seam = _FakeSeam()
    live_factory = seam._build_env_executor
    edit_factory = seam._build_edit_check_executor

    ready = srt.install_replay_verification_executor(
        seam, _GEO, tmp_path / "missing-toolchains.json", repo)

    assert ready is False
    assert os.environ["GT_VERIFY_EXECUTE"] == "0"
    assert seam._build_env_executor == live_factory
    assert seam._build_edit_check_executor == edit_factory
    assert seam._build_verification_executor() is None
