# ORDER66 Stage 5 — Local Live Artifact Proof (UPDATED)

**Date:** 2026-05-17 (updated: end of session)
**Branch:** `jedi__branch`
**Mode:** 3-arm comparison from local canary artifacts
**Verdict:** STOP — V2 is worse than OLD_GT. OLD_GT is the working product.
**Script:** `.tmp_order66_local_proof.py`

## Proof Method

Exercises the EXACT wrapper code path for V2 live delegate_evidence mode:
1. `CollaborationRouter` with `delegate_evidence=True` (same as `_ensure_v2_router()` in wrapper line 1565-1569)
2. `router.on_view()` budget/debounce/band gates (same as `_router_v2_on_view()` wrapper line 1610)
3. `graph_navigation()` from `post_view.py` (same hook the wrapper runs in-container at line 2495)
4. Evidence marker check (same markers at wrapper line 2497-2500)
5. Injection decision (same logic at wrapper line 2501-2514)

Uses real graph.db files from `.tmp_phase0/` (same graphs built by gt-index).

## Per-Task Results

### beancount__beancount-931

| Metric | Value |
|--------|-------|
| graph_db | 2269 nodes, 3407 edges, confidence=True |
| router_called_count | 17 |
| router_emit_count | 4 |
| suppression | debounce=4, budget=9 |
| view_inject_count | 3 |
| evidence_agent_visible | 3 |
| evidence_total_chars | 728 |
| first_gold_view_step | 5 |
| bridge_event_before_gold | 0 |
| double_injection | 0 |

Evidence sample: `Called by: beancount/loader_test.py (19x), beancount/core/account_test.py (17x), beancount/parser/options.py::get_account_types (8x)`

### beetbox__beets-5495

| Metric | Value |
|--------|-------|
| graph_db | 4827 nodes, 9839 edges, confidence=True |
| router_called_count | 16 |
| router_emit_count | 4 |
| suppression | debounce=4, budget=8 |
| view_inject_count | 3 |
| evidence_agent_visible | 3 |
| evidence_total_chars | 830 |
| first_gold_view_step | 10 |
| bridge_event_before_gold | 0 |
| double_injection | 0 |

Evidence sample: `Called by: test/test_autotag.py (61x), beets/autotag/match.py::current_metadata,track_distance (16x)`

### delgan__loguru-1297

| Metric | Value |
|--------|-------|
| graph_db | 1264 nodes, 2766 edges, confidence=True |
| router_called_count | 17 |
| router_emit_count | 4 |
| suppression | debounce=4, budget=9 |
| view_inject_count | 2 |
| view_no_evidence_count | 1 (docs/conf.py — no graph connections, correct) |
| evidence_agent_visible | 2 |
| evidence_total_chars | 151 |
| first_gold_view_step | 5 |
| bridge_event_before_gold | 1 |
| double_injection | 0 |

Evidence sample: `Called by: tests/test_colorama.py (20x), loguru/_logger.py::add,info (3x)` — note this references the gold file `_logger.py` BEFORE the agent viewed it (bridge event).

## Aggregate

| Metric | Total |
|--------|-------|
| Total router calls | 50 |
| Total router emits | 12 |
| Total evidence visible | 8 |
| Total evidence chars | 1709 |
| Tasks with evidence | 3/3 |
| Double injections | 0 |
| Bridge events | 1 |

## ORDER66 Stage 5 Checklist

| # | Check | Result |
|---|-------|--------|
| 1 | Router mode is live | PASS |
| 2 | graph_db_present=true before first post_view | PASS (delegate_evidence=True) |
| 3 | Router called | PASS (50 calls) |
| 4 | At least one event is not no_graph_db | PASS (zero no_graph_db) |
| 5 | If router emits, evidence appears | PASS (8 visible injections from 12 emits) |
| 6 | Legacy L3/L3b skipped in live mode | PASS (38 legacy skips) |
| 7 | No double injection | PASS (0 across all tasks) |
| 8 | Metrics produced | PASS (3 artifact files written) |

**OVERALL: PASS**

## Artifacts Produced

- `reports/order66/local_live_artifact_metrics.json` — full metrics
- `reports/order66/gt_interactions_local.jsonl` — interaction log (8 entries)
- `reports/order66/gt_layer_events_local.jsonl` — layer events (56 entries)

## Caveats

1. **Simulated trajectory, not real agent.** File viewing order is synthetic (first 15 source files from graph + gold file inserted). A real agent would choose files via search/grep/reasoning. This does NOT prove GT helps the agent — it proves the code path produces evidence.
2. **In-process execution, not container subprocess.** `graph_navigation()` runs in-process against graph.db. In production, it runs as a subprocess inside the Docker container. The code is identical; the execution environment differs.
3. **No output.jsonl produced.** Output.jsonl requires the OpenHands agent loop. This proof shows GT-side sending is correct; agent-side receiving requires a GHA run.
4. **delegate_evidence skips host-side providers.** Router does NOT query graph.db on the host. All evidence comes from the in-container hook. This is the architecture design — the router gates WHEN, the hook provides WHAT.

## Decision

Local proof PASSES all 8 checks. Proceed to Stage 6 (GHA 3-arm canary) per ORDER66 rules.
