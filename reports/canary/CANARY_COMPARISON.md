# CANARY_COMPARISON — 3-arm paired metric table

Status: regression-detection canary. **Not a success claim.**

## Arms
- BASELINE     : `.tmp_run_20_baseline` (20 tasks)
- OLD_GT       : `.tmp_diag_artifacts` (5 tasks)
- V2_ROUTER_GT : `pending VM run` (0 tasks)
- shared tasks across all populated arms: 5

> **V2_ROUTER_GT is missing.** The wrapper carries `GT_ROUTER_V2=1` but no run has been executed yet. See `docs/handoff/canary_v2_runbook.md` for the launch command. This file records BASELINE vs OLD_GT for regression detection in the meantime.

## Per-task table

### beancount__beancount-931

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 33 | 31 | — |
| first_gold_edit_step | 26 | 29 | — |
| files_viewed_before_gold | 0 | 0 | — |
| action_count | 37 | 44 | — |
| edit_file_precision | 1.00 | 1.00 | — |
| bridge_event_before_gold | 0 | 0 | — |
| agent_followed_gt_edge | 0 | 0 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| injections_per_task | 0 | 2 | — |
| resolved | N | N | — |
| action_economy (GT/BL) | 1.00 | 1.19 | — |

### beetbox__beets-5495

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 26 | 5 | — |
| first_gold_edit_step | 25 | 20 | — |
| files_viewed_before_gold | 0 | 1 | — |
| action_count | 50 | 20 | — |
| edit_file_precision | 1.00 | 1.00 | — |
| bridge_event_before_gold | 0 | 1 | — |
| agent_followed_gt_edge | 0 | 0 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| injections_per_task | 0 | 2 | — |
| resolved | N | N | — |
| action_economy (GT/BL) | 1.00 | 0.40 | — |

### delgan__loguru-1297

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 10 | 21 | — |
| first_gold_edit_step | 11 | 13 | — |
| files_viewed_before_gold | 0 | 0 | — |
| action_count | 24 | 59 | — |
| edit_file_precision | 1.00 | 1.00 | — |
| bridge_event_before_gold | 0 | 1 | — |
| agent_followed_gt_edge | 0 | 0 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| injections_per_task | 0 | 2 | — |
| resolved | N | N | — |
| action_economy (GT/BL) | 1.00 | 2.46 | — |

### delgan__loguru-1306

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 5 | 16 | — |
| first_gold_edit_step | 15 | 15 | — |
| files_viewed_before_gold | 0 | 0 | — |
| action_count | 40 | 48 | — |
| edit_file_precision | 1.00 | 1.00 | — |
| bridge_event_before_gold | 0 | 0 | — |
| agent_followed_gt_edge | 0 | 0 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| injections_per_task | 0 | 2 | — |
| resolved | N | N | — |
| action_economy (GT/BL) | 1.00 | 1.20 | — |

*Note: gold files differ across arms — baseline `['loguru/_colorama.py']` vs OLD_GT `['.openhands/TASKS.md', 'loguru/_colorama.py']`*

### kozea__weasyprint-2300

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | — | — | — |
| first_gold_edit_step | — | — | — |
| files_viewed_before_gold | 8 | 1 | — |
| action_count | 100 | 83 | — |
| edit_file_precision | 0.00 | 0.00 | — |
| bridge_event_before_gold | 0 | 0 | — |
| agent_followed_gt_edge | 0 | 0 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| injections_per_task | 0 | 2 | — |
| resolved | N | N | — |
| action_economy (GT/BL) | 1.00 | 0.83 | — |

*Note: gold files differ across arms — baseline `['.openhands/TASKS.md', 'weasyprint/layout/flex.py.bak']` vs OLD_GT `['weasyprint/layout/flex.py.bak']`*

## Aggregate medians (over shared tasks)

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 18.00 | 18.50 | — |
| files_viewed_before_gold | 0 | 0 | — |
| action_count | 40 | 48 | — |
| edit_file_precision | 1.00 | 1.00 | — |
| injections_per_task | 0 | 2 | — |
| stale_guidance_count | 0 | 0 | — |
| late_guidance_count | 0 | 0 | — |
| resolved (of 5) | 0 | 0 | — |

## Decision rule (per session directive)

- If V2 is worse than OLD_GT on action-path metrics: do NOT continue V2 activation.
- If V2 matches OLD_GT with fewer stale/late/injection events: continue to 5-task paired holdout.
- If V2 beats OLD_GT and BASELINE on action-path metrics: this is still a canary pass, not a success.

## Notes

- All metrics are descriptive. The canary is a regression-detection gate, not an evaluation.
- Resolve is a lagging outcome; do not use it as a single signal.
- `bridge_event_before_gold`, `agent_followed_gt_edge`, `stale_guidance_count`, `late_guidance_count`, `injections_per_task` are derived from `[GT]` markers in the trajectory observation messages — this matches the live wrapper's evidence-injection format.
- BASELINE traces should have these GT-derived metrics at 0; non-zero indicates a leak.
