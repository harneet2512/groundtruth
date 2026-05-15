# 06 Implementation Plan -- Ordered by ROI and Regression Safety

**Date:** 2026-05-15
**Depends on:** `01_layer_audit.md` (layer gaps), `02_research_ledger.md` (research backing)
**Principle:** Every change is flag-gated, additive, and independently revertable. No change modifies an existing emission format without a feature flag defaulting to OFF.

---

## 1. Structural next_action hierarchy in L3

### Description
L3 post-edit evidence currently populates `next_action_type` only when an `l3_targeted_verification` structured item is found (audit section L3.10). This means the reaction joiner produces 0 reactions for most L3 fires. The fix introduces a deterministic priority hierarchy for `next_action_type` based on which evidence families actually fired:

1. **callers exist, no tests** --> `READ_CALLER_CONTRACT` (agent should read the caller to understand the contract it must preserve)
2. **consumers exist** --> `READ_CONSUMER` (agent should read the consuming file to verify compatibility)
3. **signature only** --> `RUN_STATIC_SANITY` (no structural witnesses; suggest a static check)
4. **nothing structural** --> `NONE` (no directive; reaction joiner skips cleanly)
5. **mapped test exists** --> `RUN_TARGETED_TEST` appended as optional bonus alongside any of the above

This directly addresses the HIGH-severity gap "Reaction joiner produces 0 reactions because no events populate next_action_type" from the audit summary table, specifically for L3.

### Exact files modified
- `scripts/swebench/oh_gt_full_wrapper.py` -- L3 emission site (lines ~2000-2006). After extracting structured items, apply the priority hierarchy to set `next_action_type` when `l3_targeted_verification` is absent.
- `src/groundtruth/hooks/post_edit.py` -- The `generate_improved_evidence()` return value (lines ~533-824) already includes family hit flags (`callers_found`, `signature_found`, etc. in the structured items). Add a `suggested_next_action` field to the structured output that encodes the hierarchy. The wrapper consumes this field.

### Expected LOC
~30

### Risk
**LOW** -- Purely additive. The `next_action_type` field already exists on GTLayerEvent and is already consumed by the reaction joiner. This change only populates a previously-empty field. No existing emission format changes. No agent-visible text changes.

### Feature flag
`GT_STRUCTURAL_NEXT_ACTION=1` (env var, default 0). When 0, behavior is identical to current: `next_action_type` remains empty unless `l3_targeted_verification` fires.

### Tests required
1. Unit: caller exists + no test edges --> `next_action_type == "READ_CALLER_CONTRACT"`
2. Unit: consumer exists --> `next_action_type == "READ_CONSUMER"`
3. Unit: signature only, no callers/consumers --> `next_action_type == "RUN_STATIC_SANITY"`
4. Unit: no structural evidence --> `next_action_type == "NONE"` (or empty)
5. Unit: mapped test exists alongside callers --> `next_action_type == "READ_CALLER_CONTRACT"` with `next_action_test` populated
6. Unit: output with structural next_action still <= 300 tokens (hard cap preserved)
7. Integration: reaction joiner produces a reaction record when `GT_STRUCTURAL_NEXT_ACTION=1` and callers exist

### Telemetry fields added
- `GTLayerEvent.next_action_type`: now populated on every L3 fire (was only on `l3_targeted_verification`)
- `GTLayerEvent.next_action_source`: new field, one of `"structural_hierarchy"`, `"l3_targeted_verification"`, `"none"` -- distinguishes the two population paths

### Smoke metric
- `next_action_populated_rate` for L3 events: must be > 0 on any task with graph edges (currently always 0)
- Reaction joiner output count for L3 events: must be > 0

### Rollback plan
Set `GT_STRUCTURAL_NEXT_ACTION=0`. The field reverts to empty. Reaction joiner reverts to 0 L3 reactions. Zero risk to agent-visible behavior.

---

## 2. Primary-edge selection + rendered pruning in L3b

### Description
L3b is the single biggest bloat vector: 1,810 avg chars/fire with no hard token cap (audit section L3b.6). The fix introduces primary-edge selection: for each relationship type (callers, callees, importers), select the single most task-relevant edge (by issue keyword overlap score from `_score_by_issue_relevance()`) and render only that edge with its top function. All other edges are counted but not rendered.

Backed by SWE-Pruner (A3): "23-54% token reduction while maintaining or improving task success" via goal-conditioned content selection. L3b currently dumps all edges up to the per-type limit with no goal conditioning.

Additionally, at finalization band (iteration_ratio >= 0.85), suppress all broad edges (edges to files not in the brief candidate set or visited set). This prevents late-stage noise when the agent should be converging.

### Exact files modified
- `src/groundtruth/hooks/post_view.py` -- `graph_navigation()` (lines 188-402). After the existing issue-aware re-ranking (lines 296-305), add primary-edge selection: take the top-1 per type, render it fully, emit a count line for the rest (e.g., "+4 more callers"). Add finalization broad-edge suppression after progress tracking (lines 391-396).
- `scripts/swebench/oh_gt_full_wrapper.py` -- L3b emission site (lines 1712-1777). No changes needed if post_view.py handles the pruning internally. But add `next_action_type` population: when a primary edge is a brief candidate, set `next_action_type = "FOLLOW_PRIMARY_EDGE"`.

### Expected LOC
~40

### Risk
**LOW** -- The pruning reduces output; it does not add new content. The primary edge is already the top-ranked edge in the existing sort order. The change is visible to the agent (shorter L3b output) but strictly less noisy.

### Feature flag
`GT_L3B_PRIMARY_EDGE=1` (env var, default 0). When 0, full edge lists render as before.

### Tests required
1. Unit: 5 caller edges --> renders 1 primary + "+4 more callers" count line
2. Unit: hub file (in-degree > p90) --> no dump (existing behavior preserved)
3. Unit: visited file suppressed from primary selection (existing behavior preserved)
4. Unit: output with primary-edge selection <= 80 tokens per fire (target: 4x reduction from 452 avg)
5. Unit: finalization band (ratio >= 0.85) --> 0 broad edges rendered
6. Integration: L3b events now populate `next_action_type` when primary edge is a brief candidate

### Telemetry fields added
- `GTLayerEvent.l3b_primary_edge_file`: file path of selected primary edge
- `GTLayerEvent.l3b_edges_suppressed`: count of edges not rendered
- `GTLayerEvent.l3b_finalization_suppressed`: count of broad edges suppressed at finalization

### Smoke metric
- L3b avg chars/fire: target <= 450 (currently 1810, 4x reduction)
- L3b total tokens across task: target <= 900 (currently ~3600)

### Rollback plan
Set `GT_L3B_PRIMARY_EDGE=0`. Full edge lists restored. Agent sees more context, not less -- safe regression direction.

---

## 3. Extend reaction_joiner to structural actions (read_file follow classification)

### Description
The reaction joiner (audit section Meta/Reaction.10) only fires when `next_action_type` is populated, and even then only classifies test-oriented follow-through (`ran_targeted_test_after_gt`, `ran_broad_test_after_gt`). Items 1 and 2 above now populate `next_action_type` with structural actions (`READ_CALLER_CONTRACT`, `READ_CONSUMER`, `FOLLOW_PRIMARY_EDGE`). The joiner must classify these:

- Agent opens the exact file named in `next_action_file` within N iterations --> `FOLLOWED_EXACT`
- Agent opens a file in the same directory/module --> `FOLLOWED_RELATED_FILE`
- Agent runs a broad test instead of reading the structural witness --> `FOLLOWED_BROAD_ONLY`
- Agent ignores and does something unrelated --> `IGNORED`
- Insufficient trajectory data to determine --> `NOT_MEASURABLE`

### Exact files modified
- `scripts/analysis/reaction_joiner.py` -- `compute_follow_type()` (lines 52+). Add cases for structural `next_action_type` values. Add `opened_suggested_file` detection for `read_file`/`open_file` actions (not just test actions).

### Expected LOC
~20

### Risk
**LOW** -- The reaction joiner is a post-run analysis script. It does not affect agent behavior or GT emissions. Changes only affect offline metrics computation.

### Feature flag
None needed. This is analysis code, not runtime code. Always active.

### Tests required
1. Unit: L3 event with `next_action_type=READ_CALLER_CONTRACT`, agent next action opens that file --> `FOLLOWED_EXACT`
2. Unit: agent opens a file in the same directory --> `FOLLOWED_RELATED_FILE`
3. Unit: agent runs `pytest` instead of reading --> `FOLLOWED_BROAD_ONLY`
4. Unit: agent edits an unrelated file --> `IGNORED`
5. Unit: trajectory has no next action (task ended) --> `NOT_MEASURABLE`
6. Invariant: every event with a non-empty `next_action_type` produces exactly one reaction record

### Telemetry fields added
- `GTAgentReactionEvent.follow_type`: extended enum with `FOLLOWED_EXACT`, `FOLLOWED_RELATED_FILE`, `FOLLOWED_BROAD_ONLY`, `IGNORED`, `NOT_MEASURABLE`
- `GTAgentReactionEvent.structural_follow`: boolean, true when agent followed a structural (non-test) next_action

### Smoke metric
- Reaction records produced per task: must be > 0 when items 1+2 are active
- `structural_follow_rate`: fraction of structural next_actions that got `FOLLOWED_EXACT` or `FOLLOWED_RELATED_FILE`

### Rollback plan
Revert `reaction_joiner.py`. Old joiner produces 0 reactions for structural actions (existing behavior). No runtime impact.

---

## 4. L5 structural detection: ignored_next_action, structural_unverified_patch

### Description
L5 governor fired 0 times in smoke-1 (audit section L5.10) because the agent collapsed without triggering any existing anti-pattern hooks. Two new detection patterns fill the gap:

**ignored_next_action:** When L3 emits a structural `next_action_type` (from item 1) and the agent's next action is unrelated (reaction = `IGNORED`), fire an L5 detection. This catches the case where the agent ignores graph evidence -- the precursor to scaffolding traps.

**structural_unverified_patch:** When the agent reaches finalization band with source edits but has never read a caller or consumer file (structural witness count = 0), fire an L5 detection. This is distinct from the existing `unverified_patch` hook (which checks test verification) -- it checks structural verification.

### Exact files modified
- `src/groundtruth/trajectory/governor.py` -- `after_interaction()` (lines 111-155). Add dispatch for `ignored_next_action` when the prior L3 event's reaction is `IGNORED`. Add `structural_unverified_patch` check in `_handle_finish()` (lines 343-351) based on `state.structural_witness_count`.
- `src/groundtruth/trajectory/state.py` -- `L5TrajectoryState` (lines 47+). Add `structural_witness_count` field, incremented when agent opens a file that was named in any L3/L3b `next_action_file`.

### Expected LOC
~50

### Risk
**MEDIUM** -- Modifies governor control flow. If detection fires too aggressively (false positives), L5b will emit unwanted interventions. Mitigated by: (a) flag-gating, (b) L5bSafetyChecker still validates all messages before emission, (c) debounce: `ignored_next_action` fires at most once per 5 iterations.

### Feature flag
`GT_L5_STRUCTURAL_UNVERIFIED=1` (env var, default 0). When 0, neither new detection pattern fires. Existing hooks unchanged.

### Tests required
1. Unit: L3 emits `READ_CALLER_CONTRACT`, agent opens unrelated file --> `ignored_next_action` fires
2. Unit: L3 emits `READ_CALLER_CONTRACT`, agent opens the caller file --> `ignored_next_action` does NOT fire
3. Unit: agent reaches finalization with 0 structural witnesses --> `structural_unverified_patch` fires
4. Unit: agent reaches finalization with >= 1 structural witness --> `structural_unverified_patch` does NOT fire
5. Unit: `ignored_next_action` debounce: fires once, suppressed for next 4 iterations
6. Unit: flag off --> neither detection fires regardless of state
7. Integration: detection event has correct `event_type` and links to originating L3 event via `parent_event_id`

### Telemetry fields added
- `GTLayerEvent.event_type`: new values `"ignored_next_action"`, `"structural_unverified_patch"`
- `L5TrajectoryState.structural_witness_count`: persisted in state
- `L5TrajectoryState.ignored_next_action_count`: persisted in state

### Smoke metric
- L5 detection count per task: must be > 0 on tasks where agent ignores structural evidence (currently always 0)
- `structural_unverified_patch` rate across tasks: informational (no floor gate yet -- establish baseline first)

### Rollback plan
Set `GT_L5_STRUCTURAL_UNVERIFIED=0`. Governor reverts to existing 5 hooks only. No risk to agent-visible behavior because L5 itself emits nothing to agent.

---

## 5. L5b structural one-action interventions

### Description
When L5 fires the new `ignored_next_action` or `structural_unverified_patch` detections (item 4), L5b must render a one-action intervention. The intervention names a specific action (read a specific file, check a specific caller) rather than generic advice. This addresses the audit finding (L5b.11): "Next action: run one targeted test does not specify WHICH test."

Intervention format for `ignored_next_action`:
```
[GT] Caller src/foo/bar.py:145 calls this function. Read it before continuing.
```

Intervention format for `structural_unverified_patch`:
```
[GT] Patch unverified: 0 callers/consumers read. Check src/foo/bar.py (3 callers).
```

Both are single-line, under 100 tokens, and name one specific file.

### Exact files modified
- `src/groundtruth/trajectory/hooks.py` -- Add `hook_ignored_next_action()` and `hook_structural_unverified_patch()`. Each takes the relevant file path and caller count from the L5 detection event and renders a single-line message.
- `src/groundtruth/trajectory/governor.py` -- `_build_decision()` (lines 157-207). Route new detection types to new hook functions.

### Expected LOC
~30

### Risk
**LOW** -- The new hooks follow the identical pattern as existing hooks (render text, pass through L5bSafetyChecker, emit or suppress). The safety checker's 180-token cap and restart-language filter apply automatically. No change to existing hooks.

### Feature flag
Inherits `GT_L5_STRUCTURAL_UNVERIFIED=1` from item 4. No separate flag needed -- if the detection does not fire, the intervention does not fire.

### Tests required
1. Unit: `hook_ignored_next_action` output is exactly 1 line, names the file, under 100 tokens
2. Unit: `hook_structural_unverified_patch` output is exactly 1 line, names the file + caller count, under 100 tokens
3. Unit: neither hook contains restart language ("start over", "restart", "begin again")
4. Unit: L5bSafetyChecker passes both messages (under 180 token cap, no blocked phrases)
5. Unit: when safety checker blocks (e.g., file path accidentally contains "restart"), the `blocked_by_safety` event is emitted

### Telemetry fields added
- `GTLayerEvent.event_type`: new values `"intervention_ignored_next_action"`, `"intervention_structural_unverified_patch"` (L5b layer)
- `GTLayerEvent.next_action_type`: populated with `"READ_CALLER_CONTRACT"` or `"READ_TOP_CALLER"` respectively
- `GTLayerEvent.next_action_file`: the specific file named in the intervention

### Smoke metric
- L5b intervention count: must be > 0 when L5 structural detections fire
- No restart language in any L5b message (hard invariant)

### Rollback plan
Same as item 4: set `GT_L5_STRUCTURAL_UNVERIFIED=0`. Detections do not fire, interventions do not fire. Agent sees nothing from these hooks.

---

## 6. Hygiene collapse detection

### Description
In smoke-1, the agent's behavior_class was `collapsed` -- the final diff was empty. The hygiene layer (audit section Hygiene.11) stripped scaffold files but did not detect or report that the agent's entire patch collapsed to nothing. Add a collapse detection event: when `_strip_scaffold_files()` runs and the resulting `git diff` is empty (0 lines changed), emit a `HYGIENE_COLLAPSE` event with the list of files that were present before stripping.

This is purely observability -- it does not change agent behavior. But it gives verify_report.py a signal to flag "agent produced no durable work."

### Exact files modified
- `scripts/swebench/oh_gt_full_wrapper.py` -- `_strip_scaffold_files()` (lines 1187-1232). After stripping, run `git diff --stat` in the container. If output is empty, emit a `GTLayerEvent` with `layer="HYGIENE"`, `event_type="collapse_detected"`, listing the pre-strip files.

### Expected LOC
~20

### Risk
**LOW** -- Additive telemetry event only. No agent-visible change. No change to stripping logic.

### Feature flag
`GT_HYGIENE_COLLAPSE=1` (env var, default 0). When 0, no collapse detection event emitted. Stripping behavior unchanged.

### Tests required
1. Unit: empty diff after strip --> `collapse_detected` event emitted with pre-strip file list
2. Unit: non-empty diff after strip --> no `collapse_detected` event
3. Unit: `collapse_detected` event has correct layer, event_type, evidence_items

### Telemetry fields added
- `GTLayerEvent.event_type`: new value `"collapse_detected"` (HYGIENE layer)
- `GTLayerEvent.evidence_items`: list of files present before stripping, with `kind="hygiene_pre_strip_file"`

### Smoke metric
- `collapse_detected` event present on collapsed tasks (must not be absent when diff is empty)

### Rollback plan
Set `GT_HYGIENE_COLLAPSE=0`. Event not emitted. verify_report.py loses the signal but no other impact.

---

## 7. L6 relationship freshness fields

### Description
L6 reindex currently reports only `r_ok`, exit_code, mtime_delta, and latency_ms (audit section L6.9). It does not report whether the reindex actually changed the graph. Add two fields to the L6 telemetry event:

- `caller_count_after`: number of callers for functions in the reindexed file after reindex (query graph.db)
- `edges_changed`: boolean, true if the edge count for the reindexed file differs before vs after reindex

These fields let verify_report.py detect "L6 ran but graph is stale" -- the audit finding (L6.5): "r_ok only proves the DB file was touched, not that the right nodes/edges were updated."

### Exact files modified
- `scripts/swebench/oh_gt_full_wrapper.py` -- L6 reindex block (lines 1837-1897). Before reindex, query edge count for the file. After reindex, query again. Emit delta in the L6 event's `evidence_items`.

### Expected LOC
~15

### Risk
**LOW** -- Two additional SQL queries per reindex (fast, single-file scope). No change to reindex logic. No agent-visible change.

### Feature flag
None needed. Telemetry-only change, always beneficial.

### Tests required
1. Unit: reindex changes edges --> `edges_changed=True`, `caller_count_after` reflects new count
2. Unit: reindex with no edge change (e.g., whitespace-only edit) --> `edges_changed=False`
3. Unit: reindex fails (exit_code != 0) --> fields are `null`/absent (not computed)

### Telemetry fields added
- `GTLayerEvent.evidence_items[].caller_count_after`: integer
- `GTLayerEvent.evidence_items[].edges_changed`: boolean
- `GTLayerEvent.evidence_items[].edge_count_before`: integer
- `GTLayerEvent.evidence_items[].edge_count_after`: integer

### Smoke metric
- `edges_changed=True` rate across L6 events: informational (establish baseline)
- All L6 events have non-null freshness fields (completeness check)

### Rollback plan
Remove the two queries. L6 events revert to existing 4 fields. No runtime impact.

---

## 8. L1 primary witness enrichment

### Description
The L1 brief currently lists files, top functions, test files, and graph neighbors (audit section L1.3). It does not show caller code lines -- the single most valuable evidence type per RepoGraph (A1) and the audit's own L3 analysis. Add a "primary witness" line per candidate file: the single highest-confidence cross-file caller with its call-site code line.

Example enrichment:
```
1. src/cfnlint/rules/functions/SubNotJoin.py (validate, get_value, match)
   Calls: src/cfnlint/rules/functions/SubResolve.py
   Tests: tests/unit/rules/functions/test_SubNotJoin.py
   Witness: src/cfnlint/runner.py:145 -> rule.validate(template)
```

The `Witness:` line is at most 80 chars. If no cross-file caller exists with confidence >= 0.5, the line is omitted (not "Witness: none").

### Exact files modified
- `src/groundtruth/pretask/v1r_brief.py` -- `generate_v1r_brief()` (lines 324-527). After `_issue_relevant_neighbors()`, query graph.db for the top-1 caller of the top function in each candidate file. Add `witness_line` to `FileEntry`. Update `render_brief()` (lines 308-321) to append the `Witness:` line.

### Expected LOC
~25

### Risk
**LOW** -- Additive line in the brief. The 400-token cap in v1r_brief.py still applies; if the witness lines push over budget, the existing truncation logic (drop last candidate) handles it. Worst case: one fewer candidate file shown to make room for witness lines on the remaining files.

### Feature flag
None needed. The witness line is strictly more informative than its absence. If no callers exist (sparse graph), no line appears -- no regression possible.

### Tests required
1. Unit: file with 3 cross-file callers --> `Witness:` line shows the highest-confidence one
2. Unit: file with 0 cross-file callers --> no `Witness:` line (not "Witness: none")
3. Unit: witness line <= 80 chars (truncated at call site if necessary)
4. Unit: brief with witness lines still <= 400 tokens (truncation by dropping candidates)
5. Integration: structured JSON (`gt_l1_structured.json`) includes `witness_file`, `witness_line`, `witness_confidence` per candidate

### Telemetry fields added
- `gt_l1_structured.json` per candidate: `witness_file`, `witness_line_number`, `witness_code`, `witness_confidence`
- `GTLayerEvent.evidence_items`: `kind="l1_witness"` items added

### Smoke metric
- `witness_present_rate`: fraction of L1 candidates with a witness line (target: > 0 on repos with graph edges)

### Rollback plan
Remove the witness query and `Witness:` line from render. Brief reverts to current format. No risk.

---

## 9. L4 risk frame (compact, 80-120 tokens)

### Description
L4 prefetch currently emits `[VERIFIED]`/`[POSSIBLE]` lines and git precedent (audit section L4.3). The output is useful but has no framing that tells the agent what risk it faces. Add a 1-line risk frame at the top of the `<gt-prefetch>` block that summarizes the blast radius:

```
<gt-prefetch layer="L4" risk="3 callers, 1 type contract, last_commit=14d_ago">
```

The risk frame is derived from data already computed by `_select_issue_seeded_symbols()` and `_run_l4_prefetch()`: caller count from graph.db, type contract count from gt_query output, and commit recency from git log. No new data sources needed.

### Exact files modified
- `scripts/swebench/oh_gt_full_wrapper.py` -- `_run_l4_prefetch()` (lines 2226-2312). After collecting gt_query results and git precedent, compute the risk summary string and prepend it to the `<gt-prefetch>` tag attributes.

### Expected LOC
~15

### Risk
**LOW** -- Additive attribute on an existing XML tag. Does not change the content inside the tag. The attribute is metadata, not a new injection block. Total addition: ~20-30 tokens within the existing 1200-char cap.

### Feature flag
None needed. The risk frame is strictly informative and within existing token budget.

### Tests required
1. Unit: 3 callers + 1 type contract + 14-day-old commit --> `risk="3 callers, 1 type contract, last_commit=14d_ago"`
2. Unit: 0 callers --> `risk="0 callers, ..."` (not omitted)
3. Unit: risk frame fits within existing 1200-char cap (no truncation triggered by the addition)

### Telemetry fields added
- `GTLayerEvent.evidence_items`: `kind="l4_risk_frame"` item with `caller_count`, `type_contract_count`, `commit_recency_days`

### Smoke metric
- L4 events with `l4_risk_frame` present: 100% of non-empty L4 prefetches

### Rollback plan
Remove the risk attribute from the `<gt-prefetch>` tag. Tag reverts to current format. No content change.

---

## 10. Relationship quick extractors (DEFER)

### Description
Several items above (1, 2, 4, 8) query graph.db for callers, consumers, and signatures. These queries work today but are scatter-shot: each layer builds its own SQL. A proper fix is adding dedicated fast-path query functions to the Go indexer (`gt-index`) that return pre-formatted relationship summaries, eliminating redundant Python-side SQL and ensuring consistency.

### Exact files modified
- `gt-index/internal/store/sqlite.go` -- New query functions: `GetCallersForFile()`, `GetConsumersForFile()`, `GetPrimaryWitness()`, `GetEdgeCountForFile()`.
- `gt-index/cmd/gt-index/main.go` -- New subcommand: `gt-index query --callers <file>`.
- `src/groundtruth/index/graph_store.py` -- Bridge methods to call the new Go query endpoint or fall back to existing SQL.

### Expected LOC
Large (200+ across Go + Python bridge + tests).

### Risk
**HIGH** -- Modifies the Go indexer, which is the foundation of graph.db. Any bug here corrupts all downstream layers. Requires CGO build environment. Not flag-gatable at the Go level.

### Feature flag
N/A for Go changes. Python bridge can flag-gate: `GT_USE_GO_QUERIES=1` to prefer Go fast path, fall back to existing Python SQL.

### Tests required
- Go unit tests for each new query function
- Python integration tests comparing Go fast path output to existing Python SQL output (must be identical)
- Benchmark: Go fast path vs Python SQL on a 10K-node graph.db

### Telemetry fields added
None (infrastructure change).

### Smoke metric
N/A until implemented.

### Rollback plan
Python bridge falls back to existing SQL (current behavior). Go binary changes are not deployed until validated.

### Status
**DEFERRED to future.** Items 1-9 above use existing Python SQL queries against graph.db, which work correctly today. The Go fast path is a performance optimization, not a correctness fix. Implement only after items 1-9 are validated in smoke.

---

## Implementation Sequence

```
Phase 1 (items 1-3): Structural next_action + reaction measurement
  - Unblocks: reaction data for L3 and L3b
  - Duration: single PR, ~90 LOC
  - Smoke gate: next_action_populated_rate > 0, reaction records > 0

Phase 2 (items 4-5): L5 structural detection + intervention
  - Depends on: Phase 1 (needs next_action populated to detect ignoring)
  - Duration: single PR, ~80 LOC
  - Smoke gate: L5 fires > 0 on tasks where agent ignores structural evidence

Phase 3 (items 6-9): Telemetry + enrichment
  - Independent of Phases 1-2 (can parallelize)
  - Duration: single PR, ~75 LOC
  - Smoke gate: collapse detected on collapsed tasks, witness lines present, L6 freshness populated

Phase 4 (item 10): Go indexer fast path
  - Depends on: Phase 1-3 validation (proves the queries are worth optimizing)
  - Status: DEFERRED
```

Total LOC for Phases 1-3: ~245 lines across 8 files. No Go changes. No schema changes. All flag-gated.
