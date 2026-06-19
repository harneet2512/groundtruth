# deep_metrics.md — What "Deep Metrics" Means

> **When the user says "deep metrics" or "deep utilization," report ALL of the metrics below for EVERY layer, from BOTH the GT-side and agent-side perspectives. Do NOT report fired/not-fired counts. Do NOT report surface summaries. Read the actual JSONL content and compute these.**

---

## How to Compute

Download the GHA artifacts. Read:
1. `gt_layer_events_{task}.jsonl` — every GT emission/suppression
2. `gt_interactions_{task}.jsonl` — every GT→agent interaction with event_id
3. `gt_agent_reactions.jsonl` — post-hoc follow classification
4. `gt_belief_ledger_{task}.jsonl` — belief transitions
5. `output.jsonl` — real agent trajectory (94+ history entries)
6. `reaction_summary.json` — aggregate follow_type distribution

Parse these files. Do not guess from stdout logs.

---

## L1 (Pre-Task Brief)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l1_candidate_count | layer_events evidence_items where kind=l1_candidate | how many candidates L1 proposed |
| l1_candidates_with_graph_edge | evidence_items where kind=l1_candidate and file has CALLS edges | candidates backed by call graph |
| l1_candidates_with_test_edge | evidence_items where kind=l1_candidate and file has test mapping | candidates with test coverage |
| l1_rendered_chars | layer_events rendered_chars | token cost of the brief |
| l1_rendered_tokens | rendered_chars / 4 | approximate token count |
| l1_abstained | layer_events where suppressed=true | L1 chose not to emit |
| l1_abstain_reason | suppression_reason field | why L1 abstained |
| l1_belief_candidates_created | belief_ledger where new_status=candidate | belief seeds planted |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| first_file_read_in_l1 | output.jsonl first read action, check if file is in L1 candidates | did agent start with an L1 candidate |
| first_source_edit_in_l1 | output.jsonl first edit action, check if file is in L1 candidates | did agent edit an L1 candidate first |
| first_source_edit_in_l1_neighbor | check if first edit is in L1 candidates' Calls: list | did agent edit a neighbor |
| non_l1_candidate_promoted | belief_ledger where new_status=promoted | agent found a better file not in L1 |
| stale_l1_path_detected | belief_ledger where new_status=stale | L1 candidate abandoned by agent |
| gt_pullback_to_l1_count | count L3/L3b evidence pointing back to L1 candidates after agent strayed | GT tried to pull agent back |
| confirming_edges_opened_before_first_edit | output.jsonl reads of L1 Calls: files before first edit | did agent explore before committing |
| turns_to_gold_read | output.jsonl iter of first read of gold file | speed to localization |
| turns_to_gold_edit | output.jsonl iter of first edit of gold file | speed to commitment |

---

## L3 (Post-Edit Evidence)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l3_edit_events_seen | layer_events where layer=L3 | total L3 events (emitted + suppressed) |
| l3_evidence_emitted | layer_events where layer=L3 and emitted=true | fires with real evidence |
| l3_suppressed_count | layer_events where layer=L3 and suppressed=true | fires with no evidence |
| l3_suppression_reasons | suppression_reason distribution | why L3 suppressed (no_evidence, duplicate, etc.) |
| l3_caller_code_count | evidence_items where kind=l3_caller_code | actual code lines shown |
| l3_signature_count | evidence_items where kind=l3_signature | signatures shown |
| l3_test_assertion_count | evidence_items where kind=l3_test_assertion | test assertions shown |
| l3_sibling_count | evidence_items where kind=l3_sibling_pattern | sibling patterns shown |
| l3_targeted_verification_count | evidence_items where kind=l3_targeted_verification | test suggestions shown |
| l3_rendered_chars_total | sum of rendered_chars for emitted L3 events | total token cost |
| l3_rendered_chars_avg | average rendered_chars per emitted L3 event | per-fire cost |
| l3_rendered_chars_max | max rendered_chars across L3 events | worst-case bloat |
| l3_next_action_type_distribution | Counter of next_action_type for L3 events | READ_CALLER_CONTRACT vs CHECK_SIGNATURE vs RUN_TARGETED_TEST etc. |
| l3_next_action_populated_rate | L3 events with next_action / total L3 emitted | % with actionable suggestion |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| l3_followed_within_1 | reactions where gt_layer=L3 and followed_within_1=true | agent followed immediately |
| l3_followed_within_3 | reactions where gt_layer=L3 and followed_within_3=true | agent followed within 3 actions |
| l3_followed_within_5 | reactions where gt_layer=L3 and followed_within_5=true | agent followed within 5 actions |
| l3_follow_type_distribution | Counter of follow_type for L3 reactions | FOLLOWED_EXACT / RELATED / BROAD / IGNORED |
| l3_opened_suggested_file | reactions where gt_layer=L3 and opened_suggested_file=true | agent read the file GT suggested |
| l3_edited_suggested_file | reactions where gt_layer=L3 and edited_suggested_file=true | agent edited the file GT suggested |
| l3_ran_broad_test_after | reactions where gt_layer=L3 and ran_broad_test_after_gt=true | agent ran broad test instead of structural check |
| l3_ignored_count | reactions where gt_layer=L3 and follow_type=IGNORED | agent completely ignored L3 suggestion |

---

## L3b (Post-View Navigation)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l3b_fire_count | layer_events where layer=L3b and emitted=true | total navigation fires |
| l3b_suppressed_count | layer_events where layer=L3b and suppressed=true | suppressed fires |
| l3b_caller_edge_count | evidence_items where kind=l3b_caller_edge | caller edges shown |
| l3b_callee_edge_count | evidence_items where kind=l3b_callee_edge | callee edges shown |
| l3b_importer_edge_count | evidence_items where kind=l3b_importer_edge | importer edges shown |
| l3b_primary_edge_count | evidence_items where primary_edge=true | primary edges selected |
| l3b_rendered_chars_total | sum of rendered_chars for emitted L3b events | total token cost |
| l3b_rendered_chars_avg | average rendered_chars per L3b fire | avg per-fire cost (target: <300 mid, <80 late) |
| l3b_rendered_chars_max | max rendered_chars across L3b events | worst-case bloat |
| l3b_decay_applied_count | evidence_items where kind=l3b_decay_metadata and decay_applied=true | how often decay kicked in |
| l3b_iteration_band_distribution | Counter of iteration_band from decay_metadata items | early/mid/late/final fire distribution |
| l3b_broad_navigation_after_60pct | decay_metadata where broad_navigation_after_60pct=true | violations of late-run rule |
| l3b_next_action_type_distribution | Counter of next_action_type for L3b events | READ_CALLER_CONTRACT vs READ_CONSUMER etc. |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| l3b_followed_within_1 | reactions where gt_layer=L3b and followed_within_1=true | agent followed immediately |
| l3b_followed_within_3 | reactions where gt_layer=L3b and followed_within_3=true | agent followed within 3 actions |
| l3b_follow_type_distribution | Counter of follow_type for L3b reactions | FOLLOWED_EXACT / RELATED / IGNORED |
| l3b_opened_suggested_file | reactions where gt_layer=L3b and opened_suggested_file=true | agent read the suggested edge |
| l3b_ignored_count | reactions where gt_layer=L3b and follow_type=IGNORED | agent ignored navigation |
| l3b_caused_extra_reads | count of agent reads within 3 actions of L3b fire that match any L3b edge | L3b-influenced navigation |

---

## L4 (Prefetch)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l4_emitted | layer_events where layer=L4 and emitted=true | prefetch fired |
| l4_suppressed | layer_events where layer=L4 and suppressed=true | prefetch suppressed |
| l4_rendered_chars | rendered_chars for L4 events | token cost |
| l4_evidence_items | evidence_items count for L4 | number of constraints/precedents |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| l4_agent_used_signal | check if agent's first actions reference L4 content | did prefetch help |

---

## L5 (Trajectory Detector)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l5_event_count | layer_events where layer=L5 | total detections |
| l5_event_type_distribution | Counter of event_type for L5 events | ignored_next_action / unverified_patch / unsafe_finish / etc. |
| l5_ignored_next_action_count | layer_events where layer=L5 and event_type=ignored_next_action | online tracker fired |
| l5_source_edits_tracked | count from governor telemetry | files the governor saw edited |
| l5_verification_kind_distribution | Counter of verification_kind for L5 events | broad vs targeted vs irrelevant |
| l5_broad_verification_pass_count | L5 events with verification_kind=broad_project_verification | agent ran broad tests |
| l5_targeted_verification_pass_count | L5 events with verification_kind=targeted_to_edited_file | agent ran targeted tests |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| l5_agent_followed_after_detection | reactions where gt_layer=L5 and followed_within_3=true | agent responded to L5 detection |
| l5_agent_ignored_detection | reactions where gt_layer=L5 and follow_type=IGNORED | agent ignored L5 detection |

---

## L5b (Intervention Renderer)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l5b_message_count | layer_events where layer=L5b and emitted=true | interventions appended to agent |
| l5b_blocked_count | layer_events where layer=L5b and suppressed=true | blocked by safety checker |
| l5b_suppression_reasons | suppression_reason for blocked L5b events | why blocked (restart_language, token_cap, late_exploration) |
| l5b_rendered_chars_avg | average rendered_chars for L5b events | per-message cost (target: <180 tokens = <720 chars) |
| l5b_rendered_chars_max | max rendered_chars | worst-case |
| l5b_next_action_type_distribution | Counter of next_action_type for L5b events | READ_CALLER_CONTRACT vs READ_CONSUMER etc. |
| l5b_parent_event_id_coverage | % of L5b events with parent_event_id set | L5→L5b linkage integrity |

### Agent-side
| Metric | Source | Description |
|--------|--------|-------------|
| l5b_followed_within_1 | reactions where gt_layer=L5b and followed_within_1=true | immediate compliance |
| l5b_followed_within_3 | reactions where gt_layer=L5b and followed_within_3=true | compliance within 3 actions |
| l5b_follow_type_distribution | Counter of follow_type for L5b reactions | FOLLOWED_EXACT / RELATED / BROAD / IGNORED |
| l5b_ran_targeted_test_after | reactions where gt_layer=L5b and ran_targeted_test_after_gt=true | agent ran targeted test after intervention |
| l5b_ran_broad_test_after | reactions where gt_layer=L5b and ran_broad_test_after_gt=true | agent ran broad test instead |
| l5b_finished_without_follow | reactions where gt_layer=L5b and finished_without_follow=true | agent finished ignoring intervention |
| l5b_agent_changed_diff_after | reactions where gt_layer=L5b and changed_diff_after_gt=true | agent modified patch after intervention |

---

## L6 (Reindex)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| l6_reindex_count | layer_events where layer=L6 | total reindex attempts |
| l6_reindex_success | layer_events where layer=L6 and event_type=reindex | successful reindexes |
| l6_reindex_skip | layer_events where layer=L6 and event_type=reindex_skip | skipped (binary missing) |
| l6_latency_ms | from evidence_items text field | reindex latency |
| l6_before_l3_rate | should be 1.0 by design | reindex runs before L3 |

---

## Hygiene (Scaffold Strip)

### GT-side
| Metric | Source | Description |
|--------|--------|-------------|
| hygiene_emitted | layer_events where layer=HYGIENE and emitted=true | files actually stripped |
| hygiene_suppressed | layer_events where layer=HYGIENE and suppressed=true | no scaffolds found |
| hygiene_files_stripped | count of evidence_items where kind=hygiene_strip | files removed |
| hygiene_real_source_removed | should be 0 always | safety check |

---

## Belief Ledger

| Metric | Source | Description |
|--------|--------|-------------|
| belief_candidate_count | belief_ledger where new_status=candidate | L1 seeds planted |
| belief_unverified_count | belief_ledger where new_status=unverified | files edited but not verified |
| belief_verified_count | belief_ledger where new_status=verified | targeted test passed |
| belief_stale_count | belief_ledger where new_status=stale | L1 candidate abandoned |
| belief_promoted_count | belief_ledger where new_status=promoted | non-L1 file promoted by agent |
| belief_contradicted_count | belief_ledger where new_status=contradicted | evidence against a file |

---

## Cross-Layer / Meta

| Metric | Source | Description |
|--------|--------|-------------|
| total_gt_events | count of layer_events | all GT events across all layers |
| total_emitted | layer_events where emitted=true | events that produced agent-visible output |
| total_suppressed | layer_events where suppressed=true | events that were suppressed |
| total_next_action_populated | layer_events where next_action_type not in (None, NONE, NONE_UNVERIFIABLE) | events with actionable suggestion |
| total_reactions | count of agent_reactions | reactions computed |
| total_followed | reactions where follow_type contains FOLLOWED | agent complied |
| total_ignored | reactions where follow_type=IGNORED | agent ignored |
| total_not_measurable | reactions where follow_type=NOT_MEASURABLE | cannot determine |
| follow_type_distribution | Counter of follow_type across all reactions | the master compliance distribution |
| next_action_type_distribution | Counter of next_action_type across all events | what GT suggested |
| event_id_coverage | % of emitted events with non-empty event_id | telemetry integrity |
| rendered_tokens_total | sum of all rendered_chars / 4 across all layers | total token cost of GT |
| reaction_to_next_action_ratio | total_reactions / total_next_action_populated | should be 1.0 |

---

## Agent Trajectory (from output.jsonl)

| Metric | Source | Description |
|--------|--------|-------------|
| total_agent_actions | count of real actions in output.jsonl | how much the agent did |
| total_reads | actions where type=read_file | files read |
| total_edits | actions where type=edit_file | files edited |
| total_commands | actions where type=run_command | commands run |
| total_test_commands | commands classified as test by test_command_classifier | tests run |
| broad_test_count | test commands classified as broad_project_verification | broad tests |
| targeted_test_count | test commands classified as targeted_to_edited_file/symbol | targeted tests |
| first_scaffold_iter | first action creating reproduce_/debug_/temp_ file | when scaffolding started |
| behavior_class | from task metrics | collapsed / resolved / scaffold_trap / etc. |
| diff_collapsed | from task metrics | did the patch collapse to zero |

---

## Task Outcome

| Metric | Source | Description |
|--------|--------|-------------|
| resolved | eval_result.json | did the patch pass eval |
| patch_applied | eval_result.json | did the patch apply cleanly |
| cost_usd | litellm_costs.jsonl | total LLM cost |
| llm_calls | count of cost entries | total LLM calls |

---

## Expectations

1. **Never report only counts.** Always show the distribution (follow_type, next_action_type, suppression_reason, evidence_item kinds).
2. **Never say "24 reactions = PASS."** Show what's inside the 24.
3. **If >50% NOT_MEASURABLE, the joiner is broken.** Stop and fix.
4. **If next_action = 0, feature flags are missing or code is dead.** Stop and fix.
5. **If L1 evidence_items = 0, L1 structured emission is broken.** Stop and fix.
6. **If L3b avg > 800 chars, primary edge selection isn't working.** Investigate.
7. **If belief_candidate_count = 0, belief seeds are missing.** L1 fix needed.
8. **For 30-task runs, report the DISTRIBUTION across tasks, not just the average.** Show min/max/median/p25/p75.
9. **Always compare against the previous run.** Show deltas.
10. **The goal is proving GT changed agent behavior, not proving GT fired.** Follow rate and behavioral mode changes are the real metrics.
