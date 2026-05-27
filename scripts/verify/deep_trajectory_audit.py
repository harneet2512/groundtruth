#!/usr/bin/env python3
"""Deep trajectory audit — reads output.jsonl line by line, extracts every GT
injection verbatim, and produces a per-injection analysis.

NOT a grep. Parses each JSON entry, extracts the full observation text,
finds GT content, quotes it, and reports what the agent did next.

Usage:
    python scripts/verify/deep_trajectory_audit.py <output.jsonl> [--max-chars 800]
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field


GT_MARKERS = [
    "<gt-task-brief>", "<gt-edit-target>", "<gt-orientation>",
    "<gt-evidence", "<gt-scope", "<gt-context",
    "[GT]", "[GT_AUTO]", "[GT_VERIFY",
    "[SIGNATURE]", "[CONTRACT]", "[TEST]", "[BEHAVIORAL CONTRACT]",
    "PRESERVE:", "[PATTERN]", "[PEER]", "[COMPLETENESS]",
    "[SIMILAR]", "[OVERRIDE]", "[MISMATCH]",
    "[RECALL]", "SEMANTIC WARNING:",
    "Called by:", "Calls into:",
    "[REVIEW]", "[PRE-SUBMIT",
    "[GT L5:", "No Source Edits",
    "SCOPE:", "[CATCHES]", "[RAISES]",
]


@dataclass
class Injection:
    entry_idx: int
    layer: str
    gt_text: str
    agent_next_action: str
    agent_next_type: str
    markers_found: list[str] = field(default_factory=list)


def parse_history(path: str) -> list[dict]:
    """Parse output.jsonl — single JSON line with history array."""
    with open(path, encoding="utf-8", errors="ignore") as f:
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
            except json.JSONDecodeError:
                continue
    return []


def extract_text(entry: dict) -> str:
    """Get the full text content from a history entry."""
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


def get_action_summary(entry: dict) -> tuple[str, str]:
    """Extract action type and brief description from a history entry."""
    action = entry.get("action", "")
    if isinstance(action, str):
        act_type = action
    elif isinstance(action, dict):
        act_type = action.get("action", action.get("type", "unknown"))
    else:
        act_type = str(type(action).__name__)

    # Try to get the action text
    text = ""
    for key in ("args", "content", "text", "command"):
        v = entry.get(key, "")
        if isinstance(v, str) and v:
            text = v[:200]
            break
        if isinstance(v, dict):
            for subkey in ("command", "content", "path", "thought"):
                sv = v.get(subkey, "")
                if isinstance(sv, str) and sv:
                    text = sv[:200]
                    break
            if text:
                break

    return act_type, text


def find_gt_content(text: str, max_chars: int = 800) -> list[tuple[str, str]]:
    """Find all GT injection blocks in text. Returns list of (marker, content)."""
    results = []
    for marker in GT_MARKERS:
        idx = text.find(marker)
        while idx >= 0:
            # Already found at this position?
            already = any(abs(idx - text.find(m, max(0, idx-10))) < 5
                         for m, _ in results if m != marker and text.find(m, max(0, idx-10)) >= 0)

            # Extract surrounding context
            start = max(0, idx - 50)
            end = min(len(text), idx + max_chars)
            block = text[start:end]

            if not any(block in existing for _, existing in results):
                results.append((marker, block))

            idx = text.find(marker, idx + len(marker))

    # Deduplicate overlapping blocks
    unique = []
    seen_starts = set()
    for marker, block in results:
        key = block[:100]
        if key not in seen_starts:
            seen_starts.add(key)
            unique.append((marker, block))

    return unique


def audit_trajectory(path: str, max_chars: int = 800) -> list[Injection]:
    """Full trajectory audit — find every GT injection and trace agent response."""
    history = parse_history(path)
    if not history:
        print(f"ERROR: no history found in {path}", file=sys.stderr)
        return []

    print(f"Parsed {len(history)} history entries from {os.path.basename(path)}")

    injections: list[Injection] = []

    for i, entry in enumerate(history):
        text = extract_text(entry)
        if not text:
            continue

        gt_blocks = find_gt_content(text, max_chars)
        if not gt_blocks:
            continue

        # Determine layer
        layer = "unknown"
        if "<gt-task-brief>" in text:
            layer = "L1 Brief"
        elif "<gt-edit-target>" in text:
            layer = "L1+ Edit-Target"
        elif "<gt-orientation>" in text:
            layer = "L1+ Orientation"
        elif '<gt-evidence trigger="post_edit' in text or "[GT] Post-edit:" in text:
            layer = "L3 Post-Edit"
        elif "SEMANTIC WARNING:" in text:
            layer = "L3 Semantic"
        elif "<gt-scope" in text:
            layer = "Consensus"
        elif "[GT L5:" in text or "No Source Edits" in text:
            layer = "L5 Governor"
        elif "[REVIEW]" in text or "[PRE-SUBMIT" in text:
            layer = "L6 Review"
        elif "[GT] Callers of" in text:
            layer = "Grep Intercept"
        elif "Called by:" in text or "Calls into:" in text or "[GT]" in text:
            layer = "L3b Post-View"
        elif "[GT_AUTO]" in text:
            layer = "L4a Auto-Query"

        # Get agent's next action
        next_type, next_text = "", ""
        for j in range(i + 1, min(i + 3, len(history))):
            nt, nx = get_action_summary(history[j])
            if nt and nt not in ("observation", ""):
                next_type, next_text = nt, nx
                break

        markers = [m for m, _ in gt_blocks]
        combined_text = "\n---\n".join(block for _, block in gt_blocks)

        injections.append(Injection(
            entry_idx=i,
            layer=layer,
            gt_text=combined_text[:max_chars * 2],
            agent_next_action=next_text,
            agent_next_type=next_type,
            markers_found=markers,
        ))

    return injections


def print_audit(injections: list[Injection], task_name: str = "") -> None:
    """Print the full audit report."""
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(f"\n{'='*70}")
    print(f"DEEP TRAJECTORY AUDIT: {task_name}")
    print(f"{'='*70}")
    print(f"Total GT injections found: {len(injections)}")

    for idx, inj in enumerate(injections):
        print(f"\n{'-'*70}")
        print(f"INJECTION #{idx+1} (entry {inj.entry_idx}) — {inj.layer}")
        print(f"Markers: {', '.join(inj.markers_found)}")
        print(f"Agent next: [{inj.agent_next_type}] {inj.agent_next_action[:150]}")
        print(f"GT Content ({len(inj.gt_text)} chars):")
        print(f"+{'-'*68}┐")
        for line in inj.gt_text.splitlines()[:30]:
            print(f"| {line[:66]:<66} |")
        if inj.gt_text.count('\n') > 30:
            print(f"| {'... (truncated)':<66} |")
        print(f"+{'-'*68}+")

    # Summary
    print(f"\n{'='*70}")
    print("LAYER SUMMARY")
    print(f"{'='*70}")
    layers: dict[str, int] = {}
    for inj in injections:
        layers[inj.layer] = layers.get(inj.layer, 0) + 1
    for layer, count in sorted(layers.items(), key=lambda x: -x[1]):
        print(f"  {layer:<25} {count} injections")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deep_trajectory_audit.py <output.jsonl> [--max-chars 800]")
        sys.exit(1)

    path = sys.argv[1]
    max_chars = 800
    if "--max-chars" in sys.argv:
        idx = sys.argv.index("--max-chars")
        if idx + 1 < len(sys.argv):
            max_chars = int(sys.argv[idx + 1])

    task_name = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path)))))
    injections = audit_trajectory(path, max_chars)
    print_audit(injections, task_name)
