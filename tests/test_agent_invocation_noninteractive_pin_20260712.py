"""Pin: the paid-run agent invocation must be NON-INTERACTIVE, or every task washes to 0 steps.

BUG (run 29211807809 + micro-verify 29212976448 = 0 agent steps on every task): mini-swe-agent
2.4.5 hardcodes ``default_type=interactive`` (run/mini.py:101), so the bare CLI builds the
InteractiveAgent, which prompts for every step via a prompt_toolkit TUI (agents/interactive.py:60)
and, given no task, first asks what to do (run/mini.py:94). With no TTY in the container both
prompts block -> the process is Killed at 0 steps/0 tokens. ``-y`` only sets the yolo MODE on that
same interactive agent, not the class, so the earlier ``-y --exit-immediately`` fix did NOT bite.

Two-sided pin on the trial step that invokes the agent:
  * it MUST select the non-interactive DefaultAgent via ``--agent-class default`` (a revert to the
    bare/``-y`` interactive form reddens this);
  * the task MUST be guarded against empty via a ``-z`` fallback chain (a revert to a bare
    ``cat ... || echo`` chain -- which cannot detect an exist-but-empty issue file -- reddens this),
    so mini.py:94 can never fall into its interactive "what do you want to do" prompt.

This pins liveness at the source: "the agent actually takes steps" becomes an assertion on the
invocation, not a hope re-discovered only after a paid run comes back empty.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "swebench_live_lite_full.yml"


def _agent_invocation_step() -> str:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            run = step.get("run") if isinstance(step, dict) else None
            if run and "mini-swe-agent" in run and "--task" in run and "-o /gt_out/" in run:
                return run
    raise AssertionError("no trial step invokes mini-swe-agent with a --task and trajectory -o")


def _invocation_line(run: str) -> str:
    # the actual command line that starts the agent, NOT a comment mentioning it (a comment
    # containing "--agent-class default" must never be able to satisfy the pin).
    for raw in run.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("mini-swe-agent") or ("| mini-swe-agent" in line):
            return line
    raise AssertionError("no non-comment mini-swe-agent invocation line found")


def test_selects_the_noninteractive_default_agent() -> None:
    run = _agent_invocation_step()
    line = _invocation_line(run)
    assert "--agent-class default" in line, (
        "the paid-run agent invocation line must force --agent-class default (DefaultAgent); "
        f"without it mini-swe-agent 2.4.5 builds the InteractiveAgent and washes to 0 steps. line={line!r}"
    )


def test_does_not_drive_an_interactive_agent() -> None:
    run = _agent_invocation_step()
    # the exact regressed form: piping `yes ""` into an interactive `mini-swe-agent -y` (no class).
    assert 'yes "" 2>/dev/null | mini-swe-agent' not in run, (
        "reverted to feeding stdin to the interactive agent -- that is the 0-step wash"
    )


def test_task_is_guarded_against_an_empty_issue_file() -> None:
    run = _agent_invocation_step()
    # emptiness must be tested with -z (an exist-but-empty issue file makes `cat` exit 0, so a
    # `cat A || cat B` chain never falls through -> empty --task -> mini.py:94 interactive prompt).
    assert 'GT_TASK_TEXT' in run and '[ -z "$GT_TASK_TEXT" ]' in run, (
        "the --task value must fall back through a -z emptiness check, not a bare || chain"
    )
    assert '--task "$GT_TASK_TEXT"' in run, "the invocation must pass the guarded task variable"
