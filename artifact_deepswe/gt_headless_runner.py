#!/usr/bin/env python3
"""Headless mini-swe-agent runner — the non-interactive core of ``run/mini.py``.

WHY THIS EXISTS (proven on micro-verify 29214296174, both tasks 0 steps):
The ``mini`` / ``mini-swe-agent`` console entry is ``minisweagent.run.mini:app`` — the INTERACTIVE
Textual/prompt_toolkit app. In a headless container it imports, loads the global config, then never
reaches ``agent.run()`` — it produces 0 steps and writes no trajectory. ``--agent-class default``
does not help: it only changes the agent the app builds, not the app harness. The single-instance
SWE-bench runner (``run/benchmarks/swebench_single.py:87-96``) shows the construction the app wraps:

    env   = <environment>
    agent = get_agent(get_model(config=cfg["model"]), env, cfg["agent"], default_type=...)
    agent.run(problem_statement)

This runner replays exactly that construction with a LOCAL environment (the eval container IS the
task environment — no pier, no second docker env) and the non-interactive DefaultAgent
(``default_type="default"``), so the step loop actually runs. GT delivery is unchanged and
agent-class-agnostic: ``gt_mini_patch --install`` wraps ``Environment.execute`` (gt_mini_patch.py
:11623-11624), which DefaultAgent calls on every action.

ENV CONTRACT (set by the workflow so the single-quoted docker block carries NO apostrophe-prone
inline args):
  GT_RUN_MODEL   model name, e.g. deepseek/deepseek-v4-flash            (required)
  GT_RUN_TASK    the issue / problem statement                          (falls back to a literal)
  GT_RUN_CONFIG  agent config spec path                                 (default /opt/gt/agent_config.yaml)
  GT_RUN_OUTPUT  trajectory output path                                 (default /gt_out/mini-swe-agent.trajectory.json)

Exit code: 0 when the agent loop ran to a terminal exit (LimitsExceeded / submit / handled
exception — DefaultAgent.run always returns in those cases); 2 on a config/wiring error before the
loop. The workflow guards the call with ``|| true`` and reads the TRAJECTORY (n_calls) as the
authoritative step count, not this exit code.
"""
from __future__ import annotations

import os
import sys


def _bc(msg: str) -> None:
    """Flushed breadcrumb to stderr — survives a SIGKILL/hang (unbuffered), so the trial log shows
    exactly how far the runner got when it produces no trajectory."""
    sys.stderr.write(f"[GT-RUNNER] {msg}\n")
    sys.stderr.flush()


def _resolve_task(e: dict) -> str:
    """Resolve the agent task text: the guarded GT_RUN_TASK with the STEP-0 GT BRIEF prepended.

    THE B-wire (2026-07-13): the pier path prepended the curated step-0 brief onto the agent
    instruction (gt_agent._prepend_brief, gt_agent.py:1115); the mini HEADLESS path lost that wire —
    the workflow stages the issue into GT_RUN_TASK and this runner called ``agent.run(task)`` with
    the issue ALONE, so every mini run to date ran with the step-0 channel DARK (brief.txt generated
    but never seen by the agent). Restore it here: on the GT arm read the substrate brief (baked by
    the substrate-proof step to the host artifacts dir, bind-mounted READ-ONLY into the container at
    ``GT_BRIEF_FILE`` — default ``/gt_artifacts/brief.txt``) and PREPEND it so the model reads it at
    turn 0 on the observation channel it already consumes.

    Correct-or-quiet: a missing / unreadable / empty brief leaves the task UNCHANGED (a brief-less
    run is degraded, never broken). BASELINE (control) arm NEVER reads the file, so its task text is
    byte-identical to a run with no brief on disk — the paired control is preserved exactly.
    """
    task = (e.get("GT_RUN_TASK") or "").strip() or "Fix the issue in the repository."
    if e.get("GT_BASELINE") == "1":
        _bc("GT_BASELINE=1 -> brief NOT prepended (pure control arm)")
        return task
    brief_path = (e.get("GT_BRIEF_FILE") or "/gt_artifacts/brief.txt").strip()
    try:
        with open(brief_path, "rb") as fh:
            brief_text = fh.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 -- correct-or-quiet: a brief read miss must NOT fail the run
        _bc(f"no brief file -> task unchanged ({type(exc).__name__} @ {brief_path})")
        return task
    if not brief_text.strip():
        _bc(f"empty brief -> task unchanged (@ {brief_path})")
        return task
    _bc(f"brief prepended ({len(brief_text)} chars @ {brief_path})")
    return brief_text + "\n\n" + task


def run(env: dict | None = None) -> int:
    """Build model + local env + DefaultAgent from the env contract and run one task.

    ``env`` defaults to ``os.environ``; it is a parameter so a test can drive the wiring with a
    fake config path without mutating the process environment.
    """
    e = os.environ if env is None else env
    _bc(f"start pid={os.getpid()} py={sys.version.split()[0]} exe={sys.executable}")
    model_name = (e.get("GT_RUN_MODEL") or "").strip()
    if not model_name:
        _bc("FATAL: GT_RUN_MODEL unset — cannot build the model")
        return 2
    cfg_path = e.get("GT_RUN_CONFIG") or "/opt/gt/agent_config.yaml"
    # STEP-0 BRIEF (2026-07-13): prepend the substrate brief onto the task on the GT arm (see
    # _resolve_task) — restores the pier-path prepend the headless pipeline had lost. Baseline
    # arm returns the guarded issue text byte-identically.
    task = _resolve_task(e)
    out = e.get("GT_RUN_OUTPUT") or "/gt_out/mini-swe-agent.trajectory.json"
    _bc(f"env ok model={model_name} cfg={cfg_path} task_chars={len(task)} out={out}")

    # Imports are deferred so a missing minisweagent surfaces as a clean rc=2, not an import-time
    # crash before the diagnostic print.
    from minisweagent.agents import get_agent
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments import get_environment
    from minisweagent.models import get_model
    _bc("imported minisweagent")

    # DELIVERY — the second half of the B5 fix. GT evidence rides Environment.execute, which is
    # monkey-patched by gt_mini_patch._install() (gt_mini_patch.py:11639). NOTHING ELSE imports
    # gt_mini_patch in THIS process: the workflow's separate `python .../gt_mini_patch.py` runs in
    # a throwaway process, and on a uv-tool container that python cannot even import minisweagent,
    # so it patches ZERO env classes. The patch must therefore be imported in the SAME interpreter
    # that calls agent.run(). Import it here (patching the CLASS method reaches the already-built
    # env instance at call time). It self-gates on GT_BASELINE -> a no-op in the control arm; we
    # also skip the import outright there so the control process never loads GT. Fail-OPEN on a bad
    # import (breadcrumb the WARN, keep running) — the proof marker + harness gate enforce
    # fail-closed at the run boundary, not here. Breadcrumb the patched set so the trial log proves
    # delivery ATTACHED, not merely that the agent ran.
    if e.get("GT_BASELINE") != "1":
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        try:
            import gt_mini_patch  # noqa: F401 -- import side effect: _install() patches env.execute
            _bc(f"gt_mini_patch installed patched={getattr(gt_mini_patch, '_PATCHED_CLASSES', '?')}")
        except Exception as _exc:  # noqa: BLE001 -- delivery import must not crash the agent loop
            _bc(f"WARN gt_mini_patch import failed: {type(_exc).__name__}: {_exc}")
    else:
        _bc("GT_BASELINE=1 -> gt_mini_patch NOT imported (pure control arm)")

    cfg = get_config_from_spec(cfg_path)
    if not isinstance(cfg, dict):
        _bc(f"FATAL: config {cfg_path!r} did not parse to a mapping")
        return 2
    _bc(f"config loaded keys={sorted(cfg)}")

    model = get_model(config={**cfg.get("model", {}), "model_name": model_name})
    _bc("model built")
    env_obj = get_environment(cfg.get("environment", {}), default_type="local")
    _bc("env built")
    agent_cfg = {**cfg.get("agent", {}), "output_path": out}
    # default_type="default" -> the non-interactive DefaultAgent (a pure while-True step loop). This
    # is the single line that fixes the 0-step wash: NEVER "interactive" here.
    agent = get_agent(model, env_obj, agent_cfg, default_type="default")
    _bc("agent built — entering agent.run()")

    result = agent.run(task)
    steps = getattr(agent, "n_calls", "?")
    cost = getattr(agent, "cost", "?")
    exit_status = result.get("exit_status") if isinstance(result, dict) else "?"
    _bc(f"headless agent finished: exit={exit_status} steps={steps} cost={cost}")
    print(f"[GT] headless agent finished: exit={exit_status} steps={steps} cost={cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
