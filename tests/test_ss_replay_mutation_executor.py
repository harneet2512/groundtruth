from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_replay_oracle as sro  # noqa: E402
import ss_replay_toolchain as srt  # noqa: E402


_META = _REPO / "tests" / "fixtures" / "ss_replay" / "toolchains.json"
_DYNACONF = "dynaconf__dynaconf-1225"


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


def test_pinned_sed_executor_uses_exact_linux_runtime_and_confined_repo_mount(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = srt.load_replay_toolchain(_DYNACONF, _META)
    assert record is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("old\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_mutation_executor(record)
    command = "sed -i 's/old/new/' sample.py"

    assert executor(command, str(repo), 10) == (0, "", "")
    assert len(calls) == 2
    run = calls[1]
    assert run[:3] == ["docker", "run", "--rm"]
    assert run[run.index("--pull") + 1] == "never"
    assert run[run.index("--network") + 1] == "none"
    assert "--read-only" in run
    assert run[run.index("--platform") + 1] == "linux/amd64"
    assert run[run.index("--workdir") + 1] == "/testbed"
    mount = run[run.index("--mount") + 1]
    assert f"source={repo.resolve()}" in mount
    assert "target=/testbed" in mount
    assert "readonly" not in mount
    assert run[run.index("--entrypoint") + 1] == "/bin/bash"
    assert record["image"] in run
    assert run[-2:] == ["-c", command]


def test_pinned_mutation_executor_rejects_unsupported_and_unconfined_commands(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = srt.load_replay_toolchain(_DYNACONF, _META)
    assert record is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert cmd[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_mutation_executor(record)

    rc, out, err = executor("python -c 'print(1)'", str(repo), 10)
    assert rc is None and out == "" and "unsupported_command" in err
    comma_repo = tmp_path / "repo,unsafe"
    comma_repo.mkdir()
    rc, out, err = executor("sed -i 's/a/b/' sample.py", str(comma_repo), 10)
    assert rc is None and out == "" and "unsupported_target" in err
    assert len(calls) == 1  # image preflight only; neither command reached docker run


def test_pinned_mutation_executor_image_mismatch_fails_closed_without_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record = srt.load_replay_toolchain(_DYNACONF, _META)
    assert record is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = json.loads(_inspect_payload(record))
    payload["Id"] = "sha256:" + "0" * 64
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_mutation_executor(record)
    rc, out, err = executor("sed -i 's/a/b/' sample.py", str(repo), 10)

    assert rc is None and out == "" and "toolchain_unavailable" in err
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


def test_apply_edit_command_does_not_fall_back_to_host_sed_when_pinned_executor_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "sample.py"
    target.write_text("old\n", encoding="utf-8")
    calls: list[tuple[str, str, int]] = []

    def unavailable(command: str, cwd: str, timeout: int):
        calls.append((command, cwd, timeout))
        return None, "", "replay_mutation_unavailable:image_or_config_mismatch"

    monkeypatch.setattr(
        sro, "_find_bash",
        lambda: (_ for _ in ()).throw(AssertionError("host sed fallback is forbidden")),
    )
    receipt = sro.apply_edit_command(
        "sed -i 's/old/new/' sample.py", cwd=str(repo), mutation_executor=unavailable
    )

    assert len(calls) == 1
    assert calls[0][0] == "sed -i 's/old/new/' sample.py"
    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert receipt.rc is None
    assert "replay_mutation_unavailable" in receipt.reason
    assert target.read_text(encoding="utf-8") == "old\n"
