#!/usr/bin/env python3
"""Full trajectory autopsy — parse output.jsonl line by line, reconstruct
what GT generated vs what the agent actually saw vs what the agent did next.

Usage:
    python scripts/gt_autopsy.py <task_directory>

Produces autopsy.json and report.md in the task directory.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

# GT markers by layer
LAYER_MARKERS = {
    "L1_BRIEF": ["<gt-task-brief>"],
    "L1_EDIT_TARGET": ["<gt-edit-target>", "[GT EDIT PLAN]"],
    "L1_KEY_CONTRACTS": ["[GT KEY CONTRACTS]"],
    "L3_POST_EDIT": [
        '<gt-evidence trigger="post_edit',
        "[GT] Post-edit:",
        "[SIGNATURE]",
        "[BEHAVIORAL CONTRACT]",
        "PRESERVE:",
        "[CALLERS]",
        "Calls into:",
        "[TEST]",
        "[COMPLETENESS]",
        "[PATTERN]",
        "[PEER]",
        "[SIMILAR]",
        "[OVERRIDE]",
        "[MISMATCH]",
        "SEMANTIC WARNING:",
    ],
    "L3B_POST_VIEW": [
        "Called by:",
        "Calls into:",
        "[CATCHES]",
        "[RAISES]",
    ],
    "L4A_AUTO_QUERY": ["[GT_AUTO]"],
    "GREP_INTERCEPT": ["[GT] Callers of"],
    "L5_SCAFFOLD": ["<gt-advisory", "[GT L5: No Source Edits]", "[GT L5: Scaffolding"],
    "L5B_REMINDER": ["[GT L5: Scope Check]", "[GT L5: Ignored Structural Witness]"],
    "L6_PRESUBMIT": ["[REVIEW]", "[PRE-SUBMIT"],
    "CONSENSUS": ["<gt-scope", "[GT] Scope"],
}

HIDDEN_PREFIXES = (
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]",
    "[GT_DELIVERY]", "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]",
    "[GT_SUMMARY]",
)


@dataclass
class LayerResult:
    layer: str
    expected: str = "yes"
    generated: bool = False
    generated_evidence: str = ""
    visible_in_output: bool = False
    visible_text: str = ""
    visible_entry_idx: int = -1
    agent_reacted: bool = False
    agent_next_action: str = ""
    status: str = "NOT_FIRED"
    failure_class: str = ""
    markers_found: list[str] = field(default_factory=list)
    events_from_log: int = 0


@dataclass
class AutopsyResult:
    task_id: str
    directory: str
    total_history_entries: int = 0
    total_agent_actions: int = 0
    total_gt_injections: int = 0
    layers: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    candidate_bugs: list[dict] = field(default_factory=list)
    hidden_prefix_leaks: list[dict] = field(default_factory=list)


def find_file(directory: str, pattern: str) -> str | None:
    for f in os.listdir(directory):
        if fnmatch.fnmatch(f, pattern):
            return os.path.join(directory, f)
    for root, _, files in os.walk(directory):
        for f in files:
            if fnmatch.fnmatch(f, pattern):
                return os.path.join(root, f)
    return None


def parse_output_jsonl(path: str) -> list[dict]:
    """Parse output.jsonl — may be single JSON with history array or JSONL."""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "history" in obj:
                    return obj["history"]
                if isinstance(obj, list):
                    return obj
                entries.append(obj)
            except json.JSONDecodeError:
                continue
    return entries


def extract_text(entry: dict) -> str:
    """Get all text content from a history entry."""
    parts = []
    for key in ("content", "observation", "text", "message"):
        v = entry.get(key, "")
        if isinstance(v, str) and v:
            parts.append(v)
    extras = entry.get("extras", {})
    if isinstance(extras, dict):
        for key in ("content", "observation", "thought"):
            v = extras.get(key, "")
            if isinstance(v, str) and v:
                parts.append(v)
    return "\n".join(parts)


def get_action_type(entry: dict) -> str:
    """Extract action type string."""
    action = entry.get("action", "")
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        return action.get("action", action.get("type", "unknown"))
    return str(type(action).__name__) if action else ""


def get_action_text(entry: dict) -> str:
    """Extract short action description."""
    for key in ("args", "content", "text", "command"):
        v = entry.get(key, "")
        if isinstance(v, str) and v:
            return v[:200]
        if isinstance(v, dict):
            for subkey in ("command", "content", "path", "thought"):
                sv = v.get(subkey, "")
                if isinstance(sv, str) and sv:
                    return sv[:200]
    return ""


def classify_layer(text: str) -> list[tuple[str, str]]:
    """Identify which GT layers are present in text. Returns [(layer, marker)]."""
    found = []
    for layer, markers in LAYER_MARKERS.items():
        for marker in markers:
            if marker in text:
                found.append((layer, marker))
                break
    return found


def check_hidden_leaks(text: str) -> list[str]:
    """Check if hidden prefixes leaked into agent-visible text."""
    leaks = []
    for prefix in HIDDEN_PREFIXES:
        if prefix in text:
            leaks.append(prefix)
    return leaks


def parse_gt_layer_events(path: str) -> list[dict]:
    """Parse gt_layer_events JSONL."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def run_autopsy(directory: str) -> AutopsyResult:
    """Full trajectory autopsy of a task directory."""
    task_id = os.path.basename(directory.rstrip("/\\"))
    result = AutopsyResult(task_id=task_id, directory=directory)

    # Find output.jsonl
    output_path = find_file(directory, "output.jsonl")
    if not output_path:
        result.divergences.append("FATAL: output.jsonl not found")
        return result

    history = parse_output_jsonl(output_path)
    result.total_history_entries = len(history)

    # Parse gt_layer_events if available
    events_path = find_file(directory, "gt_layer_events*.jsonl")
    gt_events = parse_gt_layer_events(events_path) if events_path else []

    # Initialize layer results
    for layer in LAYER_MARKERS:
        lr = LayerResult(layer=layer)
        if layer == "L5B_REMINDER":
            lr.expected = "suppressed"
        elif layer == "L6_PRESUBMIT":
            lr.expected = "broken(OH)"
        elif layer in ("GREP_INTERCEPT", "L5_SCAFFOLD", "CONSENSUS"):
            lr.expected = "conditional"
        result.layers[layer] = lr

    # Map gt_layer_events layer names to autopsy layer keys
    event_layer_map = {
        "L1": "L1_BRIEF",
        "L1_brief": "L1_BRIEF",
        "L1_edit_target": "L1_EDIT_TARGET",
        "L1_key_contracts": "L1_KEY_CONTRACTS",
        "L3": "L3_POST_EDIT",
        "L3_post_edit": "L3_POST_EDIT",
        "L3_router_v2": "L3B_POST_VIEW",  # router_v2 on_view events are L3b
        "L3b": "L3B_POST_VIEW",
        "L3b_post_view": "L3B_POST_VIEW",
        "L4": "L4A_AUTO_QUERY",
        "L4a": "L4A_AUTO_QUERY",
        "L5": "L5_SCAFFOLD",
        "L5_governor": "L5_SCAFFOLD",
        "L5b": "L5B_REMINDER",
        "L6": "L6_PRESUBMIT",
        "L6_pre_submit": "L6_PRESUBMIT",
        "grep_intercept": "GREP_INTERCEPT",
        "consensus": "CONSENSUS",
    }

    for ev in gt_events:
        layer_name = ev.get("layer", "")
        event_type = ev.get("event_type", "")
        emitted = ev.get("emitted", False)

        # Determine autopsy layer key
        autopsy_key = event_layer_map.get(layer_name)

        # Refine: L3_router_v2 with on_edit is L3_POST_EDIT, on_view is L3B_POST_VIEW
        if layer_name == "L3_router_v2":
            if event_type == "on_edit":
                autopsy_key = "L3_POST_EDIT"
            elif event_type == "on_view":
                autopsy_key = "L3B_POST_VIEW"

        # L1 event_type refinement
        if layer_name == "L1" and event_type == "edit_target":
            autopsy_key = "L1_EDIT_TARGET"
        if layer_name == "L1" and event_type == "key_contracts":
            autopsy_key = "L1_KEY_CONTRACTS"

        # L5 event_type refinement: "ignored_next_action" and
        # "multi_file_scope_warning" and "intervention_*" are L5b, not L5 scaffold
        if layer_name == "L5" and event_type in (
            "ignored_next_action",
            "multi_file_scope_warning",
            "intervention_multi_file_scope_warning",
        ):
            autopsy_key = "L5B_REMINDER"

        # L5b events always map to L5B_REMINDER
        if layer_name == "L5b":
            autopsy_key = "L5B_REMINDER"

        # L6 reindex events are NOT pre-submit — skip them for L6_PRESUBMIT tracking
        if layer_name == "L6" and event_type == "reindex":
            autopsy_key = None  # reindex is infrastructure, not pre-submit delivery

        if autopsy_key and autopsy_key in result.layers:
            result.layers[autopsy_key].events_from_log += 1
            if emitted:
                result.layers[autopsy_key].generated = True

    # Walk history entry by entry
    for i, entry in enumerate(history):
        text = extract_text(entry)
        action_type = get_action_type(entry)
        action_text = get_action_text(entry)

        if not text and not action_type:
            continue

        is_observation = "observation" in str(entry.get("role", "")).lower() or \
                         "observation" in action_type.lower() or \
                         bool(entry.get("observation"))

        # Track agent actions
        if action_type and not is_observation:
            result.total_agent_actions += 1

        # Check for GT content in observations
        if text:
            layer_hits = classify_layer(text)
            leaks = check_hidden_leaks(text)

            if leaks:
                result.hidden_prefix_leaks.append({
                    "entry_idx": i,
                    "prefixes": leaks,
                    "context": text[:200],
                })

            if layer_hits:
                result.total_gt_injections += 1

                for layer, marker in layer_hits:
                    lr = result.layers[layer]
                    lr.visible_in_output = True
                    lr.markers_found.append(marker)
                    if lr.visible_entry_idx < 0:
                        lr.visible_entry_idx = i
                    lr.visible_text = text[:500]
                    lr.status = "DELIVERED"

                    # Check agent reaction (next non-observation entry)
                    for j in range(i + 1, min(i + 5, len(history))):
                        next_type = get_action_type(history[j])
                        next_text = get_action_text(history[j])
                        if next_type and "observation" not in next_type.lower():
                            lr.agent_reacted = True
                            lr.agent_next_action = f"[{next_type}] {next_text[:100]}"
                            break

            # Record timeline entry for GT-relevant events
            if layer_hits or action_type:
                timeline_entry = {
                    "idx": i,
                    "type": action_type or "observation",
                    "gt_layers": [l for l, _ in layer_hits],
                    "text_preview": (action_text or text)[:150],
                }
                if layer_hits:
                    timeline_entry["gt_markers"] = [m for _, m in layer_hits]
                result.timeline.append(timeline_entry)

    # Post-process: identify divergences
    for layer_key, lr in result.layers.items():
        if lr.generated and not lr.visible_in_output:
            lr.status = "LOST"
            lr.failure_class = "G1"
            result.divergences.append(
                f"{layer_key}: gt_layer_events says generated but output.jsonl lacks evidence"
            )
            result.candidate_bugs.append({
                "bug": f"{layer_key} generated but not visible",
                "failure_class": "G1",
                "known_prior": "",
                "evidence": "events say generated, output.jsonl missing markers",
            })

        if not lr.generated and not lr.visible_in_output and lr.expected == "yes":
            lr.status = "NOT_FIRED"
            lr.failure_class = "D1"

        if lr.visible_in_output and lr.expected == "broken(OH)":
            result.divergences.append(
                f"{layer_key}: marked broken but evidence IS visible — DOC_OF_HONOR status wrong?"
            )

    # Check for hidden prefix leaks
    if result.hidden_prefix_leaks:
        result.candidate_bugs.append({
            "bug": f"Hidden prefix leak: {len(result.hidden_prefix_leaks)} entries",
            "failure_class": "E3",
            "known_prior": "",
            "evidence": f"Prefixes leaked: {set(p for leak in result.hidden_prefix_leaks for p in leak['prefixes'])}",
        })

    return result


def write_report(result: AutopsyResult, directory: str) -> None:
    """Write markdown report."""
    lines = []
    lines.append(f"# Task Report: {result.task_id}")
    lines.append("")
    lines.append("## Run Info")
    lines.append(f"- Directory: {result.directory}")
    lines.append(f"- History entries: {result.total_history_entries}")
    lines.append(f"- Agent actions: {result.total_agent_actions}")
    lines.append(f"- GT injections visible: {result.total_gt_injections}")
    lines.append("")

    lines.append("## Architecture Matrix")
    lines.append("")
    lines.append("| Layer | Expected | Generated | Visible | Agent reacted | Status | Failure class |")
    lines.append("|-------|----------|-----------|---------|---------------|--------|---------------|")
    for layer_key, lr in result.layers.items():
        gen = "yes" if lr.generated else "no"
        vis = "yes" if lr.visible_in_output else "no"
        react = "yes" if lr.agent_reacted else ("n/a" if not lr.visible_in_output else "no")
        lines.append(
            f"| {layer_key} | {lr.expected} | {gen} | {vis} | {react} | {lr.status} | {lr.failure_class} |"
        )
    lines.append("")

    if result.divergences:
        lines.append("## Divergence Points")
        lines.append("")
        for d in result.divergences:
            lines.append(f"- {d}")
        lines.append("")

    if result.candidate_bugs:
        lines.append("## Candidate Bugs")
        lines.append("")
        lines.append("| Bug | Failure class | Known prior | Evidence |")
        lines.append("|-----|---------------|-------------|----------|")
        for bug in result.candidate_bugs:
            lines.append(
                f"| {bug['bug']} | {bug['failure_class']} | {bug.get('known_prior', '')} | {bug['evidence'][:80]} |"
            )
        lines.append("")

    if result.hidden_prefix_leaks:
        lines.append("## Hidden Prefix Leaks")
        lines.append("")
        for leak in result.hidden_prefix_leaks[:5]:
            lines.append(f"- Entry {leak['entry_idx']}: {leak['prefixes']}")
        lines.append("")

    if result.timeline:
        lines.append("## Timeline (GT-relevant events)")
        lines.append("")
        for t in result.timeline[:50]:
            gt = f" GT:{','.join(t['gt_layers'])}" if t.get("gt_layers") else ""
            lines.append(f"- [{t['idx']}] {t['type']}{gt}: {t['text_preview'][:100]}")
        if len(result.timeline) > 50:
            lines.append(f"- ... ({len(result.timeline) - 50} more entries)")
        lines.append("")

    report_path = os.path.join(directory, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Report written to: {report_path}")


def write_json(result: AutopsyResult, directory: str) -> None:
    """Write machine-readable autopsy JSON."""
    out = {
        "task_id": result.task_id,
        "directory": result.directory,
        "total_history_entries": result.total_history_entries,
        "total_agent_actions": result.total_agent_actions,
        "total_gt_injections": result.total_gt_injections,
        "layers": {},
        "divergences": result.divergences,
        "candidate_bugs": result.candidate_bugs,
        "hidden_prefix_leaks": result.hidden_prefix_leaks,
    }
    for layer_key, lr in result.layers.items():
        out["layers"][layer_key] = {
            "expected": lr.expected,
            "generated": lr.generated,
            "visible_in_output": lr.visible_in_output,
            "visible_entry_idx": lr.visible_entry_idx,
            "agent_reacted": lr.agent_reacted,
            "agent_next_action": lr.agent_next_action,
            "status": lr.status,
            "failure_class": lr.failure_class,
            "markers_found": lr.markers_found,
            "events_from_log": lr.events_from_log,
        }

    json_path = os.path.join(directory, "autopsy.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"Autopsy JSON written to: {json_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/gt_autopsy.py <task_directory>")
        sys.exit(1)

    directory = sys.argv[1]
    if not os.path.isdir(directory):
        print(f"ERROR: Not a directory: {directory}")
        sys.exit(1)

    result = run_autopsy(directory)

    print(f"\nAutopsy: {result.task_id}")
    print(f"  History entries: {result.total_history_entries}")
    print(f"  Agent actions: {result.total_agent_actions}")
    print(f"  GT injections visible: {result.total_gt_injections}")
    print(f"  Divergences: {len(result.divergences)}")
    print(f"  Candidate bugs: {len(result.candidate_bugs)}")

    write_json(result, directory)
    write_report(result, directory)


if __name__ == "__main__":
    main()
