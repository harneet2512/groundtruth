# Easy Wins Matrix — High-Leverage Missing Capabilities

**Date:** 2026-05-15
**Reference:** 01_layer_audit.md (failure evidence), 02_research_ledger.md (research basis), 03_decision32_summary.md (design rules)

---

## Reading Guide

- **Effort:** XS (<20 LoC), S (20-80 LoC), M (80-200 LoC), L (200+ LoC)
- **Expected Impact:** HIGH (directly unblocks reaction chain or eliminates dominant failure mode), MEDIUM (measurable improvement in token efficiency or detection coverage), LOW (correctness fix, marginal behavioral change)
- **Regression Risk:** probability that the change breaks existing passing behavior
- **Verdict:** IMPLEMENT NOW / DEFER / REJECT

---

## L1 — Pre-Task Localization Brief

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L1-1 | **Primary structural witness per candidate file** — For each candidate, render the single most-referenced function's top caller with the actual call-site code line, not just a file path | Audit §L1 item 3: brief shows `Calls: src/.../SubResolve.py` (file-level pointer). No caller code line, no contract. Agent gets navigation aid, not behavioral spec. | RepoGraph [A1]: "primary navigation signal is callers/callees." Callers with code lines are k-hop ego-subgraph fragments, the format RepoGraph validates. | S | HIGH | LOW — additive to existing brief; truncation logic already handles overflow via dropping last candidate | IMPLEMENT NOW |
| L1-2 | **Graph coverage warning** — When graph.db has <2 edges/file for top candidates, emit `[SPARSE GRAPH]` tag so agent knows structural evidence is limited | Audit §L1 item 8: density gate (lines 337-346) switches to BM25-only weights but does not inform the agent. Agent receives confident-looking brief from lexical-only retrieval with no quality signal. | SWE-Pruner [A3]: task-aware pruning requires the agent to know evidence quality. Hiding sparsity is anti-transparent. | XS | MEDIUM | NONE — pure additive tag, no change to ranking or content | IMPLEMENT NOW |
| L1-3 | **Reconcile dual token caps** — Wrapper `_brief_max_tokens()` = 500, v1r_brief.py `MAX_BRIEF_TOKENS` = 400. Two caps, one unreachable. | Audit §L1 summary gap: "double cap" noted as LOW severity. Wrapper cap never fires because v1r_brief.py cap is tighter. Dead code path. | N/A (code hygiene) | XS | LOW | NONE — removing dead cap | IMPLEMENT NOW |

---

## L3 — Post-Edit Contract Evidence

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L3-1 | **Structural next_action hierarchy (callers first)** — Always populate `next_action_type` using the Decision 32 priority: READ_CALLER_CONTRACT > READ_CONSUMER > CHECK_SIGNATURE > RUN_STATIC_SANITY > RUN_TARGETED_TEST > NONE | Audit §L3 item 10: "0 out of 1 L3 fires had next_action populated." Reaction joiner produces 0 reactions for L3. The `next_action_type` is only set when `l3_targeted_verification` structured item is found — which requires test edges. | Agentless [A5]: no-test validation hierarchy proven at 32% SWE-bench Lite. FeedbackEval [A9]: structured feedback yields 63.6% repair success. | M | HIGH | LOW — `next_action_type` is a new field population, does not change rendered evidence text. Reaction joiner already handles the field when present. | IMPLEMENT NOW |
| L3-2 | **No-test validation path** — When `_get_test_assertions_from_graph()` returns empty, still produce a structural next_action (caller contract or signature check) instead of falling through to no suggestion | Audit §L3 item 7: test assertions require v16+ schema `assertions` table. On repos without assertion extraction, the test family returns empty and L3 has no fallback next_action. Audit §L5b item 11: "When `test_file_suggestions` is empty, the agent gets direction without destination." | Agentless [A5]: "patch validation via syntax + regression checks without test execution." The caller contract IS the validation when tests are absent. | S | HIGH | LOW — adds a fallback path that only fires when the test path is empty | IMPLEMENT NOW |
| L3-3 | **Contrastive evidence header for zero-overlap callers** — The `_annotate_evidence_header()` (lines 80-139) exists but its activation condition is unclear from the audit. Verify it fires and renders correctly. | Audit §L3 item 2: function listed but behavior not observed in smoke-1. If callers have 0 keyword overlap with the issue, the header should flag "these callers are structurally connected but not issue-keyword related." | SWE-Pruner [A3]: goal-conditioned selection. Callers that have zero issue relevance are still structurally important but the agent needs to know the distinction. | XS | MEDIUM | NONE — verification of existing code, no change if working | IMPLEMENT NOW |

---

## L3b — Post-View Navigation Context

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L3b-1 | **Primary edge selection** — For each edge type (callers, callees, importers), render only the single highest task-relevance edge. All others go to structured-only telemetry. | Audit §L3b item 6: 1,810 avg chars/fire across 8 fires. `graph_navigation()` renders up to 5 edges per type with no task-relevance gate on rendering. Total L3b injection ~3,600 tokens/task. | SWE-Pruner [A3]: 23-54% token reduction improves success rate. Less-but-relevant > more-but-noisy. | M | HIGH | MEDIUM — agents that currently navigate via L3b dumps may lose edges they were using. Mitigated: structured telemetry preserves all edges for post-hoc analysis. | IMPLEMENT NOW |
| L3b-2 | **Hub suppression** — Edges from/to hub files (in-degree > p90 of graph) demoted to structured-only unless they are the only edge of their type | Audit §L3b item 11: "If all top callers/callees are hub files, the navigation context is noise. Hub penalty mitigates but does not eliminate." Current hub penalty is a scoring weight, not a render suppression. | RepoGraph [A1]: ego-subgraphs around task-relevant symbols, not hubs. CodexGraph [A2]: structured query routing avoids hub saturation. | S | MEDIUM | LOW — hub files are by definition high-connectivity; suppressing one edge when alternatives exist is safe | IMPLEMENT NOW |
| L3b-3 | **Hard rendered token cap with iteration decay** — Early <=250, Mid <=160, Late <=80, Final silent (0 tokens) | Audit §L3b item 6: "No hard token cap in `graph_navigation()`." Current decay (lines 222-242) adjusts edge limits but does not enforce a character/token ceiling. | SWE-agent [A6]: concise, well-structured feedback. FeedbackEval [A9]: diminishing returns after multiple iterations. | S | HIGH | LOW — enforces what the decay mechanism intends but does not enforce | IMPLEMENT NOW |
| L3b-4 | **Populate next_action_type for navigation events** — When the primary rendered edge implies the agent should read a specific caller/callee file, set `next_action_type = "READ_CALLER_CONTRACT"` or `"READ_CONSUMER"` | Audit §L3b item 10: "L3b events do not populate `next_action_type`, so reaction joiner skips them entirely." Zero reaction observability for the second-most-frequent layer. | Anthropic Evals [B5]: "grade outcomes not paths." Without reaction data, L3b cannot be evaluated at all. | S | HIGH | LOW — populates a previously-empty field; no change to rendered output | IMPLEMENT NOW |

---

## L4 — Pre-Task Prefetch

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L4-1 | **Compact risk frame** — Replace verbose `gt_query` output with a single compact block: symbol, return type, caller count, highest-risk contract (max 80-120 tokens total) | Audit §L4 item 6: actual cap is 1200 chars (~300 tokens). `L4_TOKEN_CAP = 120` in constants.py is NOT consumed by `_run_l4_prefetch()`. The prefetch output is 2.5x the intended budget. | SWE-agent [A6]: "terse and actionable." FeedbackEval [A9]: structured > verbose. Agentless [A5]: localization phase is the dominant contributor — the prefetch is supplementary and should be proportionally small. | S | MEDIUM | LOW — reduces output size, preserves information density | IMPLEMENT NOW |
| L4-2 | **Wire L4_TOKEN_CAP to actual rendering** — constants.py declares `L4_TOKEN_CAP = 120` but `_run_l4_prefetch()` uses `L4_PREFETCH_MAX_CHARS = 1200`. One of these is dead. | Audit §L4 item 6: "This constant is NOT consumed by `_run_l4_prefetch()`." | N/A (code hygiene — dead constant) | XS | LOW | NONE | IMPLEMENT NOW |
| L4-3 | **Remove L4 tool footer dead code** — `render_l4_tool_footer()` returns empty string. The function exists but produces nothing. | Audit §L4 item 2: "L4 tool footer is disabled." If tools are not advertised to the agent, the function is dead. | N/A (code hygiene) | XS | LOW | NONE | IMPLEMENT NOW |

---

## L5 — Trajectory Governor (Detection)

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L5-1 | **`ignored_next_action` detection** — When GT emitted next_action_type != NONE and the agent's subsequent 3 actions do not match, fire L5 detection | Audit §Meta/Reaction item 10: 0 reactions produced. Without this hook, GT never knows its suggestions are ignored. Depends on L3-1 and L3b-4 populating next_action_type first. | Augment Harness [B2]: "harness improvements drive agent reliability." Knowing ignored suggestions is the first step to improving them. | M | HIGH | LOW — purely additive detection hook. Only fires when next_action is populated AND agent diverges. False-positive risk: agent may take an equivalent action with different syntax. | IMPLEMENT NOW |
| L5-2 | **`structural_unverified_patch` detection** — At iteration >= 60%, if agent has source edits but has never viewed/queried a caller of any edited function, fire detection | Audit §L5 item 10: "0 L5 hooks fired" on collapsed trajectory. Governor tracks explicit failures but not absence of structural verification. | SAGE [A4]: "extracting concise plan abstractions — distilling key steps, dependencies, and constraints." Structural verification (reading callers) is a key dependency the agent can skip. | M | HIGH | MEDIUM — must avoid false positives on tasks where callers do not exist (sparse graph). Gate on: edited file has >=1 caller edge in graph.db. | IMPLEMENT NOW |
| L5-3 | **`collapsed_diff` detection** — At iteration >= 50%, if agent's cumulative diff has <5 meaningful lines (excluding whitespace, comments, import-only changes), fire detection | Audit §L5 item 10: smoke-1 ended with `behavior_class=collapsed`. The governor had no hook for "agent is producing nothing." | Fowler Memo [B3]: harness must include "verification of functionality and behaviour." A collapsed diff is the most basic verification failure. | S | HIGH | LOW — simple line count on diff output. No interference with agent actions. | IMPLEMENT NOW |
| L5-4 | **`broad_only_verification` detection** — Agent ran broad tests (pytest, make test, tox) but never ran a targeted test for any specific edited module | Audit §L5 item 2: `_handle_command()` classifies verification commands and records pass/fail. The classification exists but there is no hook that fires when ALL verifications are broad and NONE are targeted. | Agentless [A5]: targeted validation > broad test sweeps. Broad tests can pass while the specific edited behavior is broken. | S | MEDIUM | LOW — additive hook, only fires at late iterations when the broad-vs-targeted ratio is 100%/0% | IMPLEMENT NOW |

---

## L5b — Trajectory Interventions

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L5b-1 | **Caller/consumer/signature actions instead of test-only** — When L5 fires, L5b emits a concrete action from the Decision 32 hierarchy. For `structural_unverified_patch`: "Read callers of `validate` in src/cfnlint/runner.py:145." For `ignored_next_action`: repeat the ignored suggestion with the specific file and symbol. | Audit §L5b item 11: "Next action: run one targeted test" does not specify WHICH test. When `test_file_suggestions` is empty, "direction without destination." | Agentless [A5]: structural validation hierarchy. SWE-agent [A6]: concise, specific feedback. RepoGraph [A1]: callers with code lines, not abstract advice. | M | HIGH | LOW — replaces generic text with specific text. L5bSafetyChecker still enforces 180-token cap and restart-language filter. | IMPLEMENT NOW |
| L5b-2 | **Remove deprecated hooks** — Delete `hook_patch_hypothesis` and `hook_symptom_convergence` (dead code, never called from governor) | Audit §L5b item 11: "Deprecated hooks exist in code but are never called from the governor." | N/A (code hygiene) | XS | LOW | NONE — dead code removal | IMPLEMENT NOW |

---

## L6 — Incremental Reindex

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| L6-1 | **`caller_count_after` metric** — After reindex, query the reindexed file's function nodes and record how many incoming caller edges each has. Emit as structured telemetry. | Audit §L6 item 5: "Whether the reindex actually updated the relevant edges" is not measurable. `r_ok = exit_code == 0 AND mtime_after > mtime_before` only proves the DB was touched. | Augment Observability [B4]: "attribution — which agent/model/tool-call caused what." Caller count delta after reindex is the attribution signal for L6. | S | MEDIUM | NONE — read-only query after reindex, no change to agent-visible behavior | IMPLEMENT NOW |
| L6-2 | **`edges_changed` metric** — Compare edge count for the reindexed file before and after reindex. Emit delta as structured telemetry. | Same as L6-1. Without edge delta, it is impossible to know whether L6 actually improved the graph or was a no-op. | Same as L6-1. | S | MEDIUM | NONE — same rationale | IMPLEMENT NOW |
| L6-3 | **Rebuild GraphStore usage cache after reindex** — Call `_build_usage_cache()` after L6 completes | Audit §Relationship Indexer item 11: "Usage cache stale after reindex. `_build_usage_cache()` runs once at `initialize()`." Impact: `_top_functions()` in L1 and `get_hotspots()` may return wrong ordering. | N/A (correctness fix) | XS | LOW | NONE — cache rebuild is idempotent | IMPLEMENT NOW |

---

## Hygiene — Scaffold File Strip

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| HYG-1 | **Patch collapse detection** — Before scaffold strip, check if the agent's cumulative diff is empty or near-empty. If so, log `behavior_class=collapsed` and skip strip (nothing to strip). | Audit §Hygiene item 11: "Scaffold strip on empty diff" runs but strips files that are no longer relevant. Logging the collapse before strip provides an earlier signal than post-run classification. | N/A (operational hygiene) | XS | MEDIUM | NONE — additive check before existing logic | IMPLEMENT NOW |
| HYG-2 | **Source edit lost detection** — After scaffold strip, verify that the remaining diff still contains the agent's source edits. If a non-scaffold deletion accidentally removed a source edit, log `source_edit_lost=True`. | Audit §Hygiene item 8: "If a real project has a file named `temp_utils.py` or `debug_helpers.py` that was part of the gold fix, it gets stripped." The mitigation (comparing against base_commit tree) exists but is not verified post-strip. | N/A (correctness safety net) | S | MEDIUM | LOW — read-only verification after strip. Does not change strip behavior, only logs. | IMPLEMENT NOW |

---

## Meta/Reaction — Interaction Log + Reaction Joiner

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| META-1 | **Structural action follow classification** — Extend `compute_follow_type()` to classify agent actions against the Decision 32 next_action hierarchy. Not just "did agent open suggested file" but "did agent perform READ_CALLER_CONTRACT / READ_CONSUMER / CHECK_SIGNATURE / RUN_STATIC_SANITY / RUN_TARGETED_TEST" | Audit §Meta/Reaction item 10: reaction schema has `followed_within_1/3/5` but follow type is binary (followed/not). The hierarchy classification enables measuring which suggestion types agents actually follow. | Anthropic Evals [B5]: "grade outcomes not paths" + "combine code-based, model-based, and human graders." The action hierarchy IS the code-based grader. | M | HIGH | LOW — extends existing `compute_follow_type()` with richer classification. Backward-compatible: old binary classification is derivable from new. | IMPLEMENT NOW |
| META-2 | **Emit NOT_MEASURABLE reaction for edge cases** — When agent finishes immediately after GT event, or agent's next action is ambiguous, emit `follow_type=NOT_MEASURABLE` instead of silently dropping the event | Audit §Meta/Reaction item 10: "0 reactions produced" includes cases where classification was ambiguous, not just where next_action_type was missing. | Anthropic Evals [B5]: "include positive AND negative cases." NOT_MEASURABLE is the negative case. Without it, missing data and ignored suggestions are conflated. | XS | MEDIUM | NONE — fills a gap in the reaction pipeline | IMPLEMENT NOW |

---

## Relationship Indexer (GraphStore + gt-index)

| # | Missing Capability | Current Failure Evidence | Research Basis | Effort | Impact | Regression Risk | Verdict |
|---|-------------------|------------------------|---------------|--------|--------|----------------|---------|
| REL-1 | **Decorator edges** — Index `@decorator` relationships as USES edges: decorated function -> decorator function | Audit §Relationship Indexer item 2: `_EDGE_TYPE_TO_REF` has CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS. No USES/DECORATES. Decorators are invisible in the graph. | CodexGraph [A2]: "containment, inheritance, and usage relationships." Decorators are a usage relationship that changes function behavior (e.g., `@login_required` gates access). | L | MEDIUM | LOW — additive edge type in graph.db. Consumers must opt-in to query USES edges. | DEFER — requires Go indexer changes in tree-sitter specs. Not blocking any immediate layer fix. |
| REL-2 | **Route/URL mapping edges** — For web frameworks (Flask/Django/Express), index route decorators as ROUTE edges: handler function -> URL pattern | Same gap as REL-1. Route mappings are critical for web repos (which are common in SWE-bench) but invisible in the graph. | CodexGraph [A2]: richer relationship taxonomy. RANGER [A8]: "comprehensive knowledge graph of entire repositories." | L | MEDIUM | LOW — additive, framework-specific extraction in Go indexer specs | DEFER — framework-specific, violates "no language-specific code" principle for the indexer. Revisit when multi-framework demand is proven. |
| REL-3 | **Config consumer edges** — Index files that read from config files (YAML, TOML, JSON) as CONFIG_READS edges | Not directly observed in audit. Gap identified from CodexGraph [A2] taxonomy. Config-driven behavior changes are a common SWE-bench pattern (e.g., cfn-lint rules configured via YAML). | CodexGraph [A2]: USES edges for config consumers | L | LOW | MEDIUM — config file parsing is ambiguous (is reading a YAML key a "call"?). False positive risk. | DEFER — high ambiguity, low confidence in edge quality |
| REL-4 | **Event/signal edges** — Index pub/sub patterns (Django signals, Node.js EventEmitter, Rust channels) as EVENT edges | Not directly observed in audit. Event-driven coupling is invisible in call graphs — publisher and subscriber never directly call each other. | CodexGraph [A2]: "usage relationships." Events are a usage pattern that creates hidden coupling. | L | LOW | MEDIUM — language/framework-specific extraction required | REJECT — too framework-specific, too ambiguous for deterministic extraction. Revisit if gt-index adds framework-aware specs. |
| REL-5 | **Inheritance chain edges** — Ensure INHERITS edges are extracted and queryable for "check all subclasses of X" | Audit §Relationship Indexer item 2: INHERITS is in `_EDGE_TYPE_TO_REF` (line 54). But the Go indexer's extraction of inheritance edges is not verified in the audit — it may be schema-only with no actual edges. | CodexGraph [A2]: inheritance as a first-class relationship. RepoGraph [A1]: k-hop ego-subgraphs naturally traverse inheritance. | S | MEDIUM | LOW — verification of existing capability, fix if missing | IMPLEMENT NOW |

---

## Priority Summary

### IMPLEMENT NOW (21 items)

| ID | Layer | Capability | Effort | Impact |
|----|-------|-----------|--------|--------|
| L3-1 | L3 | Structural next_action hierarchy | M | HIGH |
| L3b-1 | L3b | Primary edge selection | M | HIGH |
| L3b-4 | L3b | Populate next_action_type | S | HIGH |
| L5-1 | L5 | ignored_next_action detection | M | HIGH |
| L5-2 | L5 | structural_unverified_patch detection | M | HIGH |
| L5-3 | L5 | collapsed_diff detection | S | HIGH |
| L5b-1 | L5b | Caller/consumer/signature actions | M | HIGH |
| META-1 | Meta | Structural action follow classification | M | HIGH |
| L1-1 | L1 | Primary structural witness per candidate | S | HIGH |
| L3-2 | L3 | No-test validation path | S | HIGH |
| L3b-3 | L3b | Hard rendered token cap with decay | S | HIGH |
| L1-2 | L1 | Graph coverage warning | XS | MEDIUM |
| L3b-2 | L3b | Hub suppression | S | MEDIUM |
| L4-1 | L4 | Compact risk frame | S | MEDIUM |
| L5-4 | L5 | broad_only_verification detection | S | MEDIUM |
| L6-1 | L6 | caller_count_after metric | S | MEDIUM |
| L6-2 | L6 | edges_changed metric | S | MEDIUM |
| HYG-1 | Hygiene | Patch collapse detection | XS | MEDIUM |
| HYG-2 | Hygiene | Source edit lost detection | S | MEDIUM |
| META-2 | Meta | NOT_MEASURABLE reaction | XS | MEDIUM |
| REL-5 | Relationship | Inheritance chain verification | S | MEDIUM |
| L3-3 | L3 | Contrastive evidence header verify | XS | MEDIUM |
| L1-3 | L1 | Reconcile dual token caps | XS | LOW |
| L4-2 | L4 | Wire L4_TOKEN_CAP | XS | LOW |
| L4-3 | L4 | Remove L4 tool footer dead code | XS | LOW |
| L5b-2 | L5b | Remove deprecated hooks | XS | LOW |
| L6-3 | L6 | Rebuild usage cache after reindex | XS | LOW |

### DEFER (3 items)

| ID | Layer | Capability | Reason |
|----|-------|-----------|--------|
| REL-1 | Relationship | Decorator edges | Requires Go indexer tree-sitter spec changes; not blocking immediate layer fixes |
| REL-2 | Relationship | Route/URL mapping edges | Framework-specific; violates language-agnostic principle |
| REL-3 | Relationship | Config consumer edges | High ambiguity, low confidence in edge quality |

### REJECT (1 item)

| ID | Layer | Capability | Reason |
|----|-------|-----------|--------|
| REL-4 | Relationship | Event/signal edges | Too framework-specific, too ambiguous for deterministic extraction |
