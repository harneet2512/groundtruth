# METRICS_CONTRACT_V2 — FINAL_ARCH_V2

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 2

No layer is considered integrated unless its required GT-side and agent-side metrics are observable.

---

## L0 Graph Substrate

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `graph_db_exists` | GT | `full_run.log` or filesystem check | Grep for `install_graph_and_hook` success / `graph.db` file existence | Graph not built; all downstream layers blind | No (prerequisite) | YES |
| `graph_schema_valid` | GT | `full_run.log` | Grep `CHECK 2: graph.db schema OK` in pre-flight or `verify_graph_db_schema` success | Stale binary; trust_tier columns missing | No (prerequisite) | YES |
| `graph_host_readable` | GT | `full_run.log` | Grep `[GT_META] B-7 pre-fetch: graph.db downloaded to host` | Router blind; all L3 V2 emissions suppressed `no_graph_db` | No (prerequisite for V2 live) | YES (for V2 arms) |
| `graph_ready_before_first_post_view` | GT | `full_run.log` | B-7 pre-fetch log appears BEFORE first `[GT_META] router_v2 on_view` | Router cached with empty DB on first call | No (prerequisite for V2) | YES (for V2 arms) |
| `graph_refreshed_after_l6` | GT | `gt_layer_events_*.jsonl` | L6 `reindex` events with `emitted=True` + mtime_delta > 0 | Post-edit graph stale; L3 evidence based on pre-edit state | Indirectly (stale evidence quality) | YES |
| `router_cache_reset_after_graph_refresh` | GT | `full_run.log` | Grep `router_v2_reset=True` after L6 reindex | Router uses stale graph.db for rest of task | No (V2 correctness) | YES (for V2 arms) |

## L1 Pre-task Seed

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `l1_brief_emitted` | GT | `gt_interactions_*.jsonl` | Layer=L1, type != empty/suppressed | Brief not injected; agent starts blind | Indirectly (missed localization) | YES |
| `l1_candidate_files` | GT | `gt_interactions_*.jsonl` or `/tmp/gt_brief_candidates.txt` | Count of files in `<gt-task-brief>` block | Brief quality: 0 = empty brief, 1-8 = normal | No (L1 quality only) | YES |
| `l1_candidate_symbols` | GT | `<gt-task-brief>` content in output.jsonl | Count of function names in brief | Symbol density of brief | No | NO |
| `l1_agent_opened_candidate` | Agent | output.jsonl | For each L1 candidate file, check if agent opened it (FileReadObservation matching candidate path) | Agent engagement with brief | Indirectly | NO |
| `l1_agent_searched_candidate` | Agent | output.jsonl | CmdRunAction containing candidate filename as grep/find target | Agent used brief as search seed | Indirectly | NO |
| `l1_candidate_before_first_gold_view` | Agent | output.jsonl | Was any L1 candidate file in gold set AND viewed before first_gold_view_step? | Brief contributed to gold discovery | Yes (collaboration efficiency) | NO |

## L2 AgentState

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `viewed_files_recorded` | GT | `/tmp/gt_agent_state_<task>.json` | Count of `viewed_files` entries | State not tracking agent trajectory | No (internal health) | YES |
| `edited_files_recorded` | GT | `/tmp/gt_agent_state_<task>.json` | Count of `edited_files` entries | State not tracking edits | No | YES |
| `searches_recorded` | GT | `/tmp/gt_agent_state_<task>.json` | Count of `searches` entries | State missing search context | No | NO |
| `current_focus_set` | GT | `/tmp/gt_agent_state_<task>.json` | `current_focus` is non-empty | Focus tracking broken | No | NO |
| `pending_suggestions_registered` | GT | `/tmp/gt_agent_state_<task>.json` | Count of `pending_suggestions` ever registered | Suggestion tracking broken | No | YES (for V2) |
| `followed_suggestions` | Agent | `/tmp/gt_agent_state_<task>.json` | Count of suggestions with status FOLLOWED_EXACT or FOLLOWED_RELATED_FILE | Agent followed GT guidance | Yes (collaboration signal) | NO |
| `ignored_suggestions` | Agent | `/tmp/gt_agent_state_<task>.json` | Count with status IGNORED | Agent did not follow GT guidance | Diagnostic | NO |
| `expired_suggestions` | Agent | `/tmp/gt_agent_state_<task>.json` | Count with status TOO_LATE | Suggestion was stale | Diagnostic | NO |
| `state_matches_output_jsonl` | GT | Cross-reference state JSON vs output.jsonl | Viewed/edited files in state match FileReadObservation/FileEditAction in trajectory | State tracking drift | No (health check) | NO |

## L3 Collaboration Router

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `router_called_count` | GT | `gt_layer_events_*.jsonl` | Count of `layer=L3_router_v2` events | Router not invoked | No (prerequisite) | YES (for V2) |
| `router_emit_count` | GT | `gt_layer_events_*.jsonl` | Count where `emitted=True` | Router suppressed everything | Indirectly | YES (for V2) |
| `router_suppression_count_by_reason` | GT | `gt_layer_events_*.jsonl` | Group by `suppression_reason` | Identifies dominant suppression cause (no_graph_db, budget, debounce, etc.) | Diagnostic | YES (for V2) |
| `router_no_graph_db_count` | GT | `gt_layer_events_*.jsonl` | Count where `suppression_reason=no_graph_db` | B-7 not fixed; router blind | No (prerequisite fix) | YES (for V2; must be 0 after B-7) |
| `router_emit_before_gold` | GT+Agent | Cross-reference: router emit events with step < first_gold_view_step | Router provided evidence before agent found gold | Yes (localization help) | NO |
| `router_evidence_agent_visible` | Agent | output.jsonl | Count of `[GT-router-v2 on_view]` or `[GT-router-v2 on_edit]` in observation content | Evidence actually reached agent | Yes (delivery confirmation) | YES (for V2) |
| `agent_followed_router_edge` | Agent | output.jsonl + gt_layer_events | After router emit naming file X, did agent open X within 3 actions? | Agent used router guidance | Yes (collaboration) | NO |

### Legacy L3/L3b metrics (when GT_ROUTER_V2=off)

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `injections_per_task` | GT | output.jsonl | Count of `[GT]`, `Called by:`, `CALLERS:`, `SIGNATURE:`, `SIBLING:`, `TWINS:` in observation content | Volume of GT evidence delivered | Diagnostic (too many = over-injection) | YES |
| `bridge_event_before_gold` | GT+Agent | output.jsonl + gold file list | Count of GT evidence referencing gold file before agent's first_gold_view_step | GT helped agent navigate to gold | Yes | YES |
| `stale_guidance_count` | GT | output.jsonl | Count of GT evidence referencing files agent already visited | Wasted injection | Negative (waste) | YES |
| `late_guidance_count` | GT | output.jsonl + first_gold_edit_step | Count of GT evidence arriving after agent already edited gold | Too-late evidence | Negative (timing bug) | YES |

## L4 Evidence Providers

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `provider_request_count` | GT | `gt_layer_events_*.jsonl` (router events) or `gt_interactions_*.jsonl` (legacy events) | Count of L3/L3b events where evidence was requested | Provider invocation volume | No | NO |
| `provider_empty_count` | GT | `gt_layer_events_*.jsonl` | Count where `evidence_items=0` or `suppression_reason=no_evidence` | Provider couldn't find relevant data | Diagnostic | NO |
| `evidence_type_count` | GT | `gt_interactions_*.jsonl` | Group by evidence marker type (CALLERS, SIGNATURE, SIBLING, TWINS, etc.) | Provider diversity | No | NO |
| `evidence_references_unvisited_file` | GT+Agent | Cross-reference evidence content vs viewed_files | Count of evidence items pointing to files agent hasn't seen | Novel information delivery | Yes (navigation value) | NO |
| `evidence_references_gold_or_relevant_file` | GT | Cross-reference evidence content vs gold file list | Count of evidence items pointing to gold/relevant files | Evidence precision | Yes | NO |
| `provider_output_rendered_to_agent` | Agent | output.jsonl | Was provider output actually in agent observation? (Not just logged) | Delivery confirmation | Yes | YES |

## L5 Post-edit Validator

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `validator_called_count` | GT | `gt_layer_events_*.jsonl` | Count of layer=L5 events from validator (not governor) | **Currently 0 — validator unwired** | Indirectly (missed contradictions) | NO (not wired yet) |
| `actionable_contradiction_count` | GT | `gt_layer_events_*.jsonl` | Count of PostEditWarning emitted | Validator found contract breaks | Yes (prevents wrong fixes) | NO |
| `gt_ok_or_narration_count` | GT | output.jsonl | Count of `[GT_OK]` in agent observations. **Must be 0.** | Noise injected to agent | Negative | YES |
| `agent_reedited_after_validator` | Agent | output.jsonl | After validator warning, did agent edit the same file again? | Agent responded to validator | Yes | NO |
| `validator_warning_before_repair` | GT+Agent | output.jsonl timing | Validator warning step < agent repair step | Warning was timely | Yes | NO |

## L6 Paired Behavior (whole-path metrics)

| Metric | Side | Source artifact | Parser logic | Failure meaning | Can cause flips? | Required before canary? |
|---|---|---|---|---|---|---|
| `first_gold_view_step` | Agent | output.jsonl + gold file list | Step index of first FileReadObservation matching gold file | Localization speed | Yes (primary) | YES |
| `first_gold_edit_step` | Agent | output.jsonl + gold file list | Step index of first FileEditAction matching gold file | Commitment speed | Yes (primary) | YES |
| `files_viewed_before_gold` | Agent | output.jsonl + gold file list | Count of DISTINCT files viewed before first_gold_view_step | Navigation efficiency | Yes | YES |
| `action_count` | Agent | output.jsonl | Total actions in trajectory | Overall efficiency | Yes (primary) | YES |
| `action_economy_vs_baseline` | Agent | Paired output.jsonl | `action_count_GT / action_count_BL` per task | GT overhead vs benefit | Yes (primary gate) | YES |
| `action_economy_vs_old_gt` | Agent | Paired output.jsonl | `action_count_V2 / action_count_OLD_GT` per task | V2 vs legacy GT comparison | Yes (primary gate) | YES |
| `edit_file_precision` | Agent | output.jsonl + gold file list | `gold_files_edited / total_files_edited` | Edit accuracy | Yes | YES |
| `bridge_event_before_gold` | GT+Agent | output.jsonl + gold file list | GT evidence referencing gold before agent found it | GT helped | Yes | YES |
| `stale_guidance_count` | GT | output.jsonl | GT evidence referencing already-visited files | Wasted injection | Negative | YES |
| `late_guidance_count` | GT | output.jsonl | GT evidence after agent already edited gold | Timing failure | Negative | YES |
| `injections_per_task` | GT | output.jsonl | Total GT evidence injected | Volume indicator | Diagnostic | YES |
| `resolved` | Agent | eval_result.json / report.json | Task resolved by SWE-bench eval harness | **Lagging only — not a gate** | Yes (lagging outcome) | YES (but not a gate) |

---

## Metric Observability Assessment

| Layer | GT-side observable? | Agent-side observable? | Notes |
|---|---|---|---|
| L0 | YES (logs, schema check) | N/A | All metrics computable from full_run.log |
| L1 | YES (gt_interactions) | PARTIAL (requires cross-referencing output.jsonl) | l1_agent_opened_candidate requires trajectory parsing |
| L2 | YES (JSON sidecar) | PARTIAL (requires cross-referencing output.jsonl) | state_matches_output_jsonl is a validation check |
| L3 V2 | YES (gt_layer_events) | YES (output.jsonl [GT-router-v2] markers) | Full observability when router emits |
| L3 Legacy | YES (gt_interactions) | YES (output.jsonl [GT] markers) | Proven working |
| L4 | PARTIAL (embedded in L3 events) | PARTIAL (via rendered content) | No standalone provider telemetry yet |
| L5 Validator | **NO — unwired** | **NO — unwired** | Dead code. All L5 validator metrics will be 0. |
| L5 Governor | YES (gt_layer_events) | PARTIAL (only when L5b fires) | 0 fires on 30-task run; max 2 injections/task rule |
| L6 | YES (metrics scripts) | YES (output.jsonl + eval) | Full observability |

---

## Required Metric Parser (compute_canary_metrics.py extensions)

The current `compute_canary_metrics.py` computes L6 paired behavior metrics. The following metrics are NOT yet computed and need parser logic:

| Metric | Parser needed | Effort |
|---|---|---|
| L0: graph_db_exists, graph_schema_valid, graph_host_readable | Grep full_run.log for marker strings | Low |
| L0: graph_ready_before_first_post_view | Compare timestamps of B-7 pre-fetch vs first on_view | Medium |
| L1: l1_brief_emitted, l1_candidate_files | Parse gt_interactions for L1 events | Low |
| L2: viewed/edited_files_recorded | Read AgentState JSON sidecar | Low |
| L3 V2: router_called/emit/suppression counts | Parse gt_layer_events for L3_router_v2 | Low (already done in deep telemetry) |
| L3 V2: router_evidence_agent_visible | Grep output.jsonl for [GT-router-v2] | Low |
| L4: provider counts | Parse gt_layer_events evidence_items | Medium |
| L5: all | **Blocked until validator is wired** | N/A |
