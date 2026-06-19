#!/usr/bin/env python3
"""GT Behavioral Impact — measures whether GT deliveries CHANGE agent behavior.

For every GT block delivered to the agent (<gt-evidence>, <gt-contract>,
<gt-nudge>, <gt-scope>, <gt-verify>, <gt-obligations>), this module captures:
  - the agent's action TYPE before the delivery
  - the agent's action TYPE after the delivery
  - whether the type CHANGED (a behavioral pivot)

A PIVOT = GT caused the agent to do something different. The aggregate
"pivots / deliveries" is the GT behavioral impact rate — the causal metric.

Usage:
  python gt_behavioral_impact.py <trajectory.json> [--out impact.json]

Reads the pier/mini-swe-agent trajectory (messages list with role/content/
tool_calls). Outputs per-delivery records + aggregate.
"""
from __future__ import annotations

import json
import os
import re
import sys


# Action type classifier — what is the agent doing?
_EDIT_PATTERNS = re.compile(
    r"str_replace|create.*file|insert|write.*file|cat\s*>|sed\s+-i|"
    r"patch\s|tee\s|echo\s.*>>|apply_patch|git\s+apply",
    re.I,
)
_TEST_PATTERNS = re.compile(
    r"pytest|cargo\s+test|go\s+test|npm\s+test|yarn\s+test|vitest|jest|"
    r"make\s+test|tox|unittest|runtests|run_tests|\.test\.|test.*runner",
    re.I,
)
_SEARCH_PATTERNS = re.compile(
    r"grep\s+-|rg\s|find\s.*-name|ag\s|ack\s|grep\s.*def\s|"
    r"grep\s.*class\s|grep\s.*import|locate\s",
    re.I,
)
_READ_PATTERNS = re.compile(
    r"cat\s|head\s|tail\s|less\s|more\s|view\s|open\s.*\.|\bls\b|"
    r"bat\s|sed\s+-n|awk\s",
    re.I,
)
_BUILD_PATTERNS = re.compile(
    r"cargo\s+build|cargo\s+check|go\s+build|tsc\b|npm\s+run\s+build|"
    r"make\b|cmake|gcc|g\+\+|rustc|javac|mvn\s+compile",
    re.I,
)
_SUBMIT_PATTERNS = re.compile(
    r"submit|COMPLETE_TASK|final.*output|git\s+diff\s*>|done",
    re.I,
)

# GT block tags we track
_GT_TAGS = re.compile(
    r"<gt-(evidence|contract|scope|nudge|verify|obligations)"
)


def classify_action(text: str) -> str:
    """Classify an agent action into a behavioral type."""
    if not text:
        return "unknown"
    if _SUBMIT_PATTERNS.search(text):
        return "submitting"
    if _TEST_PATTERNS.search(text):
        return "testing"
    if _EDIT_PATTERNS.search(text):
        return "editing"
    if _BUILD_PATTERNS.search(text):
        return "building"
    if _SEARCH_PATTERNS.search(text):
        return "searching"
    if _READ_PATTERNS.search(text):
        return "reading"
    return "thinking"


def _extract_gt_tags(content: str) -> list[str]:
    """Extract all GT delivery tag types from observation content."""
    return _GT_TAGS.findall(content)


def analyze_trajectory(trajectory: dict) -> dict:
    """Analyze a pier/mini-swe-agent trajectory for GT behavioral impact.

    Returns {deliveries: [...], summary: {...}}.
    """
    messages = trajectory.get("messages", []) or []

    # Build a timeline: [(role, action_type, gt_tags, step_num), ...]
    timeline: list[dict] = []
    step = 0

    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""

        if role == "assistant":
            step += 1
            # The action is in tool_calls (the command) + content (the thought)
            cmd = json.dumps(m.get("tool_calls") or "")
            full_text = cmd + " " + (content if isinstance(content, str) else "")
            action_type = classify_action(full_text)
            timeline.append({
                "role": "assistant",
                "step": step,
                "action_type": action_type,
                "text_preview": full_text[:120],
            })

        elif role in ("tool", "user"):
            gt_tags = _extract_gt_tags(content) if isinstance(content, str) else []
            timeline.append({
                "role": "observation",
                "step": step,
                "gt_tags": gt_tags,
                "has_gt": bool(gt_tags),
                "text_preview": (content[:120] if isinstance(content, str) else ""),
            })

    # Find every GT delivery and pair it with before/after agent actions
    deliveries = []
    for i, entry in enumerate(timeline):
        if entry.get("role") != "observation" or not entry.get("has_gt"):
            continue

        # Find the last assistant action BEFORE this observation
        action_before = None
        for j in range(i - 1, -1, -1):
            if timeline[j].get("role") == "assistant":
                action_before = timeline[j]
                break

        # Find the next assistant action AFTER this observation
        action_after = None
        for j in range(i + 1, len(timeline)):
            if timeline[j].get("role") == "assistant":
                action_after = timeline[j]
                break

        before_type = action_before["action_type"] if action_before else "none"
        after_type = action_after["action_type"] if action_after else "none"
        pivot = before_type != after_type and after_type != "none"

        delivery = {
            "step": entry["step"],
            "gt_tags": entry["gt_tags"],
            "action_before": before_type,
            "action_after": after_type,
            "pivot": pivot,
            "transition": f"{before_type} -> {after_type}" if pivot else f"{before_type} (no change)",
        }
        deliveries.append(delivery)

    # Aggregate
    total = len(deliveries)
    pivots = sum(1 for d in deliveries if d["pivot"])
    impact_rate = pivots / total if total > 0 else 0.0

    # Transition breakdown
    transitions: dict[str, int] = {}
    for d in deliveries:
        t = d["transition"]
        transitions[t] = transitions.get(t, 0) + 1

    # Per-tag-type breakdown
    tag_pivots: dict[str, dict] = {}
    for d in deliveries:
        for tag in d["gt_tags"]:
            if tag not in tag_pivots:
                tag_pivots[tag] = {"total": 0, "pivots": 0}
            tag_pivots[tag]["total"] += 1
            if d["pivot"]:
                tag_pivots[tag]["pivots"] += 1

    summary = {
        "total_deliveries": total,
        "total_pivots": pivots,
        "impact_rate": round(impact_rate, 8),
        "transitions": transitions,
        "per_tag": {
            tag: {
                "total": v["total"],
                "pivots": v["pivots"],
                "rate": round(v["pivots"] / v["total"], 8) if v["total"] > 0 else 0.0,
            }
            for tag, v in sorted(tag_pivots.items())
        },
    }

    return {"deliveries": deliveries, "summary": summary}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <trajectory.json> [--out impact.json]")
        sys.exit(1)

    traj_path = sys.argv[1]
    out_path = None
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_path = sys.argv[idx + 1]

    with open(traj_path) as f:
        traj = json.load(f)

    result = analyze_trajectory(traj)
    s = result["summary"]

    print(f"GT Behavioral Impact: {s['total_pivots']}/{s['total_deliveries']} "
          f"deliveries caused a pivot ({s['impact_rate']:.1%})")
    print()
    print("Per-tag breakdown:")
    for tag, v in s["per_tag"].items():
        print(f"  <gt-{tag}>: {v['pivots']}/{v['total']} pivots ({v['rate']:.1%})")
    print()
    print("Transitions (most common):")
    for t, c in sorted(s["transitions"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {t}: {c}")

    if out_path:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nFull report: {out_path}")


if __name__ == "__main__":
    main()
