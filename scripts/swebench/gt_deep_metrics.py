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
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys

# Single source of shell edit-truth: gt_localization_efficacy.classify is the COMPLETE
# recognizer (patch / git-apply / heredoc / sed / redirect) the efficacy synthesis
# prescribes. It is stdlib-only (same dir), so importing it is cheap and dependency-free.
# A superset regex is kept as a defensive fallback if the module cannot be imported.
try:
    from gt_localization_efficacy import classify as _classify_shell_cmd
except ImportError:  # packaged import path
    try:
        from scripts.swebench.gt_localization_efficacy import classify as _classify_shell_cmd
    except ImportError:  # detector unavailable — degrade to the local superset regex
        _classify_shell_cmd = None

# Fallback ONLY (classify normally handles this). Superset of the old blind token set:
# adds `git apply`, `patch … <`, and `>`/`>>` redirects to a source file.
_EDIT_FALLBACK_RE = re.compile(
    r"\bsed\s+-i\b|\bapply_patch\b|\bgit\s+apply\b|(?<![\w])patch\s+[^\n]*<|\btee\b|"
    r"\bcat\s*>>?\s*[\w./+-]|(?:^|\s)>>?\s*[\w./+-]+\."
    r"(?:py|go|ts|tsx|js|jsx|mjs|cjs|rs|java|kt|rb|c|cc|cpp|h|hpp|html|vue|svelte)\b",
    re.I,
)


def _shell_cmd_is_edit(cmd: str) -> bool:
    """True iff a RAW SHELL command mutates a source file. Delegates to the complete
    detector gt_localization_efficacy.classify (patch/git-apply/heredoc/sed/redirect),
    with a superset-regex fallback if that module cannot be imported. Structured editor
    VERBS (str_replace/view/create) are NOT decided here — the caller's verb guards own
    those; this fires on shell TEXT only, so a `view`/`cat`/`grep` read never counts."""
    if not cmd or not cmd.strip():
        return False
    if _classify_shell_cmd is not None:
        try:
            return _classify_shell_cmd(cmd)[0] == "EDIT"
        except Exception:
            pass
    return bool(_EDIT_FALLBACK_RE.search(cmd))


def d8(x):
    """Round to 8 decimal places — full precision, never 2-dp. Missing data
    (None / NaN / inf / unparseable) -> None (JSON null), NEVER 0.0: a missing
    measurement must be null, not an indistinguishable measured zero (G14)."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 8)


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
        # DETERMINISM (Fable G16): glob returns OS-order; two machines with retry attempts
        # present could meter DIFFERENT trajectories. Sort so the pick is stable.
        hits = sorted(glob.glob(os.path.join(base, "**", "output.jsonl"), recursive=True))
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
                     pipeline: str, results_dir: str = "") -> dict:
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

    # PATCH TRUTH — the ARTIFACT wins over every inference.
    # Precedence: the job's own model.patch file (authoritative, read from disk) >
    # trajectory has_patch (OH git_patch / mini info.submission) > log scrape.
    # The old order made "no patch" a conclusion drawn from an EMPTY submission string;
    # on real runs (wazero 1,211-line patch, info.submission == "") that is a LIE and it
    # sent the task to `unresolved_no_patch_agent_ran` == "the agent gave up".
    patch_facts = _model_patch_facts(task, results_dir) if results_dir else {"has_patch": None}
    if patch_facts.get("has_patch") is not None:
        has_patch = bool(patch_facts["has_patch"])
        patch_source = "model_patch_artifact"
    elif bool(traj.get("has_patch")):
        has_patch, patch_source = True, "trajectory"
    elif _log_has_patch(text):
        has_patch, patch_source = True, "log_git_patch"
    else:
        has_patch, patch_source = False, "no_patch_signal_found"
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
                        or "dataset_missing_agent_not_started", resolved, has_patch, patch_source, patch_facts)

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
                        reason, resolved, has_patch, patch_source, patch_facts)

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
                                reason, resolved, has_patch, patch_source, patch_facts)

    # 4. timeout / cancelled from explicit job signals.
    if re.search(r"(?i)\b(timed? out|timeout exceeded|MaxIterError|max iterations reached and)\b", text) \
            and not has_patch:
        return _verdict("timeout", "agent", agent_started,
                        _first_match(text, [r"(?i).*(timed? out|timeout|MaxIterError).*"])
                        or "timeout", resolved, has_patch, patch_source, patch_facts)
    if re.search(r"(?i)\b(cancelled|canceled|KeyboardInterrupt|SIGTERM|job cancelled)\b", text) \
            and not has_patch and not resolved:
        return _verdict("cancelled", "agent", agent_started,
                        _first_match(text, [r"(?i).*(cancelled|canceled|KeyboardInterrupt|SIGTERM).*"])
                        or "cancelled", resolved, has_patch, patch_source, patch_facts)

    # 5. agent-ran terminal states.
    if resolved is True:
        outcome, failure_stage = "resolved", "none"
    elif has_patch:
        # PATCH PRODUCED, TESTS FAILED — a TASK-class failure, never "the agent gave up".
        # This MUST stay a distinct enum value from unresolved_no_patch_agent_ran.
        outcome, failure_stage = "unresolved_with_patch", "none"
        _n = patch_facts.get("model_patch_lines")
        failure_reason = ("patch produced but hidden tests failed"
                          + (f" (model.patch {_n} lines)" if _n else ""))
    elif agent_started:
        # NO PATCH PRODUCED. Only assertable when the model.patch artifact was READ and
        # was empty; if the artifact is missing entirely, say so — do not silently sell
        # an absence as "the agent produced nothing".
        outcome, failure_stage = "unresolved_no_patch_agent_ran", "agent"
        failure_reason = ("agent ran and produced an EMPTY model.patch"
                          if patch_source == "model_patch_artifact"
                          else "agent ran; no patch signal found and no model.patch "
                               "artifact was present to check (patch state UNVERIFIED)")
    else:
        # No agent, no patch, no recognizable infra/preflight signal: record honestly
        # as infra (start blocked) rather than charging GT a no-patch failure.
        outcome, failure_stage = "infra_failed_agent_not_started", "infra"
        failure_reason = "agent never started and no patch; stage unknown"

    return _verdict(outcome, failure_stage, agent_started, failure_reason, resolved, has_patch, patch_source, patch_facts)


def _verdict(outcome: str, stage: str, agent_started: bool, reason: str,
             resolved, has_patch: bool, patch_source: str = "",
             patch_facts: dict | None = None) -> dict:
    v = {
        "outcome": outcome,
        "failure_stage": stage,
        "failure_reason": (reason or "").strip()[:500],
        "agent_started": bool(agent_started),
        "resolved": resolved,
        "has_patch": bool(has_patch),
        # WHERE has_patch came from. "model_patch_artifact" = read off disk (authoritative);
        # anything else means the artifact was absent and we fell back to an inference.
        "patch_source": patch_source or "",
    }
    for k in ("model_patch_path", "model_patch_bytes", "model_patch_lines",
              "model_patch_is_diff"):
        v[k] = (patch_facts or {}).get(k)
    return v


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
    """The AGENT'S side — derived from the agent's OWN trajectory (the only delivery
    truth). Two official pipelines write two formats: OpenHands writes ``output.jsonl``
    (a JSON-lines history); DeepSWE/pier + mini-swe-agent writes
    ``mini-swe-agent.trajectory.json``. When ``output.jsonl`` is absent (the LIVE
    DeepSWE path) this falls back to ``_find_miniswe_trajectory`` and populates the
    SAME output keys, so the function returns REAL agent metrics on either pipeline
    instead of all-zeros off the OH path. ``trajectory_source`` records which one was
    used so every downstream record (and the companion .md) is self-describing.

    Note: ``build()`` performs an equivalent miniswe merge guarded on
    ``not action_count``; because this function now populates ``action_count`` on the
    DeepSWE path, that merge short-circuits — no double counting."""
    oj = _find_output_jsonl(task, results_dir)
    out = {
        "output_jsonl": oj or "",
        "trajectory_source": "none",
        "action_count": 0,
        "edits": 0,
        # None = NOT DETECTED (missing-data law) — never 0 (which reads as "edited at
        # step 0" and poisons cross-arm first-edit deltas). Set only when an edit is seen.
        "first_edit_action": None,
        "flows_delivered": 0,
        "contracts_delivered": 0,
        "consensus_delivered": 0,
        "test_delivered": 0,
        "gt_observation_chars_total": 0,
        "resolved": None,
        "has_patch": False,
    }
    if not oj or not os.path.exists(oj):
        # No OpenHands output.jsonl — on the DeepSWE/pier path the truth lives in
        # mini-swe-agent.trajectory.json. Parse it and map onto the SAME keys so the
        # function returns real metrics (steps / edits / first_edit / brief delivery /
        # GT observation chars) instead of zeros.
        mini = _from_miniswe_trajectory(task, results_dir)
        if mini.get("found"):
            out["trajectory_source"] = "miniswe_trajectory"
            out["miniswe_trajectory_path"] = mini.get("trajectory_path", "")
            out["action_count"] = mini["action_count"]
            out["assistant_steps"] = mini.get("assistant_steps", mini["action_count"])
            out["edits"] = mini["edits"]
            out["first_edit_action"] = mini["first_edit_action"]
            out["has_patch"] = mini["has_patch"]
            out["resolved"] = mini["resolved"]
            out["gt_brief_delivered"] = mini["gt_brief_delivered"]
            out["gt_evidence_delivered"] = mini["gt_evidence_delivered"]
            out["gt_graph_map_delivered"] = mini["gt_graph_map_delivered"]
            out["gt_scope_delivered"] = mini.get("gt_scope_delivered", 0)
            out["gt_contract_delivered"] = mini.get("gt_contract_delivered", 0)
            out["gt_cochange_delivered"] = mini.get("gt_cochange_delivered", 0)
            out["gt_verify_delivered"] = mini.get("gt_verify_delivered", 0)
            out["gt_nudge_delivered"] = mini["gt_nudge_delivered"]
            out["gt_understand_calls"] = mini["gt_understand_calls"]
            out["gt_verify_calls"] = mini["gt_verify_calls"]
            out["gt_observation_chars_total"] = mini["gt_observation_chars_total"]
        return out
    out["trajectory_source"] = "output_jsonl"
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
            if _is_mutating_editor_command(str(e.get("action") or ""), a):
                out["edits"] += 1
                if out["first_edit_action"] is None:
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


_EDIT_VERBS = {"create", "str_replace", "insert", "write", "append", "edit", "modify", "overwrite"}
_NON_EDIT_VERBS = {"view", "open", "read", "undo_edit", "undo", "show"}

# A CLOSED <gt-*>...</gt-*> block (same shape as consumption_ledger._BLOCK_RE). Non-greedy
# with a backreferenced close tag: an outer <gt-task-brief>...</gt-task-brief> matches as ONE
# block (inner blocks are inside the match, never double-counted); an UNCLOSED legend tag
# matches nothing.
_GT_CLOSED_BLOCK_RE = re.compile(r"<(gt-[a-z0-9_-]+)\b[^>]*>.*?</\1>", re.S | re.I)


def _is_mutating_editor_command(tool_or_action: str, args_or_text) -> bool:
    """True only for source-changing editor commands, not view/read calls."""
    tool = (tool_or_action or "").lower()
    if isinstance(args_or_text, dict):
        cmd = str(args_or_text.get("command") or args_or_text.get("cmd") or "").strip().lower()
        path = str(args_or_text.get("path") or args_or_text.get("file_path") or "").strip()
        # Decide on the VERB (first token): OH histories carry the target INLINE
        # ("str_replace x y"), which the old exact whole-string match silently missed
        # (pre-existing edits=0 undercount, pinned by the miniswe_source suite).
        verb = cmd.split(None, 1)[0] if cmd else ""
        if verb in _NON_EDIT_VERBS:
            return False
        if verb in _EDIT_VERBS and (path or len(cmd.split()) > 1):
            return True
        if tool in {"edit", "write", "create"} and path:
            return True
        # Not a structured editor verb → it is a raw shell command. Route it through the
        # complete detector so `patch`/`git apply`/heredoc/`sed`/redirect edits count
        # (the old path returned False here — the geopandas first_edit=None symptom).
        if _shell_cmd_is_edit(cmd):
            return True
        return False
    text = str(args_or_text or "").lower()
    if "str_replace_editor" in text or "file_editor" in text:
        m = re.search(r"(?:str_replace_editor|file_editor)\s+([a-z_]+)", text)
        if m:
            return m.group(1) in _EDIT_VERBS
    # Editor-tool JSON (chat tool_calls dumps, plain or json.dumps-escaped): decide by
    # the "command" verb so a "str_replace …" edit counts and a "view …" never does.
    m = re.search(r'\\?"command\\?"\s*:\s*\\?"([a-z_]+)', text)
    if m:
        verb = m.group(1)
        if verb in _NON_EDIT_VERBS:
            return False
        if verb in _EDIT_VERBS:
            return True
    # Bare editor-verb command string ("str_replace src/a.ts old new").
    first = text.split(None, 1)[0] if text.strip() else ""
    if first in _NON_EDIT_VERBS:
        return False
    if first in _EDIT_VERBS and len(text.split()) > 1:
        return True
    # Complete shell edit-truth (superset of the old blind {sed -i, apply_patch, tee, cat >}
    # token set): now also `patch`, `git apply`, heredoc, `sed` variants, and `>`/`>>`.
    return _shell_cmd_is_edit(text)


def _deepseek_price_for(model: str) -> dict:
    m = (model or "").lower()
    # most specific match first (pro before the generic flash/chat fallthrough)
    for key in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"):
        if key in m:
            return DEEPSEEK_PRICING[key]
    return DEEPSEEK_PRICING["deepseek-v4-flash"]  # cheapest default — never over-charge


def _find_miniswe_trajectory(task: str, results_dir: str) -> str | None:
    """Locate this task's pier/mini-swe-agent trajectory. The VM runner lays it out
    under <results_dir>/pier/jobs/**/agent/mini-swe-agent.trajectory.json (results_dir
    is the per-task gt artifact dir); OH/legacy layouts also matched. When the cwd or a
    shared dir is searched, the recursive glob can hit a SIBLING task's trajectory, so
    when a task id is provided we filter candidate hits to those whose path contains the
    id and only fall back to hits[0] if none match (preserves existing callers — the
    filter only NARROWS, never widens)."""
    bases = (
        results_dir,
        os.path.join(results_dir, "pier") if results_dir else "",
        f"/tmp/results_{task}",
        f"/tmp/gt/{task}",
        ".",
    )
    seen: set[str] = set()
    for base in bases:
        if not base or base in seen:
            continue
        seen.add(base)
        # Both the bare name and the pier jobs/**/agent layout the VM runner produces.
        hits: list[str] = []
        for pat in ("mini-swe-agent.trajectory.json",
                    os.path.join("jobs", "**", "agent", "*.trajectory.json"),
                    "*.traj.json"):  # no-pier path (gt_verified_agent): <iid>.traj.json
            hits.extend(glob.glob(os.path.join(base, "**", pat), recursive=True))
        hits.sort()  # DETERMINISM (Fable G16): both scoped[0] and hits[0] below pick from
        #              this list; glob's OS-order otherwise meters different trajectories.
        if not hits:
            continue
        if task:
            # pier TRUNCATES the trial dir to <truncated-task>__<hash>/agent/... so the
            # full task id is often NOT a substring (superjson-error-stack-serialization
            # vs ...serializat__7t9KqdK) — the exact-`in` filter then rejected the CORRECT
            # trajectory → trajectory_source=none → false-zero deep_metrics (bug 9 residual).
            # Match the pier trial stem as a PREFIX of the task id (truncation only shortens).
            scoped = [h for h in hits if task in h]
            if not scoped:
                for h in hits:
                    m = re.search(r"([^/\\]+?)__[^/\\]+[/\\]agent", h)
                    if m and len(m.group(1)) >= 8 and task.startswith(m.group(1)):
                        scoped.append(h)
            if scoped:
                return scoped[0]
            # P1-24: never borrow a sibling task's trajectory when task id is known.
            continue
        return hits[0]
    return None


def _find_model_patch(task: str, results_dir: str) -> str | None:
    """Locate the job's ACTUAL patch artifact — pier writes it to
    ``<results_dir>/jobs/**/<trial>/artifacts/model.patch``.

    MEASUREMENT DEFECT (CLAUDE.md §6, run 29904040782; re-confirmed 2026-07-29 on
    /d/gt_runs/29948431988/wazero): the trajectory's ``info.submission`` is EMPTY on a
    real, submitted run whose model.patch is 1,211 lines. Inferring "no patch" from an
    empty submission string is inferring a fact from an ABSENCE. The patch file is the
    artifact; read it.

    Scoping mirrors ``_find_miniswe_trajectory``: never borrow a sibling task's patch."""
    bases = (
        results_dir,
        os.path.join(results_dir, "pier") if results_dir else "",
        f"/tmp/results_{task}",
        f"/tmp/gt/{task}",
    )
    seen: set[str] = set()
    for base in bases:
        if not base or base in seen:
            continue
        seen.add(base)
        hits: list[str] = []
        for pat in (os.path.join("artifacts", "model.patch"), "model.patch"):
            hits.extend(glob.glob(os.path.join(base, "**", pat), recursive=True))
        # DETERMINISM (Fable G16): glob returns OS-order; sort so retries pick stably.
        hits = sorted(set(hits))
        if not hits:
            continue
        if task:
            scoped = [h for h in hits if task in h]
            if not scoped:
                # pier TRUNCATES the trial dir to <truncated-task>__<hash>/ — match the
                # stem as a PREFIX of the task id (truncation only shortens).
                for h in hits:
                    m = re.search(r"([^/\\]+?)__[^/\\]+[/\\]artifacts", h)
                    if m and len(m.group(1)) >= 8 and task.startswith(m.group(1)):
                        scoped.append(h)
            if scoped:
                return scoped[0]
            # results_dir IS the per-task dir: a single unambiguous hit under it is ours.
            if base in (results_dir, os.path.join(results_dir, "pier")) and len(hits) == 1:
                return hits[0]
            continue
        return hits[0]
    return None


def _model_patch_facts(task: str, results_dir: str) -> dict:
    """Read the job's model.patch. A patch is PRESENT iff that file is non-empty.

    Returns explicit measurements, never an inference:
      model_patch_path / model_patch_bytes / model_patch_lines / model_patch_is_diff.
    ``has_patch`` is None (NOT DETECTED) when no model.patch artifact exists at all —
    absence of the artifact is not evidence the agent produced nothing."""
    out = {
        "model_patch_path": "",
        "model_patch_bytes": None,
        "model_patch_lines": None,
        "model_patch_is_diff": None,
        "has_patch": None,
    }
    p = _find_model_patch(task, results_dir)
    if not p:
        return out
    text = _safe_read_text(p)
    out["model_patch_path"] = p
    out["model_patch_bytes"] = len(text)
    out["model_patch_lines"] = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    out["model_patch_is_diff"] = "diff --git" in text
    out["has_patch"] = bool(text.strip())
    return out


def _from_miniswe_trajectory(task: str, results_dir: str) -> dict:
    """DeepSWE/pier truth: mini-swe-agent.trajectory.json (output.jsonl never written).
    Extracts agent behaviour, DeepSeek token usage (incl cache hit/miss → real cost),
    and the GT content that actually reached the agent's OBSERVATIONS (showcase counts)."""
    out = {
        "found": False, "trajectory_path": "", "model": "", "exit_status": "",
        # first_edit_action None = NOT DETECTED (never 0 → poisons first-edit deltas).
        "action_count": 0, "assistant_steps": 0, "edits": 0, "first_edit_action": None,
        "has_patch": False, "resolved": None,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "cache_hit_tokens": None, "cache_miss_tokens": None, "cost_usd": None,
        # cost the model/litellm ITSELF recorded per call (model-agnostic, summed);
        # preferred over keyed recompute when present (gemini etc. are not DeepSeek-priced).
        "recorded_cost_usd": 0.0, "cost_source": "",
        "gt_brief_delivered": 0, "gt_evidence_delivered": 0, "gt_graph_map_delivered": 0,
        "gt_nudge_delivered": 0, "gt_understand_calls": 0, "gt_verify_calls": 0,
        "gt_scope_delivered": 0, "gt_contract_delivered": 0, "gt_cochange_delivered": 0,
        "gt_verify_delivered": 0,
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
    model_stats = info.get("model_stats", {}) or {}
    out["action_count"] = int(model_stats.get("api_calls", 0) or 0)
    # mini-swe-agent rolls up the litellm-recorded spend here (instance_cost / cost) —
    # this is the model's OWN cost, model-agnostic. Use it as the run-level recorded cost.
    for ck in ("instance_cost", "cost", "total_cost"):
        cv = model_stats.get(ck)
        if isinstance(cv, (int, float)) and cv > 0:
            out["recorded_cost_usd"] = float(cv)
            out["cost_source"] = f"trajectory_model_stats.{ck}"
            break

    try:
        from trajectory_usage import extract_trajectory_usage
    except ImportError:
        from scripts.swebench.trajectory_usage import extract_trajectory_usage
    shared_usage = extract_trajectory_usage(d)
    out["prompt_tokens"] = shared_usage["prompt_tokens"]
    out["completion_tokens"] = shared_usage["completion_tokens"]
    out["total_tokens"] = (
        out["prompt_tokens"] + out["completion_tokens"]
        if shared_usage["prompt_tokens"] is not None
        and shared_usage["completion_tokens"] is not None else None
    )
    out["cache_hit_tokens"] = shared_usage["cache_hit_tokens"]
    out["cache_miss_tokens"] = shared_usage["cache_miss_tokens"]
    out["usage_source"] = shared_usage["source"]

    # SHAPE-AWARE parse — mini-swe-agent emits two message shapes and we must read BOTH
    # correctly or every agent-behaviour + token + cost metric false-zeros:
    #   chat:           role in {assistant, tool, user}; action in tool_calls; usage in
    #                   extra.response.usage (prompt_/completion_ keys).
    #   Responses-API:  role=None; the model turn carries an `output` LIST (one
    #                   function_call w/ arguments.command) + top-level `usage`
    #                   (input_tokens/output_tokens/total_tokens/input_tokens_details.
    #                   cached_tokens); the observation is type=function_call_output with
    #                   text in `output`. deepseek-v4-flash (DeepSWE + Pro) uses THIS shape.
    # DELIVERY counting (gt-math §4): the first user/system message is the brief+LEGEND;
    # its per-turn tag EXAMPLES are documentation, NOT deliveries. So the L1 brief blocks
    # (task-brief, graph-map) are counted from the brief message; per-turn blocks
    # (evidence/scope/contract/cochange/nudge/verify) are counted from OBSERVATIONS ONLY.
    step = 0
    n_assist = 0
    for m in d.get("messages", []) or []:
        role = m.get("role")
        mtype = m.get("type")
        content = m.get("content")
        if isinstance(content, list):
            content = json.dumps(content)
        content = content or ""
        extra = m.get("extra") if isinstance(m.get("extra"), dict) else {}

        is_responses_turn = role is None and mtype is None and isinstance(m.get("output"), list)
        is_chat_turn = role == "assistant"

        # ---------------- MODEL TURN (action + tokens + cost) ----------------
        if is_responses_turn or is_chat_turn:
            n_assist += 1
            step += 1
            usage = m.get("usage")
            if not isinstance(usage, dict):
                usage = (extra.get("response") or {}).get("usage")
            if not out["cost_source"]:
                rc = usage.get("cost") if isinstance(usage, dict) else None
                if not isinstance(rc, (int, float)):
                    hp = (extra.get("response") or {}).get("_hidden_params") or {}
                    rc = hp.get("response_cost") or extra.get("cost") or extra.get("response_cost")
                if isinstance(rc, (int, float)) and rc > 0:
                    out["recorded_cost_usd"] += float(rc)
            # Executed command(s): Responses-API → output[].function_call.arguments.command;
            # chat → tool_calls (the action) + content (the thought).
            cmds: list[str] = []
            if is_responses_turn:
                for it in m.get("output") or []:
                    if isinstance(it, dict) and it.get("type") == "function_call":
                        a = it.get("arguments") or ""
                        try:
                            cmds.append(str((json.loads(a) or {}).get("command", a)))
                        except (ValueError, TypeError):
                            cmds.append(str(a))
            else:
                cmds.append(json.dumps(m.get("tool_calls") or "") + content)
            for cmd in cmds:
                out["gt_understand_calls"] += cmd.count("gt_hook.py understand")
                out["gt_verify_calls"] += cmd.count("gt_hook.py verify")
                if _is_mutating_editor_command("", cmd):
                    out["edits"] += 1
                    if out["first_edit_action"] is None:
                        out["first_edit_action"] = step
            continue

        # ---------------- L1 BRIEF (first user/system instruction message) ----------------
        # Count ONLY the L1 blocks the brief legitimately carries. The per-turn tag
        # examples here are the injected LEGEND (documentation) — never count them as
        # deliveries (that was the over-count: phantom cochange, inflated evidence).
        if role in ("user", "system"):
            out["gt_brief_delivered"] += content.count("<gt-task-brief")
            out["gt_graph_map_delivered"] += content.count("<gt-graph-map")
            # chars-total contract (§7 gt_tokens_injected = chars in <gt-*> blocks): the
            # brief's CLOSED <gt-*> blocks are model-visible GT content and must count
            # (pre-existing undercount: a run whose ONLY GT content is the step-0 brief
            # reported chars_total=0). Closed-block matching keeps the unclosed-tag
            # LEGEND (documentation examples) out — same discipline as the tag counts.
            for _bm in _GT_CLOSED_BLOCK_RE.finditer(content):
                out["gt_observation_chars_total"] += len(_bm.group(0))
            continue

        # ---------------- OBSERVATION (per-turn delivery, rows 11-21) ----------------
        if mtype == "function_call_output" or role in ("tool", "exit"):
            obs = content or m.get("output") or m.get("result") or ""
            if isinstance(obs, list):
                obs = json.dumps(obs)
            obs = obs or ""
            out["gt_evidence_delivered"] += obs.count("<gt-evidence")
            out["gt_scope_delivered"] += obs.count("<gt-scope")
            out["gt_contract_delivered"] += obs.count("<gt-contract")
            out["gt_cochange_delivered"] += obs.count("<gt-cochange")
            out["gt_nudge_delivered"] += obs.count("<gt-nudge")
            out["gt_verify_delivered"] += obs.count("<gt-verify")
            if "<gt-" in obs or obs.lstrip().startswith("GT:"):
                out["gt_observation_chars_total"] += len(obs)
    out["assistant_steps"] = n_assist
    if out["action_count"] == 0:
        out["action_count"] = n_assist

    # resolved: pier verifier reward.json ({"reward": N}); the .txt rarely exists.
    _vdir = os.path.join(os.path.dirname(os.path.dirname(tj)), "verifier")
    _rj = os.path.join(_vdir, "reward.json")
    _rt = os.path.join(_vdir, "reward.txt")
    if os.path.exists(_rj):
        try:
            out["resolved"] = bool(float((_load_json(_rj) or {}).get("reward", 0) or 0) >= 1.0)
        except (ValueError, TypeError):
            pass
    elif os.path.exists(_rt):
        try:
            out["resolved"] = bool(float(open(_rt).read().strip() or "0") >= 1.0)
        except (OSError, ValueError):
            pass

    # Cost: prefer the trajectory's OWN recorded litellm cost (model-agnostic — correct
    # for gemini/vertex and any other provider). Only when the trajectory carries no
    # cost at all do we fall back to the DeepSeek-keyed recompute (which is only valid
    # for deepseek models). recorded_cost_usd>0 => recorded; else keyed.
    if out["recorded_cost_usd"] > 0:
        out["cost_usd"] = d8(out["recorded_cost_usd"])
        if not out["cost_source"]:
            out["cost_source"] = "trajectory_recorded"
        out["cost_estimate"] = False
    elif all(out[key] is not None for key in (
        "cache_hit_tokens", "cache_miss_tokens", "completion_tokens"
    )):
        p = _deepseek_price_for(out["model"] or "deepseek-v4-flash")
        out["cost_usd"] = d8(
            out["cache_hit_tokens"] / 1e6 * p["hit"]
            + out["cache_miss_tokens"] / 1e6 * p["miss"]
            + out["completion_tokens"] / 1e6 * p["out"]
        )
        out["cost_source"] = "deepseek_keyed_recompute"
        out["cost_estimate"] = True
    else:
        out["cost_usd"] = None
        out["cost_source"] = "unavailable"
        out["cost_estimate"] = None
    return out


def _from_cost_log(log_path: str) -> dict:
    """LLM token/cost efficiency from the run log's [GT_COST] lines:
    `[GT_COST] call=N in=X out=Y cached=Z cost=$W total=$T ...`. Summed across calls."""
    import re
    # SAME DEFECT CLASS as the model.patch bug: a MISSING cost log used to emit
    # llm_cost_usd=0.0 / llm_tokens_in=0 — a measured zero indistinguishable from
    # "never measured" (the module's own d8() G14 law forbids exactly this). Start
    # NULL; promote to a number only when a [GT_COST] line is actually observed.
    missing = {"llm_calls": None, "llm_tokens_in": None, "llm_tokens_out": None,
               "llm_tokens_cached": None, "llm_cost_usd": None}
    out = {"llm_calls": 0, "llm_tokens_in": 0, "llm_tokens_out": 0,
           "llm_tokens_cached": 0, "llm_cost_usd": 0.0}
    if not log_path or not os.path.exists(log_path):
        return dict(missing)
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
    # No [GT_COST] line anywhere == the cost was NOT measured on this path (DeepSWE never
    # emits them). Return NULLs so the trajectory fallback / downstream readers see
    # "unknown", not a fabricated $0.00 run.
    if not out["llm_calls"]:
        return dict(missing)
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


def _resolve_embedder_cert_path(task: str, results_dir: str) -> str:
    """Find the emitted embedder certificate for this task, if present."""
    env_cert = os.environ.get("GT_EMBEDDER_CERT")
    if env_cert and os.path.exists(env_cert):
        return env_cert
    bases = [results_dir, f"/tmp/results_{task}", f"/tmp/gt_debug_{task}", f"/tmp/gt/{task}"]
    seen: set[str] = set()
    for base in bases:
        if not base or base in seen:
            continue
        seen.add(base)
        direct = os.path.join(base, "gt_artifacts", "embedder_certificate.json")
        if os.path.exists(direct):
            return direct
        hits = glob.glob(os.path.join(base, "**", "gt_artifacts", "embedder_certificate.json"), recursive=True)
        if hits:
            return hits[0]
        hits = glob.glob(os.path.join(base, "**", "embedder_certificate.json"), recursive=True)
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
        "enriched_bases": {},
        "assertion_count": 0,
        "linked_assertion_count": 0,
        "return_type_signature_count": 0,
        "deterministic_edge_count": 0,
        "lsp_stamped_edge_count": 0,
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
        # data_flow is a PROPERTIES kind (per-param forward slice), NOT a table.
        # Also census every enrichment kind so the record shows the full enriched bases.
        if _table_exists(con, "properties"):
            out["data_flow_row_count"] = _scalar(
                con, "SELECT COUNT(*) FROM properties WHERE kind='data_flow'")
            try:
                out["enriched_bases"] = {
                    str(k): int(v) for k, v in con.execute(
                        "SELECT kind, COUNT(*) FROM properties GROUP BY kind ORDER BY 2 DESC")
                }
            except sqlite3.Error:
                out["enriched_bases"] = {}
        elif _table_exists(con, "data_flow"):
            out["data_flow_row_count"] = _scalar(con, "SELECT COUNT(*) FROM data_flow")
        if _table_exists(con, "assertions"):
            out["assertion_count"] = _scalar(con, "SELECT COUNT(*) FROM assertions")
            out["linked_assertion_count"] = _scalar(
                con,
                "SELECT COUNT(*) FROM assertions WHERE target_node_id IS NOT NULL "
                "AND target_node_id != 0",
            )
        # G3: these are NOT LSP work. return_type is indexer-written; import/same_file
        # edges are tree-sitter DETERMINISTIC resolution, not LSP-stamped. Labelling
        # them lsp_* 113x-inflated the LSP lever (measured 1934 vs 17 real lsp edges).
        # Report them under honest names; the REAL LSP work is lsp_stamped_edge_count.
        out["return_type_signature_count"] = _scalar(
            con,
            "SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL "
            "AND TRIM(return_type) != ''",
        )
        out["deterministic_edge_count"] = _scalar(
            con,
            "SELECT COUNT(*) FROM edges WHERE resolution_method IN "
            "('import','same_file') ",
        )
        # REAL LSP-stamped edges: only edges the LSP precision pass resolved/verified.
        out["lsp_stamped_edge_count"] = _scalar(
            con,
            "SELECT COUNT(*) FROM edges WHERE resolution_method IN "
            "('lsp','lsp_verified')",
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


def _read_lsp_cert(db_path: str) -> dict:
    """The AUTHORITATIVE LSP record is the emitted lsp_certificate.json
    (server_launched / lsp_warm / verified+corrected+deleted / effective_work /
    verdict_hint) — NOT the run log. Look in GT_CERT_DIR, else alongside graph.db.
    Returns {} when absent/unreadable (caller falls back to the log scrape)."""
    cert_dir = os.environ.get("GT_CERT_DIR", "") or (os.path.dirname(db_path) if db_path else "")
    if not cert_dir:
        return {}
    p = os.path.join(cert_dir, "lsp_certificate.json")
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and str(d.get("schema", "")).startswith("gt.lsp_certificate"):
                return d
    except (OSError, ValueError):
        pass
    return {}


def _from_lsp(log_text: str, db_path: str) -> dict:
    """LSP server name + launch status + REAL conversion counts.

    AUTHORITATIVE source is lsp_certificate.json. The prior version SCRAPED the run
    log and reported ``not_observed_in_log`` even when the cert recorded real
    conversions (codespace witness 2026-06-15: cert effective_work=4 (ts)/107 (js)
    while the metric said the LSP was dark — a measurement bug that would call the
    leaderboard's LSP lever dark when it converted edges). The log scrape and the
    graph-language inference are FALLBACKS, used only when no cert is present."""
    out = {
        "lsp_server_name": "unknown", "lsp_launch_status": "unknown",
        "lsp_source": "none", "lsp_warm": False, "lsp_degraded": False,
        "lsp_verified_edges": 0, "lsp_corrected_edges": 0,
        "lsp_deleted_edges": 0, "lsp_effective_work": 0, "lsp_verdict_hint": "",
    }
    cert = _read_lsp_cert(db_path)
    if cert:
        verdict = str(cert.get("verdict_hint") or "").strip()
        launched = bool(cert.get("server_launched"))
        warm = bool(cert.get("lsp_warm"))
        degraded = bool(cert.get("degraded"))
        eff = int(cert.get("effective_work") or 0)
        if verdict:
            status = verdict.lower().replace("lsp_", "")
        elif not launched:
            status = "not_launched"
        elif degraded:
            status = "degraded"
        elif warm and eff > 0:
            status = "active"
        elif warm:
            status = "warm_no_conversion"
        else:
            status = "launched_not_warm"
        out.update({
            "lsp_server_name": str(cert.get("server_command") or "").strip() or "unknown",
            "lsp_launch_status": status,
            "lsp_source": "certificate",
            "lsp_warm": warm,
            "lsp_degraded": degraded,
            "lsp_verified_edges": int(cert.get("verified_edges") or 0),
            "lsp_corrected_edges": int(cert.get("corrected_edges") or 0),
            "lsp_deleted_edges": int(cert.get("deleted_edges") or 0),
            "lsp_effective_work": eff,
            "lsp_verdict_hint": verdict,
        })
        return out
    # FALLBACK (no cert): infer from log markers, else the graph's dominant language.
    servers = ("pyright", "gopls", "rust-analyzer", "typescript-language-server", "jdtls")
    found = ""
    for s in servers:
        if log_text and s in log_text:
            found = s
            break
    if found:
        out["lsp_server_name"] = found
        out["lsp_source"] = "log_scrape"
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
                out["lsp_source"] = "graph_inference"
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


def _from_embedder(cert_path: str = "") -> dict:
    """Embedder status for metrics. Prefer the emitted proof certificate.

    A local probe is only a fallback. In DeepSWE validation, probing from the
    metrics process can fail because that process lacks runtime deps even when
    the proof substrate emitted a valid certificate.
    """
    out = {
        "embedder_path": "",
        "embedder_vector_dim": 0,
        "embedder_nonzero": False,
        "semantic_enabled": False,
        "embedder_status_source": "local_probe",
        "embedder_product_verdict": False,
        "embedder_diagnostic_only": True,
    }
    cert = _load_json(cert_path) if cert_path else None
    if isinstance(cert, dict):
        cls = str(cert.get("embedder_class", "") or "")
        dim = d8(cert.get("embedder_dim", 0) or 0)
        semantic_candidates = int(cert.get("semantic_candidate_count", 0) or 0)
        rendered_nonzero = int(cert.get("rendered_semantic_nonzero_count", 0) or 0)
        upstream_nonzero = int(cert.get("upstream_semantic_nonzero_count", 0) or 0)
        effective_w_sem = float(cert.get("effective_w_sem", 0.0) or 0.0)
        zero_or_error = (
            "Zero" in cls or "_ZeroEmbeddingModel" in cls or "load_error" in cls
        )
        nonzero = bool(
            not zero_or_error
            and dim > 0
            and (rendered_nonzero > 0 or upstream_nonzero > 0 or effective_w_sem > 0)
        )
        out.update({
            "embedder_path": str(cert.get("model_path") or cert.get("GT_MODELS_ROOT") or ""),
            "embedder_vector_dim": dim,
            "embedder_nonzero": nonzero,
            "semantic_enabled": bool(nonzero and semantic_candidates > 0),
            "embedder_status_source": "embedder_certificate",
            "embedder_certificate_path": cert_path,
            "embedder_class": cls,
            "semantic_candidate_count": d8(semantic_candidates),
            "rendered_semantic_nonzero_count": d8(rendered_nonzero),
            "upstream_semantic_nonzero_count": d8(upstream_nonzero),
            "effective_w_sem": d8(effective_w_sem),
            "embedder_product_verdict": True,
            "embedder_diagnostic_only": False,
        })
        return out

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
    out["embedder_product_verdict"] = False
    out["embedder_diagnostic_only"] = True
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

    # C15 (2026-07-27): prefer the ``acquired_*`` family. These four report whether a
    # localization LEG produced signal, which is an ACQUISITION question; the bare names
    # hold a DELIVERY count over the rendered candidate set, which the GT_BRIEF_MINIMAL +
    # GT_LOC_RESLOT reduction empties by construction. Reading the bare name under the
    # re-slot reports "no leg found anything" when the truth is "the step-0 brief rendered
    # nothing" — the exact misreading that produced the retracted "acquisition is dark"
    # finding on run 30297116212. Legacy keys stay in the chain so pre-split artifacts read
    # exactly as before.
    edge = _num("acquired_graph_edge_count",
                "graph_edge_count", "l1_candidates_with_graph_edge_count",
                "l1_candidates_with_call_edge_count")
    sem = _num("acquired_semantic_signal_count",
               "semantic_signal_count", "l1_candidates_with_semantic_count",
               "l1_semantic_signal_count")
    struct = _num("acquired_structural_signal_count",
                  "structural_signal_count", "l1_candidates_with_signature_count",
                  "l1_structural_signal_count")
    lex = _num("acquired_fts5_signal_count",
               "fts5_signal_count", "lexical_signal_count",
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


def _trajectory_layer_fallback(traj: dict) -> tuple[dict, list[str], float]:
    """Best-effort layer/token accounting from agent-observed GT text.

    The run summary is the precise source when present. DeepSWE runs can lose
    that file while still preserving the agent trajectory, so reporting zero
    injected tokens would be false. This fallback is deliberately marked as a
    proxy by the caller.
    """
    chars = d8(traj.get("gt_observation_chars_total", 0) or 0)
    if chars <= 0:
        return {}, [], 0.0

    layer_counts = {
        "L1": d8((traj.get("gt_brief_delivered", 0) or 0)
                 + (traj.get("gt_graph_map_delivered", 0) or 0)),
        "L3": d8(traj.get("gt_evidence_delivered", 0) or 0),
        "L4": d8((traj.get("gt_understand_calls", 0) or 0)
                 + (traj.get("gt_verify_calls", 0) or 0)),
        "L5": d8(traj.get("gt_nudge_delivered", 0) or 0),
    }
    active = [layer for layer, count in layer_counts.items() if count > 0]
    if not active:
        active = ["trajectory_gt_observation"]
        layer_counts = {"trajectory_gt_observation": 1.0}

    total_weight = sum(layer_counts[layer] for layer in active) or 1.0
    total_tokens = d8(chars / 4.0)
    per_layer = {}
    remaining = total_tokens
    for i, layer in enumerate(active):
        if i == len(active) - 1:
            tokens = d8(max(0.0, remaining))
        else:
            tokens = d8(total_tokens * (layer_counts[layer] / total_weight))
            remaining = d8(remaining - tokens)
        per_layer[layer] = {
            "eligible": layer_counts[layer],
            "emitted": layer_counts[layer],
            "suppressed": 0.0,
            "rendered_tokens_total": tokens,
            "utilization_score": 0.0,
            "next_action_count": 0.0,
            "emit_rate": 1.0,
            "source": "trajectory_proxy",
        }
    return per_layer, active, total_tokens


def _from_graph_cert(cert_dir: str) -> dict:
    """Substrate fallback when graph.db is NOT present locally (the run uploads the
    6 KB certs, not the multi-MB graph.db). graph_certificate.json carries the same
    census the DB query would yield — so the §4.1 substrate fields populate from the
    uploaded cert instead of being silently 0 (the deep-metrics 'blindness')."""
    if not cert_dir:
        return {}
    for cand in (os.path.join(cert_dir, "graph_certificate.json"),
                 os.path.join(cert_dir, "gt", "graph_certificate.json")):
        if not os.path.exists(cand):
            continue
        c = _load_json(cand) or {}
        edges = int(c.get("edges_count", 0) or 0)
        det = int(c.get("deterministic_edge_count", 0) or 0)
        rmd = c.get("resolution_method_distribution", {}) or {}
        return {
            "graph_nodes": int(c.get("nodes_count", 0) or 0),
            "graph_edges": edges,
            "verified_edge_count": det,
            "verified_edge_ratio": d8(det / edges) if edges else 0.0,
            "fts5_row_count": int(c.get("fts5_row_count", 0) or 0),
            "fts5_real_query_result_count": (
                int(c.get("fts5_row_count", 0) or 0) if c.get("fts5_match_probe_ok") else 0),
            "assertion_count": int(c.get("assertions_count", 0) or 0),
            "data_flow_row_count": int(c.get("data_flow_count", 0) or 0),
            # G3: rmd['lsp'] IS the real LSP-stamped count (correctly labelled here);
            # det is the deterministic (import/same_file) count. Keep them distinct.
            "deterministic_edge_count": det,
            "lsp_stamped_edge_count": int(rmd.get("lsp", 0) or 0),
            "graph_substrate_source": "graph_certificate.json",
        }
    return {}


# ---------------------------------------------------------------------------
# Delivery metrics from the RUNTIME LEDGER (K15). In the tagless native era the
# agent-visible deliveries carry NO <gt-*> tags, so the trajectory tag-scan
# (gt_delivery.*_delivered) reports all-zeros while the seam's runtime ledger
# holds the real sealed deliveries (outcome="delivered" + content_sha256_16 +
# chars_delivered). The authoritative delivery signal is those ledger rows —
# NEVER a <gt-*> tag scan. An optional byte-join verifies each sealed hash
# actually appears in the model-visible trajectory (sliding-window sha256 over
# tool-message content at length chars_delivered), capped per task.
# ---------------------------------------------------------------------------
def _resolve_runtime_ledger(task: str, results_dir: str) -> str:
    """Find this task's gt_runtime_ledger_*.jsonl. Task-scoped bases only (a bare
    glob could attach a sibling task's ledger). Prefers the trajectory's own dir."""
    env = os.environ.get("GT_RUNTIME_LEDGER")
    if env and os.path.exists(env):
        return env
    mini = _find_miniswe_trajectory(task, results_dir)
    bases = []
    if mini:
        bases.append(os.path.dirname(mini))
    bases += [results_dir, f"/tmp/results_{task}", f"/tmp/gt/{task}", f"/tmp/gt_out"]
    seen: set[str] = set()
    for base in bases:
        if not base or base in seen:
            continue
        seen.add(base)
        for pat in (f"gt_runtime_ledger_{task}.jsonl", "gt_runtime_ledger*.jsonl"):
            direct = os.path.join(base, pat)
            if "*" not in pat and os.path.exists(direct):
                return direct
            hits = sorted(glob.glob(os.path.join(base, "**", pat), recursive=True))
            # scope to this task when the id is embedded, else accept a lone sibling-free hit
            scoped = [h for h in hits if task in os.path.basename(h)] or (
                hits if len(hits) == 1 else [])
            if scoped:
                return scoped[0]
    return ""


def _tool_message_texts(mini_traj_path: str, max_msgs: int = 4000) -> list[str]:
    """Model-visible observation/tool texts from a mini-swe trajectory, for the byte
    join. Reads the SAME channels the agent saw (tool / function_call_output / user
    brief). Best-effort; empty list on any read error."""
    d = _load_json(mini_traj_path) if mini_traj_path else None
    if not isinstance(d, dict):
        return []
    out: list[str] = []
    for m in (d.get("messages") or [])[:max_msgs]:
        if not isinstance(m, dict):
            continue
        role, mtype = m.get("role"), m.get("type")
        if not (mtype == "function_call_output" or role in ("tool", "exit", "user", "system")):
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = json.dumps(c)
        if isinstance(c, str) and c:
            out.append(c)
        o = m.get("output")
        if isinstance(o, str) and o:
            out.append(o)
    return out


def _byte_join_verified(rows: list[dict], texts: list[str], *, op_budget: int = 400_000) -> int:
    """Count delivered ledger rows whose sealed content_sha256_16 is found VERBATIM in
    the model-visible trajectory: slide a window of length chars_delivered over each
    tool-message text and compare sha256(window)[:16]. Capped by a global op budget so a
    huge trajectory can never blow up the metric step; rows unproven within budget are
    simply not counted (verification is a lower bound, never an over-count)."""
    verified = 0
    ops = 0
    for row in rows:
        seal = row.get("content_sha256_16")
        length = int(row.get("chars_delivered") or 0)
        if not seal or length <= 0:
            continue
        hit = False
        for text in texts:
            if len(text) < length:
                continue
            # only worth scanning where a window could match; step 1 (exact byte offset)
            for start in range(0, len(text) - length + 1):
                ops += 1
                if ops > op_budget:
                    return verified  # budget exhausted — return the proven lower bound
                if hashlib.sha256(
                    text[start:start + length].encode("utf-8", errors="replace")
                ).hexdigest()[:16] == seal:
                    hit = True
                    break
            if hit:
                break
        if hit:
            verified += 1
    return verified


def _delivery_from_runtime_ledger(
    ledger_path: str, *, trajectory_texts: list[str] | None = None
) -> dict:
    """Delivered counts/chars from the sealed runtime-ledger rows (K15). A delivery is a
    row with outcome=="delivered" AND a content_sha256_16 seal. Returns per-layer/
    per-event breakdowns + an optional byte-join verification count. NEVER counts from
    <gt-*> tag scans."""
    out = {
        "present": False,
        "runtime_ledger_path": ledger_path or None,
        "delivered_count": 0,
        "delivered_chars": 0,
        "sealed_count": 0,
        "byte_join_verified_count": None,
        "per_layer": {},
        "per_event_type": {},
    }
    if not ledger_path or not os.path.exists(ledger_path):
        return out
    rows: list[dict] = []
    try:
        with open(ledger_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        return out
    delivered = [
        r for r in rows
        if r.get("outcome") == "delivered" and r.get("content_sha256_16")
    ]
    out["present"] = True
    out["delivered_count"] = len(delivered)
    out["sealed_count"] = sum(1 for r in rows if r.get("content_sha256_16"))
    out["delivered_chars"] = sum(int(r.get("chars_delivered") or 0) for r in delivered)
    per_layer: dict[str, int] = {}
    per_event: dict[str, int] = {}
    for r in delivered:
        lay = str(r.get("layer") or "unknown")
        ev = str(r.get("event_type") or "")
        per_layer[lay] = per_layer.get(lay, 0) + 1
        if ev:
            per_event[ev] = per_event.get(ev, 0) + 1
    out["per_layer"] = per_layer
    out["per_event_type"] = per_event
    if trajectory_texts is not None:
        out["byte_join_verified_count"] = _byte_join_verified(delivered, trajectory_texts)
    return out


def _verifier_interface_metrics(truth_data: object) -> dict[str, int | float | None]:
    """Compatibility wrapper around the shared PERF projection."""
    from gt_performance_metrics import verifier_interface_metrics

    return verifier_interface_metrics(truth_data)


def _task_truth_identity_ok(truth_data: object, task: str) -> bool:
    """True only when a task_truth doc is current-schema AND identity-matched to
    this task.

    A stale or foreign truth — null or mismatched ``instance_id``, e.g. the frozen
    baseline copies whose ``instance_id`` came through null and whose failure_class
    is INFRA — must never override the run's own reward/trajectory outcome. Honoring
    such a doc silently forced every frozen resolve to unresolved (the 83->0 defect).
    Generic identity check only: no task/repo-specific keys.
    """
    if not isinstance(truth_data, dict):
        return False
    if truth_data.get("schema") != "gt.task_truth.v1":
        return False
    iid = truth_data.get("instance_id")
    if not isinstance(iid, str) or not iid:
        return False
    return iid.strip().removeprefix("ll-full-") == str(task).strip().removeprefix("ll-full-")


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
    embedder_cert_path = _resolve_embedder_cert_path(task, results_dir)
    log_text = _safe_read_text(log_path) if log_path else ""

    # task_truth.json is authoritative for resolved/outcome when present (P0-06).
    truth_path = os.path.join(results_dir, "task_truth.json")
    if not os.path.isfile(truth_path):
        for hit in glob.glob(os.path.join(results_dir, "**", "task_truth.json"), recursive=True):
            truth_path = hit
            break
    truth_data = _load_json(truth_path) if truth_path and os.path.isfile(truth_path) else None
    # Identity guard (Codex root-cause #1): honor task_truth ONLY when it is
    # current-schema AND matched to THIS task. A stale/foreign truth (null or
    # mismatched instance_id — the frozen-baseline copies) must never override the
    # run's own outcome, or every frozen resolve collapses to unresolved.
    task_truth_rejected = None
    if truth_data is not None and not _task_truth_identity_ok(truth_data, task):
        task_truth_rejected = {
            "path": truth_path,
            "schema": truth_data.get("schema") if isinstance(truth_data, dict) else None,
            "instance_id": truth_data.get("instance_id") if isinstance(truth_data, dict) else None,
            "reason": "schema_or_instance_id_not_matched_to_task",
        }
        truth_data = None

    traj = _from_trajectory(task, results_dir)
    # DeepSWE/pier writes mini-swe-agent.trajectory.json, NOT output.jsonl — read it as
    # the agent-side truth (steps, edits, patch, tokens, GT delivery) when OH is absent.
    mini = _from_miniswe_trajectory(task, results_dir)
    if (not traj.get("action_count")) and mini.get("found"):
        traj["action_count"] = mini["action_count"]
        traj["assistant_steps"] = mini.get("assistant_steps", mini["action_count"])
        traj["edits"] = mini["edits"]
        traj["first_edit_action"] = mini["first_edit_action"]
        traj["has_patch"] = traj.get("has_patch") or mini["has_patch"]
        if traj.get("resolved") is None:
            traj["resolved"] = mini["resolved"]
        # GT delivery to the agent's observations (fired AND delivered) → showcase block
        traj["gt_brief_delivered"] = mini["gt_brief_delivered"]
        traj["gt_evidence_delivered"] = mini["gt_evidence_delivered"]
        traj["gt_graph_map_delivered"] = mini["gt_graph_map_delivered"]
        traj["gt_scope_delivered"] = mini.get("gt_scope_delivered", 0)
        traj["gt_contract_delivered"] = mini.get("gt_contract_delivered", 0)
        traj["gt_cochange_delivered"] = mini.get("gt_cochange_delivered", 0)
        traj["gt_verify_delivered"] = mini.get("gt_verify_delivered", 0)
        traj["gt_nudge_delivered"] = mini["gt_nudge_delivered"]
        traj["gt_understand_calls"] = mini["gt_understand_calls"]
        traj["gt_verify_calls"] = mini["gt_verify_calls"]
        traj["gt_observation_chars_total"] = mini["gt_observation_chars_total"]
    # If output.jsonl was absent (e.g. DeepSWE), recover patch/action signal from
    # the log so classification still works.
    if (not traj.get("action_count")) and log_text:
        traj["has_patch"] = traj.get("has_patch") or _log_has_patch(log_text)

    # The job's model.patch is the ARTIFACT truth and outranks every inference above
    # (info.submission is empty on real submitted runs). Fold it into traj so the
    # downstream first_edit_detection marker stops calling a 1,211-line patch "no patch".
    _patch_facts = _model_patch_facts(task, results_dir)
    if _patch_facts.get("has_patch") is not None:
        traj["has_patch"] = bool(_patch_facts["has_patch"])

    # Loud edit-truth marker on the efficacy axis. first_edit_action stays honest-None
    # when no edit command is locatable (never a fabricated step 0); but if the run
    # nonetheless produced a patch, say so EXPLICITLY instead of a silent None that reads
    # as "never edited". The common patch/git-apply/redirect edit is now timed correctly
    # by the shared detector, so this marker fires only for genuinely in-body edits
    # (diff/heredoc applied via a file the command line never names).
    if traj.get("first_edit_action") is not None:
        traj["first_edit_detection"] = "edit_command"
    elif traj.get("has_patch"):
        traj["first_edit_detection"] = "patch_present_but_no_edit_command"
    else:
        traj["first_edit_detection"] = "no_edit_no_patch"

    # Token accounting is separate from behavioral attribution. Action-type
    # transitions are computed below only after the exact receipt ledger exists.
    token_accounting_source = "gt_run_summary" if per_layer_raw else "none"
    fallback_per_layer, fallback_layers, fallback_tokens = _trajectory_layer_fallback(traj)
    if not per_layer_raw and fallback_tokens > 0:
        per_layer = fallback_per_layer
        inj_tokens_total = fallback_tokens
        token_accounting_source = "trajectory_proxy"

    cost = _from_cost_log(log_path)
    # DeepSWE: no [GT_COST] log lines (litellm unmapped) — derive tokens + DeepSeek-priced
    # cost from the pier trajectory's per-call usage (incl cache hit/miss) instead.
    if (not cost["llm_calls"]) and mini.get("found"):
        cost = {
            "llm_calls": mini["action_count"],
            "llm_tokens_in": mini["prompt_tokens"],
            "llm_tokens_out": mini["completion_tokens"],
            "llm_tokens_cached": mini["cache_hit_tokens"],
            "llm_cost_usd": mini["cost_usd"],
        }
    actions = traj.get("action_count", 0) or 0
    llm_total = (
        cost["llm_tokens_in"] + cost["llm_tokens_out"]
        if cost["llm_tokens_in"] is not None and cost["llm_tokens_out"] is not None
        else None
    )
    # token/cost EFFICIENCY (the constitution's honest token story: GT injection vs LLM usage)
    efficiency = {
        "model": mini.get("model") or "",
        "cost_source": (mini.get("cost_source") or "deepseek_priced_trajectory")
        if mini.get("found") else "gt_cost_log",
        "cost_estimate": bool(
            (mini.get("cost_source") or "").endswith("recompute")
            or (mini.get("cost_source") or "") == "deepseek_priced_trajectory"
        ),
        "llm_calls": d8(cost["llm_calls"]),
        "llm_tokens_in": d8(cost["llm_tokens_in"]),
        "llm_tokens_out": d8(cost["llm_tokens_out"]),
        "llm_tokens_cached": d8(cost["llm_tokens_cached"]),
        "llm_cache_hit_tokens": d8(mini.get("cache_hit_tokens")),
        "llm_cache_miss_tokens": d8(mini.get("cache_miss_tokens")),
        "llm_tokens_total": d8(llm_total),
        "llm_cost_usd": d8(cost["llm_cost_usd"]),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "gt_injected_tokens_source": token_accounting_source,
        "tokens_per_action": d8(llm_total / actions) if actions and llm_total is not None else None,
        "cost_per_action_usd": d8(cost["llm_cost_usd"] / actions) if actions and cost["llm_cost_usd"] is not None else None,
        # GT's added context as a fraction of total LLM input — the honest overhead figure
        "gt_injection_overhead_pct": d8(100.0 * inj_tokens_total / cost["llm_tokens_in"]) if cost["llm_tokens_in"] else None,
    }
    if truth_data:
        outcome_block = truth_data.get("outcome") or {}
        if outcome_block.get("resolved") is not None:
            traj["resolved"] = bool(outcome_block["resolved"])
        elif outcome_block.get("failure_class") == "RESOLVED":
            traj["resolved"] = True
        elif outcome_block.get("failure_class"):
            traj["resolved"] = False

    # --- TASK 1: outcome classification (start-blocking failures win) --------
    verdict = classify_outcome(task, log_path, traj, summ, pipeline, results_dir)
    if truth_data and (truth_data.get("outcome") or {}).get("failure_class"):
        verdict["failure_class"] = truth_data["outcome"]["failure_class"]
        verdict["outcome_authority"] = "task_truth.json"

    # --- TASK 2: required spec-F field set ----------------------------------
    graph = _from_graph_db(db_resolved)
    if not graph.get("graph_nodes"):
        # graph.db absent locally — read the uploaded graph_certificate.json instead.
        _cert_fb = _from_graph_cert(os.environ.get("GT_CERT_DIR", "") or results_dir)
        if _cert_fb.get("graph_nodes"):
            graph.update(_cert_fb)
    lsp = _from_lsp(log_text, db_resolved)
    embedder = _from_embedder(embedder_cert_path)
    env_snap = _env_snapshot()
    l1f = _from_l1_block(summ)
    l3f = _from_l3_block(summ)
    l3bf = _from_l3b_block(summ)

    summ_present = bool(summ)

    # CP007 — consumption ledger from mini trajectory when present.
    consumption = {
        "gt_blocks_delivered": 0,
        "gt_blocks_consumed": 0,
        "gt_blocks_verification_followup": 0,
        "gt_blocks_hard_enforced": 0,
        "gt_blocks_enforced": 0,
        "enforcement_semantics": "hard_block_only",
    }
    ledger_path = ""
    consumption_error = None
    mini_traj = _find_miniswe_trajectory(task, results_dir)
    if mini_traj:
        try:
            import importlib.util

            cl_path = os.path.join(
                os.path.dirname(__file__), "consumption_ledger.py"
            )
            spec = importlib.util.spec_from_file_location("consumption_ledger_gdm", cl_path)
            cl_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cl_mod)
            # v2 join: pair delivered runtime-ledger rows to the model-visible
            # trajectory blocks. Glob a sibling gt_runtime_ledger*.jsonl; the
            # reader also auto-globs when this is None (kept explicit here).
            import glob as _glob

            _rl = sorted(
                _glob.glob(
                    os.path.join(os.path.dirname(mini_traj), "gt_runtime_ledger*.jsonl")
                )
            )
            consumption = cl_mod.ledger_from_trajectory_path(
                mini_traj, runtime_ledger_path=(_rl[0] if _rl else None)
            )
            ledger_path = os.path.join(
                os.path.dirname(mini_traj), "..", "gt_consumption_ledger.json"
            )
            ledger_path = os.path.normpath(ledger_path)
            try:
                with open(ledger_path, "w", encoding="utf-8") as lf:
                    json.dump(consumption, lf, indent=2)
            except OSError:
                ledger_path = ""
        except Exception as exc:
            consumption_error = type(exc).__name__

    # Action-type transitions are diagnostic only. Reuse the exact sealed-byte
    # receipt ledger built above so tagless Profile-2 deliveries are counted once
    # and ledger-only (DARK) rows are excluded. Causality remains UNMEASURED here;
    # only a matched/shadow design can establish it.
    behavioral_impact = {
        "total_deliveries": 0,
        "total_pivots": 0,
        "impact_rate": None,
        "impact_rate_semantics": "diagnostic_action_type_transition",
        "causal_status": "UNMEASURED",
        "gt_tokens_injected": None,
        "gt_tokens_injected_units": None,
        "gt_tokens_per_pivot": None,
        "nudge_compliance_rate": None,
        "per_tag": {},
        "per_tag_impact": {},
    }
    if mini_traj and consumption_error is None:
        try:
            from gt_behavioral_impact import analyze_trajectory as _analyze_bi

            tj_data = _load_json(mini_traj)
            if isinstance(tj_data, dict):
                bi = _analyze_bi(tj_data, consumption_ledger=consumption)
                behavioral_impact = bi.get("summary", behavioral_impact)
            else:
                behavioral_impact["collection_error"] = "invalid_trajectory"
        except Exception as exc:
            behavioral_impact["collection_error"] = type(exc).__name__
    elif consumption_error is not None:
        behavioral_impact["collection_error"] = (
            "consumption_ledger_error:" + consumption_error
        )

    # Defect-3: on the mini shape the step-0 brief is a bundle of <gt-*> blocks inside the
    # m1 USER message (NOT a <gt-task-brief> tag), so the legacy tag counter reported
    # brief_delivered=0 on a DELIVERED brief. Source the count from W1's v2 consumption
    # ledger brief channel (distinct brief-channel messages) instead of re-deriving. On the
    # old shape (v1 ledger / <gt-task-brief>) this is 0 and we fall back to the legacy count,
    # so no old-shape run regresses.
    brief_delivered_v2 = len({
        e.get("msg_index")
        for e in (consumption.get("entries") or [])
        if e.get("source") == "trajectory"
        and e.get("delivery_channel") == "brief"
        and e.get("msg_index") is not None
    })

    # K15 — delivery from the sealed RUNTIME LEDGER (tagless native era). The tag-scan
    # gt_delivery.*_delivered fields are all-zero when the deliveries carry no <gt-*>
    # tags; the ledger's outcome="delivered" + content_sha256_16 rows are the real
    # signal. Byte-join each seal against the model-visible trajectory (capped).
    rl_delivery_path = _resolve_runtime_ledger(task, results_dir)
    _rl_texts = _tool_message_texts(mini_traj) if (mini_traj and rl_delivery_path) else None
    rl_delivery = _delivery_from_runtime_ledger(
        rl_delivery_path, trajectory_texts=_rl_texts
    )
    # When the trajectory tag-scan saw NO GT bytes (tagless run) but the ledger sealed
    # real deliveries, the ledger chars ARE the model-visible GT injection — fill the
    # otherwise-false-zero token/char totals (never overwrite a non-zero tag-scan count,
    # so old-shape/OH runs are byte-identical).
    if rl_delivery["present"] and rl_delivery["delivered_chars"] > 0:
        if not traj.get("gt_observation_chars_total"):
            traj["gt_observation_chars_total"] = rl_delivery["delivered_chars"]
        if not inj_tokens_total:
            inj_tokens_total = float(rl_delivery["delivered_chars"])
            token_accounting_source = "runtime_ledger_sealed_chars"
            efficiency["gt_injected_tokens_total"] = d8(inj_tokens_total)
            efficiency["gt_injected_tokens_source"] = token_accounting_source
            if cost["llm_tokens_in"]:
                efficiency["gt_injection_overhead_pct"] = d8(
                    100.0 * inj_tokens_total / cost["llm_tokens_in"])

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
        # Patch PROVENANCE — has_patch is only citable when patch_source ==
        # "model_patch_artifact" (read off disk). Anything else is an inference.
        "patch_source": verdict.get("patch_source", ""),
        "model_patch_path": verdict.get("model_patch_path"),
        "model_patch_bytes": d8(verdict.get("model_patch_bytes")),
        "model_patch_lines": d8(verdict.get("model_patch_lines")),
        "model_patch_is_diff": verdict.get("model_patch_is_diff"),
        "failure_class": verdict.get("failure_class", ""),
        "outcome_authority": verdict.get("outcome_authority", ""),
        "task_truth_path": truth_path if truth_data else None,
        "task_truth_rejected": task_truth_rejected,
        "runtime_control": (truth_data.get("runtime_control") if truth_data else None),
        # --- graph-derived (TASK 2) ---
        "graph_db_path": graph["graph_db_path"],
        "graph_nodes": d8(graph["graph_nodes"]),
        "graph_edges": d8(graph["graph_edges"]),
        "verified_edge_count": d8(graph["verified_edge_count"]),
        "verified_edge_ratio": d8(graph["verified_edge_ratio"]),
        "fts5_row_count": d8(graph["fts5_row_count"]),
        "fts5_real_query_result_count": d8(graph["fts5_real_query_result_count"]),
        "data_flow_row_count": d8(graph["data_flow_row_count"]),
        "enriched_bases": graph.get("enriched_bases", {}),
        "assertion_count": d8(graph["assertion_count"]),
        "linked_assertion_count": d8(graph["linked_assertion_count"]),
        # --- LSP (TASK 2) — now cert-authoritative, not log-scraped ---
        "lsp_server_name": lsp["lsp_server_name"],
        "lsp_launch_status": lsp["lsp_launch_status"],
        "lsp_status_source": lsp.get("lsp_source", "none"),
        "lsp_warm": lsp.get("lsp_warm", False),
        "lsp_degraded": lsp.get("lsp_degraded", False),
        "lsp_verdict_hint": lsp.get("lsp_verdict_hint", ""),
        "lsp_verified_edges": d8(lsp.get("lsp_verified_edges", 0)),
        "lsp_corrected_edges": d8(lsp.get("lsp_corrected_edges", 0)),
        "lsp_deleted_edges": d8(lsp.get("lsp_deleted_edges", 0)),
        "lsp_effective_work": d8(lsp.get("lsp_effective_work", 0)),
        # G3: honest names. deterministic_edge_count = import/same_file (tree-sitter),
        # return_type_signature_count = indexer-written contracts, lsp_stamped_edge_count
        # = the REAL LSP precision-pass work (NOT the 113x-inflated old lsp_enriched count).
        "deterministic_edge_count": d8(graph.get("deterministic_edge_count", 0)),
        "return_type_signature_count": d8(graph.get("return_type_signature_count", 0)),
        "lsp_stamped_edge_count": d8(graph.get("lsp_stamped_edge_count", 0)),
        # --- embedder / semantic (TASK 2) ---
        "embedder_path": embedder["embedder_path"],
        "embedder_vector_dim": d8(embedder["embedder_vector_dim"]),
        "embedder_nonzero": embedder["embedder_nonzero"],
        "semantic_enabled": embedder["semantic_enabled"],
        "embedder_status_source": embedder.get("embedder_status_source", ""),
        "embedder_certificate_path": embedder.get("embedder_certificate_path", ""),
        "embedder_class": embedder.get("embedder_class", ""),
        "semantic_candidate_count": d8(embedder.get("semantic_candidate_count", 0)),
        "rendered_semantic_nonzero_count": d8(embedder.get("rendered_semantic_nonzero_count", 0)),
        "upstream_semantic_nonzero_count": d8(embedder.get("upstream_semantic_nonzero_count", 0)),
        "effective_w_sem": d8(embedder.get("effective_w_sem", 0)),
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
        "layers_active": summ.get("layers_active", []) or fallback_layers,
        "total_layer_events": d8(summ.get("total_layer_events", 0)),
        "total_agent_events": d8(summ.get("total_agent_events", 0)),
        "gt_injected_tokens_total": d8(inj_tokens_total),
        "gt_injected_tokens_source": token_accounting_source,
        "efficiency": efficiency,
        # --- GT-reached-agent SHOWCASE (fired AND delivered, from agent observation) ---
        "gt_delivery": {
            "brief_delivered": d8(brief_delivered_v2 or traj.get("gt_brief_delivered", 0)),
            "evidence_delivered": d8(traj.get("gt_evidence_delivered", 0)),
            "graph_map_delivered": d8(traj.get("gt_graph_map_delivered", 0)),
            "scope_delivered": d8(traj.get("gt_scope_delivered", 0)),
            "contract_delivered": d8(traj.get("gt_contract_delivered", 0)),
            "cochange_delivered": d8(traj.get("gt_cochange_delivered", 0)),
            "verify_delivered": d8(traj.get("gt_verify_delivered", 0)),
            "nudge_delivered": d8(traj.get("gt_nudge_delivered", 0)),
            "understand_calls": d8(traj.get("gt_understand_calls", 0)),
            "verify_calls": d8(traj.get("gt_verify_calls", 0)),
            "gt_observation_chars_total": d8(traj.get("gt_observation_chars_total", 0)),
            # K15 — authoritative delivery from the sealed runtime ledger (NOT tag scans).
            "runtime_ledger": {
                "present": rl_delivery["present"],
                "runtime_ledger_path": rl_delivery["runtime_ledger_path"],
                "delivered_count": d8(rl_delivery["delivered_count"]),
                "delivered_chars": d8(rl_delivery["delivered_chars"]),
                "sealed_count": d8(rl_delivery["sealed_count"]),
                "byte_join_verified_count": (
                    d8(rl_delivery["byte_join_verified_count"])
                    if rl_delivery["byte_join_verified_count"] is not None else None
                ),
                "per_layer": rl_delivery["per_layer"],
                "per_event_type": rl_delivery["per_event_type"],
            },
            # D-V (run6 telegram: evidence_delivered=0 vs ledger delivered_count=11):
            # native delivery is TAGLESS (native_render rides the model's own channel
            # — fact_registry.py:130), so the <gt-*> tag-scan fields above structurally
            # read 0 for every native form. The sealed runtime ledger
            # (content_sha256_16 byte-join) is the ONLY reliable native-delivery signal
            # (adding text heuristics would false-positive on ordinary tool output).
            # Surface the ledger truth as the headline so a delivered run is NEVER
            # reported as 0-delivered. Falls back to the tagged-scan sum ONLY when no
            # ledger is present (legacy OH tagged path) — byte-identical there.
            "delivered_total_authoritative": d8(
                rl_delivery["delivered_count"] if rl_delivery.get("present")
                else (traj.get("gt_evidence_delivered", 0)
                      + traj.get("gt_scope_delivered", 0)
                      + traj.get("gt_contract_delivered", 0)
                      + traj.get("gt_cochange_delivered", 0)
                      + traj.get("gt_verify_delivered", 0)
                      + traj.get("gt_nudge_delivered", 0))
            ),
            "delivery_authority": (
                "runtime_ledger_seal" if rl_delivery.get("present") else "tag_scan"),
        },
        "behavioral_impact": {
            "total_deliveries": behavioral_impact.get("total_deliveries", 0),
            "total_pivots": behavioral_impact.get("total_pivots", 0),
            "impact_rate": (
                d8(behavioral_impact.get("impact_rate"))
                if int(behavioral_impact.get("total_deliveries") or 0) > 0
                else None
            ),
            "impact_rate_semantics": behavioral_impact.get(
                "impact_rate_semantics", "diagnostic_action_type_transition"
            ),
            "causal_status": behavioral_impact.get("causal_status", "UNMEASURED"),
            "gt_tokens_injected": behavioral_impact.get("gt_tokens_injected"),
            "gt_tokens_injected_units": behavioral_impact.get(
                "gt_tokens_injected_units"
            ),
            "gt_tokens_per_pivot": behavioral_impact.get("gt_tokens_per_pivot"),
            "nudge_compliance_rate": behavioral_impact.get("nudge_compliance_rate"),
            "per_tag": behavioral_impact.get("per_tag", {}),
            "per_tag_impact": behavioral_impact.get(
                "per_tag_impact", behavioral_impact.get("per_tag", {})
            ),
            **(
                {"collection_error": behavioral_impact["collection_error"]}
                if "collection_error" in behavioral_impact else {}
            ),
        },
        "gt_blocks_delivered": d8(consumption.get("gt_blocks_delivered", 0)),
        "gt_blocks_consumed": d8(consumption.get("gt_blocks_consumed", 0)),
        "gt_blocks_verification_followup": d8(
            consumption.get("gt_blocks_verification_followup", 0)
        ),
        "gt_blocks_hard_enforced": d8(consumption.get("gt_blocks_hard_enforced", 0)),
        "gt_blocks_enforced": d8(consumption.get("gt_blocks_hard_enforced", 0)),
        "gt_enforcement_semantics": consumption.get(
            "enforcement_semantics", "hard_block_only"
        ),
        "gt_consumption_ledger_path": ledger_path or None,
        "per_layer": per_layer,
        "agent": {k: (d8(v) if isinstance(v, (int, float)) else v) for k, v in traj.items()},
        # --- provenance of optional inputs (what was missing, never fatal) ---
        "inputs_present": {
            "gt_run_summary": summ_present,
            "output_jsonl": bool(oj and os.path.exists(oj)),
            "miniswe_trajectory": bool(mini.get("found")),
            # which trajectory actually populated the agent-side metrics: the OH
            # output.jsonl, the DeepSWE/pier mini-swe-agent trajectory, or neither.
            "trajectory_source": traj.get("trajectory_source", "none"),
            "run_log": bool(log_path and os.path.exists(log_path)),
            "graph_db": bool(db_resolved),
            "cost_log": bool(cost["llm_calls"]),
        },
    }
    # Performance metrics (sections 1-6, 8-9 of MANDATORY_METRICS.md). A MISSING module is
    # a COLLECTION FAILURE, not performance={} — fail LOUD on ImportError so a run launched
    # without scripts/swebench on PYTHONPATH cannot silently ship no performance section.
    # (compute_performance_metrics never raises — it catches internally — so the compute
    # call needs no broad guard.) A genuinely-absent trajectory is an explicit UNMEASURED
    # marker, never a silent empty dict (defect #1 / #6 / #7).
    try:
        from gt_performance_metrics import (
            build_metric_applicability,
            compute_performance_metrics,
        )
    except ImportError as exc:
        # Path-independent fallback: the module is a SIBLING of this file by construction,
        # so load it by path (the same pattern as the consumption-ledger load above) before
        # failing. Only a TRULY absent/broken module raises — fail-loud, never perf={}.
        _pm_path = os.path.join(os.path.dirname(__file__), "gt_performance_metrics.py")
        if not os.path.isfile(_pm_path):
            raise ImportError(
                "gt_deep_metrics: gt_performance_metrics is REQUIRED for the mandatory "
                "performance metrics (sections 1-6,8-9). A missing module is a COLLECTION "
                "FAILURE, not performance={}. Expected at: " + _pm_path
            ) from exc
        import importlib.util as _ilu
        _pm_spec = _ilu.spec_from_file_location("gt_performance_metrics_gdm", _pm_path)
        if _pm_spec is None or _pm_spec.loader is None:
            raise ImportError(
                "gt_deep_metrics: cannot load gt_performance_metrics from " + _pm_path
            ) from exc
        _pm_mod = _ilu.module_from_spec(_pm_spec)
        _pm_spec.loader.exec_module(_pm_mod)  # a broken module raises here — fail loud
        compute_performance_metrics = _pm_mod.compute_performance_metrics
        build_metric_applicability = _pm_mod.build_metric_applicability
    tj_path = _find_miniswe_trajectory(task, results_dir)
    if tj_path:
        # instance_id=task lets the perf reader resolve TRUE dataset gold from the
        # SWE-bench `patch` (GT_GOLD_JSONL / --gold-jsonl) -> gold_source="dataset_gold"
        # so steps_to_gold_* COMPUTE instead of nulling on the submission proxy.
        # OFFLINE-ONLY: the gold set lands only inside deep["performance"].
        deep["performance"] = compute_performance_metrics(
            tj_path, results_dir, instance_id=task, consumption_ledger=consumption,
            verifier_truth=truth_data,
        )
    else:
        deep["performance"] = {"status": "UNMEASURED", "reason": "no_miniswe_trajectory"}
    deep["metric_applicability"] = build_metric_applicability(
        deep["performance"],
        deep["behavioral_impact"],
        behavioral_collection_succeeded=bool(
            mini_traj
            and consumption_error is None
            and "collection_error" not in deep["behavioral_impact"]
        ),
    )
    # --- ADDITIVE (B-TE): trajectory-economics section. PURE ADDITION — appends a
    # single top-level key and mutates NO existing key. It never raises: a missing or
    # broken module yields a stamped section, never a failed record. Everything else in
    # `deep` is byte-identical to the pre-B-TE output.
    try:
        try:
            from gt_trajectory_economics import compute_trajectory_economics as _cte
        except ImportError:
            import importlib.util as _ilu2
            _te_path = os.path.join(os.path.dirname(__file__), "gt_trajectory_economics.py")
            _te_spec = _ilu2.spec_from_file_location("gt_trajectory_economics_gdm", _te_path)
            _te_mod = _ilu2.module_from_spec(_te_spec)
            _te_spec.loader.exec_module(_te_mod)
            _cte = _te_mod.compute_trajectory_economics
        deep["trajectory_economics"] = _cte(
            task, results_dir,
            graph_db=db_resolved or "",
            ledger_path=rl_delivery_path or "",
            gold_jsonl=os.environ.get("GT_GOLD_JSONL", ""),
        )
    except Exception as _te_exc:  # noqa: BLE001 — additive section must never fail the record
        deep["trajectory_economics"] = {
            "schema": "gt.trajectory_economics.v1",
            "precision_decimals": 8,
            "deferred": ["divergence_step", "resolve_stability",
                         "trajectory_determinism", "compaction_reorientation"],
            "collection_error": f"{type(_te_exc).__name__}: {str(_te_exc)[:200]}",
        }
    return deep


def pair(gt: dict, base: dict) -> dict:
    """GT-on vs baseline deltas at 8-dp (negative delta on action_count = GT is better)."""
    g, b = gt.get("agent", {}), base.get("agent", {})
    def dlt(k):
        # G14: d8 now returns None for missing/NaN. A delta over a missing arm
        # is itself unknown -> None, never a fabricated 0.0.
        gv, bv = d8(g.get(k, 0)), d8(b.get(k, 0))
        if gv is None or bv is None:
            return None
        return d8(gv - bv)
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

- pipeline: `{deep.get('pipeline')}`  ·  model: `{eff.get('model') or 'n/a'}`  ·  trajectory source: `{(deep.get('inputs_present') or {}).get('trajectory_source', 'none')}`
- branch `{deep.get('branch')}` @ `{(deep.get('git_commit') or '')[:12]}`
- **outcome: {deep.get('outcome')}**  ·  resolved={deep.get('resolved')}  ·  has_patch={deep.get('has_patch')}

## Steps / agent behaviour
| metric | value |
|---|---|
{rows([("agent steps (api_calls)", f(a.get('action_count'))), ("assistant messages", f(a.get('assistant_steps'))), ("source edits", f(a.get('edits'))), ("first edit at step", f(a.get('first_edit_action'))), ("first edit detection", a.get('first_edit_detection'))])}

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
{rows([("input tokens", f(eff.get('llm_tokens_in'))), ("  cache-hit", f(eff.get('llm_cache_hit_tokens'))), ("  cache-miss", f(eff.get('llm_cache_miss_tokens'))), ("output tokens", f(eff.get('llm_tokens_out'))), ("total tokens", f(eff.get('llm_tokens_total'))), ("**cost USD**", f"**{f(eff.get('llm_cost_usd'))}**"), ("cost / action USD", f(eff.get('cost_per_action_usd'))), ("cost source", eff.get('cost_source'))])}

## GT reached the agent (fired AND delivered — from agent observation)
Delivery total is the **sealed-ledger authority** (native delivery is TAGLESS, so the per-surface counts below are `<gt-*>` tag scans and read 0 for native forms).
| surface | count |
|---|---|
{rows([("**delivered total (sealed ledger)**", f"**{f(g.get('delivered_total_authoritative'))}**"), ("delivery authority", g.get('delivery_authority')), ("brief delivered", f(g.get('brief_delivered'))), ("gt-evidence delivered (tag scan)", f(g.get('evidence_delivered'))), ("graph-map delivered (tag scan)", f(g.get('graph_map_delivered'))), ("nudges delivered (tag scan)", f(g.get('nudge_delivered'))), ("gt_hook understand calls", f(g.get('understand_calls'))), ("gt_hook verify calls", f(g.get('verify_calls'))), ("GT observation chars", f(g.get('gt_observation_chars_total')))])}

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
{rows([("graph nodes", f(deep.get('graph_nodes'))), ("graph edges", f(deep.get('graph_edges'))), ("verified edge ratio", f(deep.get('verified_edge_ratio'))), ("deterministic edges (import/same_file)", f(deep.get('deterministic_edge_count'))), ("LSP-stamped edges", f(deep.get('lsp_stamped_edge_count'))), ("LSP server", f"{deep.get('lsp_server_name')} ({deep.get('lsp_launch_status')})"), ("FTS5 rows / hits", f"{f(deep.get('fts5_row_count'))} / {f(deep.get('fts5_real_query_result_count'))}"), ("semantic (embedder)", f"{deep.get('semantic_enabled')} dim={f(deep.get('embedder_vector_dim'))}"), ("assertions / linked", f"{f(deep.get('assertion_count'))} / {f(deep.get('linked_assertion_count'))}")])}

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
          f"lsp={deep.get('lsp_server_name')}/{deep.get('lsp_launch_status')}"
          f"(eff={deep.get('lsp_effective_work')},src={deep.get('lsp_status_source')}) "
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
