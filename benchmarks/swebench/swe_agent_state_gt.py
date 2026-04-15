#!/usr/bin/env python3
"""GT state command for SWE-agent — two-channel post-edit hook.

v2.0.0 architecture — micro-update + verification channels:

  Channel A: MICRO-UPDATE (cheap, every material edit)
    - Fires when any source file content hash changes
    - Max 3 lines / 400 chars — one constraint + one next action
    - Confidence-gated: verified=assert, likely=hedge, abstain=silence
    - Anti-bloat: dedup, cooldown window, compliance suppression

  Channel B: VERIFICATION (expensive, checkpointed, budgeted)
    - Pre-submit always (no budget)
    - Every Nth material edit if budget remains
    - Loop detection trigger
    - Budget: MAX_VERIFY per task (presubmit exempt)

  STARTUP: once at session start (localization brief)

Research basis:
  ContextBench (2602.05892): precision > recall; 19-43% context unused
  SWE-Skills-Bench (2603.15401): weak guidance worse than none
  SkillReducer (2603.29919): compressed output +2.8% quality
  Complexity Trap (2508.21433): observation masking saves 52% cost
  Anthropic Effective Harnesses: checkpoint at boundaries
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


# ── Utilities ──────────────────────────────────────────────────────────────

def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


def get_tool_counts() -> dict:
    if GT_TOOL_COUNTS.exists():
        try:
            return json.loads(GT_TOOL_COUNTS.read_text())
        except Exception:
            pass
    return {}


def increment_tool_count(tool: str) -> None:
    counts = get_tool_counts()
    counts[tool] = counts.get(tool, 0) + 1
    try:
        GT_TOOL_COUNTS.write_text(json.dumps(counts))
    except Exception:
        pass


def log_event(event, **kwargs):
    try:
        entry = {"ts": time.strftime("%H:%M:%S"), "event": event}
        entry.update(kwargs)
        with open(GT_TELEMETRY, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _is_presubmit_action(state: dict) -> bool:
    action = str(state.get("action", "")).lower()
    last_output = str(state.get("last_output", "")).lower()
    combined = action + " " + last_output
    return any(sig.lower() in combined for sig in _SUBMIT_SIGNALS)


def truncate_output(text: str, max_chars: int, max_lines: int) -> str:
    lines = text.strip().split("\n")[:max_lines]
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result


def normalize_for_dedup(text: str) -> str:
    """Normalize micro-update text for dedup hashing."""
    t = re.sub(r":\d+", "", text)          # strip line numbers
    t = re.sub(r"\s+", " ", t).strip()     # collapse whitespace
    t = re.sub(r"/[^\s]+/", "", t)         # strip absolute paths
    return t


def load_micro_state() -> dict:
    if GT_MICRO_STATE.exists():
        try:
            return json.loads(GT_MICRO_STATE.read_text())
        except Exception:
            pass
    return {"window": [], "last_hash": "", "edit_count": 0,
            "verify_used": 0, "compliance": {}, "file_edit_counts": {}}


def save_micro_state(ms: dict) -> None:
    try:
        GT_MICRO_STATE.write_text(json.dumps(ms))
    except Exception:
        pass


def load_hashes() -> dict:
    if GT_HASHES.exists():
        try:
            return json.loads(GT_HASHES.read_text())
        except Exception:
            pass
    return {}


def save_hashes(h: dict) -> None:
    try:
        GT_HASHES.write_text(json.dumps(h))
    except Exception:
        pass


# ── Material Edit Detection ────────────────────────────────────────────────

def detect_material_edits() -> list[str]:
    """Return list of source files whose content hash changed since last check."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
        )
        diff_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return []

    hashes = load_hashes()
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

    save_hashes(hashes)
    return changed


# ── Channel A: Micro-Update ────────────────────────────────────────────────

def build_micro_update(changed_files: list[str]) -> str | None:
    """Build a cheap micro-update by querying graph.db directly.

    No subprocess call — uses sqlite3 directly for speed.
    Returns structured 1-3 line micro or None to suppress.
    """
    if not changed_files or not os.path.exists(GT_DB):
        return None

    focus_file = changed_files[0]  # most recently changed

    try:
        conn = sqlite3.connect(GT_DB, timeout=3)
        conn.row_factory = sqlite3.Row

        # Find symbols in the focus file
        nodes = conn.execute(
            "SELECT id, name, label, signature, return_type FROM nodes "
            "WHERE file_path = ? AND is_test = 0 AND label IN ('Function','Method','Class') "
            "ORDER BY start_line",
            (focus_file,)
        ).fetchall()

        if not nodes:
            conn.close()
            return None

        # For each symbol, count callers + check properties
        best_constraint = None
        best_confidence = 0.0
        best_symbol = ""

        for node in nodes[:10]:  # cap to avoid slow queries
            nid = node["id"]
            name = node["name"]

            # Count cross-file callers
            callers = conn.execute(
                "SELECT COUNT(*) as cnt FROM edges "
                "WHERE target_id = ? AND type = 'CALLS' AND source_file != ?",
                (nid, focus_file)
            ).fetchone()
            caller_count = callers["cnt"] if callers else 0

            # Check return_shape property
            ret_shape = conn.execute(
                "SELECT value FROM properties WHERE node_id = ? AND kind = 'return_shape'",
                (nid,)
            ).fetchone()

            # Check assertions targeting this symbol
            assertion_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM assertions WHERE target_name = ?",
                (name,)
            ).fetchone()
            assert_count = assertion_count["cnt"] if assertion_count else 0

            # Score this symbol
            score = caller_count * 0.4 + assert_count * 0.3
            if ret_shape and ret_shape["value"] == "value":
                score += 0.3  # non-None return obligation

            if score > best_confidence and score >= 0.3:
                best_confidence = score
                best_symbol = name

                # Build constraint text
                parts = []
                if caller_count > 0:
                    parts.append(f"{caller_count} callers depend on {name}()")
                if ret_shape and ret_shape["value"] == "value":
                    parts.append("must return a value (not None)")
                if assert_count > 0:
                    parts.append(f"{assert_count} test assertions reference {name}")

                best_constraint = " — ".join(parts) if parts else None

        conn.close()

        if not best_constraint or best_confidence < 0.3:
            return None

        # Determine confidence tier
        if best_confidence >= 1.0:
            tier = "verified"
        elif best_confidence >= 0.5:
            tier = "likely"
        else:
            return None  # abstain — below threshold

        # Build structured micro-update
        lines = [f"GT MICRO [{tier}] CONSTRAIN: {best_symbol}() — {best_constraint}"]
        if tier == "verified":
            lines.append(f"DONT_BREAK: {best_symbol}() signature and return type")
        lines.append(f"NEXT: verify your edit to {os.path.basename(focus_file)} preserves {best_symbol}() contract")

        return "\n".join(lines)

    except Exception:
        return None


def should_suppress_micro(micro_text: str, micro_state: dict) -> tuple[bool, str]:
    """Check if micro-update should be suppressed. Returns (suppress, reason)."""
    norm = normalize_for_dedup(micro_text)
    micro_hash = hashlib.sha256(norm.encode()).hexdigest()[:12]

    # Exact dedup
    if micro_hash == micro_state.get("last_hash"):
        return True, "exact_dedup"

    # Window dedup
    window = micro_state.get("window", [])
    if micro_hash in window:
        return True, "window_dedup"

    # Compliance suppression: if same constraint delivered M+ times and agent complied
    compliance = micro_state.get("compliance", {})
    if micro_hash in compliance and compliance[micro_hash] >= COMPLIANCE_THRESHOLD_M:
        return True, "compliance"

    return False, ""


def update_micro_state_after_emit(micro_text: str, micro_state: dict) -> None:
    """Update dedup state after emitting a micro-update."""
    norm = normalize_for_dedup(micro_text)
    micro_hash = hashlib.sha256(norm.encode()).hexdigest()[:12]

    micro_state["last_hash"] = micro_hash

    window = micro_state.get("window", [])
    window.append(micro_hash)
    if len(window) > DEDUP_WINDOW_K:
        window = window[-DEDUP_WINDOW_K:]
    micro_state["window"] = window

    # Increment compliance counter (agent saw it, may comply next edit)
    compliance = micro_state.get("compliance", {})
    compliance[micro_hash] = compliance.get(micro_hash, 0) + 1
    micro_state["compliance"] = compliance


# ── Channel B: Verification ────────────────────────────────────────────────

def should_run_verification(micro_state: dict, is_presubmit: bool) -> bool:
    if is_presubmit:
        return True
    edit_count = micro_state.get("edit_count", 0)
    verify_used = micro_state.get("verify_used", 0)
    if verify_used >= MAX_VERIFY_PER_TASK:
        return False
    # Every Nth edit
    if edit_count > 0 and edit_count % VERIFY_EVERY_N_EDITS == 0:
        return True
    # Loop detection: same file edited 3+ times
    file_counts = micro_state.get("file_edit_counts", {})
    if any(c >= 3 for c in file_counts.values()):
        return True
    return False


def run_verification(changed_files: list[str]) -> str | None:
    """Run expensive gt_intel verification on changed files."""
    outputs = []
    for fpath in changed_files[:2]:  # cap at 2 files
        run_incremental_reindex(fpath)
        evidence = run_gt_intel_filtered(fpath)
        if evidence:
            outputs.append(evidence)

    if not outputs:
        return None

    merged = "\n".join(outputs)
    return f"GT VERIFY:\n{merged}"


def run_gt_intel_filtered(file_path: str) -> str:
    """Run gt_intel and return only VERIFIED lines (no stale/ok/info)."""
    try:
        cmd = ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={file_path}",
               f"--root={REPO_ROOT}", "--reminder"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                cwd=REPO_ROOT)
        out = result.stdout.strip()
        if not out or len(out) < 10 or "Error" in out[:30] or "Traceback" in out[:50]:
            return ""
        return _filter_verified_only(out)
    except Exception:
        return ""


def _filter_verified_only(raw: str) -> str:
    """Keep only [VERIFIED] lines from gt_intel output. Strip tags/stale/ok."""
    lines = []
    for line in raw.split("\n"):
        s = line.strip()
        if "[VERIFIED]" in s:
            # Clean up the line
            s = s.replace("<gt-evidence>", "").replace("</gt-evidence>", "").strip()
            if s.startswith("[STALE]"):
                # Extract after [STALE] ... [VERIFIED]
                idx = s.find("[VERIFIED]")
                if idx >= 0:
                    s = s[idx:]
            lines.append(s)
    return "\n".join(lines[:2])  # max 2 constraints


def run_incremental_reindex(file_path: str) -> bool:
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={file_path}",
             f"--root={REPO_ROOT}", f"--output={GT_DB}"],
            capture_output=True, timeout=10, cwd=REPO_ROOT,
        )
        return True
    except Exception:
        return False


def run_gt_intel(file_path, mode="reminder"):
    """Run gt_intel.py and return filtered output."""
    try:
        cmd = ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={file_path}",
               f"--root={REPO_ROOT}", f"--{mode}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                cwd=REPO_ROOT)
        out = result.stdout.strip()
        if out and len(out) > 10 and "Error" not in out[:30] and "Traceback" not in out[:50]:
            return _filter_verified_only(out)
    except Exception:
        pass
    return ""


# ── Startup ────────────────────────────────────────────────────────────────

def generate_pre_edit_briefing():
    """Confidence-gated pre-edit localization micro-briefing."""
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
        loc_state = compute_localization(conn, issue_text, root=".")

        if not loc_state.candidates:
            log_event("pre_edit_briefing", status="no_candidates")
            conn.close()
            return ""

        top = loc_state.candidates[0]
        out = format_localization_briefing(loc_state, conn, ".")
        conn.close()

        if out:
            log_event("pre_edit_briefing", status="emitted",
                      tier=top.tier, confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            return out

    except Exception as e:
        log_event("pre_edit_briefing", status="error", detail=str(e)[:100])

    return ""


# ── Main ───────────────────────────────────────────────────────────────────

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

    # ── 1. STARTUP (once) ────────────────────────────────────────────────
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

    # ── 2. PRESUBMIT (always, no budget) ─────────────────────────────────
    if _is_presubmit_action(state):
        changed = detect_material_edits()
        if changed:
            verdict = run_verification(changed)
            if verdict:
                state["gt_evidence"] = truncate_output(verdict, 600, 5)
        increment_tool_count("presubmit")
        log_event("checkpoint_presubmit",
                  status="emitted" if state.get("gt_evidence") else "empty")
        STATE_PATH.write_text(json.dumps(state))
        return

    # ── 3. DETECT MATERIAL EDITS ─────────────────────────────────────────
    changed = detect_material_edits()
    if not changed:
        log_event("cycle", status="no_edit")
        STATE_PATH.write_text(json.dumps(state))
        return

    log_event("material_edit", files_changed=len(changed), files=changed[:3])

    micro_state = load_micro_state()

    # Track per-file edit counts (for loop detection)
    fec = micro_state.get("file_edit_counts", {})
    for f in changed:
        fec[f] = fec.get(f, 0) + 1
    micro_state["file_edit_counts"] = fec
    micro_state["edit_count"] = micro_state.get("edit_count", 0) + 1

    # ── 4. CHANNEL A: MICRO-UPDATE (every edit, no budget) ───────────────
    micro = build_micro_update(changed)
    if micro:
        suppress, reason = should_suppress_micro(micro, micro_state)
        if not suppress:
            state["gt_evidence"] = truncate_output(micro, MICRO_MAX_CHARS, MICRO_MAX_LINES)
            update_micro_state_after_emit(micro, micro_state)
            log_event("micro_emitted", chars=len(state["gt_evidence"]),
                      file=changed[0], edit_count=micro_state["edit_count"])
        else:
            log_event("micro_suppressed", reason=reason, file=changed[0])
    else:
        log_event("micro_attempt", result="no_signal", file=changed[0])

    # ── 5. CHANNEL B: VERIFICATION (checkpointed, budgeted) ─────────────
    if should_run_verification(micro_state, is_presubmit=False):
        verdict = run_verification(changed)
        micro_state["verify_used"] = micro_state.get("verify_used", 0) + 1
        if verdict:
            # Verification overrides micro if present
            state["gt_evidence"] = truncate_output(verdict, 600, 5)
            log_event("verify_emitted", chars=len(verdict),
                      budget_remaining=MAX_VERIFY_PER_TASK - micro_state["verify_used"])
        else:
            log_event("verify_abstained",
                      budget_remaining=MAX_VERIFY_PER_TASK - micro_state["verify_used"])
    else:
        # Log why verification was skipped
        if micro_state.get("verify_used", 0) >= MAX_VERIFY_PER_TASK:
            log_event("verify_skipped", reason="budget_exhausted")

    save_micro_state(micro_state)
    STATE_PATH.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
