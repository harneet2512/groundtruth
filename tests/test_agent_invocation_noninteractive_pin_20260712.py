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
        # the invocation line runs the runner under the resolved interpreter ($GT_PY), so it no
        # longer starts with a literal `python`; match the runner path + an interpreter token.
        if "gt_headless_runner.py" in line and ("$GT_PY" in line or "python" in line):
            return line
    raise AssertionError("no non-comment gt_headless_runner.py launch line found")


def test_launches_via_the_headless_runner() -> None:
    run = _trial_run()
    line = _agent_launch_line(run)
    assert "/opt/gt/gt_headless_runner.py" in line, (
        "the agent must be launched by the headless runner; got line={line!r}".format(line=line)
    )


def test_runner_launched_under_resolved_interpreter_with_pythonpath() -> None:
    # B5 root cause: bare `python` on a uv-tool container cannot import minisweagent -> 0 steps.
    # The runner MUST launch under the resolved GT_PY with /opt/gt on PYTHONPATH (so it can import
    # both minisweagent and gt_mini_patch for in-process delivery).
    run = _trial_run()
    line = _agent_launch_line(run)
    assert "$GT_PY" in line, f"the runner must launch under the resolved interpreter $GT_PY: {line!r}"
    assert "PYTHONPATH=/opt/gt" in line, f"the runner needs PYTHONPATH=/opt/gt for its imports: {line!r}"


def test_trial_resolves_interpreter_by_probing_minisweagent() -> None:
    # the GT_PY resolution must PROVE importability (probe `import minisweagent`), never assume a
    # bare `python` works — that assumption is the exact B5 defect.
    run = _trial_run()
    assert 'GT_PY=' in run, "the trial step must resolve a GT_PY interpreter"
    assert '-c "import minisweagent"' in run, (
        "GT_PY must be chosen by probing `import minisweagent`, not assumed"
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


def _all_runs() -> list[str]:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    return [
        str(step.get("run"))
        for job in doc.get("jobs", {}).values()
        for step in job.get("steps", []) or []
        if isinstance(step, dict) and step.get("run")
    ]


def test_eval_step_reads_patch_from_the_agent_output_dir() -> None:
    # D1 pin: the eval step is a SEPARATE step (trial-step env does not carry over), so its
    # HOST_GT_OUT default MUST be the agent output dir /tmp/gt_out. The old /tmp/gt default made
    # the raw-patch sources always miss -> reward 0 on every task even with a perfect patch.
    evals = [r for r in _all_runs() if r and "agent_patch.diff" in r and "run_evaluation" in r]
    assert evals, "no official-eval step found"
    for r in evals:
        assert 'HOST_GT_OUT="${HOST_GT_OUT:-/tmp/gt_out}"' in r, (
            "the eval step must default HOST_GT_OUT to /tmp/gt_out (the agent output bind-mount), "
            "never /tmp/gt (the substrate artifacts dir)"
        )


def test_liveness_check_is_trajectory_based_not_stdout_grep() -> None:
    # D2 pin: the headless runner prints neither THOUGHT nor observations to stdout, so a
    # stdout-grep liveness check false-negatives every headless run. The check must read the
    # native trajectory (api_calls) with the runner finish line as fallback.
    checks = [r for r in _all_runs() if r and "AGENT_DID_NOT_RUN" in r]
    assert checks, "no liveness-check step found"
    for r in checks:
        assert "mini-swe-agent.trajectory.json" in r, (
            "liveness must be read from the native trajectory (api_calls), not stdout grep"
        )
        assert "api_calls" in r, "liveness must count info.model_stats.api_calls"
        assert "headless agent finished" in r, "the runner finish line must be the fallback signal"
        code_lines = [ln for ln in r.splitlines() if not ln.strip().startswith("#")]
        assert not any("THOUGHT" in ln for ln in code_lines), (
            "the interactive-CLI THOUGHT grep must not come back (in code, comments exempt)"
        )


def test_liveness_fallback_no_match_reaches_fail_closed_marker() -> None:
    """A missing runner finish line must not abort before AGENT_DID_NOT_RUN is emitted.

    GitHub runs multiline shell steps with ``bash -e -o pipefail``.  A bare no-match
    ``grep | ...`` inside an assignment therefore terminates the step immediately,
    bypassing the explicit zero-step classification that downstream diagnosis relies on.
    """
    checks = [r for r in _all_runs() if r and "AGENT_DID_NOT_RUN" in r]
    assert checks, "no liveness-check step found"
    for run in checks:
        fallback = next(
            line.strip()
            for line in run.splitlines()
            if line.strip().startswith("AGENT_STEPS=$(grep ")
        )
        assert fallback.endswith("|| true)"), (
            "the finish-line fallback must tolerate grep no-match so execution reaches "
            f"the explicit AGENT_DID_NOT_RUN gate: {fallback!r}"
        )


def test_receipt_and_profile_run_under_resolved_interpreter() -> None:
    # D3 pin: capability_receipt + rl_profile exports must run under $GT_PY — bare `python` is
    # <3.10 on 155/300 images (the same divergence class as B5) -> empty receipt -> profile dark.
    run = _trial_run()
    for mod in ("groundtruth.runtime.capability_receipt", "groundtruth.runtime.rl_profile"):
        for raw in run.splitlines():
            if mod in raw and not raw.strip().startswith("#"):
                assert '"$GT_PY"' in raw, f"{mod} must run under the resolved $GT_PY: {raw.strip()!r}"


def test_issue_text_reaches_python_via_argv_and_fails_closed() -> None:
    # D5 pin: `task="$MTASK"` inside the -c string expanded to an UNQUOTED python expression
    # (hyphenated ids = NameError) -> ISSUE_TEXT empty -> blind placeholder run. The id must reach
    # python via sys.argv and an empty issue must abort BEFORE any container/model spend.
    stagers = [r for r in _all_runs() if r and "ISSUE_TEXT=$(" in r]
    assert stagers, "no issue-staging step found"
    for r in stagers:
        code_lines = [ln for ln in r.splitlines() if not ln.strip().startswith("#")]
        assert not any('task="$MTASK"' in ln for ln in code_lines), (
            "the task id must not be shell-interpolated into the python -c source (hyphenated ids "
            "parse as subtraction -> NameError -> empty issue -> blind run)"
        )
        assert "sys.argv[1]" in r, "the task id must reach python via sys.argv"
        assert 'if [ -z "$ISSUE_TEXT" ]' in r and "exit 1" in r, (
            "an empty ISSUE_TEXT must fail closed before spend, never blind-run a placeholder"
        )


def test_runner_self_installs_gt_delivery_in_process() -> None:
    # The load-bearing half of the B5 delivery fix: nothing else imports gt_mini_patch in the agent
    # process, so the runner MUST import it itself (its _install() patches Environment.execute in
    # THIS interpreter). A revert to relying on the separate-process install reddens here.
    src = _RUNNER.read_text(encoding="utf-8")
    assert "import gt_mini_patch" in src, (
        "the runner must import gt_mini_patch in-process so GT evidence rides Environment.execute"
    )
    # and it must respect the control arm (no GT load under GT_BASELINE=1)
    assert 'GT_BASELINE' in src, "the runner must skip the gt_mini_patch import in the baseline arm"


def test_runner_exit_is_preserved_after_patch_capture() -> None:
    """A failed agent must stay failed while still exporting its partial patch/artifacts."""
    run = _trial_run()
    code = "\n".join(
        line for line in run.splitlines() if not line.strip().startswith("#")
    )
    launch = _agent_launch_line(run)
    assert "|| true" not in launch, "the headless runner exit code must not be erased"
    assert "GT_RUNNER_RC=0" in code
    assert "|| GT_RUNNER_RC=$?" in launch
    launch_pos = code.index(launch)
    rc_init = code.rfind("GT_RUNNER_RC=0", 0, launch_pos)
    patch_capture = code.index("git diff HEAD > /gt_out/agent_patch.diff", launch_pos)
    terminal_exit = code.index('exit "$GT_RUNNER_RC"', patch_capture)
    assert rc_init >= 0
    assert rc_init < launch_pos < patch_capture < terminal_exit


def test_outer_pipeline_captures_docker_status_before_any_fallback() -> None:
    run = _trial_run()
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    tee_index = next(
        index for index, line in enumerate(lines)
        if "2>&1 | tee" in line and "trial_output.log" in line
    )
    assert "|| true" not in lines[tee_index]
    assert lines[tee_index + 1] == "AGENT_RC=${PIPESTATUS[0]}"
