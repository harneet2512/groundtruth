"""Deep utilization metrics — goes beyond fired counts to prove GT changed agent behavior.

Computes per-layer semantic utilization from the 3 JSONL streams:
- gt_layer_events.jsonl (GT-side)
- gt_agent_reactions.jsonl (agent-side, post-hoc)
- gt_belief_ledger.jsonl (belief transitions)
"""
from __future__ import annotations

import json
import os
from typing import Any


def compute_deep_utilization(
    layer_events_path: str,
    reactions_path: str,
    belief_path: str,
) -> dict[str, Any]:
    """Compute per-layer deep utilization from structured JSONL.

    Returns dict with per-layer metrics that prove whether GT
    changed the agent's behavior, not just whether it fired.
    """
    events = _load_jsonl(layer_events_path)
    reactions = _load_jsonl(reactions_path)
    beliefs = _load_jsonl(belief_path)

    result: dict[str, Any] = {
        "total_layer_events": len(events),
        "total_reactions": len(reactions),
        "total_beliefs": len(beliefs),
        "events_with_evidence_items": sum(1 for e in events if e.get("evidence_items")),
        "events_emitted": sum(1 for e in events if e.get("emitted")),
        "events_suppressed": sum(1 for e in events if e.get("suppressed")),
        "events_with_next_action": sum(1 for e in events if e.get("next_action_type")),
        "reactions_followed_within_1": sum(1 for r in reactions if r.get("followed_within_1")),
        "reactions_followed_within_3": sum(1 for r in reactions if r.get("followed_within_3")),
        "reactions_followed_within_5": sum(1 for r in reactions if r.get("followed_within_5")),
        "reactions_ignored": sum(1 for r in reactions if r.get("ignored")),
        "reactions_not_measurable": sum(1 for r in reactions if r.get("follow_type") == "NOT_MEASURABLE"),
        "beliefs_candidate": sum(1 for b in beliefs if b.get("new_status") == "candidate"),
        "beliefs_unverified": sum(1 for b in beliefs if b.get("new_status") == "unverified"),
        "beliefs_verified": sum(1 for b in beliefs if b.get("new_status") == "verified"),
        "beliefs_promoted": sum(1 for b in beliefs if b.get("new_status") == "promoted"),
        "beliefs_stale": sum(1 for b in beliefs if b.get("new_status") == "stale"),
    }

    # Per-layer breakdown
    layers = {}
    for e in events:
        layer = e.get("layer", "?")
        if layer not in layers:
            layers[layer] = {"emitted": 0, "suppressed": 0, "with_evidence": 0, "with_next_action": 0}
        if e.get("emitted"):
            layers[layer]["emitted"] += 1
        if e.get("suppressed"):
            layers[layer]["suppressed"] += 1
        if e.get("evidence_items"):
            layers[layer]["with_evidence"] += 1
        if e.get("next_action_type"):
            layers[layer]["with_next_action"] += 1

    result["per_layer"] = layers

    # Follow-type distribution
    follow_types: dict[str, int] = {}
    for r in reactions:
        ft = r.get("follow_type", "?")
        follow_types[ft] = follow_types.get(ft, 0) + 1
    result["follow_type_distribution"] = follow_types

    return result


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records
