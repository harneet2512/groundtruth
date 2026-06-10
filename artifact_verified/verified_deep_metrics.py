#!/usr/bin/env python3
"""PATH B (SWE-bench Verified) deep-metrics emitter — stdlib-only.

WHY THIS EXISTS (the bug it closes): the shared scripts/swebench/gt_deep_metrics.py
finder only locates an OpenHands `output.jsonl` or a pier
`**/mini-swe-agent.trajectory.json`. The Verified adapter writes its trajectory as
`<results>/<iid>/<iid>.traj.json` (gt_verified_agent.py:305 via DefaultAgent.save),
which NEITHER finder matches -> NO deep log is emitted on this path. The
constitution is explicit: "a run without its persisted 8-decimal deep log is NOT
done — it cannot be cited, claimed, or compared." This module parses the Verified
artifacts directly and emits `gt_deep_metrics_<iid>.json` at 8-dp precision with
the constitution's required fields, in the `gt_deep_metrics.v2` schema the research
analyzer (docs/RESEARCH_ARTIFACT_SPEC_20260610.md) consumes, PLUS the §8
runner-gap fields (model.params, language, substrate_digest, task_repo_commit,
edit_to_gold/gold_edited/edited_files, wall-clock, max_iter, trajectory SHA256).

Inputs (all best-effort — absence is RECORDED in `inputs_present`, never fatal,
never imputed; honesty rule §7 of the spec):
  <task_dir>/<iid>.traj.json          mini-swe-agent trajectory (DefaultAgent.save:
                                      info.model_stats / info.config / messages[*])
  <task_dir>/results/outcome.json     gt.verified_outcome.v2 (resolved / classification /
                                      eval_no_report) — the official-eval verdict
  <task_dir>/gt_artifacts/brief.txt   the delivered brief (gt_sent_tokens source)
  <task_dir>/gt_artifacts/*_certificate.json + run_manifest.json + foundational_gate_report.json
                                      substrate certs (graph/LSP depth, substrate_digest,
                                      task_repo_commit)

Trajectory truth (AGENT-OBSERVATION rule): GT delivery counts come from the agent's
OWN observation messages (role tool/user content), not telemetry. Tokens + cost come
from each assistant message's litellm `extra.response.usage` / `_hidden_params.
response_cost` (model-agnostic — correct for any provider, never DeepSeek-keyed
here since litellm has no price map entry for deepseek-v4-flash and cost pins 0.0).

Usage:
    python artifact_verified/verified_deep_metrics.py <iid> <task_dir> [--out <path>]
The default output is <task_dir>/gt_deep_metrics_<iid>.json (+ a .md companion).
Always exits 0 unless the JSON write itself fails (the record must always land).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA = "gt_deep_metrics.v2"
PATH_B_PRODUCER = "verified_deep_metrics"


# ---------------------------------------------------------------------------
# precision + small IO helpers (stdlib-only, never raise)
# ---------------------------------------------------------------------------
def d8(x) -> float:
    """8-decimal float (constitution mandate). None/NaN/bad -> 0.0."""
    try:
        return round(float(x), 8)
    except (TypeError, ValueError):
        return 0.0


def _load_json(path: str | Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _read_text(path: str | Path, max_bytes: int = 8_000_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def _sha256_file(path: str | Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _git(*args: str) -> str:
    try:
        repo_root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, text=True, timeout=15
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _approx_tokens(text: str) -> int:
    """Token estimate when no tokenizer/usage is available (~4 chars/token, the
    standard rough heuristic). Only used for gt_sent_tokens (the brief), which the
    model never reports usage for separately."""
    return int(round(len(text or "") / 4.0))


# ---------------------------------------------------------------------------
# trajectory location + parse (<iid>.traj.json — the Verified-only shape)
# ---------------------------------------------------------------------------
def find_traj(iid: str, task_dir: str) -> str:
    """The <iid>.traj.json the Verified adapter writes. Searched at the task dir,
    its results/ subdir, and the canonical /tmp/results layout the workflow uses
    (gt_verified_agent.run_instance: output_dir/<iid>/<iid>.traj.json)."""
    cands = [
        os.path.join(task_dir, f"{iid}.traj.json"),
        os.path.join(task_dir, "results", iid, f"{iid}.traj.json"),
        os.path.join(task_dir, iid, f"{iid}.traj.json"),
        os.path.join("/tmp/results", iid, f"{iid}.traj.json"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    # Bounded walk under the task dir for the exact filename (no sibling-task glob).
    target = f"{iid}.traj.json"
    for root, _dirs, files in os.walk(task_dir):
        if target in files:
            return os.path.join(root, target)
        # keep the walk shallow — these dirs are small per-task artifact trees
    return ""


# Source-file extensions (parity with gt_mini_patch._SRC_EXT) for edit detection.
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)
_EDIT_MARKERS = ("sed -i", "str_replace", "apply_patch", "tee ", ">", ">>",
                 "writeFileSync", "open(")


def _cmd_text_of(msg: dict) -> str:
    """The executed command text of an assistant message — tool_calls (structured)
    + content (the THOUGHT). mini-swe-agent puts the action in extra.actions and/or
    tool_calls; we scan both plus the raw content."""
    parts: list[str] = []
    for k in ("tool_calls",):
        v = msg.get(k)
        if v:
            parts.append(json.dumps(v))
    extra = msg.get("extra") or {}
    if isinstance(extra, dict):
        acts = extra.get("actions")
        if acts:
            parts.append(json.dumps(acts))
    c = msg.get("content")
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        parts.append(json.dumps(c))
    return "\n".join(parts)


def _edited_src_files(cmd: str) -> list[str]:
    """Source files this command WROTE (best-effort, for edited_files/gold_edited).
    Conservative: only return tokens that end in a source extension AND the command
    carries an edit marker."""
    if not cmd or not any(m in cmd for m in _EDIT_MARKERS):
        return []
    out: list[str] = []
    for raw in cmd.replace("'", " ").replace('"', " ").replace(",", " ").split():
        t = raw.strip("()<>;|&`\\")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t:
            # normalize container abs path the same way the pillars do
            tn = t.replace("\\", "/")
            for cr in ("/testbed/", "/home/user/", "/workspace/", "/app/", "/repo/"):
                if tn.startswith(cr):
                    tn = tn[len(cr):]
                    break
            tn = tn.lstrip("./").lstrip("/")
            if tn and tn not in out:
                out.append(tn)
    return out


def parse_trajectory(traj: dict) -> dict:
    """Pull agent behavior, tokens/cost (per-call, model-agnostic), and GT delivery
    (from the agent's OBSERVATION messages) out of the mini-swe-agent trajectory."""
    out = {
        "found": True,
        "exit_status": "",
        "submission": "",
        "gt_baseline": None,
        "wall_seconds": 0.0,
        "model_name": "",
        "step_limit": 0,
        "cost_limit": 0.0,
        "max_iter": 0,
        # agent behavior
        "action_count": 0,
        "n_calls": 0,
        "edits": 0,
        "first_edit_action": 0,
        "edit_to_gold_action": 0,
        "gold_edited": None,            # determinable only with a gold target -> None here
        "edited_files": [],
        # tokens / cost (per-call, summed)
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "",
        # GT delivery (from agent observation content — the truth)
        "gt_brief_delivered": 0,
        "gt_evidence_delivered": 0,
        "gt_scope_delivered": 0,
        "gt_contract_delivered": 0,
        "gt_cochange_delivered": 0,
        "gt_nudge_delivered": 0,
        "gt_observation_chars_total": 0,
        "raw_delivered_gt_samples": [],   # verbatim GT text the agent saw (capped)
        "has_patch": False,
        "resolved": None,
    }
    info = traj.get("info") or {}
    ms = info.get("model_stats") or {}
    out["n_calls"] = int(ms.get("api_calls", 0) or 0)
    rec_cost = ms.get("instance_cost")
    if isinstance(rec_cost, (int, float)) and rec_cost > 0:
        out["cost_usd"] = float(rec_cost)
        out["cost_source"] = "trajectory_model_stats.instance_cost"
    cfg = info.get("config") or {}
    mcfg = cfg.get("model")
    if isinstance(mcfg, dict):
        out["model_name"] = str(mcfg.get("model_name") or mcfg.get("model") or "")
    acfg = cfg.get("agent")
    if isinstance(acfg, dict):
        out["step_limit"] = int(acfg.get("step_limit", 0) or 0)
        try:
            out["cost_limit"] = float(acfg.get("cost_limit", 0.0) or 0.0)
        except (TypeError, ValueError):
            out["cost_limit"] = 0.0
    out["max_iter"] = out["step_limit"] or 250  # the harness max_iter is step_limit
    out["exit_status"] = str(info.get("exit_status", "") or "")
    out["submission"] = str(info.get("submission", "") or "")
    if "gt_baseline" in info:
        out["gt_baseline"] = bool(info.get("gt_baseline"))
    try:
        out["wall_seconds"] = float(info.get("gt_wall_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        out["wall_seconds"] = 0.0
    out["has_patch"] = "diff --git" in out["submission"]

    step = 0
    for m in traj.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        extra = m.get("extra") if isinstance(m.get("extra"), dict) else {}
        # per-call usage + cost (model-agnostic; correct for any provider)
        resp = extra.get("response") if isinstance(extra.get("response"), dict) else {}
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
        if usage:
            out["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            out["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            out["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
            out["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            out["cache_miss_tokens"] += int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        if not out["cost_source"] or out["cost_source"].startswith("per_call"):
            hp = resp.get("_hidden_params") if isinstance(resp.get("_hidden_params"), dict) else {}
            rc = hp.get("response_cost")
            if not isinstance(rc, (int, float)):
                rc = extra.get("cost")
            if isinstance(rc, (int, float)) and rc > 0:
                # sum per-call recorded cost only when the run-level rollup was absent
                if out["cost_source"] in ("", "per_call_response_cost"):
                    out["cost_usd"] += float(rc)
                    out["cost_source"] = "per_call_response_cost"

        if role == "assistant":
            step += 1
            cmd = _cmd_text_of(m)
            ef = _edited_src_files(cmd)
            if ef:
                out["edits"] += 1
                if not out["first_edit_action"]:
                    out["first_edit_action"] = step
                for f in ef:
                    if f not in out["edited_files"]:
                        out["edited_files"].append(f)
        elif role in ("tool", "user", "exit"):
            content = m.get("content")
            if isinstance(content, list):
                content = json.dumps(content)
            content = content or ""
            if "<gt-task-brief" in content:
                out["gt_brief_delivered"] += content.count("<gt-task-brief")
            if "<gt-evidence" in content:
                out["gt_evidence_delivered"] += content.count("<gt-evidence")
            if "<gt-scope" in content:
                out["gt_scope_delivered"] += content.count("<gt-scope")
            if "<gt-contract" in content:
                out["gt_contract_delivered"] += content.count("<gt-contract")
            if "<gt-cochange" in content:
                out["gt_cochange_delivered"] += content.count("<gt-cochange")
            if "<gt-nudge" in content:
                out["gt_nudge_delivered"] += content.count("<gt-nudge")
            if "<gt-" in content:
                out["gt_observation_chars_total"] += len(content)
                if len(out["raw_delivered_gt_samples"]) < 8:
                    # verbatim RAW delivered GT text (AGENT-OBSERVATION rule) —
                    # the slice around the first GT tag, capped.
                    idx = content.find("<gt-")
                    out["raw_delivered_gt_samples"].append(content[idx:idx + 600])

    out["action_count"] = out["n_calls"] or step
    return out


# ---------------------------------------------------------------------------
# outcome.json (gt.verified_outcome.v2) — official-eval verdict + INFRA classify
# ---------------------------------------------------------------------------
def parse_outcome(task_dir: str) -> dict:
    out = {
        "found": False, "resolved": None, "classification": None,
        "eval_no_report": None, "had_predictions": None,
        "in_resolved_denominator": None,
    }
    for cand in (
        os.path.join(task_dir, "results", "outcome.json"),
        os.path.join(task_dir, "outcome.json"),
        os.path.join("/tmp/results", "outcome.json"),
    ):
        d = _load_json(cand)
        if isinstance(d, dict):
            out["found"] = True
            out["resolved"] = d.get("resolved")
            out["classification"] = d.get("classification")
            out["eval_no_report"] = d.get("eval_no_report")
            out["had_predictions"] = d.get("had_predictions")
            # INFRA / eval_no_report tasks are EXCLUDED from the resolved denominator
            # (workflow fix #2). A graded task (resolved is true/false) is included.
            cls = out["classification"]
            if cls == "INFRA" or out["eval_no_report"] is True:
                out["in_resolved_denominator"] = False
            elif isinstance(out["resolved"], bool):
                out["in_resolved_denominator"] = True
            else:
                out["in_resolved_denominator"] = False
            return out
    return out


# ---------------------------------------------------------------------------
# substrate certs (graph/LSP depth + provenance) — best-effort, pass-through
# ---------------------------------------------------------------------------
def _gt_artifacts_dir(task_dir: str) -> str:
    for cand in (os.path.join(task_dir, "gt_artifacts"),
                 os.path.join(task_dir, "gt"),
                 "/tmp/gt"):
        if os.path.isdir(cand):
            return cand
    return ""


def parse_substrate(task_dir: str) -> dict:
    art = _gt_artifacts_dir(task_dir)
    out = {
        "gt_artifacts_dir": art,
        "graph_db_sha256": "",
        "graph_nodes": None, "graph_edges": None, "det_pct": None,
        "resolution_method_dist": None,
        "lsp_resolved": None, "lsp_residual": None, "lsp_verified": None,
        "lsp_corrected": None, "lsp_deleted": None, "lsp_no_op_valid": None,
        "substrate_digest": None, "task_repo_commit": None, "graph_hash": None,
        "brief_chars": 0, "gt_sent_tokens": 0,
        "language": None,
    }
    if not art:
        return out
    dbp = os.path.join(art, "graph.db")
    if os.path.isfile(dbp):
        out["graph_db_sha256"] = _sha256_file(dbp)
    gc = _load_json(os.path.join(art, "graph_certificate.json"))
    if isinstance(gc, dict):
        out["graph_nodes"] = gc.get("nodes") if gc.get("nodes") is not None else gc.get("node_count")
        out["graph_edges"] = gc.get("edges") if gc.get("edges") is not None else gc.get("edge_count")
        out["det_pct"] = gc.get("det_pct")
        out["resolution_method_dist"] = gc.get("resolution_method_dist")
        out["language"] = gc.get("language") or gc.get("dominant_language")
    lc = _load_json(os.path.join(art, "lsp_certificate.json"))
    if isinstance(lc, dict):
        out["lsp_resolved"] = lc.get("resolved")
        out["lsp_residual"] = lc.get("residual")
        out["lsp_verified"] = lc.get("verified")
        out["lsp_corrected"] = lc.get("corrected")
        out["lsp_deleted"] = lc.get("deleted")
        out["lsp_no_op_valid"] = lc.get("no_op_valid") or lc.get("lsp_no_op_valid")
    man = _load_json(os.path.join(art, "run_manifest.json"))
    if isinstance(man, dict):
        out["substrate_digest"] = man.get("substrate_digest") or os.environ.get("GT_SUBSTRATE_DIGEST")
        out["task_repo_commit"] = man.get("task_repo_commit") or man.get("repo_commit") \
            or os.environ.get("GT_TASK_REPO_COMMIT")
        out["graph_hash"] = man.get("graph_hash") or man.get("graph_edges_hash")
    if out["substrate_digest"] is None:
        out["substrate_digest"] = os.environ.get("GT_SUBSTRATE_DIGEST") or None
    if out["task_repo_commit"] is None:
        out["task_repo_commit"] = os.environ.get("GT_TASK_REPO_COMMIT") or None
    brief = _read_text(os.path.join(art, "brief.txt"))
    out["brief_chars"] = len(brief)
    out["gt_sent_tokens"] = _approx_tokens(brief)
    return out


# ---------------------------------------------------------------------------
# build the 8-dp deep record
# ---------------------------------------------------------------------------
def build(iid: str, task_dir: str) -> dict:
    traj_path = find_traj(iid, task_dir)
    traj_raw = _load_json(traj_path) if traj_path else None
    traj_sha = _sha256_file(traj_path) if traj_path else ""
    if isinstance(traj_raw, dict):
        tj = parse_trajectory(traj_raw)
    else:
        tj = {"found": False}
    outcome = parse_outcome(task_dir)
    sub = parse_substrate(task_dir)

    found = bool(tj.get("found"))
    resolved = outcome.get("resolved")
    if resolved is None and found:
        resolved = tj.get("resolved")
    has_patch = bool(tj.get("has_patch")) if found else False

    # outcome enum (parity with gt_deep_metrics.OUTCOMES vocabulary)
    if outcome.get("classification") == "INFRA" or outcome.get("eval_no_report") is True:
        outcome_enum, failure_stage = "infra_failed_agent_not_started", "infra"
    elif resolved is True:
        outcome_enum, failure_stage = "resolved", "none"
    elif has_patch:
        outcome_enum, failure_stage = "unresolved_with_patch", "none"
    elif found and (tj.get("action_count") or 0) > 0:
        outcome_enum, failure_stage = "unresolved_no_patch_agent_ran", "agent"
    else:
        outcome_enum, failure_stage = "infra_failed_agent_not_started", "infra"

    actions = int(tj.get("action_count", 0) or 0) if found else 0
    llm_in = int(tj.get("prompt_tokens", 0) or 0)
    llm_out = int(tj.get("completion_tokens", 0) or 0)
    gt_sent = int(sub.get("gt_sent_tokens", 0) or 0)
    cost = d8(tj.get("cost_usd", 0.0)) if found else 0.0

    efficiency = {
        "model": tj.get("model_name", "") if found else "",
        "cost_source": tj.get("cost_source", "") or "none_litellm_unmapped",
        "llm_calls": d8(tj.get("n_calls", 0)),
        "llm_tokens_in": d8(llm_in),
        "llm_tokens_out": d8(llm_out),
        "llm_cache_hit_tokens": d8(tj.get("cache_hit_tokens", 0)),
        "llm_cache_miss_tokens": d8(tj.get("cache_miss_tokens", 0)),
        "llm_tokens_total": d8(llm_in + llm_out),
        "llm_cost_usd": cost,
        "gt_injected_tokens_total": d8(gt_sent),
        "gt_sent_tokens": d8(gt_sent),
        "tokens_per_action": d8((llm_in + llm_out) / actions) if actions else 0.0,
        "cost_per_action_usd": d8(cost / actions) if actions else 0.0,
        "gt_injection_overhead_pct": d8(100.0 * gt_sent / llm_in) if llm_in else 0.0,
    }

    deep = {
        "task_id": iid,
        "schema": SCHEMA,
        "producer": PATH_B_PRODUCER,
        "pipeline": "verified-miniswe",
        "precision_decimals": 8,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "git_commit": os.environ.get("GT_GIT_COMMIT") or _git("rev-parse", "HEAD") or "unknown",
        # --- outcome / failure attribution ---
        "outcome": outcome_enum,
        "failure_stage": failure_stage,
        "agent_started": bool(found and actions > 0),
        "resolved": resolved,
        "has_patch": has_patch,
        "exit_status": tj.get("exit_status", "") if found else "",
        "gt_baseline": tj.get("gt_baseline") if found else None,
        # --- official-eval verdict + INFRA exclusion (workflow fix #2) ---
        "official_eval": {
            "classification": outcome.get("classification"),
            "eval_no_report": outcome.get("eval_no_report"),
            "had_predictions": outcome.get("had_predictions"),
            "in_resolved_denominator": outcome.get("in_resolved_denominator"),
        },
        # --- §8 runner-gap fields (RESEARCH_ARTIFACT_SPEC) ---
        "model": {
            "name": tj.get("model_name", "") if found else "",
            # sampling params are pinned in verified_gt.yaml (DeepSeek official-spec);
            # recorded here verbatim so the experiment card can pin replay. NEVER
            # reconstructed if absent from the config snapshot.
            "params": _model_params(),
        },
        "language": sub.get("language"),
        "substrate_digest": sub.get("substrate_digest"),
        "task_repo_commit": sub.get("task_repo_commit"),
        "graph_hash": sub.get("graph_hash"),
        "graph_db_sha256": sub.get("graph_db_sha256"),
        "trajectory_path": traj_path,
        "trajectory_sha256": traj_sha,
        "max_iter": tj.get("max_iter", 250) if found else 250,
        "wall_clock_s": d8(tj.get("wall_seconds", 0.0)) if found else 0.0,
        # --- substrate depth (per-language, from certs) ---
        "substrate": {
            "graph_nodes": sub.get("graph_nodes"),
            "graph_edges": sub.get("graph_edges"),
            "det_pct": sub.get("det_pct"),
            "resolution_method_dist": sub.get("resolution_method_dist"),
            "lsp_resolved": sub.get("lsp_resolved"),
            "lsp_residual": sub.get("lsp_residual"),
            "lsp_verified": sub.get("lsp_verified"),
            "lsp_corrected": sub.get("lsp_corrected"),
            "lsp_deleted": sub.get("lsp_deleted"),
            "lsp_no_op_valid": sub.get("lsp_no_op_valid"),
            "gt_artifacts_dir": sub.get("gt_artifacts_dir"),
            "brief_chars": d8(sub.get("brief_chars", 0)),
        },
        # --- token / cost efficiency ---
        "gt_injected_tokens_total": d8(gt_sent),
        "efficiency": efficiency,
        # --- GT reached the agent (fired AND delivered — agent observation) ---
        "gt_delivery": {
            "brief_delivered": d8(tj.get("gt_brief_delivered", 0)) if found else 0.0,
            "evidence_delivered": d8(tj.get("gt_evidence_delivered", 0)) if found else 0.0,
            "scope_delivered": d8(tj.get("gt_scope_delivered", 0)) if found else 0.0,
            "contract_delivered": d8(tj.get("gt_contract_delivered", 0)) if found else 0.0,
            "cochange_delivered": d8(tj.get("gt_cochange_delivered", 0)) if found else 0.0,
            "nudge_delivered": d8(tj.get("gt_nudge_delivered", 0)) if found else 0.0,
            "gt_observation_chars_total": d8(tj.get("gt_observation_chars_total", 0)) if found else 0.0,
            "raw_delivered_gt_samples": tj.get("raw_delivered_gt_samples", []) if found else [],
        },
        # --- agent behavior (constitution deep-log block) ---
        "agent": {
            "action_count": d8(actions),
            "n_calls": d8(tj.get("n_calls", 0)) if found else 0.0,
            "edits": d8(tj.get("edits", 0)) if found else 0.0,
            "first_edit_action": d8(tj.get("first_edit_action", 0)) if found else 0.0,
            "edit_to_gold_action": d8(tj.get("edit_to_gold_action", 0)) if found else 0.0,
            "gold_edited": tj.get("gold_edited") if found else None,
            "edited_files": tj.get("edited_files", []) if found else [],
        },
        # --- provenance of optional inputs (honesty rule: what was missing) ---
        "inputs_present": {
            "trajectory": found,
            "outcome_json": outcome.get("found"),
            "substrate_certs": bool(sub.get("gt_artifacts_dir")),
            "brief": bool(sub.get("brief_chars")),
            "graph_db": bool(sub.get("graph_db_sha256")),
        },
    }
    return deep


def _model_params() -> dict | None:
    """The pinned sampling params from verified_gt.yaml (DeepSeek official-spec).
    Read from the config on disk so the card can pin replay; returns None (-> the
    analyzer records `model.params: NOT COLLECTED`) if the config is unreadable.
    NEVER reconstructed from defaults."""
    cfg_path = Path(__file__).resolve().parent / "verified_gt.yaml"
    text = _read_text(cfg_path)
    if not text:
        return None
    # stdlib-only: a tiny line scan for the locked model_kwargs (no yaml dep).
    keys = ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty")
    params: dict = {}
    for line in text.splitlines():
        s = line.strip()
        for k in keys:
            if s.startswith(k + ":"):
                val = s.split(":", 1)[1].strip()
                try:
                    params[k] = float(val) if "." in val else int(val)
                except ValueError:
                    params[k] = val
        if "type: disabled" in s:
            params["thinking"] = "disabled"
    return params or None


# ---------------------------------------------------------------------------
# markdown companion (human-readable, honest about NOT COLLECTED)
# ---------------------------------------------------------------------------
def _md(deep: dict) -> str:
    eff = deep.get("efficiency", {})
    g = deep.get("gt_delivery", {})
    a = deep.get("agent", {})
    s = deep.get("substrate", {})

    def v(x):
        return "NOT COLLECTED" if x is None else x

    return (
        f"# Verified deep metrics — `{deep.get('task_id')}`\n\n"
        f"- pipeline: `{deep.get('pipeline')}` · model: `{v(deep.get('model', {}).get('name')) or 'n/a'}`\n"
        f"- branch `{deep.get('branch')}` @ `{(deep.get('git_commit') or '')[:12]}`\n"
        f"- **outcome: {deep.get('outcome')}** · resolved={v(deep.get('resolved'))} · has_patch={deep.get('has_patch')}\n"
        f"- in_resolved_denominator: {v(deep.get('official_eval', {}).get('in_resolved_denominator'))} "
        f"(classification={v(deep.get('official_eval', {}).get('classification'))})\n\n"
        f"## Agent behavior\n"
        f"- steps: {a.get('action_count')} · edits: {a.get('edits')} · first_edit@: {a.get('first_edit_action')}\n"
        f"- edited_files: {a.get('edited_files')}\n"
        f"- wall_clock_s: {deep.get('wall_clock_s')} · max_iter: {deep.get('max_iter')}\n\n"
        f"## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)\n"
        f"- in: {eff.get('llm_tokens_in')} out: {eff.get('llm_tokens_out')} total: {eff.get('llm_tokens_total')}\n"
        f"- gt_sent_tokens: {eff.get('gt_sent_tokens')} · overhead%: {eff.get('gt_injection_overhead_pct')}\n"
        f"- cost_usd: {eff.get('llm_cost_usd')} (source: {eff.get('cost_source')})\n\n"
        f"## GT reached the agent (fired AND delivered — agent observation)\n"
        f"- brief: {g.get('brief_delivered')} · evidence: {g.get('evidence_delivered')} · scope: {g.get('scope_delivered')}\n"
        f"- contract: {g.get('contract_delivered')} · cochange: {g.get('cochange_delivered')} · nudge: {g.get('nudge_delivered')}\n"
        f"- GT observation chars: {g.get('gt_observation_chars_total')}\n\n"
        f"## Substrate depth\n"
        f"- nodes: {v(s.get('graph_nodes'))} edges: {v(s.get('graph_edges'))} det_pct: {v(s.get('det_pct'))}\n"
        f"- LSP resolved/residual: {v(s.get('lsp_resolved'))}/{v(s.get('lsp_residual'))}\n"
        f"- substrate_digest: {v(deep.get('substrate_digest'))}\n"
        f"- graph.db sha256: {deep.get('graph_db_sha256') or 'NOT COLLECTED'}\n"
        f"- trajectory sha256: {deep.get('trajectory_sha256') or 'NOT COLLECTED'}\n\n"
        f"_inputs present: {deep.get('inputs_present')}_\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("instance_id", help="SWE-bench Verified instance id")
    ap.add_argument("task_dir", nargs="?", default="",
                    help="Per-task artifact dir (contains <iid>.traj.json / results/ / gt_artifacts/)")
    ap.add_argument("--out", default="", help="Output path (default <task_dir>/gt_deep_metrics_<iid>.json)")
    args = ap.parse_args(argv)

    iid = args.instance_id
    task_dir = args.task_dir or os.environ.get("GT_TASK_DIR") or "."

    # ALWAYS WRITE (constitution): even total input loss yields a record carrying
    # outcome/failure attribution + whatever provenance exists.
    try:
        deep = build(iid, task_dir)
    except Exception as exc:  # noqa: BLE001 — the emitter must never fail the run
        deep = {
            "task_id": iid,
            "schema": SCHEMA,
            "producer": PATH_B_PRODUCER,
            "pipeline": "verified-miniswe",
            "precision_decimals": 8,
            "outcome": "infra_failed_agent_not_started",
            "failure_stage": "infra",
            "failure_reason": f"verified_deep_metrics emitter error: "
                              f"{type(exc).__name__}: {str(exc)[:300]}",
            "agent_started": False,
            "resolved": None,
            "has_patch": False,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "git_commit": _git("rev-parse", "HEAD") or "unknown",
            "efficiency": {}, "agent": {}, "gt_delivery": {}, "inputs_present": {},
        }

    out_path = args.out or os.path.join(task_dir, f"gt_deep_metrics_{iid}.json")
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(deep, f, indent=2)
    except OSError as exc:
        print(f"[GT_DEEP_V] FAILED to write {out_path}: {exc}", file=sys.stderr)
        return 1
    md_path = (out_path[:-5] + ".md") if out_path.endswith(".json") else out_path + ".md"
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(_md(deep))
    except OSError:
        pass

    eff = deep.get("efficiency", {})
    agent = deep.get("agent", {})
    print(f"[GT_DEEP_V] wrote {out_path}: outcome={deep.get('outcome')} "
          f"resolved={deep.get('resolved')} has_patch={deep.get('has_patch')} "
          f"actions={agent.get('action_count')} "
          f"in_denominator={deep.get('official_eval', {}).get('in_resolved_denominator')} "
          f"llm_tokens={eff.get('llm_tokens_total')} cost=${eff.get('llm_cost_usd')} "
          f"gt_sent_tokens={eff.get('gt_sent_tokens')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
