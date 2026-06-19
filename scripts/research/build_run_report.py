#!/usr/bin/env python3
"""build_run_report.py — assemble a RUN_REPORT/ artifact bundle from a run directory.

Spec: docs/RESEARCH_ARTIFACT_SPEC_20260610.md
Stdlib-only. Three input layouts (sniffed): vm-sweep (gt_agent_run.sh OUT_DIR),
pier-jobs (pier jobs/ tree), gha-openhands (GHA artifact with output.jsonl + gt_debug/).

Honesty rules (binding, tested):
  - a field absent from input NEVER appears as a number in output ("NOT COLLECTED")
  - telemetry (emitted) is never reported as agent-observation (consumed/correct)
  - n<5 cells report counts (k/n), never rates
  - every table carries a sources: footer

Usage:
  python scripts/research/build_run_report.py <run_dir> [--out DIR] [--ledgers DIR]
                                              [--claim TEXT] [--baseline RUN_DIR]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform as _platform
import random
import re
import sys
import time
from pathlib import Path

ANALYZER_VERSION = "1.0.0"
NOT_COLLECTED = "NOT COLLECTED"
BOOTSTRAP_SEED = 20260610
BOOTSTRAP_N = 2000
MIN_N_FOR_RATES = 5

# Runner-level classes that are infrastructure, not agent/GT behavior (gt_agent_run.sh + pier).
INFRA_FAILURE_CLASSES = {
    "TASK_DIR_MISSING", "TASK_IMAGE_PULL_FAIL", "DISK_LOW", "SRC_EXTRACT_FAIL",
    "GT_ISSUE_MISSING", "GT_RUN_PROOF_FAIL", "GT_ARTIFACT_MISSING", "PIER_TIMEOUT",
    "PIER_RUN_FAIL", "DEEPSWE_ADAPTER_FAIL",
}

# ---------------------------------------------------------------- small utils


def jload(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def jsonl_iter(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield lineno, json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def f8(v):
    """8-dp string for a number; NOT COLLECTED passthrough for anything else."""
    if is_num(v):
        return f"{v:.8f}"
    return NOT_COLLECTED


def cell(v):
    """Markdown cell: numbers verbatim, None/missing -> NOT COLLECTED."""
    if v is None:
        return NOT_COLLECTED
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.8f}"
    return str(v)


def distribution(values):
    """min/p25/median/p75/max over a list of numbers; None when empty."""
    vals = sorted(v for v in values if is_num(v))
    if not vals:
        return None
    n = len(vals)

    def pct(p):
        if n == 1:
            return vals[0]
        k = p * (n - 1)
        lo = int(k)
        hi = min(lo + 1, n - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

    return {"n": n, "min": vals[0], "p25": pct(0.25), "median": pct(0.5),
            "p75": pct(0.75), "max": vals[-1]}


def bootstrap_ci_median(values, seed=BOOTSTRAP_SEED, n_boot=BOOTSTRAP_N):
    """Percentile bootstrap 95% CI on the median. Only call with n>=MIN_N_FOR_RATES."""
    vals = [v for v in values if is_num(v)]
    if len(vals) < MIN_N_FOR_RATES:
        return None
    rng = random.Random(seed)
    meds = []
    for _ in range(n_boot):
        sample = [vals[rng.randrange(len(vals))] for _ in range(len(vals))]
        sample.sort()
        m = len(sample) // 2
        meds.append(sample[m] if len(sample) % 2 else (sample[m - 1] + sample[m]) / 2)
    meds.sort()
    return {"ci_lo": meds[int(0.025 * n_boot)], "ci_hi": meds[int(0.975 * n_boot)],
            "seed": seed, "n_boot": n_boot}


def rate_or_count(k, n):
    """Spec 6.3: n<5 -> counts not rates."""
    if n is None or n == 0:
        return NOT_COLLECTED
    if n < MIN_N_FOR_RATES:
        return f"{k}/{n}"
    return f"{k}/{n} ({k / n:.8f})"


# ---------------------------------------------------------------- layout sniff


def sniff_layout(run_dir: Path) -> str:
    if (run_dir / "row.json").exists() or list(run_dir.glob("*/row.json")):
        return "vm-sweep"
    for pat in ("jobs/*/*/result.json", "*/jobs/*/*/result.json",
                "artifacts/*/jobs/*/*/result.json", "result.json"):
        hits = list(run_dir.glob(pat))
        if hits and any("trial_name" in (jload(h) or {}) for h in hits[:3]):
            return "pier-jobs"
    if (run_dir / "output.jsonl").exists() or (run_dir / "gt_debug").exists() \
            or list(run_dir.glob("artifacts/*/gt_debug")):
        return "gha-openhands"
    return "unknown"


# ---------------------------------------------------------------- task record

def new_task(instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "language": None, "model": None, "run_id": None,
        "resolved": None, "has_patch": None, "outcome": None,
        "failure_class": None, "outcome_class": None, "exit_status": None,
        "n_agent_steps": None, "max_iter": None,
        "first_edit_action": None, "edit_to_gold_action": None,
        "gt_injected_tokens": None,
        "llm_cost_usd": None, "llm_calls": None,
        "llm_tokens_in": None, "llm_tokens_out": None,
        "llm_tokens_cached": None, "llm_cache_hit_tokens": None,
        "llm_cache_miss_tokens": None,
        "gt_injection_overhead_pct": None,
        "wall_s": None, "timings_s": None,
        "per_layer": {},            # layer -> telemetry counters
        "layer_exemplars": {},      # layer -> {pointer, snippet}
        "suppression_reasons": {},  # layer -> {reason: count}
        "causality": None,          # scorecard tier2_causality (task-level, agent-observation)
        "legitimacy": None,         # scorecard tier6
        "graph": {},                # nodes/edges/det_pct/verified_edge_ratio/resolution dist...
        "lsp": {},                  # resolved/residual/no-op
        "embedder": {},
        "integrity": {
            "substrate_digest": None, "gt_git_commit": None, "task_repo_commit": None,
            "graph_db_sha256": None, "graph_hash_post_lsp": None,
            "witness_gt_prebuilt_active": None, "witness_hook_hash_match": None,
            "trajectory_path": None, "trajectory_sha256": None,
            "ledger_path": None,
        },
        "image": None, "model_params": None,
        "provenance": {},   # field -> source path[:line]
    }


def prov(task: dict, field: str, source) -> None:
    task["provenance"][field] = str(source)


# ---------------------------------------------------------------- shared parsers


def parse_deep_metrics(task: dict, dm_path: Path) -> None:
    dm = jload(dm_path)
    if not dm:
        return
    src = dm_path
    direct = {
        "outcome": "outcome", "resolved": "resolved", "has_patch": "has_patch",
        "gt_injected_tokens": "gt_injected_tokens_total",
    }
    for field, key in direct.items():
        if key in dm and dm[key] is not None and task.get(field) is None:
            task[field] = dm[key]
            prov(task, field, src)
    if dm.get("git_commit") and not task["integrity"]["gt_git_commit"]:
        task["integrity"]["gt_git_commit"] = dm["git_commit"]
        prov(task, "integrity.gt_git_commit", src)
    eff = dm.get("efficiency") or {}
    for field, key in (("llm_cost_usd", "llm_cost_usd"), ("llm_calls", "llm_calls"),
                       ("llm_tokens_in", "llm_tokens_in"), ("llm_tokens_out", "llm_tokens_out"),
                       ("llm_tokens_cached", "llm_tokens_cached"),
                       ("llm_cache_hit_tokens", "llm_cache_hit_tokens"),
                       ("llm_cache_miss_tokens", "llm_cache_miss_tokens"),
                       ("gt_injection_overhead_pct", "gt_injection_overhead_pct")):
        if key in eff and eff[key] is not None:
            task[field] = eff[key]
            prov(task, field, src)
    agent = dm.get("agent") or {}
    for field, key in (("n_agent_steps", "action_count"),
                       ("first_edit_action", "first_edit_action"),
                       ("edit_to_gold_action", "edit_to_gold_action")):
        if key in agent and agent[key] is not None and task.get(field) is None:
            task[field] = agent[key]
            prov(task, field, src)
    for layer, counters in (dm.get("per_layer") or {}).items():
        slot = task["per_layer"].setdefault(layer, {})
        for k in ("eligible", "emitted", "suppressed", "rendered_tokens_total",
                  "utilization_score"):
            if k in counters and counters[k] is not None:
                slot[k] = counters[k]
        prov(task, f"per_layer.{layer}", src)
    g = task["graph"]
    for k_out, k_in in (("nodes", "graph_nodes"), ("edges", "graph_edges"),
                        ("verified_edge_count", "verified_edge_count"),
                        ("verified_edge_ratio", "verified_edge_ratio"),
                        ("fts5_row_count", "fts5_row_count"),
                        ("assertion_count", "assertion_count"),
                        ("linked_assertion_count", "linked_assertion_count"),
                        ("lsp_enriched_edge_count", "lsp_enriched_edge_count")):
        if k_in in dm and dm[k_in] is not None and k_out not in g:
            g[k_out] = dm[k_in]
            prov(task, f"graph.{k_out}", src)
    if dm.get("lsp_server_name"):
        task["lsp"].setdefault("server_name", dm["lsp_server_name"])
        prov(task, "lsp.server_name", src)
    emb = task["embedder"]
    for k_out, k_in in (("vector_dim", "embedder_vector_dim"),
                        ("nonzero", "embedder_nonzero"),
                        ("semantic_enabled", "semantic_enabled")):
        if k_in in dm and dm[k_in] is not None and k_out not in emb:
            emb[k_out] = dm[k_in]
            prov(task, f"embedder.{k_out}", src)


def parse_layer_events(task: dict, le_path: Path) -> None:
    for lineno, ev in jsonl_iter(le_path):
        layer = ev.get("layer")
        if not layer:
            continue
        slot = task["per_layer"].setdefault(layer, {})
        slot["events_eligible"] = slot.get("events_eligible", 0) + (1 if ev.get("eligible") else 0)
        slot["events_emitted"] = slot.get("events_emitted", 0) + (1 if ev.get("emitted") else 0)
        slot["events_suppressed"] = slot.get("events_suppressed", 0) + (1 if ev.get("suppressed") else 0)
        if ev.get("suppressed") and ev.get("suppression_reason"):
            reasons = task["suppression_reasons"].setdefault(layer, {})
            reasons[ev["suppression_reason"]] = reasons.get(ev["suppression_reason"], 0) + 1
        if ev.get("emitted") and ev.get("rendered_text") and layer not in task["layer_exemplars"]:
            snippet = " ".join(str(ev["rendered_text"]).split())[:120]
            task["layer_exemplars"][layer] = {
                "pointer": f"{le_path.name}:{lineno}", "snippet": snippet,
                "iter": ev.get("iter"),
            }
        if ev.get("max_iter") is not None and task.get("max_iter") is None:
            task["max_iter"] = ev["max_iter"]
            prov(task, "max_iter", f"{le_path}:{lineno}")
    if task["per_layer"]:
        prov(task, "layer_events", le_path)


def parse_scorecard(task: dict, sc_path: Path) -> None:
    sc = jload(sc_path)
    if not sc:
        return
    sc_task = sc.get("task") or sc.get("instance_id") or ""
    if sc_task and sc_task not in task["instance_id"] and task["instance_id"] not in sc_task:
        return
    if sc.get("tier2_causality") is not None:
        task["causality"] = sc["tier2_causality"]
        prov(task, "causality", sc_path)
    if sc.get("tier6_legitimacy") is not None:
        task["legitimacy"] = sc["tier6_legitimacy"]
        prov(task, "legitimacy", sc_path)
    t1 = sc.get("tier1_outcome") or {}
    if task["resolved"] is None and "resolved" in t1:
        task["resolved"] = bool(t1["resolved"])
        prov(task, "resolved", sc_path)
    if sc.get("run_id") and not task["run_id"]:
        task["run_id"] = sc["run_id"]
        prov(task, "run_id", sc_path)


def parse_graph_certificate(task: dict, gc_path: Path) -> None:
    gc = jload(gc_path)
    if not gc:
        return
    g = task["graph"]
    for k_out, k_in in (("nodes", "nodes_count"), ("edges", "edges_count"),
                        ("calls_count", "calls_count"), ("det_pct", "det_pct"),
                        ("name_match_count", "name_match_count"),
                        ("deterministic_count", "deterministic_count"),
                        ("fts5_row_count", "fts5_row_count"),
                        ("fts5_match_probe_ok", "fts5_match_probe_ok"),
                        ("assertion_count", "assertions_count"),
                        ("resolution_method_dist", "resolution_method_dist"),
                        ("schema_version", "schema_version")):
        if k_in in gc and gc[k_in] is not None:
            g[k_out] = gc[k_in]
            prov(task, f"graph.{k_out}", gc_path)


def parse_lsp_certificate(task: dict, lc_path: Path) -> None:
    lc = jload(lc_path)
    if not lc:
        return
    for k in ("resolved", "residual", "verified", "corrected", "deleted",
              "lsp_no_op_valid", "lsp_no_op_reason", "resolve_frac"):
        if k in lc and lc[k] is not None:
            task["lsp"][k] = lc[k]
            prov(task, f"lsp.{k}", lc_path)


def attach_trajectory(task: dict, traj_path: Path | None) -> None:
    if traj_path and traj_path.exists():
        task["integrity"]["trajectory_path"] = str(traj_path)
        task["integrity"]["trajectory_sha256"] = sha256_file(traj_path)
        prov(task, "integrity.trajectory_sha256", traj_path)


def attach_ledger(task: dict, ledger_dir: Path | None) -> None:
    if not ledger_dir or not ledger_dir.is_dir():
        return
    iid = task["instance_id"].lower()
    for md in sorted(ledger_dir.glob("*.md")):
        stem = md.stem.lower()
        if stem == "readme":
            continue
        if stem in iid or iid in stem:
            task["integrity"]["ledger_path"] = str(md)
            prov(task, "integrity.ledger_path", md)
            return


# ---------------------------------------------------------------- collectors


def collect_gha(run_dir: Path, ledger_dir: Path | None) -> list[dict]:
    tasks = []
    bases = [run_dir] + sorted(p.parent for p in run_dir.glob("artifacts/*/gt_debug"))
    seen = set()
    for base in bases:
        gt_debug = base / "gt_debug"
        if not gt_debug.is_dir():
            continue
        for dm_path in sorted(gt_debug.glob("gt_deep_metrics_*.json")):
            iid = dm_path.stem.replace("gt_deep_metrics_", "")
            if iid in seen:
                continue
            seen.add(iid)
            task = new_task(iid)
            parse_deep_metrics(task, dm_path)
            le = gt_debug / f"gt_layer_events_{iid}.jsonl"
            if le.exists():
                parse_layer_events(task, le)
            for sc in (base / "scorecard.json", run_dir / "scorecard.json"):
                if sc.exists():
                    parse_scorecard(task, sc)
                    break
            for er_path in (base / "eval_result.json", run_dir / "eval_result.json"):
                er = jload(er_path)
                if er:
                    if iid in (er.get("resolved_ids") or []):
                        task["resolved"] = True
                        prov(task, "resolved", er_path)
                    elif iid in (er.get("unresolved_ids") or []):
                        task["resolved"] = False
                        prov(task, "resolved", er_path)
                    break
            traj = base / "output.jsonl"
            if not traj.exists():
                traj = run_dir / "output.jsonl"
            attach_trajectory(task, traj if traj.exists() else None)
            attach_ledger(task, ledger_dir)
            tasks.append(task)
    return tasks


def collect_pier_trial(trial_dir: Path, ledger_dir: Path | None) -> dict | None:
    res = jload(trial_dir / "result.json")
    if not res:
        return None
    task_name = res.get("task_name") or ""
    iid = task_name.split("/")[-1] if task_name else trial_dir.name
    task = new_task(iid)
    prov(task, "instance_id", trial_dir / "result.json")
    cfg = res.get("config") or {}
    agent_cfg = cfg.get("agent") or {}
    if agent_cfg.get("model_name"):
        task["model"] = agent_cfg["model_name"]
        prov(task, "model", trial_dir / "result.json")
    if res.get("task_checksum"):
        task["integrity"]["task_repo_commit"] = None  # checksum is not a commit; keep separate
        task["graph"]["task_checksum"] = res["task_checksum"]
        prov(task, "graph.task_checksum", trial_dir / "result.json")
    exc = trial_dir / "exception.txt"
    if exc.exists() and exc.stat().st_size > 0:
        task["failure_class"] = "PIER_EXCEPTION"
        prov(task, "failure_class", exc)
    reward_f = trial_dir / "verifier" / "reward.txt"
    if reward_f.exists():
        try:
            reward = reward_f.read_text(encoding="utf-8").strip()
            task["resolved"] = reward == "1"
            prov(task, "resolved", reward_f)
        except OSError:
            pass
    patch_f = trial_dir / "artifacts" / "model.patch"
    if patch_f.exists():
        task["has_patch"] = patch_f.stat().st_size > 0
        prov(task, "has_patch", patch_f)
    agent_dir = trial_dir / "agent"
    traj = None
    if agent_dir.is_dir():
        cands = sorted(agent_dir.glob("*.txt"), key=lambda p: p.stat().st_size, reverse=True)
        traj = cands[0] if cands else None
    attach_trajectory(task, traj)
    # deep metrics, if the runner dropped one next to the trial
    for dm in trial_dir.glob("gt_deep_metrics_*.json"):
        parse_deep_metrics(task, dm)
    attach_ledger(task, ledger_dir)
    return task


def collect_pier(run_dir: Path, ledger_dir: Path | None) -> list[dict]:
    tasks = []
    seen = set()
    for pat in ("jobs/*/*/result.json", "*/jobs/*/*/result.json",
                "artifacts/*/jobs/*/*/result.json", "result.json"):
        for res_path in sorted(run_dir.glob(pat)):
            trial_dir = res_path.parent
            if trial_dir in seen:
                continue
            seen.add(trial_dir)
            t = collect_pier_trial(trial_dir, ledger_dir)
            if t:
                tasks.append(t)
    return tasks


def collect_vm_task(task_dir: Path, ledger_dir: Path | None) -> dict | None:
    row = jload(task_dir / "row.json")
    if not row:
        return None
    iid = row.get("instance_id") or task_dir.name
    task = new_task(iid)
    src = task_dir / "row.json"
    for field, key in (("language", "language"), ("model", "model"), ("image", "image"),
                       ("run_id", "run_id"), ("outcome_class", "outcome_class"),
                       ("exit_status", "exit_status"), ("n_agent_steps", "n_agent_steps")):
        if row.get(key) not in (None, ""):
            task[field] = row[key]
            prov(task, field, src)
    if row.get("failure_class"):
        task["failure_class"] = row["failure_class"]
        prov(task, "failure_class", src)
    if row.get("reward") is not None:
        task["resolved"] = row["reward"] == 1
        prov(task, "resolved", src)
    if row.get("outcome_class") == "RESOLVED" and task["resolved"] is None:
        task["resolved"] = True
        prov(task, "resolved", src)
    integ = task["integrity"]
    for field, key in (("substrate_digest", "substrate_digest"),
                       ("gt_git_commit", "gt_git_commit"),
                       ("task_repo_commit", "task_repo_commit"),
                       ("witness_gt_prebuilt_active", "gt_prebuilt_active"),
                       ("witness_hook_hash_match", "hook_hash_match")):
        if row.get(key) not in (None, ""):
            integ[field] = row[key]
            prov(task, f"integrity.{field}", src)
    if isinstance(row.get("timings_s"), dict):
        task["timings_s"] = row["timings_s"]
        agent_s = row["timings_s"].get("agent")
        if is_num(agent_s) and agent_s >= 0:
            task["wall_s"] = agent_s
        prov(task, "timings_s", src)
    for dm in task_dir.glob("gt_deep_metrics_*.json"):
        parse_deep_metrics(task, dm)
    gt_dir = task_dir / "gt"
    if (gt_dir / "graph_certificate.json").exists():
        parse_graph_certificate(task, gt_dir / "graph_certificate.json")
    if (gt_dir / "lsp_certificate.json").exists():
        parse_lsp_certificate(task, gt_dir / "lsp_certificate.json")
    if (gt_dir / "graph.db").exists():
        integ["graph_db_sha256"] = sha256_file(gt_dir / "graph.db")
        prov(task, "integrity.graph_db_sha256", gt_dir / "graph.db")
    manifest = jload(gt_dir / "run_manifest.json") or {}
    for key in ("graph_hash_after_lsp", "graph_hash", "graph_sha256"):
        if manifest.get(key):
            integ["graph_hash_post_lsp"] = manifest[key]
            prov(task, "integrity.graph_hash_post_lsp", gt_dir / "run_manifest.json")
            break
    # trajectory: pier trial agent transcript first, trial_output.log fallback
    traj = None
    cands = sorted(task_dir.glob("pier/jobs/*/*/agent/*.txt"),
                   key=lambda p: p.stat().st_size, reverse=True)
    if cands:
        traj = cands[0]
    elif (task_dir / "trial_output.log").exists():
        traj = task_dir / "trial_output.log"
    attach_trajectory(task, traj)
    # nested pier trial result (resolved fallback via reward)
    for res_path in sorted(task_dir.glob("pier/jobs/*/*/result.json")):
        nested = collect_pier_trial(res_path.parent, None)
        if nested:
            if task["resolved"] is None and nested["resolved"] is not None:
                task["resolved"] = nested["resolved"]
                prov(task, "resolved", res_path.parent / "verifier" / "reward.txt")
            if task["has_patch"] is None and nested["has_patch"] is not None:
                task["has_patch"] = nested["has_patch"]
                prov(task, "has_patch", res_path.parent / "artifacts" / "model.patch")
            if task["model"] is None and nested["model"] is not None:
                task["model"] = nested["model"]
                prov(task, "model", res_path)
        break
    attach_ledger(task, ledger_dir)
    return task


def collect_vm(run_dir: Path, ledger_dir: Path | None) -> list[dict]:
    tasks = []
    if (run_dir / "row.json").exists():
        t = collect_vm_task(run_dir, ledger_dir)
        return [t] if t else []
    for task_dir in sorted(p.parent for p in run_dir.glob("*/row.json")):
        t = collect_vm_task(task_dir, ledger_dir)
        if t:
            tasks.append(t)
    return tasks


# ---------------------------------------------------------------- classification


def classify_failure(task: dict) -> tuple[str, str]:
    """Spec §4.2: first match wins. Returns (class, basis)."""
    fc = task.get("failure_class") or ""
    if fc in INFRA_FAILURE_CLASSES or fc == "PIER_EXCEPTION":
        return "infra", f"failure_class={fc}"
    if (task.get("outcome_class") or "").upper() == "INFRA":
        return "infra", "outcome_class=INFRA"
    if task["integrity"]["trajectory_path"] is None:
        return "infra", "no trajectory file on disk"
    exit_status = (task.get("exit_status") or "").lower()
    if any(s in exit_status for s in ("limit", "max_steps", "exhaust")):
        return "step-exhausted", f"exit_status={task['exit_status']}"
    steps, max_iter = task.get("n_agent_steps"), task.get("max_iter")
    if is_num(steps) and is_num(max_iter) and steps >= max_iter:
        return "step-exhausted", f"n_agent_steps={steps} >= max_iter={max_iter}"
    cz = task.get("causality") or {}
    l1 = task["per_layer"].get("L1") or {}
    l1_emitted = (l1.get("emitted") or l1.get("events_emitted") or 0) > 0
    if l1_emitted and cz.get("correct") == 0 and cz.get("delivered", 0) >= 1:
        return "localization-miss", "L1 emitted; scorecard tier2 correct=0"
    if cz.get("delivered", 0) >= 1 and cz.get("consumed") == 0:
        return "delivered-not-consumed", "scorecard tier2 delivered>=1, consumed=0"
    if cz.get("consumed", 0) >= 1 and task.get("has_patch"):
        return "consumed-wrong-fix", "scorecard tier2 consumed>=1, patch present, unresolved"
    missing = []
    if not cz:
        missing.append("tier2_causality (scorecard/ledger)")
    if task.get("max_iter") is None:
        missing.append("max_iter")
    if task.get("exit_status") is None:
        missing.append("exit_status")
    return ("UNCLASSIFIED(missing-signals)",
            "cannot decide; missing: " + (", ".join(missing) or "unknown"))


# ---------------------------------------------------------------- emitters

MD_SOURCES_NOTE = ("\n---\nsources: every value above traces to the files listed per row/"
                   "section; fields absent from input are printed as `NOT COLLECTED`, never "
                   "imputed.\n")


def sources_footer(paths) -> str:
    uniq = sorted({str(p) for p in paths if p})
    body = "\n".join(f"- `{p}`" for p in uniq) or "- (none)"
    return f"\n## Sources consumed\n{body}\n{MD_SOURCES_NOTE}"


def lang_of(task: dict) -> str:
    return task.get("language") or "unknown (language NOT COLLECTED)"


def write_layer_effectiveness(out_dir: Path, tasks: list[dict]) -> None:
    rows = []
    by_cell: dict[tuple[str, str], list[dict]] = {}
    for t in tasks:
        for layer in t["per_layer"]:
            by_cell.setdefault((layer, lang_of(t)), []).append(t)
    for (layer, lang), cell_tasks in sorted(by_cell.items()):
        n = len(cell_tasks)
        agg = {"eligible": 0, "emitted": 0, "suppressed": 0, "rendered_tokens_total": 0}
        have = {k: False for k in agg}
        utils, delivered_tasks = [], 0
        exemplar = ""
        for t in cell_tasks:
            pl = t["per_layer"][layer]
            for k in agg:
                v = pl.get(k, pl.get(f"events_{k}"))
                if is_num(v):
                    agg[k] += v
                    have[k] = True
            u = pl.get("utilization_score")
            if is_num(u):
                utils.append(u)
            emitted = pl.get("emitted", pl.get("events_emitted"))
            if is_num(emitted) and emitted > 0:
                delivered_tasks += 1
            ex = t["layer_exemplars"].get(layer)
            if ex and not exemplar:
                exemplar = f"{ex['pointer']} — \"{ex['snippet']}\""
            if not exemplar and t["integrity"]["ledger_path"]:
                exemplar = f"ledger: {Path(t['integrity']['ledger_path']).name}"
        util_d = distribution(utils)
        rows.append({
            "layer": layer, "language": lang, "tasks": n,
            "eligible": agg["eligible"] if have["eligible"] else None,
            "emitted": agg["emitted"] if have["emitted"] else None,
            "suppressed": agg["suppressed"] if have["suppressed"] else None,
            "rendered_tokens_total": agg["rendered_tokens_total"] if have["rendered_tokens_total"] else None,
            "utilization_min_med_max": (f"{util_d['min']:.8f}/{util_d['median']:.8f}/{util_d['max']:.8f}"
                                        if util_d else None),
            "delivered_tasks": rate_or_count(delivered_tasks, n),
            "exemplar": exemplar or None,
        })
    md = ["# Layer-effectiveness matrix (per layer x language)", "",
          "Column classes: eligible/emitted/suppressed/rendered_tokens/utilization = "
          "**telemetry** (what GT tried to send). consumed/correct/gt_caused = "
          "**agent-observation** and exist only at TASK level in this data (see the "
          "task-level causality table below) — they are NEVER attributed per layer here "
          "unless a ledger verdict exists.", "",
          "| layer | language | tasks | eligible | emitted | suppressed | rendered_tokens "
          "| utilization (min/med/max) | delivered_tasks | exemplar (file:line — verbatim) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| " + " | ".join(cell(r[k]) for k in
                                     ("layer", "language", "tasks", "eligible", "emitted",
                                      "suppressed", "rendered_tokens_total",
                                      "utilization_min_med_max", "delivered_tasks",
                                      "exemplar")) + " |")
    if not rows:
        md.append(f"| {NOT_COLLECTED} (no per-layer telemetry in this run) " + "| " * 9 + "|")
    # task-level causality (agent-observation)
    md += ["", "## Task-level causality (agent-observation: scorecard tier2 / ledger)", "",
           "| task | delivered | correct | consumed | gt_caused | source |", "|---|---|---|---|---|---|"]
    any_cz = False
    for t in tasks:
        cz = t.get("causality")
        if cz is None:
            md.append(f"| {t['instance_id']} | {NOT_COLLECTED} | {NOT_COLLECTED} | "
                      f"{NOT_COLLECTED} | {NOT_COLLECTED} | (no scorecard/ledger) |")
            continue
        any_cz = True
        src = t["provenance"].get("causality", "?")
        md.append(f"| {t['instance_id']} | {cell(cz.get('delivered'))} | {cell(cz.get('correct'))}"
                  f" | {cell(cz.get('consumed'))} | {cell(cz.get('gt_caused'))} | `{src}` |")
    if not any_cz:
        md.append("")
        md.append("No agent-observation causality data in this run — emitted counts above "
                  "MUST NOT be read as consumption.")
    # suppression reasons
    sup = {}
    for t in tasks:
        for layer, reasons in t["suppression_reasons"].items():
            slot = sup.setdefault(layer, {})
            for r, c in reasons.items():
                slot[r] = slot.get(r, 0) + c
    if sup:
        md += ["", "## Suppression reasons (telemetry)", ""]
        for layer, reasons in sorted(sup.items()):
            md.append(f"- {layer}: " + ", ".join(f"{r}={c}" for r, c in sorted(reasons.items())))
    srcs = [t["provenance"].get("layer_events") for t in tasks] + \
           [t["provenance"].get("causality") for t in tasks]
    md.append(sources_footer(srcs))
    (out_dir / "layer_effectiveness.md").write_text("\n".join(md), encoding="utf-8")
    with open(out_dir / "layer_effectiveness.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["layer", "language", "tasks", "eligible", "emitted",
                                          "suppressed", "rendered_tokens_total",
                                          "utilization_min_med_max", "delivered_tasks",
                                          "exemplar"])
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in w.fieldnames})


def write_failure_taxonomy(out_dir: Path, tasks: list[dict]) -> None:
    rows = []
    for t in tasks:
        if t["resolved"] is True:
            cz = t.get("causality")
            if cz is None:
                klass, basis = "resolved (causation NOT COLLECTED)", "no scorecard/ledger causality"
            elif cz.get("gt_caused"):
                klass, basis = "resolved (gt_caused per scorecard)", t["provenance"].get("causality", "")
            else:
                klass, basis = "resolved (NOT gt_caused)", "scorecard tier2 gt_caused=0"
        elif t["resolved"] is False or t["resolved"] is None and (t.get("failure_class") or t.get("outcome_class")):
            klass, basis = classify_failure(t)
            if t["resolved"] is None:
                klass += " [resolved verdict NOT COLLECTED]"
        else:
            klass, basis = "UNCLASSIFIED(missing-signals)", "resolved verdict NOT COLLECTED"
        rows.append({"instance_id": t["instance_id"], "class": klass, "basis": basis,
                     "resolved": t["resolved"], "failure_class": t.get("failure_class"),
                     "outcome_class": t.get("outcome_class")})
    counts = {}
    for r in rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    md = ["# Failure taxonomy", "",
          "Classification rules: docs/RESEARCH_ARTIFACT_SPEC_20260610.md §4.2 — first match "
          "wins; each basis names the on-disk signal consumed. A resolve without causality "
          "evidence is NEVER counted as a GT win.", "",
          "## Class counts", ""]
    n = len(rows)
    for klass, k in sorted(counts.items(), key=lambda kv: -kv[1]):
        md.append(f"- {klass}: {rate_or_count(k, n)}")
    md += ["", "## Per-task classification", "",
           "| task | class | basis (signal consumed) | resolved | runner failure_class | outcome_class |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['instance_id']} | {r['class']} | {r['basis']} | {cell(r['resolved'])}"
                  f" | {cell(r['failure_class'])} | {cell(r['outcome_class'])} |")
    md.append(sources_footer(t["provenance"].get("resolved") for t in tasks))
    (out_dir / "failure_taxonomy.md").write_text("\n".join(md), encoding="utf-8")
    with open(out_dir / "failure_taxonomy.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["instance_id", "class", "basis", "resolved",
                                          "failure_class", "outcome_class"])
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in w.fieldnames})


def write_token_economics(out_dir: Path, tasks: list[dict]) -> None:
    md = ["# Token economics", "",
          "| task | gt_injected_tokens | llm_tokens_in | llm_tokens_out | llm_tokens_cached "
          "| cache_hit | cache_miss | llm_cost_usd (8dp) | gt_injection_overhead_pct | "
          "actions | resolved |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    costs, overheads = [], []
    for t in tasks:
        md.append("| " + " | ".join([
            t["instance_id"], cell(t["gt_injected_tokens"]), cell(t["llm_tokens_in"]),
            cell(t["llm_tokens_out"]), cell(t["llm_tokens_cached"]),
            cell(t["llm_cache_hit_tokens"]), cell(t["llm_cache_miss_tokens"]),
            f8(t["llm_cost_usd"]), cell(t["gt_injection_overhead_pct"]),
            cell(t["n_agent_steps"]), cell(t["resolved"])]) + " |")
        if is_num(t["llm_cost_usd"]):
            costs.append(t["llm_cost_usd"])
        if is_num(t["gt_injection_overhead_pct"]):
            overheads.append(t["gt_injection_overhead_pct"])
    md += ["", "## Aggregates (per-task; bootstrap CI only when n>=5)", ""]
    for name, vals in (("llm_cost_usd", costs), ("gt_injection_overhead_pct", overheads)):
        d = distribution(vals)
        if not d:
            md.append(f"- {name}: {NOT_COLLECTED}")
            continue
        line = (f"- {name}: n={d['n']} min={d['min']:.8f} median={d['median']:.8f} "
                f"max={d['max']:.8f}")
        ci = bootstrap_ci_median(vals)
        if ci:
            line += (f" | bootstrap95(median)=[{ci['ci_lo']:.8f}, {ci['ci_hi']:.8f}] "
                     f"(seed={ci['seed']}, n_boot={ci['n_boot']})")
        else:
            line += f" | n<{MIN_N_FOR_RATES}: counts only, no CI"
        md.append(line)
    hit = sum(t["llm_cache_hit_tokens"] for t in tasks if is_num(t["llm_cache_hit_tokens"]))
    miss = sum(t["llm_cache_miss_tokens"] for t in tasks if is_num(t["llm_cache_miss_tokens"]))
    has_cache = any(is_num(t["llm_cache_hit_tokens"]) and is_num(t["llm_cache_miss_tokens"])
                    for t in tasks)
    if has_cache and (hit + miss) > 0:
        md.append(f"- cache-hit economics: hit={hit} miss={miss} "
                  f"hit_fraction={hit / (hit + miss):.8f}")
    elif has_cache:
        md.append("- cache-hit economics: collected but hit+miss=0 "
                  "(provider reported no cache activity)")
    else:
        md.append(f"- cache-hit economics: {NOT_COLLECTED}")
    md.append(sources_footer(t["provenance"].get("llm_cost_usd") for t in tasks))
    (out_dir / "token_economics.md").write_text("\n".join(md), encoding="utf-8")


def write_language_depth(out_dir: Path, tasks: list[dict]) -> None:
    by_lang: dict[str, list[dict]] = {}
    for t in tasks:
        by_lang.setdefault(lang_of(t), []).append(t)
    md = ["# Per-language substrate depth profile", ""]
    for lang, ts in sorted(by_lang.items()):
        md.append(f"## {lang} (n={len(ts)})\n")
        md.append("| task | nodes | edges | det_pct (fact-ratio) | verified_edge_ratio | "
                  "resolution dist | LSP resolved/residual | LSP no-op valid | FTS5 rows | "
                  "assertions linked/total | embedder dim/nonzero |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for t in ts:
            g, l, e = t["graph"], t["lsp"], t["embedder"]
            res_dist = g.get("resolution_method_dist")
            res_s = (", ".join(f"{k}={v}" for k, v in sorted(res_dist.items()))
                     if isinstance(res_dist, dict) else NOT_COLLECTED)
            lsp_rr = (f"{l['resolved']}/{l['residual']}"
                      if is_num(l.get("resolved")) and is_num(l.get("residual"))
                      else NOT_COLLECTED)
            asserts = (f"{g['linked_assertion_count']}/{g['assertion_count']}"
                       if is_num(g.get("linked_assertion_count")) and is_num(g.get("assertion_count"))
                       else (cell(g.get("assertion_count")) if "assertion_count" in g else NOT_COLLECTED))
            emb = (f"{e['vector_dim']}/{e['nonzero']}"
                   if "vector_dim" in e and "nonzero" in e else NOT_COLLECTED)
            md.append("| " + " | ".join([
                t["instance_id"], cell(g.get("nodes")), cell(g.get("edges")),
                cell(g.get("det_pct")), cell(g.get("verified_edge_ratio")), res_s, lsp_rr,
                cell(l.get("lsp_no_op_valid")), cell(g.get("fts5_row_count")), asserts, emb])
                + " |")
        md.append("")
    if not by_lang:
        md.append(NOT_COLLECTED)
    md.append("Notes: det_pct = deterministic CALLS edges / all CALLS edges (graph "
              "certificate); a valid LSP no-op (residual=0) is NOT a failure — reason is "
              "carried verbatim when present.")
    for t in tasks:
        if t["lsp"].get("lsp_no_op_reason"):
            md.append(f"- {t['instance_id']}: lsp_no_op_reason = "
                      f"\"{t['lsp']['lsp_no_op_reason']}\"")
    md.append(sources_footer([t["provenance"].get("graph.nodes") for t in tasks] +
                             [t["provenance"].get("lsp.resolved") for t in tasks]))
    (out_dir / "language_depth.md").write_text("\n".join(md), encoding="utf-8")


def write_behavioral_deltas(out_dir: Path, tasks: list[dict], baseline_tasks: list[dict]) -> None:
    md = ["# Behavioral metrics (distributions, never bare means)", ""]
    metrics = (("action_count / n_agent_steps", "n_agent_steps"),
               ("first_edit_action", "first_edit_action"),
               ("edit_to_gold_action", "edit_to_gold_action"),
               ("wall_clock_s (agent)", "wall_s"))
    md.append("| metric | n | min | p25 | median | p75 | max |")
    md.append("|---|---|---|---|---|---|---|")
    for label, key in metrics:
        d = distribution([t.get(key) for t in tasks])
        if d:
            md.append(f"| {label} | {d['n']} | {d['min']:.8f} | {d['p25']:.8f} | "
                      f"{d['median']:.8f} | {d['p75']:.8f} | {d['max']:.8f} |")
        else:
            md.append(f"| {label} | 0 | {NOT_COLLECTED} | | | | |")
    md.append("")
    if baseline_tasks:
        base_by_id = {b["instance_id"]: b for b in baseline_tasks}
        paired = [(t, base_by_id[t["instance_id"]]) for t in tasks
                  if t["instance_id"] in base_by_id]
        md.append(f"## Paired deltas (pairing key = instance_id; {len(paired)} pairs)")
        md.append("")
        if paired:
            md.append("| metric | n_pairs | deltas (task: gt - baseline) |")
            md.append("|---|---|---|")
            for label, key in metrics:
                ds = [(t["instance_id"], t[key] - b[key]) for t, b in paired
                      if is_num(t.get(key)) and is_num(b.get(key))]
                if ds:
                    s = "; ".join(f"{iid}: {d:+.8f}" for iid, d in ds)
                    md.append(f"| {label} | {len(ds)} | {s} |")
                else:
                    md.append(f"| {label} | 0 | {NOT_COLLECTED} in one or both arms |")
            if len(paired) < MIN_N_FOR_RATES:
                md.append("")
                md.append(f"n_pairs < {MIN_N_FOR_RATES}: per-task deltas listed raw; no "
                          "Wilcoxon / no rate claims (spec §6.3).")
        else:
            md.append("No instance_id overlap with the supplied baseline — UNPAIRED; no "
                      "delta is computed.")
    else:
        md.append("## Paired deltas")
        md.append("")
        md.append(f"{NOT_COLLECTED} — no gt_metrics_delta_<task>.json files and no "
                  "--baseline run supplied. Any comparison made outside this artifact is "
                  "UNPAIRED and must be labeled so (spec §6.1/§6.4).")
    md.append(sources_footer(t["provenance"].get("n_agent_steps") for t in tasks))
    (out_dir / "behavioral_deltas.md").write_text("\n".join(md), encoding="utf-8")


def write_integrity_chain(out_dir: Path, tasks: list[dict]) -> None:
    md = ["# Per-task integrity chain (substrate digest -> graph -> witness -> trajectory SHA)",
          "",
          "| task | substrate_digest | gt_git_commit | task_repo_commit | graph.db sha256 | "
          "graph_hash post-LSP | witness prebuilt/hash_match | trajectory file | "
          "trajectory sha256 | ledger |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for t in tasks:
        i = t["integrity"]
        wit = (f"{i['witness_gt_prebuilt_active']}/{i['witness_hook_hash_match']}"
               if i["witness_gt_prebuilt_active"] is not None
               or i["witness_hook_hash_match"] is not None else NOT_COLLECTED)
        traj = Path(i["trajectory_path"]).name if i["trajectory_path"] else NOT_COLLECTED
        led = Path(i["ledger_path"]).name if i["ledger_path"] else NOT_COLLECTED
        md.append("| " + " | ".join([
            t["instance_id"], cell(i["substrate_digest"]), cell(i["gt_git_commit"]),
            cell(i["task_repo_commit"]), cell(i["graph_db_sha256"]),
            cell(i["graph_hash_post_lsp"]), wit, traj, cell(i["trajectory_sha256"]), led])
            + " |")
    md.append("")
    md.append("Each link is reported independently; a missing link is `NOT COLLECTED`, the "
              "chain is never collapsed to a single boolean. trajectory/graph SHA256 are "
              "computed by this analyzer over the on-disk files at report time.")
    md.append(sources_footer([t["integrity"]["trajectory_path"] for t in tasks]))
    (out_dir / "integrity_chain.md").write_text("\n".join(md), encoding="utf-8")


def build_experiment_card(run_dir: Path, layout: str, tasks: list[dict], claim: str | None):
    fields_missing = []

    def first_nonnull(key_fn, missing_name):
        for t in tasks:
            v = key_fn(t)
            if v not in (None, ""):
                return v
        fields_missing.append(missing_name)
        return None

    run_id = first_nonnull(lambda t: t.get("run_id"), "run_id")
    gt_commit = first_nonnull(lambda t: t["integrity"]["gt_git_commit"], "gt_git_commit")
    substrate = first_nonnull(lambda t: t["integrity"]["substrate_digest"], "substrate_digest")
    model = first_nonnull(lambda t: t.get("model"), "model.name")
    model_params = first_nonnull(lambda t: t.get("model_params"), "model.params")
    task_ids = sorted(t["instance_id"] for t in tasks)
    langs: dict[str, int] = {}
    lang_missing = False
    for t in tasks:
        if t.get("language"):
            langs[t["language"]] = langs.get(t["language"], 0) + 1
        else:
            lang_missing = True
    if lang_missing:
        fields_missing.append("language (per task)")
    costs = [t["llm_cost_usd"] for t in tasks]
    cost_total = (f"{sum(costs):.8f}" if costs and all(is_num(c) for c in costs) else None)
    if cost_total is None:
        fields_missing.append("llm_cost_usd (all tasks)")
    walls = [t["wall_s"] for t in tasks]
    wall_total = sum(w for w in walls if is_num(w)) if any(is_num(w) for w in walls) else None
    if wall_total is None:
        fields_missing.append("wall_clock_s")
    for name, key_fn in (("edit_to_gold_action", lambda t: t.get("edit_to_gold_action")),
                         ("max_iter", lambda t: t.get("max_iter")),
                         ("dataset.manifest_sha256", lambda t: None)):
        if all(key_fn(t) in (None, "") for t in tasks):
            fields_missing.append(name)
    if not any(t.get("causality") for t in tasks):
        fields_missing.append("tier2_causality (agent-observation)")
    if not any(t["integrity"]["graph_db_sha256"] for t in tasks):
        fields_missing.append("graph.db (file for sha256)")
    replay_cmd = None
    if layout == "vm-sweep" and gt_commit and substrate and model and task_ids:
        replay_cmd = (f"OUT_DIR=<dir> MODEL={model} GT_GIT_COMMIT={gt_commit} "
                      f"GT_SUBSTRATE_DIGEST={substrate} scripts/vm/gt_agent_run.sh "
                      f"--tasks {','.join(task_ids)}")
    else:
        missing_for_replay = [n for n, v in (("gt_git_commit", gt_commit),
                                             ("substrate_digest", substrate),
                                             ("model", model)) if not v]
        if layout != "vm-sweep":
            missing_for_replay.append(f"runner entrypoint for layout={layout}")
        fields_missing.extend(f"replay.{m}" for m in missing_for_replay)
    card = {
        "schema": "gt_experiment_card.v1",
        "run_id": run_id, "layout": layout, "claim": claim,
        "commits": {
            "gt_git_commit": gt_commit,
            "task_repo_commits": {t["instance_id"]: t["integrity"]["task_repo_commit"]
                                  for t in tasks if t["integrity"]["task_repo_commit"]},
            "deepswe_bench_sha": None,
        },
        "images": {"substrate_digest": substrate,
                   "task_images": {t["instance_id"]: t["image"] for t in tasks if t.get("image")}},
        "model": {"name": model, "params": model_params},
        "dataset": {
            "manifest_sha256": None,
            "task_ids": task_ids,
            "task_ids_sha256": hashlib.sha256(",".join(task_ids).encode()).hexdigest(),
            "n_tasks": len(task_ids), "languages": langs,
        },
        "cost_usd_8dp": cost_total,
        "wall_clock_s": wall_total,
        "platform": {"analyzer_host": _platform.platform(), "run_host": None},
        "replay": {"command": replay_cmd,
                   "contract": "one command, same commits+digests+params+task set"},
        "fields_missing": sorted(set(fields_missing)),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "analyzer_version": ANALYZER_VERSION,
        "input_run_dir": str(run_dir),
        "statistics": {"bootstrap_seed": BOOTSTRAP_SEED, "bootstrap_n": BOOTSTRAP_N,
                       "min_n_for_rates": MIN_N_FOR_RATES},
    }
    return card


def write_tech_report(out_dir: Path, card: dict, tasks: list[dict]) -> None:
    n = card["dataset"]["n_tasks"]
    resolved = [t for t in tasks if t["resolved"] is True]
    unresolved = [t for t in tasks if t["resolved"] is False]
    unknown = [t for t in tasks if t["resolved"] is None]
    caused = [t for t in resolved if (t.get("causality") or {}).get("gt_caused")]
    miss = card["fields_missing"]
    md = [
        "# TECHNICAL REPORT (DRAFT — assembled from run data; TODO markers need a human)",
        "",
        f"Run: `{card['input_run_dir']}` | layout: {card['layout']} | "
        f"run_id: {card['run_id'] or NOT_COLLECTED} | generated: {card['generated_utc']} | "
        f"analyzer v{card['analyzer_version']}",
        "",
        "## Executive brief (2-page skeleton)",
        "",
        f"1. **Claim under test:** {card['claim'] or 'TODO(human): one sentence — what does this run test?'}",
        f"2. **Design:** n={n} tasks; languages: "
        f"{', '.join(f'{k}={v}' for k, v in card['dataset']['languages'].items()) or NOT_COLLECTED}; "
        f"model: {card['model']['name'] or NOT_COLLECTED}; pairing: see behavioral_deltas.md.",
        f"3. **Headline numbers (each exists in a Tier 2 table; none invented):** "
        f"resolved {len(resolved)}/{n}; unresolved {len(unresolved)}/{n}; "
        f"verdict-unknown {len(unknown)}/{n}; resolved-with-causality-evidence "
        f"{len(caused)}/{len(resolved) if resolved else 0}; "
        f"cost_usd={card['cost_usd_8dp'] or NOT_COLLECTED}.",
        "4. **What failed:** see failure_taxonomy.md class counts.",
        "5. **What this does NOT show:** " +
        ("missing fields: " + "; ".join(miss) + ". " if miss else "") +
        (f"n<{MIN_N_FOR_RATES} cells are reported as counts, not rates. " if n < MIN_N_FOR_RATES else "") +
        "No unpaired comparison in this bundle carries a significance claim.",
        "",
        "## 1. Problem statement",
        "",
        "Coding agents navigate repositories with no verified map; GroundTruth supplies a "
        "deterministic, certificate-gated codebase-intelligence substrate (ONE pipeline: "
        "FTS5 retrieval -> graph traversal -> LSP-enriched contracts -> curated brief). "
        "This run measures whether that substrate's delivered context changes agent "
        "behavior/outcomes.",
        "TODO(human): tighten to this run's specific question.",
        "",
        "## 2. Architecture (reference, not restated)",
        "",
        "State of record: `gt_gt.md` §11–§13 + `DOC_OF_HONOR.md`. This report does not "
        "restate architecture; all layer names below refer to those documents.",
        "",
        "## 3. Methodology",
        "",
        f"- model: {card['model']['name'] or NOT_COLLECTED}; "
        f"params: {json.dumps(card['model']['params']) if card['model']['params'] else NOT_COLLECTED}",
        f"- task set: {n} ids (sha256 {card['dataset']['task_ids_sha256'][:16]}…), "
        f"full list in experiment_card.json",
        f"- commits/digests: gt={card['commits']['gt_git_commit'] or NOT_COLLECTED}; "
        f"substrate={card['images']['substrate_digest'] or NOT_COLLECTED}",
        f"- replay: `{card['replay']['command'] or NOT_COLLECTED}`",
        "- TODO(human): pairing rationale and arm definitions.",
        "",
        "## 4. Results",
        "",
        "- Behavioral distributions: see `behavioral_deltas.md` (included in this bundle).",
        "- Token economics: see `token_economics.md`.",
        "- Layer effectiveness: see `layer_effectiveness.md` — telemetry and "
        "agent-observation columns are separated by source; do not merge them.",
        "- TODO(human): interpretation; lead with the trajectory finding, not pass/fail "
        "(resolved-is-not-the-prize rule).",
        "",
        "## 5. Failure taxonomy",
        "",
        "See `failure_taxonomy.md` (rules in spec §4.2). "
        "TODO(human): one exemplar narrative per non-empty class, citing "
        "task_ledgers/<task>.md lines.",
        "",
        "## 6. Substrate depth",
        "",
        "See `language_depth.md`.",
        "",
        "## 7. Integrity & legitimacy",
        "",
        "See `integrity_chain.md`. Legitimacy fields (scorecard tier6) per task:",
    ]
    for t in tasks:
        leg = t.get("legitimacy")
        md.append(f"- {t['instance_id']}: " +
                  (json.dumps(leg) if leg else NOT_COLLECTED))
    md += [
        "",
        "## 8. Limitations (auto-generated; extend by hand)",
        "",
    ]
    if miss:
        md.append("Fields this run did not collect (spec §8 — runner work items):")
        md.extend(f"- {m}" for m in miss)
    if n < MIN_N_FOR_RATES:
        md.append(f"- n={n} < {MIN_N_FOR_RATES}: all cells are counts; no rates, no CIs, "
                  "no significance claims are possible from this bundle.")
    md += [
        "- TODO(human): design limitations (task selection, harness, single-model).",
        "",
        "## 9. Roadmap",
        "",
        "TODO(human).",
        "",
        "---",
        "sources: experiment_card.json, tasks_normalized.json and the sibling Tier 2 "
        "artifacts in this RUN_REPORT/ directory.",
    ]
    (out_dir / "TECH_REPORT_DRAFT.md").write_text("\n".join(md), encoding="utf-8")


# ---------------------------------------------------------------- main


def collect(run_dir: Path, ledger_dir: Path | None) -> tuple[str, list[dict]]:
    layout = sniff_layout(run_dir)
    if layout == "vm-sweep":
        tasks = collect_vm(run_dir, ledger_dir)
    elif layout == "pier-jobs":
        tasks = collect_pier(run_dir, ledger_dir)
    elif layout == "gha-openhands":
        tasks = collect_gha(run_dir, ledger_dir)
    else:
        tasks = []
    return layout, tasks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="output dir (default: <run_dir>/RUN_REPORT)")
    ap.add_argument("--ledgers", type=Path, default=None,
                    help="task_ledgers/ dir (default: <run_dir>/task_ledgers if present)")
    ap.add_argument("--claim", default=None, help="the claim under test (verbatim)")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="baseline run dir for PAIRED deltas (pairing key=instance_id)")
    args = ap.parse_args(argv)

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"ERROR: not a directory: {run_dir}", file=sys.stderr)
        return 2
    ledger_dir = args.ledgers
    if ledger_dir is None and (run_dir / "task_ledgers").is_dir():
        ledger_dir = run_dir / "task_ledgers"

    layout, tasks = collect(run_dir, ledger_dir)
    if layout == "unknown":
        print(f"ERROR: could not sniff layout of {run_dir} (no row.json, no pier "
              f"result.json, no output.jsonl/gt_debug)", file=sys.stderr)
        return 3
    if not tasks:
        print(f"ERROR: layout={layout} detected but no task records extracted", file=sys.stderr)
        return 4

    baseline_tasks = []
    if args.baseline:
        _, baseline_tasks = collect(args.baseline.resolve(), None)

    out_dir = (args.out or (run_dir / "RUN_REPORT")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    card = build_experiment_card(run_dir, layout, tasks, args.claim)
    with open(out_dir / "experiment_card.json", "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2)
    with open(out_dir / "tasks_normalized.json", "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, default=str)
    write_layer_effectiveness(out_dir, tasks)
    write_failure_taxonomy(out_dir, tasks)
    write_token_economics(out_dir, tasks)
    write_language_depth(out_dir, tasks)
    write_behavioral_deltas(out_dir, tasks, baseline_tasks)
    write_integrity_chain(out_dir, tasks)
    write_tech_report(out_dir, card, tasks)

    print(f"RUN_REPORT written: {out_dir}")
    print(f"  layout={layout}  tasks={len(tasks)}  fields_missing={len(card['fields_missing'])}")
    for m in card["fields_missing"]:
        print(f"  NOT COLLECTED: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
