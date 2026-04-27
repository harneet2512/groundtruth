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
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
STATE_PATH = Path("/root/state.json")
GT_DB = "/tmp/gt_graph.db"
GT_INTEL = "/tmp/gt_intel.py"  # wrapper (budget-enforced)
GT_INTEL_REAL = "/tmp/gt_intel_real.py"  # real gt_intel.py (bypass wrapper for --findings-json)
GT_INDEX = "/tmp/gt-index"
REPO_ROOT = "/testbed"
GT_HASHES = Path("/tmp/gt_file_hashes.json")
GT_TELEMETRY = Path("/tmp/gt_hook_telemetry.jsonl")
GT_CHECKPOINT_STARTUP = Path("/tmp/gt_checkpoint_startup")
GT_BRIEFING_DONE = GT_CHECKPOINT_STARTUP
GT_TOOL_COUNTS = Path("/tmp/gt_tool_counts.json")
GT_TOOL_COUNTS_INTERNAL = Path("/tmp/gt_tool_counts_internal.json")
GT_MICRO_STATE = Path("/tmp/gt_micro_state.json")
GT_DIFF_HASH = Path("/tmp/gt_last_diff_hash")
GT_ACK_STATE = Path("/tmp/gt_ack_state.json")
GT_POLICY_STATE = Path("/tmp/gt_policy_state.json")
GT_HINT_SUPPRESSION = Path("/tmp/gt_hint_suppression.json")
GT_BUDGET_EVENTS = Path("/tmp/gt_budget_events.jsonl")
GT_BUDGET_EVENTS_OFFSET = Path("/tmp/gt_budget_events.offset")  # v12 watermark
GT_PER_TASK_SUMMARY = Path("/tmp/gt_per_task_summary.json")
GT_LAST_ACTION = Path("/tmp/gt_last_action.txt")
GT_ACK_CALLS = Path("/tmp/gt_ack_calls.jsonl")  # v13 typed-ack append-only log
GT_ACK_CALLS_OFFSET = Path("/tmp/gt_ack_calls.offset")  # v13 typed-ack watermark
GT_LAST_MATERIAL_EDIT_TS = Path("/tmp/gt_last_material_edit.ts")  # v12 submit-gate signal
GT_LAST_GT_CHECK_TS = Path("/tmp/gt_last_gt_check.ts")
GT_DB_READY = Path("/tmp/gt_graph.db.ready")
GT_NO_EDIT_NUDGE_STATE = Path("/tmp/gt_no_edit_nudge_state.json")

# ── vNext Decision Interface ──────────────────────────────────────────────
# Set GT_VNEXT=1 to enable Finding-based structured output on all 3 surfaces.
GT_VNEXT_ENABLED = os.environ.get("GT_VNEXT", "0") == "1"
GT_VNEXT_NOVELTY = Path("/tmp/gt_vnext_novelty.json")  # fingerprint set
GT_VNEXT_META = Path("/tmp/gt_vnext_meta.json")  # per-task metadata


def _vnext_novelty_set() -> set:
    try:
        return set(json.loads(GT_VNEXT_NOVELTY.read_text()))
    except Exception:
        return set()


def _vnext_save_novelty(seen: set) -> None:
    GT_VNEXT_NOVELTY.write_text(json.dumps(list(seen)))


def _vnext_fingerprint(f: dict) -> str:
    loc = f.get("location", {})
    return f"{f.get('kind','')}|{loc.get('file','')}|{loc.get('line','')}|{loc.get('symbol','')}"


def _vnext_filter_novel(findings: list) -> tuple[list, int]:
    """Filter findings through novelty set. Returns (novel, suppressed_count)."""
    seen = _vnext_novelty_set()
    novel = []
    suppressed = 0
    for f in findings:
        fp = _vnext_fingerprint(f)
        if fp in seen:
            suppressed += 1
        else:
            seen.add(fp)
            novel.append(f)
    _vnext_save_novelty(seen)
    return novel, suppressed


def _vnext_format_findings(findings: list, surface: str, include_binding: bool = False) -> str:
    """Format Finding dicts as surface-tagged text."""
    if not findings:
        return ""
    lines = [f'<gt-evidence surface="{surface}">']
    fix_count = 0
    for f in findings:
        tier = f.get("tier", "INFO")
        kind = f.get("kind", "")
        msg = f.get("message", "")
        loc = f.get("location", {})
        loc_s = f"{loc.get('file','')}:{loc.get('line','')}" if loc.get("line") else loc.get("file", "")
        conf = f.get("confidence", 0)
        action = f.get("agent_action", "verify").upper().replace("_", " ")
        lines.append(f"[{tier}] [{kind}] {msg} @ {loc_s} ({conf:.2f}) — {action}")
        if conf >= 0.85:
            fix_count += 1
    if include_binding and fix_count > 0:
        lines.append("---")
        lines.append(f"BINDING: {fix_count} finding(s) require explicit fix or ACK before submit.")
    lines.append("</gt-evidence>")
    return "\n".join(lines)


def _vnext_run_findings(fpath: str) -> list:
    """Run gt_intel.py --findings-json on a file. Returns list of Finding dicts."""
    real = GT_INTEL_REAL if os.path.exists(GT_INTEL_REAL) else GT_INTEL
    try:
        env = os.environ.copy()
        env["GT_FRESHNESS_STRICT"] = os.environ.get("GT_FRESHNESS_STRICT", "1")
        result = subprocess.run(
            ["python3", real, f"--db={GT_DB}", f"--file={fpath}",
             f"--root={REPO_ROOT}", "--findings-json", "--surface=event_brief"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=15, cwd=REPO_ROOT, env=env,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if err:
            log_event("vnext_findings_stderr", file=fpath, stderr=err[:200])
        if out and out.startswith("["):
            return json.loads(out)
        elif out:
            log_event("vnext_findings_non_json", file=fpath, stdout=out[:200])
    except Exception as e:
        log_event("vnext_findings_error", file=fpath, error=str(e)[:200])
    return []


def _vnext_run_briefing_findings(issue_text: str) -> list:
    """Run gt_intel.py --enhanced-briefing --findings-json. Returns Finding dicts."""
    real = GT_INTEL_REAL if os.path.exists(GT_INTEL_REAL) else GT_INTEL
    log_event("vnext_briefing_subprocess", real=real, exists=os.path.exists(real),
              db_exists=os.path.exists(GT_DB))
    try:
        issue_path = "/tmp/gt_vnext_issue.txt"
        with open(issue_path, "w") as f:
            f.write(issue_text[:5000])
        env = os.environ.copy()
        cmd = ["python3", real, f"--db={GT_DB}", "--enhanced-briefing",
               f"--issue-text=@{issue_path}", f"--root={REPO_ROOT}",
               "--findings-json", "--surface=task_map"]
        _vnext_update_meta(task_map_cmd=" ".join(cmd), task_map_cwd=REPO_ROOT)
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=45, cwd=REPO_ROOT, env=env,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        _vnext_update_meta(
            task_map_returncode=result.returncode,
            task_map_stdout_len=len(out),
            task_map_stderr_len=len(err),
        )
        if err:
            log_event("vnext_briefing_stderr", stderr=err[:300])
            _vnext_update_meta(task_map_stderr=err[:500])
        if out and out.startswith("["):
            return json.loads(out)
        elif out:
            log_event("vnext_briefing_non_json", stdout=out[:300])
            _vnext_update_meta(task_map_stdout_snippet=out[:300])
        else:
            log_event("vnext_briefing_empty_stdout", returncode=result.returncode)
            if not err:
                _vnext_update_meta(task_map_debug="empty_stdout_no_stderr")
    except Exception as e:
        import traceback as _tb
        log_event("vnext_briefing_error", error=str(e)[:200],
                  traceback=_tb.format_exc()[:500])
        _vnext_update_meta(
            task_map_error=str(e)[:300],
            task_map_traceback=_tb.format_exc()[:500],
        )
    return []


def _vnext_update_meta(**kw):
    """Update vNext per-task metadata."""
    try:
        meta = json.loads(GT_VNEXT_META.read_text()) if GT_VNEXT_META.exists() else {}
    except Exception:
        meta = {}
    meta.update(kw)
    GT_VNEXT_META.write_text(json.dumps(meta))


def _vnext_meta_get(key, default=0):
    """Read a single vNext metadata field."""
    try:
        meta = json.loads(GT_VNEXT_META.read_text()) if GT_VNEXT_META.exists() else {}
        return meta.get(key, default)
    except Exception:
        return default


def _open_gt_db(timeout: int = 15) -> "sqlite3.Connection":
    """Open GT_DB with WAL + busy_timeout so readers don't block on writers.

    Must stay within SWE-agent's 25s state-hook budget — no sentinel blocking
    here. install.sh's gt_wait_index 300 already waits for the indexer before
    the agent starts; if it times out, WAL mode lets us still read a partial
    graph concurrently with ongoing indexer writes.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(GT_DB, timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={timeout * 1000}")
    except Exception:
        pass
    return conn


# ── Config ─────────────────────────────────────────────────────────────────
MAX_VERIFY_PER_TASK = 8
MICRO_MAX_CHARS = 300
MICRO_MAX_LINES = 3
NEXT_WINDOW_SIZE = 4  # ε3: widened from 2 so typed-ack + behavioral fallback can both resolve even when the agent emits its echo one cycle late. With ε1 precedence in place the armed ack_id is no longer stomped mid-cycle, so a longer window stops being a false-positive factory.
# ε1: channel precedence for ack arming. Higher rank wins same-cycle ties.
# Prevents material_edit from silently stomping a micro/verify ack_id the
# agent may already be echoing (the observed ack_followed=0 cause).
_CHANNEL_RANK = {
    "briefing": 4,
    "verify": 3,
    "micro": 2,
    "material_edit": 1,
}
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
    from gt_intel_real import (
        CONFIDENCE_AMBIGUITY_MARGIN,
        CONFIDENCE_ADVISORY_FLOOR,
        CONFIDENCE_BLOCKING_FLOOR,
        classify_confidence_policy,
        classify_steering_decision,
        steering_hint_fingerprint,
    )
except BaseException:
    CONFIDENCE_AMBIGUITY_MARGIN = 0.12
    CONFIDENCE_ADVISORY_FLOOR = 0.60
    CONFIDENCE_BLOCKING_FLOOR = 0.80
    CONFIDENCE_SILENT_FLOOR = CONFIDENCE_ADVISORY_FLOOR

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
        target=None,
        next_action=None,
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
    """Agent-visible tool counts only. Hook-internal calls are tracked
    separately in GT_TOOL_COUNTS_INTERNAL and do not consume agent budget."""
    if GT_TOOL_COUNTS.exists():
        try:
            return json.loads(GT_TOOL_COUNTS.read_text())
        except Exception:
            pass
    return {}


def get_internal_tool_counts():
    """Hook-internal tool counts (startup briefing, passive evidence, etc.).
    Recorded for telemetry but never gated against agent-visible limits."""
    if GT_TOOL_COUNTS_INTERNAL.exists():
        try:
            return json.loads(GT_TOOL_COUNTS_INTERNAL.read_text())
        except Exception:
            pass
    return {}


def increment_tool_count(tool):
    """Increment agent-visible tool count. Used when the AGENT explicitly
    calls a gt_* tool."""
    counts = get_tool_counts()
    counts[tool] = counts.get(tool, 0) + 1
    try:
        GT_TOOL_COUNTS.write_text(json.dumps(counts))
    except Exception:
        pass


def increment_internal_tool_count(tool):
    """Increment hook-internal tool count. Used for automatic startup
    briefing, passive evidence injection, and other hook-initiated calls.
    Does NOT consume agent budget."""
    counts = get_internal_tool_counts()
    counts[tool] = counts.get(tool, 0) + 1
    try:
        GT_TOOL_COUNTS_INTERNAL.write_text(json.dumps(counts))
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


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _load_no_edit_nudge_state():
    if not GT_NO_EDIT_NUDGE_STATE.exists():
        return {}
    try:
        return json.loads(GT_NO_EDIT_NUDGE_STATE.read_text())
    except Exception:
        return {}


def _write_no_edit_nudge_state(data):
    try:
        GT_NO_EDIT_NUDGE_STATE.write_text(json.dumps(data))
    except Exception:
        pass


def _count_telemetry_events(limit=500):
    counts = {}
    try:
        if GT_TELEMETRY.exists():
            for line in GT_TELEMETRY.read_text().splitlines()[-limit:]:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line).get("event")
                except Exception:
                    event = None
                if event:
                    counts[event] = counts.get(event, 0) + 1
    except Exception:
        pass
    return counts


def _maybe_emit_no_edit_liveness_nudge(state, cycle):
    """Break clean read-only loops without changing edit detection or ack logic."""
    if os.environ.get("GT_NO_EDIT_NUDGE", "1").lower() in {"0", "false", "no", "off"}:
        return False
    counts = _count_telemetry_events()
    if counts.get("material_edit", 0) > 0:
        return False

    first_cycle = _env_int("GT_NO_EDIT_NUDGE_FIRST_CYCLE", 12)
    every = max(1, _env_int("GT_NO_EDIT_NUDGE_EVERY", 8))
    max_nudges = max(1, _env_int("GT_NO_EDIT_NUDGE_MAX", 3))
    if cycle < first_cycle:
        return False

    nudge_state = _load_no_edit_nudge_state()
    sent = int(nudge_state.get("sent", 0) or 0)
    last_cycle = int(nudge_state.get("last_cycle", 0) or 0)
    if sent >= max_nudges:
        return False
    if last_cycle and cycle - last_cycle < every:
        return False

    text = (
        "[GT LIVE NUDGE] No source edit has been detected after "
        f"{cycle} actions. You have enough context to try the smallest "
        "/testbed source edit now. Next action should modify a non-test "
        "/testbed source file in one shell action. If you use Python, use "
        "python - <<'PY' so the edit code and execution are in the same "
        "fenced block; do not put `cat > /tmp/script.py` in one block and "
        "`python /tmp/script.py` in another. On the following action, run "
        "git -C /testbed diff -- <edited-file>. Do not keep repeating "
        "grep/sed/cat unless you can name the exact blocker."
    )
    state["gt_evidence"] = truncate(text, 600, 5)
    _write_no_edit_nudge_state({"sent": sent + 1, "last_cycle": cycle})
    log_event("no_edit_liveness_nudge",
              cycle=cycle,
              sent=sent + 1,
              first_cycle=first_cycle,
              every=every,
              max_nudges=max_nudges)
    return True


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


GT_SUBMIT_MARKER = Path("/tmp/gt_submit_detected")


def _is_presubmit(state):
    action = str(state.get("action", "")).lower()
    output = str(state.get("last_output", "")).lower()
    # SWE-agent does not write action/last_output to state.json.
    # Check multiple signals:
    # 1. state fields (may be empty in SWE-agent)
    # 2. GT_LAST_ACTION file (gt_* tool wrappers)
    # 3. /root/model.patch existence (submit tool creates it)
    # 4. /tmp/gt_submit_detected marker
    last_action_file = ""
    if GT_LAST_ACTION.exists():
        try:
            last_action_file = GT_LAST_ACTION.read_text().strip().lower()
        except Exception:
            pass
    combined = action + " " + output + " " + last_action_file
    if any(s.lower() in combined for s in _SUBMIT_SIGNALS):
        return True
    # Detect submit via model.patch creation (submit tool writes it)
    model_patch = Path("/root/model.patch")
    if model_patch.exists() and not GT_SUBMIT_MARKER.exists():
        GT_SUBMIT_MARKER.touch()
        return True
    return False


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
    _trace = os.environ.get("GT_TRACE_HASH_SEED") == "1"
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
        elif _trace:
            log_event("hash_trace_git_empty",
                      arm_env=os.environ.get("GT_LSP_ENABLED", ""),
                      cwd=REPO_ROOT)
    except Exception as e:
        log_event("git_diff_error", detail=str(e)[:100])
        return []

    _hash_file_exists = GT_HASHES.exists()
    hashes = _load_json(GT_HASHES)
    _hash_count_before = len(hashes) if isinstance(hashes, dict) else 0
    changed = []
    _filtered_ext = 0
    _filtered_test = 0
    _filtered_hash_none = 0
    _matched_stored = 0

    for fpath in diff_files:
        ext = os.path.splitext(fpath)[1]
        if ext not in SOURCE_EXTS:
            _filtered_ext += 1
            continue
        base = os.path.basename(fpath)
        if base.startswith("test_") or base.startswith("reproduce"):
            _filtered_test += 1
            continue
        abs_path = os.path.join(REPO_ROOT, fpath)
        h = file_hash(abs_path)
        if h is None:
            _filtered_hash_none += 1
            continue
        if h != hashes.get(fpath):
            hashes[fpath] = h
            changed.append(fpath)
        else:
            _matched_stored += 1

    _save_json(GT_HASHES, hashes)

    if _trace:
        log_event("hash_trace_detect",
                  arm_env=os.environ.get("GT_LSP_ENABLED", ""),
                  diff_files=len(diff_files),
                  hash_file_existed=_hash_file_exists,
                  hash_count_before=_hash_count_before,
                  hash_count_after=len(hashes) if isinstance(hashes, dict) else 0,
                  changed=len(changed),
                  filtered_ext=_filtered_ext,
                  filtered_test=_filtered_test,
                  filtered_hash_none=_filtered_hash_none,
                  matched_stored=_matched_stored)

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
        conn = _open_gt_db(timeout=15)
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
        conn = _open_gt_db(timeout=15)
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
# v13e tool_signature_read: bash utilities the agent uses to verify that a
# just-landed edit is on-disk. When one of these reads the armed focus file
# inside the ack window, count it as follow-through. Rationale: SWE-agent's
# thought-action scaffold does not expose a dedicated "verify" verb; the
# canonical post-edit check IS `sed -n A,Bp <file>` / `grep -n <pat> <file>`.
# Not counting it forces every window to expire ack_not_observed regardless
# of whether the agent actually verified.
_READ_VERIFY_TOOLS = ("sed", "grep", "awk", "cat", "head", "tail",
                      "less", "nl", "view", "pr")
_READ_VERIFY_RE = re.compile(r"\b(" + "|".join(_READ_VERIFY_TOOLS) + r")\b")


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
        # ε1: channel precedence. A same-cycle ack from a higher-or-equal
        # precedence channel is kept; the new, lower-precedence arm is
        # rejected. Cross-cycle arms always replace (prior arm is stale).
        _existing_ack = _load_ack()
        if _existing_ack is not None:
            _ex_cycle = int(_existing_ack.get("cycle", -1))
            _ex_channel = str(_existing_ack.get("channel", "") or "")
            if _ex_cycle == int(cycle) and _ex_channel:
                _ex_rank = _CHANNEL_RANK.get(_ex_channel, 0)
                _new_rank = _CHANNEL_RANK.get(channel, 0)
                if _ex_rank >= _new_rank:
                    log_event("ack_arm_suppressed_by_precedence",
                              cycle=int(cycle),
                              attempted_channel=channel,
                              attempted_tier=tier,
                              attempted_file=focus_file or "",
                              attempted_symbol=focus_symbol or "",
                              kept_channel=_ex_channel,
                              kept_tier=_existing_ack.get("tier"),
                              kept_ack_id=_existing_ack.get("ack_id"))
                    return None
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
            return None
        intervention_id = hashlib.sha256(
            f"{cycle}|{channel}|{tier}|{focus_file or ''}|{focus_symbol or ''}".encode("utf-8")
        ).hexdigest()[:12]
        # v13 typed-ack: 8-char random id the agent must echo back via gt_ack.
        ack_id = secrets.token_hex(4)
        expected_next_action_text = _expected_next_action_text(expected_next_action)
        _write_policy({
            "intervention_id": intervention_id,
            "ack_id": ack_id,
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
            "arm_ts_epoch": int(time.time()),  # D1 fix: for scanning budget-events window
            "channel": channel,
            "tier": tier,
            "intervention_id": intervention_id,
            "ack_id": ack_id,
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
        log_event("ack_armed",
                  ack_id=ack_id,
                  channel=channel,
                  tier=tier,
                  expected_next_action_text=expected_next_action_text,
                  expected_next_action_kind=expected_next_action.get("kind"),
                  file=focus_file or "",
                  symbol=focus_symbol or "",
                  expires_at_cycle=int(cycle) + NEXT_WINDOW_SIZE)
        # Delivery instrumentation (Step 3): payload digest snapshot at
        # arm time. material_edit is a silent behavioral arm (no payload
        # shipped to the agent); all others carry advisory text via
        # state["gt_evidence"] and will be matched with steer_delivered
        # or cycle_end at emit/end of cycle.
        _has_payload = channel != "material_edit"
        _payload_src = expected_next_action_text or hint_shape or channel or ""
        _payload_digest = hashlib.sha1(
            _payload_src.encode("utf-8", errors="replace")
        ).hexdigest()[:8] if _payload_src else ""
        log_event("steer_armed",
                  ack_id=ack_id,
                  channel=channel,
                  insertion_path=channel,
                  has_payload=_has_payload,
                  payload_digest=_payload_digest,
                  payload_len=len(_payload_src),
                  file=focus_file or "",
                  symbol=focus_symbol or "",
                  cycle=int(cycle))
        return ack_id
    except Exception as e:
        log_event("ack_arm_error", detail=str(e)[:120])
        return None


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
    if kind == "verify_edit":
        return f"verify {target}".strip()
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
    if channel == "material_edit":
        # Concrete gt_check target: lets the typed-ack classifier close via
        # expected_next_action_match when the model runs gt_check <file>.
        if focus_file:
            return {
                "kind": "gt_check",
                "target": focus_file,
                "text": f"gt_check {focus_file}",
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


def _scan_gt_actions_since(arm_ts_epoch, expected_kind=None, expected_target=None):
    """D1 fix (2026-04-20): scan the append-only budget-events log for gt_action
    entries written after arm_ts_epoch.

    Returns the most recent gt_action payload ("gt_check /path/file") whose cmd
    matches expected_kind (stripped of the "gt_" prefix) and whose arg contains
    expected_target (substring or basename match). Returns None if no match.

    This is race-free by construction: every gt_* wrapper call appends one line
    here before doing any other work, so _check_ack can discover actions even
    when the single-file LAST_ACTION is stale, unlinked, or read before the
    current turn's wrapper writes it.
    """
    if not GT_BUDGET_EVENTS.exists():
        return None
    exp_kind_bare = (expected_kind or "").strip()
    if exp_kind_bare.startswith("gt_"):
        exp_kind_bare = exp_kind_bare[3:]
    exp_target = (expected_target or "").strip()
    target_base = exp_target.split("/")[-1] if exp_target else ""
    match = None
    try:
        with open(GT_BUDGET_EVENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") != "gt_action":
                    continue
                ts = rec.get("ts")
                try:
                    ts = int(ts) if ts is not None else 0
                except (TypeError, ValueError):
                    ts = 0
                if arm_ts_epoch and ts < int(arm_ts_epoch):
                    continue
                if exp_kind_bare and rec.get("cmd") != exp_kind_bare:
                    continue
                if exp_target:
                    arg = str(rec.get("arg") or "")
                    if not (exp_target in arg or (target_base and arg.endswith(target_base))):
                        continue
                match = rec.get("payload") or (f"gt_{rec.get('cmd','')} {rec.get('arg','')}".strip())
    except Exception:
        return None
    return match


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


def _ack_hint_line(ack_id, expected_text):
    """v13 typed-ack advisory appended to every armed emission.

    Tells the model exactly what single tool call confirms the window. This is
    the structural round-trip: without it, the classifier is back to free-text
    entailment and resolves everything as ack_not_observed.
    """
    if not ack_id:
        return ""
    target = (expected_text or "").strip()
    if target:
        return (
            f"\n[GT-ACK] Expected next: {target}. "
            f"Immediately after, call: gt_ack --id {ack_id} --note \"<1-line>\""
        )
    return (
        f"\n[GT-ACK] After your next action, call: "
        f"gt_ack --id {ack_id} --note \"<1-line>\""
    )


def _read_new_ack_calls():
    """v13 typed-ack: read new gt_ack tool calls since the last watermark.

    Returns a list of {ts, id, note} dicts for each newly-appended call. The
    watermark file (GT_ACK_CALLS_OFFSET) stores the last file offset read, so
    subsequent cycles only see new calls. Safe against missing files and
    malformed lines — returns [] on any error.
    """
    if not GT_ACK_CALLS.exists():
        return []
    try:
        offset = 0
        if GT_ACK_CALLS_OFFSET.exists():
            try:
                offset = int(GT_ACK_CALLS_OFFSET.read_text().strip() or "0")
            except Exception:
                offset = 0
        calls = []
        with open(GT_ACK_CALLS, "r") as f:
            f.seek(offset)
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    calls.append(json.loads(s))
                except Exception:
                    continue
            new_offset = f.tell()
        try:
            GT_ACK_CALLS_OFFSET.write_text(str(new_offset))
        except Exception:
            pass
        return calls
    except Exception:
        return []


def _check_ack(cycle, action, changed_files):
    """Compare current cycle's action against armed pre-emit snapshot.
    Emits exactly one of: ack_followed / ack_ignored / ack_not_observed.

    v13 typed-ack primary path: if a gt_ack(id=<armed_id>) tool call arrived
    since the window was armed, emit ack_followed immediately. Falls back to
    the legacy substring/edit-delta classifier when no typed call is present.
    """
    armed = _load_ack()
    if not armed:
        # Drain unmatched gt_ack calls so they do not leak into a later window.
        _read_new_ack_calls()
        return
    arm_cycle = armed.get("cycle", 0)
    if cycle <= arm_cycle:
        return  # same cycle as arming — cannot self-ack

    # v13 typed-ack: match first. The typed call is the cleanest signal.
    armed_id = (armed.get("ack_id") or "").strip()
    new_calls = _read_new_ack_calls()
    matched_call = None
    stale_ids = []
    for c in new_calls:
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        if armed_id and cid == armed_id:
            matched_call = c
            break
        stale_ids.append(cid)
    for sid in stale_ids:
        log_event("ack_stale_id", armed_id=armed_id, observed_id=sid,
                  channel=armed.get("channel"),
                  hint_shape=armed.get("hint_shape"))
    if matched_call:
        log_event("ack_followed",
                  source="typed_ack",
                  ack_id=armed_id,
                  note=str(matched_call.get("note") or "")[:200],
                  channel=armed.get("channel"),
                  hint_shape=armed.get("hint_shape"),
                  hint_fingerprint=armed.get("hint_fingerprint"),
                  expected_next_action_text=armed.get("expected_next_action_text"))
        log_event("ack_engagement",
                  classification="focus_navigation",
                  source="typed_ack",
                  ack_id=armed_id,
                  channel=armed.get("channel"),
                  file=armed.get("file", "") or "")
        _clear_ack()
        return

    expires_at = armed.get("expires_at_cycle", arm_cycle + NEXT_WINDOW_SIZE)

    # v12: prefer the wrapper-written ground truth action over the scaffold's
    # empty/noisy state.action. Root cause of v11 ack_followed=0: action was
    # always "" in the bash thought-action scaffold, so every symbol-match
    # check failed and every window expired as ack_not_observed.
    #
    # D1 fix (2026-04-20): the single-file LAST_ACTION is racy — the hook
    # unlinks it at line 2348 on every invocation, so any wrapper write that
    # happens later in the same cycle is lost on the next read. Primary
    # observation is now a window-scoped scan of the append-only
    # gt_budget_events.jsonl log. Previous sources retained as fallbacks.
    ack_source = "state_action"
    _gt_action = None
    _arm_ts = armed.get("arm_ts_epoch")
    _exp_kind = (armed.get("expected_next_action_kind") or "").strip()
    _exp_target = (armed.get("expected_next_action_target") or "").strip()
    if _arm_ts:
        _gt_action = _scan_gt_actions_since(_arm_ts, _exp_kind, _exp_target)
        if _gt_action:
            ack_source = "budget_events_scan"
    if not _gt_action:
        _gt_action = _read_last_action()
        if _gt_action:
            ack_source = "gt_last_action"
    if not _gt_action:
        _gt_action = _read_last_submit_observation()
        if _gt_action:
            ack_source = "submit_observed"
    if _gt_action:
        action = _gt_action

    # v13b tool-signature resolver: resolve the window when a tool call matching
    # the armed expected_next_action (kind + target) arrives, even if the agent
    # never issued the gt_ack wrapper. Empirically models follow the expected
    # action but drop the secondary confirmation call (see 2026-04-19 smoke).
    # This path runs before the legacy set-membership checks so the emitted
    # source is explicit and the classifier terminates on the strongest signal.
    exp_kind = (armed.get("expected_next_action_kind") or "").strip()
    exp_target = (armed.get("expected_next_action_target") or "").strip()
    raw_action_str = str(action or "")
    if exp_kind and raw_action_str:
        tokens = raw_action_str.replace("\n", " ").split()
        head_tok = tokens[0].split("/")[-1] if tokens else ""
        if head_tok == exp_kind:
            target_hit = True
            if exp_target:
                target_base = exp_target.split("/")[-1]
                target_hit = any(
                    (exp_target in tok) or tok.endswith(target_base)
                    for tok in tokens[1:]
                )
            if target_hit:
                log_event(
                    "ack_followed",
                    source="tool_signature",
                    reason="expected_next_action_match",
                    ack_id=armed_id,
                    expected_kind=exp_kind,
                    expected_target=exp_target,
                    channel=armed.get("channel"),
                    tier=armed.get("tier"),
                    hint_shape=armed.get("hint_shape"),
                    hint_fingerprint=armed.get("hint_fingerprint"),
                    cycle=cycle,
                    arm_cycle=arm_cycle,
                    intervention_id=armed.get("intervention_id"),
                    observation_source=ack_source,
                )
                log_event("ack_engagement",
                          classification="focus_navigation",
                          source="tool_signature",
                          ack_id=armed_id,
                          channel=armed.get("channel"),
                          file=armed.get("file", "") or "")
                _clear_ack()
                return

    # v13e tool_signature_read: count bash read-verification on the armed
    # focus_file as follow-through. Agent idiom after an edit is
    # `sed -n A,Bp /path/file` or `grep -n pat /path/file` — it never calls
    # gt_check explicitly. Resolving this branch is the structural fix for
    # the 0/450 ack_followed regime observed on 2026-04-18.
    exp_target_base = exp_target.split("/")[-1] if exp_target else ""
    focus_file_pre = armed.get("file", "") or ""
    focus_file_base = focus_file_pre.split("/")[-1] if focus_file_pre else ""
    search_bases = {b for b in (exp_target_base, focus_file_base) if b}
    if raw_action_str and search_bases:
        cmd_text = raw_action_str
        if cmd_text.startswith("bash "):
            _, _, _rest = cmd_text.partition("-c")
            if _rest.strip():
                cmd_text = _rest
        m = _READ_VERIFY_RE.search(cmd_text)
        if m and any(b in cmd_text for b in search_bases):
            log_event(
                "ack_followed",
                source="tool_signature_read",
                reason="verify_by_read",
                read_cmd=m.group(1),
                ack_id=armed_id,
                expected_kind=exp_kind,
                expected_target=exp_target,
                channel=armed.get("channel"),
                tier=armed.get("tier"),
                hint_shape=armed.get("hint_shape"),
                hint_fingerprint=armed.get("hint_fingerprint"),
                cycle=cycle,
                arm_cycle=arm_cycle,
                intervention_id=armed.get("intervention_id"),
                observation_source=ack_source,
                file=focus_file_pre,
            )
            log_event("ack_engagement",
                      classification="tool_signature_read",
                      source="tool_signature_read",
                      ack_id=armed_id,
                      channel=armed.get("channel"),
                      read_cmd=m.group(1),
                      file=focus_file_pre)
            _clear_ack()
            return

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
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="blocked_submit",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
            log_event("ack_engagement",
                      classification="focus_navigation",
                      source="targeted_submit",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
        else:
            log_event("ack_ignored", reason="non_targeted_submit",
                      channel=armed.get("channel"), tier=armed.get("tier"),
                      file=focus_file, symbol=focus_symbol, cycle=cycle,
                      arm_cycle=arm_cycle,
                      intervention_id=armed.get("intervention_id"),
                      observation_source=ack_source)
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="non_targeted_submit",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
        log_event("ack_engagement",
                  classification="focus_navigation",
                  source="targeted_lookup",
                  ack_id=armed.get("ack_id"),
                  channel=armed.get("channel"),
                  symbol=focus_symbol,
                  file=focus_file)
        _clear_ack()
        return

    # File-level targeted gt_check / gt_symbols
    if file_match and action_head in _GT_TOOLS_FILE_CMDS:
        log_event("ack_followed", reason="targeted_check",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        log_event("ack_engagement",
                  classification="focus_navigation",
                  source="targeted_check",
                  ack_id=armed.get("ack_id"),
                  channel=armed.get("channel"),
                  file=focus_file)
        _clear_ack()
        return

    # File-level edit/read on focus file
    if file_match and action_head in _EDIT_TOOLS:
        log_event("ack_followed", reason="targeted_edit_or_read",
                  channel=armed.get("channel"), tier=armed.get("tier"),
                  file=focus_file, cycle=cycle, arm_cycle=arm_cycle,
                  intervention_id=armed.get("intervention_id"),
                  observation_source=ack_source)
        log_event("ack_engagement",
                  classification="focus_navigation",
                  source="targeted_edit_or_read",
                  ack_id=armed.get("ack_id"),
                  channel=armed.get("channel"),
                  file=focus_file)
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
        log_event("ack_engagement",
                  classification="focus_navigation",
                  source="targeted_edit_inferred",
                  ack_id=armed.get("ack_id"),
                  channel=armed.get("channel"),
                  file=focus_file)
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
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="non_targeted_gt_action",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="non_targeted_edit",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="non_targeted_edit_inferred",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
            log_event("ack_engagement",
                      classification="delivered_no_followup",
                      source="expiry_with_activity",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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
            log_event("ack_engagement",
                      classification="no_visible_engagement",
                      source="expiry_no_activity",
                      ack_id=armed.get("ack_id"),
                      channel=armed.get("channel"),
                      file=focus_file)
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


_REINDEX_BUDGET_S = 5.0
_FRESHNESS_STRICT_DEFAULT = "1"


def _freshness_strict():
    v = os.environ.get("GT_FRESHNESS_STRICT", _FRESHNESS_STRICT_DEFAULT).strip().lower()
    return v not in ("0", "false", "no", "off", "")


def run_verification(changed_files):
    """Run gt_intel --reminder on changed files. Returns compact verdict.

    C+D freshness gate: for each changed file, synchronously reindex with a
    hard budget. If the reindex outcome is not 'fresh', do not call gt_intel
    at all — emit a structured withheld line instead. This closes both
    failure modes of the pre-C+D behavior:
      - silent try/except reindex swallowing errors
      - hook stripping [STALE] tags and presenting stale evidence as fresh

    Deduplicates: suppresses if identical to last verify output.
    """
    global _LAST_VERIFY_HASH
    outputs = []
    for fpath in changed_files[:2]:
        rx = _reindex_verify(fpath, budget_s=_REINDEX_BUDGET_S)
        log_event("reindex_result",
                  file=fpath,
                  outcome=rx["outcome"],
                  elapsed_ms=rx["elapsed_ms"],
                  db_mtime_before=rx["db_mtime_before"],
                  db_mtime_after=rx["db_mtime_after"],
                  file_mtime=rx["file_mtime"])
        if rx["outcome"] != "fresh":
            if _freshness_strict():
                outputs.append(
                    f"[WITHHELD] gt_check on {fpath} skipped: reindex "
                    f"{rx['outcome']} (budget {_REINDEX_BUDGET_S:.0f}s). "
                    f"Re-run after a successful edit."
                )
                continue
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


def _reindex_verify(fpath, budget_s=_REINDEX_BUDGET_S):
    """Check freshness of graph.db vs fpath; reindex only if stale.

    Short-circuits when graph.db is already newer than the file (no subprocess
    call). When stale, attempts an incremental reindex with a hard budget.
    If the gt-index binary is missing (e.g., VMs that use the Python-indexer
    fallback with no incremental mode), skips reindex and reports outcome
    'stale_no_indexer' — keeps freshness semantics without requiring the
    binary to exist.

    Returns a dict with:
      outcome: 'fresh' | 'stale' | 'stale_no_indexer' | 'timeout' | 'error'
      elapsed_ms: float
      db_mtime_before, db_mtime_after, file_mtime: float (0.0 if missing)
    """
    def _mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    db_before = _mtime(GT_DB)
    src = fpath if os.path.isabs(fpath) else os.path.join(REPO_ROOT, fpath)
    file_mtime = _mtime(src)
    start = time.monotonic()

    # Short-circuit: if graph.db is already at or ahead of the file, we're
    # fresh without needing a subprocess call.
    if db_before > 0 and file_mtime > 0 and db_before >= file_mtime:
        return {
            "outcome": "fresh",
            "elapsed_ms": round((time.monotonic() - start) * 1000.0, 1),
            "db_mtime_before": db_before,
            "db_mtime_after": db_before,
            "file_mtime": file_mtime,
        }

    # File is newer than graph.db. Try to reindex the single file.
    if not os.path.exists(GT_INDEX):
        return {
            "outcome": "stale_no_indexer",
            "elapsed_ms": round((time.monotonic() - start) * 1000.0, 1),
            "db_mtime_before": db_before,
            "db_mtime_after": db_before,
            "file_mtime": file_mtime,
        }

    outcome = "error"
    db_after = db_before
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={fpath}",
             f"--root={REPO_ROOT}", f"--output={GT_DB}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=budget_s, cwd=REPO_ROOT,
        )
        db_after = _mtime(GT_DB)
        if db_after > 0 and db_after >= file_mtime:
            outcome = "fresh"
        else:
            outcome = "stale"
    except subprocess.TimeoutExpired:
        db_after = _mtime(GT_DB)
        outcome = "timeout"
    except Exception:
        db_after = _mtime(GT_DB)
        outcome = "error"
    elapsed_ms = (time.monotonic() - start) * 1000.0
    return {
        "outcome": outcome,
        "elapsed_ms": round(elapsed_ms, 1),
        "db_mtime_before": db_before,
        "db_mtime_after": db_after,
        "file_mtime": file_mtime,
    }


def _run_gt_intel(fpath):
    """Run gt_intel --reminder, return actionable tiers (VERIFIED + WARNING).

    Previously filtered to [VERIFIED] only, which dropped CALLER/TEST/IMPACT/
    PRECEDENT family output (tier [WARNING], 0.5-0.9 confidence) and starved
    the agent of everything except IMPORT evidence. Expanding to [WARNING]
    restores the 7-family signal; [INFO] (<0.5) still suppressed as noise.
    """
    try:
        env = os.environ.copy()
        env["GT_FRESHNESS_STRICT"] = os.environ.get(
            "GT_FRESHNESS_STRICT", _FRESHNESS_STRICT_DEFAULT)
        result = subprocess.run(
            ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={fpath}",
             f"--root={REPO_ROOT}", "--reminder"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=15, cwd=REPO_ROOT, env=env,
        )
        out = result.stdout.strip()
        if not out or len(out) < 10 or "Error" in out[:30]:
            return ""
        actionable_tags = ("[VERIFIED]", "[WARNING]", "[CONTRACT]",
                           "[CRITICAL]", "[WITHHELD]")
        lines = []
        for line in out.split("\n"):
            s = line.strip()
            if "[STALE]" in s:
                # C+D contract: gt_intel should no longer emit [STALE] when
                # GT_FRESHNESS_STRICT is on. If it does, our contract has a
                # leak — log it but drop the line defensively.
                log_event("stale_leak_detected", file=fpath, line=s[:120])
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
        conn = _open_gt_db(timeout=15)
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

    # ── vNext task_map surface ──
    if GT_VNEXT_ENABLED:
        log_event("vnext_task_map_attempt", gt_intel_real=os.path.exists(GT_INTEL_REAL))
        findings = _vnext_run_briefing_findings(issue_text)
        log_event("vnext_task_map_result", findings_count=len(findings))
        if findings:
            novel, suppressed = _vnext_filter_novel(findings)
            text = _vnext_format_findings(novel, "task_map")
            _vnext_update_meta(
                task_map_emitted=True,
                task_map_findings_count=len(novel),
                task_map_suppressed=suppressed,
            )
            log_event("pre_edit_briefing", status="vnext_task_map",
                      findings=len(novel), suppressed=suppressed)
            if text:
                return text
        else:
            _vnext_update_meta(task_map_emitted=False)
            log_event("pre_edit_briefing", status="vnext_task_map_empty")
        # Fall through to legacy briefing if vNext produced nothing

    try:
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        from gt_intel_real import (
            classify_confidence_policy,
            compute_localization,
            format_localization_briefing,
        )

        conn = _open_gt_db(timeout=15)
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
            _ack_id = _arm_ack(_read_cycle(), channel="orient", tier=top.tier,
                     focus_file=top.node.file_path, focus_symbol=top.node.name,
                     pre_action="", pre_changed=[])
            log_event("pre_edit_briefing", status="emitted",
                      tier=top.tier, confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path,
                      ack_id=_ack_id)
            if _ack_id:
                out = out + _ack_hint_line(_ack_id, f"gt_lookup {top.node.name}")
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
        state["gt_arm"] = arm or ""
        state["gt_run_id"] = run_id or ""
        state["gt_instance_id"] = iid or ""
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
        # Lane C fix (2026-04-22): expose ack/steer/engagement counters in the
        # per-step state dump so run.log is a sufficient source of truth when
        # gt_hook_telemetry.jsonl does not reach the host (e.g. container gone
        # before the final scraper sweep, or identity-mismatch skip). Under
        # gt-lsp-hybrid the reporter previously saw 0s for ack_armed because
        # the counter key did not exist in this dict, even though _arm_ack()
        # had fired log_event("ack_armed", ...) into the container-local jsonl.
        state["gt_events"] = json.dumps({
            "material_edit": ev_counts.get("material_edit", 0),
            "micro_emitted": ev_counts.get("micro_emitted", 0),
            "micro_suppressed": ev_counts.get("micro_suppressed", 0),
            "verify_emitted": ev_counts.get("verify_emitted", 0),
            "verify_suppressed": ev_counts.get("verify_suppressed", 0),
            "ack_followed": ev_counts.get("ack_followed", 0),
            "ack_ignored": ev_counts.get("ack_ignored", 0),
            "ack_not_observed": ev_counts.get("ack_not_observed", 0),
            "ack_armed": ev_counts.get("ack_armed", 0),
            "ack_armed_on_edit": ev_counts.get("ack_armed_on_edit", 0),
            "ack_stale_id": ev_counts.get("ack_stale_id", 0),
            "ack_engagement": ev_counts.get("ack_engagement", 0),
            "steer_armed": ev_counts.get("steer_armed", 0),
            "steer_delivered": ev_counts.get("steer_delivered", 0),
            "steer_dropped": ev_counts.get("steer_dropped", 0),
            "budget_denied": ev_counts.get("budget_denied", 0),
            "submit_observed": ev_counts.get("submit_observed", 0),
            "lsp_promotion": ev_counts.get("lsp_promotion", 0),
            "checkpoint_startup": ev_counts.get("checkpoint_startup", 0),
            "identity_missing": ev_counts.get("identity_missing", 0),
        })
        state["gt_policy"] = json.dumps(_load_policy())
        # vNext metadata (survives container removal via state.json → traj)
        if GT_VNEXT_ENABLED:
            try:
                vnext_meta = json.loads(GT_VNEXT_META.read_text()) if GT_VNEXT_META.exists() else {}
            except Exception:
                vnext_meta = {}
            vnext_meta["gt_vnext_enabled"] = True
            # Add vnext telemetry event counts
            for k in ["vnext_task_map_attempt", "vnext_task_map_result",
                       "vnext_briefing_subprocess", "vnext_briefing_stderr",
                       "vnext_briefing_non_json", "vnext_briefing_empty_stdout",
                       "vnext_briefing_error", "vnext_event_brief",
                       "vnext_review_patch", "vnext_findings_stderr",
                       "vnext_findings_non_json", "vnext_findings_error"]:
                vnext_meta[k] = ev_counts.get(k, 0)
            state["gt_vnext"] = json.dumps(vnext_meta)
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


def _compute_stuck_evidence(file_path, max_tokens=200):
    """Compute targeted evidence when agent is stuck on a file.

    Reflexion pattern (Shinn et al., NeurIPS 2023, arXiv:2303.11366): provide
    concrete constraints at the failure point. Self-Refine (Madaan et al.,
    NeurIPS 2023, arXiv:2303.17651): specific > generic feedback.
    GT's call graph provides the reflection signal: callers + assertions.
    """
    if not os.path.exists(GT_DB):
        return ""
    try:
        conn = sqlite3.connect(GT_DB, timeout=10)
        conn.row_factory = sqlite3.Row

        node = conn.execute(
            """SELECT n.id, n.name FROM nodes n
               LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
               WHERE n.file_path LIKE ? AND n.is_test = 0
                 AND n.label IN ('Function', 'Method')
               GROUP BY n.id ORDER BY COUNT(e.id) DESC LIMIT 1""",
            (f"%{file_path}",),
        ).fetchone()

        if not node:
            conn.close()
            return ""

        nid, name = node["id"], node["name"]
        lines = [f"[GT STUCK] {name}() edited 3+ times without gt_check. Key constraints:"]
        chars_used = len(lines[0])
        max_chars = max_tokens * 4

        callers = conn.execute(
            """SELECT n.name, e.source_file, e.source_line
               FROM edges e JOIN nodes n ON n.id = e.source_id
               WHERE e.target_id = ? AND e.type = 'CALLS'
                 AND e.source_file NOT LIKE ?
               LIMIT 3""",
            (nid, f"%{file_path}"),
        ).fetchall()

        for c in callers:
            line = f"  CALLER: {c['name']}() at {c['source_file']}:{c['source_line']}"
            if chars_used + len(line) > max_chars:
                break
            lines.append(line)
            chars_used += len(line)

        try:
            asserts = conn.execute(
                """SELECT a.expression, a.kind, t.name as test_name
                   FROM assertions a
                   JOIN nodes t ON a.test_node_id = t.id
                   WHERE a.target_node_id = ? LIMIT 3""",
                (nid,),
            ).fetchall()
            for a in asserts:
                expr = (a["expression"] or "")[:80]
                line = f"  ASSERT: {a['test_name']}: {expr}"
                if chars_used + len(line) > max_chars:
                    break
                lines.append(line)
                chars_used += len(line)
        except sqlite3.OperationalError:
            pass

        lines.append(f"  Next: gt_check {file_path}")
        conn.close()
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        log_event("stuck_evidence_error", file=file_path, error=str(e)[:100])
        return ""


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
        # Emit arm-config signal so the reporter can tag lsp_enabled even
        # when the task produces zero material edits (the lsp_promotion_*
        # branch below would not fire in that case). Emitted only when
        # enabled so the reporter can use presence as the truthy signal.
        if os.environ.get("GT_LSP_ENABLED") == "1":
            log_event("lsp_config", lsp_enabled=1)
        briefing = generate_pre_edit_briefing_safe()
        # Hook-internal: startup briefing is automatic, not agent-initiated.
        # Must NOT consume agent-visible gt_orient budget. Prior behavior
        # used increment_tool_count which consumed the 1-call cap, causing
        # Qwen3-Coder's first explicit gt_orient to get BUDGET_EXHAUSTED
        # and derailing 6/10 nolsp tasks at cycle 1.
        increment_internal_tool_count("gt_orient")
        log_event("checkpoint_startup",
                  status="emitted" if briefing else "empty")
        if briefing:
            state["gt_evidence"] = briefing
            _write_state(state)
            log_event("startup_complete", status="briefing_emitted")
            return
        GT_CHECKPOINT_STARTUP.touch()
        log_event("startup_complete", status="briefing_empty")

    # ── 2. PRESUBMIT (always, no budget cost) ──────────────────────────
    _presubmit = _is_presubmit(state)
    _model_patch_exists = Path("/root/model.patch").exists()
    if GT_VNEXT_ENABLED:
        _vnext_update_meta(
            presubmit_check=True,
            presubmit_detected=_presubmit,
            model_patch_exists=_model_patch_exists,
            submit_marker_exists=GT_SUBMIT_MARKER.exists(),
        )
    if _presubmit:
        changed = detect_material_edits()

        # ── vNext review_patch surface ──
        if GT_VNEXT_ENABLED and changed:
            all_findings = []
            for fpath in changed[:3]:
                all_findings.extend(_vnext_run_findings(fpath))
            novel, suppressed = _vnext_filter_novel(all_findings)
            text = _vnext_format_findings(novel, "review_patch", include_binding=True)
            high_conf = sum(1 for f in novel if f.get("confidence", 0) >= 0.85)
            _vnext_update_meta(
                review_patch_called_pre_submit=True,
                submit_paused_for_review=bool(novel),
                review_findings_count=len(novel),
                review_high_confidence_count=high_conf,
                duplicate_findings_suppressed=suppressed,
                agent_had_chance_to_respond_to_review_patch=True,
            )
            log_event("vnext_review_patch",
                      findings=len(novel), high_conf=high_conf,
                      suppressed=suppressed, files=changed[:3])
            if text:
                state["gt_evidence"] = text
                _write_state(state)
                increment_tool_count("presubmit")
                _terminal_close_ack(_step, reason="presubmit")
                _emit_per_task_summary(reason="presubmit")
                return
            # Fall through to legacy verify if vNext produced nothing

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
        if _maybe_emit_no_edit_liveness_nudge(state, _step):
            log_event("cycle", status="no_edit_nudged")
            _write_state(state)
            _emit_per_task_summary(reason="periodic_no_edit")
            return
        log_event("cycle", status="no_edit")
        _write_state(state)
        # Emit summary on no-edit cycles too — without this, tasks that never
        # make a material edit (agent stalls, submit-blocked loops) finish with
        # zero summary writes and the reporter tags them identity_missing.
        _emit_per_task_summary(reason="periodic_no_edit")
        return

    ms = load_micro_state()
    ms["edit_count"] = ms.get("edit_count", 0) + 1
    fec = ms.get("file_edit_counts", {})
    for f in changed:
        fec[f] = fec.get(f, 0) + 1
    ms["file_edit_counts"] = fec

    log_event("material_edit", files=changed[:3], edit_count=ms["edit_count"])

    # ── Stuck detection (Tier 2b: Reflexion pattern) ──
    # Research: Reflexion (Shinn et al., NeurIPS 2023, arXiv:2303.11366) shows +11pp
    # on HumanEval from feedback triggered by failure states. Self-Refine (Madaan et al.,
    # NeurIPS 2023, arXiv:2303.17651) shows specific feedback > generic (+13% vs -1.5%).
    # Trigger: 3+ edits to same file without gt_check → agent is stuck.
    for f in changed:
        if fec.get(f, 0) >= 3:
            stuck_ev = _compute_stuck_evidence(f)
            if stuck_ev:
                log_event("stuck_detected", file=f, edit_count=fec[f])
                state["gt_evidence"] = stuck_ev
                save_micro_state(ms)
                _write_state(state)
                _emit_per_task_summary(reason="stuck_detected")
                return

    # ── vNext event_brief surface ──
    if GT_VNEXT_ENABLED:
        all_findings = []
        for fpath in changed[:2]:
            all_findings.extend(_vnext_run_findings(fpath))
        if all_findings:
            novel, suppressed = _vnext_filter_novel(all_findings)
            if novel:
                text = _vnext_format_findings(novel, "event_brief")
                _vnext_update_meta(
                    event_brief_called=True,
                    event_brief_findings_count=_vnext_meta_get("event_brief_findings_count", 0) + len(novel),
                    event_brief_suppressed=_vnext_meta_get("event_brief_suppressed", 0) + suppressed,
                )
                log_event("vnext_event_brief",
                          findings=len(novel), suppressed=suppressed,
                          files=changed[:2])
                state["gt_evidence"] = text
                save_micro_state(ms)
                _write_state(state)
                _emit_per_task_summary(reason="vnext_event_brief")
                return
            else:
                log_event("vnext_event_brief", findings=0,
                          suppressed=suppressed, reason="all_suppressed")
        else:
            log_event("vnext_event_brief", findings=0, reason="no_findings")

    # ── 4. CHANNEL A: MICRO-UPDATE ─────────────────────────────────────
    micro_result = build_micro_update_safe(changed)

    # ── 4a. LSP-HYBRID: promote ambiguous edges (if enabled) ──────────
    # β: the legacy GT_LSP_READY sentinel is gone. install.sh's
    # gt_wait_index already blocks startup on the db, and WAL mode lets
    # readers tolerate concurrent indexer writes — nothing in the source
    # tree ever created /tmp/gt_lsp_ready, so the gate could only be
    # satisfied by out-of-tree tooling.
    promo_stats: dict = {}
    lsp_enabled = os.environ.get("GT_LSP_ENABLED") == "1"
    if lsp_enabled and changed:
        if "/tmp" not in sys.path:
            sys.path.insert(0, "/tmp")
        try:
            from lsp_promoter import promote_ambiguous_edges
        except Exception as e:
            promo_stats = {"outcome": "failed", "error": f"import:{e}"[:200]}
            log_event("lsp_promotion_failed", **promo_stats)
        else:
            promo_stats = promote_ambiguous_edges(
                source_files=changed, db_path=GT_DB,
                root=REPO_ROOT, language="python") or {}
            _outcome = promo_stats.get("outcome", "ran_noop")
            lsp_conf = 0.0
            lsp_level = 0
            if promo_stats.get("verified", 0) > 0:
                lsp_conf = 0.78
                lsp_level = 2
            elif promo_stats.get("corrected", 0) > 0:
                lsp_conf = 0.68
                lsp_level = 1
            try:
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
                _tier, _reason = lsp_decision.tier, lsp_decision.reason
            except Exception as e:
                _tier, _reason = 0, f"classifier_error:{e}"[:120]
                lsp_decision = None
            # γ2: one umbrella event (lsp_promotion) carries all stats so
            # existing dashboards keep working; three distinct events
            # (succeeded/noop/failed) make the SHOULD gate truthful.
            _event_name = {
                "ran_ok": "lsp_promotion_succeeded",
                "ran_noop": "lsp_promotion_noop",
                "failed": "lsp_promotion_failed",
            }.get(_outcome, "lsp_promotion_noop")
            log_event(_event_name, **promo_stats,
                      decision_tier=_tier, decision_reason=_reason)
            log_event("lsp_promotion", **promo_stats,
                      decision_tier=_tier, decision_reason=_reason)
            # Re-run micro only when LSP actually improved edges.
            if _outcome == "ran_ok" and lsp_decision is not None and _tier > 0 \
                    and (promo_stats.get("verified", 0) > 0
                         or promo_stats.get("corrected", 0) > 0):
                micro_result = build_micro_update_safe(changed)
    elif lsp_enabled:
        log_event("lsp_status", enabled=True, status="skipped",
                  reason="no_changed_files", changed=0)
    else:
        log_event("lsp_status", enabled=False, status="disabled",
                  reason="GT_LSP_ENABLED!=1", changed=0)

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
                _ack_id = _arm_ack(_step, channel="micro", tier=tier,
                         focus_file=focus_file, focus_symbol=focus_sym,
                         pre_action=state.get("action", ""), pre_changed=changed,
                         hint_fingerprint=fingerprint, hint_shape="micro")
                if _ack_id:
                    state["gt_evidence"] = state["gt_evidence"] + _ack_hint_line(
                        _ack_id, f"gt_check {focus_file}" if focus_file else "")
                log_event("micro_emitted", chars=len(state["gt_evidence"]),
                          file=focus_file, focus_symbol=focus_sym,
                          intent=intent, tier=tier, score=round(score, 2),
                          decision_tier=decision.tier,
                          decision_reason=decision.reason,
                          hint_fingerprint=fingerprint,
                          ack_id=_ack_id,
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
                _verify_ack_id = None
                if not GT_ACK_STATE.exists():
                    _verify_ack_id = _arm_ack(_step, channel="verify", tier=verify_tier,
                             focus_file=(changed[0] if changed else ""),
                             focus_symbol="",
                             pre_action=state.get("action", ""),
                             pre_changed=changed,
                             hint_fingerprint=verify_fingerprint,
                             hint_shape="verify")
                if _verify_ack_id:
                    _target = "submit"
                    state["gt_evidence"] = truncate(
                        state["gt_evidence"] + _ack_hint_line(_verify_ack_id, _target),
                        900, 7)
                log_event("verify_emitted", chars=len(verdict),
                          tier=verify_tier,
                          ack_id=_verify_ack_id,
                          decision_tier=verify_decision.tier,
                          decision_reason=verify_decision.reason,
                          budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"],
                          hint_fingerprint=verify_fingerprint)
        else:
            log_event("verify_suppressed", reason="no_verdict",
                      budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"])

    # ── 6. CHANNEL C: MATERIAL_EDIT FALLBACK (feature-flagged) ──────────
    # When GT_ARM_ON_MATERIAL_EDIT=1, arm a file-level window on every
    # material edit that isn't already covered by briefing/micro/verify.
    # Dedup rule (see plan Step 1):
    #   - same-cycle + same-file -> skip (duplicate)
    #   - same-cycle + different-file -> arm anyway; material_edit is the
    #     file-level fallback and should win for the newly-edited file.
    #   - prior-cycle window (stale) -> safe to arm.
    if os.environ.get("GT_ARM_ON_MATERIAL_EDIT") == "1" and changed:
        _me_file = changed[0]
        _me_cycle = int(_step)
        _existing = _load_ack()
        _should_arm = True
        if _existing is not None:
            _ex_cycle = int(_existing.get("cycle", -1))
            _ex_file = _existing.get("file", "") or ""
            _ex_chan = (_existing.get("channel", "") or "")
            if _ex_cycle == _me_cycle:
                if _ex_file == _me_file and _ex_chan == "material_edit":
                    _should_arm = False
                    log_event("ack_arm_dedup",
                              kept="existing",
                              reason="same_cycle_same_file_same_channel",
                              prior_channel=_existing.get("channel"),
                              prior_file=_ex_file,
                              attempted_channel="material_edit",
                              attempted_file=_me_file,
                              cycle=_me_cycle)
        if _should_arm:
            _me_ack_id = _arm_ack(_me_cycle, channel="material_edit",
                                  tier="edit",
                                  focus_file=_me_file, focus_symbol="",
                                  pre_action=state.get("action", ""),
                                  pre_changed=changed,
                                  hint_shape="material_edit")
            if _me_ack_id:
                log_event("ack_armed_on_edit",
                          ack_id=_me_ack_id,
                          file=_me_file,
                          edit_count=ms.get("edit_count") if isinstance(ms, dict) else None,
                          cycle=_me_cycle)
                # Deliver a concrete directive so the next turn sees it.
                # Non-silent material_edit: payload goes through next_step_template
                # gt_evidence block, then steer_delivered fires at cycle_end.
                _me_hint = (
                    f"GT DIRECTIVE: recent edit to {_me_file} is unverified. "
                    f"Run: gt_check {_me_file}"
                )
                _me_existing = state.get("gt_evidence", "") or ""
                _me_combined = (
                    _me_hint + ("\n" + _me_existing if _me_existing else "")
                )
                state["gt_evidence"] = truncate(_me_combined, 600, 5)

    save_micro_state(ms)
    _write_state(state)

    # Final diagnostic: confirm what was delivered
    ev = state.get("gt_evidence", "")
    # Step 3: pair delivery with the currently-armed window so we can
    # distinguish "armed + delivered" from "armed + dropped". A window
    # armed this cycle with a non-empty gt_evidence counts as delivered;
    # a window armed without evidence (e.g. budget suppression) counts
    # as dropped.
    _armed_now = _load_ack()
    _delivered_ack_id = ""
    _delivered_channel = ""
    if _armed_now is not None and int(_armed_now.get("cycle", -1)) == int(_step):
        _delivered_ack_id = _armed_now.get("ack_id", "") or ""
        _delivered_channel = _armed_now.get("channel", "") or ""
        log_event("steer_delivered" if bool(ev) else "steer_dropped",
                  ack_id=_delivered_ack_id,
                  channel=_delivered_channel,
                  insertion_path=_delivered_channel,
                  delivered=bool(ev),
                  truncated=False,
                  payload_len=len(ev),
                  cycle=int(_step),
                  file=_armed_now.get("file", "") or "")
    log_event("cycle_end", delivered=bool(ev), chars=len(ev),
              evidence_preview=ev[:80] if ev else "",
              armed_ack_id=_delivered_ack_id,
              armed_channel=_delivered_channel)
    # Flush per-task summary on every cycle. Previous versions only wrote
    # this on presubmit/step_limit, which is 1-2s before container death.
    # The scraper polls every few seconds, so the narrow write window was
    # almost always missed, producing identity_missing on every row. Writing
    # on every cycle gives the scraper dozens of chances to catch the file.
    _emit_per_task_summary(reason="periodic")


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

    # ── vNext task_map surface (safe version) ──
    if GT_VNEXT_ENABLED:
        log_event("vnext_task_map_attempt", gt_intel_real=os.path.exists(GT_INTEL_REAL),
                  source="safe")
        findings = _vnext_run_briefing_findings(issue_text)
        log_event("vnext_task_map_result", findings_count=len(findings), source="safe")
        if findings:
            novel, suppressed = _vnext_filter_novel(findings)
            text = _vnext_format_findings(novel, "task_map")
            _vnext_update_meta(
                task_map_emitted=True,
                task_map_findings_count=len(novel),
                task_map_suppressed=suppressed,
            )
            log_event("pre_edit_briefing", status="vnext_task_map",
                      findings=len(novel), suppressed=suppressed, source="safe")
            if text:
                return text
        else:
            _vnext_update_meta(task_map_emitted=False, task_map_empty_reason="no_findings")
            log_event("pre_edit_briefing", status="vnext_task_map_empty", source="safe")
        # Fall through to legacy

    try:
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        from gt_intel_real import (
            check_staleness,
            compute_localization,
            format_localization_briefing,
            is_critical_path,
        )

        conn = _open_gt_db(timeout=15)
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
            weak = (
                f"[GT] Weak file-level hint: {top.node.file_path} is the top candidate. "
                f"Inspect this file before editing; after any edit, run gt_check {top.node.file_path}."
            )
            log_event("pre_edit_briefing", status="no_output",
                      reason=decision.reason, tier=top.tier,
                      confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path,
                      weak_file_fallback=True)
            return weak

        _ack_id = _arm_ack(_read_cycle(), channel="orient", tier=top.tier,
                 focus_file=top.node.file_path, focus_symbol=top.node.name,
                 pre_action="", pre_changed=[],
                 hint_fingerprint=fingerprint, hint_shape="briefing")
        log_event("pre_edit_briefing", status="emitted",
                  tier=top.tier, confidence=round(top.confidence, 2),
                  target=top.node.name, file=top.node.file_path,
                  ack_id=_ack_id,
                  decision_tier=decision.tier, decision_reason=decision.reason)
        if _ack_id:
            out = out + _ack_hint_line(_ack_id, f"gt_lookup {top.node.name}")
        return out

    except Exception as e:
        log_event("pre_edit_briefing", status="error_fallback_orient", detail=str(e)[:100])

    return _fallback_orient()


if __name__ == "__main__":
    main()
