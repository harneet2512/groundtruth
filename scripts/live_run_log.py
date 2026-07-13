#!/usr/bin/env python3
"""Live per-task run logger for a GHA SWE-bench-Live run.

GHA gates a *running* job's console logs until the job finishes, but each task uploads its
`ll-full-<task>` artifact the moment its job completes. This poller turns that into files you can
tail live:

  D:/gt_runs/<run_id>/logs/_LIVE_STATUS.log   — refreshed every poll: per-task state table + run state
  D:/gt_runs/<run_id>/logs/<task>.log         — written ONCE, the instant that task completes:
                                                 resolved / steps(action_count) / tokens / cost /
                                                 failure class / GT delivery + the trial_output.log tail
                                                 (what the agent actually did)

usage:  python scripts/live_run_log.py <run_id> [repo]
Runs until the run reaches a terminal state, then writes _DONE.log with the final aggregate.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO = sys.argv[2] if len(sys.argv) > 2 else "harneet2512/groundtruth"
RUN_ID = sys.argv[1]
GT = "D:/Groundtruth"
ROOT = f"D:/gt_runs/{RUN_ID}"
LOGDIR = f"{ROOT}/logs"
TMP = f"{ROOT}/_artifacts_tmp"
os.makedirs(LOGDIR, exist_ok=True)
os.makedirs(TMP, exist_ok=True)
STATUS = f"{LOGDIR}/_LIVE_STATUS.log"


def _resolve_bash() -> str:
    """Git-Bash explicitly — NEVER the Windows WSL `/bin/bash` shim (execvpe fails)."""
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.isfile(c):
            return c
    return "bash"


_BASH = _resolve_bash()


def _ghh(*args: str) -> str:
    """gh via the ghh.sh wrapper (harneet2512 token). Returns stdout ('' on error)."""
    try:
        p = subprocess.run([_BASH, "scripts/ghh.sh", *args], cwd=GT,
                           capture_output=True, text=True, timeout=180)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _view() -> dict:
    out = _ghh("run", "view", RUN_ID, "-R", REPO, "--json", "status,conclusion,jobs")
    try:
        return json.loads(out) if out.strip() else {}
    except Exception:
        return {}


def _trial_jobs(d: dict) -> list[dict]:
    return [j for j in d.get("jobs", []) if j.get("name") not in ("prepare", "summarize")]


def _write_status(d: dict) -> None:
    trial = _trial_jobs(d)
    lines = [f"=== {_utc()}  run {RUN_ID}  [{d.get('status')}/{d.get('conclusion') or '-'}] ==="]
    prep = [j for j in d.get("jobs", []) if j.get("name") == "prepare"]
    if prep:
        lines.append(f"prepare: {prep[0].get('status')}/{prep[0].get('conclusion') or '-'}")
    done = sum(1 for j in trial if j.get("status") == "completed")
    run = sum(1 for j in trial if j.get("status") == "in_progress")
    q = sum(1 for j in trial if j.get("status") == "queued")
    lines.append(f"trial: {done} done / {run} running / {q} queued  (of {len(trial)})")
    lines.append("-" * 60)
    for j in sorted(trial, key=lambda x: x.get("name", "")):
        nm = (j.get("name") or "").replace(" (python)", "").replace(" (all)", "")
        lines.append(f"  {j.get('status'):12} {str(j.get('conclusion') or '-'):10} {nm[:48]}")
    with open(STATUS, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _task_name(job: dict) -> str:
    # job name is the matrix task id, possibly with a suffix; strip trailing ' (lang)'.
    nm = (job.get("name") or "").split(" (")[0].strip()
    return nm


def _download_and_log(task: str) -> None:
    art = f"ll-full-{task}"
    dst = f"{TMP}/{task}"
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    _ghh("run", "download", RUN_ID, "-R", REPO, "-n", art, "-D", dst)
    log = f"{LOGDIR}/{task}.log"
    out = [f"=== {task}  (logged {_utc()}) ==="]
    # deep metrics
    dm = glob.glob(f"{dst}/gt_deep_metrics_*.json")
    if dm:
        try:
            m = json.load(open(dm[0], encoding="utf-8"))
            a = m.get("agent", {}) or {}
            e = m.get("efficiency", {}) or {}
            g = m.get("gt_delivery", {}) or {}
            out += [
                f"resolved: {m.get('resolved')}    has_patch: {m.get('has_patch')}",
                f"agent_started: {m.get('agent_started')}    STEPS(action_count): {a.get('action_count')}    first_edit: {a.get('first_edit_action')}",
                f"tokens: {e.get('llm_tokens_total')}    cost: ${e.get('llm_cost_usd') or e.get('recorded_cost_usd')}",
                f"failure_class: {m.get('failure_class')}    stage: {m.get('failure_stage')}    reason: {m.get('failure_reason')}",
                f"GT delivery: brief={g.get('brief_delivered')} evidence={g.get('evidence_delivered')} contract={g.get('contract_delivered')} scope={g.get('scope_delivered')} cochange={g.get('cochange_delivered')} verify={g.get('verify_delivered')} nudge={g.get('nudge_delivered')}",
                f"graph: nodes={m.get('graph_nodes')} edges={m.get('graph_edges')} verified_ratio={m.get('verified_edge_ratio')}  fts5_rows={m.get('fts5_row_count')}  semantic={m.get('semantic_enabled')}(dim={m.get('embedder_vector_dim')})",
            ]
        except Exception as ex:
            out.append(f"(deep-metrics parse error: {ex})")
    else:
        out.append("(no gt_deep_metrics json in artifact)")
    # reward
    for rp in glob.glob(f"{dst}/**/reward.json", recursive=True) + [f"{dst}/reward.txt"]:
        if os.path.isfile(rp):
            try:
                out.append(f"reward: {open(rp, encoding='utf-8').read().strip()[:80]}")
                break
            except Exception:
                pass
    # what the agent actually did — trial_output.log tail
    tlog = f"{dst}/trial_output.log"
    if os.path.isfile(tlog):
        try:
            tail = open(tlog, encoding="utf-8", errors="replace").read().splitlines()[-60:]
            out += ["--- trial_output.log (tail 60) ---", *tail]
        except Exception as ex:
            out.append(f"(trial_output.log read error: {ex})")
    with open(log, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    # free disk: drop the downloaded artifact (esp. graph.db) after extracting
    shutil.rmtree(dst, ignore_errors=True)


def main() -> int:
    logged: set[str] = set()
    while True:
        d = _view()
        if not d:
            time.sleep(30)
            continue
        _write_status(d)
        for j in _trial_jobs(d):
            if j.get("status") == "completed":
                t = _task_name(j)
                if t and t not in logged:
                    _download_and_log(t)
                    logged.add(t)
        if d.get("status") == "completed":
            # final aggregate
            agg = {"run": RUN_ID, "conclusion": d.get("conclusion"),
                   "tasks_logged": sorted(logged), "finished": _utc()}
            with open(f"{LOGDIR}/_DONE.log", "w", encoding="utf-8") as f:
                f.write(json.dumps(agg, indent=2) + "\n")
            return 0
        time.sleep(45)


if __name__ == "__main__":
    raise SystemExit(main())
