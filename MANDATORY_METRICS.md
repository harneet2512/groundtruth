# MANDATORY METRICS — every run, no exception

> **This file is the CONTRACT.** Every paid run (DeepSWE, OH, any harness) MUST
> compute and persist ALL metrics below. A run without its metrics file is NOT
> done — it cannot be cited, claimed, or compared. No rounding below 8 decimal
> places. No metric may be skipped silently.

Output file: `gt_deep_metrics_<task>.json` (per-task) + `gt_run_metrics_<run>.json` (aggregate)

---

## 1. LOCALIZATION PERFORMANCE

| metric | formula | unit |
|---|---|---|
| gold_in_L1_top_k | is gold file in GT's top-K? | bool |
| gold_rank | position of gold file in L1 ranking (999 if absent) | int |
| files_to_gold_view | unique files viewed before first gold file view | int |
| steps_to_gold_view | agent steps before first gold file view | int |
| files_to_gold_edit | unique files edited before first gold file edit | int |
| steps_to_gold_edit | agent steps before first gold file edit | int |
| localization_precision | \|edited ∩ gold\| / \|edited\| | float 0-1 |
| localization_recall | \|edited ∩ gold\| / \|gold\| | float 0-1 |
| false_file_rate | \|edited - gold\| / \|edited\| | float 0-1 |
| exploration_ratio | unique_files_viewed / unique_files_edited | float |
| gold_view_precision | gold_files_viewed / total_files_viewed | float 0-1 |
| wasted_views | files viewed not in final patch or gold | int |
| navigation_directness | gold_files_in_L1_top5 / total_gold_files | float 0-1 |
| self_localization_needed | grep/find/search commands before first gold view | int |

## 2. EDIT QUALITY

| metric | formula | unit |
|---|---|---|
| edit_attempts_per_gold | total_edits_to_gold_files / gold_files_edited | float |
| rewrite_count | times agent edited SAME file >1 (overwrote own work) | int |
| compile_failures_after_edit | build/type errors after agent edits | int |
| edit_revert_rate | undo/checkout commands / total_edits | float 0-1 |
| first_edit_correctness | first edit to gold file survived to final patch? | bool per file |
| patch_size | additions + deletions in final diff | int |
| patch_files | files in final diff | int |

## 3. INTERFACE PRESERVATION

| metric | formula | unit |
|---|---|---|
| contract_compliance_rate | edits preserving GT-warned interfaces / GT contract warnings | float 0-1 |
| signature_changes_warned | edits that changed a GT-warned signature | int (0=perfect) |
| p2p_regression_rate | 1 - p2p_pass_rate | float 0-1 |
| caller_breakage_count | callers that fail after agent edit | int |

## 4. SCOPE COMPLETENESS

| metric | formula | unit |
|---|---|---|
| scope_coverage | gold_files_edited / total_gold_files | float 0-1 |
| scope_excess | non_gold_files_edited / total_files_edited | float 0-1 |
| multi_file_discovery | for multi-file tasks: 2nd/3rd gold file found? | bool |
| scope_gap_files | gold files agent NEVER opened | int (0=perfect) |

## 5. STUCK/RECOVERY

| metric | formula | unit |
|---|---|---|
| degenerate_loop_count | consecutive identical/near-identical commands | int |
| steps_in_loops | total steps wasted in degenerate loops | int |
| nudge_recovery_steps | steps from nudge to next productive action | int |
| coherence_collapse_count | >50% rewrite of a file agent just wrote | int |
| stuck_duration | longest streak of non-productive steps | int |

## 6. VERIFY-BEFORE-SUBMIT

| metric | formula | unit |
|---|---|---|
| test_before_submit | agent ran tests in last 5 steps before submit? | bool |
| test_runs_total | total test invocations during run | int |
| test_edit_ratio | test_runs / edits | float |
| obligation_test_rate | obligations_tested / obligations_edited | float 0-1 |
| verify_gap | steps between last edit and first test after it | int |

## 7. GT BEHAVIORAL IMPACT

| metric | formula | unit |
|---|---|---|
| impact_rate | pivots / deliveries | float 0-1 |
| per_tag_impact | per-tag (nudge/contract/evidence/scope) pivot rate | dict |
| gt_tokens_injected | total chars in all <gt-*> blocks | int |
| gt_tokens_per_pivot | gt_tokens_injected / pivots | float |
| nudge_compliance_rate | nudges followed by correct action / nudges delivered | float 0-1 |

## 8. GT-SPECIFIC ATTRIBUTION

| metric | formula | unit |
|---|---|---|
| L1_followed_rate | L1_top5_files_opened / L1_top5_count | float 0-1 |
| contract_consulted_rate | edits where agent read contract BEFORE editing | float 0-1 |
| obligation_completion_rate | obligations_addressed / obligations_delivered | float 0-1 |
| nudge_action_rate | nudges followed by correct action / nudges | float 0-1 |
| scope_chain_followed | scope_files_opened / scope_files_listed | float 0-1 |

## 9. TOKEN/COST EFFICIENCY

| metric | formula | unit |
|---|---|---|
| total_steps | total agent API calls | int |
| total_tokens_in | prompt tokens consumed | int |
| total_tokens_out | completion tokens consumed | int |
| total_cost_usd | dollar cost of the run | float |
| cache_hit_rate | cache_hit / (cache_hit + cache_miss) | float 0-1 |
| tokens_per_gold_edit | total_tokens / gold_files_edited | float |
| cost_per_resolved | total_cost / resolved (aggregated) | float |
| gt_token_overhead | gt_injected_tokens / total_prompt_tokens | float 0-1 |
| wasted_token_rate | tokens on non-gold file reads/edits / total | float 0-1 |

## 10. CORRECTNESS

| metric | formula | unit |
|---|---|---|
| f2p_pass_rate | passed_f2p / total_f2p | float 0-1 |
| p2p_pass_rate | passed_p2p / total_p2p | float 0-1 |
| partial_score | f2p × p2p | float 0-1 |
| resolved | binary: f2p==1.0 && p2p==1.0 | bool |

## 11. COMPARATIVE (when baseline exists)

| metric | formula | unit |
|---|---|---|
| resolve_delta | resolved_gt - resolved_baseline | int (-1/0/+1) |
| steps_delta | steps_baseline - steps_gt | int |
| cost_delta | cost_baseline - cost_gt | float |
| files_to_gold_delta | files_to_gold_baseline - files_to_gold_gt | int |
| first_edit_delta | first_edit_baseline - first_edit_gt | int |

---

## Persistence rules

1. **8 decimal places** on every float. No rounding.
2. **Per-task file:** `gt_deep_metrics_<task>.json` — ALL metrics above.
3. **Per-run aggregate:** `gt_run_metrics_<run_id>.json` — means/medians across tasks.
4. **Behavioral impact detail:** `gt_behavioral_impact_<task>.json` — per-delivery pivot records.
5. **A run without its metrics is NOT done.** Cannot be cited or compared.
6. **Gold files** come from the verifier's FAIL_TO_PASS test paths (the files the patch must touch). When gold is unavailable (blind run), localization metrics are N/A, not fabricated.
7. **Cost** is ALWAYS recorded — even on "free" models, record the token counts + what the cost WOULD be at market rate.

## Implementation

- `scripts/swebench/gt_deep_metrics.py` — the per-task emitter (already writes most of section 9/10)
- `scripts/swebench/gt_behavioral_impact.py` — section 7 (already built)
- Sections 1-6, 8, 11 — TO BE BUILT into gt_deep_metrics.py or a companion `gt_performance_metrics.py`
- The Collect step in `deepswe_full.yml` calls the emitter; a run that skips Collect has no metrics → not done.
