"""Pin: the paid-run cost guard must actually PASS against the real pier config.

BUG (killed run 29211603701 + earlier 29207999330, mis-blamed on "stale state"): the prepare
"Cost guard" step used `re.search(r"^\\s*step_limit:\\s*([0-9]+)\\s*$", ...)` — anchored right after
the digits. The real config line is `step_limit: 150  # PAIRED-CONTROL ...` (a valid YAML inline
comment), so `\\s*$` never matched → the guard aborted "agent.step_limit missing/zero" though the
value was set to 150 the whole time → prepare FAILED → all 30 trial jobs skipped → 0 paid work.

Two-sided pin:
  * the guard regex in the workflow must be COMMENT-TOLERANT (contains the `(?:#.*)?` allowance),
    so a revert to the strict `\\s*$` form reddens this test.
  * the real config, parsed by that same comment-tolerant regex, must yield step_limit>0 AND
    cost_limit>0 — so dropping/zeroing either value in deepswe_gt_pier.yaml also reddens it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_CFG = _ROOT / "artifact_deepswe" / "gt_integration" / "deepswe_gt_pier.yaml"

_SL = re.compile(r"^\s*step_limit:\s*([0-9]+)\s*(?:#.*)?$", re.M)
_CL = re.compile(r"^\s*cost_limit:\s*([0-9.]+)\s*(?:#.*)?$", re.M)


def _cost_guard_run() -> str:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and "Cost guard" in (step.get("name") or ""):
                return step["run"]
    raise AssertionError("Cost guard step not found in swebench_live_lite_full.yml")


def test_guard_regex_tolerates_inline_comment() -> None:
    run = _cost_guard_run()
    # the two regexes must carry the trailing-comment allowance, not the strict end-anchor
    assert "(?:#.*)?" in run, "cost-guard regex reverted to comment-intolerant form (the B4 bug)"
    assert re.search(r"step_limit:.*\(\?:#\.\*\)\?", run), "step_limit regex must allow a trailing comment"
    assert re.search(r"cost_limit:.*\(\?:#\.\*\)\?", run), "cost_limit regex must allow a trailing comment"


def test_real_config_passes_the_guard() -> None:
    txt = _CFG.read_text(encoding="utf-8")
    sl, cl = _SL.search(txt), _CL.search(txt)
    assert sl and int(sl.group(1)) > 0, "deepswe_gt_pier.yaml step_limit missing/zero (as the guard sees it)"
    assert cl and float(cl.group(1)) > 0, "deepswe_gt_pier.yaml cost_limit missing/zero (as the guard sees it)"


def test_config_step_limit_is_under_agent() -> None:
    # the guard's error text says "agent.step_limit" — keep the value under the agent: mapping so a
    # proper YAML consumer and the regex agree.
    cfg = yaml.safe_load(_CFG.read_text(encoding="utf-8"))
    agent = cfg.get("agent", {})
    assert int(agent.get("step_limit", 0)) > 0, "step_limit must live under agent: with a positive value"
    assert float(agent.get("cost_limit", 0)) > 0, "cost_limit must live under agent: with a positive value"
