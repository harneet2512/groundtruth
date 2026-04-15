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
  - Every Nth material edit if budget remains
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

# ── Config ─────────────────────────────────────────────────────────────────
MAX_VERIFY_PER_TASK = 8
MICRO_MAX_CHARS = 400
MICRO_MAX_LINES = 3
DEDUP_WINDOW_K = 3
COMPLIANCE_THRESHOLD_M = 3
VERIFY_EVERY_N_EDITS = 3
_SUBMIT_SIGNALS = {"COMPLETE_TASK_AND_SUBMIT", "submit", "git diff > patch"}
SOURCE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php",
               ".c", ".cpp", ".h", ".cs", ".kt", ".swift"}


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


def log_event(event, **kw):
    try:
        entry = {"ts": time.strftime("%H:%M:%S"), "event": event}
        entry.update(kw)
        with open(GT_TELEMETRY, "a") as f:
            f.write(json.dumps(entry) + "\n")
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
        "window": [], "last_hash": "", "edit_count": 0,
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
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
        )
        diff_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
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
            hashes[fpath] = h
            changed.append(fpath)

    _save_json(GT_HASHES, hashes)
    return changed


# ═══════════════════════════════════════════════════════════════════════════
# Channel A: Micro-update (cheap, direct sqlite3, no subprocess)
# ═══════════════════════════════════════════════════════════════════════════

def build_micro_update(changed_files):
    """Build a micro-update by querying graph.db directly.

    Returns structured ≤3 line text or None if no signal.
    """
    if not changed_files or not os.path.exists(GT_DB):
        return None

    focus = changed_files[0]
    try:
        conn = sqlite3.connect(GT_DB, timeout=3)
        conn.row_factory = sqlite3.Row

        nodes = conn.execute(
            "SELECT id, name, label, return_type FROM nodes "
            "WHERE file_path = ? AND is_test = 0 "
            "AND label IN ('Function','Method','Class') ORDER BY start_line",
            (focus,)
        ).fetchall()

        if not nodes:
            conn.close()
            return None

        best = None
        best_score = 0.0

        for node in nodes[:10]:
            nid, name = node["id"], node["name"]

            callers = conn.execute(
                "SELECT COUNT(*) as c FROM edges "
                "WHERE target_id = ? AND type = 'CALLS' AND source_file != ?",
                (nid, focus)
            ).fetchone()
            caller_count = callers["c"] if callers else 0

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

            if score > best_score and score >= 0.3:
                best_score = score
                parts = []
                if caller_count > 0:
                    parts.append(f"{caller_count} callers depend on {name}()")
                if ret_shape and ret_shape["value"] == "value":
                    parts.append("must return a value (not None)")
                if asserts > 0:
                    parts.append(f"{asserts} test assertions check {name}")
                best = (name, " — ".join(parts), score)

        conn.close()

        if not best or best[2] < 0.3:
            return None

        sym, constraint, sc = best
        tier = "verified" if sc >= 1.0 else "likely"
        base = os.path.basename(focus)

        lines = [f"GT MICRO [{tier}] CONSTRAIN: {sym}() — {constraint}"]
        if tier == "verified":
            lines.append(f"DONT_BREAK: {sym}() signature and return type")
        lines.append(f"NEXT: verify edit to {base} preserves {sym}() contract")

        return "\n".join(lines)

    except Exception:
        return None


def should_suppress_micro(text, ms):
    """Check dedup/window/compliance. Returns (suppress, reason)."""
    norm = normalize_for_dedup(text)
    h = hashlib.sha256(norm.encode()).hexdigest()[:12]

    if h == ms.get("last_hash"):
        return True, "exact_dedup"
    if h in ms.get("window", []):
        return True, "window_dedup"
    if h in ms.get("compliance", {}) and ms["compliance"][h] >= COMPLIANCE_THRESHOLD_M:
        return True, "compliance"
    return False, ""


def record_micro_emit(text, ms):
    """Update dedup state after emission."""
    norm = normalize_for_dedup(text)
    h = hashlib.sha256(norm.encode()).hexdigest()[:12]
    ms["last_hash"] = h
    window = ms.get("window", [])
    window.append(h)
    ms["window"] = window[-DEDUP_WINDOW_K:]
    comp = ms.get("compliance", {})
    comp[h] = comp.get(h, 0) + 1
    ms["compliance"] = comp


# ═══════════════════════════════════════════════════════════════════════════
# Channel B: Verification (expensive, budgeted)
# ═══════════════════════════════════════════════════════════════════════════

def should_verify(ms, presubmit=False):
    if presubmit:
        return True
    if ms.get("verify_used", 0) >= MAX_VERIFY_PER_TASK:
        return False
    ec = ms.get("edit_count", 0)
    if ec > 0 and ec % VERIFY_EVERY_N_EDITS == 0:
        return True
    if any(c >= 3 for c in ms.get("file_edit_counts", {}).values()):
        return True
    return False


def run_verification(changed_files):
    """Run gt_intel --reminder on changed files. Returns compact verdict."""
    outputs = []
    for fpath in changed_files[:2]:
        _try_reindex(fpath)
        ev = _run_gt_intel(fpath)
        if ev:
            outputs.append(ev)
    if not outputs:
        return None
    merged = "\n".join(outputs)
    return f"GT VERIFY:\n{merged}"


def _try_reindex(fpath):
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={fpath}",
             f"--root={REPO_ROOT}", f"--output={GT_DB}"],
            capture_output=True, timeout=10, cwd=REPO_ROOT,
        )
    except Exception:
        pass


def _run_gt_intel(fpath):
    """Run gt_intel --reminder, return only [VERIFIED] lines."""
    try:
        result = subprocess.run(
            ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={fpath}",
             f"--root={REPO_ROOT}", "--reminder"],
            capture_output=True, text=True, timeout=15, cwd=REPO_ROOT,
        )
        out = result.stdout.strip()
        if not out or len(out) < 10 or "Error" in out[:30]:
            return ""
        # Keep only VERIFIED lines (no stale, no info, no ok)
        lines = []
        for line in out.split("\n"):
            s = line.strip()
            if "[VERIFIED]" in s and "[STALE]" not in s:
                s = s.replace("<gt-evidence>", "").replace("</gt-evidence>", "").strip()
                lines.append(s)
        return "\n".join(lines[:2])
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Startup briefing (unchanged from v1.1.0)
# ═══════════════════════════════════════════════════════════════════════════

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

    if not issue_text or not os.path.exists(GT_DB):
        log_event("pre_edit_briefing", status="skipped", reason="no_issue_or_db")
        return ""

    try:
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        from gt_intel import compute_localization, format_localization_briefing

        conn = sqlite3.connect(GT_DB, timeout=5)
        loc = compute_localization(conn, issue_text, root=".")

        if not loc.candidates:
            log_event("pre_edit_briefing", status="no_candidates")
            conn.close()
            return ""

        top = loc.candidates[0]
        out = format_localization_briefing(loc, conn, ".")
        conn.close()

        if out:
            log_event("pre_edit_briefing", status="emitted",
                      tier=top.tier, confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            return out

    except Exception as e:
        log_event("pre_edit_briefing", status="error", detail=str(e)[:100])

    return ""


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:
            state = {}

    state["working_dir"] = os.getcwd()
    state.pop("gt_evidence", None)

    if not os.path.exists(GT_DB):
        log_event("cycle", status="no_db")
        STATE_PATH.write_text(json.dumps(state))
        return

    # ── 1. STARTUP (once) ──────────────────────────────────────────────
    if not GT_CHECKPOINT_STARTUP.exists():
        briefing = generate_pre_edit_briefing()
        increment_tool_count("gt_orient")
        log_event("checkpoint_startup",
                  status="emitted" if briefing else "empty")
        if briefing:
            state["gt_evidence"] = briefing
            STATE_PATH.write_text(json.dumps(state))
            return
        GT_CHECKPOINT_STARTUP.touch()

    # ── 2. PRESUBMIT (always, no budget cost) ──────────────────────────
    if _is_presubmit(state):
        changed = detect_material_edits()
        verdict = run_verification(changed) if changed else None
        if verdict:
            state["gt_evidence"] = truncate(verdict, 600, 5)
        increment_tool_count("presubmit")
        log_event("checkpoint_presubmit",
                  status="emitted" if state.get("gt_evidence") else "empty")
        STATE_PATH.write_text(json.dumps(state))
        return

    # ── 3. DETECT MATERIAL EDITS ───────────────────────────────────────
    changed = detect_material_edits()
    if not changed:
        log_event("cycle", status="no_edit")
        STATE_PATH.write_text(json.dumps(state))
        return

    ms = load_micro_state()
    ms["edit_count"] = ms.get("edit_count", 0) + 1
    fec = ms.get("file_edit_counts", {})
    for f in changed:
        fec[f] = fec.get(f, 0) + 1
    ms["file_edit_counts"] = fec

    log_event("material_edit", files=changed[:3], edit_count=ms["edit_count"])

    # ── 4. CHANNEL A: MICRO-UPDATE ─────────────────────────────────────
    micro = build_micro_update(changed)
    if micro:
        suppress, reason = should_suppress_micro(micro, ms)
        if not suppress:
            state["gt_evidence"] = truncate(micro, MICRO_MAX_CHARS, MICRO_MAX_LINES)
            record_micro_emit(micro, ms)
            log_event("micro_emitted", chars=len(state["gt_evidence"]),
                      file=changed[0])
        else:
            log_event("micro_suppressed", reason=reason, file=changed[0])
    else:
        log_event("micro_skip", reason="no_signal", file=changed[0])

    # ── 5. CHANNEL B: VERIFICATION (budgeted) ─────────────────────────
    if should_verify(ms):
        verdict = run_verification(changed)
        ms["verify_used"] = ms.get("verify_used", 0) + 1
        if verdict:
            state["gt_evidence"] = truncate(verdict, 600, 5)
            log_event("verify_emitted", chars=len(verdict),
                      budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"])
        else:
            log_event("verify_abstain",
                      budget_left=MAX_VERIFY_PER_TASK - ms["verify_used"])

    save_micro_state(ms)
    STATE_PATH.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
