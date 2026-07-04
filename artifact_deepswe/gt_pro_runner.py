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

import importlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

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


def _verify_graph_witness() -> None:
    """A3: close the Pro certify->consume chain (the deepswe path enforces this at
    gt_agent.py:1163-1233; the Pro path had NO witness, so nothing verified the agent read
    the graph the gates certified). Fingerprint the graph the agent will actually consume
    (gt_mini_patch._db_path() == GT_HOST_GRAPH_DB, the untouched substrate mount) and compare
    to the substrate's LSP-resolved authority hash (graph_hash_after_lsp in the lsp/graph
    certificate). Under a REQUIRED-stack / proof run a divergent or unprovable consume
    HARD-STOPS before any LLM spend. Best-effort (print-only) on unflagged iteration runs.
    Emits the same [GT_META] graph_witness line format as gt_agent so task_truth reconciles it.
    """
    proof = (os.environ.get("GT_PROOF_MODE") == "1"
             or os.environ.get("GT_REQUIRE_FULL_STACK") == "1")

    def _stop(reason: str) -> None:
        print(f"[gt_pro_runner] GRAPH_WITNESS_FAIL: {reason}", flush=True)
        sys.exit(1)

    # 1. Resolve the graph the agent consumes — reuse gt_mini_patch's own resolver so we
    #    fingerprint EXACTLY what the per-turn pillars read (not a divergent path).
    resolved_db = ""
    try:
        import gt_mini_patch as _gmp  # already imported at module load
        resolved_db = _gmp._db_path() or ""
    except Exception as e:  # noqa: BLE001
        resolved_db = os.environ.get("GT_HOST_GRAPH_DB", "") or ""
        if proof and not resolved_db:
            _stop(f"cannot resolve consumed graph ({type(e).__name__}: {e})")
    if not resolved_db or not os.path.isfile(resolved_db):
        if proof:
            _stop(f"consumed graph absent: {resolved_db!r}")
        print(f"[gt_pro_runner] graph_witness: no graph to fingerprint ({resolved_db!r}) — skip",
              flush=True)
        return

    # 2. Canonical READ-ONLY fingerprint of the consumed graph.
    try:
        from groundtruth.runtime import proof as _pf
        hook_hash = _pf.graph_edges_hash(resolved_db) or ""
    except Exception as e:  # noqa: BLE001
        if proof:
            _stop(f"graph_edges_hash unavailable ({type(e).__name__}: {e})")
        print(f"[gt_pro_runner] graph_witness: hash unavailable ({e}) — skip", flush=True)
        return
    if not hook_hash and proof:
        _stop(f"graph_edges_hash_empty for {resolved_db!r}")

    # 3. Substrate authority hash from the certificate(s).
    cert_dir = os.environ.get("GT_CERT_DIR", "") or ""
    post_lsp_hash = ""
    for _name in ("lsp_certificate.json", "graph_certificate.json"):
        for _base in (cert_dir, "/gt_artifacts", "/tmp/gt"):
            if not _base:
                continue
            _p = os.path.join(_base, _name)
            if os.path.isfile(_p):
                try:
                    _c = json.load(open(_p, encoding="utf-8"))
                    post_lsp_hash = _c.get("graph_hash_after_lsp") or _c.get("graph_hash") or ""
                except Exception:  # noqa: BLE001
                    post_lsp_hash = ""
                if post_lsp_hash:
                    break
        if post_lsp_hash:
            break
    hash_match = bool(post_lsp_hash) and (hook_hash == post_lsp_hash)

    # 4. Emit the canonical witness line (same format as gt_agent -> task_truth reconciles).
    _fmt = None
    try:
        _fmt = getattr(importlib.import_module("graph_certificate"), "format_graph_witness", None)
    except Exception:  # noqa: BLE001
        _fmt = None
    if _fmt is not None:
        base = _fmt(host_resolved_graph_db=resolved_db, hook_graph_db=resolved_db,
                    hook_graph_hash=hook_hash, prebuilt_active="true")
    else:
        base = (f"[GT_META] graph_witness host_resolved_graph_db={resolved_db} "
                f"hook_graph_db={resolved_db} hook_graph_hash={hook_hash} _gt_prebuilt_active=true")
    print(f"{base} | graph_hash_after_lsp={post_lsp_hash or '(absent)'} "
          f"hook_graph_hash_matches_post_lsp={hash_match}", flush=True)

    # 5. FAIL-CLOSED under proof/required-stack: a divergent or unprovable consume must
    #    HARD-STOP before any paid LLM spend (mirrors gt_agent.py:1225-1244).
    if proof and post_lsp_hash and not hash_match:
        _stop(f"GRAPH_FAIL_HASH_MISMATCH hook={hook_hash} != post_lsp={post_lsp_hash} "
              f"— consumed graph diverges from the substrate's certified graph")
    if proof and not post_lsp_hash:
        _stop(f"post_lsp_hash_absent cert_dir={cert_dir!r} — cannot prove the consumed graph "
              f"== the substrate's LSP-resolved graph")


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

    instance_id = (os.environ.get("GT_PRO_INSTANCE_ID", "") or "task").strip()

    try:
        issue = open(issue_path, encoding="utf-8").read().strip()
    except Exception:  # noqa: BLE001
        issue = ""
    if not issue:
        issue = "Fix the issue described in the repository."

    # Q1 PROACTIVE DELIVERY: prepend the GT brief (generated at index time) to the task so the
    # agent sees the curated evidence BEFORE its first search. brief.txt is in the RO cert dir.
    # This is the leakage-safe curated brief (localization + graph-map + contracts) — NOT test names.
    brief = ""
    _cert = os.environ.get("GT_CERT_DIR", "")
    _brief_candidates = [
        os.environ.get("GT_PRO_BRIEF", ""),
        os.path.join(_cert, "brief.txt") if _cert else "",
        os.path.join(_cert, "gt_artifacts", "brief.txt") if _cert else "",
        "/gt_out/brief.txt",
    ]
    for _bp in _brief_candidates:
        try:
            if _bp and os.path.exists(_bp):
                brief = open(_bp, encoding="utf-8").read().strip()
                if brief:
                    break
        except Exception:  # noqa: BLE001
            continue
    if brief:
        task = ("<gt-brief> GroundTruth evidence — reason over these candidate targets, callers, and "
                "contracts BEFORE searching; confirm the exact edit target with grep.\n"
                f"{brief}\n</gt-brief>\n\n{issue}")
        print(f"[gt_pro_runner] proactive brief prepended ({len(brief)} bytes)")
    else:
        task = issue
        print("[gt_pro_runner] WARN no brief.txt found — running with issue only (Q1 not delivered)")

    # A3: verify certify->consume BEFORE building the model / spending any LLM calls.
    _verify_graph_witness()

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
        result = agent.run(task)
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

    # Save the trajectory for the gt_math §4 audit (rows 25/26 — Q1 time-of-delivery, Q3
    # consumption). Same {iid}.traj.json shape gt_verified_agent writes, so the gt-math reader
    # finds it on the no-pier Pro path.
    try:
        traj_dir = os.environ.get("GT_PRO_TRAJ_DIR", "/gt_out")
        os.makedirs(traj_dir, exist_ok=True)
        traj_path = Path(traj_dir) / f"{instance_id}.traj.json"
        agent.save(traj_path, {"info": {"exit_status": exit_status, "instance_id": instance_id,
                    "llm_calls": llm_calls, "cost": cost, "steps": steps,
                    "brief_delivered": bool(brief)}})
        print(f"[gt_pro_runner] saved trajectory: {traj_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[gt_pro_runner] traj save failed: {type(e).__name__}: {e}")

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
