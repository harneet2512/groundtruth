"""Run summary metrics — reads JSONL streams, computes per-layer utilization + proof spine.

No UI, no dashboard module. Outputs gt_run_summary_{task}.json and prints text tables.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records


def compute_layer_utilization(
    layer_events: list[dict],
    reactions: list[dict],
    layer: str,
) -> float:
    """0.00-1.00 utilization score per layer (Decision 34 rubric)."""
    layer_evts = [e for e in layer_events if e.get("layer") == layer]
    layer_reactions = [r for r in reactions if r.get("gt_layer") == layer]

    if not layer_evts:
        return 0.00

    has_emitted = any(e.get("emitted") for e in layer_evts)
    if not has_emitted:
        return 0.00

    has_structured = any(e.get("event_id") for e in layer_evts)
    if not has_structured:
        return 0.25

    if not layer_reactions:
        return 0.50

    has_followed = any(
        r.get("follow_type", "").startswith("FOLLOWED")
        for r in layer_reactions
    )

    has_suppression_reasons = all(
        e.get("suppression_reason") for e in layer_evts if e.get("suppressed")
    )

    if has_followed and has_suppression_reasons:
        return 1.00

    return 0.75


def compute_proof_spine(
    layer_events: list[dict],
    reactions: list[dict],
) -> dict[str, bool]:
    """Proof spine checks — every one must be True for a valid run."""
    emitted_events = [e for e in layer_events if e.get("emitted")]
    suppressed_events = [e for e in layer_events if e.get("suppressed")]
    next_action_events = [e for e in layer_events if e.get("next_action_type")]

    reaction_gt_ids = {r.get("gt_event_id") for r in reactions}

    return {
        "every_emitted_event_has_id": all(
            e.get("event_id") for e in emitted_events
        ) if emitted_events else True,
        "every_suppression_has_reason": all(
            e.get("suppression_reason") for e in suppressed_events
        ) if suppressed_events else True,
        "every_next_action_has_reaction": all(
            e.get("event_id") in reaction_gt_ids
            for e in next_action_events
        ) if next_action_events else True,
        "every_rendered_message_has_id": all(
            e.get("event_id") for e in layer_events
            if e.get("rendered_text")
        ),
        "no_malformed_events": all(
            e.get("schema_version") for e in layer_events
        ) if layer_events else True,
    }


def compute_hard_fails(
    layer_events: list[dict],
    reactions: list[dict],
) -> list[str]:
    """Return list of hard fail descriptions."""
    fails = []

    for e in layer_events:
        if e.get("emitted") and not e.get("event_id"):
            fails.append(f"FATAL: emitted event without event_id at iter {e.get('iter')}")
        if e.get("suppressed") and not e.get("suppression_reason"):
            fails.append(f"FATAL: suppressed event without reason at iter {e.get('iter')} layer={e.get('layer')}")
        if e.get("rendered_text") and not e.get("event_id"):
            fails.append(f"FATAL: rendered message without event_id at iter {e.get('iter')}")

        # L5 framework-specific check
        if e.get("layer") == "L5":
            et = e.get("event_type", "")
            for fw in ("pytest", "jest", "cargo", "go_test", "npm_test"):
                if fw in et.lower():
                    fails.append(f"DESIGN_VIOLATION: L5 event type contains framework name: {et}")

    reaction_gt_ids = {r.get("gt_event_id") for r in reactions}
    for e in layer_events:
        if e.get("next_action_type") and e.get("event_id") not in reaction_gt_ids:
            fails.append(f"FATAL: next_action without reaction for event {e.get('event_id')} layer={e.get('layer')}")

    return fails


def compute_run_summary(
    layer_events_path: str,
    reactions_path: str,
    agent_events_path: str = "",
    belief_path: str = "",
) -> dict[str, Any]:
    """Compute full run summary from JSONL streams."""
    layer_events = _load_jsonl(layer_events_path)
    reactions = _load_jsonl(reactions_path)
    agent_events = _load_jsonl(agent_events_path)
    beliefs = _load_jsonl(belief_path)

    layers_seen = set(e.get("layer", "") for e in layer_events if e.get("emitted"))

    per_layer: dict[str, dict] = {}
    for layer in sorted(layers_seen):
        levts = [e for e in layer_events if e.get("layer") == layer]
        lreactions = [r for r in reactions if r.get("gt_layer") == layer]

        emitted = [e for e in levts if e.get("emitted")]
        suppressed = [e for e in levts if e.get("suppressed")]
        with_next_action = [e for e in levts if e.get("next_action_type")]

        follow_dist = Counter(r.get("follow_type", "?") for r in lreactions)

        per_layer[layer] = {
            "eligible": sum(1 for e in levts if e.get("eligible")),
            "emitted": len(emitted),
            "suppressed": len(suppressed),
            "suppression_reasons": dict(Counter(
                e.get("suppression_reason", "?") for e in suppressed
            )),
            "rendered_tokens_total": sum(
                e.get("rendered_tokens_estimate", 0) for e in emitted
            ),
            "next_action_count": len(with_next_action),
            "reactions_total": len(lreactions),
            "follow_type_distribution": dict(follow_dist),
            "utilization_score": compute_layer_utilization(layer_events, reactions, layer),
        }

    proof = compute_proof_spine(layer_events, reactions)
    hard_fails = compute_hard_fails(layer_events, reactions)

    return {
        "total_layer_events": len(layer_events),
        "total_agent_events": len(agent_events),
        "total_reactions": len(reactions),
        "total_beliefs": len(beliefs),
        "layers_active": sorted(layers_seen),
        "per_layer": per_layer,
        "proof_spine": proof,
        "proof_spine_pass": all(proof.values()),
        "hard_fails": hard_fails,
        "hard_fail_count": len(hard_fails),
        "run_valid": len([f for f in hard_fails if f.startswith("FATAL")]) == 0,
    }


def print_summary(summary: dict) -> None:
    """Print boring proof tables to stdout."""
    print("=" * 60)
    print("GT RUN SUMMARY")
    print("=" * 60)
    print(f"Layer events: {summary['total_layer_events']}")
    print(f"Agent events: {summary['total_agent_events']}")
    print(f"Reactions:    {summary['total_reactions']}")
    print(f"Beliefs:      {summary['total_beliefs']}")
    print(f"Active layers: {', '.join(summary['layers_active'])}")
    print()

    print("--- Per-Layer Utilization ---")
    print(f"{'Layer':<8} {'Emit':>5} {'Supp':>5} {'React':>6} {'Util':>5}")
    for layer, data in summary.get("per_layer", {}).items():
        print(
            f"{layer:<8} {data['emitted']:>5} {data['suppressed']:>5} "
            f"{data['reactions_total']:>6} {data['utilization_score']:>5.2f}"
        )
    print()

    print("--- Proof Spine ---")
    for check, passed in summary.get("proof_spine", {}).items():
        status = "PASS" if passed else "FAIL"
        print(f"  {check}: {status}")
    print(f"  Overall: {'PASS' if summary.get('proof_spine_pass') else 'FAIL'}")
    print()

    if summary.get("hard_fails"):
        print("--- Hard Fails ---")
        for fail in summary["hard_fails"]:
            print(f"  {fail}")
    else:
        print("--- Hard Fails: 0 ---")

    print(f"\nRun valid: {summary.get('run_valid')}")
    print("=" * 60)
