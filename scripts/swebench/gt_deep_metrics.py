#!/usr/bin/env python3
"""Deep per-run metric dumper — writes gt_deep_metrics_<task>.json at 8-decimal
precision from the run's recorded telemetry + the agent's OWN observations
(output.jsonl), per the constitution rule "Deep per-run logging at 8-decimal
precision". Every run (OH / mini-swe-agent / DeepSWE) calls this after the agent
finishes. A run without this file is not done.

Usage: gt_deep_metrics.py <task_id> [results_dir] [--baseline <baseline_deep.json>]
Sources (best-effort, all optional — absence is recorded, never fatal):
  /tmp/gt_run_summary_<task>.json   per-layer eligible/emitted/suppressed/rendered_tokens/util
  /tmp/gt_layer_events_<task>.jsonl  layer firings
  /tmp/gt_interactions_<task>.jsonl  every delivery
  <results_dir>/**/output.jsonl      the agent trajectory (TRUTH for delivery + tokens)
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys


def d8(x) -> float:
    """Round to 8 decimal places — full precision, never 2-dp. NaN/None -> 0.0."""
    try:
        return round(float(x), 8)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Outcome classification enum (TASK 1) — the eight terminal states a task lands
# in. The contract: a preflight/infra/HF/dataset failure must NEVER be charged
# to GT as a "no-patch" failure. That is the whole point of failure_stage.
# ---------------------------------------------------------------------------
OUTCOMES = (
    "resolved",
    "unresolved_with_patch",
    "unresolved_no_patch_agent_ran",
    "preflight_failed_agent_not_started",
    "infra_failed_agent_not_started",
    "dataset_missing_agent_not_started",
    "timeout",
    "cancelled",
)


def _safe_read_text(path: str, max_bytes: int = 8_000_000) -> str:
    """Read a log file best-effort. Never raises. Caps to avoid OOM on huge logs."""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def _git(*args: str) -> str:
    """Run a git command from the repo root, returning stripped stdout or ''."""
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=15
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _find_output_jsonl(task: str, results_dir: str) -> str | None:
    for base in (results_dir, f"/tmp/results_{task}", f"/tmp/gt/{task}"):
        if not base:
            continue
        hits = glob.glob(os.path.join(base, "**", "output.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return None


def _find_log(task: str, results_dir: str, explicit: str = "") -> str | None:
    """Locate the run log for this task. OpenHands writes full_run.log; DeepSWE
    writes trial_output.log. Search the artifact dir, /tmp, and explicit path."""
    if explicit and os.path.exists(explicit):
        return explicit
    names = ("full_run.log", "trial_output.log")
    # Only task-scoped bases — never a bare /tmp recursive glob (would pull a
    # sibling task's log and misclassify this one).
    bases = [results_dir, f"/tmp/results_{task}", f"/tmp/gt_debug_{task}", f"/tmp/gt/{task}"]
    for base in bases:
        if not base:
            continue
        for name in names:
            direct = os.path.join(base, name)
            if os.path.exists(direct):
                return direct
            hits = glob.glob(os.path.join(base, "**", name), recursive=True)
            if hits:
                return hits[0]
    for cand in (f"/tmp/agent_{task}.log", f"/tmp/{task}.log"):
        if os.path.exists(cand):
            return cand
    return None


# OpenHands trajectory/log fingerprints vs DeepSWE (pier + mini-swe-agent).
_OH_MARKERS = ("openhands:INFO", "run_infer.py", "CodeActAgent", "/output.jsonl", "AgentController")
_DEEPSWE_MARKERS = (
    "gt-mini-swe-agent", "mini-swe-agent", "pier view jobs", "NonZeroAgentExitCodeError",
    "trial_output", "[GT L1]", "Results written to jobs/", "Total runtime:",
)


def detect_pipeline(task: str, results_dir: str, log_path: str, oj: str | None,
                    explicit: str = "") -> str:
    """Infer which official pipeline produced these artifacts.
    Returns 'swe-live-openhands' or 'deepswe-miniswe'. An explicit --pipeline wins."""
    if explicit:
        e = explicit.strip().lower()
        if "deep" in e or "mini" in e or "pier" in e:
            return "deepswe-miniswe"
        if "open" in e or "oh" in e or "live" in e:
            return "swe-live-openhands"
    # An output.jsonl trajectory is OpenHands-only.
    if oj and os.path.exists(oj):
        return "swe-live-openhands"
    # Log-name heuristic first (cheap, decisive).
    if log_path and os.path.basename(log_path) == "trial_output.log":
        return "deepswe-miniswe"
    if log_path and os.path.basename(log_path) == "full_run.log":
        # full_run.log is OH, but content-check to be safe.
        text = _safe_read_text(log_path, 400_000)
        if any(m in text for m in _DEEPSWE_MARKERS) and not any(m in text for m in _OH_MARKERS):
            return "deepswe-miniswe"
        return "swe-live-openhands"
    # Fall back to content fingerprints across whatever log we have.
    text = _safe_read_text(log_path, 400_000) if log_path else ""
    oh_hits = sum(1 for m in _OH_MARKERS if m in text)
    ds_hits = sum(1 for m in _DEEPSWE_MARKERS if m in text)
    if ds_hits > oh_hits:
        return "deepswe-miniswe"
    return "swe-live-openhands"


def classify_outcome(task: str, log_path: str, traj: dict, summ: dict,
                     pipeline: str) -> dict:
    """TASK 1 — return the terminal outcome enum + failure attribution.

    Signal precedence (a real start-blocking failure must win over no-patch):
      1. dataset_missing  -> wrapper raised it, agent never started.
      2. preflight_failed -> "PREFLIGHT:" with FAILURES / illegitimate prebuilt,
                             and no agent actions.
      3. infra_failed     -> HF 429 / FileNotFoundError / build|index|LSP fatal
                             before the agent started.
      4. timeout/cancelled-> explicit job signals.
      5. agent ran        -> resolved / unresolved_with_patch /
                             unresolved_no_patch_agent_ran.
    """
    text = _safe_read_text(log_path)
    low = text.lower()

    # Did the agent actually start? OH = CodeActAgent steps; DeepSWE = pier steps
    # / [GT L1] seeding past index / mini-swe-agent banner; either path = actions
    # observed in the trajectory.
    agent_actions = int(traj.get("action_count", 0) or 0)
    agent_started = bool(agent_actions > 0)
    if not agent_started and text:
        oh_started = any(m in text for m in ("CodeActAgent", "AgentController", "step "))
        ds_started = ("mini-swe-agent" in text or "gt-mini-swe-agent" in text
                      or "Total runtime:" in text or "Reward" in text)
        # [GT L1] alone is index-time (pre-agent); require an agent banner.
        agent_started = bool(oh_started or ds_started)

    has_patch = bool(traj.get("has_patch")) or _log_has_patch(text)
    resolved = traj.get("resolved")
    if resolved is None:
        resolved = _resolved_from_log(text)

    failure_stage = "none"
    failure_reason = ""
    outcome = ""

    # 1. dataset missing — wrapper raises this exact token.
    if "dataset_missing_agent_not_started" in text:
        return _verdict("dataset_missing_agent_not_started", "dataset", False,
                        _first_match(text, [r".*dataset_missing_agent_not_started.*"])
                        or "dataset_missing_agent_not_started", resolved, has_patch)

    # 2. preflight failure — only when the agent never started.
    preflight_fail = (
        bool(re.search(r"PREFLIGHT:\s*\d+\s+FAILURES?", text))
        or bool(re.search(r"=== .*PREFLIGHT:\s*FAIL", text))
        or "PREFLIGHT FAILED" in text
        or "illegitimate_prebuilt_artifact_detected" in text
    )
    if preflight_fail and not agent_started:
        reason = (_first_match(text, [
            r".*illegitimate_prebuilt_artifact_detected.*",
            r".*PREFLIGHT:\s*\d+\s+FAILURES?.*",
            r".*PREFLIGHT.*FAIL.*",
        ]) or "preflight failed")
        return _verdict("preflight_failed_agent_not_started", "preflight", False,
                        reason, resolved, has_patch)

    # 3. infra failure before the agent started (HF 429, FileNotFoundError,
    #    build/index/LSP fatal). Classify the stage from the matched signal.
    infra_patterns = [
        (r"(?i)429.*too many requests|huggingface.*429|rate.?limit.*load_dataset", "infra"),
        (r"(?i)couldn'?t reach .* on the hub|connectionerror.*datasets|hfhubhttperror", "infra"),
        (r"(?i)filenotfounderror", "infra"),
        (r"(?i)docker.*(buildx|build failed|cannot connect)|no space left on device", "infra"),
        (r"(?i)(index|gt-index).*(fatal|failed|aborted|exit status [1-9])", "index"),
        (r"(?i)(lsp|pyright|gopls|rust-analyzer).*(fatal|crash|failed to (start|launch))", "lsp"),
        (r"(?i)traceback \(most recent call last\)", "infra"),
    ]
    if not agent_started and not has_patch:
        for pat, stage in infra_patterns:
            m = re.search(pat, text)
            if m:
                reason = _line_around(text, m.start()) or m.group(0)
                return _verdict("infra_failed_agent_not_started", stage, False,
                                reason, resolved, has_patch)

    # 4. timeout / cancelled from explicit job signals.
    if re.search(r"(?i)\b(timed? out|timeout exceeded|MaxIterError|max iterations reached and)\b", text) \
            and not has_patch:
        return _verdict("timeout", "agent", agent_started,
                        _first_match(text, [r"(?i).*(timed? out|timeout|MaxIterError).*"])
                        or "timeout", resolved, has_patch)
    if re.search(r"(?i)\b(cancelled|canceled|KeyboardInterrupt|SIGTERM|job cancelled)\b", text) \
            and not has_patch and not resolved:
        return _verdict("cancelled", "agent", agent_started,
                        _first_match(text, [r"(?i).*(cancelled|canceled|KeyboardInterrupt|SIGTERM).*"])
                        or "cancelled", resolved, has_patch)

    # 5. agent-ran terminal states.
    if resolved is True:
        outcome, failure_stage = "resolved", "none"
    elif has_patch:
        outcome, failure_stage = "unresolved_with_patch", "none"
    elif agent_started:
        outcome, failure_stage = "unresolved_no_patch_agent_ran", "agent"
        failure_reason = "agent ran but produced no patch"
    else:
        # No agent, no patch, no recognizable infra/preflight signal: record honestly
        # as infra (start blocked) rather than charging GT a no-patch failure.
        outcome, failure_stage = "infra_failed_agent_not_started", "infra"
        failure_reason = "agent never started and no patch; stage unknown"

    return _verdict(outcome, failure_stage, agent_started, failure_reason, resolved, has_patch)


def _verdict(outcome: str, stage: str, agent_started: bool, reason: str,
             resolved, has_patch: bool) -> dict:
    return {
        "outcome": outcome,
        "failure_stage": stage,
        "failure_reason": (reason or "").strip()[:500],
        "agent_started": bool(agent_started),
        "resolved": resolved,
        "has_patch": bool(has_patch),
    }


def _first_match(text: str, patterns: list[str]) -> str:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()[:500]
    return ""


def _line_around(text: str, idx: int) -> str:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:500]


def _log_has_patch(text: str) -> bool:
    if not text:
        return False
    # OH records Test Result: {'git_patch': 'diff --git ...'}; a non-empty diff is a patch.
    if re.search(r"'git_patch':\s*'diff --git", text):
        return True
    if re.search(r'"git_patch":\s*"diff --git', text):
        return True
    return False


def _resolved_from_log(text: str) -> bool | None:
    if not text:
        return None
    m = re.search(r"(?i)\"?resolved\"?\s*[:=]\s*(true|false)", text)
    if m:
        return m.group(1).lower() == "true"
    # DeepSWE pier reward: Reward 1.0 == resolved, 0.0 == not.
    m = re.search(r"(?i)\breward\b\D{0,40}?\b1\.0\b", text)
    if m:
        return True
    return None


def _from_trajectory(task: str, results_dir: str) -> dict:
    """The AGENT'S side — derived from output.jsonl history (the only delivery truth)."""
    oj = _find_output_jsonl(task, results_dir)
    out = {
        "output_jsonl": oj or "",
        "action_count": 0,
        "edits": 0,
        "first_edit_action": 0,
        "flows_delivered": 0,
        "contracts_delivered": 0,
        "consensus_delivered": 0,
        "test_delivered": 0,
        "gt_observation_chars_total": 0,
        "resolved": None,
        "has_patch": False,
    }
    if not oj or not os.path.exists(oj):
        return out
    try:
        d = json.loads(open(oj, encoding="utf-8").readline())
    except (OSError, json.JSONDecodeError, StopIteration):
        return out
    hist = d.get("history", [])
    n = 0
    for e in hist:
        if e.get("action"):
            n += 1
            a = e.get("args", {})
            if e.get("action") in ("edit",) or "str_replace" in str(a.get("command", "")):
                out["edits"] += 1
                if not out["first_edit_action"]:
                    out["first_edit_action"] = n
        c = e.get("content") or ""
        if c:
            if "flows:" in c:
                out["flows_delivered"] += 1
            if "[CONTRACT]" in c:
                out["contracts_delivered"] += 1
            if "gt-scope" in c or "CONSENSUS" in c:
                out["consensus_delivered"] += 1
            if "[TEST" in c.upper() or "Called by" in c:
                out["test_delivered"] += 1
            if c.startswith("[GT]") or "<gt-" in c:
                out["gt_observation_chars_total"] += len(c)
    out["action_count"] = n
    tr = d.get("test_result") or {}
    p = tr.get("git_patch") or d.get("git_patch") or ""
    out["has_patch"] = bool(p.strip())
    out["resolved"] = d.get("resolved")
    return out


# --- DeepSeek pricing (USD per 1M tokens) — api-docs.deepseek.com/quick_start/pricing,
#     verified 2026-06-05. deepseek-chat == non-thinking deepseek-v4-flash (same price). ---
DEEPSEEK_PRICING = {
    "deepseek-v4-flash": {"hit": 0.0028, "miss": 0.14, "out": 0.28},
    "deepseek-v4-pro": {"hit": 0.003625, "miss": 0.435, "out": 0.87},
    "deepseek-chat": {"hit": 0.0028, "miss": 0.14, "out": 0.28},
}


def _deepseek_price_for(model: str) -> dict:
    m = (model or "").lower()
    # most specific match first (pro before the generic flash/chat fallthrough)
    for key in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"):
        if key in m:
            return DEEPSEEK_PRICING[key]
    return DEEPSEEK_PRICING["deepseek-v4-flash"]  # cheapest default — never over-charge


def _find_miniswe_trajectory(task: str, results_dir: str) -> str | None:
    for base in (results_dir, f"/tmp/results_{task}", f"/tmp/gt/{task}", "."):
        if not base:
            continue
        hits = glob.glob(
            os.path.join(base, "**", "mini-swe-agent.trajectory.json"), recursive=True)
        if hits:
            return hits[0]
    return None


def _from_miniswe_trajectory(task: str, results_dir: str) -> dict:
    """DeepSWE/pier truth: mini-swe-agent.trajectory.json (output.jsonl never written).
    Extracts agent behaviour, DeepSeek token usage (incl cache hit/miss → real cost),
    and the GT content that actually reached the agent's OBSERVATIONS (showcase counts)."""
    out = {
        "found": False, "trajectory_path": "", "model": "", "exit_status": "",
        "action_count": 0, "edits": 0, "first_edit_action": 0,
        "has_patch": False, "resolved": None,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit_tokens": 0, "cache_miss_tokens": 0, "cost_usd": 0.0,
        "gt_brief_delivered": 0, "gt_evidence_delivered": 0, "gt_graph_map_delivered": 0,
        "gt_nudge_delivered": 0, "gt_understand_calls": 0, "gt_verify_calls": 0,
        "gt_observation_chars_total": 0,
    }
    tj = _find_miniswe_trajectory(task, results_dir)
    if not tj:
        return out
    d = _load_json(tj)
    if not isinstance(d, dict):
        return out
    out["found"] = True
    out["trajectory_path"] = tj
    info = d.get("info", {}) or {}
    cfg = info.get("config", {}) or {}
    model_cfg = cfg.get("model")
    if isinstance(model_cfg, dict):  # pier nests it: config.model.model_name
        out["model"] = str(model_cfg.get("model_name") or model_cfg.get("model") or "")
    else:
        out["model"] = str(cfg.get("model_name") or model_cfg or "")
    out["exit_status"] = str(info.get("exit_status", ""))
    sub = str(info.get("submission") or "")
    out["has_patch"] = bool("diff --git" in sub)
    out["action_count"] = int((info.get("model_stats", {}) or {}).get("api_calls", 0) or 0)

    step = 0
    n_assist = 0
    for m in d.get("messages", []) or []:
        role = m.get("role")
        content = m.get("content") or ""
        usage = None
        extra = m.get("extra")
        if isinstance(extra, dict):
            usage = (extra.get("response") or {}).get("usage")
        if isinstance(usage, dict):
            out["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            out["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            out["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
            out["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            out["cache_miss_tokens"] += int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        if role == "assistant":
            n_assist += 1
            step += 1
            # The executed command lives in tool_calls (mini-swe-agent), NOT content
            # (content is just the THOUGHT). Scan the structured tool_calls text.
            cmd = json.dumps(m.get("tool_calls") or "")
            if isinstance(content, str):
                cmd += content
            out["gt_understand_calls"] += cmd.count("gt_hook.py understand")
            out["gt_verify_calls"] += cmd.count("gt_hook.py verify")
            if ("sed -i" in cmd or "str_replace" in cmd
                    or "apply_patch" in cmd or "tee " in cmd):
                out["edits"] += 1
                if not out["first_edit_action"]:
                    out["first_edit_action"] = step
        elif role in ("tool", "user"):
            # GT content in the agent's OBSERVATIONS = fired AND delivered (the truth)
            out["gt_brief_delivered"] += content.count("<gt-task-brief>")
            out["gt_evidence_delivered"] += content.count("<gt-evidence>")
            out["gt_graph_map_delivered"] += content.count("<gt-graph-map>")
            out["gt_nudge_delivered"] += content.count("<gt-nudge")
            if "<gt-" in content or content.lstrip().startswith("GT:"):
                out["gt_observation_chars_total"] += len(content)
    if out["action_count"] == 0:
        out["action_count"] = n_assist

    # resolved from the pier verifier reward.txt (sibling of the agent/ dir)
    reward_path = os.path.join(os.path.dirname(os.path.dirname(tj)), "verifier", "reward.txt")
    if os.path.exists(reward_path):
        try:
            out["resolved"] = bool(float(open(reward_path).read().strip() or "0") >= 1.0)
        except (OSError, ValueError):
            pass

    # DeepSeek-priced cost: cache hit + cache miss + output, separately
    p = _deepseek_price_for(out["model"] or "deepseek-v4-flash")
    out["cost_usd"] = d8(
        out["cache_hit_tokens"] / 1e6 * p["hit"]
        + out["cache_miss_tokens"] / 1e6 * p["miss"]
        + out["completion_tokens"] / 1e6 * p["out"]
    )
    return out


def _from_cost_log(log_path: str) -> dict:
    """LLM token/cost efficiency from the run log's [GT_COST] lines:
    `[GT_COST] call=N in=X out=Y cached=Z cost=$W total=$T ...`. Summed across calls."""
    import re
    out = {"llm_calls": 0, "llm_tokens_in": 0, "llm_tokens_out": 0,
           "llm_tokens_cached": 0, "llm_cost_usd": 0.0}
    if not log_path or not os.path.exists(log_path):
        return out
    pat = re.compile(
        r"\[GT_COST\]\s+call=(\d+)\s+in=(\d+)\s+out=(\d+)\s+cached=(\d+)\s+cost=\$([0-9.]+)"
    )
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if not m:
                    continue
                out["llm_calls"] += 1
                out["llm_tokens_in"] += int(m.group(2))
                out["llm_tokens_out"] += int(m.group(3))
                out["llm_tokens_cached"] += int(m.group(4))
                out["llm_cost_usd"] += float(m.group(5))
    except OSError:
        pass
    return out


def _resolve_db_path(task: str, results_dir: str, explicit: str = "") -> str:
    """Find graph.db via --db / env / artifact dir. Returns '' if none found."""
    for cand in (explicit, os.environ.get("GT_GRAPH_DB"),
                 os.environ.get("GT_PREBUILT_GRAPH_DB")):
        if cand and os.path.exists(cand):
            return cand
    # Task-scoped bases only — a bare /tmp glob would attach an unrelated repo's
    # graph.db to this task's record.
    for base in (results_dir, f"/tmp/results_{task}", f"/tmp/gt_debug_{task}", f"/tmp/gt/{task}"):
        if not base:
            continue
        cand = os.path.join(base, "graph.db")
        if os.path.exists(cand):
            return cand
        hits = glob.glob(os.path.join(base, "**", "graph.db"), recursive=True)
        if hits:
            return hits[0]
    return ""


def _table_exists(con: "sqlite3.Connection", name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _scalar(con: "sqlite3.Connection", sql: str, args: tuple = ()) -> int:
    try:
        row = con.execute(sql, args).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def _from_graph_db(db_path: str) -> dict:
    """TASK 2 graph-derived fields — query graph.db directly. Every reader is
    guarded; a missing table yields 0/'' rather than crashing."""
    out = {
        "graph_db_path": db_path or "",
        "graph_nodes": 0,
        "graph_edges": 0,
        "verified_edge_count": 0,
        "verified_edge_ratio": 0.0,
        "fts5_row_count": 0,
        "fts5_real_query_result_count": 0,
        "data_flow_row_count": 0,
        "assertion_count": 0,
        "linked_assertion_count": 0,
        "lsp_return_type_signature_count": 0,
        "lsp_enriched_edge_count": 0,
    }
    if not db_path or not os.path.exists(db_path):
        return out
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            con = sqlite3.connect(db_path)
        except sqlite3.Error:
            return out
    try:
        out["graph_nodes"] = _scalar(con, "SELECT COUNT(*) FROM nodes")
        out["graph_edges"] = _scalar(con, "SELECT COUNT(*) FROM edges")
        out["verified_edge_count"] = _scalar(
            con, "SELECT COUNT(*) FROM edges WHERE confidence >= 0.9")
        if out["graph_edges"]:
            out["verified_edge_ratio"] = d8(
                out["verified_edge_count"] / out["graph_edges"])
        # FTS5 table is nodes_fts on current binaries, symbols_fts on the legacy
        # Python indexer schema. Probe both; count rows + run a real MATCH query.
        for fts in ("nodes_fts", "symbols_fts"):
            if _table_exists(con, fts):
                rows = _scalar(con, f"SELECT COUNT(*) FROM {fts}")
                out["fts5_row_count"] = max(out["fts5_row_count"], rows)
                # A real (non-count) query — proves the FTS index actually returns hits.
                try:
                    r = con.execute(
                        f"SELECT COUNT(*) FROM {fts} WHERE {fts} MATCH ?",
                        ("get OR set OR init OR run OR handle",),
                    ).fetchone()
                    if r and r[0]:
                        out["fts5_real_query_result_count"] = max(
                            out["fts5_real_query_result_count"], int(r[0]))
                except sqlite3.Error:
                    pass
        if _table_exists(con, "data_flow"):
            out["data_flow_row_count"] = _scalar(con, "SELECT COUNT(*) FROM data_flow")
        if _table_exists(con, "assertions"):
            out["assertion_count"] = _scalar(con, "SELECT COUNT(*) FROM assertions")
            out["linked_assertion_count"] = _scalar(
                con,
                "SELECT COUNT(*) FROM assertions WHERE target_node_id IS NOT NULL "
                "AND target_node_id != 0",
            )
        # LSP enrichment proxies: nodes carrying a populated return_type/signature
        # are the LSP-enriched contracts; import/same_file edges are verified.
        out["lsp_return_type_signature_count"] = _scalar(
            con,
            "SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL "
            "AND TRIM(return_type) != ''",
        )
        out["lsp_enriched_edge_count"] = _scalar(
            con,
            "SELECT COUNT(*) FROM edges WHERE resolution_method IN "
            "('import','same_file') ",
        )
    finally:
        con.close()
    return out


_LSP_BY_LANG = {
    "python": "pyright", "go": "gopls", "rust": "rust-analyzer",
    "typescript": "typescript-language-server", "javascript": "typescript-language-server",
    "ts": "typescript-language-server", "js": "typescript-language-server",
    "java": "jdtls",
}


def _from_lsp(log_text: str, db_path: str) -> dict:
    """LSP server name + launch status. Inferred from log markers, else from the
    dominant language in graph.db (the server that WOULD be dispatched)."""
    out = {"lsp_server_name": "unknown", "lsp_launch_status": "unknown"}
    servers = ("pyright", "gopls", "rust-analyzer", "typescript-language-server", "jdtls")
    found = ""
    for s in servers:
        if log_text and s in log_text:
            found = s
            break
    if found:
        out["lsp_server_name"] = found
        if re.search(rf"(?i){re.escape(found)}.*(launched|started|ready|initialized)", log_text):
            out["lsp_launch_status"] = "launched"
        elif re.search(rf"(?i){re.escape(found)}.*(fail|crash|not found|missing|error)", log_text):
            out["lsp_launch_status"] = "failed"
        else:
            out["lsp_launch_status"] = "detected"
        return out
    # No log marker — infer the dispatch target from the graph's dominant language.
    if db_path and os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT language, COUNT(*) c FROM nodes WHERE language IS NOT NULL "
                    "GROUP BY language ORDER BY c DESC LIMIT 1"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                out["lsp_server_name"] = _LSP_BY_LANG.get(str(row[0]).lower(), "unknown")
                out["lsp_launch_status"] = "not_observed_in_log"
        except sqlite3.Error:
            pass
    return out


def _env_snapshot() -> dict:
    """TASK 2 — every GT_REQUIRE_*/GT_FORCE_*/GT_FORBID_*/HF_*OFFLINE env var
    currently set (the run's hard-gate configuration)."""
    snap = {}
    for k, v in os.environ.items():
        if (k.startswith("GT_REQUIRE_") or k.startswith("GT_FORCE_")
                or k.startswith("GT_FORBID_")
                or (k.startswith("HF_") and "OFFLINE" in k)):
            snap[k] = v
    return snap


def _from_embedder() -> dict:
    """TASK 2 — load the embedding model and embed a probe to confirm it is real.
    semantic_enabled = model loaded AND the probe vector is nonzero."""
    out = {
        "embedder_path": "",
        "embedder_vector_dim": 0,
        "embedder_nonzero": False,
        "semantic_enabled": False,
    }
    try:
        from groundtruth.memory.enrich.embed import get_embedding_model

        model = get_embedding_model()
        try:
            out["embedder_path"] = str(model.model_dir)
        except Exception:
            out["embedder_path"] = ""
        vec = model.embed("def get_user(id): return None", is_query=True)
        out["embedder_vector_dim"] = int(len(vec))
        out["embedder_nonzero"] = bool(any(abs(float(x)) > 1e-12 for x in vec))
        out["semantic_enabled"] = bool(out["embedder_vector_dim"] > 0 and out["embedder_nonzero"])
    except Exception as exc:  # noqa: BLE001 - embedder is optional, never fatal
        out["embedder_path"] = f"unavailable: {type(exc).__name__}: {str(exc)[:120]}"
    return out


_TIERS = (("verified", 0.9), ("warning", 0.5), ("info", 0.0))


def _tier_for(score: float) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 0.9:
        return "verified"
    if s >= 0.5:
        return "warning"
    return "info"


def _from_l1_block(summ: dict) -> dict:
    """TASK 2 L1 fields. A peer agent is adding graph_edge_count / semantic /
    structural / fts5 / confidence_tier to the brief — read them if present, else
    fall back to the existing l1 telemetry keys, else 0/'unknown'."""
    l1 = summ.get("l1", {}) or {}

    def _num(*keys):
        for k in keys:
            v = l1.get(k)
            if isinstance(v, (int, float)):
                return d8(v)
        return 0.0

    edge = _num("graph_edge_count", "l1_candidates_with_graph_edge_count",
                "l1_candidates_with_call_edge_count")
    sem = _num("semantic_signal_count", "l1_candidates_with_semantic_count",
               "l1_semantic_signal_count")
    struct = _num("structural_signal_count", "l1_candidates_with_signature_count",
                  "l1_structural_signal_count")
    lex = _num("fts5_signal_count", "lexical_signal_count",
               "l1_candidates_with_bm25_signal_count", "l1_lexical_signal_count")
    tier = l1.get("confidence_tier") or l1.get("l1_confidence_tier")
    if not tier:
        score = l1.get("l1_confidence_score")
        tier = _tier_for(score) if isinstance(score, (int, float)) and score else "unknown"
    return {
        "l1_graph_edge_count": edge,
        "l1_semantic_signal_count": sem,
        "l1_structural_signal_count": struct,
        "l1_lexical_signal_count": lex,
        "l1_confidence_tier": str(tier),
    }


def _from_l3_block(summ: dict) -> dict:
    """TASK 2 L3 fields. real_evidence = emitted code-bearing evidence (callers,
    signatures, assertions, sibling patterns); metadata_only = suppressed/weak."""
    l3 = summ.get("l3", {}) or {}

    def _num(*keys):
        for k in keys:
            v = l3.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    # Real evidence = code-bearing emitted evidence. Prefer the explicit emitted
    # count; fall back to a presence-OR over caller/signature/assertion/sibling.
    real = _num("l3_evidence_emitted")
    if real == 0:
        real = max(
            min(_num("l3_caller_code_line_count"), 1),
            min(_num("l3_consumer_count"), 1),
            min(_num("l3_signature_count"), 1),
            min(_num("l3_test_assertion_count"), 1),
            min(_num("l3_sibling_pattern_count"), 1),
        )
    meta = _num("l3_suppressed_count") + _num("l3_weak_evidence_flag")
    return {
        "l3_real_evidence_count": d8(real),
        "l3_metadata_only_count": d8(meta),
    }


def _from_l3b_block(summ: dict) -> dict:
    """TASK 2 L3b token-budget fields. cap_enforced iff rendered tokens were
    clipped to the band cap."""
    l3b = summ.get("l3b", {}) or {}
    per_layer = summ.get("per_layer", {}) or {}
    rendered = per_layer.get("L3b", {}).get("rendered_tokens_total")

    def _num(v):
        return int(v) if isinstance(v, (int, float)) else 0

    cap = _num(l3b.get("l3b_token_cap_for_band")) or _num(l3b.get("l3b_token_cap"))
    tokens = _num(rendered) if isinstance(rendered, (int, float)) else \
        _num(l3b.get("l3b_token_count"))
    cap_enforced = bool(cap and tokens >= cap)
    if not cap and isinstance(l3b.get("l3b_decay_applied"), bool):
        cap_enforced = bool(l3b.get("l3b_decay_applied"))
    return {
        "l3b_token_count": d8(tokens),
        "l3b_token_cap": d8(cap),
        "l3b_cap_enforced": cap_enforced,
    }


def build(task: str, results_dir: str, log_path: str = "",
          db_path: str = "", pipeline_arg: str = "") -> dict:
    summ = _load_json(f"/tmp/gt_run_summary_{task}.json") or {}
    per_layer_raw = summ.get("per_layer", {})
    per_layer = {}
    inj_tokens_total = 0.0
    for layer, m in per_layer_raw.items():
        rt = d8(m.get("rendered_tokens_total", 0))
        inj_tokens_total += rt
        elig = d8(m.get("eligible", 0))
        emit = d8(m.get("emitted", 0))
        per_layer[layer] = {
            "eligible": elig,
            "emitted": emit,
            "suppressed": d8(m.get("suppressed", 0)),
            "rendered_tokens_total": rt,
            "utilization_score": d8(m.get("utilization_score", 0)),
            "next_action_count": d8(m.get("next_action_count", 0)),
            "emit_rate": d8(emit / elig) if elig else 0.0,
        }
    # --- resolve inputs for both pipelines (graceful when absent) -----------
    oj = _find_output_jsonl(task, results_dir)
    if not log_path:
        log_path = _find_log(task, results_dir) or ""
    pipeline = detect_pipeline(task, results_dir, log_path, oj, pipeline_arg)
    db_resolved = _resolve_db_path(task, results_dir, db_path)
    log_text = _safe_read_text(log_path) if log_path else ""

    traj = _from_trajectory(task, results_dir)
    # DeepSWE/pier writes mini-swe-agent.trajectory.json, NOT output.jsonl — read it as
    # the agent-side truth (steps, edits, patch, tokens, GT delivery) when OH is absent.
    mini = _from_miniswe_trajectory(task, results_dir)
    if (not traj.get("action_count")) and mini.get("found"):
        traj["action_count"] = mini["action_count"]
        traj["edits"] = mini["edits"]
        traj["first_edit_action"] = mini["first_edit_action"]
        traj["has_patch"] = traj.get("has_patch") or mini["has_patch"]
        if traj.get("resolved") is None:
            traj["resolved"] = mini["resolved"]
        # GT delivery to the agent's observations (fired AND delivered) → showcase block
        traj["gt_brief_delivered"] = mini["gt_brief_delivered"]
        traj["gt_evidence_delivered"] = mini["gt_evidence_delivered"]
        traj["gt_graph_map_delivered"] = mini["gt_graph_map_delivered"]
        traj["gt_nudge_delivered"] = mini["gt_nudge_delivered"]
        traj["gt_understand_calls"] = mini["gt_understand_calls"]
        traj["gt_verify_calls"] = mini["gt_verify_calls"]
        traj["gt_observation_chars_total"] = mini["gt_observation_chars_total"]
    # If output.jsonl was absent (e.g. DeepSWE), recover patch/action signal from
    # the log so classification still works.
    if (not traj.get("action_count")) and log_text:
        traj["has_patch"] = traj.get("has_patch") or _log_has_patch(log_text)

    cost = _from_cost_log(log_path)
    # DeepSWE: no [GT_COST] log lines (litellm unmapped) — derive tokens + DeepSeek-priced
    # cost from the pier trajectory's per-call usage (incl cache hit/miss) instead.
    if (not cost["llm_calls"]) and mini.get("found") and mini.get("total_tokens"):
        cost = {
            "llm_calls": mini["action_count"],
            "llm_tokens_in": mini["prompt_tokens"],
            "llm_tokens_out": mini["completion_tokens"],
            "llm_tokens_cached": mini["cache_hit_tokens"],
            "llm_cost_usd": mini["cost_usd"],
        }
    actions = traj.get("action_count", 0) or 0
    llm_total = cost["llm_tokens_in"] + cost["llm_tokens_out"]
    # token/cost EFFICIENCY (the constitution's honest token story: GT injection vs LLM usage)
    efficiency = {
        "model": mini.get("model") or "",
        "cost_source": "deepseek_priced_trajectory" if (mini.get("found") and mini.get("total_tokens")) else "gt_cost_log",
        "llm_calls": d8(cost["llm_calls"]),
        "llm_tokens_in": d8(cost["llm_tokens_in"]),
        "llm_tokens_out": d8(cost["llm_tokens_out"]),
        "llm_tokens_cached": d8(cost["llm_tokens_cached"]),
        "llm_cache_hit_tokens": d8(mini.get("cache_hit_tokens", 0)),
        "llm_cache_miss_tokens": d8(mini.get("cache_miss_tokens", 0)),
        "llm_tokens_total": d8(llm_total),
        "llm_cost_usd": d8(cost["llm_cost_usd"]),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "tokens_per_action": d8(llm_total / actions) if actions else 0.0,
        "cost_per_action_usd": d8(cost["llm_cost_usd"] / actions) if actions else 0.0,
        # GT's added context as a fraction of total LLM input — the honest overhead figure
        "gt_injection_overhead_pct": d8(100.0 * inj_tokens_total / cost["llm_tokens_in"]) if cost["llm_tokens_in"] else 0.0,
    }
    # --- TASK 1: outcome classification (start-blocking failures win) --------
    verdict = classify_outcome(task, log_path, traj, summ, pipeline)

    # --- TASK 2: required spec-F field set ----------------------------------
    graph = _from_graph_db(db_resolved)
    lsp = _from_lsp(log_text, db_resolved)
    embedder = _from_embedder()
    env_snap = _env_snapshot()
    l1f = _from_l1_block(summ)
    l3f = _from_l3_block(summ)
    l3bf = _from_l3b_block(summ)

    summ_present = bool(summ)
    deep = {
        "task_id": task,
        "schema": "gt_deep_metrics.v2",
        "precision_decimals": 8,
        "pipeline": pipeline,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "git_commit": _git("rev-parse", "HEAD") or "unknown",
        # --- outcome / failure attribution (TASK 1) ---
        "outcome": verdict["outcome"],
        "agent_started": verdict["agent_started"],
        "failure_stage": verdict["failure_stage"],
        "failure_reason": verdict["failure_reason"],
        "resolved": verdict["resolved"],
        "has_patch": verdict["has_patch"],
        # --- graph-derived (TASK 2) ---
        "graph_db_path": graph["graph_db_path"],
        "graph_nodes": d8(graph["graph_nodes"]),
        "graph_edges": d8(graph["graph_edges"]),
        "verified_edge_count": d8(graph["verified_edge_count"]),
        "verified_edge_ratio": d8(graph["verified_edge_ratio"]),
        "fts5_row_count": d8(graph["fts5_row_count"]),
        "fts5_real_query_result_count": d8(graph["fts5_real_query_result_count"]),
        "data_flow_row_count": d8(graph["data_flow_row_count"]),
        "assertion_count": d8(graph["assertion_count"]),
        "linked_assertion_count": d8(graph["linked_assertion_count"]),
        # --- LSP (TASK 2) ---
        "lsp_server_name": lsp["lsp_server_name"],
        "lsp_launch_status": lsp["lsp_launch_status"],
        "lsp_enriched_edge_count": d8(graph["lsp_enriched_edge_count"]),
        "lsp_return_type_signature_count": d8(graph["lsp_return_type_signature_count"]),
        # --- embedder / semantic (TASK 2) ---
        "embedder_path": embedder["embedder_path"],
        "embedder_vector_dim": d8(embedder["embedder_vector_dim"]),
        "embedder_nonzero": embedder["embedder_nonzero"],
        "semantic_enabled": embedder["semantic_enabled"],
        # --- env hard-gate snapshot (TASK 2) ---
        "gt_require_env": env_snap,
        # --- L1 / L3 / L3b spec-F fields (TASK 2) ---
        "l1_graph_edge_count": l1f["l1_graph_edge_count"],
        "l1_semantic_signal_count": l1f["l1_semantic_signal_count"],
        "l1_structural_signal_count": l1f["l1_structural_signal_count"],
        "l1_lexical_signal_count": l1f["l1_lexical_signal_count"],
        "l1_confidence_tier": l1f["l1_confidence_tier"],
        "l3_real_evidence_count": l3f["l3_real_evidence_count"],
        "l3_metadata_only_count": l3f["l3_metadata_only_count"],
        "l3b_token_count": l3bf["l3b_token_count"],
        "l3b_token_cap": l3bf["l3b_token_cap"],
        "l3b_cap_enforced": l3bf["l3b_cap_enforced"],
        # --- existing v1 fields (preserved) ---
        "layers_active": summ.get("layers_active", []),
        "total_layer_events": d8(summ.get("total_layer_events", 0)),
        "total_agent_events": d8(summ.get("total_agent_events", 0)),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "efficiency": efficiency,
        # --- GT-reached-agent SHOWCASE (fired AND delivered, from agent observation) ---
        "gt_delivery": {
            "brief_delivered": d8(traj.get("gt_brief_delivered", 0)),
            "evidence_delivered": d8(traj.get("gt_evidence_delivered", 0)),
            "graph_map_delivered": d8(traj.get("gt_graph_map_delivered", 0)),
            "nudge_delivered": d8(traj.get("gt_nudge_delivered", 0)),
            "understand_calls": d8(traj.get("gt_understand_calls", 0)),
            "verify_calls": d8(traj.get("gt_verify_calls", 0)),
            "gt_observation_chars_total": d8(traj.get("gt_observation_chars_total", 0)),
        },
        "per_layer": per_layer,
        "agent": {k: (d8(v) if isinstance(v, (int, float)) else v) for k, v in traj.items()},
        # --- provenance of optional inputs (what was missing, never fatal) ---
        "inputs_present": {
            "gt_run_summary": summ_present,
            "output_jsonl": bool(oj and os.path.exists(oj)),
            "run_log": bool(log_path and os.path.exists(log_path)),
            "graph_db": bool(db_resolved),
            "cost_log": bool(cost["llm_calls"]),
        },
    }
    return deep


def pair(gt: dict, base: dict) -> dict:
    """GT-on vs baseline deltas at 8-dp (negative delta on action_count = GT is better)."""
    g, b = gt.get("agent", {}), base.get("agent", {})
    def dlt(k):
        return d8(d8(g.get(k, 0)) - d8(b.get(k, 0)))
    return {
        "task_id": gt.get("task_id"),
        "schema": "gt_metrics_delta.v1",
        "precision_decimals": 8,
        "action_count_delta": dlt("action_count"),
        "first_edit_delta": dlt("first_edit_action"),
        "token_delta": d8(d8(gt.get("gt_injected_tokens_total", 0))),  # GT-side injected cost
        "resolved_gt": g.get("resolved"),
        "resolved_baseline": b.get("resolved"),
        "flows_delivered": d8(g.get("flows_delivered", 0)),
        "contracts_delivered": d8(g.get("contracts_delivered", 0)),
    }


def _opt(flag: str, default: str = "") -> str:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _write_markdown(deep: dict, md_path: str) -> None:
    """Human-readable companion to the JSON — explains steps, tokens, money, GT delivery,
    and whether the stack (graph/LSP/semantic) was live. This is the 'explaining' file."""
    eff = deep.get("efficiency", {}) or {}
    g = deep.get("gt_delivery", {}) or {}
    a = deep.get("agent", {}) or {}

    def f(x):
        try:
            s = f"{float(x):.8f}".rstrip("0").rstrip(".")
            return s if s else "0"
        except (TypeError, ValueError):
            return str(x)

    rows = lambda pairs: "\n".join(f"| {k} | {v} |" for k, v in pairs)
    md = f"""# DeepSWE deep metrics — `{deep.get('task_id')}`

- pipeline: `{deep.get('pipeline')}`  ·  model: `{eff.get('model') or 'n/a'}`
- branch `{deep.get('branch')}` @ `{(deep.get('git_commit') or '')[:12]}`
- **outcome: {deep.get('outcome')}**  ·  resolved={deep.get('resolved')}  ·  has_patch={deep.get('has_patch')}

## Steps / agent behaviour
| metric | value |
|---|---|
{rows([("agent steps (api_calls)", f(a.get('action_count'))), ("source edits", f(a.get('edits'))), ("first edit at step", f(a.get('first_edit_action')))])}

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
{rows([("input tokens", f(eff.get('llm_tokens_in'))), ("  cache-hit", f(eff.get('llm_cache_hit_tokens'))), ("  cache-miss", f(eff.get('llm_cache_miss_tokens'))), ("output tokens", f(eff.get('llm_tokens_out'))), ("total tokens", f(eff.get('llm_tokens_total'))), ("**cost USD**", f"**{f(eff.get('llm_cost_usd'))}**"), ("cost / action USD", f(eff.get('cost_per_action_usd'))), ("cost source", eff.get('cost_source'))])}

## GT reached the agent (fired AND delivered — from agent observation)
| surface | count |
|---|---|
{rows([("brief delivered", f(g.get('brief_delivered'))), ("gt-evidence delivered", f(g.get('evidence_delivered'))), ("graph-map delivered", f(g.get('graph_map_delivered'))), ("nudges delivered", f(g.get('nudge_delivered'))), ("gt_hook understand calls", f(g.get('understand_calls'))), ("gt_hook verify calls", f(g.get('verify_calls'))), ("GT observation chars", f(g.get('gt_observation_chars_total')))])}

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
{rows([("graph nodes", f(deep.get('graph_nodes'))), ("graph edges", f(deep.get('graph_edges'))), ("verified edge ratio", f(deep.get('verified_edge_ratio'))), ("LSP-enriched edges", f(deep.get('lsp_enriched_edge_count'))), ("LSP server", f"{deep.get('lsp_server_name')} ({deep.get('lsp_launch_status')})"), ("FTS5 rows / hits", f"{f(deep.get('fts5_row_count'))} / {f(deep.get('fts5_real_query_result_count'))}"), ("semantic (embedder)", f"{deep.get('semantic_enabled')} dim={f(deep.get('embedder_vector_dim'))}"), ("assertions / linked", f"{f(deep.get('assertion_count'))} / {f(deep.get('linked_assertion_count'))}")])}

_inputs present: {deep.get('inputs_present')}_
"""
    try:
        with open(md_path, "w", encoding="utf-8") as fp:
            fp.write(md)
    except OSError:
        pass


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Positionals can be eaten by --flag values; drop any that immediately follow
    # a known value-flag so a task_id is always positional[0].
    val_flags = {"--log", "--db", "--pipeline", "--baseline", "--out"}
    cleaned, skip = [], False
    for tok in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if tok in val_flags:
            skip = True
            continue
        if not tok.startswith("--"):
            cleaned.append(tok)
    args = cleaned
    if not args:
        print("usage: gt_deep_metrics.py <task_id> [results_dir] "
              "[--db graph.db] [--pipeline swe-live-openhands|deepswe-miniswe] "
              "[--log run.log] [--baseline <file>] [--out <file>]")
        return 2
    task = args[0]
    results_dir = args[1] if len(args) > 1 else f"/tmp/results_{task}"
    log_path = _opt("--log")
    if not log_path and os.path.exists(f"/tmp/agent_{task}.log"):
        log_path = f"/tmp/agent_{task}.log"
    db_path = _opt("--db")
    pipeline_arg = _opt("--pipeline")

    # TASK 3 — ALWAYS WRITE. Never crash: even total input loss yields a record
    # carrying outcome/failure_stage/failure_reason + whatever graph/env exists.
    try:
        deep = build(task, results_dir, log_path, db_path, pipeline_arg)
    except Exception as exc:  # noqa: BLE001 — emitter must never fail the run
        deep = {
            "task_id": task,
            "schema": "gt_deep_metrics.v2",
            "precision_decimals": 8,
            "pipeline": pipeline_arg or "unknown",
            "outcome": "infra_failed_agent_not_started",
            "agent_started": False,
            "failure_stage": "infra",
            "failure_reason": f"deep_metrics emitter error: {type(exc).__name__}: {str(exc)[:300]}",
            "resolved": None,
            "has_patch": False,
            "gt_require_env": _env_snapshot(),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "git_commit": _git("rev-parse", "HEAD") or "unknown",
            "efficiency": {}, "per_layer": {}, "agent": {},
            "inputs_present": {},
        }

    out_path = _opt("--out") or f"/tmp/gt_deep_metrics_{task}.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(deep, f, indent=2)
    except OSError as exc:
        print(f"[GT_DEEP] FAILED to write {out_path}: {exc}", file=sys.stderr)
        return 1
    # Human-readable companion (steps / tokens / money / GT delivery / stack).
    _write_markdown(deep, (out_path[:-5] + ".md") if out_path.endswith(".json") else out_path + ".md")
    eff = deep.get("efficiency", {}) or {}
    agent = deep.get("agent", {}) or {}
    print(f"[GT_DEEP] wrote {out_path}: pipeline={deep.get('pipeline')} "
          f"outcome={deep.get('outcome')} stage={deep.get('failure_stage')} "
          f"agent_started={deep.get('agent_started')} resolved={deep.get('resolved')} "
          f"has_patch={deep.get('has_patch')} "
          f"nodes={deep.get('graph_nodes')} edges={deep.get('graph_edges')} "
          f"verified_ratio={deep.get('verified_edge_ratio')} "
          f"fts5_rows={deep.get('fts5_row_count')} fts5_hits={deep.get('fts5_real_query_result_count')} "
          f"assertions={deep.get('assertion_count')}/{deep.get('linked_assertion_count')} "
          f"lsp={deep.get('lsp_server_name')}/{deep.get('lsp_launch_status')} "
          f"semantic={deep.get('semantic_enabled')}(dim={deep.get('embedder_vector_dim')}) "
          f"actions={agent.get('action_count')} "
          f"llm_tokens={eff.get('llm_tokens_total')} cost=${eff.get('llm_cost_usd')}")
    if "--baseline" in sys.argv:
        bpath = sys.argv[sys.argv.index("--baseline") + 1]
        base = _load_json(bpath)
        if base:
            delta = pair(deep, base)
            dpath = f"/tmp/gt_metrics_delta_{task}.json"
            with open(dpath, "w", encoding="utf-8") as f:
                json.dump(delta, f, indent=2)
            print(f"[GT_DEEP] wrote {dpath}: action_count_delta={delta['action_count_delta']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
