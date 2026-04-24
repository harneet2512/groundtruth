"""Classify a GT trace for regression autopsy.

This classifier answers: did the agent follow GT's steer, and was the steer
useful? It does NOT read any implementation internals of the hook or reporter.
It reads a frozen telemetry event stream and produces a classification dict.

The expected outputs are defined in tests/fixtures/trajectory/EXPECTED_BEHAVIOR.md,
derived from frozen artifacts before this code was written.
"""
from __future__ import annotations

import os
from pathlib import Path


def classify_trace(events: list[dict], *, gold_files: list[str] | None = None) -> dict:
    """Classify a telemetry event stream for regression autopsy.

    Args:
        events: list of dicts from gt_hook_telemetry.jsonl
        gold_files: optional list of file paths the gold/expected patch touches

    Returns dict with:
        behavioral_alignment: int — count of post-steer edits that target a
            previously-steered file
        steer_targets_gold_file: bool | None — True if any steer targets a file
            in gold_files; None if gold_files not provided
        low_information_confirmation: bool — True if on the first steer's cycle,
            the agent had already edited or was editing the same file
        repeated_steer_count: int — count of steers that target a file already
            steered earlier in the trace AND already edited by the agent
        agent_noncompliance: bool — True only if steer was specific, timely,
            and agent took a conflicting action (edited a DIFFERENT file after)
        possible_noncompliance: bool — True if any post-steer edit targets a
            file NOT mentioned in any prior steer
        failure_class: str — one of the defined failure classes
        excluded_from_steer_effectiveness: bool — True if this trace should
            not be used for steer effectiveness analysis
        steer_harmful: bool — reserved for cases where following steer made
            things worse; requires external baseline comparison
    """
    steers = []
    edits = []
    all_events = []

    for ev in events:
        e = ev.get("event", "")
        cycle = ev.get("cycle", 0)
        all_events.append((e, cycle, ev))

        if e == "steer_delivered":
            steers.append({
                "cycle": cycle,
                "file": _normalize_path(ev.get("file", "")),
                "channel": ev.get("channel", ""),
            })
        elif e == "material_edit":
            files = ev.get("files", [])
            for f in files:
                edits.append({
                    "cycle": cycle,
                    "file": _normalize_path(f),
                })

    # --- Bootstrap failure detection ---
    has_material_edit = len(edits) > 0
    has_steer = len(steers) > 0
    max_cycle = max((c for _, c, _ in all_events), default=0)

    if not has_material_edit and max_cycle <= 2:
        return {
            "behavioral_alignment": 0,
            "steer_targets_gold_file": None,
            "low_information_confirmation": False,
            "repeated_steer_count": 0,
            "agent_noncompliance": False,
            "possible_noncompliance": False,
            "failure_class": "bootstrap_infra_failure",
            "excluded_from_steer_effectiveness": True,
            "steer_harmful": False,
        }

    # --- Behavioral alignment ---
    # Count post-steer edits that target a file previously steered.
    steered_files_by_cycle: dict[str, int] = {}
    behavioral_alignment = 0

    for s in steers:
        sf = s["file"]
        if sf:
            steered_files_by_cycle.setdefault(sf, s["cycle"])

    for ed in edits:
        ef = ed["file"]
        if ef in steered_files_by_cycle:
            steer_cycle = steered_files_by_cycle[ef]
            if ed["cycle"] >= steer_cycle:
                behavioral_alignment += 1

    # --- Steer relevance (vs gold patch) ---
    steer_targets_gold_file = None
    if gold_files is not None:
        gold_normalized = {_normalize_path(f) for f in gold_files}
        steer_files = {s["file"] for s in steers if s["file"]}
        steer_targets_gold_file = bool(steer_files & gold_normalized)

    # --- Low-information confirmation ---
    # First steer arrived at cycle N. Was the agent already editing that
    # file at cycle N or earlier?
    low_information_confirmation = False
    if steers:
        first_steer = steers[0]
        fs_file = first_steer["file"]
        fs_cycle = first_steer["cycle"]
        for ed in edits:
            if ed["file"] == fs_file and ed["cycle"] <= fs_cycle:
                low_information_confirmation = True
                break

    # --- Steer repetition ---
    # A steer is "repeated" if the same file was already steered earlier
    # AND the agent has already demonstrated alignment (edited that file).
    seen_steer_files: set[str] = set()
    aligned_files = {ed["file"] for ed in edits}
    repeated_steer_count = 0

    for s in steers:
        sf = s["file"]
        if sf in seen_steer_files and sf in aligned_files:
            repeated_steer_count += 1
        seen_steer_files.add(sf)

    # --- Noncompliance ---
    # True ONLY if: steer is specific (has a file), steer arrives before
    # an edit, and the agent's next edit targets a DIFFERENT file.
    agent_noncompliance = False
    possible_noncompliance = False

    if steers and edits:
        steered_file_set = {s["file"] for s in steers if s["file"]}
        for ed in edits:
            # Only check edits that come AFTER at least one steer
            if any(s["cycle"] <= ed["cycle"] for s in steers):
                if ed["file"] not in steered_file_set:
                    possible_noncompliance = True

        # Full noncompliance: steer specific + timely + agent conflicts
        # AND agent never edited the steered file at all
        if possible_noncompliance and behavioral_alignment == 0:
            agent_noncompliance = True

    # --- Failure class ---
    if not has_material_edit:
        failure_class = "bootstrap_infra_failure"
    elif not has_steer:
        failure_class = "no_steer_delivered"
    elif agent_noncompliance:
        failure_class = "agent_noncompliance"
    elif behavioral_alignment > 0 and repeated_steer_count >= 3:
        failure_class = "steer_too_noisy"
    elif behavioral_alignment > 0 and low_information_confirmation:
        failure_class = "steer_low_information"
    elif behavioral_alignment > 0:
        failure_class = "baseline_variance"
    else:
        failure_class = "unknown"

    return {
        "behavioral_alignment": behavioral_alignment,
        "steer_targets_gold_file": steer_targets_gold_file,
        "low_information_confirmation": low_information_confirmation,
        "repeated_steer_count": repeated_steer_count,
        "agent_noncompliance": agent_noncompliance,
        "possible_noncompliance": possible_noncompliance,
        "failure_class": failure_class,
        "excluded_from_steer_effectiveness": not has_steer or not has_material_edit,
        "steer_harmful": False,
    }


def _normalize_path(p: str) -> str:
    """Normalize a file path for comparison."""
    if not p:
        return ""
    return p.strip().replace("\\", "/").lstrip("/")
