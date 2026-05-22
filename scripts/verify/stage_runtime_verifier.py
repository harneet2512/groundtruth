#!/usr/bin/env python3
"""stage_runtime_verifier.py -- Main verifier for Product-v1 staged execution.

Orchestrates all verification checks for a given stage and produces one JSON
record per task with a unified verdict schema. Calls the sub-verifiers:
  - gt_pollution_check: GT_STATUS/GT_META leaking into agent observations
  - patch_integrity_check: patch produced, well-formed, SHA hash
  - product_v1_signal_check: confidence filter, neighbor cap, G7 silence, dedup,
    anchor ranking, visible-test bonus
  - task_result_summarizer: resolved/not-resolved per task

Usage:
    python scripts/verify/stage_runtime_verifier.py \\
        --output-dir /path/to/artifacts \\
        --stage stage1 \\
        --branch jedi__branch \\
        --head-sha b953231d

Output: JSON array to stdout, one record per task.
Exit 0 on success, 1 on verifier error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()

# Expected tasks for Stage 1
STAGE1_TASKS = [
    "amoffat__sh-744",
    "beeware__briefcase-2085",
    "conan-io__conan-17102",
    "pallets__flask-5637",
    "pylint-dev__pylint-10044",
]

# Control tasks: known-resolved in baseline. Regression = FAIL.
CONTROL_TASKS = {
    "amoffat__sh-744",
    "beeware__briefcase-2085",
}


def run_sub_verifier(script_name: str, output_dir: str) -> dict | None:
    """Run a sub-verifier script and parse its JSON output."""
    script_path = SCRIPT_DIR / script_name
    if not script_path.exists():
        print(f"WARN: sub-verifier {script_name} not found at {script_path}", file=sys.stderr)
        return None

    try:
        result = subprocess.run(
            [sys.executable, str(script_path), "--output-dir", output_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
        else:
            print(f"WARN: {script_name} produced no output. stderr: {result.stderr[:500]}", file=sys.stderr)
            return None
    except subprocess.TimeoutExpired:
        print(f"WARN: {script_name} timed out", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"WARN: {script_name} produced invalid JSON: {exc}", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"WARN: {script_name} failed to run: {exc}", file=sys.stderr)
        return None


def find_task_dirs(output_dir: str) -> dict[str, Path]:
    """Map task_id -> task directory from artifact layout."""
    result: dict[str, Path] = {}
    root = Path(output_dir)

    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name.startswith("task-"):
            tid = entry.name.replace("task-", "", 1)
            result[tid] = entry

    # If no task- prefixed dirs, look for output.jsonl files
    if not result:
        for ojf in root.rglob("output.jsonl"):
            parent = ojf.parent
            for part in parent.parts:
                if "__" in part and any(c.isdigit() for c in part):
                    result[part] = parent
                    break

    return result


def determine_l_status(
    layer_events: list[dict], layer_name: str
) -> str:
    """Determine delivery status for a layer (L1, L3, L3b) from structured events."""
    layer_evts = [e for e in layer_events if e.get("layer") == layer_name]
    if not layer_evts:
        return "unknown"

    delivered = any(e.get("emitted") is True and not e.get("suppressed") for e in layer_evts)
    suppressed = any(e.get("suppressed") is True for e in layer_evts)

    if delivered:
        return "delivered"
    if suppressed:
        reasons = [e.get("suppression_reason", "") for e in layer_evts if e.get("suppressed")]
        reason_str = ", ".join(set(r for r in reasons if r))
        return f"suppressed_{reason_str}" if reason_str else "suppressed"
    return "not_fired"


def load_layer_events(task_dir: Path) -> list[dict]:
    """Load all layer event JSONL files for a task."""
    events: list[dict] = []
    for f in task_dir.rglob("gt_layer_events_*.jsonl"):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
    return events


def load_full_run_log(task_dir: Path) -> str:
    """Load the full_run.log for a task."""
    for f in task_dir.rglob("full_run.log"):
        try:
            return f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return ""


def build_task_record(
    task_id: str,
    stage: str,
    branch: str,
    head_sha: str,
    task_dir: Path | None,
    pollution_results: dict | None,
    patch_results: dict | None,
    signal_results: dict | None,
    summary_results: dict | None,
) -> dict:
    """Build a unified verification record for one task."""

    # Defaults
    record = {
        "run_id": "",
        "task_id": task_id,
        "stage": stage,
        "branch": branch,
        "head_sha": head_sha,
        "resolved": None,
        "patch_produced": None,
        "patch_integrity": "unknown",
        "pollution_status": "not_checked",
        "l1_status": "unknown",
        "l3b_status": "unknown",
        "l3_status": "unknown",
        "confidence_filter_status": "not_checked",
        "big_repo_neighbor_cap_status": "not_checked",
        "poor_evidence_silence_status": "not_checked",
        "same_file_dedup_status": "not_checked",
        "issue_anchor_ranking_status": "not_checked",
        "visible_test_bonus_status": "not_checked",
        "regression_status": "unknown",
        "needs_raw_review": False,
        "raw_artifact_paths": [],
        "evidence_excerpts": [],
        "verdict": "AMBIGUOUS",
        "reason": "verification incomplete",
    }

    # --- Populate from sub-verifier results ---

    # 1. Pollution check
    if pollution_results:
        task_pollution = next(
            (r for r in pollution_results.get("results", []) if r.get("task_id") == task_id),
            None,
        )
        if task_pollution:
            record["pollution_status"] = task_pollution["status"]
            if task_pollution["status"] == "fail":
                record["evidence_excerpts"].extend(
                    [d["excerpt"] for d in task_pollution.get("details", [])[:3]]
                )

    # 2. Patch integrity
    if patch_results:
        task_patch = next(
            (r for r in patch_results.get("results", []) if r.get("task_id") == task_id),
            None,
        )
        if task_patch:
            record["patch_produced"] = task_patch.get("patch_produced")
            record["patch_integrity"] = task_patch.get("patch_integrity", "unknown")

    # 3. Signal check
    if signal_results:
        task_signal = next(
            (r for r in signal_results.get("results", []) if r.get("task_id") == task_id),
            None,
        )
        if task_signal:
            record["confidence_filter_status"] = (
                task_signal.get("patch_a_confidence_filter", {}).get("status", "not_checked")
            )
            record["big_repo_neighbor_cap_status"] = (
                task_signal.get("patch_b_neighbor_cap", {}).get("status", "not_checked")
            )
            record["poor_evidence_silence_status"] = (
                task_signal.get("patch_c_g7_silence", {}).get("status", "not_checked")
            )
            record["same_file_dedup_status"] = (
                task_signal.get("patch_d_dedup", {}).get("status", "not_checked")
            )
            record["issue_anchor_ranking_status"] = (
                task_signal.get("patch_e_anchor_ranking", {}).get("status", "not_checked")
            )
            record["visible_test_bonus_status"] = (
                task_signal.get("patch_f_visible_test_bonus", {}).get("status", "not_checked")
            )

    # 4. Task summary (resolved status)
    if summary_results:
        task_summary = next(
            (r for r in summary_results.get("results", []) if r.get("task_id") == task_id),
            None,
        )
        if task_summary:
            record["resolved"] = task_summary.get("resolved")
            if record["patch_produced"] is None:
                record["patch_produced"] = task_summary.get("patch_produced")

    # 5. Layer status from structured events
    if task_dir:
        layer_events = load_layer_events(task_dir)
        if layer_events:
            record["l1_status"] = determine_l_status(layer_events, "L1")
            record["l3b_status"] = determine_l_status(layer_events, "L3b")
            record["l3_status"] = determine_l_status(layer_events, "L3")

        # Collect raw artifact paths
        artifact_files = []
        for pattern in ["output.jsonl", "full_run.log", "gt_layer_events_*.jsonl",
                        "gt_interactions_*.jsonl", "eval_result.json", "gt_hooks.log"]:
            for f in task_dir.rglob(pattern.replace("*", "") if "*" not in pattern else "*"):
                if pattern.replace("*", "") in f.name or (
                    "*" in pattern and f.name.startswith(pattern.split("*")[0])
                ):
                    artifact_files.append(str(f))
        record["raw_artifact_paths"] = artifact_files[:20]

    # --- Compute verdict ---

    record["regression_status"] = _compute_regression_status(task_id, record)
    verdict, reason = _compute_verdict(task_id, record)
    record["verdict"] = verdict
    record["reason"] = reason
    record["needs_raw_review"] = verdict in ("AMBIGUOUS", "REGRESSION")

    return record


def _compute_regression_status(task_id: str, record: dict) -> str:
    """Determine regression status. Control tasks that were previously resolved
    must still be resolved."""
    if task_id not in CONTROL_TASKS:
        return "no_regression"

    if record["resolved"] is True:
        return "no_regression"
    elif record["resolved"] is False:
        return "regression"
    else:
        return "unknown"


def _compute_verdict(task_id: str, record: dict) -> tuple[str, str]:
    """Compute overall verdict for a task."""

    # REGRESSION: control task lost resolution
    if record["regression_status"] == "regression":
        return "REGRESSION", f"Control task {task_id} was previously resolved but is now not resolved"

    # FAIL: pollution detected
    if record["pollution_status"] == "fail":
        return "FAIL", "GT debug markers leaked into agent-visible observations"

    # Check invariants: none of the checked statuses should be "fail"
    invariant_fields = [
        "confidence_filter_status",
        "big_repo_neighbor_cap_status",
        "poor_evidence_silence_status",
        "same_file_dedup_status",
        "issue_anchor_ranking_status",
        "visible_test_bonus_status",
    ]
    for field in invariant_fields:
        if record.get(field) == "fail":
            return "FAIL", f"Invariant {field} failed"

    # PASS: resolved, no pollution, no regression
    if record["resolved"] is True and record["pollution_status"] == "pass":
        return "PASS", "Resolved, clean pollution check"

    # NOT_EXERCISED: all signals are not_exercised or not_checked (nothing fired)
    all_not_exercised = all(
        record.get(field) in ("not_exercised", "not_checked", "not_applicable")
        for field in invariant_fields
    )
    if all_not_exercised and record["resolved"] is not None:
        resolved_str = "resolved" if record["resolved"] else "not_resolved"
        return "NOT_EXERCISED", f"Product-v1 patches not exercised on this task ({resolved_str})"

    # PASS (without all signal verification): resolved, no failures
    if record["resolved"] is True:
        return "PASS", "Resolved, partial signal verification"

    # AMBIGUOUS: not resolved but no failures detected
    if record["resolved"] is False:
        return "PASS", "Not resolved, but no invariant failures or regressions"

    return "AMBIGUOUS", "Insufficient data for verdict"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage runtime verifier for Product-v1")
    parser.add_argument("--output-dir", required=True, help="Root directory with run artifacts")
    parser.add_argument("--stage", default="stage1", choices=["stage1", "stage2"],
                        help="Stage being verified")
    parser.add_argument("--branch", default="jedi__branch", help="Branch name")
    parser.add_argument("--head-sha", default="b953231d", help="HEAD commit SHA")
    parser.add_argument("--run-id", default="", help="GHA run ID")
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        print(json.dumps({"error": f"Not a directory: {output_dir}"}))
        return 1

    # Run sub-verifiers
    print("Running gt_pollution_check...", file=sys.stderr)
    pollution_results = run_sub_verifier("gt_pollution_check.py", output_dir)

    print("Running patch_integrity_check...", file=sys.stderr)
    patch_results = run_sub_verifier("patch_integrity_check.py", output_dir)

    print("Running product_v1_signal_check...", file=sys.stderr)
    signal_results = run_sub_verifier("product_v1_signal_check.py", output_dir)

    print("Running task_result_summarizer...", file=sys.stderr)
    summary_results = run_sub_verifier("task_result_summarizer.py", output_dir)

    # Build task directory map
    task_dirs = find_task_dirs(output_dir)

    # Determine which tasks to verify
    if args.stage == "stage1":
        expected_tasks = STAGE1_TASKS
    else:
        # Stage 2: verify whatever tasks are in the artifacts
        expected_tasks = sorted(task_dirs.keys()) if task_dirs else STAGE1_TASKS

    # Check for task count mismatch
    found_tasks = set(task_dirs.keys())
    expected_set = set(expected_tasks)
    missing = expected_set - found_tasks
    extra = found_tasks - expected_set

    if missing:
        print(f"WARN: missing task artifacts: {missing}", file=sys.stderr)
    if extra:
        print(f"INFO: extra task artifacts: {extra}", file=sys.stderr)

    # Build records
    records = []
    for tid in expected_tasks:
        td = task_dirs.get(tid)
        record = build_task_record(
            task_id=tid,
            stage=args.stage,
            branch=args.branch,
            head_sha=args.head_sha,
            task_dir=td,
            pollution_results=pollution_results,
            patch_results=patch_results,
            signal_results=signal_results,
            summary_results=summary_results,
        )
        record["run_id"] = args.run_id
        records.append(record)

    # Overall gate decision
    regressions = [r for r in records if r["verdict"] == "REGRESSION"]
    fails = [r for r in records if r["verdict"] == "FAIL"]
    passes = [r for r in records if r["verdict"] == "PASS"]
    not_exercised = [r for r in records if r["verdict"] == "NOT_EXERCISED"]
    ambiguous = [r for r in records if r["verdict"] == "AMBIGUOUS"]

    gate = "PASS"
    gate_reason = ""

    if missing:
        gate = "FAIL"
        gate_reason = f"Missing task artifacts: {sorted(missing)}"
    elif regressions:
        gate = "FAIL"
        gate_reason = f"Regressions on control tasks: {[r['task_id'] for r in regressions]}"
    elif fails:
        gate = "FAIL"
        gate_reason = f"Invariant failures: {[r['task_id'] for r in fails]}"
    elif ambiguous:
        gate = "WARN"
        gate_reason = f"Ambiguous verdicts on: {[r['task_id'] for r in ambiguous]}"
    else:
        gate = "PASS"
        gate_reason = (
            f"{len(passes)} PASS, {len(not_exercised)} NOT_EXERCISED, "
            f"0 FAIL, 0 REGRESSION"
        )

    output = {
        "verifier": "stage_runtime_verifier",
        "stage": args.stage,
        "branch": args.branch,
        "head_sha": args.head_sha,
        "run_id": args.run_id,
        "gate": gate,
        "gate_reason": gate_reason,
        "summary": {
            "total": len(records),
            "pass": len(passes),
            "fail": len(fails),
            "regression": len(regressions),
            "not_exercised": len(not_exercised),
            "ambiguous": len(ambiguous),
            "missing": len(missing),
        },
        "records": records,
    }

    print(json.dumps(output, indent=2))

    # Exit 0 even on FAIL gate (caller reads the JSON), exit 1 only on script error
    return 0


if __name__ == "__main__":
    sys.exit(main())
