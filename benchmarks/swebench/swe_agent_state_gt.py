#!/usr/bin/env python3
"""GT state command for SWE-agent — v2.0 two-channel micro-steering hook.

Channel A: MICRO-UPDATE (cheap, every material edit, ≤3 lines / 400 chars)
  - Per-file content hash detection (not global diff hash)
  - Direct sqlite3 query against graph.db (no subprocess)
  - Structured format: GT MICRO [tier] CONSTRAIN/VERIFY/STOP
  - Anti-bloat: exact dedup, window dedup, compliance suppression
  - Steer-score gated: novelty × confidence × relevance

Channel B: VERIFICATION (expensive, budgeted, checkpointed)
  - Pre-submit always (no budget)
  - Loop detection (same file edited 3+ times)
  - Budget: MAX_VERIFY_PER_TASK (presubmit exempt)

STARTUP: localization brief (once, unchanged from v1.1.0)

Research basis:
  ContextBench (2602.05892): precision > recall
  SWE-Skills (2603.15401): weak guidance worse than none
  Anthropic harness engineering: boundaries, not commentary
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
STATE_PATH = Path("/root/state.json")
GT_DB = "/tmp/gt_graph.db"
GT_INTEL = "/tmp/gt_intel.py"
GT_INDEX = "/tmp/gt-index"
REPO_ROOT = "/testbed"
GT_HASHES = Path("/tmp/gt_file_hashes.json")
GT_TELEMETRY = Path("/tmp/gt_hook_telemetry.jsonl")
GT_CHECKPOINT_STARTUP = Path("/tmp/gt_checkpoint_startup")
GT_BRIEFING_DONE = GT_CHECKPOINT_STARTUP
GT_TOOL_COUNTS = Path("/tmp/gt_tool_counts.json")
GT_MICRO_STATE = Path("/tmp/gt_micro_state.json")
GT_DIFF_HASH = Path("/tmp/gt_last_diff_hash")
GT_ACK_STATE = Path("/tmp/gt_ack_state.json")
GT_POLICY_STATE = Path("/tmp/gt_policy_state.json")
GT_HINT_SUPPRESSION = Path("/tmp/gt_hint_suppression.json")
GT_BUDGET_EVENTS = Path("/tmp/gt_budget_events.jsonl")
GT_BUDGET_EVENTS_OFFSET = Path("/tmp/gt_budget_events.offset")  # v12 watermark
GT_PER_TASK_SUMMARY = Path("/tmp/gt_per_task_summary.json")
GT_LAST_ACTION = Path("/tmp/gt_last_action.txt")
GT_LAST_MATERIAL_EDIT_TS = Path("/tmp/gt_last_material_edit.ts")  # v12 submit-gate signal
GT_LAST_GT_CHECK_TS = Path("/tmp/gt_last_gt_check.ts")
GT_LSP_READY = Path("/tmp/gt_lsp_ready")

# ── Config ─────────────────────────────────────────────────────────────────
MAX_VERIFY_PER_TASK = 8
MICRO_MAX_CHARS = 300
MICRO_MAX_LINES = 3
NEXT_WINDOW_SIZE = 2  # v12: shortened window; ground-truth action is written by wrapper, so 6 cycles was over-generous and inflated ack_not_observed
DEDUP_WINDOW_K = 3
COMPLIANCE_THRESHOLD_M = 3
VERIFY_EVERY_N_EDITS = 3   # legacy setting; periodic verify is currently suppressed
MAX_STEPS = 150            # force-submit evidence after this many hook cycles (matches leaderboard per_instance_call_limit)
TIER_VERIFIED = 0.8    # multiple callers + assertions/return
TIER_SILENT = 0.6      # silence weak hints; advisory starts at the shared confidence floor
GT_TOOL_LIMITS = {"orient": 1, "lookup": 2, "impact": 2, "check": 3}
_SUBMIT_SIGNALS = {"COMPLETE_TASK_AND_SUBMIT", "submit", "git diff > patch"}
SOURCE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php",
               ".c", ".cpp", ".h", ".cs", ".kt", ".swift"}

try:
    from gt_intel import (
        CONFIDENCE_AMBIGUITY_MARGIN,
        CONFIDENCE_ADVISORY_FLOOR,
        CONFIDENCE_BLOCKING_FLOOR,
        classify_confidence_policy,
        classify_steering_decision,
        steering_hint_fingerprint,
    )
except Exception:
    CONFIDENCE_AMBIGUITY_MARGIN = 0.12
    CONFIDENCE_ADVISORY_FLOOR = 0.60
    CONFIDENCE_BLOCKING_FLOOR = 0.80

    def classify_confidence_policy(
        confidence: float,
        *,
        unique: bool = True,
        fresh: bool = True,
        is_test: bool = False,
        ambiguity_margin: float = 0.0,
    ) -> tuple[str, str]:
        if is_test:
            return "silent", "test_path"
        if not fresh:
            return "silent", "stale"
        if not unique:
            return "silent", "ambiguous_target"
        if ambiguity_margin and ambiguity_margin < CONFIDENCE_AMBIGUITY_MARGIN:
            return "silent", "near_tie"
        if confidence >= CONFIDENCE_BLOCKING_FLOOR:
            return "blocking", "high_confidence"
        if confidence >= CONFIDENCE_ADVISORY_FLOOR:
            return "advisory", "moderate_confidence"
        return "silent", "low_confidence"

    def classify_steering_decision(
        *,
        stage: str,
        confidence: float,
        unique: bool = True,
        fresh: bool = True,
        is_test: bool = False,
        ambiguity_margin: float = 0.0,
        evidence_level: int = 0,
        direct_diff: bool = False,
        direct_test: bool = False,
        direct_caller: bool = False,
        lsp_only: bool = False,
        presubmit: bool = False,
        target: str | None = None,
        next_action: str | None = None,
    ):
        class _Decision:
            def __init__(self, tier, mode, reason):
                self.tier = tier
                self.mode = mode
                self.reason = reason
                self.target = target
                self.next_action = next_action
                self.confidence = confidence
                self.evidence_level = evidence_level
                self.unique = unique
                self.fresh = fresh
                self.is_test = is_test
                self.presubmit = presubmit
                self.lsp_only = lsp_only

        if is_test or not fresh or evidence_level <= 0 or confidence < CONFIDENCE_SILENT_FLOOR:
            return _Decision(0, "silent", "low_confidence")
        if lsp_only and not (direct_diff or direct_test or direct_caller):
            evidence_level = max(0, evidence_level - 1)
        if ambiguity_margin and ambiguity_margin < CONFIDENCE_AMBIGUITY_MARGIN and evidence_level < 2:
            return _Decision(1, "shortlist", "near_tie")
        if not unique and evidence_level < 2:
            return _Decision(1, "shortlist", "ambiguous_target")
        if presubmit:
            if unique and evidence_level >= 3 and confidence >= CONFIDENCE_BLOCKING_FLOOR:
                return _Decision(3, "blocking", "high_confidence_presubmit")
            if evidence_level >= 2 and confidence >= CONFIDENCE_ADVISORY_FLOOR:
                return _Decision(2, "one_step", "presubmit_warning")
            return _Decision(1, "shortlist", "presubmit_shortlist")
        if unique and evidence_level >= 2 and confidence >= CONFIDENCE_BLOCKING_FLOOR:
            return _Decision(2, "one_step", "high_confidence")
        if evidence_level >= 1 and confidence >= CONFIDENCE_ADVISORY_FLOOR:
            return _Decision(1, "shortlist", "moderate_confidence")
        return _Decision(0, "silent", "insufficient_evidence")

    def steering_hint_fingerprint(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════

def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


def get_tool_counts():
    if GT_TOOL_COUNTS.exists():
        try:
            return json.loads(GT_TOOL_COUNTS.read_text())
        except Exception:
            pass
    return {}


def increment_tool_count(tool):
    counts = get_tool_counts()
    counts[tool] = counts.get(tool, 0) + 1
    try:
        GT_TOOL_COUNTS.write_text(json.dumps(counts))
    except Exception:
        pass


GT_IDENTITY_FILE = Path("/tmp/gt_identity.env")


def _read_identity():
    """Return (arm, run_id, telem_host_dir) freshly resolved on every call.

    Env vars take precedence. If any is missing, fall back to
    /tmp/gt_identity.env (written synchronously by gt_tool_install.sh
    from the per-task bundle). Re-reads the file every call so a hook
    emitted before the file existed still recovers once it appears —
    Gate 1 v11 failed because module-level caching locked in None.
    """
    arm = os.environ.get("GT_ARM", "").strip()
    run_id = os.environ.get("GT_RUN_ID", "").strip()
    telem_host_dir = os.environ.get("GT_TELEMETRY_DIR", "")
    if arm and run_id and telem_host_dir:
        return arm, run_id, telem_host_dir
    try:
        if GT_IDENTITY_FILE.exists():
            for _line in GT_IDENTITY_FILE.read_text().splitlines():
                if "=" in _line and not _line.lstrip().startswith("#"):
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                    if _k and _v:
                        os.environ.setdefault(_k, _v)
            arm = arm or os.environ.get("GT_ARM", "").strip()
            run_id = run_id or os.environ.get("GT_RUN_ID", "").strip()
            telem_host_dir = telem_host_dir or os.environ.get("GT_TELEMETRY_DIR", "")
    except Exception:
        pass
    return arm, run_id, telem_host_dir


def _resolve_instance_id():
    """Best-effort instance_id for telemetry stamping.
    Precedence: GT_INSTANCE_ID env → /root/state.json instance_id → 'unknown'."""
    iid = os.environ.get("GT_INSTANCE_ID", "").strip()
    if iid:
        return iid
    try:
        if STATE_PATH.exists():
            s = json.loads(STATE_PATH.read_text())
            v = s.get("instance_id") or s.get("task_id") or ""
            if v:
                return str(v)
    except Exception:
        pass
    return "unknown"


def _read_cycle():
    try:
        return int(Path("/tmp/gt_step_count").read_text().strip())
    except Exception:
        return 0


def _task_scope():
    arm, run_id, _ = _read_identity()
    iid = _resolve_instance_id()
    parts = [p for p in (run_id, iid, arm) if p]
    scope = "__".join(parts) if parts else "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", scope)[:160]


def _budget_remaining():
    counts = get_tool_counts()
    return {
        tool: max(0, limit - int(counts.get(f"gt_{tool}", 0) or 0))
        for tool, limit in GT_TOOL_LIMITS.items()
    }


def _load_policy():
    if not GT_POLICY_STATE.exists():
        return {}
    try:
        return json.loads(GT_POLICY_STATE.read_text())
    except Exception:
        return {}


def _write_policy(policy):
    try:
        GT_POLICY_STATE.write_text(json.dumps(policy))
    except Exception:
        pass


def _clear_policy():
    try:
        if GT_POLICY_STATE.exists():
            GT_POLICY_STATE.unlink()
    except Exception:
        pass


def _load_hint_suppression():
    if not GT_HINT_SUPPRESSION.exists():
        return {}
    try:
        return json.loads(GT_HINT_SUPPRESSION.read_text())
    except Exception:
        return {}


def _write_hint_suppression(data):
    try:
        GT_HINT_SUPPRESSION.write_text(json.dumps(data))
    except Exception:
        pass


def _clear_hint_suppression():
    try:
        if GT_HINT_SUPPRESSION.exists():
            GT_HINT_SUPPRESSION.unlink()
    except Exception:
        pass


def _hint_is_suppressed(shape: str, fingerprint: str) -> bool:
    sup = _load_hint_suppression()
    if not sup:
        return False
    if sup.get("shape") != shape:
        return False
    return sup.get("fingerprint") == fingerprint


_DRAINED_EVENTS = (
    "budget_denied",
    "orient_redirected",
    "submit_observed",
    "submit_gate_blocked",
    "submit_gate_bypassed",
)


def _drain_budget_events():
    """v12: incremental drain with byte-offset watermark.

    Prior behavior truncated the file, so events that landed between cycles
    could be lost if a reader saw them first. We now track a byte offset in
    GT_BUDGET_EVENTS_OFFSET and only replay lines past it. Safe to call every
    cycle — idempotent and monotonic.
    """
    if not GT_BUDGET_EVENTS.exists():
        return
    try:
        offset = int(GT_BUDGET_EVENTS_OFFSET.read_text().strip())
    except Exception:
        offset = 0
    try:
        size = GT_BUDGET_EVENTS.stat().st_size
    except Exception:
        return
    if offset > size:
        # File was truncated/recreated (e.g. by tests); reset.
        offset = 0
    if offset >= size:
        return
    try:
        with open(GT_BUDGET_EVENTS, "r") as f:
            f.seek(offset)
            new_data = f.read()
            new_offset = f.tell()
    except Exception:
        return
    try:
        GT_BUDGET_EVENTS_OFFSET.write_text(str(new_offset))
    except Exception:
        pass
    for line in new_data.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        event = ev.get("event")
        if event not in _DRAINED_EVENTS:
            continue
        payload = {k: v for k, v in ev.items() if k not in ("event",)}
        log_event(event, **payload)


_IDENTITY_WARNED = False


def _check_identity(arm, run_id, telem_host_dir, instance_id):
    """Emit 'identity_missing' once if arm/run_id/instance_id not all resolvable.
    Per CANARY_VERIFY: any task with missing identity must be marked run_invalid."""
    global _IDENTITY_WARNED
    missing = []
    if not arm:
        missing.append("arm")
    if not run_id:
        missing.append("run_id")
    if not instance_id or instance_id == "unknown":
        missing.append("instance_id")
    if missing and not _IDENTITY_WARNED:
        _IDENTITY_WARNED = True
        # Don't recurse into log_event — write directly.
        try:
            entry = {
                "ts": time.strftime("%H:%M:%S"),
                "event": "identity_missing",
                "missing": missing,
                "arm": arm or None,
                "run_id": run_id or None,
                "instance_id": instance_id or None,
            }
            line = json.dumps(entry) + "\n"
            with open(GT_TELEMETRY, "a") as f:
                f.write(line)
            if telem_host_dir and os.path.isdir(telem_host_dir):
                with open(os.path.join(telem_host_dir, "gt_hook_telemetry.jsonl"), "a") as f:
                    f.write(line)
            sys.stderr.write("[gt_state] IDENTITY_MISSING: %s\n" % ",".join(missing))
            # Also append a visible warning to gt_install.log so harvest
            # surfaces the failure immediately rather than after a full run.
            try:
                with open("/tmp/gt_install.log", "a") as f:
                    f.write("[gt_state] IDENTITY_MISSING: %s (file=%s exists=%s)\n" % (
                        ",".join(missing),
                        str(GT_IDENTITY_FILE),
                        GT_IDENTITY_FILE.exists(),
                    ))
            except Exception:
                pass
        except Exception:
            pass


def log_event(event, **kw):
    try:
        arm, run_id, telem_host_dir = _read_identity()
        iid = _resolve_instance_id()
        _check_identity(arm, run_id, telem_host_dir, iid)
        policy = _load_policy()
        if policy:
            policy["budget_remaining"] = _budget_remaining()
            _write_policy(policy)
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "event": event,
            "run_id": run_id or None,
            "arm": arm or None,
            "instance_id": iid,
            "gt_arm": arm or None,
            "gt_run_id": run_id or None,
            "gt_instance_id": iid,
            "cycle": _read_cycle(),
        }
        if policy:
            entry.update({
                "intervention_id": policy.get("intervention_id"),
                "expected_next_action": policy.get("expected_next_action"),
                "confidence_tier": policy.get("confidence_tier"),
                "budget_remaining": policy.get("budget_remaining"),
                "budget_scope": policy.get("budget_scope"),
            })
        entry.update(kw)
        line = json.dumps(entry) + "\n"
        # Container-local (always)
        with open(GT_TELEMETRY, "a") as f:
            f.write(line)
        # Host-visible (if configured via env var)
        if telem_host_dir:
            host_path = os.path.join(telem_host_dir, "gt_hook_telemetry.jsonl")
            try:
                with open(host_path, "a") as f:
                    f.write(line)
            except Exception:
                pass
        # Always keep a fallback copy — useful when host dir isn't mounted in-container
        fb = "/tmp/.gt"
        os.makedirs(fb, exist_ok=True)
        with open(os.path.join(fb, "gt_hook_telemetry.jsonl"), "a") as f:
            f.write(line)
    except Exception:
        pass


def _emit_per_task_summary(reason):
    """Write the per-task GT summary row. Called at presubmit and step_limit.

    Schema (one JSON object per task, keyed by instance_id):
      run_id, arm, instance_id, cycle, reason
      gt_{orient,lookup,impact,check}_count
      material_edit_count, micro_emit_count, micro_suppress_count,
      verify_emit_count, verify_suppress_count,
      ack_{followed,ignored,not_observed}_count,
      within_call_budget (cycle <= MAX_STEPS), identity_ok
    """
    try:
        _drain_budget_events()
        arm, run_id, telem_host_dir = _read_identity()
        counts = get_tool_counts()
        # Walk telemetry to count events (per-task scope = this container).
        ev_counts = {}
        try:
            if GT_TELEMETRY.exists():
                for line in GT_TELEMETRY.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    e = j.get("event", "")
                    ev_counts[e] = ev_counts.get(e, 0) + 1
        except Exception:
            pass
        iid = _resolve_instance_id()
        row = {
            "run_id": run_id or None,
            "arm": arm or None,
            "instance_id": iid,
            "gt_arm": arm or None,
            "gt_run_id": run_id or None,
            "gt_instance_id": iid,
            "cycle": _read_cycle(),
            "reason": reason,
            "gt_orient_count": counts.get("gt_orient", 0),
            "gt_lookup_count": counts.get("gt_lookup", 0),
            "gt_impact_count": counts.get("gt_impact", 0),
            "gt_check_count": counts.get("gt_check", 0),
            "material_edit_count": ev_counts.get("material_edit", 0),
            "micro_emit_count": ev_counts.get("micro_emitted", 0),
            "micro_suppress_count": ev_counts.get("micro_suppressed", 0),
            "verify_emit_count": ev_counts.get("verify_emitted", 0),
            "verify_suppress_count": ev_counts.get("verify_suppressed", 0),
            "ack_followed_count": ev_counts.get("ack_followed", 0),
            "ack_ignored_count": ev_counts.get("ack_ignored", 0),
            "ack_not_observed_count": ev_counts.get("ack_not_observed", 0),
            "budget_denied_count": ev_counts.get("budget_denied", 0),
            "lsp_promotion_count": ev_counts.get("lsp_promotion", 0),
            "within_call_budget": _read_cycle() <= MAX_STEPS,
            "identity_ok": bool(arm and run_id and iid and iid != "unknown"),
        }
        # Write container-local and host-visible
        body = json.dumps(row, indent=2)
        GT_PER_TASK_SUMMARY.write_text(body)
        if telem_host_dir:
            try:
                with open(os.path.join(telem_host_dir, "gt_per_task_summary.json"), "w") as f:
                    f.write(body)
            except Exception:
                pass
        fb = "/tmp/.gt"
        os.makedirs(fb, exist_ok=True)
        with open(os.path.join(fb, "gt_per_task_summary.json"), "w") as f:
            f.write(body)
    except Exception as e:
        try:
            sys.stderr.write("[gt_state] summary_emit_error: %s\n" % str(e)[:200])
        except Exception:
            pass


def _is_presubmit(state):
    action = str(state.get("action", "")).lower()
    output = str(state.get("last_output", "")).lower()
    combined = action + " " + output
    return any(s.lower() in combined for s in _SUBMIT_SIGNALS)


def _load_json(path, default=None):
    if default is None:
        default = {}
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return dict(default)


def _save_json(path, data):
    try:
        path.write_text(json.dumps(data))
    except Exception:
        pass


def truncate(text, max_chars, max_lines):
    lines = text.strip().split("\n")[:max_lines]
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result


def normalize_for_dedup(text):
    t = re.sub(r":\d+", "", text)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"/[^\s]+/", "", t)
    return t


# ═══════════════════════════════════════════════════════════════════════════
# Micro state (persists between actions)
# ═══════════════════════════════════════════════════════════════════════════

def load_micro_state():
    return _load_json(GT_MICRO_STATE, {
        "scope_window": [], "last_scope_key": "", "edit_count": 0,
        "verify_used": 0, "compliance": {}, "file_edit_counts": {},
    })


def save_micro_state(ms):
    _save_json(GT_MICRO_STATE, ms)


# ═══════════════════════════════════════════════════════════════════════════
# Material edit detection (per-file content hash)
# ═══════════════════════════════════════════════════════════════════════════

def detect_material_edits():
    """Return source files whose content hash changed since last check."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, cwd=REPO_ROOT,
        )
        diff_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        # Also check staged changes
        result2 = subprocess.run(
            ["git", "diff", "--staged", "--name-only"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, cwd=REPO_ROOT,
        )
        staged = [f.strip() for f in result2.stdout.strip().split("\n") if f.strip()]
        diff_files = list(dict.fromkeys(diff_files + staged))  # merge, dedup
        if diff_files:
            log_event("git_diff_found", files=diff_files[:5], unstaged=len(diff_files)-len(staged), staged=len(staged))
    except Exception as e:
        log_event("git_diff_error", detail=str(e)[:100])
        return []

    hashes = _load_json(GT_HASHES)
    changed = []

    for fpath in diff_files:
        ext = os.path.splitext(fpath)[1]
        if ext not in SOURCE_EXTS:
            continue
        base = os.path.basename(fpath)
        if base.startswith("test_") or base.startswith("reproduce"):
            continue
        abs_path = os.path.join(REPO_ROOT, fpath)
        h = file_hash(abs_path)
        if h is None:
            continue
        if h != hashes.get(fpath):
            hashes[fpath] = h
            changed.append(fpath)

    _save_json(GT_HASHES, hashes)

    # v12: stamp material-edit timestamp for the submit-gate wrapper.
    # The submit wrapper compares mtime(gt_last_material_edit.ts) vs
    # mtime(gt_last_gt_check.ts) to decide whether to block submission.
    if changed:
        try:
            GT_LAST_MATERIAL_EDIT_TS.write_text(str(int(time.time())))
        except Exception:
            pass

    return changed


def detect_material_edits_peek():
    """Non-consuming sibling of detect_material_edits().

    Returns the same set of changed source files that detect_material_edits()
    would, but does NOT persist the new hash state and does NOT log the
    git_diff_found/git_diff_error events. Calling this N times in a row
    returns the same set until detect_material_edits() actually consumes
    the transition.

    Used by _check_ack to see str_replace_editor / bash edits when the
    action string itself is empty (SWE-agent never populates state["action"]
    and only gt_* wrappers write GT_LAST_ACTION).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, cwd=REPO_ROOT,
        )
        diff_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        result2 = subprocess.run(
            ["git", "diff", "--staged", "--name-only"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, cwd=REPO_ROOT,
        )
        staged = [f.strip() for f in result2.stdout.strip().split("\n") if f.strip()]
        diff_files = list(dict.fromkeys(diff_files + staged))
    except Exception:
        return []

    hashes = _load_json(GT_HASHES)
    changed = []
    for fpath in diff_files:
        ext = os.path.splitext(fpath)[1]
        if ext not in SOURCE_EXTS:
            continue
        base = os.path.basename(fpath)
        if base.startswith("test_") or base.startswith("reproduce"):
            continue
        abs_path = os.path.join(REPO_ROOT, fpath)
        h = file_hash(abs_path)
        if h is None:
            continue
        if h != hashes.get(fpath):
            changed.append(fpath)
    return changed


# ═══════════════════════════════════════════════════════════════════════════
# Diff-aware symbol targeting
# ═══════════════════════════════════════════════════════════════════════════

def _diff_hunk_symbols(filepath):
    """Parse git diff hunks to find which functions/classes were actually edited.

    Returns deduplicated list of symbol names found by scanning backward from
    each changed hunk to the nearest def/class header.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", filepath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, cwd=REPO_ROOT,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
    except Exception:
        return []

    # Extract edited line ranges from @@ headers
    edited_ranges = []
    for m in re.finditer(
        r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', result.stdout, re.M
    ):
        start = int(m.group(1))
        count = int(m.group(2) or 1)
        edited_ranges.append((start, start + count - 1))

    if not edited_ranges:
        return []

    # Read file and scan backward from each hunk to find enclosing def/class
    abs_path = os.path.join(REPO_ROOT, filepath)
    try:
        with open(abs_path) as f:
            lines = f.readlines()
    except Exception:
        return []

    _def_re = re.compile(r'^\s*(?:async\s+)?(?:def|class)\s+(\w+)')
    symbols = []
    for hunk_start, _ in edited_ranges:
        # Scan backward up to 200 lines to find enclosing scope
        for i in range(min(hunk_start - 1, len(lines) - 1), max(hunk_start - 201, -1), -1):
            if i < 0:
                break
            m = _def_re.match(lines[i])
            if m:
                symbols.append(m.group(1))
                break

    # Deduplicate preserving order
    return list(dict.fromkeys(symbols))


# ═══════════════════════════════════════════════════════════════════════════
# Channel A: Micro-update (cheap, direct sqlite3, no subprocess)
# ═══════════════════════════════════════════════════════════════════════════

def build_micro_update(changed_files):
    """Build a micro-update by querying graph.db directly.

    Uses diff-aware targeting: parses git diff hunks to find the actually-edited
    function, rather than picking the highest-caller symbol in the file.

    Returns a structured dict or None.
    """
    if not changed_files or not os.path.exists(GT_DB):
        return None

    focus = changed_files[0]

    # Step 1: Determine which symbols were actually edited
    hunk_symbols = _diff_hunk_symbols(focus)

    try:
        conn = sqlite3.connect(GT_DB, timeout=3)
        conn.row_factory = sqlite3.Row

        if hunk_symbols:
            # Diff-aware: query only nodes matching edited symbols
            placeholders = ",".join("?" for _ in hunk_symbols)
            nodes = conn.execute(
                f"SELECT id, name, label, return_type FROM nodes "
                f"WHERE file_path = ? AND is_test = 0 "
                f"AND label IN ('Function','Method','Class') "
                f"AND name IN ({placeholders}) ORDER BY start_line",
                [focus] + hunk_symbols,
            ).fetchall()
            intent = "CONSTRAIN"
        else:
            # Fallback: all nodes in file (original highest-caller heuristic)
            nodes = conn.execute(
                "SELECT id, name, label, return_type FROM nodes "
                "WHERE file_path = ? AND is_test = 0 "
                "AND label IN ('Function','Method','Class') ORDER BY start_line",
                (focus,)
            ).fetchall()
            intent = "LOCALIZE"

        if not nodes:
            conn.close()
            return None

        best = None
        best_score = -1.0
        second_score = -1.0
        best_meta = {}

        for node in nodes[:10]:
            nid, name = node["id"], node["name"]

            # Count ALL callers (cross-file + same-file) for scoring
            all_callers = conn.execute(
                "SELECT COUNT(*) as c FROM edges "
                "WHERE target_id = ? AND type = 'CALLS'",
                (nid,)
            ).fetchone()
            caller_count = all_callers["c"] if all_callers else 0

            ret_shape = conn.execute(
                "SELECT value FROM properties WHERE node_id = ? AND kind = 'return_shape'",
                (nid,)
            ).fetchone()

            assert_count = conn.execute(
                "SELECT COUNT(*) as c FROM assertions WHERE target_node_id = ?",
                (nid,)
            ).fetchone()
            asserts = assert_count["c"] if assert_count else 0

            score = caller_count * 0.4 + asserts * 0.3
            if ret_shape and ret_shape["value"] == "value":
                score += 0.3

            if score > best_score:
                second_score = best_score
                best_score = score
                parts = []
                if caller_count > 0:
                    parts.append(f"{caller_count} callers")
                if ret_shape and ret_shape["value"] == "value":
                    parts.append("returns value")
                if asserts > 0:
                    parts.append(f"{asserts} assertions")
                best = (name, ", ".join(parts) if parts else "edited", score)
                best_meta = {
                    "caller_count": caller_count,
                    "asserts": asserts,
                    "ret_shape": ret_shape["value"] if ret_shape else "",
                }
            elif score > second_score:
                second_score = score

        conn.close()

        if not best:
            return None

        sym, constraint, sc = best
        gap = sc - max(second_score, 0.0)
        policy_mode, policy_reason = classify_confidence_policy(
            sc,
            unique=gap >= CONFIDENCE_AMBIGUITY_MARGIN,
            fresh=True,
            is_test=False,
            ambiguity_margin=gap,
        )
        if policy_mode == "silent" or sc < TIER_SILENT:
            log_event("micro_suppressed", reason=policy_reason or "no_signal",
                      score=round(sc, 3), tier="silent",
                      file=focus, focus_symbol=sym,
                      policy_mode=policy_mode, ambiguity_gap=round(gap, 3))
            return None

        # Tiering: verified needs strong multi-signal evidence
        if False and sc < TIER_SILENT:
            log_event("micro_suppressed", reason="no_signal",
                      score=round(sc, 3), tier="silent",
                      file=focus, focus_symbol=sym)
            return None  # SILENT — weak hints worse than none (2603.15401)
        tier = "verified" if sc >= TIER_VERIFIED else "likely"
        base = os.path.basename(focus)

        # Diagnostic framing only — no [NEXT] directive.
        # Basis: SWE-PRM (arXiv 2509.02360) diagnostic > prescriptive;
        # push-style "[NEXT] <tool>" has no 2026 replication (OpenHands PR #5092
        # null A/B; Anthropic/OpenAI SOTA scaffolds omit mid-trajectory push
        # steering). Agent pulls gt_* tools when evidence warrants.
        tag = "VERIFIED" if tier == "verified" else "LIKELY"
        if intent == "CONSTRAIN":
            text = f"[{tag}] {sym}() {constraint} — Next: gt_check {focus}"
        else:
            text = f"[{tag}] {sym}() in {base}: {constraint} — Next: gt_lookup {sym}"

        return (text, focus, sym, intent, tier, sc)

    except Exception:
        return None


def build_micro_update_safe(changed_files):
    """Deterministic micro builder that returns a structured decision.

    This is the safe path used by main(). It applies the shared evidence gate
    and emits only one concrete next step, shortlist, or silence.
    """
    if not changed_files or not os.path.exists(GT_DB):
        return None

    focus = changed_files[0]

    hunk_symbols = _diff_hunk_symbols(focus)
    try:
        conn = sqlite3.connect(GT_DB, timeout=3)
        conn.row_factory = sqlite3.Row
        if hunk_symbols:
            placeholders = ",".join("?" for _ in hunk_symbols)
            nodes = conn.execute(
                f"SELECT id, name, label, return_type FROM nodes "
                f"WHERE file_path = ? AND is_test = 0 "
                f"AND label IN ('Function','Method','Class') "
                f"AND name IN ({placeholders}) ORDER BY start_line",
                [focus] + hunk_symbols,
            ).fetchall()
            intent = "CONSTRAIN"
        else:
            nodes = conn.execute(
                "SELECT id, name, label, return_type FROM nodes "
                "WHERE file_path = ? AND is_test = 0 "
                "AND label IN ('Function','Method','Class') ORDER BY start_line",
                (focus,),
            ).fetchall()
            intent = "LOCALIZE"

        if not nodes:
            conn.close()
            return None

        best = None
        best_score = -1.0
        second_score = -1.0
        best_meta = {}
        for node in nodes[:10]:
            nid, name = node["id"], node["name"]
            all_callers = conn.execute(
                "SELECT COUNT(*) as c FROM edges WHERE target_id = ? AND type = 'CALLS'",
                (nid,),
            ).fetchone()
            caller_count = all_callers["c"] if all_callers else 0
            ret_shape = conn.execute(
                "SELECT value FROM properties WHERE node_id = ? AND kind = 'return_shape'",
                (nid,),
            ).fetchone()
            assert_count = conn.execute(
                "SELECT COUNT(*) as c FROM assertions WHERE target_node_id = ?",
                (nid,),
            ).fetchone()
            asserts = assert_count["c"] if assert_count else 0
            score = caller_count * 0.4 + asserts * 0.3
            if ret_shape and ret_shape["value"] == "value":
                score += 0.3
            if score > best_score:
                second_score = best_score
                best_score = score
                best = (name, score)
                best_meta = {
                    "caller_count": caller_count,
                    "asserts": asserts,
                    "ret_shape": ret_shape["value"] if ret_shape else "",
                }
            elif score > second_score:
                second_score = score
        conn.close()

        if not best:
            return None

        sym, sc = best
        gap = sc - max(second_score, 0.0)
        caller_count = int(best_meta.get("caller_count", 0) or 0)
        assert_count = int(best_meta.get("asserts", 0) or 0)
        ret_shape = best_meta.get("ret_shape", "")
        direct_diff = bool(hunk_symbols)
        direct_caller = caller_count > 0
        evidence_level = 1 + int(direct_diff) + int(direct_caller) + int(assert_count > 0) + int(ret_shape == "value")
        unique = gap >= CONFIDENCE_AMBIGUITY_MARGIN
        next_action = f"gt_check {focus}" if intent == "CONSTRAIN" else f"gt_lookup {sym}"
        decision = classify_steering_decision(
            stage="micro",
            confidence=sc,
            unique=unique,
            fresh=True,
            is_test=False,
            ambiguity_margin=gap,
            evidence_level=evidence_level,
            direct_diff=direct_diff,
            direct_test=False,
            direct_caller=direct_caller,
            lsp_only=False,
            presubmit=False,
            target=focus,
            next_action=next_action,
        )
        if decision.tier == 0:
            log_event("micro_suppressed", reason=decision.reason,
                      score=round(sc, 3), tier="silent",
                      file=focus, focus_symbol=sym,
                      policy_mode=decision.mode, ambiguity_gap=round(gap, 3))
            return None

        base = os.path.basename(focus)
        tag = "SHORTLIST" if decision.tier == 1 else "NEXT"
        if decision.tier >= 2:
            text = f"[{tag}] {sym}() in {base} — Next: {next_action}"
        else:
            text = f"[{tag}] {sym}() in {base} — likely next: {next_action}"
        fingerprint = steering_hint_fingerprint({
            "hook": "micro",
            "file": focus,
            "symbol": sym,
            "intent": intent,
            "tier": decision.tier,
            "confidence": round(sc, 4),
            "gap": round(gap, 4),
            "next_action": next_action,
            "evidence_level": evidence_level,
            "caller_count": caller_count,
            "assert_count": assert_count,
            "ret_shape": ret_shape,
            "direct_diff": direct_diff,
        })
        return {
            "text": text,
            "focus_file": focus,
            "focus_symbol": sym,
            "intent": intent,
            "tier": "verified" if decision.tier >= 2 else "likely",
            "score": sc,
            "decision": decision,
            "fingerprint": fingerprint,
            "shape": "micro",
            "next_action": next_action,
        }
    except Exception:
        return None


def _dedup_key(focus_file, focus_symbol, intent):
    """Scoped dedup key: (file, symbol, intent) tuple hashed."""
    raw = "%s|%s|%s" % (focus_file, focus_symbol, intent)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def should_suppress_micro(text, focus_file, focus_symbol, intent, ms):
    """First-per-file-version policy: emit once per file content hash, then suppress.

    Research basis: 80% of hook fires are waste (KCP study). Firing on
    every material edit floods context. First-per-file-version gives the
    agent one clean signal per meaningful change, then stays quiet.
    """
    key = _dedup_key(focus_file, focus_symbol, intent)

    # First-per-file-version: suppress if we already emitted for this file
    # at its current content hash (set in detect_material_edits)
    emitted_versions = ms.get("emitted_file_versions", {})
    current_hash = _load_json(GT_HASHES).get(focus_file, "")
    if current_hash and emitted_versions.get(focus_file) == current_hash:
        return True, "file_version_dedup"

    if key == ms.get("last_scope_key"):
        return True, "exact_dedup"
    if key in ms.get("scope_window", []):
        return True, "window_dedup"
    comp = ms.get("compliance", {})
    if comp.get(key, 0) >= COMPLIANCE_THRESHOLD_M:
        return True, "compliance"
    return False, ""


def record_micro_emit(text, focus_file, focus_symbol, intent, ms):
    """Update scoped dedup state after emission.  Records file-version so
    subsequent edits to the same file-version are suppressed (first-per-version)."""
    key = _dedup_key(focus_file, focus_symbol, intent)
    ms["last_scope_key"] = key
    window = ms.get("scope_window", [])
    window.append(key)
    ms["scope_window"] = window[-DEDUP_WINDOW_K:]
    comp = ms.get("compliance", {})
    comp[key] = comp.get(key, 0) + 1
    ms["compliance"] = comp
    # Track file-version for first-per-version policy
    current_hash = _load_json(GT_HASHES).get(focus_file, "")
    if current_hash:
        emitted = ms.get("emitted_file_versions", {})
        emitted[focus_file] = current_hash
        ms["emitted_file_versions"] = emitted


# ═══════════════════════════════════════════════════════════════════════════
# Acknowledgment (structural, trace-grade)
# ═══════════════════════════════════════════════════════════════════════════
#
# Arm on emit (micro/verify); scan cycles N+1 .. N+NEXT_WINDOW_SIZE for a
# behavioral delta matching the emitted evidence. Deterministic; no keyword
# overlap. Three outcomes logged as `ack_followed`, `ack_ignored`,
# `ack_not_observed`.

_ACTION_FILE_RE = re.compile(
    r"[A-Za-z0-9_./\-]+\.(?:py|js|ts|go|rs|java|rb|php|c|cpp|h|cs|kt|swift)"
)
_GT_TOOLS_SYMBOL_CMDS = ("gt_lookup", "gt_impact", "gt_explain", "gt_trace")
_GT_TOOLS_FILE_CMDS = ("gt_check", "gt_symbols", "gt_context")
_EDIT_TOOLS = ("str_replace_editor", "create", "open_file", "view",
               "str_replace", "insert")


def _file_suffix_key(path):
    """Return (basename, last-two-dir-components) tuple for loose matching."""
    if not path:
        return ("", "")
    parts = path.replace("\\", "/").strip("/").split("/")
    base = parts[-1]
    suffix = "/".join(parts[-3:-1]) if len(parts) >= 3 else "/".join(parts[:-1])
    return (base, suffix)


def _action_file_refs(action):
    """Extract file paths referenced in action string."""
    if not action:
        return set()
    return set(_ACTION_FILE_RE.findall(str(action)))


def _action_symbol_refs(action):
    """Extract symbol-like arguments from gt_lookup/gt_impact/etc. actions."""
    if not action:
        return set()
    toks = str(action).split()
    if not toks:
        return set()
    first = toks[0].split("/")[-1]
    if first in _GT_TOOLS_SYMBOL_CMDS:
        return {t.strip("()'\"") for t in toks[1:] if t and not t.startswith("-")}
    return set()


def _arm_ack(cycle, channel, tier, focus_file, focus_symbol,
             pre_action, pre_changed, expected_next_action=None,
             hint_fingerprint=None, hint_shape=None):
    """Snapshot pre-emit state so a later cycle can detect behavioral delta.

    The ack window is only armed when we can reduce the intervention to one
    concrete next action. Broad prompts such as "submit or repair" are
    intentionally left unarmed so the live metric stays behaviorally
    meaningful instead of expiring unresolved.
    """
    try:
        if expected_next_action is None:
            expected_next_action = _concrete_expected_next_action(
                channel=channel,
                tier=tier,
                focus_file=focus_file,
                focus_symbol=focus_symbol,
            )
        if not expected_next_action:
            log_event("ack_arm_skipped", reason="broad_expected_action",
                      channel=channel, tier=tier,
                      file=focus_file, symbol=focus_symbol)
            return
        intervention_id = hashlib.sha256(
            f"{cycle}|{channel}|{tier}|{focus_file or ''}|{focus_symbol or ''}".encode("utf-8")
        ).hexdigest()[:12]
        expected_next_action_text = _expected_next_action_text(expected_next_action)
        _write_policy({
            "intervention_id": intervention_id,
            "channel": channel,
            "hint_shape": hint_shape or channel,
            "hint_fingerprint": hint_fingerprint,
            "tier": tier,
            "expected_next_action": expected_next_action,
            "expected_next_action_kind": expected_next_action.get("kind"),
            "expected_next_action_target": expected_next_action.get("target"),
            "expected_next_action_text": expected_next_action_text,
            "confidence_tier": tier,
            "budget_remaining": _budget_remaining(),
            "budget_scope": _task_scope(),
            "file": focus_file or "",
            "symbol": focus_symbol or "",
        })
        GT_ACK_STATE.write_text(json.dumps({
            "cycle": int(cycle),
            "channel": channel,
            "tier": tier,
            "intervention_id": intervention_id,
            "expected_next_action": expected_next_action,
            "expected_next_action_kind": expected_next_action.get("kind"),
            "expected_next_action_target": expected_next_action.get("target"),
            "expected_next_action_text": expected_next_action_text,
            "confidence_tier": tier,
            "hint_shape": hint_shape or channel,
            "hint_fingerprint": hint_fingerprint,
            "file": focus_file or "",
            "file_key": list(_file_suffix_key(focus_file)),
            "symbol": focus_symbol or "",
            "pre_emit_action": str(pre_action or "")[:500],
            "pre_emit_changed": sorted(pre_changed or []),
            "pre_emit_file_refs": sorted(_action_file_refs(pre_action)),
            "pre_emit_symbol_refs": sorted(_action_symbol_refs(pre_action)),
            "expires_at_cycle": int(cycle) + NEXT_WINDOW_SIZE,
        }))
    except Exception as e:
        log_event("ack_arm_error", detail=str(e)[:120])


def _expected_next_action_text(spec):
    if not spec:
        return ""
    kind = spec.get("kind") or ""
    target = spec.get("target") or ""
    if kind == "gt_lookup":
        return f"gt_lookup {target}".strip()
    if kind == "gt_impact":
        return f"gt_impact {target}".strip()
    if kind == "gt_check":
        return f"gt_check {target}".strip()
    if kind == "submit":
        return "submit"
    if kind == "repair":
        return f"repair {target}".strip()
    return str(spec.get("text") or "").strip()


def _concrete_expected_next_action(channel, tier, focus_file, focus_symbol):
    """Return one concrete next action or None.

    We intentionally avoid multi-valued windows because those are the main
    source of unresolved real-task acks.
    """
    if channel == "orient":
        if focus_symbol and tier in ("verified", "likely"):
            # Use lookup for localization and impact when the candidate is
            # already a concrete symbol.
            return {
                "kind": "gt_lookup",
                "target": focus_symbol,
                "text": f"gt_lookup {focus_symbol}",
            }
        if focus_file and tier in ("verified", "likely"):
            return {
                "kind": "gt_check",
                "target": focus_file,
                "text": f"gt_check {focus_file}",
            }
        return None
    if channel == "micro":
        if focus_file:
            return {
                "kind": "gt_check",
                "target": focus_file,
                "text": f"gt_check {focus_file}",
            }
        return None
    if channel == "verify":
        if focus_file:
            return {
                "kind": "submit",
                "target": focus_file,
                "text": "submit",
            }
        return None
    return None


def _load_ack():
    if not GT_ACK_STATE.exists():
        return None
    try:
        return json.loads(GT_ACK_STATE.read_text())
    except Exception:
        return None


def _clear_ack():
    try:
        if GT_ACK_STATE.exists():
            GT_ACK_STATE.unlink()
    except Exception:
        pass
    _clear_policy()


def _read_last_action():
    """v12 ground-truth read.

    GT_LAST_ACTION is written by /tmp/gt_intel_wrapper.py for gt_* actions
    and by the submit wrapper for submission outcomes (before execution).
    SWE-agent's bash thought-action scaffold does not surface the bash action
    string cleanly to the hook — state.get("action","") is usually empty for
    non-gt actions — so the wrapper file is the only reliable observation
    channel. See Anthropic "Building Effective Agents": ground truth must
    come from the environment, not self-report.

    The wrapper writes "<cmd>:<arg>" (e.g. "lookup:foo" or
    "submit_blocked:/path/to/file.py") for compactness; normalize to
    "gt_<cmd> <arg>" for GT tool calls and "submit[_status] <path>" for
    submit-path observation so _action_file_refs() can parse it with the same
    token logic as bash-action strings.
    """
    try:
        if GT_LAST_ACTION.exists():
            txt = GT_LAST_ACTION.read_text().strip()
            if not txt:
                return None
            if ":" in txt and not txt.startswith("gt_"):
                tool, _, arg = txt.partition(":")
                if tool in ("orient", "lookup", "impact", "check"):
                    return f"gt_{tool} {arg}".strip()
                if tool in ("submit", "submit_blocked", "submit_bypassed"):
                    return f"{tool} {arg}".strip()
            return txt
    except Exception:
        pass
    return None


def _read_last_submit_observation():
    """Fallback submit observation sourced from the durable budget event log.

    The submit wrapper already emits `submit_observed` records into
    /tmp/gt_budget_events.jsonl before handoff. If the plain last-action marker
    is missing or stale, this lets _check_ack recover the completion-path
    signal without widening the steering surface.
    """
    try:
        if not GT_BUDGET_EVENTS.exists():
            return None
        lines = GT_BUDGET_EVENTS.read_text().splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") != "submit_observed":
            continue
        status = (ev.get("status") or "").strip()
        file = (ev.get("file") or "").strip()
        if status == "blocked":
            return f"submit_blocked {file}".strip()
        if status == "bypassed":
            return f"submit_bypassed {file}".strip()
        if status == "allowed":
            return f"submit {file}".strip()
    return None


def _check_ack(cycle, action, changed_files):
    """Compare current cycle's action against armed pre-emit snapshot.
    Emits exactly one of: ack_followed / ack_ignored / ack_not_observed."""
    armed = _load_ack()
    if not armed:
        return
    arm_cycle = armed.get("cycle", 0)
    if cycle <= arm_cycle:
        return  # same cycle as arming — cannot self-ack
    expires_at = armed.get("expires_at_cycle", arm_cycle + NEXT_WINDOW_SIZE)

    # v12: prefer the wrapper-written ground truth action over the scaffold's
    # empty/noisy state.action. Root cause of v11 ack_followed=0: action was
    # always "" in the bash thought-action scaffold, so every symbol-match
    # check failed and every window expired as ack_not_observed.
    ack_source = "state_action"
    _gt_action = _read_last_action()
    if not _gt_action:
        _gt_action = _read_last_submit_observation()
        if _gt_action:
            ack_source = "submit_observed"
    if _gt_action:
        action = _gt_action

    focus_file = armed.get("file", "")
    focus_key = tuple(armed.get("file_key") or ("", ""))
    focus_symbol = armed.get("symbol", "")
    hint_shape = armed.get("hint_shape") or armed.get("channel") or ""
    hint_fingerprint = armed.get("hint_fingerprint")
    pre_files = set(armed.get("pre_emit_file_refs", []))
    pre_symbols = set(armed.get("pre_emit_symbol_refs", []))
    pre_changed = set(armed.get("pre_emit_changed", []))

    new_files = _action_file_refs(action)
    new_symbols = _action_symbol_refs(action)
    added_files = new_files - pre_files
    added_symbols = new_symbols - pre_symbols
    edit_delta = set(changed_files or []) - pre_changed

    action_head = (str(action or "").split() or [""])[0].split("/")[-1]
    file_match = False
    for ref in added_files:
        rk = _file_suffix_key(ref)
        if rk[0] and rk[0] == focus_key[0]:
            file_match = True
            break
        if focus_file and ref.endswith(focus_file.split("/")[-1]):
            file_match = True
            break

    # Submission-path observation: a submit attempt itself is a meaningful
    # follow/ignore signal for verify windows. Prefer the wrapper-written
    # action marker, but if that is missing/stale, accept the durable
    # submit_observed event as a fallback so the live metric remains
    # end-to-end observable.
    if action_head in ("submit", "submit_bypassed", "submit_blocked"):
        if action_head == "submit_blocked":
            log_event("ack_ignored", reason="blocked_submit",
                      channel=armed.get("channel"), tier=armed.get("tier"),
                      file=focus_file, symbol=focus_symbol, cycle=cycle,
                      arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
            _clear_ack()
            return
        if file_match:
            log_event("ack_followed", reason="targeted_submit",
                      channel=armed.get("channel"), tier=armed.get("tier"),
                      file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
        else:
            log_event("ack_ignored", reason="non_targeted_submit",
                      channel=armed.get("channel"), tier=armed.get("tier"),
                      file=focus_file, symbol=focus_symbol, cycle=cycle,
                      arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
        _clear_ack()
        return

    # Symbol-level targeted follow
    if focus_symbol and focus_symbol in added_symbols \
            and action_head in _GT_TOOLS_SYMBOL_CMDS:
        log_event("ack_followed", reason="targeted_lookup",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  symbol=focus_symbol, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        _clear_ack()
        return

    # File-level targeted gt_check / gt_symbols
    if file_match and action_head in _GT_TOOLS_FILE_CMDS:
        log_event("ack_followed", reason="targeted_check",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        _clear_ack()
        return

    # File-level edit/read on focus file
    if file_match and action_head in _EDIT_TOOLS:
        log_event("ack_followed", reason="targeted_edit_or_read",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        _clear_ack()
        return

    # Hash-peek-inferred classification: action_head is empty for
    # str_replace_editor / bash (gt_* wrappers and the submit wrapper write
    # GT_LAST_ACTION instead), but detect_material_edits_peek() still surfaces
    # filesystem changes.
    # If any peek-detected edit matches focus → targeted_edit_inferred;
    # otherwise (edit_delta non-empty, disjoint) → non_targeted_edit_inferred.
    edit_delta_hits_focus = False
    for ef in edit_delta:
        ek = _file_suffix_key(ef)
        if ek[0] and ek[0] == focus_key[0]:
            edit_delta_hits_focus = True
            break
        if focus_file and ef.endswith(focus_file.split("/")[-1]):
            edit_delta_hits_focus = True
            break
    if edit_delta_hits_focus:
        log_event("ack_followed", reason="targeted_edit_inferred",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        _clear_ack()
        return

    # Stronger ignored signal: the model took a meaningful action inside the
    # evidence window, but on the wrong file/symbol. This is the common
    # "read the evidence, then go edit tests or another module" pattern.
    if cycle > arm_cycle:
        if action_head in _GT_TOOLS_SYMBOL_CMDS + _GT_TOOLS_FILE_CMDS:
            log_event("ack_ignored", reason="non_targeted_gt_action",
                      channel=armed.get("channel"),
                      tier=armed.get("tier"), file=focus_file,
                      symbol=focus_symbol, cycle=cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source,
                      arm_cycle=arm_cycle)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
            _clear_ack()
            return
        if action_head in _EDIT_TOOLS and action_head:
            log_event("ack_ignored", reason="non_targeted_edit",
                      channel=armed.get("channel"),
                      tier=armed.get("tier"), file=focus_file,
                      symbol=focus_symbol, cycle=cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source,
                      arm_cycle=arm_cycle)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
            _clear_ack()
            return
        if edit_delta:
            # edit_delta non-empty and none matched focus above → wrong file
            log_event("ack_ignored", reason="non_targeted_edit_inferred",
                      channel=armed.get("channel"),
                      tier=armed.get("tier"), file=focus_file,
                      symbol=focus_symbol, cycle=cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source,
                      arm_cycle=arm_cycle)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
            _clear_ack()
            return

    # Window expiry
    if cycle >= expires_at:
        if edit_delta or action_head in _EDIT_TOOLS + _GT_TOOLS_FILE_CMDS \
                + _GT_TOOLS_SYMBOL_CMDS:
            log_event("ack_ignored", channel=armed.get("channel"),
                      tier=armed.get("tier"), file=focus_file,
                      symbol=focus_symbol, cycle=cycle, arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
            if hint_fingerprint:
                _write_hint_suppression({
                    "shape": hint_shape,
                    "fingerprint": hint_fingerprint,
                    "reason": "ack_ignored",
                    "cycle": cycle,
                    "target": armed.get("expected_next_action_target"),
                })
        else:
            log_event("ack_not_observed", channel=armed.get("channel"),
                      tier=armed.get("tier"), file=focus_file,
                      symbol=focus_symbol, cycle=cycle, arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
        _clear_ack()


def _terminal_close_ack(cycle, reason):
    """Close any armed ack as not_observed at terminal exits (submit/step_limit)."""
    armed = _load_ack()
    if not armed:
        return
    log_event("ack_not_observed", reason=reason,
              channel=armed.get("channel"), tier=armed.get("tier"),
              file=armed.get("file"), symbol=armed.get("symbol"),
              cycle=cycle, arm_cycle=armed.get("cycle"),
              intervention_id=armed.get("intervention_id"))
    _clear_ack()


# ═══════════════════════════════════════════════════════════════════════════
# Channel B: Verification (expensive, budgeted)
# ═══════════════════════════════════════════════════════════════════════════

def should_verify(ms, presubmit=False):
    # Confidence-gated policy: verification is only surfaced at the presubmit
    # boundary in this pass. Periodic mid-task verify is intentionally silent.
    return bool(presubmit)


_LAST_VERIFY_HASH = ""


def run_verification(changed_files):
    """Run gt_intel --reminder on changed files. Returns compact verdict.
    Deduplicates: suppresses if identical to last verify output."""
    global _LAST_VERIFY_HASH
    outputs = []
    for fpath in changed_files[:2]:
        _try_reindex(fpath)
        ev = _run_gt_intel(fpath)
        if ev:
            outputs.append(ev)
    if not outputs:
        return None
    merged = "\n".join(outputs)
    # Verify dedup — don't repeat identical verify text
    vh = hashlib.sha256(merged.encode()).hexdigest()[:12]
    if vh == _LAST_VERIFY_HASH:
        log_event("verify_dedup", reason="identical_to_last")
        return None
    _LAST_VERIFY_HASH = vh
    return f"GT VERIFY:\n{merged}"


def _try_reindex(fpath):
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={fpath}",
             f"--root={REPO_ROOT}", f"--output={GT_DB}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, cwd=REPO_ROOT,
        )
    except Exception:
        pass


def _run_gt_intel(fpath):
    """Run gt_intel --reminder, return actionable tiers (VERIFIED + WARNING).

    Previously filtered to [VERIFIED] only, which dropped CALLER/TEST/IMPACT/
    PRECEDENT family output (tier [WARNING], 0.5-0.9 confidence) and starved
    the agent of everything except IMPORT evidence. Expanding to [WARNING]
    restores the 7-family signal; [INFO] (<0.5) still suppressed as noise.
    """
    try:
        result = subprocess.run(
            ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={fpath}",
             f"--root={REPO_ROOT}", "--reminder"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, cwd=REPO_ROOT,
        )
        out = result.stdout.strip()
        if not out or len(out) < 10 or "Error" in out[:30]:
            return ""
        actionable_tags = ("[VERIFIED]", "[WARNING]", "[CONTRACT]", "[CRITICAL]")
        lines = []
        for line in out.split("\n"):
            s = line.strip()
            if "[STALE]" in s:
                continue
            if any(tag in s for tag in actionable_tags):
                s = s.replace("<gt-evidence>", "").replace("</gt-evidence>", "").strip()
                lines.append(s)
        return "\n".join(lines[:4])  # widened from 2→4 to accommodate extra families
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Startup briefing (unchanged from v1.1.0)
# ═══════════════════════════════════════════════════════════════════════════

def _fallback_orient():
    """Minimal orient summary when full briefing fails — ensures first delivery is never empty."""
    try:
        conn = sqlite3.connect(GT_DB, timeout=3)
        total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        files = conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[0]
        # Top 5 most-called symbols
        top = conn.execute(
            "SELECT n.name, n.file_path, COUNT(e.id) as c "
            "FROM nodes n JOIN edges e ON e.target_id = n.id "
            "WHERE n.is_test = 0 AND e.type = 'CALLS' "
            "GROUP BY n.id ORDER BY c DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if not top:
            return ""
        lines = [f"[GT ORIENT] {total} symbols across {files} files. Key symbols:"]
        for name, fpath, cnt in top:
            lines.append(f"  - {name} ({fpath}) — {cnt} callers")
        return "\n".join(lines)
    except Exception:
        return ""


def generate_pre_edit_briefing():
    if GT_BRIEFING_DONE.exists():
        return ""
    GT_BRIEFING_DONE.touch()

    issue_text = os.environ.get("PROBLEM_STATEMENT", "")
    if not issue_text:
        for p in ["/tmp/gt_issue.txt", "/tmp/problem_statement.txt"]:
            if os.path.exists(p):
                try:
                    issue_text = open(p).read()[:3000]
                    break
                except Exception:
                    pass

    if not os.path.exists(GT_DB):
        log_event("pre_edit_briefing", status="skipped", reason="no_db")
        return ""

    if not issue_text:
        log_event("pre_edit_briefing", status="fallback_orient", reason="no_issue_text")
        return _fallback_orient()

    try:
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        from gt_intel import (
            classify_confidence_policy,
            compute_localization,
            format_localization_briefing,
        )

        conn = sqlite3.connect(GT_DB, timeout=5)
        loc = compute_localization(conn, issue_text, root=".")

        if not loc.candidates:
            log_event("pre_edit_briefing", status="no_candidates_fallback_orient")
            conn.close()
            return _fallback_orient()

        top = loc.candidates[0]
        next_conf = loc.candidates[1].confidence if len(loc.candidates) > 1 else 0.0
        gap = top.confidence - next_conf
        unique = len(loc.candidates) == 1 or gap >= 0.12
        policy_mode, policy_reason = classify_confidence_policy(
            top.confidence,
            unique=unique,
            fresh=True,
            is_test=bool(getattr(top.node, "is_test", False)),
            ambiguity_margin=gap,
        )
        out = format_localization_briefing(loc, conn, ".")
        conn.close()

        if out:
            _arm_ack(_read_cycle(), channel="orient", tier=top.tier,
                     focus_file=top.node.file_path, focus_symbol=top.node.name,
                     pre_action="", pre_changed=[])
            log_event("pre_edit_briefing", status="emitted",
                      tier=top.tier, confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            return out

        if policy_mode == "silent":
            log_event("pre_edit_briefing", status="policy_silent",
                      reason=policy_reason, tier=top.tier,
                      confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            GT_CHECKPOINT_STARTUP.touch()
            return ""

    except Exception as e:
        log_event("pre_edit_briefing", status="error_fallback_orient", detail=str(e)[:100])

    # Last resort: deliver minimal orient so first delivery is never empty
    return _fallback_orient()


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def _flush_gt_to_state(state):
    """Stamp GT counters + identity + last-event onto the state dict.

    The hook's container-local telemetry (/tmp/gt_hook_telemetry.jsonl)
    and $GT_TELEMETRY_DIR writes may not escape the sweagent container
    (no shared mount). But sweagent merges /root/state.json into every
    traj step, so anything we put here lands in the .traj file on the
    host and survives container removal.
    """
    try:
        arm, run_id, _telem_host_dir = _read_identity()
        counts = get_tool_counts()
        iid = _resolve_instance_id()
        # sweagent's AgentRunResult schema validates state values as strings;
        # serialize dict payloads to JSON strings so they survive the schema
        # round-trip. Readers can `json.loads(state["gt_identity"])` to recover.
        state["gt_identity"] = json.dumps({
            "arm": arm or None,
            "run_id": run_id or None,
            "instance_id": iid,
            "cycle": _read_cycle(),
        })
        state["gt_arm"] = arm or None
        state["gt_run_id"] = run_id or None
        state["gt_instance_id"] = iid
        state["gt_counters"] = json.dumps({
            "orient": int(counts.get("gt_orient", 0)),
            "lookup": int(counts.get("gt_lookup", 0)),
            "impact": int(counts.get("gt_impact", 0)),
            "check": int(counts.get("gt_check", 0)),
        })
        # Event tallies from telemetry
        ev_counts = {}
        try:
            if GT_TELEMETRY.exists():
                for _line in GT_TELEMETRY.read_text().splitlines()[-500:]:
                    if not _line.strip():
                        continue
                    try:
                        _j = json.loads(_line)
                    except Exception:
                        continue
                    _e = _j.get("event", "")
                    ev_counts[_e] = ev_counts.get(_e, 0) + 1
        except Exception:
            pass
        state["gt_events"] = json.dumps({
            "material_edit": ev_counts.get("material_edit", 0),
            "micro_emitted": ev_counts.get("micro_emitted", 0),
            "micro_suppressed": ev_counts.get("micro_suppressed", 0),
            "verify_emitted": ev_counts.get("verify_emitted", 0),
            "verify_suppressed": ev_counts.get("verify_suppressed", 0),
            "ack_followed": ev_counts.get("ack_followed", 0),
            "ack_ignored": ev_counts.get("ack_ignored", 0),
            "ack_not_observed": ev_counts.get("ack_not_observed", 0),
            "budget_denied": ev_counts.get("budget_denied", 0),
            "submit_observed": ev_counts.get("submit_observed", 0),
            "lsp_promotion": ev_counts.get("lsp_promotion", 0),
            "checkpoint_startup": ev_counts.get("checkpoint_startup", 0),
            "identity_missing": ev_counts.get("identity_missing", 0),
        })
        state["gt_policy"] = json.dumps(_load_policy())
        # Tail of the telemetry log — last event line of interest
        try:
            if GT_TELEMETRY.exists():
                _lines = [l for l in GT_TELEMETRY.read_text().splitlines() if l.strip()]
                if _lines:
                    state["gt_last_event"] = _lines[-1]  # already a JSON string
        except Exception:
            pass
    except Exception as _e:
        state["gt_flush_error"] = str(_e)[:200]


def _write_state(state):
    """Flush GT telemetry into state then persist for sweagent to read."""
    try:
        _flush_gt_to_state(state)
    except Exception:
        pass
    STATE_PATH.write_text(json.dumps(state))


def main():
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:
            state = {}

    state["working_dir"] = os.getcwd()
    state.pop("gt_evidence", None)
    _drain_budget_events()

    if not os.path.exists(GT_DB):
        log_event("cycle", status="no_db")
        _write_state(state)
        return

    # ── STEP COUNTER (belt-and-suspenders with model.per_instance_call_limit) ─
    _step_file = Path("/tmp/gt_step_count")
    _step = int(_step_file.read_text().strip()) if _step_file.exists() else 0
    _step += 1
    _step_file.write_text(str(_step))
    if _step >= MAX_STEPS:
        # A3: write a real patch from current diff (EXIT trap is unreliable).
        try:
            pr = subprocess.run(
                ["git", "-C", REPO_ROOT, "diff", "--",
                 ".", ":(exclude)**/test_*", ":(exclude)**/reproduce*"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
            )
            patch = pr.stdout or ""
            if patch.strip():
                Path("/root/model.patch").write_text(patch)
                log_event("max_steps_patch_fallback",
                          bytes=len(patch), status="written")
            else:
                log_event("max_steps_patch_fallback",
                          bytes=0, status="empty_diff")
        except Exception as e:
            log_event("max_steps_patch_fallback",
                      status="error", detail=str(e)[:200])

        _terminal_close_ack(_step, reason="step_limit")
        state["gt_evidence"] = (
            "[SUBMIT NOW] You have used %d/%d steps. "
            "Submit your changes immediately with the submit command. "
            "Do not make any more edits." % (_step, MAX_STEPS)
        )
        log_event("step_limit_reached", step=_step, max=MAX_STEPS)
        _emit_per_task_summary(reason="step_limit")
        _write_state(state)
        return

    # ── 0. ACKNOWLEDGMENT CHECK (structural, trace-grade) ──────────────
    # If a prior cycle armed an ack snapshot, compare this cycle's action
    # against the snapshot to detect `followed` / `ignored` / `not_observed`.
    #
    # SWE-agent never writes `action` to /root/state.json — it only
    # initializes it to `{}` and reads state commands back. So
    # state.get("action") is always "". Instead, the gt_* wrappers deposit
    # their last invocation at /tmp/gt_last_action.txt; we consume it here
    # so the classifier can see targeted gt_check / gt_lookup / gt_impact
    # calls on the focus file/symbol. For edit-tool matching, we also pass
    # the freshly-detected changed file list.
    _current_action = state.get("action", "") or ""
    if not _current_action and GT_LAST_ACTION.exists():
        try:
            _current_action = GT_LAST_ACTION.read_text().strip()
        except Exception:
            _current_action = ""
        try:
            GT_LAST_ACTION.unlink()
        except Exception:
            pass
    # Use detect_material_edits_peek() (non-consuming) so the ack classifier
    # can see str_replace_editor / bash edits. The canonical consuming call
    # at line ~1148 still runs afterwards, so micro_emitted behavior is
    # preserved. This closes the "classifier blind to non-GT actions" gap
    # that made rerun8 return ack_not_observed across 229 str_replace_editor
    # invocations — _current_action is "" for those tools because only
    # gt_* wrappers write GT_LAST_ACTION, so edit_delta is the only signal
    # available to the classifier inside the window.
    _check_ack(_step, _current_action, detect_material_edits_peek())

    # ── 1. STARTUP (once) ──────────────────────────────────────────────
    if not GT_CHECKPOINT_STARTUP.exists():
        briefing = generate_pre_edit_briefing_safe()
        increment_tool_count("gt_orient")
        log_event("checkpoint_startup",
                  status="emitted" if briefing else "empty")
        if briefing:
            state["gt_evidence"] = briefing
            _write_state(state)
            return
        GT_CHECKPOINT_STARTUP.touch()

    # ── 2. PRESUBMIT (always, no budget cost) ──────────────────────────
    if _is_presubmit(state):
        changed = detect_material_edits()
        verdict = run_verification(changed) if changed else None
        if verdict:
            state["gt_evidence"] = truncate(verdict, 600, 5)
            log_event("verify_emitted", chars=len(verdict),
                      presubmit=True, tier="likely",
                      files=(changed or [])[:3])
        else:
            log_event("verify_suppressed", presubmit=True,
                      reason="no_verdict" if changed else "no_edit")
        increment_tool_count("presubmit")
        log_event("checkpoint_presubmit",
                  status="emitted" if state.get("gt_evidence") else "empty")
        _terminal_close_ack(_step, reason="presubmit")
        _emit_per_task_summary(reason="presubmit")
        _write_state(state)
        return

    # ── 3. DETECT MATERIAL EDITS ───────────────────────────────────────
    changed = detect_material_edits()
    if not changed:
        log_event("cycle", status="no_edit")
        _write_state(state)
        return

    ms = load_micro_state()
    ms["edit_count"] = ms.get("edit_count", 0) + 1
    fec = ms.get("file_edit_counts", {})
    for f in changed:
        fec[f] = fec.get(f, 0) + 1
    ms["file_edit_counts"] = fec

    log_event("material_edit", files=changed[:3], edit_count=ms["edit_count"])

    # ── 4. CHANNEL A: MICRO-UPDATE ─────────────────────────────────────
    micro_result = build_micro_update_safe(changed)

    # ── 4a. LSP-HYBRID: promote ambiguous edges (if enabled) ──────────
    promo_stats = {}
    lsp_enabled = os.environ.get("GT_LSP_ENABLED") == "1"
    lsp_ready = GT_LSP_READY.exists()
    if lsp_enabled and changed:
        if not lsp_ready:
            log_event("lsp_status", enabled=True, status="warming_up", ready=False,
                      changed=len(changed))
        else:
            try:
                if "/tmp" not in sys.path:
                    sys.path.insert(0, "/tmp")
                from lsp_promoter import promote_ambiguous_edges
                promo_stats = promote_ambiguous_edges(
                    source_files=changed, db_path=GT_DB,
                    root=REPO_ROOT, language="python")
                lsp_conf = 0.0
                lsp_level = 0
                if promo_stats.get("verified", 0) > 0:
                    lsp_conf = 0.78
                    lsp_level = 2
                elif promo_stats.get("corrected", 0) > 0:
                    lsp_conf = 0.68
                    lsp_level = 1
                lsp_decision = classify_steering_decision(
                    stage="lsp",
                    confidence=lsp_conf,
                    unique=True,
                    fresh=True,
                    is_test=False,
                    ambiguity_margin=1.0,
                    evidence_level=lsp_level,
                    direct_diff=bool(changed),
                    direct_test=False,
                    direct_caller=False,
                    lsp_only=True,
                    presubmit=False,
                    target=(changed[0] if changed else ""),
                    next_action=("gt_check %s" % changed[0]) if changed else "gt_lookup",
                )
                log_event("lsp_promotion", **promo_stats,
                          decision_tier=lsp_decision.tier,
                          decision_reason=lsp_decision.reason)
                # If edges improved and the shared gate allows it, re-run micro
                if (promo_stats.get("verified", 0) > 0 or promo_stats.get("corrected", 0) > 0) \
                        and lsp_decision.tier > 0:
                    micro_result = build_micro_update_safe(changed)
            except Exception as e:
                promo_stats = {"error": str(e)[:200]}
                log_event("lsp_promotion_error", detail=str(e)[:200])
    elif lsp_enabled:
        log_event("lsp_status", enabled=True, status="ready" if lsp_ready else "warming_up",
                  ready=lsp_ready, changed=0)
    else:
        log_event("lsp_status", enabled=False, status="disabled", ready=False, changed=0)

    if micro_result:
        micro_text = micro_result["text"]
        focus_file = micro_result["focus_file"]
        focus_sym = micro_result["focus_symbol"]
        intent = micro_result["intent"]
        tier = micro_result["tier"]
        score = micro_result["score"]
        fingerprint = micro_result["fingerprint"]
        decision = micro_result["decision"]
        if _hint_is_suppressed("micro", fingerprint):
            log_event("micro_suppressed", reason="ack_ignored_repeat",
                      file=focus_file, focus_symbol=focus_sym, intent=intent)
        else:
            suppress, reason = should_suppress_micro(
                micro_text, focus_file, focus_sym, intent, ms)
            if not suppress:
                state["gt_evidence"] = truncate(micro_text, MICRO_MAX_CHARS, MICRO_MAX_LINES)
                record_micro_emit(micro_text, focus_file, focus_sym, intent, ms)
                # Arm structural ack: next cycles will be compared against this snapshot
                _arm_ack(_step, channel="micro", tier=tier,
                         focus_file=focus_file, focus_symbol=focus_sym,
                         pre_action=state.get("action", ""), pre_changed=changed,
                         hint_fingerprint=fingerprint, hint_shape="micro")
                log_event("micro_emitted", chars=len(state["gt_evidence"]),
                          file=focus_file, focus_symbol=focus_sym,
                          intent=intent, tier=tier, score=round(score, 2),
                          decision_tier=decision.tier,
                          decision_reason=decision.reason,
                          hint_fingerprint=fingerprint,
                          promotion_stats=promo_stats)
            else:
                log_event("micro_suppressed", reason=reason, file=changed[0],
                          focus_symbol=focus_sym, intent=intent)
    else:
        log_event("micro_suppressed", reason="no_signal_no_candidate",
                  file=changed[0] if changed else "")

    # ── 5. CHANNEL B: VERIFICATION (budgeted) ─────────────────────────
    if should_verify(ms):
        verdict = run_verification(changed)
        ms["verify_used"] = ms.get("verify_used", 0) + 1
        if verdict:
            verify_conf = 0.90 if "[VERIFIED]" in verdict else 0.65
            verify_level = 3 if "[VERIFIED]" in verdict else 2
            verify_decision = classify_steering_decision(
                stage="verify",
                confidence=verify_conf,
                unique=True,
                fresh=True,
                is_test=False,
                ambiguity_margin=1.0,
                evidence_level=verify_level,
                direct_diff=True,
                direct_test=("[VERIFIED]" in verdict),
                direct_caller=False,
                lsp_only=False,
                presubmit=True,
                target=(changed[0] if changed else ""),
                next_action=("gt_check %s" % changed[0]) if changed else "gt_check",
            )
            verify_fingerprint = steering_hint_fingerprint({
                "hook": "verify",
                "files": changed[:2],
                "verdict": verdict[:200],
                "decision_tier": verify_decision.tier,
                "decision_reason": verify_decision.reason,
            })
            if verify_decision.tier == 0:
                log_event("verify_suppressed", presubmit=True,
                          reason=verify_decision.reason, files=(changed or [])[:3])
            elif _hint_is_suppressed("verify", verify_fingerprint):
                log_event("verify_suppressed", presubmit=True,
                          reason="ack_ignored_repeat", files=(changed or [])[:3])
            else:
                increment_tool_count("gt_check")
                if state.get("gt_evidence", "").startswith("GT MICRO"):
                    combined = state["gt_evidence"] + "\n" + verdict
                    state["gt_evidence"] = truncate(combined, 600, 5)
                else:
                    state["gt_evidence"] = truncate(verdict, 600, 5)
                # Arm structural ack on verify-only emits (skip if micro already armed)
                verify_tier = "verified" if "[VERIFIED]" in verdict else "likely"
                if not GT_ACK_STATE.exists():
                    _arm_ack(_step, channel="verify", tier=verify_tier,
                             focus_file=(changed[0] if changed else ""),
                             focus_symbol="",
                             pre_action=state.get("action", ""),
                             pre_changed=changed,
                             hint_fingerprint=verify_fingerprint,
                             hint_shape="verify")
                log_event("verify_emitted", chars=len(verdict),
                          tier=verify_tier,
                          decision_tier=verify_decision.tier,
                          decision_reason=verify_decision.reason,
                          budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"],
                          hint_fingerprint=verify_fingerprint)
        else:
            log_event("verify_suppressed", reason="no_verdict",
                      budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"])

    save_micro_state(ms)
    _write_state(state)

    # Final diagnostic: confirm what was delivered
    ev = state.get("gt_evidence", "")
    log_event("cycle_end", delivered=bool(ev), chars=len(ev),
              evidence_preview=ev[:80] if ev else "")


def generate_pre_edit_briefing_safe():
    """Deterministic startup briefing that respects the shared evidence gate."""
    if GT_BRIEFING_DONE.exists():
        return ""
    GT_BRIEFING_DONE.touch()

    issue_text = os.environ.get("PROBLEM_STATEMENT", "")
    if not issue_text:
        for p in ["/tmp/gt_issue.txt", "/tmp/problem_statement.txt"]:
            if os.path.exists(p):
                try:
                    issue_text = open(p).read()[:3000]
                    break
                except Exception:
                    pass

    if not os.path.exists(GT_DB):
        log_event("pre_edit_briefing", status="skipped", reason="no_db")
        return ""

    if not issue_text:
        log_event("pre_edit_briefing", status="fallback_orient", reason="no_issue_text")
        return _fallback_orient()

    try:
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        from gt_intel import compute_localization, format_localization_briefing

        conn = sqlite3.connect(GT_DB, timeout=5)
        loc = compute_localization(conn, issue_text, root=".")
        if not loc.candidates:
            log_event("pre_edit_briefing", status="no_candidates_silent")
            conn.close()
            return ""

        top = loc.candidates[0]
        next_conf = loc.candidates[1].confidence if len(loc.candidates) > 1 else 0.0
        gap = top.confidence - next_conf
        unique = len(loc.candidates) == 1 or gap >= CONFIDENCE_AMBIGUITY_MARGIN
        staleness = check_staleness(GT_DB, top.node.file_path, ".")
        caller_count = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
                (top.node.id,),
            ).fetchone()
            caller_count = int(row[0] if row else 0)
        except Exception:
            caller_count = 0
        evidence_level = 1
        if top.tier == "likely":
            evidence_level = 2
        elif top.tier == "verified":
            evidence_level = 3
        if caller_count > 0:
            evidence_level += 1
        next_action = (
            f"gt_impact {top.node.name}"
            if is_critical_path(top.node.file_path)
            else f"gt_lookup {top.node.name}" if top.node.name else f"gt_check {top.node.file_path}"
        )
        decision = classify_steering_decision(
            stage="briefing",
            confidence=top.confidence,
            unique=unique,
            fresh=staleness is None,
            is_test=bool(getattr(top.node, "is_test", False)),
            ambiguity_margin=gap,
            evidence_level=evidence_level,
            direct_diff=False,
            direct_test=bool(getattr(top.node, "is_test", False)),
            direct_caller=caller_count > 0,
            lsp_only=False,
            presubmit=False,
            target=top.node.file_path,
            next_action=next_action,
        )
        fingerprint = steering_hint_fingerprint({
            "hook": "briefing",
            "file": top.node.file_path,
            "symbol": top.node.name,
            "confidence": round(top.confidence, 4),
            "gap": round(gap, 4),
            "tier": top.tier,
            "decision_tier": decision.tier,
            "issue_identifiers": loc.issue_identifiers[:3],
        })
        if decision.tier == 0:
            log_event("pre_edit_briefing", status="policy_silent",
                      reason=decision.reason, tier=top.tier,
                      confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            conn.close()
            return ""
        if _hint_is_suppressed("briefing", fingerprint):
            log_event("pre_edit_briefing", status="suppressed_repeat",
                      reason="ack_ignored_repeat", tier=top.tier,
                      confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            conn.close()
            return ""

        out = format_localization_briefing(loc, conn, ".")
        conn.close()
        if not out:
            log_event("pre_edit_briefing", status="no_output",
                      reason=decision.reason, tier=top.tier,
                      confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            return ""

        _arm_ack(_read_cycle(), channel="orient", tier=top.tier,
                 focus_file=top.node.file_path, focus_symbol=top.node.name,
                 pre_action="", pre_changed=[],
                 hint_fingerprint=fingerprint, hint_shape="briefing")
        log_event("pre_edit_briefing", status="emitted",
                  tier=top.tier, confidence=round(top.confidence, 2),
                  target=top.node.name, file=top.node.file_path,
                  decision_tier=decision.tier, decision_reason=decision.reason)
        return out

    except Exception as e:
        log_event("pre_edit_briefing", status="error_fallback_orient", detail=str(e)[:100])

    return _fallback_orient()


if __name__ == "__main__":
    main()
