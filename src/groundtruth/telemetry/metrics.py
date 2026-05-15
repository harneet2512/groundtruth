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


_LAYER_NO_REACTION_BY_DESIGN = {
    "L1": "Brief is one-shot injection at iter 0 — no next_action, agent navigates independently",
    "L4": "Prefetch context at first read — no next_action, agent uses passively",
    "L6": "Reindex is invisible to agent — no agent action boundary, no reaction possible",
    "HYGIENE": "Scaffold strip at finish — cleanup layer, agent does not respond to it",
}


def compute_layer_utilization(
    layer_events: list[dict],
    reactions: list[dict],
    layer: str,
) -> tuple[float, str]:
    """0.00-1.00 utilization score per layer (Decision 34 rubric).

    Returns (score, documented_reason). documented_reason is empty when score >= 0.75.
    """
    layer_evts = [e for e in layer_events if e.get("layer") == layer]
    layer_reactions = [r for r in reactions if r.get("gt_layer") == layer]

    if not layer_evts:
        return 0.00, "no_events_emitted"

    has_emitted = any(e.get("emitted") for e in layer_evts)
    if not has_emitted:
        return 0.00, "no_emitted_events"

    has_structured = any(e.get("event_id") for e in layer_evts)
    if not has_structured:
        return 0.25, "emitted_text_but_no_structured_event_id"

    if not layer_reactions:
        reason = _LAYER_NO_REACTION_BY_DESIGN.get(layer, "")
        if reason:
            return 0.75, f"by_design:{reason}"
        return 0.50, "structured_gt_side_but_no_agent_reaction"

    has_followed = any(
        r.get("follow_type", "").startswith("FOLLOWED")
        for r in layer_reactions
    )

    has_suppression_reasons = all(
        e.get("suppression_reason") for e in layer_evts if e.get("suppressed")
    )

    if has_followed and has_suppression_reasons:
        return 1.00, ""

    return 0.75, ""


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


def _compute_l1_metrics(
    layer_events: list[dict], reactions: list[dict], agent_events: list[dict],
) -> dict[str, Any]:
    """L1 GT-side + agent-side + tandem metrics."""
    l1 = [e for e in layer_events if e.get("layer") == "L1"]
    l1_emitted = [e for e in l1 if e.get("emitted")]
    l1_reactions = [r for r in reactions if r.get("gt_layer") == "L1"]

    evidence_items = []
    for e in l1_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    candidates = [i for i in evidence_items if i.get("kind") == "l1_candidate"]

    return {
        "l1_brief_generated": len(l1) > 0,
        "l1_brief_injected": len(l1_emitted) > 0,
        "l1_candidate_count": len(candidates),
        "l1_candidate_files": [i.get("file_path") for i in candidates if i.get("file_path")],
        "l1_confidence_level": l1_emitted[0].get("confidence_level") or "not_emitted_by_wrapper" if l1_emitted else "N/A",
        "l1_confidence_score": l1_emitted[0].get("confidence_score", 0.0) or 0.0 if l1_emitted else "N/A",
        "l1_confidence_basis": l1_emitted[0].get("confidence_basis") or "not_emitted_by_wrapper" if l1_emitted else "N/A",
        "l1_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l1_emitted),
        "l1_candidates_with_graph_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l1_graph_edge"),
        "l1_candidates_with_test_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l1_test_edge"),
        "l1_candidates_with_signature_count": sum(1 for i in evidence_items if i.get("kind") == "l1_signature"),
        "l1_gt_pullback_to_l1_count": 0,
        "l1_reactions_count": len(l1_reactions),
        "l1_utilization_score": compute_layer_utilization(layer_events, reactions, "L1")[0],
        "l1_utilization_reason": compute_layer_utilization(layer_events, reactions, "L1")[1],
    }


def _compute_l3_metrics(
    layer_events: list[dict], reactions: list[dict],
) -> dict[str, Any]:
    """L3 GT-side + agent-side + utilization metrics."""
    l3 = [e for e in layer_events if e.get("layer") == "L3"]
    l3_emitted = [e for e in l3 if e.get("emitted")]
    l3_suppressed = [e for e in l3 if e.get("suppressed")]
    l3_reactions = [r for r in reactions if r.get("gt_layer") == "L3"]
    l3_with_na = [e for e in l3 if e.get("next_action_type")]

    evidence_items = []
    for e in l3_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    follow_dist = Counter(r.get("follow_type", "?") for r in l3_reactions)
    followed_3 = sum(1 for r in l3_reactions if r.get("followed_within_3"))

    return {
        "l3_edit_events_seen": len(l3),
        "l3_source_edit_events": sum(1 for e in l3 if e.get("file_kind") == "DURABLE_PRODUCT_FILE"),
        "l3_evidence_emitted": len(l3_emitted),
        "l3_suppressed_count": len(l3_suppressed),
        "l3_suppression_reason_distribution": dict(Counter(e.get("suppression_reason", "?") for e in l3_suppressed)),
        "l3_caller_code_line_count": sum(1 for i in evidence_items if i.get("kind") == "l3_caller_code"),
        "l3_signature_count": sum(1 for i in evidence_items if i.get("kind") == "l3_signature"),
        "l3_test_assertion_count": sum(1 for i in evidence_items if i.get("kind") == "l3_test_assertion"),
        "l3_next_action_type_distribution": dict(Counter(e.get("next_action_type") for e in l3_with_na)),
        "l3_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l3_emitted),
        "l3_metadata_only_count": sum(1 for e in l3_emitted if not e.get("evidence_items")),
        "l3_next_action_population_rate": len(l3_with_na) / max(len(l3_emitted), 1),
        "l3_reaction_coverage_rate": len(l3_reactions) / max(len(l3_with_na), 1),
        "l3_follow_rate_within_3": followed_3 / max(len(l3_reactions), 1),
        "l3_ignore_rate": follow_dist.get("IGNORED", 0) / max(len(l3_reactions), 1),
        "l3_follow_type_distribution": dict(follow_dist),
        "l3_utilization_score": compute_layer_utilization(layer_events, reactions, "L3")[0],
        "l3_utilization_reason": compute_layer_utilization(layer_events, reactions, "L3")[1],
    }


def _compute_l3b_metrics(
    layer_events: list[dict], reactions: list[dict],
) -> dict[str, Any]:
    """L3b GT-side + agent-side + utilization metrics."""
    l3b = [e for e in layer_events if e.get("layer") == "L3b"]
    l3b_emitted = [e for e in l3b if e.get("emitted")]
    l3b_suppressed = [e for e in l3b if e.get("suppressed")]
    l3b_reactions = [r for r in reactions if r.get("gt_layer") == "L3b"]

    evidence_items = []
    for e in l3b_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    follow_dist = Counter(r.get("follow_type", "?") for r in l3b_reactions)

    return {
        "l3b_file_read_events": len(l3b),
        "l3b_navigation_emitted": len(l3b_emitted),
        "l3b_suppressed_count": len(l3b_suppressed),
        "l3b_caller_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_caller_edge"),
        "l3b_callee_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_callee_edge"),
        "l3b_importer_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_importer_edge"),
        "l3b_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l3b_emitted),
        "l3b_avg_chars_per_fire": (
            sum(e.get("rendered_chars", 0) for e in l3b_emitted) // max(len(l3b_emitted), 1)
        ),
        "l3b_total_chars_per_task": sum(e.get("rendered_chars", 0) for e in l3b_emitted),
        "l3b_follow_type_distribution": dict(follow_dist),
        "l3b_utilization_score": compute_layer_utilization(layer_events, reactions, "L3b")[0],
        "l3b_utilization_reason": compute_layer_utilization(layer_events, reactions, "L3b")[1],
    }


def _compute_l5_metrics(
    layer_events: list[dict], reactions: list[dict],
) -> dict[str, Any]:
    """L5 GT-side + agent-side + tandem metrics (generalized event types only)."""
    l5 = [e for e in layer_events if e.get("layer") == "L5"]
    l5_emitted = [e for e in l5 if e.get("emitted")]
    l5_suppressed = [e for e in l5 if e.get("suppressed")]
    l5_reactions = [r for r in reactions if r.get("gt_layer") == "L5"]

    l5b = [e for e in layer_events if e.get("layer") == "L5b"]
    l5b_emitted = [e for e in l5b if e.get("emitted")]
    l5b_suppressed = [e for e in l5b if e.get("suppressed")]

    event_type_dist = Counter(e.get("event_type", "?") for e in l5)
    bucket_dist = Counter(e.get("event_bucket", "?") for e in l5 if e.get("event_bucket"))
    confidence_dist = Counter(e.get("confidence_level", "?") for e in l5 if e.get("confidence_level"))
    follow_dist = Counter(r.get("follow_type", "?") for r in l5_reactions)

    return {
        "l5_agent_events_seen_total": len(l5),
        "l5_agent_events_by_bucket": dict(bucket_dist),
        "l5_agent_events_by_type": dict(event_type_dist),
        "l5_detection_fired_count": len(l5_emitted),
        "l5_detection_suppressed_count": len(l5_suppressed),
        "l5_suppression_reason_distribution": dict(Counter(e.get("suppression_reason", "?") for e in l5_suppressed)),
        "l5_confidence_distribution": dict(confidence_dist),
        "l5_structural_witness_ignored_count": event_type_dist.get("STRUCTURAL_WITNESS_IGNORED", 0) + event_type_dist.get("goku_STRUCTURAL_WITNESS_IGNORED", 0),
        "l5_weak_verification_after_edit_count": event_type_dist.get("WEAK_VERIFICATION_AFTER_EDIT", 0) + event_type_dist.get("goku_WEAK_VERIFICATION_AFTER_EDIT", 0),
        "l5_finish_with_unverified_edit_count": event_type_dist.get("FINISH_WITH_UNVERIFIED_EDIT", 0) + event_type_dist.get("goku_FINISH_WITH_UNVERIFIED_EDIT", 0),
        "l5_patch_collapsed_or_lost_count": event_type_dist.get("PATCH_COLLAPSED_OR_LOST", 0) + event_type_dist.get("goku_PATCH_COLLAPSED_OR_LOST", 0),
        "l5_no_durable_progress_count": event_type_dist.get("NO_DURABLE_PROGRESS", 0) + event_type_dist.get("goku_NO_DURABLE_PROGRESS", 0),
        "l5_detection_to_l5b_rate": len(l5b_emitted) / max(len(l5_emitted), 1),
        "l5_detection_blocked_by_safety_count": len(l5b_suppressed),
        "l5_follow_type_distribution": dict(follow_dist),
        "l5_detection_to_agent_follow_rate": sum(1 for r in l5_reactions if r.get("follow_type", "").startswith("FOLLOWED")) / max(len(l5_reactions), 1),
        "l5b_messages_emitted": len(l5b_emitted),
        "l5b_messages_suppressed": len(l5b_suppressed),
        "l5b_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l5b_emitted),
        "l5_utilization_score": compute_layer_utilization(layer_events, reactions, "L5")[0],
        "l5_utilization_reason": compute_layer_utilization(layer_events, reactions, "L5")[1],
        "l5b_utilization_score": compute_layer_utilization(layer_events, reactions, "L5b")[0],
        "l5b_utilization_reason": compute_layer_utilization(layer_events, reactions, "L5b")[1],
    }


def _compute_l6_metrics(layer_events: list[dict]) -> dict[str, Any]:
    """L6 reindex metrics."""
    l6 = [e for e in layer_events if e.get("layer") == "L6"]
    l6_emitted = [e for e in l6 if e.get("emitted")]
    l6_suppressed = [e for e in l6 if e.get("suppressed")]
    return {
        "l6_reindex_attempt_count": len(l6),
        "l6_reindex_success_count": len(l6_emitted),
        "l6_reindex_failure_count": len(l6_suppressed),
        "l6_success_rate": len(l6_emitted) / max(len(l6), 1),
    }


def _compute_hygiene_metrics(layer_events: list[dict]) -> dict[str, Any]:
    """Hygiene metrics."""
    hyg = [e for e in layer_events if e.get("layer") == "HYGIENE"]
    hyg_emitted = [e for e in hyg if e.get("emitted")]
    return {
        "hygiene_invoked_on_finish": len(hyg) > 0,
        "hygiene_scaffold_files_detected": len(hyg_emitted),
    }


def _compute_meta_reaction_metrics(
    layer_events: list[dict], reactions: list[dict],
) -> dict[str, Any]:
    """Meta/reaction proof spine metrics."""
    emitted = [e for e in layer_events if e.get("emitted")]
    with_na = [e for e in layer_events if e.get("next_action_type")]
    suppressed = [e for e in layer_events if e.get("suppressed")]

    follow_dist = Counter(r.get("follow_type", "?") for r in reactions)
    reaction_gt_ids = {r.get("gt_event_id") for r in reactions}

    return {
        "gt_layer_events_count": len(layer_events),
        "gt_layer_events_by_layer": dict(Counter(e.get("layer") for e in layer_events)),
        "gt_rendered_messages_count": sum(1 for e in emitted if e.get("rendered_text")),
        "gt_rendered_messages_with_event_id": sum(1 for e in emitted if e.get("rendered_text") and e.get("event_id")),
        "gt_rendered_messages_missing_event_id": sum(1 for e in emitted if e.get("rendered_text") and not e.get("event_id")),
        "gt_next_action_events_count": len(with_na),
        "gt_next_action_events_by_layer": dict(Counter(e.get("layer") for e in with_na)),
        "gt_next_action_type_distribution": dict(Counter(e.get("next_action_type") for e in with_na)),
        "gt_suppressed_events_with_reason_rate": (
            sum(1 for e in suppressed if e.get("suppression_reason")) / max(len(suppressed), 1)
        ),
        "reaction_events_count": len(reactions),
        "reaction_events_by_layer": dict(Counter(r.get("gt_layer") for r in reactions)),
        "reaction_coverage_rate": len(reactions) / max(len(with_na), 1),
        "reaction_missing_for_next_action_count": sum(1 for e in with_na if e.get("event_id") not in reaction_gt_ids),
        "followed_exact_count": follow_dist.get("FOLLOWED_EXACT", 0),
        "followed_related_file_count": follow_dist.get("FOLLOWED_RELATED_FILE", 0),
        "followed_structural_witness_count": follow_dist.get("FOLLOWED_STRUCTURAL_WITNESS", 0),
        "followed_broad_only_count": follow_dist.get("FOLLOWED_BROAD_ONLY", 0),
        "partial_count": follow_dist.get("PARTIAL", 0),
        "ignored_count": follow_dist.get("IGNORED", 0),
        "contradicted_count": follow_dist.get("CONTRADICTED", 0),
        "too_late_count": follow_dist.get("TOO_LATE", 0),
        "not_measurable_count": follow_dist.get("NOT_MEASURABLE", 0),
        "followed_within_1_count": sum(1 for r in reactions if r.get("followed_within_1")),
        "followed_within_3_count": sum(1 for r in reactions if r.get("followed_within_3")),
        "followed_within_5_count": sum(1 for r in reactions if r.get("followed_within_5")),
        "event_to_reaction_join_rate": len(reactions) / max(len(with_na), 1),
        "next_action_to_reaction_rate": len(reactions) / max(len(with_na), 1),
    }


def _compute_agent_event_metrics(agent_events: list[dict]) -> dict[str, Any]:
    """Metrics from agent event stream."""
    bucket_dist = Counter(e.get("event_bucket", "?") for e in agent_events)
    kind_dist = Counter(e.get("file_kind", "?") for e in agent_events if e.get("file_kind"))
    return {
        "agent_events_total": len(agent_events),
        "agent_events_by_bucket": dict(bucket_dist),
        "agent_file_kind_distribution": dict(kind_dist),
    }


def compute_run_summary(
    layer_events_path: str,
    reactions_path: str,
    agent_events_path: str = "",
    belief_path: str = "",
) -> dict[str, Any]:
    """Compute full run summary from JSONL streams. Fills every metric cell."""
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
            "utilization_score": compute_layer_utilization(layer_events, reactions, layer)[0],
            "utilization_reason": compute_layer_utilization(layer_events, reactions, layer)[1],
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
        "l1": _compute_l1_metrics(layer_events, reactions, agent_events),
        "l3": _compute_l3_metrics(layer_events, reactions),
        "l3b": _compute_l3b_metrics(layer_events, reactions),
        "l5": _compute_l5_metrics(layer_events, reactions),
        "l6": _compute_l6_metrics(layer_events),
        "hygiene": _compute_hygiene_metrics(layer_events),
        "meta_reaction": _compute_meta_reaction_metrics(layer_events, reactions),
        "agent_events": _compute_agent_event_metrics(agent_events),
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
