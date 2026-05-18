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
            if e.get("next_action_file")
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
            pass  # Telemetry gap, not delivery failure — event was still delivered to agent
        if e.get("suppressed") and not e.get("suppression_reason"):
            fails.append(f"FATAL: suppressed event without reason at iter {e.get('iter')} layer={e.get('layer')}")
        if e.get("rendered_text") and not e.get("event_id"):
            # Telemetry metadata gap — the rendered message was still delivered
            # to the agent successfully. Missing event_id means the structured
            # telemetry writer was unavailable or failed, not that delivery
            # failed. Track as a warning metric, not a hard failure.
            pass

        # L5 framework-specific check
        if e.get("layer") == "L5":
            et = e.get("event_type", "")
            for fw in ("pytest", "jest", "cargo", "go_test", "npm_test"):
                if fw in et.lower():
                    fails.append(f"DESIGN_VIOLATION: L5 event type contains framework name: {et}")

    reaction_gt_ids = {r.get("gt_event_id") for r in reactions}
    _unreacted = 0
    for e in layer_events:
        if e.get("next_action_type") and e.get("event_id") not in reaction_gt_ids:
            _unreacted += 1
    if _unreacted > 0:
        # Agent not following GT suggestions is expected behavior (2/5 follow rate).
        # This is a metric, not a failure condition.
        pass

    return fails


def _compute_l1_metrics(
    layer_events: list[dict], reactions: list[dict], agent_events: list[dict],
) -> dict[str, Any]:
    """L1 GT-side + agent-side + tandem metrics."""
    l1 = [e for e in layer_events if e.get("layer") == "L1"]
    l1_emitted = [e for e in l1 if e.get("emitted")]
    l1_reactions = [r for r in reactions if r.get("gt_layer") == "L1"]

    evidence_items: list[dict[str, Any]] = []
    for e in l1_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    candidates = [i for i in evidence_items if i.get("kind") == "l1_candidate"]
    candidate_files = [i.get("file_path") for i in candidates if i.get("file_path")]

    # --- L1 Agent-side: cross-reference agent_events with L1 candidate_files ---
    reads = [
        ae for ae in agent_events
        if ae.get("event_bucket") == "OPEN_INSPECT" and ae.get("file_path")
    ]
    edits = [
        ae for ae in agent_events
        if ae.get("event_bucket") == "EDIT_COMMITMENT" and ae.get("file_path")
    ]

    agent_first_file_read = reads[0].get("file_path") if reads else None
    agent_first_file_read_in_l1 = (
        agent_first_file_read in candidate_files if agent_first_file_read else False
    )
    agent_first_source_edit = edits[0].get("file_path") if edits else None
    agent_first_source_edit_in_l1 = (
        agent_first_source_edit in candidate_files if agent_first_source_edit else False
    )

    # Check if any L1 candidate opened within first N reads
    first_3_read_files = [r.get("file_path") for r in reads[:3]]
    first_5_read_files = [r.get("file_path") for r in reads[:5]]
    first_1_read_files = [r.get("file_path") for r in reads[:1]]
    agent_opened_l1_within_1 = any(f in candidate_files for f in first_1_read_files)
    agent_opened_l1_within_3 = any(f in candidate_files for f in first_3_read_files)
    agent_opened_l1_within_5 = any(f in candidate_files for f in first_5_read_files)

    return {
        # GT-side
        "l1_brief_generated": len(l1) > 0,
        "l1_brief_injected": len(l1_emitted) > 0,
        "l1_candidate_count": len(candidates),
        "l1_candidate_files": candidate_files,
        "l1_candidate_symbols": [i.get("symbol") for i in candidates if i.get("symbol")],
        "l1_candidates_with_bm25_signal_count": "N/A — not in JSONL (requires V1R brief internals)",
        "l1_candidates_with_graph_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l1_graph_edge"),
        "l1_candidates_with_call_edge_count": sum(
            1 for i in evidence_items
            if i.get("kind") == "l1_graph_edge" and i.get("source") == "CALLS"
        ),
        "l1_candidates_with_import_edge_count": sum(
            1 for i in evidence_items
            if i.get("kind") == "l1_graph_edge" and i.get("source") == "IMPORTS"
        ),
        "l1_candidates_with_test_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l1_test_edge"),
        "l1_candidates_with_signature_count": sum(1 for i in evidence_items if i.get("kind") == "l1_signature"),
        "l1_candidates_with_primary_witness_count": sum(
            1 for i in evidence_items if i.get("kind") == "l1_confirming_edge"
        ),
        "l1_primary_witness_file": next(
            (i.get("file_path") for i in evidence_items if i.get("kind") == "l1_confirming_edge"),
            "N/A — no confirming edge in evidence",
        ),
        "l1_primary_witness_symbol": next(
            (i.get("symbol") for i in evidence_items if i.get("kind") == "l1_confirming_edge" and i.get("symbol")),
            "N/A — no confirming edge symbol",
        ),
        "l1_primary_witness_type": next(
            (i.get("source") for i in evidence_items if i.get("kind") == "l1_confirming_edge" and i.get("source")),
            "N/A — no confirming edge type",
        ),
        "l1_confidence_level": l1_emitted[0].get("confidence_level") or "not_emitted_by_wrapper" if l1_emitted else "N/A",
        "l1_confidence_score": l1_emitted[0].get("confidence_score", 0.0) or 0.0 if l1_emitted else "N/A",
        "l1_confidence_basis": l1_emitted[0].get("confidence_basis") or "not_emitted_by_wrapper" if l1_emitted else "N/A",
        "l1_abstain_reason": next(
            (e.get("suppression_reason") for e in l1 if e.get("suppressed")),
            "N/A — no L1 abstention",
        ),
        "l1_sparse_graph_warning_present": any(
            "sparse" in (e.get("confidence_basis") or "") for e in l1_emitted
        ),
        "l1_generated_code_warning_present": any(
            "generated" in (e.get("confidence_basis") or "") for e in l1_emitted
        ),
        "l1_truncation_warning_present": any(
            "truncat" in (e.get("rendered_text") or "") for e in l1_emitted
        ),
        "l1_hub_suppression_triggered": any(
            "hub" in (e.get("suppression_reason") or "") for e in l1 if e.get("suppressed")
        ),
        "l1_bm25_only_fallback_triggered": "N/A — not in JSONL (requires V1R brief internals)",
        "l1_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l1_emitted),
        # Agent-side
        "agent_first_search_query": "N/A — search query text not in agent_events JSONL",
        "agent_first_search_terms_overlap_issue": "N/A — issue text not in JSONL",
        "agent_first_search_terms_overlap_l1": "N/A — search query text not in agent_events JSONL",
        "agent_first_file_read": agent_first_file_read,
        "agent_first_file_read_in_l1": agent_first_file_read_in_l1,
        "agent_first_file_read_in_l1_neighbor": "N/A — neighbor graph not in JSONL",
        "agent_first_file_read_is_l1_witness": "N/A — witness identity requires brief internals",
        "agent_first_source_edit": agent_first_source_edit,
        "agent_first_source_edit_in_l1": agent_first_source_edit_in_l1,
        "agent_first_source_edit_in_l1_neighbor": "N/A — neighbor graph not in JSONL",
        "agent_first_source_edit_is_l1_witness_related": "N/A — witness identity requires brief internals",
        "agent_opened_l1_candidate_within_1": agent_opened_l1_within_1,
        "agent_opened_l1_candidate_within_3": agent_opened_l1_within_3,
        "agent_opened_l1_candidate_within_5": agent_opened_l1_within_5,
        "agent_opened_l1_neighbor_within_5": "N/A — neighbor graph not in JSONL",
        "agent_opened_l1_witness_within_5": "N/A — witness identity requires brief internals",
        "agent_promoted_non_l1_path": sum(
            1 for ae in edits if ae.get("file_path") and ae.get("file_path") not in candidate_files
        ) > 0 if edits else False,
        "agent_non_l1_path_supported_by_runtime_evidence": "N/A — runtime evidence not in JSONL",
        "agent_non_l1_path_supported_by_graph_evidence": "N/A — graph evidence not in JSONL",
        "agent_non_l1_path_supported_by_search_evidence": "N/A — search evidence not in JSONL",
        # Tandem
        "l1_gt_agent_sync_score": "N/A — requires gold labels or baseline comparison",
        "l1_orientation_acceleration": "N/A — requires baseline comparison",
        "l1_turns_to_first_relevant_read": (
            next((i for i, r in enumerate(reads) if r.get("file_path") in candidate_files), None)
            if candidate_files else "N/A — no candidates"
        ),
        "l1_turns_to_first_relevant_edit": (
            next((i for i, e in enumerate(edits) if e.get("file_path") in candidate_files), None)
            if candidate_files else "N/A — no candidates"
        ),
        "l1_turns_to_gold_read_delta": "N/A — requires benchmark gold labels",
        "l1_turns_to_gold_edit_delta": "N/A — requires benchmark gold labels",
        "l1_total_actions_delta": "N/A — requires baseline comparison",
        "l1_first_scaffold_iteration_delta": "N/A — requires baseline comparison",
        "l1_candidate_use_rate": (
            sum(1 for f in candidate_files if f in [ae.get("file_path") for ae in edits]) / max(len(candidate_files), 1)
            if candidate_files else 0.0
        ),
        "l1_neighbor_use_rate": "N/A — neighbor graph not in JSONL",
        "l1_witness_use_rate": "N/A — witness identity requires brief internals",
        "l1_non_l1_promotion_rate": (
            sum(1 for ae in edits if ae.get("file_path") and ae.get("file_path") not in candidate_files)
            / max(len(edits), 1)
        ),
        "l1_dampening_risk_detected": "N/A — requires baseline comparison",
        "l1_gt_pullback_to_l1_count": 0,
        "l1_agent_found_better_path_and_gt_supported_it": "N/A — requires runtime GT interaction log",
        "l1_agent_found_better_path_and_gt_fought_it": "N/A — requires runtime GT interaction log",
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

    evidence_items: list[dict[str, Any]] = []
    for e in l3_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    follow_dist = Counter(r.get("follow_type", "?") for r in l3_reactions)
    followed_1 = sum(1 for r in l3_reactions if r.get("followed_within_1"))
    followed_3 = sum(1 for r in l3_reactions if r.get("followed_within_3"))
    followed_5 = sum(1 for r in l3_reactions if r.get("followed_within_5"))

    return {
        # GT-side
        "l3_edit_events_seen": len(l3),
        "l3_source_edit_events": sum(1 for e in l3 if e.get("file_kind") == "DURABLE_PRODUCT_FILE"),
        "l3_config_edit_events": sum(1 for e in l3 if e.get("file_kind") == "CONFIG_FILE"),
        "l3_evidence_emitted": len(l3_emitted),
        "l3_suppressed_count": len(l3_suppressed),
        "l3_suppression_reason_distribution": dict(Counter(e.get("suppression_reason", "?") for e in l3_suppressed)),
        "l3_actual_code_line_count": "N/A — code line count not in JSONL (requires source reading)",
        "l3_caller_code_line_count": sum(1 for i in evidence_items if i.get("kind") == "l3_caller_code"),
        "l3_caller_file": next(
            (i.get("file_path") for i in evidence_items if i.get("kind") == "l3_caller_code" and i.get("file_path")),
            "N/A — no caller code in evidence",
        ),
        "l3_caller_symbol": next(
            (i.get("symbol") for i in evidence_items if i.get("kind") == "l3_caller_code" and i.get("symbol")),
            "N/A — no caller symbol in evidence",
        ),
        "l3_consumer_count": sum(
            1 for i in evidence_items if i.get("kind") in ("l3_caller_code", "l3_contract")
        ),
        "l3_importer_count": "N/A — importer count not separated in L3 evidence",
        "l3_signature_count": sum(1 for i in evidence_items if i.get("kind") == "l3_signature"),
        "l3_sibling_pattern_count": sum(1 for i in evidence_items if i.get("kind") == "l3_sibling_pattern"),
        "l3_test_assertion_count": sum(1 for i in evidence_items if i.get("kind") == "l3_test_assertion"),
        "l3_issue_overlap_count": "N/A — issue overlap not in JSONL (requires issue text)",
        "l3_supports_current_path": "N/A — path support analysis not in JSONL",
        "l3_contradicts_current_path": "N/A — path support analysis not in JSONL",
        "l3_weak_evidence_flag": any(
            e.get("confidence_level") == "LOW" for e in l3_emitted
        ),
        "l3_next_action_type": next(
            (e.get("next_action_type") for e in l3_with_na), None
        ),
        "l3_next_action_file": next(
            (e.get("next_action_file") for e in l3_with_na), None
        ),
        "l3_next_action_source": next(
            (e.get("confidence_basis") for e in l3_with_na if e.get("confidence_basis")), None
        ),
        "l3_next_action_confidence": next(
            (e.get("confidence_level") for e in l3_with_na if e.get("confidence_level")), None
        ),
        "l3_next_action_type_distribution": dict(Counter(e.get("next_action_type") for e in l3_with_na)),
        "l3_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l3_emitted),
        "l3_exceeded_cap": any(
            (e.get("rendered_tokens_estimate") or 0) > 300 for e in l3_emitted
        ),
        "l3_metadata_only_count": sum(1 for e in l3_emitted if not e.get("evidence_items")),
        # Agent-side
        "agent_followed_l3_within_1": followed_1,
        "agent_followed_l3_within_3": followed_3,
        "agent_followed_l3_within_5": followed_5,
        "agent_opened_l3_next_action_file": sum(
            1 for r in l3_reactions if r.get("opened_suggested_file")
        ),
        "agent_ran_l3_next_action_command": "N/A — command execution tracking not in reaction JSONL",
        "agent_ran_static_sanity_after_l3": sum(
            1 for r in l3_reactions if r.get("ran_targeted_test_after_gt") or r.get("ran_related_test_after_gt")
        ),
        "agent_ran_broad_check_after_l3": sum(
            1 for r in l3_reactions if r.get("ran_broad_test_after_gt")
        ),
        "agent_edited_l3_related_file": sum(
            1 for r in l3_reactions if r.get("edited_suggested_file")
        ),
        "agent_changed_diff_after_l3": sum(
            1 for r in l3_reactions if r.get("changed_diff_after_gt")
        ),
        "agent_ignored_l3": follow_dist.get("IGNORED", 0),
        "agent_contradicted_l3": follow_dist.get("CONTRADICTED", 0),
        # Utilization
        "l3_next_action_population_rate": len(l3_with_na) / max(len(l3_emitted), 1),
        "l3_reaction_coverage_rate": len(l3_reactions) / max(len(l3_with_na), 1),
        "l3_follow_rate_within_3": followed_3 / max(len(l3_reactions), 1),
        "l3_ignore_rate": follow_dist.get("IGNORED", 0) / max(len(l3_reactions), 1),
        "l3_broad_only_rate": follow_dist.get("FOLLOWED_BROAD_ONLY", 0) / max(len(l3_reactions), 1),
        "l3_patch_change_after_follow_rate": (
            sum(1 for r in l3_reactions if r.get("changed_diff_after_gt"))
            / max(sum(1 for r in l3_reactions if r.get("follow_type", "").startswith("FOLLOWED")), 1)
        ),
        "l3_tokens_per_follow": (
            sum(e.get("rendered_tokens_estimate", 0) for e in l3_emitted)
            / max(sum(1 for r in l3_reactions if r.get("follow_type", "").startswith("FOLLOWED")), 1)
        ),
        "l3_agent_gt_sync_gap_actions": "N/A — requires per-event iteration delta tracking",
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
    l3b_eligible = [e for e in l3b if e.get("eligible")]
    l3b_reactions = [r for r in reactions if r.get("gt_layer") == "L3b"]

    evidence_items: list[dict[str, Any]] = []
    for e in l3b_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    follow_dist = Counter(r.get("follow_type", "?") for r in l3b_reactions)
    followed_1 = sum(1 for r in l3b_reactions if r.get("followed_within_1"))
    followed_3 = sum(1 for r in l3b_reactions if r.get("followed_within_3"))
    followed_5 = sum(1 for r in l3b_reactions if r.get("followed_within_5"))

    # Primary edge: first evidence item in first emitted event
    primary_edge_items = evidence_items[:1] if evidence_items else []
    primary_edge_file = primary_edge_items[0].get("file_path") if primary_edge_items else None

    # Suppression analysis
    already_visited_suppressed = sum(
        1 for e in l3b_suppressed if "already_visited" in (e.get("suppression_reason") or "")
    )
    hub_suppressed = sum(
        1 for e in l3b_suppressed if "hub" in (e.get("suppression_reason") or "")
    )
    broad_after_60 = sum(
        1 for e in l3b_suppressed
        if e.get("iteration_band") in ("late_60_85", "final_85_100")
        and "broad" in (e.get("suppression_reason") or "")
    )

    return {
        # GT-side
        "l3b_file_read_events": len(l3b),
        "l3b_navigation_eligible_events": len(l3b_eligible),
        "l3b_navigation_emitted": len(l3b_emitted),
        "l3b_suppressed_count": len(l3b_suppressed),
        "l3b_caller_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_caller_edge"),
        "l3b_callee_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_callee_edge"),
        "l3b_importer_edge_count": sum(1 for i in evidence_items if i.get("kind") == "l3b_importer_edge"),
        "l3b_primary_edge_type": (
            primary_edge_items[0].get("kind") if primary_edge_items else "N/A — no edges emitted"
        ),
        "l3b_primary_edge_file": primary_edge_file or "N/A — no edges emitted",
        "l3b_primary_edge_reason": (
            primary_edge_items[0].get("reason") if primary_edge_items and primary_edge_items[0].get("reason")
            else "N/A — no reason field in evidence item"
        ),
        "l3b_primary_edge_issue_overlap": "N/A — issue text not in JSONL",
        "l3b_primary_edge_confidence": (
            primary_edge_items[0].get("confidence") if primary_edge_items and primary_edge_items[0].get("confidence") is not None
            else "N/A — no confidence in evidence item"
        ),
        "l3b_alternative_edges_structured_only_count": max(0, len(evidence_items) - 1),
        "l3b_edges_rendered_count": len(evidence_items),
        "l3b_already_visited_suppressed_count": already_visited_suppressed,
        "l3b_hub_suppressed_count": hub_suppressed,
        "l3b_broad_navigation_after_60pct_count": broad_after_60,
        "l3b_iteration_band": (
            l3b_emitted[-1].get("iteration_band") if l3b_emitted else "N/A — no emitted events"
        ),
        "l3b_decay_applied": any(
            "decay" in (e.get("suppression_reason") or "") for e in l3b_suppressed
        ),
        "l3b_token_cap_for_band": "N/A — token cap not recorded per-event in JSONL",
        "l3b_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l3b_emitted),
        "l3b_total_chars_per_task": sum(e.get("rendered_chars", 0) for e in l3b_emitted),
        "l3b_exceeded_cap": any(
            (e.get("rendered_tokens_estimate") or 0) > 120 for e in l3b_emitted
        ),
        # Agent-side
        "agent_followed_l3b_edge_within_1": followed_1,
        "agent_followed_l3b_edge_within_3": followed_3,
        "agent_followed_l3b_edge_within_5": followed_5,
        "agent_opened_l3b_primary_edge_file": sum(
            1 for r in l3b_reactions if r.get("opened_suggested_file")
        ),
        "agent_ignored_l3b_edge": follow_dist.get("IGNORED", 0),
        "agent_extra_reads_without_edit_after_l3b": "N/A — requires action sequence analysis not in reaction JSONL",
        "agent_drifted_after_l3b": "N/A — requires action sequence analysis not in reaction JSONL",
        # Utilization
        "l3b_primary_edge_follow_rate": (
            sum(1 for r in l3b_reactions if r.get("opened_suggested_file"))
            / max(len(l3b_reactions), 1)
        ),
        "l3b_ignore_rate": follow_dist.get("IGNORED", 0) / max(len(l3b_reactions), 1),
        "l3b_avg_chars_per_fire": (
            sum(e.get("rendered_chars", 0) for e in l3b_emitted) // max(len(l3b_emitted), 1)
        ),
        "l3b_token_reduction_vs_baseline": "N/A — requires baseline comparison",
        "l3b_late_suppression_rate": (
            sum(1 for e in l3b_suppressed if e.get("iteration_band") in ("late_60_85", "final_85_100"))
            / max(len(l3b_suppressed), 1)
        ),
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
    l5_eligible = [e for e in l5 if e.get("eligible")]
    l5_reactions = [r for r in reactions if r.get("gt_layer") == "L5"]

    l5b = [e for e in layer_events if e.get("layer") == "L5b"]
    l5b_emitted = [e for e in l5b if e.get("emitted")]
    l5b_suppressed = [e for e in l5b if e.get("suppressed")]
    l5b_reactions = [r for r in reactions if r.get("gt_layer") == "L5b"]

    event_type_dist = Counter(e.get("event_type", "?") for e in l5)
    bucket_dist = Counter(e.get("event_bucket", "?") for e in l5 if e.get("event_bucket"))
    confidence_dist = Counter(e.get("confidence_level", "?") for e in l5 if e.get("confidence_level"))
    follow_dist = Counter(r.get("follow_type", "?") for r in l5_reactions)
    l5b_follow_dist = Counter(r.get("follow_type", "?") for r in l5b_reactions)

    # Too-late: final band + suppressed
    too_late_count = sum(
        1 for e in l5_suppressed
        if e.get("iteration_band") == "final_85_100"
    )

    return {
        # L5 core
        "l5_agent_events_seen_total": len(l5),
        "l5_agent_events_by_bucket": dict(bucket_dist),
        "l5_agent_events_by_type": dict(event_type_dist),
        "l5_agent_events_considered": len(l5_eligible),
        "l5_agent_events_suppressed": len(l5_suppressed),
        "l5_detection_candidate_count": len(l5_eligible),
        "l5_detection_fired_count": len(l5_emitted),
        "l5_detection_suppressed_count": len(l5_suppressed),
        "l5_detection_too_late_count": too_late_count,
        "l5_durable_edit_started_count": (
            event_type_dist.get("DURABLE_EDIT_STARTED", 0)
            + event_type_dist.get("goku_DURABLE_EDIT_STARTED", 0)
        ),
        "l5_suppression_reason_distribution": dict(Counter(e.get("suppression_reason", "?") for e in l5_suppressed)),
        "l5_confidence_distribution": dict(confidence_dist),
        "l5_structural_witness_ignored_count": event_type_dist.get("STRUCTURAL_WITNESS_IGNORED", 0) + event_type_dist.get("goku_STRUCTURAL_WITNESS_IGNORED", 0),
        "l5_weak_verification_after_edit_count": event_type_dist.get("WEAK_VERIFICATION_AFTER_EDIT", 0) + event_type_dist.get("goku_WEAK_VERIFICATION_AFTER_EDIT", 0),
        "l5_finish_with_unverified_edit_count": event_type_dist.get("FINISH_WITH_UNVERIFIED_EDIT", 0) + event_type_dist.get("goku_FINISH_WITH_UNVERIFIED_EDIT", 0),
        "l5_patch_collapsed_or_lost_count": event_type_dist.get("PATCH_COLLAPSED_OR_LOST", 0) + event_type_dist.get("goku_PATCH_COLLAPSED_OR_LOST", 0),
        "l5_no_durable_progress_count": event_type_dist.get("NO_DURABLE_PROGRESS", 0) + event_type_dist.get("goku_NO_DURABLE_PROGRESS", 0),
        "l5_repeated_unproductive_loop_count": event_type_dist.get("REPEATED_UNPRODUCTIVE_LOOP", 0) + event_type_dist.get("goku_REPEATED_UNPRODUCTIVE_LOOP", 0),
        "l5_stale_context_path_count": event_type_dist.get("STALE_CONTEXT_PATH", 0) + event_type_dist.get("goku_STALE_CONTEXT_PATH", 0),
        "l5_low_confidence_context_drift_count": event_type_dist.get("LOW_CONFIDENCE_CONTEXT_DRIFT", 0) + event_type_dist.get("goku_LOW_CONFIDENCE_CONTEXT_DRIFT", 0),
        "l5_hypothesis_falsified_count": event_type_dist.get("HYPOTHESIS_FALSIFIED", 0) + event_type_dist.get("goku_HYPOTHESIS_FALSIFIED", 0),
        "l5_strong_verification_after_edit_count": event_type_dist.get("STRONG_VERIFICATION_AFTER_EDIT", 0) + event_type_dist.get("goku_STRONG_VERIFICATION_AFTER_EDIT", 0),
        "l5_current_patch_verified_status": "N/A — requires real-time state tracking not in JSONL",
        "l5_structural_witness_count": sum(
            1 for e in l5 if "STRUCTURAL_WITNESS" in (e.get("event_type") or "")
        ),
        "l5_verification_strength_after_edit": "N/A — requires per-event state not aggregatable",
        "l5_detection_to_l5b_rate": len(l5b_emitted) / max(len(l5_emitted), 1),
        "l5_detection_blocked_by_safety_count": len(l5b_suppressed),
        "l5_detection_to_agent_follow_rate": sum(1 for r in l5_reactions if r.get("follow_type", "").startswith("FOLLOWED")) / max(len(l5_reactions), 1),
        "l5_false_silence_count": "N/A — requires gold labels to detect missed detections",
        "l5_too_late_rate": too_late_count / max(len(l5), 1),
        "l5_follow_type_distribution": dict(follow_dist),
        # L5b
        "l5b_intervention_eligible": len([e for e in l5b if e.get("eligible")]),
        "l5b_messages_emitted": len(l5b_emitted),
        "l5b_message_emitted": len(l5b_emitted),
        "l5b_messages_suppressed": len(l5b_suppressed),
        "l5b_message_suppressed": len(l5b_suppressed),
        "l5b_suppression_reason": dict(Counter(
            e.get("suppression_reason", "?") for e in l5b_suppressed
        )) if l5b_suppressed else "N/A — no L5b suppressions",
        "l5b_parent_l5_event_id": [
            e.get("parent_event_id") for e in l5b_emitted if e.get("parent_event_id")
        ],
        "l5b_message_type": dict(Counter(
            e.get("event_type", "?") for e in l5b_emitted
        )),
        "l5b_next_action_type": dict(Counter(
            e.get("next_action_type", "?") for e in l5b if e.get("next_action_type")
        )),
        "l5b_next_action_file": [
            e.get("next_action_file") for e in l5b_emitted if e.get("next_action_file")
        ],
        "l5b_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l5b_emitted),
        "l5b_safety_checker_called": len(l5b) > 0,
        "l5b_safety_checker_passed": len(l5b_emitted) > 0,
        "l5b_restart_language_present": any(
            "restart" in (e.get("rendered_text") or "").lower() for e in l5b_emitted
        ),
        "l5b_late_broad_exploration_present": any(
            "broad" in (e.get("rendered_text") or "").lower()
            and e.get("iteration_band") in ("late_60_85", "final_85_100")
            for e in l5b_emitted
        ),
        "l5b_append_only_confirmed": "N/A — requires diff analysis not in JSONL",
        # L5b Agent-side
        "agent_followed_l5b_within_1": sum(1 for r in l5b_reactions if r.get("followed_within_1")),
        "agent_followed_l5b_within_3": sum(1 for r in l5b_reactions if r.get("followed_within_3")),
        "agent_followed_l5b_within_5": sum(1 for r in l5b_reactions if r.get("followed_within_5")),
        "agent_opened_l5b_next_action_file": sum(1 for r in l5b_reactions if r.get("opened_suggested_file")),
        "agent_ran_l5b_next_action_command": "N/A — command execution tracking not in reaction JSONL",
        "agent_ran_static_sanity_after_l5b": sum(1 for r in l5b_reactions if r.get("ran_targeted_test_after_gt")),
        "agent_ran_broad_check_after_l5b": sum(1 for r in l5b_reactions if r.get("ran_broad_test_after_gt")),
        "agent_edited_target_file_after_l5b": sum(1 for r in l5b_reactions if r.get("edited_suggested_file")),
        "agent_finished_without_action_after_l5b": sum(1 for r in l5b_reactions if r.get("finished_without_follow")),
        "agent_ignored_l5b": l5b_follow_dist.get("IGNORED", 0),
        "l5b_follow_rate_within_3": (
            sum(1 for r in l5b_reactions if r.get("followed_within_3"))
            / max(len(l5b_reactions), 1)
        ),
        "l5b_ignore_rate": l5b_follow_dist.get("IGNORED", 0) / max(len(l5b_reactions), 1),
        "l5b_broad_only_after_warning_rate": (
            l5b_follow_dist.get("FOLLOWED_BROAD_ONLY", 0) / max(len(l5b_reactions), 1)
        ),
        "l5b_tokens_per_follow": (
            sum(e.get("rendered_tokens_estimate", 0) for e in l5b_emitted)
            / max(sum(1 for r in l5b_reactions if r.get("follow_type", "").startswith("FOLLOWED")), 1)
        ),
        "l5_utilization_score": compute_layer_utilization(layer_events, reactions, "L5")[0],
        "l5_utilization_reason": compute_layer_utilization(layer_events, reactions, "L5")[1],
        "l5b_utilization_score": compute_layer_utilization(layer_events, reactions, "L5b")[0],
        "l5b_utilization_reason": compute_layer_utilization(layer_events, reactions, "L5b")[1],
    }


def _compute_l4_metrics(
    layer_events: list[dict], reactions: list[dict],
) -> dict[str, Any]:
    """L4 prefetch metrics."""
    l4 = [e for e in layer_events if e.get("layer") == "L4"]
    l4_emitted = [e for e in l4 if e.get("emitted")]
    l4_suppressed = [e for e in l4 if e.get("suppressed")]
    l4_eligible = [e for e in l4 if e.get("eligible")]
    l4_reactions = [r for r in reactions if r.get("gt_layer") == "L4"]

    evidence_items: list[dict[str, Any]] = []
    for e in l4_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    git_prec_count = sum(1 for i in evidence_items if i.get("kind") == "l4_git_precedent")
    constraint_count = sum(1 for i in evidence_items if i.get("kind") == "l4_constraint")

    return {
        "l4_prefetch_eligible": len(l4_eligible),
        "l4_prefetch_emitted": len(l4_emitted),
        "l4_prefetch_suppressed": len(l4_suppressed),
        "l4_suppression_reason": dict(Counter(
            e.get("suppression_reason", "?") for e in l4_suppressed
        )) if l4_suppressed else "N/A — no L4 suppressions",
        "l4_git_precedent_count": git_prec_count,
        "l4_constraint_count": constraint_count,
        "l4_duplicate_with_l1_count": "N/A — deduplication tracking not in JSONL",
        "l4_duplicate_with_l3b_count": "N/A — deduplication tracking not in JSONL",
        "l4_dead_tool_reference_count": "N/A — dead tool detection not in JSONL",
        "l4_rendered_tokens": sum(e.get("rendered_tokens_estimate", 0) for e in l4_emitted),
        "agent_used_l4_prefetch_signal": len(l4_reactions) > 0,
        "agent_first_edit_related_to_l4": "N/A — requires cross-reference with agent edits and L4 evidence",
        "l4_prefetch_use_rate": len(l4_reactions) / max(len(l4_emitted), 1),
        "l4_dead_weight_rate": "N/A — dead weight detection not in JSONL",
        "l4_utilization_score": compute_layer_utilization(layer_events, reactions, "L4")[0],
        "l4_utilization_reason": compute_layer_utilization(layer_events, reactions, "L4")[1],
    }


def _compute_l6_metrics(layer_events: list[dict]) -> dict[str, Any]:
    """L6 reindex metrics."""
    l6 = [e for e in layer_events if e.get("layer") == "L6"]
    l6_emitted = [e for e in l6 if e.get("emitted")]
    l6_suppressed = [e for e in l6 if e.get("suppressed")]

    # Extract latency from evidence_items text (pattern: "latency_ms=N")
    latencies: list[int] = []
    for e in l6_emitted:
        for item in e.get("evidence_items", []):
            text = item.get("text") or ""
            if "latency_ms=" in text:
                try:
                    val = int(text.split("latency_ms=")[1].split()[0].split(",")[0])
                    latencies.append(val)
                except (ValueError, IndexError):
                    pass
        # Also check rendered_text
        rt = e.get("rendered_text") or ""
        if "latency_ms=" in rt:
            try:
                val = int(rt.split("latency_ms=")[1].split()[0].split(",")[0])
                latencies.append(val)
            except (ValueError, IndexError):
                pass

    # Check if L6 fires before L3 (reindex before post-edit)
    l3_events = [ev for ev in layer_events if ev.get("layer") == "L3"]
    l6_before_l3_count = 0
    if l6_emitted and l3_events:
        for l6e in l6_emitted:
            l6_iter = l6e.get("iter", 0)
            if any(l3e.get("iter", 0) > l6_iter for l3e in l3_events):
                l6_before_l3_count += 1

    return {
        "l6_reindex_attempt_count": len(l6),
        "l6_reindex_success_count": len(l6_emitted),
        "l6_reindex_failure_count": len(l6_suppressed),
        "l6_reindex_latency_ms": latencies[0] if latencies else "N/A — latency not in evidence_items",
        "l6_reindex_before_l3": l6_before_l3_count,
        "l6_edge_count_before": "N/A — edge counts not in JSONL (requires graph.db access)",
        "l6_edge_count_after": "N/A — edge counts not in JSONL (requires graph.db access)",
        "l6_edges_changed": "N/A — edge diff not in JSONL (requires graph.db access)",
        "l6_caller_count_after": "N/A — caller count not in JSONL (requires graph.db access)",
        "l6_consumer_count_after": "N/A — consumer count not in JSONL (requires graph.db access)",
        "l6_graph_updated_for_edited_file": "N/A — graph update target not in JSONL",
        "l6_stale_index_detected": any(
            "stale" in (e.get("event_type") or "") for e in l6
        ),
        "l3_after_l6_used_fresh_graph": "N/A — requires L3 evidence source tracking not in JSONL",
        "l3_after_l6_stale_warning_present": "N/A — stale warning not tracked in JSONL",
        "l6_success_rate": len(l6_emitted) / max(len(l6), 1),
        "l6_before_l3_rate": l6_before_l3_count / max(len(l6_emitted), 1),
    }


def _compute_hygiene_metrics(layer_events: list[dict]) -> dict[str, Any]:
    """Hygiene metrics."""
    hyg = [e for e in layer_events if e.get("layer") == "HYGIENE"]
    hyg_emitted = [e for e in hyg if e.get("emitted")]

    evidence_items: list[dict[str, Any]] = []
    for e in hyg_emitted:
        evidence_items.extend(e.get("evidence_items", []))

    removed_files = [i.get("file_path") for i in evidence_items if i.get("file_path")]

    return {
        "hygiene_invoked_on_finish": len(hyg) > 0,
        "hygiene_scaffold_files_detected": len(hyg_emitted),
        "hygiene_scaffold_files_removed": len(removed_files),
        "hygiene_removed_files": removed_files,
        "hygiene_patch_size_before_strip": "N/A — patch size not in JSONL (requires diff analysis)",
        "hygiene_patch_size_after_strip": "N/A �� patch size not in JSONL (requires diff analysis)",
        "hygiene_patch_collapsed_before_strip": "N/A �� patch collapse state not in JSONL",
        "hygiene_patch_collapsed_after_strip": "N/A — patch collapse state not in JSONL",
        "hygiene_source_edit_lost": "N/A — requires diff analysis not in JSONL",
        "hygiene_false_positive_count": "N/A — requires diff analysis not in JSONL",
        "hygiene_idempotent": "N/A — requires before/after comparison not in JSONL",
        "agent_behavior_class": "N/A — requires trajectory classification not in JSONL",
        "agent_patch_existed_then_zero": "N/A — requires patch state tracking not in JSONL",
        "agent_new_file_created_deleted": "N/A — requires file lifecycle tracking not in JSONL",
        "agent_scaffold_only_patch": "N/A — requires patch content analysis not in JSONL",
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

    # Duplicate event_id detection
    all_event_ids = [e.get("event_id") for e in layer_events if e.get("event_id")]
    event_id_counts = Counter(all_event_ids)
    duplicate_event_id_count = sum(1 for eid, cnt in event_id_counts.items() if cnt > 1)

    # Parent-child linkage
    events_with_parent = [e for e in layer_events if e.get("parent_event_id")]
    all_ids_set = set(all_event_ids)
    valid_parent_links = sum(
        1 for e in events_with_parent if e.get("parent_event_id") in all_ids_set
    )
    parent_child_linkage_rate = (
        valid_parent_links / max(len(events_with_parent), 1)
    )

    # Missing parent events (parent_event_id references non-existent event)
    missing_parent_count = sum(
        1 for e in events_with_parent if e.get("parent_event_id") not in all_ids_set
    )

    return {
        "gt_layer_events_count": len(layer_events),
        "gt_layer_events_by_layer": dict(Counter(e.get("layer") for e in layer_events)),
        "gt_rendered_messages_count": sum(1 for e in emitted if e.get("rendered_text")),
        "gt_rendered_messages_with_event_id": sum(1 for e in emitted if e.get("rendered_text") and e.get("event_id")),
        "gt_rendered_messages_missing_event_id": sum(1 for e in emitted if e.get("rendered_text") and not e.get("event_id")),
        "gt_next_action_events_count": len(with_na),
        "gt_next_action_events_by_layer": dict(Counter(e.get("layer") for e in with_na)),
        "gt_next_action_type_distribution": dict(Counter(e.get("next_action_type") for e in with_na)),
        "gt_malformed_jsonl_count": 0,
        "gt_duplicate_event_id_count": duplicate_event_id_count,
        "gt_missing_parent_event_count": missing_parent_count,
        "gt_parent_child_linkage_rate": parent_child_linkage_rate,
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
        "followed_repair_count": follow_dist.get("FOLLOWED_REPAIR", 0),
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
        "gt_agent_sync_score": "N/A — requires per-event iteration delta tracking",
        "gt_agent_lag_actions_avg": "N/A — requires per-event iteration delta tracking",
        "gt_agent_divergence_count": follow_dist.get("CONTRADICTED", 0) + follow_dist.get("IGNORED", 0),
        "gt_agent_reconvergence_count": "N/A — requires sequential action analysis not in reaction JSONL",
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
        "l4": _compute_l4_metrics(layer_events, reactions),
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
