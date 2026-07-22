"""Import-fallback telemetry belongs only to a successfully installed seam copy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import gt_mini_patch as g


def test_runtime_import_failure_is_pending_until_install_commit(monkeypatch, capsys):
    written = []
    monkeypatch.setattr(g, "_RUNTIME_AVAILABLE", True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: written.append(dict(row)) or True)
    g._PENDING_RUNTIME_IMPORT_FAILURES.clear()

    g._runtime_import_failed("fixture_runtime", RuntimeError("broken"))

    assert written == []
    assert len(g._PENDING_RUNTIME_IMPORT_FAILURES) == 1
    assert "runtime_import_fallback=true module=fixture_runtime" in capsys.readouterr().err

    g._commit_runtime_import_failures()
    assert len(written) == 1
    assert written[0]["outcome"] == "provider_failed"
    assert "runtime_import_fallback[fixture_runtime]" in written[0]["reason"]
    assert g._PENDING_RUNTIME_IMPORT_FAILURES == []
    g._commit_runtime_import_failures()
    assert len(written) == 1  # at-most-once if install is retried


def test_successful_install_commits_real_fallback_telemetry(monkeypatch):
    written = []
    monkeypatch.setattr(g, "_RUNTIME_AVAILABLE", True)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_should_auto_invert", lambda: False)
    monkeypatch.setattr(g, "_observation_batch_required", lambda: False)
    monkeypatch.setattr(g, "_install_default_agent_batch_hook", lambda: True)
    monkeypatch.setattr(g, "_ENV_CLASSES", [])
    monkeypatch.setattr(g, "_write_profile_receipt", lambda: None)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: written.append(dict(row)) or True)
    g._PENDING_RUNTIME_IMPORT_FAILURES.clear()
    g._runtime_import_failed("real_fallback", ImportError("missing"))

    g._install()

    assert [row["outcome"] for row in written] == ["provider_failed"]
    assert "runtime_import_fallback[real_fallback]" in written[0]["reason"]
    assert g._PENDING_RUNTIME_IMPORT_FAILURES == []


def test_failed_first_import_cannot_contaminate_successful_backup_import(tmp_path):
    """Reproduce .pth failure then backup success in one fresh interpreter."""
    repo = Path(__file__).resolve().parents[2]
    patch_dir = repo / "artifact_deepswe"
    ledger = tmp_path / "art" / "gt_runtime_ledger.jsonl"
    marker = tmp_path / "art" / "gt_proof_active"
    profile = ledger.parent / "gt_profile_receipt.json"
    code = textwrap.dedent(
        r"""
        import importlib.abc
        import json
        import os
        from pathlib import Path
        import sys
        import types

        ledger = Path(os.environ["GT_RUNTIME_LEDGER"])
        marker = Path(os.environ["GT_PROOF_MARKER"])
        profile = ledger.parent / "gt_profile_receipt.json"

        blocked_runtime = {
            "groundtruth.runtime.action_translation",
            "groundtruth.runtime.context_budget",
            "groundtruth.runtime.ledger",
            "groundtruth.runtime.trajectory_state",
            "groundtruth.runtime.episode_state",
            "groundtruth.runtime.verification_horizon",
        }

        class FirstAttemptBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "minisweagent" or fullname in blocked_runtime:
                    raise ImportError("first-attempt-block:" + fullname)
                return None

        blocker = FirstAttemptBlocker()
        sys.meta_path.insert(0, blocker)
        first_error = ""
        try:
            import gt_mini_patch
        except Exception as exc:
            first_error = type(exc).__name__ + ":" + str(exc)
        finally:
            sys.modules.pop("gt_mini_patch", None)

        after_failed = {
            "ledger": ledger.exists(),
            "profile": profile.exists(),
            "proof": marker.exists(),
        }

        sys.meta_path.remove(blocker)
        mini = types.ModuleType("minisweagent")
        mini.__path__ = []
        mini.__version__ = "fixture"
        agents = types.ModuleType("minisweagent.agents")
        agents.__path__ = []
        default = types.ModuleType("minisweagent.agents.default")

        class DefaultAgent:
            def __init__(self):
                self.model = None

        default.DefaultAgent = DefaultAgent
        envpkg = types.ModuleType("minisweagent.environments")
        envpkg.__path__ = []
        sys.modules.update({
            "minisweagent": mini,
            "minisweagent.agents": agents,
            "minisweagent.agents.default": default,
            "minisweagent.environments": envpkg,
        })
        for leaf, class_name in (
            ("local", "LocalEnvironment"),
            ("docker", "DockerEnvironment"),
            ("singularity", "SingularityEnvironment"),
        ):
            module = types.ModuleType("minisweagent.environments." + leaf)
            env_class = type(class_name, (), {"execute": lambda self, action: {
                "output": "", "returncode": 0}})
            setattr(module, class_name, env_class)
            sys.modules[module.__name__] = module

        imported = __import__("gt_mini_patch")
        rows = []
        if ledger.exists():
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        print(json.dumps({
            "first_error": first_error,
            "after_failed": after_failed,
            "patched": imported._PATCHED_CLASSES,
            "rows": rows,
            "profile": profile.exists(),
            "proof": marker.exists(),
        }))
        """
    )
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": os.pathsep.join((str(patch_dir), str(repo / "src"))),
        "GT_RUNTIME_LEDGER": str(ledger),
        "GT_PROOF_MARKER": str(marker),
        "GT_PROOF_MODE": "1",
        "GT_RL_PROFILE": "2",
        "GT_ORACLE_ROUTE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert "GT_OBSERVATION_BATCH_CONSTRUCTOR_UNAVAILABLE" in result["first_error"]
    assert result["after_failed"] == {"ledger": False, "profile": False, "proof": False}
    assert len(result["patched"]) == 3
    assert result["profile"] is True and result["proof"] is True
    assert not any(row.get("outcome") == "provider_failed" for row in result["rows"])
    assert "runtime_import_fallback=true" in proc.stderr  # immediate diagnosis retained
