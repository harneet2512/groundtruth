#!/usr/bin/env python3
"""GT state command for SWE-agent -- runs after every action.

v1.0.4 architecture:
  - Phase 1: Pre-edit briefing (first invocation only, before any edits)
  - Phase 2: Post-edit evidence (after each new file edit, hash-based dedup)
  - Phase 3: JSONL telemetry for every cycle

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
GT_HASHES = Path("/tmp/gt_file_hashes.json")
GT_TELEMETRY = Path("/tmp/gt_hook_telemetry.jsonl")
GT_BRIEFING_DONE = Path("/tmp/gt_briefing_done")
GT_BUDGET = Path("/tmp/gt_budget.json")
SOURCE_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php",
               ".c", ".cpp", ".h", ".cs", ".kt", ".swift"}


def file_hash(path):
    """SHA-256 of file contents."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


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
               "--root=.", f"--{mode}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = result.stdout.strip()
        if out and len(out) > 10 and "Error" not in out[:30] and "Traceback" not in out[:50]:
            return out
    except Exception:
        pass
    return ""


def run_incremental_reindex(file_path):
    """Incremental re-index a single file."""
    try:
        subprocess.run(
            [GT_INDEX, "--incremental", f"--files={file_path}",
             "--root=.", f"--output={GT_DB}"],
            capture_output=True, timeout=10
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

    # Phase 2: Confidence-gated pre-edit localization (v1.0.4 redesign).
    # v3 showed raw --enhanced-briefing was net negative (wrong targets).
    # Now uses structured LocalizationState with confidence tiers:
    #   verified → structural guidance, likely → candidate list, possible → soft hint.
    if not GT_BRIEFING_DONE.exists():
        briefing = generate_pre_edit_briefing()
        if briefing:
            state["gt_evidence"] = briefing
            STATE_PATH.write_text(json.dumps(state))
            return

    # Get modified files from git diff
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=5
        )
        files = [f.strip() for f in diff.stdout.strip().split("\n") if f.strip()]
    except Exception:
        files = []

    if not files:
        log_event("cycle", status="no_diff")
        STATE_PATH.write_text(json.dumps(state))
        return

    # Load previously seen content hashes (Phase 4: hash-based refire)
    hashes = {}
    if GT_HASHES.exists():
        try:
            hashes = json.loads(GT_HASHES.read_text())
        except Exception:
            hashes = {}

    gt_output = ""
    selected_file = None

    for fpath in files:
        ext = os.path.splitext(fpath)[1]
        if ext not in SOURCE_EXTS:
            continue
        base = os.path.basename(fpath)
        if base.startswith("test_") or base.startswith("reproduce"):
            continue

        # Phase 4: Content-hash refire -- only evaluate if content actually changed
        current_hash = file_hash(fpath)
        if current_hash is None:
            continue
        old_hash = hashes.get(fpath)
        if current_hash == old_hash:
            continue  # Content unchanged since last evaluation

        hashes[fpath] = current_hash
        selected_file = fpath

        # Incremental re-index
        reindexed = run_incremental_reindex(fpath)

        # Run GT evidence
        gt_output = run_gt_intel(fpath)

        # Phase 3: Telemetry
        families = []
        if gt_output:
            for fam in ["OBLIGATION", "NEGATIVE", "CALLER", "TEST", "CRITIQUE",
                         "IMPACT", "PRECEDENT", "SIBLING", "IMPORT", "TYPE"]:
                if fam in gt_output.upper():
                    families.append(fam)

        log_event("post_edit",
                  file=fpath,
                  hash=current_hash,
                  reindexed=reindexed,
                  evidence_empty=not bool(gt_output),
                  families=families,
                  evidence_len=len(gt_output) if gt_output else 0)

        if gt_output:
            break  # One file per cycle

    GT_HASHES.write_text(json.dumps(hashes))

    if not selected_file:
        log_event("cycle", status="no_new_edits", diff_files=len(files))

    if gt_output:
        state["gt_evidence"] = gt_output

    # Log budget state for telemetry
    budget_state = {}
    if GT_BUDGET.exists():
        try:
            budget_state = json.loads(GT_BUDGET.read_text())
        except Exception:
            pass
    if budget_state:
        log_event("budget_snapshot", **budget_state)

    STATE_PATH.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
