"""Workspace metadata readiness belongs to the substrate runtime, not workflow heuristics."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_PROOF = ROOT / "scripts" / "swebench" / "gt_run_proof.py"
_SPEC = importlib.util.spec_from_file_location("gt_run_proof_workspace_probe", _PROOF)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["gt_run_proof_workspace_probe"] = _MOD
_SPEC.loader.exec_module(_MOD)


def test_go_workspace_metadata_probe_success(monkeypatch, tmp_path):
    def _run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        # d8fe8b37: probe uses `go list -e` + a probe-only env override (-mod=mod + live
        # GOPROXY) so an incomplete offline cache can still resolve the transitive set.
        assert cmd == ["go", "list", "-e", "./..."]
        assert cwd == str(tmp_path)
        assert env is not None and env.get("GOFLAGS") == "-mod=mod"
        assert env.get("GOPROXY") == "https://proxy.golang.org,direct"
        return subprocess.CompletedProcess(cmd, 0, stdout="repo/pkg\nrepo/pkg/sub\n", stderr="")

    monkeypatch.setattr(_MOD.subprocess, "run", _run)
    result = _MOD.probe_workspace_metadata("go", str(tmp_path), os.environ.copy())
    assert result["status"] == "ok"
    assert result["package_count"] == 2


def test_go_workspace_metadata_probe_failure(monkeypatch, tmp_path):
    def _run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="no required module provides package"
        )

    monkeypatch.setattr(_MOD.subprocess, "run", _run)
    result = _MOD.probe_workspace_metadata("go", str(tmp_path), os.environ.copy())
    assert result["status"] == "fail"
    assert result["code"] == "GO_WORKSPACE_METADATA_FAIL"
    assert "no required module provides package" in result["message"]


def test_rust_workspace_metadata_probe_success(monkeypatch, tmp_path):
    def _run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        assert cmd == ["cargo", "metadata", "--format-version=1", "--no-deps"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"packages":[]}', stderr="")

    monkeypatch.setattr(_MOD.subprocess, "run", _run)
    result = _MOD.probe_workspace_metadata("rust", str(tmp_path), os.environ.copy())
    assert result["status"] == "ok"


def test_workspace_metadata_probe_skips_python(monkeypatch, tmp_path):
    called = False

    def _run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess.run should not be called for python")

    monkeypatch.setattr(_MOD.subprocess, "run", _run)
    result = _MOD.probe_workspace_metadata("python", str(tmp_path), os.environ.copy())
    assert result["status"] == "skip"
    assert called is False
