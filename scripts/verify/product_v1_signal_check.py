#!/usr/bin/env python3
"""product_v1_signal_check.py -- Verify Product-v1 patch signals in GT structured events.

Checks GT layer event logs and hook logs for evidence that each Product-v1 patch
(A through F) was exercised during the run:

  A. Confidence filter >= 0.7 on CALLS edges, >= 0.5 on IMPORTS/EXTENDS
  B. Big-repo neighbor cap (limit=3 when nodes > 5000)
  C. G7 silence gate (zero output for isolated functions)
  D. Normalized per-file evidence dedup (sort+strip before MD5)
  E. Issue-anchor ranking (callers sorted by anchor relevance)
  F. Visible-test bonus (test_names extracted, assertion lines included)

For each patch, reports: fired / not_exercised / not_checked.

Usage:
    python scripts/verify/product_v1_signal_check.py --output-dir /path/to/artifacts

Output: JSON to stdout.
Exit 0 on success, 1 on script error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def find_files(output_dir: str, pattern: str) -> list[Path]:
    """Find files matching a glob-like pattern in the output directory."""
    results = []
    for root, _dirs, files in os.walk(output_dir):
        for f in files:
            if _matches_pattern(f, pattern):
                results.append(Path(root) / f)
    return sorted(results)


def _matches_pattern(filename: str, pattern: str) -> bool:
    """Simple pattern matching: supports * as wildcard."""
    if "*" not in pattern:
        return filename == pattern
    parts = pattern.split("*")
    if len(parts) == 2:
        return filename.startswith(parts[0]) and filename.endswith(parts[1])
    return pattern.replace("*", "") in filename


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, returning list of parsed records."""
    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def read_text(path: Path) -> str:
    """Read a text file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def infer_task_id_from_path(path: Path) -> str:
    """Infer task ID from artifact path."""
    for part in path.parts:
        if part.startswith("task-"):
            return part.replace("task-", "", 1)
        if "__" in part and any(c.isdigit() for c in part):
            return part
    return path.parent.name


def check_patch_a_confidence_filter(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch A: confidence filter >= 0.7 on CALLS edges.

    Evidence: SQL queries contain 'confidence >= 0.7' or structured events
    mention confidence filtering.
    """
    signals = []

    # Check structured events for confidence-related fields
    for evt in layer_events:
        conf_score = evt.get("confidence_score")
        conf_level = evt.get("confidence_level")
        if conf_score is not None or conf_level is not None:
            signals.append(f"event: conf_score={conf_score}, conf_level={conf_level}")

    # Check hook logs for confidence filter execution
    if "confidence >= 0.7" in hook_logs or "confidence >= 0.5" in hook_logs:
        signals.append("hook_log: confidence filter SQL present")

    # Check full run log for confidence evidence
    conf_matches = re.findall(r"confidence[>=\s]+0\.[0-9]+", full_run_log)
    if conf_matches:
        signals.append(f"run_log: {len(conf_matches)} confidence references")

    if signals:
        return {"status": "active", "signals": signals[:5]}
    return {"status": "not_checked", "signals": []}


def check_patch_b_neighbor_cap(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch B: big-repo neighbor cap (limit=3 when nodes > 5000).

    Evidence: node count > 5000 in logs AND reduced candidate limit visible.
    """
    signals = []

    # Check for node count mentions
    node_count_matches = re.findall(r"node_count[=:]\s*(\d+)", full_run_log)
    for m in node_count_matches:
        count = int(m)
        if count > 5000:
            signals.append(f"big_repo: node_count={count} (cap active)")
        else:
            signals.append(f"small_repo: node_count={count} (cap not_applicable)")

    # Check for explicit cap messages
    if "limit=3" in hook_logs or "neighbor_cap" in hook_logs:
        signals.append("hook_log: neighbor cap reference")

    if "node_count" in full_run_log and "5000" in full_run_log:
        signals.append("run_log: big-repo threshold check")

    if not signals:
        return {"status": "not_checked", "signals": []}

    is_big = any("big_repo" in s for s in signals)
    if is_big:
        return {"status": "active", "signals": signals[:5]}
    return {"status": "not_applicable", "signals": signals[:5]}


def check_patch_c_g7_silence(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch C: G7 silence gate for isolated functions.

    Evidence: GT_META g7_silence marker in stderr, or structured event with
    suppression_reason containing 'g7' or 'silence' or 'isolated'.
    """
    signals = []

    # Structured events: check for suppressed events mentioning g7/silence
    for evt in layer_events:
        if evt.get("suppressed") and evt.get("suppression_reason", ""):
            reason = evt["suppression_reason"].lower()
            if "g7" in reason or "silence" in reason or "isolated" in reason:
                signals.append(f"structured_event: suppressed reason={evt['suppression_reason']}")

    # Hook logs / run log: look for g7_silence marker
    g7_matches = re.findall(r"\[GT_META\] g7_silence:.*", full_run_log)
    for m in g7_matches[:3]:
        signals.append(f"run_log: {m.strip()[:120]}")

    if "g7_silence" in hook_logs:
        signals.append("hook_log: g7_silence marker present")

    if signals:
        return {"status": "fired", "signals": signals[:5]}
    return {"status": "not_exercised", "signals": []}


def check_patch_d_dedup(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch D: normalized per-file evidence dedup.

    Evidence: structured events with event_type containing 'dedup' or
    suppression_reason='duplicate'.
    """
    signals = []
    dedup_count = 0

    for evt in layer_events:
        et = (evt.get("event_type", "") or "").lower()
        sr = (evt.get("suppression_reason", "") or "").lower()
        if "dedup" in et or sr == "duplicate":
            dedup_count += 1
            if dedup_count <= 3:
                layer = evt.get("layer", "?")
                signals.append(f"structured_event: {layer} {et} reason={sr}")

    # Also check run log for dedup indicators
    dedup_log_matches = re.findall(r"\[dedup\]|\bdedup\b.*hash", full_run_log)
    if dedup_log_matches:
        signals.append(f"run_log: {len(dedup_log_matches)} dedup references")

    if dedup_count > 0:
        signals.insert(0, f"total_dedup_events: {dedup_count}")
        return {"status": "fired", "signals": signals[:5]}

    if signals:
        return {"status": "not_checked", "signals": signals[:5]}
    return {"status": "not_exercised", "signals": []}


def check_patch_e_anchor_ranking(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch E: issue-anchor ranking.

    Evidence: /tmp/gt_issue_anchors.json referenced, anchor symbols loaded,
    or structured events mentioning anchor ranking.
    """
    signals = []

    # Check for anchor file references
    if "gt_issue_anchors" in full_run_log:
        signals.append("run_log: gt_issue_anchors reference")

    if "gt_issue_anchors" in hook_logs:
        signals.append("hook_log: gt_issue_anchors reference")

    # Check for anchor symbols in structured events
    for evt in layer_events:
        et = (evt.get("event_type", "") or "").lower()
        if "anchor" in et:
            signals.append(f"structured_event: {evt.get('layer', '?')} {et}")

    # Check for issue terms extraction
    if "issue_terms" in full_run_log or "issue_anchors" in full_run_log:
        anchor_loads = re.findall(r"anchors?.*loaded|loaded.*anchors?", full_run_log, re.IGNORECASE)
        if anchor_loads:
            signals.append(f"run_log: anchor load ({anchor_loads[0][:80]})")

    if signals:
        return {"status": "active", "signals": signals[:5]}
    return {"status": "not_checked", "signals": []}


def check_patch_f_visible_test_bonus(
    layer_events: list[dict], hook_logs: str, full_run_log: str
) -> dict:
    """Patch F: visible-test bonus.

    Evidence: test_names extracted from anchors, assertion lines in evidence.
    """
    signals = []

    # Check for test_names in logs
    test_name_matches = re.findall(r"test_names.*\[.*\]", full_run_log)
    if test_name_matches:
        signals.append(f"run_log: test_names extracted ({test_name_matches[0][:80]})")

    # Check for assertion line evidence
    assertion_matches = re.findall(r"assert[A-Z]|self\.assert|pytest\.raises", full_run_log)
    if len(assertion_matches) > 0:
        signals.append(f"run_log: {len(assertion_matches)} assertion references in evidence")

    # Check structured events for test-related evidence
    for evt in layer_events:
        et = (evt.get("event_type", "") or "").lower()
        if "test" in et and ("bonus" in et or "visible" in et):
            signals.append(f"structured_event: {evt.get('layer', '?')} {et}")

    if "test_names" in hook_logs:
        signals.append("hook_log: test_names reference")

    if signals:
        return {"status": "fired", "signals": signals[:5]}
    return {"status": "not_exercised", "signals": []}


def analyze_task(output_dir: str, task_dir: Path) -> dict:
    """Analyze a single task's artifacts for Product-v1 signals."""
    task_id = infer_task_id_from_path(task_dir)

    # Collect all relevant files
    layer_event_files = find_files(str(task_dir), "gt_layer_events_*.jsonl")
    hook_log_files = find_files(str(task_dir), "gt_hooks.log")
    full_run_log_files = find_files(str(task_dir), "full_run.log")
    interaction_files = find_files(str(task_dir), "gt_interactions_*.jsonl")

    # Load data
    layer_events: list[dict] = []
    for f in layer_event_files:
        layer_events.extend(read_jsonl(f))

    hook_logs = ""
    for f in hook_log_files:
        hook_logs += read_text(f)

    full_run_log = ""
    for f in full_run_log_files:
        full_run_log += read_text(f)

    # If no structured events, also check interaction logs
    if not layer_events:
        for f in interaction_files:
            layer_events.extend(read_jsonl(f))

    return {
        "task_id": task_id,
        "artifacts_found": {
            "layer_event_files": len(layer_event_files),
            "hook_log_files": len(hook_log_files),
            "full_run_log_files": len(full_run_log_files),
            "interaction_files": len(interaction_files),
            "total_layer_events": len(layer_events),
        },
        "patch_a_confidence_filter": check_patch_a_confidence_filter(
            layer_events, hook_logs, full_run_log
        ),
        "patch_b_neighbor_cap": check_patch_b_neighbor_cap(
            layer_events, hook_logs, full_run_log
        ),
        "patch_c_g7_silence": check_patch_c_g7_silence(
            layer_events, hook_logs, full_run_log
        ),
        "patch_d_dedup": check_patch_d_dedup(
            layer_events, hook_logs, full_run_log
        ),
        "patch_e_anchor_ranking": check_patch_e_anchor_ranking(
            layer_events, hook_logs, full_run_log
        ),
        "patch_f_visible_test_bonus": check_patch_f_visible_test_bonus(
            layer_events, hook_logs, full_run_log
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Product-v1 patch signals in GT structured events"
    )
    parser.add_argument("--output-dir", required=True, help="Root directory with run artifacts")
    args = parser.parse_args()

    output_dir = args.output_dir
    if not os.path.isdir(output_dir):
        print(json.dumps({"error": f"Not a directory: {output_dir}"}))
        return 1

    # Find task directories (GHA artifact pattern: task-<id>/)
    task_dirs: list[Path] = []
    for entry in sorted(Path(output_dir).iterdir()):
        if entry.is_dir() and (
            entry.name.startswith("task-")
            or any(f.name == "output.jsonl" for f in entry.rglob("output.jsonl"))
        ):
            task_dirs.append(entry)

    # If no task subdirs, treat the whole output_dir as one task
    if not task_dirs:
        task_dirs = [Path(output_dir)]

    results = []
    for td in task_dirs:
        results.append(analyze_task(output_dir, td))

    # Summary: count how many patches were exercised across tasks
    patch_summary = {}
    for patch_key in [
        "patch_a_confidence_filter",
        "patch_b_neighbor_cap",
        "patch_c_g7_silence",
        "patch_d_dedup",
        "patch_e_anchor_ranking",
        "patch_f_visible_test_bonus",
    ]:
        statuses = [r[patch_key]["status"] for r in results]
        patch_summary[patch_key] = {
            "fired_or_active": sum(1 for s in statuses if s in ("fired", "active")),
            "not_applicable": sum(1 for s in statuses if s == "not_applicable"),
            "not_exercised": sum(1 for s in statuses if s == "not_exercised"),
            "not_checked": sum(1 for s in statuses if s == "not_checked"),
        }

    output = {
        "check": "product_v1_signal_check",
        "tasks_checked": len(results),
        "patch_summary": patch_summary,
        "results": results,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
