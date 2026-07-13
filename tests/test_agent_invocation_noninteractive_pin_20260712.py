"""Pin: the paid-run agent must be launched HEADLESSLY, or every task washes to 0 steps.

BUG (run 29211807809 + micro-verify 29212976448 + micro-verify 29214296174, all 0 agent steps):
the `mini-swe-agent` console entry IS the interactive `mini` app (minisweagent.run.mini:app,
confirmed via entry_points). In a headless container it reaches import + config-load then never
calls agent.run() -- 0 steps, no trajectory. `--agent-class default` did NOT fix it (it swaps the
agent the app builds, not the app harness). The working baseline-83 / GT-on(75) ran mini-swe-agent
PROGRAMMATICALLY (pier + GTMiniSweAgent), never this CLI. FIX: gt_headless_runner.py replays the
non-interactive core of mini.py (get_model -> get_environment(local) -> get_agent(default) ->
agent.run), verified end-to-end locally with a DeterministicModel.

Two-sided pin on the trial step:
  * it MUST launch the agent via `python /opt/gt/gt_headless_runner.py` (a revert to running the
    agent through the interactive `mini-swe-agent`/`mini` CLI reddens this);
  * the GT_RUN_* env contract MUST be set, with the task -z guarded (an empty issue file must fall
    back, never launch on an empty task);
  * the runner MUST be staged into /opt/gt (else the container has nothing to run).

This pins liveness at the source: "the agent actually takes steps" becomes an assertion on HOW it
is launched, not a hope re-discovered only after a paid run returns empty.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_RUNNER = _ROOT / "artifact_deepswe" / "gt_headless_runner.py"


def _trial_run() -> str:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            run = step.get("run") if isinstance(step, dict) else None
            if run and "gt_headless_runner.py" in run and "GT_RUN_MODEL" in run:
                return run
    raise AssertionError("no trial step launches the agent via gt_headless_runner.py")


def _agent_launch_line(run: str) -> str:
    for raw in run.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if "gt_headless_runner.py" in line and line.startswith("python"):
            return line
    raise AssertionError("no non-comment `python .../gt_headless_runner.py` launch line found")


def test_launches_via_the_headless_runner() -> None:
    run = _trial_run()
    line = _agent_launch_line(run)
    assert "/opt/gt/gt_headless_runner.py" in line, (
        "the agent must be launched by the headless runner; got line={line!r}".format(line=line)
    )


def test_does_not_run_the_agent_through_the_interactive_cli() -> None:
    run = _trial_run()
    # the regressed forms: running the AGENT via the interactive mini CLI. (mini-extra config set /
    # install lines are fine -- those configure, they do not run the agent -- so ban only the
    # agent-run invocations that carry a --task or --agent-class.)
    for raw in run.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        assert not ("mini-swe-agent" in line and ("--task" in line or "--agent-class" in line)), (
            f"reverted to running the agent through the interactive mini CLI: {line!r}"
        )


def test_gt_run_env_contract_is_set_with_task_guard() -> None:
    run = _trial_run()
    assert "export GT_RUN_MODEL=" in run, "GT_RUN_MODEL must be exported for the runner"
    assert "export GT_RUN_OUTPUT=" in run, "GT_RUN_OUTPUT must be exported for the runner"
    assert "export GT_RUN_TASK=" in run, "GT_RUN_TASK must be exported for the runner"
    # the task must fall back through a -z emptiness check (exist-but-empty issue file makes cat
    # exit 0, so a bare || chain never falls through -> empty task).
    assert 'GT_TASK_TEXT' in run and '[ -z "$GT_TASK_TEXT" ]' in run, (
        "the task value must fall back through a -z emptiness check, not a bare || chain"
    )
    assert 'export GT_RUN_TASK="$GT_TASK_TEXT"' in run, "the runner must receive the guarded task"


def test_runner_is_staged_into_opt_gt() -> None:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    staged = any(
        isinstance(step, dict)
        and "cp artifact_deepswe/gt_headless_runner.py" in (step.get("run") or "")
        for job in doc.get("jobs", {}).values()
        for step in job.get("steps", []) or []
    )
    assert staged, "gt_headless_runner.py must be cp'd into HOST_GT_INJECT (/opt/gt) before the run"


def test_runner_file_exists_and_forces_default_agent() -> None:
    src = _RUNNER.read_text(encoding="utf-8")
    assert 'default_type="default"' in src, (
        "the runner must build the non-interactive DefaultAgent (default_type=\"default\"), never interactive"
    )
    assert "agent.run(task)" in src, "the runner must actually call agent.run(task)"
