# GOAL: Deep Layer Grounded Metrics — Fill Every Cell

This document is the goal. Every metric below must be computed from structured JSONL records.
No stdout-only metrics. No fired counts as utilization. Every cell must be filled.

Source: user prompt 2026-05-15, Phases 8-9.

---

## Utilization Score Per Layer

| Score | Meaning |
|---|---|
| 0.00 | absent or fired-only fake metric |
| 0.25 | emits text but no structured GT-side values |
| 0.50 | structured GT-side values but no agent-side reaction |
| 0.75 | structured GT-side values + agent-side reaction measurement |
| 1.00 | structured values + correct suppression + agent behavior proxy proves usefulness |

Layer is unhealthy below 0.75.

---

## L1 GT-side

- l1_brief_generated
- l1_brief_injected
- l1_candidate_count
- l1_candidate_files
- l1_candidate_symbols
- l1_candidates_with_bm25_signal_count
- l1_candidates_with_graph_edge_count
- l1_candidates_with_call_edge_count
- l1_candidates_with_import_edge_count
- l1_candidates_with_test_edge_count
- l1_candidates_with_signature_count
- l1_candidates_with_primary_witness_count
- l1_primary_witness_file
- l1_primary_witness_symbol
- l1_primary_witness_type
- l1_confidence_score
- l1_confidence_level
- l1_confidence_basis
- l1_abstain_reason
- l1_sparse_graph_warning_present
- l1_generated_code_warning_present
- l1_truncation_warning_present
- l1_hub_suppression_triggered
- l1_bm25_only_fallback_triggered

## L1 Agent-side

- agent_first_search_query
- agent_first_search_terms_overlap_issue
- agent_first_search_terms_overlap_l1
- agent_first_file_read
- agent_first_file_read_in_l1
- agent_first_file_read_in_l1_neighbor
- agent_first_file_read_is_l1_witness
- agent_first_source_edit
- agent_first_source_edit_in_l1
- agent_first_source_edit_in_l1_neighbor
- agent_first_source_edit_is_l1_witness_related
- agent_opened_l1_candidate_within_1
- agent_opened_l1_candidate_within_3
- agent_opened_l1_candidate_within_5
- agent_opened_l1_neighbor_within_5
- agent_opened_l1_witness_within_5
- agent_promoted_non_l1_path
- agent_non_l1_path_supported_by_runtime_evidence
- agent_non_l1_path_supported_by_graph_evidence
- agent_non_l1_path_supported_by_search_evidence

## L1 Tandem

- l1_gt_agent_sync_score
- l1_orientation_acceleration
- l1_turns_to_first_relevant_read
- l1_turns_to_first_relevant_edit
- l1_turns_to_gold_read_delta (if benchmark labels available)
- l1_turns_to_gold_edit_delta (if benchmark labels available)
- l1_total_actions_delta (if baseline available)
- l1_first_scaffold_iteration_delta (if baseline available)
- l1_candidate_use_rate
- l1_neighbor_use_rate
- l1_witness_use_rate
- l1_non_l1_promotion_rate
- l1_dampening_risk_detected
- l1_gt_pullback_to_l1_count
- l1_agent_found_better_path_and_gt_supported_it
- l1_agent_found_better_path_and_gt_fought_it

## L1 Hard Fail

- GT pulls agent back to weak L1 when stronger non-L1 runtime/search/graph evidence exists.

---

## L3 GT-side

- l3_edit_events_seen
- l3_source_edit_events
- l3_config_edit_events
- l3_evidence_emitted
- l3_suppressed_count
- l3_suppression_reason_distribution
- l3_actual_code_line_count
- l3_caller_code_line_count
- l3_caller_file
- l3_caller_symbol
- l3_consumer_count
- l3_importer_count
- l3_signature_count
- l3_sibling_pattern_count
- l3_test_assertion_count
- l3_issue_overlap_count
- l3_supports_current_path
- l3_contradicts_current_path
- l3_weak_evidence_flag
- l3_next_action_type
- l3_next_action_file
- l3_next_action_source
- l3_next_action_confidence
- l3_rendered_tokens
- l3_exceeded_cap
- l3_metadata_only_count

## L3 Agent-side

- agent_followed_l3_within_1
- agent_followed_l3_within_3
- agent_followed_l3_within_5
- agent_opened_l3_next_action_file
- agent_ran_l3_next_action_command
- agent_ran_static_sanity_after_l3
- agent_ran_broad_check_after_l3
- agent_edited_l3_related_file
- agent_changed_diff_after_l3
- agent_ignored_l3
- agent_contradicted_l3

## L3 Utilization

- l3_next_action_population_rate
- l3_reaction_coverage_rate
- l3_follow_rate_within_3
- l3_ignore_rate
- l3_broad_only_rate
- l3_patch_change_after_follow_rate
- l3_tokens_per_follow
- l3_agent_gt_sync_gap_actions

---

## L3b GT-side

- l3b_file_read_events
- l3b_navigation_eligible_events
- l3b_navigation_emitted
- l3b_suppressed_count
- l3b_caller_edge_count
- l3b_callee_edge_count
- l3b_importer_edge_count
- l3b_primary_edge_type
- l3b_primary_edge_file
- l3b_primary_edge_reason
- l3b_primary_edge_issue_overlap
- l3b_primary_edge_confidence
- l3b_alternative_edges_structured_only_count
- l3b_edges_rendered_count
- l3b_already_visited_suppressed_count
- l3b_hub_suppressed_count
- l3b_broad_navigation_after_60pct_count
- l3b_iteration_band
- l3b_decay_applied
- l3b_token_cap_for_band
- l3b_rendered_tokens
- l3b_total_chars_per_task
- l3b_exceeded_cap

## L3b Agent-side

- agent_followed_l3b_edge_within_1
- agent_followed_l3b_edge_within_3
- agent_followed_l3b_edge_within_5
- agent_opened_l3b_primary_edge_file
- agent_ignored_l3b_edge
- agent_extra_reads_without_edit_after_l3b
- agent_drifted_after_l3b

## L3b Utilization

- l3b_primary_edge_follow_rate
- l3b_ignore_rate
- l3b_avg_chars_per_fire
- l3b_total_chars_per_task
- l3b_token_reduction_vs_baseline
- l3b_late_suppression_rate

---

## L4 Metrics

- l4_prefetch_eligible
- l4_prefetch_emitted
- l4_prefetch_suppressed
- l4_suppression_reason
- l4_git_precedent_count
- l4_constraint_count
- l4_duplicate_with_l1_count
- l4_duplicate_with_l3b_count
- l4_dead_tool_reference_count
- l4_rendered_tokens
- agent_used_l4_prefetch_signal
- agent_first_edit_related_to_l4
- l4_prefetch_use_rate
- l4_dead_weight_rate

---

## L5 Metrics

- l5_agent_events_seen_total
- l5_agent_events_by_bucket
- l5_agent_events_by_type
- l5_agent_events_considered
- l5_agent_events_suppressed
- l5_detection_candidate_count
- l5_detection_fired_count
- l5_detection_suppressed_count
- l5_detection_too_late_count
- l5_durable_edit_started_count
- l5_structural_witness_ignored_count
- l5_weak_verification_after_edit_count
- l5_finish_with_unverified_edit_count
- l5_patch_collapsed_or_lost_count
- l5_no_durable_progress_count
- l5_repeated_unproductive_loop_count
- l5_stale_context_path_count
- l5_low_confidence_context_drift_count
- l5_hypothesis_falsified_count
- l5_strong_verification_after_edit_count
- l5_current_patch_verified_status
- l5_structural_witness_count
- l5_verification_strength_after_edit
- l5_detection_to_l5b_rate
- l5_detection_blocked_by_safety_count
- l5_detection_to_agent_follow_rate
- l5_false_silence_count
- l5_too_late_rate

## L5 Per-Event Log Fields

Every L5 event in JSONL must have:

- l5_event_type
- event_bucket
- triggering_agent_action_type
- triggering_file
- triggering_file_kind
- triggering_command
- triggering_check_kind
- iteration_band
- latest_durable_edit_file
- latest_durable_edit_kind
- latest_durable_edit_iter
- structural_witness_available
- structural_witness_type
- structural_witness_file
- structural_witness_followed
- verification_kind_after_edit
- verification_strength
- confidence_level
- confidence_basis
- fired
- suppressed
- suppression_reason
- l5b_eligible
- l5b_emitted
- l5b_blocked_by_safety
- agent_followed_l5b_within_3

---

## L5b Metrics

- l5b_intervention_eligible
- l5b_message_emitted
- l5b_message_suppressed
- l5b_suppression_reason
- l5b_parent_l5_event_id
- l5b_message_type
- l5b_next_action_type
- l5b_next_action_file
- l5b_rendered_tokens
- l5b_safety_checker_called
- l5b_safety_checker_passed
- l5b_restart_language_present
- l5b_late_broad_exploration_present
- l5b_append_only_confirmed
- agent_followed_l5b_within_1
- agent_followed_l5b_within_3
- agent_followed_l5b_within_5
- agent_opened_l5b_next_action_file
- agent_ran_l5b_next_action_command
- agent_ran_static_sanity_after_l5b
- agent_ran_broad_check_after_l5b
- agent_edited_target_file_after_l5b
- agent_finished_without_action_after_l5b
- agent_ignored_l5b
- l5b_follow_rate_within_3
- l5b_ignore_rate
- l5b_broad_only_after_warning_rate
- l5b_tokens_per_follow

---

## L6 Metrics

- l6_reindex_attempt_count
- l6_reindex_success_count
- l6_reindex_failure_count
- l6_reindex_latency_ms
- l6_reindex_before_l3
- l6_edge_count_before
- l6_edge_count_after
- l6_edges_changed
- l6_caller_count_after
- l6_consumer_count_after
- l6_graph_updated_for_edited_file
- l6_stale_index_detected
- l3_after_l6_used_fresh_graph
- l3_after_l6_stale_warning_present
- l6_success_rate
- l6_before_l3_rate

---

## Hygiene Metrics

- hygiene_invoked_on_finish
- hygiene_scaffold_files_detected
- hygiene_scaffold_files_removed
- hygiene_removed_files
- hygiene_patch_size_before_strip
- hygiene_patch_size_after_strip
- hygiene_patch_collapsed_before_strip
- hygiene_patch_collapsed_after_strip
- hygiene_source_edit_lost
- hygiene_false_positive_count
- hygiene_idempotent
- agent_behavior_class
- agent_patch_existed_then_zero
- agent_new_file_created_deleted
- agent_scaffold_only_patch

---

## Meta/Reaction

- gt_layer_events_count
- gt_layer_events_by_layer
- gt_rendered_messages_count
- gt_rendered_messages_with_event_id
- gt_rendered_messages_missing_event_id
- gt_next_action_events_count
- gt_next_action_events_by_layer
- gt_next_action_type_distribution
- gt_malformed_jsonl_count
- gt_duplicate_event_id_count
- gt_missing_parent_event_count
- gt_parent_child_linkage_rate
- gt_suppressed_events_with_reason_rate
- reaction_events_count
- reaction_events_by_layer
- reaction_coverage_rate
- reaction_missing_for_next_action_count
- followed_exact_count
- followed_related_file_count
- followed_structural_witness_count
- followed_broad_only_count
- followed_repair_count
- partial_count
- ignored_count
- contradicted_count
- too_late_count
- not_measurable_count
- followed_within_1_count
- followed_within_3_count
- followed_within_5_count
- event_to_reaction_join_rate
- next_action_to_reaction_rate
- gt_agent_sync_score
- gt_agent_lag_actions_avg
- gt_agent_divergence_count
- gt_agent_reconvergence_count

---

## What "Ready" Means

Every metric above has a value (number, string, bool, or "N/A — not applicable for this task").
No metric is blank. No metric is "layer fired = true."
Utilization score per layer >= 0.75 or documented reason why not.
Proof spine passes (event_ids, suppression_reasons, reaction coverage).
