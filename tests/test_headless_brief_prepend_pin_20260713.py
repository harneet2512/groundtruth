"""Pin the headless runner's native-task side of the canonical runtime cutover.

The runner passes the issue to ``agent.run`` byte-for-byte. Repository-derived
task-start evidence is staged by the canonical runtime and joined at the provider
boundary; local task construction must neither prepend it nor pre-mark it delivered.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACT_DIR = _ROOT / "artifact_deepswe"
_RUNNER = _ARTIFACT_DIR / "gt_headless_runner.py"

# The runner lives under artifact_deepswe/ (imported in-container from /opt/gt); make it
# importable here without the real minisweagent (its module-level imports are os/sys only —
# the minisweagent + gt_mini_patch imports are DEFERRED inside run()).
if str(_ARTIFACT_DIR) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_DIR))

import gt_headless_runner as ghr  # noqa: E402


_BRIEF = "<gt-task-brief>\nEDIT src/pkg/mod.py::fix_it — preserve the Optional[User] return.\n</gt-task-brief>"
_ISSUE = "The widget crashes when the config key is missing. Fix it."


# ── behavior: the exact value fed to agent.run(task) ────────────────────────────────

def test_gt_arm_keeps_native_issue_bytes_for_canonical_attachment(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_bytes(_BRIEF.encode("utf-8"))  # LF bytes = real container brief.txt (no CRLF translation)
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(brief)}  # GT_BASELINE unset -> GT arm
    task = ghr._resolve_task(env)
    assert task == _ISSUE
    assert _BRIEF not in task


def test_gt_arm_records_exact_producer_seal_without_changing_task_bytes(tmp_path):
    brief = tmp_path / "brief.txt"
    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    brief.write_bytes(_BRIEF.encode("utf-8"))
    env = {
        "GT_RUN_TASK": _ISSUE,
        "GT_BRIEF_FILE": str(brief),
        "GT_INSEAM_METRICS": "1",
        "GT_RUNTIME_LEDGER": str(ledger),
    }

    task = ghr._resolve_task(env)

    assert task == _ISSUE
    assert not ledger.exists(), "local task construction is not delivery"


def test_brief_producer_seal_is_default_off(tmp_path):
    brief = tmp_path / "brief.txt"
    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    brief.write_bytes(_BRIEF.encode("utf-8"))

    task = ghr._resolve_task({
        "GT_RUN_TASK": _ISSUE,
        "GT_BRIEF_FILE": str(brief),
        "GT_RUNTIME_LEDGER": str(ledger),
    })

    assert task == _ISSUE
    assert not ledger.exists()


def test_baseline_arm_task_is_byte_identical(tmp_path):
    # A brief IS on disk + GT_BRIEF_FILE points at it, but the baseline (control) arm must NEVER
    # read it: the resolved task must equal the guarded issue text byte-for-byte.
    brief = tmp_path / "brief.txt"
    brief.write_bytes(_BRIEF.encode("utf-8"))  # LF bytes = real container brief.txt (no CRLF translation)
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(brief), "GT_BASELINE": "1"}
    task = ghr._resolve_task(env)
    assert task == _ISSUE, (
        "baseline arm must be byte-identical to the issue text (no brief read) — the paired "
        f"control is broken otherwise; got {task!r}"
    )
    assert _BRIEF not in task, "no brief bytes may leak into the baseline task"


def test_absent_brief_leaves_task_unchanged(tmp_path):
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(tmp_path / "does_not_exist.txt")}
    task = ghr._resolve_task(env)
    assert task == _ISSUE, "a missing brief file must leave the task unchanged (correct-or-quiet)"


def test_empty_brief_leaves_task_unchanged(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("   \n\n  ", encoding="utf-8")  # whitespace-only == empty
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(brief)}
    task = ghr._resolve_task(env)
    assert task == _ISSUE, "a whitespace-only brief must be treated as empty -> task unchanged"


# ── source-structure: run() feeds _resolve_task's output straight to agent.run ──────

def test_run_wires_resolve_task_into_agent_run():
    src = _RUNNER.read_text(encoding="utf-8")
    assert "task = _resolve_task(e)" in src, (
        "run() must build the native task through the guarded resolver"
    )
    assert "agent.run(task)" in src, "run() must pass the resolved task to agent.run"


# ── end-to-end: the value that actually reaches agent.run(task) ─────────────────────

def _install_fake_minisweagent(monkeypatch, captured):
    class _FakeAgent:
        n_calls = 1
        cost = 0.0

        def run(self, task):
            captured["task"] = task
            return {"exit_status": "Submitted"}

    agents = types.ModuleType("minisweagent.agents")
    agents.get_agent = lambda *a, **k: _FakeAgent()
    config = types.ModuleType("minisweagent.config")
    config.get_config_from_spec = lambda p: {"model": {}, "environment": {}, "agent": {}}
    environments = types.ModuleType("minisweagent.environments")
    environments.get_environment = lambda *a, **k: object()
    models = types.ModuleType("minisweagent.models")
    models.get_model = lambda *a, **k: object()
    pkg = types.ModuleType("minisweagent")
    for name, mod in [
        ("minisweagent", pkg),
        ("minisweagent.agents", agents),
        ("minisweagent.config", config),
        ("minisweagent.environments", environments),
        ("minisweagent.models", models),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    # gt_mini_patch is imported in-process on the GT arm; stub it so run() has no real side effects.
    gmp = types.ModuleType("gt_mini_patch")
    gmp._PATCHED_CLASSES = []
    gmp.install_canonical_runtime = lambda **kwargs: types.SimpleNamespace(
        attached=True,
        attempt_runtime=object(),
        provider_boundary=object(),
        commitment_boundary=object(),
    )
    gmp.ledger_write_failures = lambda: 0
    monkeypatch.setitem(sys.modules, "gt_mini_patch", gmp)


def test_agent_run_receives_native_task_and_canonical_attachment(tmp_path, monkeypatch):
    captured: dict[str, str] = {}
    _install_fake_minisweagent(monkeypatch, captured)
    brief = tmp_path / "brief.txt"
    brief.write_bytes(_BRIEF.encode("utf-8"))  # LF bytes = real container brief.txt (no CRLF translation)
    env = {
        "GT_RUN_MODEL": "deepseek/deepseek-v4-flash",
        "GT_RUN_TASK": _ISSUE,
        "GT_BRIEF_FILE": str(brief),
        "GT_RUN_CONFIG": str(tmp_path / "cfg.yaml"),
        "GT_RUN_OUTPUT": str(tmp_path / "out.json"),
    }
    rc = ghr.run(env)
    assert rc == 0, "run() must complete the agent loop"
    assert "task" in captured, "agent.run was never called"
    assert captured["task"] == _ISSUE


def test_agent_run_baseline_receives_issue_only(tmp_path, monkeypatch):
    captured: dict[str, str] = {}
    _install_fake_minisweagent(monkeypatch, captured)
    brief = tmp_path / "brief.txt"
    brief.write_bytes(_BRIEF.encode("utf-8"))  # LF bytes = real container brief.txt (no CRLF translation)
    env = {
        "GT_RUN_MODEL": "deepseek/deepseek-v4-flash",
        "GT_RUN_TASK": _ISSUE,
        "GT_BRIEF_FILE": str(brief),
        "GT_BASELINE": "1",
        "GT_RUN_CONFIG": str(tmp_path / "cfg.yaml"),
        "GT_RUN_OUTPUT": str(tmp_path / "out.json"),
    }
    rc = ghr.run(env)
    assert rc == 0
    assert captured.get("task") == _ISSUE, (
        "baseline agent.run must receive the issue text byte-identically (no brief)"
    )
