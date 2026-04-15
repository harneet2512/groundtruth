#!/usr/bin/env python3
"""GT state command for SWE-agent -- runs after every action.

v1.1.0 architecture — explicit checkpoint semantics:

  CHECKPOINT_STARTUP   → once at session start: gt_orient + localization brief
  CHECKPOINT_DIFF      → when diff hash changes: gt_check on edited files
                         Budget: max 3 gt_check calls per session (enforced)
  CHECKPOINT_PRESUBMIT → when agent signals submit: final gt_check on full diff
                         Does not count toward gt_check budget

Research basis:
  ContextBench (2602.05892): sparse high-value interventions beat noisy scaffolding.
  SWE-Replay (2601.22129): checkpointed delivery reduces redundant evidence.

Refire logic: uses content hash (SHA-256), not mtime.
A file is re-evaluated only when its content actually changes.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_PATH = Path("/root/state.json")
GT_DB = "/tmp/gt_graph.db"
GT_INTEL = "/tmp/gt_intel.py"
GT_INDEX = "/tmp/gt-index"
# SWE-agent may invoke state command from /root, not /testbed.
# All git commands must use REPO_ROOT as cwd.
REPO_ROOT = "/testbed"
GT_HASHES = Path("/tmp/gt_file_hashes.json")
GT_TELEMETRY = Path("/tmp/gt_hook_telemetry.jsonl")
# Checkpoint sentinels
GT_CHECKPOINT_STARTUP = Path("/tmp/gt_checkpoint_startup")   # was: gt_briefing_done
GT_BRIEFING_DONE = GT_CHECKPOINT_STARTUP                     # back-compat alias
GT_BUDGET = Path("/tmp/gt_budget.json")
GT_DIFF_HASH = Path("/tmp/gt_last_diff_hash")
GT_TOOL_COUNTS = Path("/tmp/gt_tool_counts.json")

# Budget: max gt_check calls per session (presubmit does not count)
_GT_CHECK_BUDGET = 3
# Submit signals: if the agent action contains any of these, trigger presubmit
_SUBMIT_SIGNALS = {"COMPLETE_TASK_AND_SUBMIT", "submit", "git diff > patch"}
SOURCE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php",
               ".c", ".cpp", ".h", ".cs", ".kt", ".swift"}


def file_hash(path):
    """SHA-256 of file contents."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


def diff_hash():
    """SHA-256 of the full current git diff (staged + unstaged)."""
    try:
        result = subprocess.run(
            ["git", "diff"], capture_output=True, text=True, timeout=10,
            cwd=REPO_ROOT,
        )
        return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]
    except Exception:
        return None


def get_tool_counts() -> dict:
    """Read current tool counts from disk."""
    if GT_TOOL_COUNTS.exists():
        try:
            return json.loads(GT_TOOL_COUNTS.read_text())
        except Exception:
            pass
    return {}


def increment_tool_count(tool: str) -> None:
    """Increment telemetry counter for a GT tool call."""
    counts = get_tool_counts()
    counts[tool] = counts.get(tool, 0) + 1
    try:
        GT_TOOL_COUNTS.write_text(json.dumps(counts))
    except Exception:
        pass


def _gt_check_budget_exhausted() -> bool:
    """Return True if the gt_check budget has been spent (>=3 calls)."""
    counts = get_tool_counts()
    return counts.get("gt_check", 0) >= _GT_CHECK_BUDGET


def _is_presubmit_action(state: dict) -> bool:
    """Detect if the current agent action is a submit/complete signal."""
    action = str(state.get("action", "")).lower()
    last_output = str(state.get("last_output", "")).lower()
    combined = action + " " + last_output
    return any(sig.lower() in combined for sig in _SUBMIT_SIGNALS)


def log_event(event, **kwargs):
    """Append a structured telemetry event."""
    try:
        entry = {"ts": time.strftime("%H:%M:%S"), "event": event}
        entry.update(kwargs)
        with open(GT_TELEMETRY, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run_gt_intel(file_path, mode="reminder"):
    """Run gt_intel.py and return output."""
    try:
        cmd = ["python3", GT_INTEL, f"--db={GT_DB}", f"--file={file_path}",
               f"--root={REPO_ROOT}", f"--{mode}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                cwd=REPO_ROOT)
        out = result.stdout.strip()
        if out and len(out) > 10 and "Error" not in out[:30] and "Traceback" not in out[:50]:
            return _filter_hook_evidence(out)
    except Exception:
        pass
    return ""


def _filter_hook_evidence(raw_evidence: str) -> str:
    """Filter hook evidence for agent consumption.

    Rules:
    - Remove [STALE] lines entirely — stale evidence confuses the agent
    - Keep [VERIFIED] and [WARNING] lines — these are actionable
    - Remove [OK] / [INFO] lines — no signal
    - If nothing actionable remains, return empty (suppress delivery)
    """
    if not raw_evidence:
        return ""

    lines = raw_evidence.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        # Skip stale warnings
        if stripped.startswith("[STALE]"):
            continue
        # Skip empty/OK lines
        if stripped.startswith("[OK]") or stripped.startswith("[INFO]"):
            continue
        # Skip the gt-evidence XML tags but keep content
        if stripped == "<gt-evidence>" or stripped == "</gt-evidence>":
            filtered.append(stripped)
            continue
        # Keep VERIFIED and WARNING lines
        if "[VERIFIED]" in stripped or "[WARNING]" in stripped:
            filtered.append(line)
            continue
        # Keep lines within evidence block that aren't filtered
        if stripped and not stripped.startswith("["):
            filtered.append(line)

    result = "\n".join(filtered).strip()

    # If only XML tags remain with no content, suppress
    content = result.replace("<gt-evidence>", "").replace("</gt-evidence>", "").strip()
    if not content:
        return ""

    return result


def run_incremental_reindex(file_path):
    """Incremental re-index a single file."""
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={file_path}",
             f"--root={REPO_ROOT}", f"--output={GT_DB}"],
            capture_output=True, timeout=10,
            cwd=REPO_ROOT,
        )
        return True
    except Exception:
        return False


def generate_pre_edit_briefing():
    """Confidence-gated pre-edit localization micro-briefing.

    Research basis: BugCerberus (hierarchical localization), Think-Search-Patch
    (candidate refinement). Confidence gates the strength of the message:
    - High: target + structural constraints (OBLIGATION/CALLER/TEST)
    - Medium: candidate shortlist only (no structural steering)
    - Low: minimal hint (avoid over-steering)
    """
    if GT_BRIEFING_DONE.exists():
        return ""

    GT_BRIEFING_DONE.touch()

    # Find issue text
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

    # Use confidence-gated localization instead of raw --enhanced-briefing
    try:
        # Bootstrap sys.path for groundtruth_v2 + gt_intel imports
        for p in ["/tmp", "/root/tools/groundtruth/bin", os.path.dirname(GT_INTEL)]:
            if p and p not in sys.path and os.path.isdir(p):
                sys.path.insert(0, p)

        import sqlite3
        from gt_intel import compute_localization, format_localization_briefing

        conn = sqlite3.connect(GT_DB, timeout=5)
        state = compute_localization(conn, issue_text, root=".")

        if not state.candidates:
            log_event("pre_edit_briefing", status="no_candidates",
                      identifiers=len(state.issue_identifiers))
            conn.close()
            return ""

        top = state.candidates[0]
        out = format_localization_briefing(state, conn, ".")
        conn.close()

        if out:
            log_event("pre_edit_briefing",
                      status="emitted",
                      tier=top.tier,
                      confidence=round(top.confidence, 2),
                      structural_unlocked=state.structural_unlocked,
                      target=top.node.name,
                      file=top.node.file_path,
                      lines=out.count("\n") + 1)
            return out
        else:
            log_event("pre_edit_briefing", status="empty_output")

    except Exception as e:
        log_event("pre_edit_briefing", status="error", detail=str(e)[:100])
        # Fallback: try the old subprocess path if import fails
        try:
            with open("/tmp/gt_briefing_issue.txt", "w") as f:
                f.write(issue_text[:3000])
            result = subprocess.run(
                ["python3", GT_INTEL, f"--db={GT_DB}",
                 "--enhanced-briefing", "--issue-text=@/tmp/gt_briefing_issue.txt",
                 "--root=."],
                capture_output=True, text=True, timeout=20
            )
            out = result.stdout.strip()
            if out and len(out) > 20:
                log_event("pre_edit_briefing", status="fallback_emitted")
                return out
        except Exception:
            pass

    return ""


def _collect_diff_evidence() -> tuple[str, list[str], dict]:
    """Collect GT post-edit evidence for changed files.

    Returns (merged_evidence, edited_files, updated_hashes).
    Handles incremental reindex + per-file gt_intel.
    """
    try:
        diff_names = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=5,
            cwd=REPO_ROOT,
        )
        files = [f.strip() for f in diff_names.stdout.strip().split("\n") if f.strip()]
    except Exception:
        files = []

    hashes: dict = {}
    if GT_HASHES.exists():
        try:
            hashes = json.loads(GT_HASHES.read_text())
        except Exception:
            hashes = {}

    gt_outputs: list[str] = []
    edited_files: list[str] = []

    for fpath in files:
        ext = os.path.splitext(fpath)[1]
        if ext not in SOURCE_EXTS:
            continue
        base = os.path.basename(fpath)
        if base.startswith("test_") or base.startswith("reproduce"):
            continue

        current_hash = file_hash(fpath)
        if current_hash is None:
            continue
        if current_hash == hashes.get(fpath):
            continue  # Content unchanged since last evaluation

        hashes[fpath] = current_hash
        edited_files.append(fpath)

        reindexed = run_incremental_reindex(fpath)
        file_evidence = run_gt_intel(fpath)

        families = [
            fam for fam in ["OBLIGATION", "NEGATIVE", "CALLER", "TEST",
                             "CRITIQUE", "IMPACT", "PRECEDENT", "SIBLING",
                             "IMPORT", "TYPE"]
            if file_evidence and fam in file_evidence.upper()
        ]
        log_event("post_edit",
                  file=fpath,
                  hash=current_hash,
                  reindexed=reindexed,
                  evidence_empty=not bool(file_evidence),
                  families=families,
                  evidence_len=len(file_evidence) if file_evidence else 0)

        if file_evidence:
            gt_outputs.append(file_evidence)

    return "\n\n".join(gt_outputs), edited_files, hashes


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

    # ── CHECKPOINT_STARTUP ────────────────────────────────────────────────────
    # Fires exactly once per session. Emits localization brief + semantic
    # constraints. Research basis: Think-Search-Patch, BugCerberus.
    if not GT_CHECKPOINT_STARTUP.exists():
        briefing = generate_pre_edit_briefing()
        increment_tool_count("gt_orient")
        log_event("checkpoint_startup",
                  status="emitted" if briefing else "empty",
                  gt_orient_total=get_tool_counts().get("gt_orient", 1))
        if briefing:
            state["gt_evidence"] = briefing
            STATE_PATH.write_text(json.dumps(state))
            return
        # Even if empty, mark done so we don't retry
        GT_CHECKPOINT_STARTUP.touch()

    # ── CHECKPOINT_PRESUBMIT ──────────────────────────────────────────────────
    # Fires when agent signals intent to submit. Does NOT count toward budget.
    # Research basis: SWE-Replay — final diff check before patch submission.
    if _is_presubmit_action(state):
        gt_output, edited_files, hashes = _collect_diff_evidence()
        increment_tool_count("presubmit")
        log_event("checkpoint_presubmit",
                  status="emitted" if gt_output else "empty",
                  edited_files=len(edited_files))
        if hashes:
            GT_HASHES.write_text(json.dumps(hashes))
        if gt_output:
            state["gt_evidence"] = gt_output
        STATE_PATH.write_text(json.dumps(state))
        return

    # ── CHECKPOINT_DIFF ───────────────────────────────────────────────────────
    # Fires when the diff hash changes (material edit detected).
    # Budget-enforced: at most _GT_CHECK_BUDGET (3) gt_check calls per session.
    # Research basis: ContextBench — sparse high-value interventions beat noisy.
    current_diff_hash = diff_hash()
    last_diff_hash = GT_DIFF_HASH.read_text().strip() if GT_DIFF_HASH.exists() else None

    if current_diff_hash and current_diff_hash == last_diff_hash:
        log_event("cycle", status="diff_unchanged")
        STATE_PATH.write_text(json.dumps(state))
        return

    if current_diff_hash:
        GT_DIFF_HASH.write_text(current_diff_hash)

    # Budget gate: skip if budget exhausted
    if _gt_check_budget_exhausted():
        log_event("cycle", status="budget_exhausted",
                  gt_check_count=get_tool_counts().get("gt_check", 0))
        increment_tool_count("suppressions")
        STATE_PATH.write_text(json.dumps(state))
        return

    gt_output, edited_files, hashes = _collect_diff_evidence()
    increment_tool_count("gt_check")
    GT_HASHES.write_text(json.dumps(hashes))

    if not edited_files:
        log_event("cycle", status="no_new_edits",
                  gt_check_count=get_tool_counts().get("gt_check", 0))
    else:
        log_event("checkpoint_diff",
                  status="emitted" if gt_output else "empty",
                  edited_files=len(edited_files),
                  gt_check_count=get_tool_counts().get("gt_check", 0),
                  budget_remaining=_GT_CHECK_BUDGET - get_tool_counts().get("gt_check", 0))

    if gt_output:
        state["gt_evidence"] = gt_output

    STATE_PATH.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
