# Task Report: [TASK_ID]

## Run Info
- Run ID:
- Arm:
- Git SHA:
- Date:

## Artifact Validity

| File | Present | Size | Notes |
|------|---------|------|-------|
| output.jsonl | | | |
| gt_layer_events*.jsonl | | | |
| graph.db | | | |
| evidence_metrics.json | | | |
| eval_result.json | | | |

## Architecture Matrix

| Layer | Expected | Generated | Visible in output.jsonl | Agent reacted | Status | Failure class |
|-------|----------|-----------|-------------------------|---------------|--------|---------------|
| L0_INDEX | yes | | n/a | n/a | | |
| L1_BRIEF | yes | | | | | |
| L1_EDIT_TARGET | yes | | | | | |
| L3_POST_EDIT | yes | | | | | |
| L3B_POST_VIEW | yes | | | | | |
| L4A_AUTO_QUERY | yes | | | | | |
| GREP_INTERCEPT | if grep | | | | | |
| L5_SCAFFOLD | if scaffold | | | | | |
| L5B_REMINDER | suppressed | | | | | |
| L6_REINDEX | if edit | | | | | |
| L6_PRESUBMIT | broken(OH) | | | | | |
| CONSENSUS | if candidate | | | | | |
| METRICS | yes | | n/a | n/a | | |

## Timeline
(action-by-action reconstruction of GT-relevant events)

## Divergence Points
(where expected architecture != actual behavior)

## Candidate Bugs

| Bug | Failure class | Known-prior match | Reproducible | Suggested test |
|-----|---------------|-------------------|--------------|----------------|

## Research-Sensitive Fixes Required
(list bugs needing research check before patch)

## Fix Priority
1. (highest)
2.
3.
