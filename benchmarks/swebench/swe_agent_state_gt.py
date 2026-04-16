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
MICRO_MAX_CHARS = 300
MICRO_MAX_LINES = 2
DEDUP_WINDOW_K = 3
COMPLIANCE_THRESHOLD_M = 3
VERIFY_EVERY_N_EDITS = 1   # verify on every material edit (was 3)
TIER_VERIFIED = 0.8    # multiple callers + assertions/return
TIER_SILENT = 0.1      # lowered from 0.4 — emit even with weak signal
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


_TELEM_HOST_DIR = os.environ.get("GT_TELEMETRY_DIR", "")


def log_event(event, **kw):
    try:
        entry = {"ts": time.strftime("%H:%M:%S"), "event": event}
        entry.update(kw)
        line = json.dumps(entry) + "\n"
        # Container-local (always)
        with open(GT_TELEMETRY, "a") as f:
            f.write(line)
        # Host-visible (if configured via env var)
        if _TELEM_HOST_DIR:
            host_path = os.path.join(_TELEM_HOST_DIR, "gt_hook_telemetry.jsonl")
            with open(host_path, "a") as f:
                f.write(line)
        else:
            # Fallback: write to /tmp/.gt/ (NOT /testbed/ to avoid polluting git patches)
            fb = "/tmp/.gt"
            os.makedirs(fb, exist_ok=True)
            with open(os.path.join(fb, "gt_hook_telemetry.jsonl"), "a") as f:
                f.write(line)
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
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
        )
        diff_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        # Also check staged changes
        result2 = subprocess.run(
            ["git", "diff", "--staged", "--name-only"],
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
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
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
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

    Returns (text, focus_file, focus_symbol, intent, tier, score) or None.
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
        best_score = 0.0

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
                best_score = score
                parts = []
                if caller_count > 0:
                    parts.append(f"{caller_count} callers")
                if ret_shape and ret_shape["value"] == "value":
                    parts.append("returns value")
                if asserts > 0:
                    parts.append(f"{asserts} assertions")
                best = (name, ", ".join(parts) if parts else "edited", score)

        conn.close()

        if not best:
            return None

        sym, constraint, sc = best

        # Tiering: verified needs strong multi-signal evidence
        if sc < TIER_SILENT:
            return None  # SILENT — weak hints worse than none (2603.15401)
        tier = "verified" if sc >= TIER_VERIFIED else "likely"
        base = os.path.basename(focus)

        # Checklist format: [MUST]/[CHECK] + [NEXT] — 2 lines max
        tag = "MUST" if tier == "verified" else "CHECK"
        if intent == "CONSTRAIN":
            line1 = f"[{tag}] {sym}() — {constraint}, preserve signature/return"
            next_line = f"[NEXT] python3 -m pytest tests/ -k {sym} -x -q"
        else:
            line1 = f"[{tag}] {sym}() in {base} — {constraint}"
            next_line = f"[NEXT] python3 -m pytest tests/ -k {sym} -x -q"

        text = f"{line1}\n{next_line}"
        return (text, focus, sym, intent, tier, sc)

    except Exception:
        return None


def _dedup_key(focus_file, focus_symbol, intent):
    """Scoped dedup key: (file, symbol, intent) tuple hashed."""
    raw = "%s|%s|%s" % (focus_file, focus_symbol, intent)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def should_suppress_micro(text, focus_file, focus_symbol, intent, ms):
    """Check scoped dedup/window/compliance. Returns (suppress, reason)."""
    key = _dedup_key(focus_file, focus_symbol, intent)

    if key == ms.get("last_scope_key"):
        return True, "exact_dedup"
    if key in ms.get("scope_window", []):
        return True, "window_dedup"
    comp = ms.get("compliance", {})
    if comp.get(key, 0) >= COMPLIANCE_THRESHOLD_M:
        return True, "compliance"
    return False, ""


def record_micro_emit(text, focus_file, focus_symbol, intent, ms):
    """Update scoped dedup state after emission."""
    key = _dedup_key(focus_file, focus_symbol, intent)
    ms["last_scope_key"] = key
    window = ms.get("scope_window", [])
    window.append(key)
    ms["scope_window"] = window[-DEDUP_WINDOW_K:]
    comp = ms.get("compliance", {})
    comp[key] = comp.get(key, 0) + 1
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

        from gt_intel import compute_localization, format_localization_briefing

        conn = sqlite3.connect(GT_DB, timeout=5)
        loc = compute_localization(conn, issue_text, root=".")

        if not loc.candidates:
            log_event("pre_edit_briefing", status="no_candidates_fallback_orient")
            conn.close()
            return _fallback_orient()

        top = loc.candidates[0]
        out = format_localization_briefing(loc, conn, ".")
        conn.close()

        if out:
            log_event("pre_edit_briefing", status="emitted",
                      tier=top.tier, confidence=round(top.confidence, 2),
                      target=top.node.name, file=top.node.file_path)
            return out

    except Exception as e:
        log_event("pre_edit_briefing", status="error_fallback_orient", detail=str(e)[:100])

    # Last resort: deliver minimal orient so first delivery is never empty
    return _fallback_orient()


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

    # ── 0. NEXT-FOLLOW TRACKING ───────────────────────────────────────
    # Track whether the agent acted on GT's [NEXT] suggestion.
    # Read recent bash history to see what the agent actually ran.
    _ms_pre = load_micro_state()
    last_next = _ms_pre.get("last_next_instruction", "")
    next_window = _ms_pre.get("next_window", 0)
    if last_next and next_window > 0:
        # Get last few commands from bash history
        recent_cmd = ""
        try:
            r = subprocess.run(
                ["tail", "-3", "/root/.bash_history"],
                capture_output=True, text=True, timeout=2,
            )
            recent_cmd = r.stdout.lower()
        except Exception:
            pass

        # Also check the last command output for test signals
        try:
            r2 = subprocess.run(
                ["tail", "-20", "/root/.command_output"],
                capture_output=True, text=True, timeout=2,
            )
            recent_output = r2.stdout.lower()
        except Exception:
            recent_output = ""

        # Behavioral signals: agent ran some form of test/verification
        test_signals = ["pytest", "test", "assert", "passed", "failed",
                        "error", "traceback", "reproduce", "verify", "python -c"]
        cmd_match = any(sig in recent_cmd for sig in test_signals)
        output_match = any(sig in recent_output for sig in test_signals)

        if cmd_match or output_match:
            log_event("next_follow", cmd=recent_cmd.strip()[:100],
                      expected=last_next[:100], window_remaining=next_window,
                      match_type="cmd" if cmd_match else "output")
            _ms_pre["last_next_instruction"] = ""
            _ms_pre["next_window"] = 0
        else:
            _ms_pre["next_window"] = next_window - 1
            if next_window <= 1:
                log_event("next_ignored", expected=last_next[:100],
                          last_cmd=recent_cmd.strip()[:80])
                _ms_pre["last_next_instruction"] = ""
                _ms_pre["next_window"] = 0
        save_micro_state(_ms_pre)

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
    micro_result = build_micro_update(changed)

    # ── 4a. LSP-HYBRID: promote ambiguous edges (if enabled) ──────────
    promo_stats = {}
    if os.environ.get("GT_LSP_ENABLED") == "1" and changed:
        try:
            if "/tmp" not in sys.path:
                sys.path.insert(0, "/tmp")
            from lsp_promoter import promote_ambiguous_edges
            promo_stats = promote_ambiguous_edges(
                source_files=changed, db_path=GT_DB,
                root=REPO_ROOT, language="python")
            log_event("lsp_promotion", **promo_stats)
            # If edges improved, re-run micro targeting with upgraded graph
            if promo_stats.get("verified", 0) > 0 or promo_stats.get("corrected", 0) > 0:
                micro_result = build_micro_update(changed)
        except Exception as e:
            promo_stats = {"error": str(e)[:200]}
            log_event("lsp_promotion_error", detail=str(e)[:200])
    else:
        log_event("lsp_status", enabled=False)

    if micro_result:
        micro_text, focus_file, focus_sym, intent, tier, score = micro_result
        suppress, reason = should_suppress_micro(
            micro_text, focus_file, focus_sym, intent, ms)
        if not suppress:
            state["gt_evidence"] = truncate(micro_text, MICRO_MAX_CHARS, MICRO_MAX_LINES)
            record_micro_emit(micro_text, focus_file, focus_sym, intent, ms)
            # Extract [NEXT] line for follow tracking
            next_line = ""
            for ln in micro_text.split("\n"):
                if ln.startswith("[NEXT]"):
                    next_line = ln
                    break
            ms["last_next_instruction"] = next_line
            ms["next_window"] = 3  # give agent 3 steps to follow NEXT
            log_event("micro_emitted", chars=len(state["gt_evidence"]),
                      file=focus_file, focus_symbol=focus_sym,
                      intent=intent, tier=tier, score=round(score, 2),
                      next_action=next_line[:80],
                      promotion_stats=promo_stats)
        else:
            log_event("micro_suppressed", reason=reason, file=changed[0],
                      focus_symbol=focus_sym, intent=intent)
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

    # Final diagnostic: confirm what was delivered
    ev = state.get("gt_evidence", "")
    log_event("cycle_end", delivered=bool(ev), chars=len(ev),
              evidence_preview=ev[:80] if ev else "")


if __name__ == "__main__":
    main()
