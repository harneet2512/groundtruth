"""Pin: the headless runner must PREPEND the step-0 GT brief onto the agent task.

THE FINDING (verified by a §4 audit + direct probes on both arms, 2026-07-13): the step-0 GT
brief is generated (gt_artifacts/brief.txt, now minimal+native) but was NEVER injected into the
agent's task text on the mini HEADLESS pipeline. The workflow stages ISSUE_TEXT -> GT_RUN_TASK ->
gt_headless_runner.py -> agent.run(task), so the agent saw the issue text ALONE — byte-identical
across arms. The old pier path prepended the brief (gt_agent._prepend_brief, gt_agent.py:1115);
the headless path lost the wire, so every mini run to date ran with the step-0 channel DARK.

FIX (runner-side, minimal): gt_headless_runner._resolve_task() reads the substrate brief from
GT_BRIEF_FILE (default /gt_artifacts/brief.txt — the read-only substrate mount the workflow
already provides) and PREPENDS it to the task on the GT arm. Correct-or-quiet: a missing /
unreadable / empty brief leaves the task UNCHANGED. BASELINE arm NEVER reads the file, so its
task text is byte-identical to a run with no brief on disk (paired control preserved).

Two-sided behavioral pin on the exact value fed to agent.run(task):
  * GT arm + a readable non-empty brief -> the resolved task STARTS WITH the brief bytes
    (prepend is BEFORE the issue, never after — the mutation that appends reddens this);
  * GT_BASELINE=1 -> the resolved task is byte-identical to the guarded issue text (the
    mutation that drops the baseline guard reddens this);
  * brief file absent -> task unchanged + a breadcrumb (never a crash — correct-or-quiet).

RED-first: on the pre-fix tree _resolve_task does not exist (AttributeError) and run() passes the
raw issue to agent.run, so the starts-with pin fails. Mutations (verified): (a) prepend AFTER
instead of BEFORE reddens test_gt_arm_prepends_brief_before_issue; (b) dropping the baseline guard
reddens test_baseline_arm_task_is_byte_identical.
"""
from __future__ import annotations

import hashlib
import json
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

def test_gt_arm_prepends_brief_before_issue(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_bytes(_BRIEF.encode("utf-8"))  # LF bytes = real container brief.txt (no CRLF translation)
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(brief)}  # GT_BASELINE unset -> GT arm
    task = ghr._resolve_task(env)
    assert task.startswith(_BRIEF), (
        "the step-0 brief must be prepended BEFORE the issue (the agent reads turn-0 top-down); "
        f"got task[:80]={task[:80]!r}"
    )
    assert _ISSUE in task, "the original issue text must be preserved after the brief"
    assert task == _BRIEF + "\n\n" + _ISSUE, "brief and issue must be joined by exactly one blank line"


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

    assert task == _BRIEF + "\n\n" + _ISSUE
    row = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert row["layer"] == "brief.task"
    assert row["event_type"] == "task_start"
    assert row["outcome"] == "delivered"
    assert row["chars_delivered"] == len(_BRIEF)
    assert row["content_sha256_16"] == hashlib.sha256(
        _BRIEF.encode("utf-8")
    ).hexdigest()[:16]
    assert row["seal_scope"] == "block"


def test_brief_producer_seal_is_default_off(tmp_path):
    brief = tmp_path / "brief.txt"
    ledger = tmp_path / "gt_runtime_ledger.jsonl"
    brief.write_bytes(_BRIEF.encode("utf-8"))

    task = ghr._resolve_task({
        "GT_RUN_TASK": _ISSUE,
        "GT_BRIEF_FILE": str(brief),
        "GT_RUNTIME_LEDGER": str(ledger),
    })

    assert task == _BRIEF + "\n\n" + _ISSUE
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


def test_absent_brief_leaves_task_unchanged(tmp_path, capsys):
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(tmp_path / "does_not_exist.txt")}
    task = ghr._resolve_task(env)
    assert task == _ISSUE, "a missing brief file must leave the task unchanged (correct-or-quiet)"
    err = capsys.readouterr().err
    assert "task unchanged" in err, "a missing brief must emit a breadcrumb, not fail silently"


def test_empty_brief_leaves_task_unchanged(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("   \n\n  ", encoding="utf-8")  # whitespace-only == empty
    env = {"GT_RUN_TASK": _ISSUE, "GT_BRIEF_FILE": str(brief)}
    task = ghr._resolve_task(env)
    assert task == _ISSUE, "a whitespace-only brief must be treated as empty -> task unchanged"


def test_default_brief_path_is_the_substrate_mount():
    # No GT_BRIEF_FILE set -> the default MUST be the read-only substrate mount path the workflow
    # provides (/gt_artifacts/brief.txt). With no file there in the test env, correct-or-quiet
    # leaves the task unchanged; the DEFAULT is asserted from the runner source.
    src = _RUNNER.read_text(encoding="utf-8")
    assert '"/gt_artifacts/brief.txt"' in src, (
        "the runner default brief path must be /gt_artifacts/brief.txt (the read-only substrate mount)"
    )
    assert 'GT_BRIEF_FILE' in src, "the runner must read the GT_BRIEF_FILE env override"


# ── source-structure: run() feeds _resolve_task's output straight to agent.run ──────

def test_run_wires_resolve_task_into_agent_run():
    src = _RUNNER.read_text(encoding="utf-8")
    assert "task = _resolve_task(e)" in src, (
        "run() must build the task via _resolve_task(e) so the brief-prepended value reaches agent.run"
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
    monkeypatch.setitem(sys.modules, "gt_mini_patch", gmp)


def test_agent_run_receives_the_brief_prepended_task(tmp_path, monkeypatch):
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
    assert captured["task"].startswith(_BRIEF), (
        "the task that actually reached agent.run must start with the brief bytes; "
        f"got {captured['task'][:80]!r}"
    )
    assert _ISSUE in captured["task"], "the issue text must survive into agent.run"


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
