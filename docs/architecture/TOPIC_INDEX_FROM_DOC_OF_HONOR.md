# Topic Index — Derived from DOC_OF_HONOR.md

Source: DOC_OF_HONOR.md sections. No invented topics.

## T01: L0 Index Pipeline (§0.1–0.5)

- **DOC section:** 0.1 Go Binary, 0.2 Schema, 0.3 Indexing Pipeline, 0.4 Properties, 0.5 Resolution
- **Intended behavior:** gt-index parses source → graph.db with nodes, edges, properties, assertions. 3-stage resolution with confidence scoring. Edge deduplication.
- **Expected trigger:** GHA pre-index step before agent starts
- **Expected agent-visible output:** None directly — graph.db is consumed by downstream layers
- **Expected code path:** gt-index/cmd/gt-index/main.go → parser.go → resolver.go → sqlite.go
- **Proof type:** code/DB proof, unit/invariant test

## T02: L0 Assertion Resolution (§0.6)

- **DOC section:** 0.6 Assertion Resolution (Multi-Signal Scoring)
- **Intended behavior:** TCTracer-inspired multi-signal scoring links test assertions to functions. 5 signals, threshold 3.5.
- **Expected trigger:** Pass 4 of indexing pipeline
- **Expected agent-visible output:** Indirect — feeds L3 [TEST] evidence
- **Expected code path:** main.go:375-400 resolveAssertionTarget()
- **Proof type:** code/DB proof (resolution rate on real repos)

## T03: L0 Pre-Index Orchestration (§0.11)

- **DOC section:** 0.11 Pre-Index Orchestration
- **Intended behavior:** GHA extracts /testbed, runs gt-index, sets GT_PREBUILT_GRAPH_DB env var
- **Expected trigger:** GHA workflow before agent step
- **Expected agent-visible output:** None — enables all downstream layers
- **Expected code path:** canary_3arm.yml lines 174-197, oh_gt_full_wrapper.py:414,422-424
- **Proof type:** trajectory-visible (graph.db present at agent startup)

## T04: Path Resolution (§1.1)

- **DOC section:** 1.1 resolve_to_stored_path()
- **Intended behavior:** Universal path resolver. All queries use LIKE suffix with _escape_like().
- **Expected trigger:** Every graph.db query
- **Expected agent-visible output:** Correct file matching in all evidence
- **Expected code path:** post_edit.py:199, post_view.py:539, graph_map.py:103, oh_gt_full_wrapper.py:3360
- **Proof type:** unit/invariant test

## T05: L1 Brief / Orientation (§2.1)

- **DOC section:** 2.1 L1 Brief — Task Start
- **Intended behavior:** Ranked file list with callers, callees, contracts, tests from graph.db
- **Expected trigger:** Task initialization (wrapper startup)
- **Expected agent-visible output:** `<gt-task-brief>` with numbered file list, caller lines, calls, contracts
- **Expected code path:** src/groundtruth/brief/graph_map.py
- **Proof type:** trajectory-visible, invariant test

## T06: L1+ Edit Target / Edit Plan / Key Contracts (§2.1+)

- **DOC section:** 2.1+ L1 Enhancement — Edit Plan + Key Contracts
- **Intended behavior:** Append [GT EDIT PLAN] and [GT KEY CONTRACTS] to L1 brief. Edit target from issue-keyword matching, not caller count alone.
- **Expected trigger:** Pre-built graph.db exists at task start
- **Expected agent-visible output:** `<gt-edit-target>` with Key function, signature, caller count. `[GT KEY CONTRACTS]` with guard clauses, conditional returns, side effects.
- **Expected code path:** oh_gt_full_wrapper.py:5410-5456 (edit plan), 5548-5625 (edit target keyword matching)
- **Proof type:** trajectory-visible, invariant test, benchmark proof
- **RISK:** HIGH — pypsa got wrong file/function, flexget got wrong function, sh got wrong function

## T07: L3 Post-Edit Evidence (§2.2)

- **DOC section:** 2.2 L3 Post-Edit — Agent Edits a File
- **Intended behavior:** Priority-ordered evidence within 2000 char budget: behavioral contract → callers → callees → signature → override → tests → siblings → twins → obligations
- **Expected trigger:** Agent runs file_editor edit operation
- **Expected agent-visible output:** `<gt-evidence trigger="post_edit:file">` with [BEHAVIORAL CONTRACT], [CALLERS], [SIGNATURE], [TEST], [PATTERN], [COMPLETENESS], [SIMILAR], [OVERRIDE]
- **Expected code path:** src/groundtruth/hooks/post_edit.py generate_improved_evidence()
- **Proof type:** trajectory-visible, unit/invariant test

## T08: L3 Behavioral Contracts (§2.2 priority 0.5)

- **DOC section:** 2.2 priority 0.5 — Behavioral contract (properties-first, regex fallback)
- **Intended behavior:** PRESERVE: guard_clause, conditional_return, side_effect from properties table
- **Expected trigger:** Post-edit hook fires
- **Expected agent-visible output:** `[BEHAVIORAL CONTRACT] PRESERVE: if X then Y; PARAMS: ...; MUTATES: ...`
- **Expected code path:** post_edit.py:1636-1811
- **Proof type:** trajectory-visible, invariant test

## T09: L3 Caller/Callee Evidence (§2.2 priority 1, 1.5)

- **DOC section:** 2.2 priority 1 (callers), 1.5 (callees)
- **Intended behavior:** Cross-file callers with 3-line context and usage classification. Callees with confidence >= 0.6.
- **Expected trigger:** Post-edit hook fires
- **Expected agent-visible output:** `Called by: file.py:N pre >> call [usage_tag]`; `Calls into: file.py::func (Nx)`
- **Expected code path:** post_edit.py:724-731 (_format_caller_line), post_edit.py:1884-1916
- **Proof type:** trajectory-visible

## T10: L3 Test Evidence (§2.2 priority 3)

- **DOC section:** 2.2 priority 3 — Test assertions
- **Intended behavior:** Ranked by issue-keyword overlap. 100-char expressions, assertRaises formatting, file basename.
- **Expected trigger:** Post-edit hook fires, depends on assertion linking (T02)
- **Expected agent-visible output:** `[TEST] assertEqual(result, None) (test_users.py)`
- **Expected code path:** post_edit.py:2252-2283 (rendering), 1311-1344 (ranking)
- **Proof type:** trajectory-visible
- **RISK:** MEDIUM — flexget didn't get [TEST] despite test file existing

## T11: L3 Completeness / Obligation Check (§2.2 priority 6, §4.2 L4b-4)

- **DOC section:** 2.2 priority 6 (obligations), 4.2 L4b-4 (obligation check)
- **Intended behavior:** AST-based shared-state detection. [COMPLETENESS] fires when edited method shares self.attrs with sibling methods.
- **Expected trigger:** Post-edit of Python file
- **Expected agent-visible output:** `[COMPLETENESS] Class.method shares attr with Class.other`
- **Expected code path:** obligation_check.py, wired in wrapper post-edit
- **Proof type:** trajectory-visible, invariant test
- **NOTE:** PRIOR-004 fixed — class-wide noise suppressed when edited function unknown

## T12: L3 Pattern/Sibling/Peer Evidence (§2.2 priority 4, 2b)

- **DOC section:** 2.2 priority 4 (sibling), 2b (peers)
- **Intended behavior:** Sibling methods in same class with len>=2 gate. Interface peers.
- **Expected trigger:** Post-edit hook fires
- **Expected agent-visible output:** `[PATTERN] sibling func() does: ...`; `[PEER] file.py::func(): signature`
- **Expected code path:** post_edit.py:2414 (sibling gate), post_edit.py:1914-1942 (peers)
- **Proof type:** trajectory-visible

## T13: L3b Post-View Navigation (§2.3)

- **DOC section:** 2.3 L3b Post-View — Agent Reads a File
- **Intended behavior:** Callers (conf>=0.7), callees (conf>=0.7), importers (conf>=0.5). Hub-penalized ranking, visited-file suppression, issue-aware re-ranking.
- **Expected trigger:** Agent runs file_editor view operation
- **Expected agent-visible output:** `Called by: file.py:N call_snippet [tag]`; `Calls into: file.py::func (Nx) [tag]`
- **Expected code path:** src/groundtruth/hooks/post_view.py graph_navigation()
- **Proof type:** trajectory-visible
- **RISK:** MEDIUM — duplication (same callers 5x on weasyprint file re-reads)

## T14: L4a Auto-Query (§2.4)

- **DOC section:** 2.4 L4a Auto-Query — First File Read
- **Intended behavior:** Key symbols with callers on first 2 source file reads. Issue-keyword boosted (L4b-3).
- **Expected trigger:** First read of non-test, non-scaffold source file
- **Expected agent-visible output:** `[GT_AUTO] Key symbols in file.py: func() called by: ...`
- **Expected code path:** oh_gt_full_wrapper.py:3334-3417
- **Proof type:** trajectory-visible

## T15: Consensus / Scope (§3.1)

- **DOC section:** 3.1 Scope-Aware Consensus
- **Intended behavior:** Layer A fires once on first brief-candidate view. Layer B progressive on subsequent candidate views.
- **Expected trigger:** Agent views a file matching brief candidate, before source edits
- **Expected agent-visible output:** `<gt-scope files="N"> 1. file.py — primary target ...`
- **Expected code path:** oh_gt_full_wrapper.py:3419-3488
- **Proof type:** trajectory-visible

## T16: L5 Scaffold Governor (§2.5)

- **DOC section:** 2.5 L5 Scaffold Governor
- **Intended behavior:** Advisory when agent creates scaffold files without prior source edits. Brief candidates + caller counts.
- **Expected trigger:** Agent creates/edits scaffold file (test_, reproduce_, debug_, etc.) without source edits
- **Expected agent-visible output:** `<gt-advisory layer="L5" trigger="non_source_without_progress">` with redirect to source files
- **Expected code path:** oh_gt_full_wrapper.py:613-714
- **Proof type:** trajectory-visible

## T17: L5b Ignored Witness Reminder (§2.6)

- **DOC section:** 2.6 L5b Late Reminder — Ignored Structural Witness
- **Intended behavior:** Reminder when agent ignores GT-suggested next_action for 3 consecutive actions. SUPPRESSED by default (goku_active=1).
- **Expected trigger:** Agent ignores GT suggestion 3x
- **Expected agent-visible output:** When goku_active=0: `[GT L5: Ignored Structural Witness] ...`. When goku_active=1: nothing (telemetry only).
- **Expected code path:** oh_gt_full_wrapper.py:1744-1819
- **Proof type:** trajectory-visible
- **RISK:** HIGH — 9x noise on weasyprint and cfn-lint when goku_active=0

## T18: L6 Reindex (§2.7)

- **DOC section:** 2.7 L6 Incremental Reindex — After Every Edit
- **Intended behavior:** gt-index -file runs BEFORE L3 post-edit hook. Graph.db updated with edited file.
- **Expected trigger:** Agent edits a file
- **Expected agent-visible output:** None directly — enables L3 to query fresh graph data
- **Expected code path:** oh_gt_full_wrapper.py:798-806
- **Proof type:** code/DB proof

## T19: L6 Pre-Submit Review (§2.8)

- **DOC section:** 2.8 L6 Pre-Submit Review — Agent Finishes
- **Intended behavior:** Review changed files for PRESERVE targets and test suggestions.
- **Expected trigger:** AgentFinishAction
- **Expected agent-visible output:** NOTHING — runs in finish handler after state=FINISHED. Telemetry only.
- **Expected code path:** oh_gt_full_wrapper.py:4520-4649
- **Proof type:** not benchmark-evaluable (agent never sees it)
- **Status:** BROKEN (OH architectural limitation)

## T20: Grep Intercept (§2.9)

- **DOC section:** 2.9 Grep Intercept — Agent Searches
- **Intended behavior:** Augment grep results with graph-based callers. Rate-limited to 5 per task.
- **Expected trigger:** Agent runs grep or rg
- **Expected agent-visible output:** `[GT] Callers of 'symbol': file.py:N call_snippet`
- **Expected code path:** oh_gt_full_wrapper.py:3185-3277
- **Proof type:** trajectory-visible

## T21: MCP Tools (§4.1)

- **DOC section:** 4.1 Registered Tools
- **Intended behavior:** 7 active tools via FastMCP stdio. 22 deprecated.
- **Expected trigger:** Agent calls tool by name
- **Expected agent-visible output:** Tool response
- **Expected code path:** src/groundtruth/mcp/server.py
- **Proof type:** not benchmark-evaluable (0% autonomous adoption)

## T22: Stuck Detector Compatibility (§4.3)

- **DOC section:** 4.3 Stuck Detector Compatibility
- **Intended behavior:** Fingerprint raw observation before GT modification. Skip GT injection when same (action, obs) pair repeats 4+ times in last 8 entries.
- **Expected trigger:** Repeated identical agent actions
- **Expected agent-visible output:** GT suppressed — agent sees raw observation only
- **Expected code path:** oh_gt_full_wrapper.py:3010-3035
- **Proof type:** trajectory-visible (stuck_compat_skip_count in metrics)

## T23: Dedup / Noise Control (§5.1)

- **DOC section:** 5.1 Dedup
- **Intended behavior:** MD5 hash of stripped body, keyed per-file per-layer. Evolution cap at >5. L5 one-shot per file. Grep intercept max 5.
- **Expected trigger:** Every evidence delivery attempt
- **Expected agent-visible output:** Deduplicated evidence (no exact repeats)
- **Expected code path:** oh_gt_full_wrapper.py:4249-4278 (L3), 3595-3620 (L3b)
- **Proof type:** trajectory-visible
- **RISK:** MEDIUM — post-view duplication on file re-reads (weasyprint same callers 5x)

## T24: Evidence Budget (§5.2)

- **DOC section:** 5.2 Evidence Budget
- **Intended behavior:** L3 post_edit: 2000 chars. L1 brief: 2000 chars. L3b: no cap (dedup only).
- **Expected trigger:** Evidence generation
- **Expected agent-visible output:** Truncated evidence within budget
- **Expected code path:** post_edit.py:73, graph_map.py:38
- **Proof type:** unit/invariant test

## T25: Hidden Prefixes / Observability (§5.3)

- **DOC section:** 5.3 Observability — Logging Prefixes
- **Intended behavior:** [GT_META], [GT_STATUS], [GT_TRACE], etc. filtered from agent observations.
- **Expected trigger:** Every GT output
- **Expected agent-visible output:** NONE — hidden prefixes must not reach agent
- **Expected code path:** oh_gt_full_wrapper.py:61-67 _is_hidden_line()
- **Proof type:** trajectory-visible (verify 0 leaks)

## T26: Delivery Ledger (§5.4)

- **DOC section:** 5.4 Delivery Ledger — _deliver_or_trace()
- **Intended behavior:** Every delivery passes through. Empty payload → ROUTER_EMIT_HOOK_EMPTY. Missing markers → MARKER_MISMATCH. Valid → DELIVERED agent_visible=true.
- **Expected trigger:** Every evidence delivery
- **Expected agent-visible output:** Evidence delivered or suppression logged
- **Expected code path:** oh_gt_full_wrapper.py:1230-1276
- **Proof type:** code/DB proof

## T27: Condenser (§5.5)

- **DOC section:** 5.5 Condenser
- **Intended behavior:** DISABLED. NoOpCondenserConfig.
- **Expected trigger:** N/A
- **Expected agent-visible output:** N/A — disabled by design
- **Expected code path:** canary_3arm.yml EVAL_CONDENSER=""
- **Proof type:** code proof (verify disabled)

## T28: Confidence Thresholds (§8)

- **DOC section:** Layer 8: Confidence Thresholds (Cross-Cutting)
- **Intended behavior:** L3 CALLS >= 0.6, L3b CALLS >= 0.7, fallback 0.5 for EXTENDS/IMPORTS/auto-query. COALESCE default 0.5 everywhere.
- **Expected trigger:** Every graph.db edge query
- **Expected agent-visible output:** Filtered edges (no low-confidence noise)
- **Expected code path:** 12 query sites listed in §8
- **Proof type:** unit/invariant test

---

## Topic Severity Ranking (from cursor-rerun trajectory audit)

| Rank | Topic | Risk | Evidence |
|------|-------|------|----------|
| 1 | T06: L1+ Edit Target | HIGH | pypsa wrong file+function, flexget wrong function, sh wrong function (3/6 wrong) |
| 2 | T05: L1 Brief | HIGH | pypsa brief missed actual target file entirely |
| 3 | T17: L5b Ignored Witness | HIGH | 9x noise on weasyprint+cfn-lint |
| 4 | T23: Dedup | MEDIUM | weasyprint same callers 5x on re-reads |
| 5 | T10: L3 Test Evidence | MEDIUM | flexget missing [TEST] despite test file existing |
| 6 | T13: L3b Post-View | MEDIUM | duplication, confidence threshold effects |
| 7 | T19: L6 Pre-Submit | LOW | BROKEN by design (OH limitation), telemetry only |
| 8 | T21: MCP Tools | LOW | 0% adoption, passive hooks dominate |
| 9–28 | Others | LOW | Working or not benchmark-evaluable |
