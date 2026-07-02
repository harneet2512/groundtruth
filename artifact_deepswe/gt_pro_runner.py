#!/usr/bin/env python3
"""Headless mini-swe-agent runner for the NON-pier SWE-bench Pro path.

Why this exists: swebench_pro_full.yml previously invoked the `mini-swe-agent`
console script, which maps to `minisweagent.run.mini:app` — the INTERACTIVE
prompt_toolkit/Textual TUI. In CI (no TTY) that entrypoint dies at display init
before making a single LLM call (observed: actions=0, llm_tokens=0, cost=$0 on
every task, agent rc=0). This driver uses mini-swe-agent's PROGRAMMATIC API
(`DefaultAgent`, which is already non-interactive) so the agent actually runs
headless. Pro does NOT use pier — the GT integration on this path is the
`gt_mini_patch` monkeypatch of `LocalEnvironment.execute` (imported below), which
appends GT evidence to every command observation the agent sees.

Env in:
  GT_PRO_MODEL     litellm model id (e.g. deepseek/deepseek-v4-flash)   [required]
  GT_PRO_ISSUE     path to the issue text file (default /gt_out/issue.txt)
  GT_PRO_REPO      repo working dir inside the container (default /app)
  GT_STEP_LIMIT    agent step cap (default 250)
  GT_PRO_COST_LIMIT   $ cap (default 2.0; 0 disables)
  GT_PRO_PATCH_OUT patch path (default /tmp/patch.txt)

Writes the agent's git diff to GT_PRO_PATCH_OUT and prints a one-line
GT_PRO_RESULT summary (exit_status, steps, cost) the workflow greps for.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

# GT integration (non-pier path): importing gt_mini_patch runs its _install(),
# which wraps LocalEnvironment.execute to append GT evidence. Best-effort — if it
# can't import (missing GT deps in this venv) the agent still runs, just without
# the evidence augmentation, and we log it rather than crash.
for _p in ("/opt/gt", "/opt/gt/src"):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import gt_mini_patch  # noqa: F401  -> _install() patches LocalEnvironment.execute
    print("[gt_pro_runner] gt_mini_patch imported (GT evidence augmentation ON)")
except Exception as e:  # noqa: BLE001
    print(f"[gt_pro_runner] WARN gt_mini_patch import failed (evidence OFF): {type(e).__name__}: {e}")


def main() -> int:
    model_name = os.environ.get("GT_PRO_MODEL", "").strip()
    if not model_name:
        print("[gt_pro_runner] FATAL: GT_PRO_MODEL unset")
        return 2
    issue_path = os.environ.get("GT_PRO_ISSUE", "/gt_out/issue.txt")
    repo = os.environ.get("GT_PRO_REPO", "/app")
    patch_out = os.environ.get("GT_PRO_PATCH_OUT", "/tmp/patch.txt")
    step_limit = int(os.environ.get("GT_STEP_LIMIT", "250") or "250")
    cost_limit = float(os.environ.get("GT_PRO_COST_LIMIT", "2.0") or "2.0")

    try:
        issue = open(issue_path, encoding="utf-8").read().strip()
    except Exception:  # noqa: BLE001
        issue = ""
    if not issue:
        issue = "Fix the issue described in the repository."

    from minisweagent.agents.default import DefaultAgent
    from minisweagent.config import builtin_config_dir, get_config_from_spec
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models import get_model

    # mini-swe-agent's own SWE-bench config: system/instance templates tuned for
    # repo-fix tasks. We override the model id and the step/cost caps.
    cfg = get_config_from_spec(str(builtin_config_dir / "benchmarks" / "swebench.yaml"))
    agent_cfg = dict(cfg.get("agent", {}) or {})
    agent_cfg["step_limit"] = step_limit
    agent_cfg["cost_limit"] = cost_limit

    model = get_model(model_name, config=cfg.get("model", {}) or {})
    env = LocalEnvironment(cwd=repo)  # patched by gt_mini_patch above
    agent = DefaultAgent(model, env, **agent_cfg)
    print(f"[gt_pro_runner] running model={model_name} step_limit={step_limit} "
          f"cost_limit={cost_limit} repo={repo} issue_chars={len(issue)}")

    exit_status = "unknown"
    try:
        result = agent.run(issue)
        exit_status = (result or {}).get("exit_status", "completed") if isinstance(result, dict) else "completed"
        print(f"[gt_pro_runner] agent finished: exit_status={exit_status}")
    except Exception as e:  # noqa: BLE001 -- surface the REAL error (was silently swallowed before)
        exit_status = f"error:{type(e).__name__}"
        print(f"[gt_pro_runner] agent.run raised: {type(e).__name__}: {e}")
        traceback.print_exc()

    # Telemetry the probe checks: real LLM activity (llm_calls>0) means the harness WORKS.
    steps = getattr(agent, "n_calls", None)
    if steps is None:
        steps = len(getattr(agent, "messages", []) or [])
    llm_calls = cost = None
    try:
        from minisweagent.models import GLOBAL_MODEL_STATS
        llm_calls = getattr(GLOBAL_MODEL_STATS, "n_calls", None)
        cost = getattr(GLOBAL_MODEL_STATS, "cost", None)
    except Exception:  # noqa: BLE001
        pass
    print(f"GT_PRO_RESULT exit_status={exit_status} steps={steps} llm_calls={llm_calls} cost={cost}")

    # Capture the patch (uncorrupted git diff) for the verifier.
    try:
        subprocess.run(
            f"cd {repo} && git add -A 2>/dev/null; git diff --cached > {patch_out} 2>/dev/null",
            shell=True, check=False,
        )
        sz = os.path.getsize(patch_out) if os.path.exists(patch_out) else 0
        print(f"[gt_pro_runner] patch bytes={sz} -> {patch_out}")
    except Exception as e:  # noqa: BLE001
        print(f"[gt_pro_runner] patch capture failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
