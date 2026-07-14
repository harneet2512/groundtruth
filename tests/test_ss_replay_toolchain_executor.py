from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_replay_toolchain as srt  # noqa: E402


_META = _REPO / "tests" / "fixtures" / "ss_replay" / "toolchains.json"
_GEO = "geopandas__geopandas-3471"


class _FakeSeam:
    @staticmethod
    def _build_env_executor():
        return "unrelated-executor"

    @staticmethod
    def _build_edit_check_executor():
        raise AssertionError("edit-check executor must be replaced")


def _inspect_payload(record: dict[str, str], *, containerd: bool = False) -> str:
    digest = record["image"].rsplit("@sha256:", 1)[1]
    return json.dumps({
        "Id": f"sha256:{digest if containerd else record['image_config_sha256']}",
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


def test_replay_toolchain_metadata_covers_every_recorded_edit_syntax_runtime():
    records = json.loads(_META.read_text(encoding="utf-8"))["tasks"]
    cases = json.loads(
        (_META.parent / "cases.json").read_text(encoding="utf-8"))
    expected = set()
    for value in cases.values():
        if not isinstance(value, list):
            continue
        for case in value:
            if not isinstance(case, dict) or not case.get("task"):
                continue
            labels = case.get("deliveries") or [case.get("delivery", "")]
            if any(str(label).startswith("edit.syntax") for label in labels):
                expected.add(case["task"])
    assert expected
    assert set(records) == expected
    recorded_root = Path("D:/gt_runs/29236533134/art")
    if recorded_root.is_dir():
        observed = set()
        for ledger in recorded_root.glob("*/gt_runtime_ledger_*.jsonl"):
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            if any(row.get("layer") == "edit.syntax"
                   and row.get("outcome") == "delivered"
                   and int(row.get("chars_delivered") or 0) > 0 for row in rows):
                observed.add(ledger.parent.name)
        assert observed == expected
    for task in records:
        record = srt.load_replay_toolchain(task, _META)
        assert record is not None
        assert "@sha256:" in record["image"]
        assert len(record["image"].rsplit("@sha256:", 1)[1]) == 64
        assert len(record["image_config_sha256"]) == 64


def test_replay_toolchain_missing_or_mutable_metadata_fails_closed(tmp_path):
    missing = tmp_path / "missing.json"
    assert srt.load_replay_toolchain("any__task", missing) is None

    mutable = tmp_path / "mutable.json"
    mutable.write_text(json.dumps({
        "schema": "gt.ss_replay_toolchains.v1",
        "tasks": {"any__task": {
            "image": "registry.invalid/task:latest",
            "image_config_sha256": "a" * 64,
            "platform": "linux/amd64",
            "repo_root": "/testbed",
            "python_version": "3.10.17",
            "python_env_sha256": "b" * 64,
        }},
    }), encoding="utf-8")
    assert srt.load_replay_toolchain("any__task", mutable) is None

    root_only = json.loads(mutable.read_text(encoding="utf-8"))
    record = root_only["tasks"]["any__task"]
    record["image"] = "registry.invalid/task@sha256:" + "c" * 64
    record["repo_root"] = "/"
    mutable.write_text(json.dumps(root_only), encoding="utf-8")
    assert srt.load_replay_toolchain("any__task", mutable) is None


def test_mount_mapping_rejects_comma_in_resolved_workspace(tmp_path):
    repo = tmp_path / "repo,with-comma"
    repo.mkdir()
    target = repo / "broken.py"
    target.write_text("def broken(:\n", encoding="utf-8")
    assert srt._confined_target("broken.py", str(repo), "/testbed") is None


def test_installer_rejects_seam_without_dedicated_edit_boundary(tmp_path):
    class IncompatibleSeam:
        @staticmethod
        def _build_env_executor():
            return None

    with pytest.raises(RuntimeError, match="edit-check executor boundary"):
        srt.install_replay_edit_executor(
            IncompatibleSeam(), "missing__task", tmp_path / "missing.json")


def test_missing_task_metadata_installs_quiet_executor_not_host_fallback(tmp_path):
    seam = _FakeSeam()
    unrelated = seam._build_env_executor
    assert not srt.install_replay_edit_executor(seam, "missing__task", tmp_path / "none.json")
    assert seam._build_env_executor == unrelated
    assert seam._build_env_executor() == "unrelated-executor"
    executor = seam._build_edit_check_executor()
    rc, out, err = executor(["python", "-V"], "D:/testbed", 10)
    assert rc is None and out == "" and "toolchain_unavailable" in err


def test_parser_executor_rejects_non_parser_commands_without_executing(
    monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert cmd[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_edit_executor(record)
    rc, out, err = executor(["pytest", "-q"], "D:/testbed", 10)
    assert rc is None and out == ""
    assert "unsupported_command" in err
    assert len(calls) == 1  # image preflight only; the rejected command never executes


def test_parser_executor_config_digest_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    payload = json.loads(_inspect_payload(record))
    payload["Id"] = "sha256:" + "0" * 64
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_edit_executor(record)
    parse_code = "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])"
    rc, out, err = executor(
        ["python", "-I", "-c", parse_code, "D:/testbed/pkg/broken.py"],
        "D:/testbed", 10)
    assert rc is None and out == "" and "toolchain_unavailable" in err
    assert len(calls) == 1


def test_parser_executor_accepts_containerd_manifest_id_only_when_repo_digest_matches(
    monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    payload = _inspect_payload(record, containerd=True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_edit_executor(record)
    rc, out, err = executor(["pytest", "-q"], "D:/testbed", 10)
    assert rc is None and out == "" and "unsupported_command" in err

    wrong = json.loads(payload)
    wrong["RepoDigests"] = [record["image"].rsplit("@", 1)[0] + "@sha256:" + "f" * 64]
    monkeypatch.setattr(
        srt.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, json.dumps(wrong), ""))
    quiet = srt.build_replay_edit_executor(record)
    rc, out, err = quiet(["pytest", "-q"], "D:/testbed", 10)
    assert rc is None and out == "" and "toolchain_unavailable" in err


def test_parser_executor_rejects_parse_code_mutation_without_executing(
    monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_edit_executor(record)
    mutated = "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])"
    rc, out, err = executor(
        ["python", "-I", "-c", mutated, "D:/testbed/pkg/broken.py"],
        "D:/testbed", 10)
    assert rc is None and out == "" and "unsupported_command" in err
    assert len(calls) == 1


def test_parser_executor_mounts_only_the_edited_file_at_recorded_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    repo = tmp_path / "repo"
    target = repo / "geopandas" / "tools" / "_random.py"
    target.parent.mkdir(parents=True)
    target.write_text("def broken(:\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, _inspect_payload(record), "")
        return subprocess.CompletedProcess(cmd, 1, "", "SyntaxError: invalid syntax")

    monkeypatch.setattr(srt.subprocess, "run", fake_run)
    executor = srt.build_replay_edit_executor(record)
    parse_code = "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])"
    rc, out, err = executor(
        ["python", "-I", "-c", parse_code, str(target)], str(repo), 10)

    assert (rc, out, err) == (1, "", "SyntaxError: invalid syntax")
    run = calls[1]
    assert run[:3] == ["docker", "run", "--rm"]
    assert "--network" in run and run[run.index("--network") + 1] == "none"
    mount = run[run.index("--mount") + 1]
    assert f"source={target.resolve()}" in mount
    assert "target=/testbed/geopandas/tools/_random.py" in mount
    assert run[-1] == "/testbed/geopandas/tools/_random.py"
    assert "pytest" not in run


def test_pinned_geopandas_runtime_executes_real_parser_when_image_is_local(tmp_path: Path):
    record = srt.load_replay_toolchain(_GEO, _META)
    assert record is not None
    inspected = subprocess.run(
        ["docker", "image", "inspect", record["image"]], capture_output=True, text=True)
    if inspected.returncode != 0:
        pytest.skip("pinned geopandas replay image is not locally available")

    repo = tmp_path / "repo"
    target = repo / "geopandas" / "tools" / "_random.py"
    target.parent.mkdir(parents=True)
    target.write_text("def broken(:\n", encoding="utf-8")
    executor = srt.build_replay_edit_executor(record)
    parse_code = "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])"
    rc, out, err = executor(
        ["python", "-I", "-c", parse_code, str(target)], str(repo), 10)

    assert rc == 1 and out == ""
    assert 'File "/testbed/geopandas/tools/_random.py", line 1' in err
    assert "/usr/local/lib/python3.10/ast.py" in err
    assert "SyntaxError: invalid syntax" in err
