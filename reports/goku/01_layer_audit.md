# GT Layer Audit — Comprehensive

**Date:** 2026-05-15
**Scope:** All 10 GT layers as wired in `oh_gt_full_wrapper.py` + standalone modules
**Reference smoke:** smoke-1 cfn-lint-3862 (12 GTLayerEvents, 0 reactions, behavior_class=collapsed)

---

## L1 — Pre-Task Localization Brief

### 1. Current intended job
Inject a ranked list of candidate files + functions + test mappings + graph neighbors into the agent's first user turn, BEFORE the agent takes any action. Map-only: inject once, stay silent. No prose constraints, no behavioral nudges.

### 2. Source files and key functions
- **`src/groundtruth/pretask/v1r_brief.py`**
  - `generate_v1r_brief()` (lines 324-527): main entry point. Calls `run_v74()` for hybrid retrieval, then builds `FileEntry` list with `_top_functions()`, `_test_files_for()`, `_issue_relevant_neighbors()`.
  - `render_brief()` (lines 308-321): formats the `<gt-task-brief>` block.
  - `_top_functions()` (lines 40-60): queries graph.db for highest-referenced functions in a file.
  - `_test_files_for()` (lines 63-82): queries graph.db for test files that import the candidate file.
  - `_issue_relevant_neighbors()` (lines 85-132): queries graph.db for callers/callees, then ranks by issue keyword overlap.
  - `_detect_overconfident_convergence()` (lines 164-182): checks if all top candidates cluster in same module (Decision 26).
  - `_expand_via_cochange()` (lines 185-242): git log co-change expansion for cross-domain bridging.
  - `_expand_via_test_coimport()` (lines 245-305): shared test importer expansion.
  - Modulus gate (lines 420-457): suppresses brief entirely when all top candidates are hub files.
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `patched_get_instruction()` (lines 2619-2680): injects brief into agent's first message content.
  - `patched_initialize_runtime()` (lines 2320-2580): runs brief generation inside container, handles L2 telemetry, runs L4 prefetch.

### 3. What is emitted to agent (rendered text)
```
<gt-task-brief>
1. src/cfnlint/rules/functions/SubNotJoin.py (validate, get_value, match)
   Calls: src/cfnlint/rules/functions/SubResolve.py
   Tests: tests/unit/rules/functions/test_SubNotJoin.py
2. src/cfnlint/decode/node.py (...)
   ...
</gt-task-brief>
```
Token cap: `MAX_BRIEF_TOKENS = 400` (line 20). Truncation via `_brief_max_tokens()` in wrapper (line 392, 500 token max in wrapper).

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L1"`, `event_type="localization_brief"`, `evidence_items` from `/tmp/gt_l1_structured.json` (lines 2644-2658 in wrapper).
- **GTBeliefEvent** per candidate: `new_status="candidate"` (lines 2664-2672 in wrapper).
- **GTTelemetry** hit: `tel.record_brief(ok, l2_present)` (wrapper lines 2540-2544).
- Structured JSON written to `/tmp/gt_l1_structured.json` when `GT_STRUCTURED_EVENTS=1` (v1r_brief.py lines 498-525).
- Interaction log entry: `layer="L1"`, `trigger="brief"`, `type="brief_injection"` (wrapper line 2659).

### 5. What is NOT measurable
- Whether the agent actually READ the brief. The brief is prepended to the first user message; there is no mechanism to detect if the agent's attention attended to it vs the issue text.
- Whether the brief's file ranking influenced the agent's first file open. The interaction log records `agent_action_after` on the L1 entry, but correlation != causation.
- Precision of the brief against gold files is measurable only post-hoc with gold labels.

### 6. Token/bloat behavior
- Smoke-1 observed: **371 chars** for L1 brief (cited from task prompt).
- Token estimate: `len(text) // 4 + 1` (v1r_brief.py line 158). At 371 chars, ~93 tokens.
- Hard cap: 400 tokens (v1r_brief.py line 20), trimmed by dropping last candidate file until under budget (lines 485-488).
- Wrapper applies secondary 500-token cap via `_brief_max_tokens()` (wrapper line 2548).
- **Risk:** Brief can be empty (0 tokens) when modulus gate fires or no candidates pass confidence floor. Empty brief = agent gets zero localization signal.

### 7. Test dependencies
Yes. `_test_files_for()` (lines 63-82) queries edges where `source.is_test = 1`. The `Tests:` line in the rendered brief depends on test edges existing in graph.db. If graph has no test files indexed or no test-to-source edges, this line is absent. Functions and Calls lines work without test edges.

### 8. Generalization to real repos
- **Hybrid retrieval** (v7.4): uses BM25 (lexical), graph reach, anchor proximity, hub penalty. This is repo-agnostic.
- **Density gate** (v1r_brief.py lines 337-346): if edges/file < 2.0, switches to BM25-only weights. Handles sparse repos.
- **Decision 26 co-change expansion** (lines 185-242): requires git history. Works on any git repo.
- **Modulus gate** (lines 420-457): requires >= 50 indexed files. Skipped for small repos.
- **Language:** graph.db is language-agnostic (Go indexer handles 30 languages). The brief code has zero language-specific logic.
- **Weakness:** `_issue_relevant_neighbors()` reads file content and checks keyword overlap. This is English-biased (keyword extraction via regex `[A-Za-z_]\w{2,}`).

### 9. GT-side metrics available
- `brief_text` length, `token_estimate`, `files` count (V1RBriefResult dataclass).
- v74 retrieval diagnostics: `ranked_full`, `focus_set` from V74BriefResult.
- L2 fusion metadata: `signal_counts`, `fused_candidates`, `wall_ms` (wrapper lines 813-845).
- Structured: `candidate_count`, `graph_edge_count`, `test_edge_count`, `signature_count`, `abstain_reason` (v1r_brief.py lines 511-521).

### 10. Agent-side reaction metrics available
- Interaction log `agent_action_after` field — what the agent did on the turn immediately following L1 injection.
- Behavioral: did the agent open a brief candidate file within its first 3 actions? (Measurable from trajectory, requires post-run join.)
- The reaction joiner (`scripts/analysis/reaction_joiner.py`) only fires on events with `next_action_type` populated. L1 events do NOT populate `next_action_type` (it is always None/empty), so **the reaction joiner produces 0 reactions for L1**.

### 11. Fake/weak evidence patterns
- **Empty brief masquerading as injection:** When modulus gate fires or candidates are all hubs, `brief_text = ""` and `token_estimate = 0`. The wrapper (line 2553) catches this and injects nothing. Telemetry correctly records `tel.record_brief(False, ...)` — this is clean.
- **`[GT_BRIEF_FAILED]` fallback:** When brief generation crashes or produces < 100 chars, wrapper synthesizes a failure message (lines 2536-2539). This IS injected to the agent but is correctly logged as `ok=False`.
- **Site-packages path rewriting** (wrapper line 2546): can silently corrupt paths if the derived package name doesn't match the workspace layout. Rare but unaudited.
- **Low signal brief = gt_search redirect** (lines 2514-2525): when 0 fused candidates, injects "Use gt_search to locate..." — this is advisory prose, not evidence. Telemetry records `ok=True` for these, which is misleading.

---

## L3 — Post-Edit Contract Evidence

### 1. Current intended job
After the agent edits a source file, inject caller code lines, sibling patterns, signatures, and test assertions from graph.db. Priority-ordered, capped at ~300 tokens. Tells the agent what contracts it may have broken.

### 2. Source files and key functions
- **`src/groundtruth/hooks/post_edit.py`**
  - `generate_improved_evidence()` (lines 533-824): main evidence pipeline. Priority: callers -> test assertions -> signature -> siblings.
  - `_get_callers_from_graph()` (lines 197-322): cross-file callers with confidence >= 0.5, dynamic hops for thin wrappers.
  - `_get_signature_from_graph()` (lines 325-348): function signature + return type.
  - `_get_siblings_from_graph()` (lines 351-418): sibling functions with body snippets.
  - `_get_test_assertions_from_graph()` (lines 421-463): test assertions from `assertions` table.
  - `_annotate_evidence_header()` (lines 80-139): task-relevance annotation when callers have 0 keyword overlap (Phase 4 contrastive evidence).
  - `main()` (lines 1335-1733): CLI entry point, manages legacy fallback path (5 evidence families).
  - `_find_funcs_at_lines()` (lines 1233-1287): maps diff line ranges to function names (graph.db -> AST -> regex fallback).
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `patched_run_action()` post_edit branch (lines 1779-2028): L6 reindex, then L3 hook, dedup, evidence injection.
  - `make_edit_hook_command_with_artifacts()` (lines 709-734): builds CLI command.

### 3. What is emitted to agent (rendered text)
```
<gt-evidence trigger="post_edit:src/cfnlint/rules/functions/SubNotJoin.py">
CALLERS (2 unseen):
  src/cfnlint/runner.py:145  → rule.validate(template)
  src/cfnlint/decode/cfn_yaml.py:89  → sub_not_join.match(value)
SIGNATURE: def validate(self, template: dict) -> list[Match]
MUST PRESERVE: returns list[Match] (2 callers depend on this)
SIBLING: get_value uses: return template.get(key, default)
</gt-evidence>
```

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L3"`, `event_type="post_edit_contract"` (wrapper line 2007).
- `evidence_items` from `__GT_STRUCTURED__` JSON suffix when `GT_STRUCTURED_EVENTS=1`.
- `next_action_type` and `next_action_test` extracted from structured items when `l3_targeted_verification` kind found (wrapper lines 2000-2006).
- **GTTelemetry hit:** `tel.record_hook("L3", ok, empty)` (wrapper line 1970).
- Interaction log: `layer="L3"`, `trigger="post_edit:<path>"`, `type="evidence"` or `"GT_OK"` or `"dedup"`.
- When no evidence: type `"GT_OK"` with text `"[GT_OK] No concerns."` (wrapper line 1979).

### 5. What is NOT measurable
- Whether the agent read the caller code lines and adjusted its edit. The evidence is appended to the observation; agent could ignore it entirely.
- Whether "MUST PRESERVE" warnings prevented a regression. No counterfactual exists.
- Whether the evidence was CORRECT. If graph.db has stale edges (pre-reindex), callers may reference deleted/renamed functions. L6 fires before L3 specifically to mitigate this (wrapper line 1837 comment), but reindex can fail silently.

### 6. Token/bloat behavior
- Hard cap: `_MAX_EVIDENCE_CHARS = 1200` (~300 tokens) (post_edit.py line 54).
- Late-repair cap: `_LATE_REPAIR_MAX_CHARS = 600` (~150 tokens) when iteration_ratio >= 0.60 (post_edit.py line 607).
- Smoke-1 observed: **971 chars for 1 fire** (cited from task prompt).
- Decay: after 3 edits, `base_max` drops from 3 to 2 callers (line 598).
- **Dedup:** wrapper hashes evidence body (MD5 12-char prefix) and skips re-injection if hash matches previous for same file (wrapper lines 1982-1988). Deduped fires logged as `type="dedup"`.

### 7. Test dependencies
Yes. `_get_test_assertions_from_graph()` requires the `assertions` table in graph.db (v16+ schema). If the Go indexer didn't extract assertions (pre-v16 or language without assertion extractor), this family returns empty. The callers, siblings, and signature families do NOT depend on test edges.

### 8. Generalization to real repos
- Fully language-agnostic: all queries hit graph.db `nodes`/`edges` tables. No Python-specific code in the improved path.
- Legacy fallback (lines 1500-1710) imports `groundtruth.evidence.change`, `.contract`, `.pattern`, `.structural`, `.semantic` — some of these use Python AST parsing. The legacy path fires only when `generate_improved_evidence()` returns empty.
- `_find_funcs_at_lines()` has 3 paths: graph.db (agnostic) -> Python AST -> regex fallback. Works for any language with the regex path, but accuracy drops.
- **Weakness:** `_read_source_line()` reads actual files from `/testbed/`. If workspace root is wrong (e.g., `/workspace/django/`), callers show empty code lines. `_detect_workspace_root()` (lines 838-872) mitigates but can fail.

### 9. GT-side metrics available
- `evidence_source`: `"improved_l3"` vs legacy family breakdown (post_edit.py log_entry).
- `output_lines`, `wall_time_ms`, `files_changed`, `old_content_source`.
- Per-family: `ran`, `items_found`, `after_abstention` counts.
- Structured items: `l3_caller_code`, `l3_signature`, `l3_sibling_pattern`, `l3_test_assertion`, `l3_targeted_verification` kinds.

### 10. Agent-side reaction metrics available
- `next_action_type` populated when `l3_targeted_verification` item found (wrapper lines 2000-2006).
- Reaction joiner fires ONLY when `next_action_type` is populated. In smoke-1, **0 out of 1 L3 fires had next_action populated** (no `l3_targeted_verification` items extracted), so 0 reactions.
- Interaction log `agent_action_after` shows what the agent did next.
- `changed_diff_after_gt` measurable by comparing diff hashes before/after L3 injection.

### 11. Fake/weak evidence patterns
- **`[GT_OK] No concerns.`**: Emitted when `has_evidence` check fails (wrapper line 1979). This is a placeholder that restores structural observation presence. It contains zero information. Logged as `type="GT_OK"`, so correctly distinguishable from real evidence.
- **`[GT_STATUS] skipped:scaffolding_file`**: Emitted when agent edits a scaffold file (wrapper lines 1829-1834). Zero evidence content.
- **Dedup with zero content:** When dedup fires, agent sees `<gt-evidence trigger="post_edit:..." dedup="true" />` — an empty tag.
- **Late-repair truncation:** At iteration_ratio >= 0.60, evidence cap drops to 600 chars. If the first caller's code line is long, it may be the only evidence shown.

---

## L3b — Post-View Navigation Context

### 1. Current intended job
After the agent reads/views a source file, inject structural coupling context: callers, callees, importers. Shows the agent where this file connects in the graph so agent + GT collaborate on localization.

### 2. Source files and key functions
- **`src/groundtruth/hooks/post_view.py`**
  - `graph_navigation()` (lines 188-402): main function. Queries callers, callees, importers from graph.db with confidence >= 0.5 filtering, issue-aware re-ranking, hub-penalized scoring, visited-file suppression, brief candidate annotation.
  - `_score_by_issue_relevance()` (lines 113-128): re-ranks neighbors by issue keyword overlap in file content.
  - `_load_visited_files()` (lines 131-136): reads `/tmp/gt_viewed.txt` for suppression.
  - `_load_brief_candidates()` (lines 140-146): reads `/tmp/gt_brief_candidates.txt` for `[CANDIDATE]` annotation.
  - `_in_degree_for_file()` (lines 149-163): in-degree for hub penalty.
  - `_top_functions_for_file()` (lines 166-185): top functions by reference count for symbol-level hints.
  - `main()` (lines 405-471): CLI entry point. Skips test files (line 428).
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `patched_run_action()` post_view branch (lines 1712-1777): runs hook, checks evidence, dedup, injects.
  - `make_view_hook_command()` (lines 689-702): builds CLI command.

### 3. What is emitted to agent (rendered text)
```
<gt-evidence trigger="post_view:src/cfnlint/rules/functions/SubNotJoin.py">
Called by: src/cfnlint/runner.py::run,validate (3x), src/cfnlint/decode/cfn_yaml.py::load (2x)
Calls into: src/cfnlint/rules/functions/SubResolve.py::resolve,match (5x)
Imported by: tests/unit/rules/functions/test_SubNotJoin.py
</gt-evidence>
```

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L3b"`, `event_type="navigation"` (wrapper line 1766).
- `evidence_items` from `__GT_STRUCTURED__` suffix: `l3b_caller_edge`, `l3b_callee_edge`, `l3b_importer_edge`, `l3b_decay_metadata`.
- **GTTelemetry hit:** `tel.record_hook("L3b", ok, empty)` (wrapper line 1740).
- Interaction log: `layer="L3b"`, `type="evidence"` or `"GT_OK"` or `"dedup"`.

### 5. What is NOT measurable
- Whether the navigation context influenced the agent's next file open. The agent may have already decided where to go.
- Whether the `[CANDIDATE]` annotation drew attention to brief files.
- Navigation is passive (informational); there is no "next_action" directive, so the reaction joiner cannot produce follow metrics.

### 6. Token/bloat behavior
- Smoke-1 observed: **14,486 total chars across 8 fires = 1,810 avg chars/fire** (7 views, cited from task prompt). Note: 8 fires, not 7 — one view may have triggered twice or there is a counting discrepancy; 14486/8 = 1810.75.
- No hard token cap in `graph_navigation()`. The limit is controlled by `limit` parameter (default 5 edges per type).
- Iteration-aware decay when `GT_REBUILD_L3B=1` (lines 222-242): `L3B_EDGE_LIMITS` from constants.py: early=3, mid=2, late=1, final=0.
- Importers suppressed after 60% iteration ratio (line 366).
- Progress tracking and focus tags at 85%+ (lines 391-396).
- **This is the single biggest bloat vector.** At 1810 avg chars (452 tokens), L3b injects more tokens per fire than any other layer. With 8 fires in a single task, total L3b injection is ~3600 tokens.

### 7. Test dependencies
No direct dependency. The callers/callees queries do not filter on `is_test`. The importers query does not filter on `is_test`. However, test files in the graph will appear as callers/importers in the output. `_is_test_file()` filtering happens only at main() entry (line 428) — test files are SKIPPED entirely, not enriched.

### 8. Generalization to real repos
- Fully language-agnostic: all queries are pure SQL on graph.db.
- Issue-aware re-ranking (lines 296-305) reads file content for keyword overlap — same English-bias as L1.
- Hub penalty scale is computed dynamically from the graph's p90 in-degree (lines 310-313), not hardcoded. Adapts to repo size.
- Visited-file suppression (lines 291-293) prevents repeating the same navigation context.
- Works well on densely-connected graphs. On sparse graphs (< 2 edges/file), all lists are empty and hook returns `[GT_STATUS] no_evidence:no_graph_edges`.

### 9. GT-side metrics available
- `output_lines`, `wall_time_ms` (post_view.py log_entry).
- `total_callers` count returned from `graph_navigation()`.
- Structured: per-edge items with file path, call count, source.
- Decay metadata: `decay_applied`, `edge_limit_before/after`, `iteration_band`, `broad_navigation_after_60pct`.

### 10. Agent-side reaction metrics available
- **None via reaction joiner.** L3b events do not populate `next_action_type`, so `join_gt_to_agent()` skips them entirely (reaction_joiner.py line 25: `if not evt.get("next_action_type"): continue`).
- Interaction log `agent_action_after` is the only signal.
- **This is a measurement gap.** L3b is the second most frequent layer (8 fires in smoke-1) with zero reaction observability.

### 11. Fake/weak evidence patterns
- **`[GT_OK] No concerns.`**: Emitted when `has_evidence` check fails (wrapper line 1748). Same placeholder as L3.
- **Dedup tag:** `<gt-evidence trigger="post_view:..." dedup="true" />` — empty.
- **Hub-dominated output:** If all top callers/callees are hub files (e.g., `__init__.py`, `utils.py`), the navigation context is noise. Hub penalty mitigates but does not eliminate this.
- **Stale navigation:** If agent views a file after a series of edits but before L6 reindex runs, the graph edges may not reflect the current state. L6 only runs on post_edit, not post_view.

---

## L4 — Pre-Task Prefetch (gt_query Evidence)

### 1. Current intended job
At task start, after L1 brief generation, pre-fetch `gt_query` evidence for issue-relevant symbols. Injects `[VERIFIED]`/`[POSSIBLE]` contract lines and git precedent (last commit for top candidate files) into the brief.

### 2. Source files and key functions
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `_run_l4_prefetch()` (lines 2226-2312): main prefetch function. Extracts candidate files from brief, selects issue-seeded symbols, runs `gt_query` per symbol, appends git precedent.
  - `_select_issue_seeded_symbols()` (lines 2135-2223): extracts identifiers from issue text, filters against graph.db nodes in candidate files, ranks by centrality.
  - `_extract_candidate_files()` (lines 2122-2132): regex extraction of file paths from brief text.
  - `install_l4_tools()` (lines 1327-1385): uploads gt_query/gt_search/gt_navigate/gt_validate tool bundles to container.
  - `render_l4_tool_footer()` (line 809-810): **returns empty string** — L4 tool footer is disabled.

### 3. What is emitted to agent (rendered text)
```
<gt-prefetch layer="L4" queries="2" symbols="validate,match" wall_ms="1234">
# gt_query: validate
[VERIFIED] validate(template) returns list[Match] — 3 callers depend on return type
[POSSIBLE] match(value) may be called from SubResolve.resolve
# src/cfnlint/rules/functions/SubNotJoin.py: last commit: abc1234 Fix SubNotJoin validation
</gt-prefetch>
```

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L4"`, `event_type="prefetch"` (wrapper lines 2558-2569).
- `evidence_items`: `[{"kind": "l4_constraint", "text": ..., "source": "graph_db"}]`.
- **GTTelemetry:** `tel.record_l4_prefetch(queries, lines)` (line 2298).
- Also: `tel.record_l4()` fires every time the agent uses `gt_query`/`gt_search`/etc. in a CmdRunAction (wrapper lines 1626-1629). This is L4 **usage** telemetry, distinct from L4 **prefetch** telemetry.

### 5. What is NOT measurable
- Whether the agent used the prefetch evidence vs the brief. Both are in the first message.
- Whether the git precedent line influenced the agent's approach. No directional metric exists.
- Whether the agent used the L4 tools (gt_query, gt_search, gt_navigate, gt_validate) AT ALL. Smoke-1 showed **0 L4 tool usage** (from memory `project_oh_smoke_r2_layer_audit_2026_05_07.md`). Tool availability != tool usage.

### 6. Token/bloat behavior
- Per-symbol: max `L4_PREFETCH_MAX_LINES_PER_QUERY = 4` lines (line 2115).
- Total: max `L4_PREFETCH_MAX_CHARS = 1200` chars (~300 tokens) (line 2117).
- Max queries: `L4_PREFETCH_MAX_QUERIES = 3` (line 2114).
- Wall timeout: 30 seconds (line 2118).
- Git precedent: max 2 candidate files, 80 chars each (lines 2277-2287).
- Token cap: `L4_TOKEN_CAP = 120` in constants.py (line 13) — but this constant is NOT consumed by `_run_l4_prefetch()`. The actual cap is 1200 chars.
- Noise filtering: skips lines matching `L4_NOISE_PATTERNS = ("body spans", "sibling:")` (line 2119).

### 7. Test dependencies
Indirect. `gt_query` (the tool) queries graph.db which includes test nodes. Issue-seeded symbol selection (`_select_issue_seeded_symbols()`) filters to `n.label IN ('Function','Method','Class')` and `n.is_exported = 1`, which would include exported test helpers but not private test functions. The git precedent has no test dependency.

### 8. Generalization to real repos
- Language-agnostic: symbol selection queries graph.db nodes.
- `_extract_candidate_files()` uses a generic file-path regex (line 2120).
- Issue-seeded symbol extraction uses `[A-Za-z_][A-Za-z0-9_]{2,}` regex — works for any programming language identifiers.
- `gt_query` tool is a Python wrapper around graph.db queries — agnostic.
- **Weakness:** `_select_issue_seeded_symbols()` builds raw SQL with string concatenation (line 2166: `f"n.file_path LIKE '%{f.replace(chr(39), '')}'"`) — this is SQL injection vulnerable, though in a controlled environment.

### 9. GT-side metrics available
- `queries_run`, `symbols`, `total_lines`, `wall_ms` printed to stdout (line 2291).
- Structured event with `l4_constraint` items.
- `tel.record_l4_prefetch(queries, lines)` for aggregate.

### 10. Agent-side reaction metrics available
- **Zero from reaction joiner.** L4 prefetch is part of the first message (same as L1); no separate event with `next_action_type`.
- L4 tool USAGE is tracked via `tel.record_l4()` when the agent runs gt_query etc. in CmdRunAction.
- In smoke-1: **0 L4 tool usages** observed. The tools exist in PATH but the agent never invoked them.

### 11. Fake/weak evidence patterns
- **Empty prefetch:** When no symbols match or gt_query returns < 10 chars per symbol, returns `""` (line 2301). Telemetry logs `emitted=False, suppressed=True, suppression_reason="no_prefetch_results"` (lines 2565-2568). Clean.
- **Stale gt_query output:** gt_query runs against graph.db at task start. If the agent later edits files and L6 reindexes, the prefetch is stale. This is expected (prefetch is one-shot).
- **`[L4_PREFETCH_TRUNCATED]`** marker: appears when evidence exceeds 1200 chars (line 2305).

---

## L5 — Trajectory Governor (Detection)

### 1. Current intended job
Detect trajectory-level anti-patterns: premature commitment, repeated failures, no durable source progress, unsafe finish, unverified patches. L5 is the DETECTOR; L5b is the INTERVENTION that the agent sees.

### 2. Source files and key functions
- **`src/groundtruth/trajectory/governor.py`**
  - `L5Governor` class (lines 104-415): main governor. Maintains `L5TrajectoryState`, dispatches to hook functions.
  - `after_interaction()` (lines 111-155): entry point called by wrapper on every CmdRunAction, FileEditAction, FinishAction.
  - `_handle_command()` (lines 209-280): classifies verification commands, records pass/fail, triggers failure hooks.
  - `_handle_source_edit()` (lines 308-330): checks premature commitment.
  - `_handle_non_source_edit()` (lines 333-340): checks no durable source progress.
  - `_handle_finish()` (lines 343-351): checks unsafe finish.
  - `_build_decision()` (lines 157-207): builds L5Decision, applies L5bSafetyChecker.
- **`src/groundtruth/trajectory/state.py`**
  - `L5TrajectoryState` (lines 47+): persists across interactions, tracks iteration bands, failure snapshots, verification counts.
  - `compute_band()` (lines 34-44): maps iteration ratio to 4 bands (EARLY_EXPLORATION, MID_COMMITMENT, LATE_REPAIR, FINALIZATION).
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - Governor init: wrapper line 2357-2367 (in `patched_initialize_runtime()`).
  - Governor CmdRunAction dispatch: wrapper lines 1657-1702.
  - Governor source edit tracking: wrapper lines 1793-1802.
  - Governor finish dispatch: wrapper lines 2031-2065.

### 3. What is emitted to agent (rendered text)
L5 itself emits NOTHING to the agent. L5 fires a detection event. If the detection passes L5bSafetyChecker, L5b emits the intervention message (see L5b section below).

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L5"`, `event_type=<hook_name>` (wrapper lines 1670-1674).
- `verification_kind` field populated on command events.
- L5 telemetry JSONL: `/tmp/gt_l5_telemetry.jsonl` (governor.py line 411).
- **GTTelemetry:** `tel.record_gate(bool(unresolved))` at finish (wrapper line 2092).

### 5. What is NOT measurable
- Whether the agent was genuinely "stuck" or just executing a multi-step plan. The governor uses heuristics (e.g., "no source edit before verification") that can false-positive on valid workflows.
- Whether a "repeated failure" is the SAME logical failure or a new regression. `state.repeated_failure_count` tracks consecutive failures but doesn't semantically compare error messages (parsers.py does extract `failing_unit` and `assertion_or_error` for comparison).

### 6. Token/bloat behavior
L5 itself produces 0 tokens to the agent. See L5b.

### 7. Test dependencies
Indirect. `_get_test_suggestions()` (governor.py lines 353-381) queries graph.db for test files connected to edited files. This requires test edges in the graph. If absent, suggestions are empty. The governor still fires; it just can't suggest specific test files.

### 8. Generalization to real repos
- Governor logic is language-agnostic: it operates on action types (edit, command, finish), not language constructs.
- `_is_source_edit()` (governor.py lines 46-61) checks file extensions — covers 25 extensions including `.py`, `.go`, `.js`, `.ts`, `.rs`, `.java`, `.c`, `.cpp`, `.rb`, `.php`, `.swift`, `.kt`, `.scala`, `.cs`, `.yml`, `.yaml`, `.toml`, `.json`, `.cfg`. Scaffold detection by filename prefix.
- Verification command classification (`classifier.py`) likely has Python-specific patterns (pytest, unittest). Not audited here.

### 9. GT-side metrics available
- L5 log entries: `hook`, `iter`, `band`, `phase`, `fired`, `suppressed_reason`, `message_len`, `message_text`, `next_action` (governor.py lines 391-414).
- JSONL at `/tmp/gt_l5_telemetry.jsonl`.
- State: `l5_messages_emitted`, `verification_commands_run`, `edited_source_files`, `repeated_failure_count`.

### 10. Agent-side reaction metrics available
- Reaction joiner can fire on L5 events IF `next_action_type` is populated by `_build_decision()`. This IS populated when L5b fires (lines 178-185).
- In smoke-1: **0 L5 hooks fired** (governor never triggered). Therefore 0 reactions.

### 11. Fake/weak evidence patterns
- **False suppression:** L5bSafetyChecker (hooks.py lines 234-263) blocks messages containing restart language or broad exploration phrases. It also blocks messages > 180 tokens. This can suppress legitimate interventions.
- **Silent governor:** When `GT_REBUILD_L5=0` (default), the `unverified_patch` hook is disabled (governor.py line 258). Other hooks still fire.
- **Debounce:** `hook_unverified_patch` fires once per edit cycle (hooks.py lines 168-169). If the agent makes many edits without verification, only one warning fires.

---

## L5b — Trajectory Interventions (Agent-Visible Messages)

### 1. Current intended job
When L5 detects an anti-pattern, L5b is the rendered message injected into the agent's observation. Seven hook types: no_durable_source_progress, premature_commitment, hypothesis_falsified, same_failure_persisted, unverified_patch, unsafe_finish, symptom_convergence (deprecated).

### 2. Source files and key functions
- **`src/groundtruth/trajectory/hooks.py`**
  - `hook_no_durable_source_progress()` (lines 25-45): fires when agent edits only scaffold/non-source files.
  - `hook_premature_commitment()` (lines 48-68): fires when agent edits source before inspecting confirming callers/tests.
  - `hook_hypothesis_falsified()` (lines 90-112): fires after test failure following a source edit. Docstring: "THE KEY HOOK".
  - `hook_same_failure_persisted()` (lines 115-135): fires when same failure repeats after agent's edit.
  - `hook_unverified_patch()` (lines 155-185): fires when broad tests pass but no targeted test run.
  - `hook_unsafe_finish()` (lines 188-230): fires on finish with unresolved failure or unverified patch.
  - `hook_patch_hypothesis()` (lines 71-87): DEPRECATED, not called from governor.
  - `hook_symptom_convergence()` (lines 138-152): DEPRECATED, not called from governor.
  - `L5bSafetyChecker` class (lines 234-263): validates messages before emission.
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - Old L5 scaffold advisory: `_render_scaffold_advisory()` (lines 532-566), `_maybe_fire_l5()` (lines 569-592). This is the wrapper-level scaffold redirect, distinct from the governor hooks.

### 3. What is emitted to agent (rendered text)
Example (hypothesis_falsified):
```
[GT L5: Hypothesis Falsified]
Iteration: 45/100
Evidence: verification failed after editing src/cfnlint/rules/functions/SubNotJoin.py.
Failing unit: test_sub_not_join::test_valid (AssertionError: expected [] got [Match(...)])
Next action: revise the edit that produces the wrong result.
```

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L5b"`, `event_type="intervention_<hook_name>"` (wrapper lines 1676-1683).
- `parent_event_id` links to the L5 detection event.
- `next_action_type`, `next_action_file`, `next_action_test` populated.
- If safety-blocked: `layer="L5b"`, `event_type="blocked_by_safety"`, `suppressed=True` (wrapper lines 1694-1700).

### 5. What is NOT measurable
- Whether the agent actually changed behavior because of the intervention vs coincidental course correction. The `followed_within_N` fields in the reaction schema exist for this, but require the reaction joiner to fire (needs `next_action_type`).
- Whether "Next action: revise the edit" led to a BETTER edit or a worse one.

### 6. Token/bloat behavior
- Hard cap: `_MAX_L5_TOKENS = 180` (hooks.py line 9).
- L5bSafetyChecker enforces: `token_estimate = len(text) // 4; if token_estimate > 180: return False` (hooks.py line 262).
- `L5B_TOKEN_CAP = 180` in constants.py (line 14).
- Messages include `_iteration_prefix()` (adds ~25 chars when ratio >= 0.60) and `_late_repair_suffix()` (adds ~55 chars in LATE_REPAIR/FINALIZATION bands).
- Typical message: 150-250 chars (~40-65 tokens). Under cap.

### 7. Test dependencies
Indirect. `hook_unverified_patch()` uses `test_file_suggestions` from governor's `_get_test_suggestions()` which queries graph.db for test-connected files. Without test edges, suggestions are empty but the hook still fires.

### 8. Generalization to real repos
Same as L5 — all hooks are action-type based, not language-specific. The message text is generic ("revise the edit", "run one targeted test", "stop scaffolding"). No language-specific advice.

### 9. GT-side metrics available
- All L5 metrics apply (L5b is the output of L5).
- `suppression_reason` from L5bSafetyChecker: `"restart_language"`, `"late_broad_exploration"`, `"exceeds_token_cap"`.

### 10. Agent-side reaction metrics available
- Reaction joiner CAN fire on L5b events because `next_action_type` IS populated by `_build_decision()`.
- Fields: `followed_within_1/3/5`, `followed_eventually`, `ignored`, `contradicted`, `ran_targeted_test_after_gt`, `opened_suggested_file`, etc.
- In smoke-1: **0 L5b interventions** (governor never triggered), so 0 reactions. This is the correct null result — no detection means no intervention means no reaction.

### 11. Fake/weak evidence patterns
- **Deprecated hooks:** `hook_patch_hypothesis` and `hook_symptom_convergence` exist in code but are never called from the governor. Dead code.
- **Safety over-filtering:** L5bSafetyChecker blocks any message containing "start over", "restart", "begin again" even in context like "do not start over" (the check at lines 253-255 tries to handle this with a lookbehind, but only checks for "do not"/"don't"/"never" in the 10 chars before the phrase — fragile).
- **Generic advice:** "Next action: run one targeted test" does not specify WHICH test. When `test_file_suggestions` is empty (no test edges), the agent gets direction without destination.

---

## L6 — Incremental Reindex

### 1. Current intended job
After the agent edits a source file, re-run `gt-index` in single-file mode to update graph.db with the new/changed symbols and edges. This keeps L3 and L3b evidence fresh.

### 2. Source files and key functions
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `make_reindex_command()` (lines 678-686): builds `gt-index -root=... -file=<rel_path> -output=graph.db` command.
  - Reindex execution in `patched_run_action()` post_edit branch (lines 1837-1897): runs reindex, checks exit code + mtime delta, logs success/failure.
  - `install_graph_and_hook()` (lines 1423-1552): initial full index at task start.
  - Scaffold early-exit path (lines 1816-1834): also reindexes scaffold files (to keep graph current) but skips L3.

### 3. What is emitted to agent (rendered text)
Nothing. L6 is invisible to the agent. It runs silently between the agent's edit and the L3 evidence injection.

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="L6"`, `event_type="reindex"` (wrapper lines 1881-1891).
- `evidence_items`: `[{"kind": "l6_reindex", "file_path": ..., "reason": "reindex success/failed: exit=N", "text": "latency_ms=... mtime_delta=..."}]`.
- **GTTelemetry:** `tel.record_reindex(r_ok)` (wrapper line 1878).
- Interaction log: `layer="L6"`, `type="reindex_ok"` or `"reindex_fail"` or `"reindex_skip"`.

### 5. What is NOT measurable
- Whether the reindex actually updated the relevant edges. `r_ok = (exit_code == 0) AND (mtime_after > mtime_before)` (wrapper line 1870) only proves the DB file was touched, not that the right nodes/edges were updated.
- The quality of post-reindex L3 evidence vs pre-reindex. No A/B comparison exists.

### 6. Token/bloat behavior
Zero tokens to agent. Internal overhead: one subprocess call per edit, typically 0.5-5 seconds.

### 7. Test dependencies
None. `gt-index -file=<path>` indexes the given file regardless of test status.

### 8. Generalization to real repos
- `gt-index` supports 30 languages via tree-sitter specs. Single-file mode is language-agnostic.
- **Weakness:** Single-file reindex updates nodes/edges for that file but does NOT update edges FROM other files that now call NEW functions in the edited file. The new function exists in graph.db after reindex, but callers won't know about it until they are reindexed too.

### 9. GT-side metrics available
- `r_ok` (bool), exit_code, mtime_delta, latency_ms.
- Panic/fatal keyword detection in reindex output (wrapper lines 1872-1875).

### 10. Agent-side reaction metrics available
None. L6 is invisible to agent.

### 11. Fake/weak evidence patterns
- **`r_ok=True` with broken edges:** If gt-index exits 0 and touches the DB but encounters a parse error on the file (e.g., syntax error in agent's half-written edit), it may write partial/empty nodes. The mtime check passes but evidence quality degrades.
- **Binary unavailable:** If `config.gt_index_bin = ""` (upload failed), all reindexes are skipped. Telemetry records this as `reindex_skip`, not `reindex_fail`. L3 evidence uses stale graph.db for the entire task.

---

## Hygiene — Scaffold File Strip

### 1. Current intended job
At task finish (or max_iter timeout), delete untracked scaffold files created by the agent (reproduce_*, debug_*, temp_*, etc.) to prevent them from contaminating the submitted patch.

### 2. Source files and key functions
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `_strip_scaffold_files()` (lines 1187-1232): main function. Lists untracked files, filters by scaffold prefix, deletes scaffolds, preserves non-scaffold new files.
  - `_is_scaffold_name()` (lines 1181-1184): basename prefix check against `SCAFFOLDING_PREFIXES` (lines 224-233).
  - Called at finish (wrapper line 2072) and max_iter timeout (wrapper line 1651).
  - Idempotent via `config.scaffold_stripped` flag (line 1199).

### 3. What is emitted to agent (rendered text)
```
GT_ENFORCE: Stripping 3 scaffold files.
GT_ENFORCE: Kept 2 new non-scaffold files: src/new_module.py, tests/test_new.py
```
These are printed to stdout (lines 1217, 1221) — visible in container logs but NOT injected into agent observation. The agent does not see Hygiene output.

### 4. What is emitted as structured telemetry
- **GTLayerEvent** with `layer="HYGIENE"`, `event_type="scaffold_strip"` (wrapper lines 1223-1232).
- `evidence_items`: one `{"kind": "hygiene_strip", "file_path": ..., "reason": "scaffold file removed"}` per stripped file.
- `emitted=True` if files were stripped, `suppressed=True` with `suppression_reason="no_scaffold_files"` if none found.

### 5. What is NOT measurable
- Whether the scaffold files, if left in place, would have caused the patch to fail evaluation. SWE-bench evaluation applies the diff to a clean repo; untracked files in the diff would be noise but may not cause test failures.
- Whether the agent's git diff already excluded them. Scaffold files created outside the repo root or already .gitignored would not appear in the diff anyway.

### 6. Token/bloat behavior
Zero tokens to agent. No observation injection.

### 7. Test dependencies
None. Scaffold detection is purely filename-based.

### 8. Generalization to real repos
- Scaffold prefixes (`reproduce_`, `repro_`, `debug_`, `verify_fix`, `verify_implementation`, `test_fix`, `scratch_`, `temp_`) are language-agnostic and match common AI agent scaffolding patterns.
- **Weakness:** If a real project has a file named `temp_utils.py` or `debug_helpers.py` that was part of the gold fix, it gets stripped. The mitigation (lines 1210-1215) compares against `base_commit` tree to preserve files that existed before the agent started.
- Verified in docs: "19.3% of SWE-bench-Live Lite gold patches add new files" (line 1197 comment).

### 9. GT-side metrics available
- Count of stripped files, count of kept new files.
- Per-file paths in evidence_items.

### 10. Agent-side reaction metrics available
None. Agent does not see Hygiene output.

### 11. Fake/weak evidence patterns
- **Scaffold strip on empty diff:** If the agent's diff collapsed to zero (behavior_class=collapsed in smoke-1), scaffold strip still runs but strips files that are no longer in the working tree. Harmless but logged as "stripped N files" which could be misleading in telemetry.

---

## Meta/Reaction — Interaction Log + Reaction Joiner

### 1. Current intended job
Record every GT->agent interaction with full context (what GT sent, what the agent was doing, what the agent did next). Post-run, join GT events to agent trajectory to compute follow-through metrics.

### 2. Source files and key functions
- **`scripts/swebench/oh_gt_full_wrapper.py`**
  - `_log_gt_interaction()` (lines 981-1078): writes to in-memory list + per-task JSONL file + instance_ref.
  - `_flush_interaction_log()` (lines 1167-1178): writes to instance_ref at task end.
  - `_emit_structured_event()` (lines 1080-1135): emits GTLayerEvent via writer.
  - `_emit_belief_event()` (lines 1138-1164): emits GTBeliefEvent via writer.
  - `_compute_has_real_evidence()` (lines 961-978): classifies whether GT output is real evidence vs placeholder.
- **`src/groundtruth/telemetry/writer.py`**
  - `GTTelemetryWriter` (lines 13-99): thread-safe JSONL writer for 3 streams (layer events, agent reactions, belief ledger).
  - `emit_layer_event()` (lines 43-52): writes GTLayerEvent.
  - `emit_agent_reaction()` (lines 54-63): writes GTAgentReactionEvent.
  - `emit_belief_event()` (lines 65-74): writes GTBeliefEvent.
- **`scripts/analysis/reaction_joiner.py`**
  - `join_gt_to_agent()` (lines 8-50): joins GT events to trajectory actions by iteration number.
  - `compute_follow_type()` (lines 52+): determines follow-through classification (FOLLOWED_EXACT, IGNORED, CONTRADICTED, etc.).
- **`src/groundtruth/telemetry/schemas.py`**
  - `GTLayerEvent` (lines 85-178): 40+ fields covering identity, position, trigger, decision, evidence, rendered output, next action, state.
  - `GTAgentReactionEvent` (lines 181-257): 35+ fields covering GT event linkage, agent response classification, follow metrics.
  - `GTBeliefEvent` (lines 260-304): file-level belief tracking (candidate -> supported -> verified -> etc.).

### 3. What is emitted to agent (rendered text)
Nothing. Meta/Reaction is purely observability infrastructure. The agent never sees these records.

### 4. What is emitted as structured telemetry
- **3 JSONL streams per task:**
  - `gt_layer_events_<task_id>.jsonl`: one line per GT layer event (L1, L3, L3b, L4, L5, L5b, L6, HYGIENE).
  - `gt_agent_reactions_<task_id>.jsonl`: one line per GT-to-agent reaction (joined post-run).
  - `gt_belief_ledger_<task_id>.jsonl`: one line per belief state change.
- **Interaction log:** `gt_interactions_<task_id>.jsonl` and `gt_meta_<task_id>.jsonl` — per-event records with `gt_sent`, `gt_sent_bytes`, `gt_sent_tokens`, `has_real_evidence`, `agent_action_before`, `agent_action_after`.

### 5. What is NOT measurable
- **Causal impact.** The reaction joiner measures structural follow-through (did the agent open the suggested file? did it run a targeted test?) but cannot prove causation. The agent may have been going to do that anyway.
- **Attention attribution.** We know the agent saw the GT evidence (it was in the observation), but not whether the LLM's attention mechanism weighted it.
- **Quality of follow.** `followed_within_1=True` means the agent did what GT suggested within 1 iteration, but the quality of that action is not measured (e.g., did the targeted test pass?).

### 6. Token/bloat behavior
Zero tokens to agent. JSONL files can grow large on long tasks (100 iterations * ~5 events/iter = 500 lines).

### 7. Test dependencies
None. Pure observability infrastructure.

### 8. Generalization to real repos
Fully general. Telemetry schemas are language/repo agnostic.

### 9. GT-side metrics available
- `has_real_evidence` classification per event (`_compute_has_real_evidence()`, lines 961-978).
- `gt_sent_bytes`, `gt_sent_tokens` per event.
- Per-layer hit counts from `GTTelemetry.utilization()`.
- Overall utilization weighted score (L1=0.2, L2=0.15, L3=0.2, L3b=0.1, L4=0.1, L5=0.15, L6=0.1) — wrapper lines 2090-2098.

### 10. Agent-side reaction metrics available
- Full reaction schema: `followed_within_1/3/5`, `followed_eventually`, `ignored`, `partial_follow`, `contradicted`, `finished_without_follow`, `ran_broad_test_after_gt`, `ran_targeted_test_after_gt`, `opened_suggested_file`, `edited_suggested_file`, `changed_diff_after_gt`.
- **CRITICAL GAP:** The reaction joiner (reaction_joiner.py line 25) ONLY processes events where `next_action_type` is populated. In smoke-1: 12 GTLayerEvents, **0 had next_action populated**, producing **0 reactions**. This means the reaction pipeline is structurally correct but produces no output for the current layer configuration.
- Layers that populate `next_action_type`: L3 (only when `l3_targeted_verification` found), L5b (always when fired). Layers that do NOT: L1, L3b, L4, L6, HYGIENE.

### 11. Fake/weak evidence patterns
- **`has_real_evidence` classifier is conservative.** It checks for specific GT tags in the output text (line 973-978). If a layer emits evidence in a new format, it will be classified as `has_real_evidence=False`. This is a maintenance burden.
- **Backfill race condition:** `agent_action_after` is backfilled on the PREVIOUS entry when the NEXT event fires (lines 1013-1029). If two GT events fire on consecutive iterations, the backfill may not capture the true agent response.
- **Instance_ref injection failures:** The belt-and-suspenders approach (lines 1069-1077) tries both dict assignment and setattr. If instance_ref is neither dict nor writable object, interaction log is lost from the artifact. The JSONL file write (lines 1052-1065) is the primary persistence — this is reliable.

---

## Relationship Indexer (GraphStore)

### 1. Current intended job
Bridge layer between the Go indexer's `graph.db` schema (nodes/edges) and the Python `SymbolStore` interface. All layers (L1, L3, L3b, L4, L5, L6) consume graph.db through this bridge.

### 2. Source files and key functions
- **`src/groundtruth/index/graph_store.py`**
  - `GraphStore` class (lines 121-845): extends `SymbolStore`, overrides read methods.
  - `is_graph_db()` (lines 59-86): schema detection (validates tables + required columns).
  - `_node_row_to_symbol()` (lines 88-106): maps Go indexer node to SymbolRecord.
  - `_edge_row_to_ref()` (lines 109-118): maps Go indexer edge to RefRecord.
  - `initialize()` (lines 132-147): opens DB, sets WAL mode, pre-computes usage cache.
  - `_has_confidence_column()` (lines 149-157): v14+ schema detection.
  - `_confidence_filter()` (lines 159-163): SQL fragment for confidence gating.
  - `get_hotspots()` (lines 422-446): most-referenced symbols with confidence filter.
  - `get_dead_code()` (lines 448-466): exported symbols with zero incoming edges.
  - `get_functions_in_file()` (lines 678-705): all function/method nodes in a file.
  - `get_sibling_functions()` (lines 709-759): same-class or same-file siblings.
  - `get_assertions_in_file()` (lines 761-784): assertions from v16+ schema.
  - `get_function_at_line()` (lines 786-812): function node containing a specific line.
  - Write methods: all return `Err(READ_ONLY_ERR)` (lines 821-845).

### 3. What is emitted to agent (rendered text)
Nothing directly. GraphStore is infrastructure consumed by L1, L3, L3b, and L4.

### 4. What is emitted as structured telemetry
None directly. Consumers (L3, L3b) emit telemetry that includes data from GraphStore queries.

### 5. What is NOT measurable
- Edge correctness. GraphStore returns whatever graph.db contains. If the Go indexer produced false-positive edges (name_match with confidence 0.2), GraphStore faithfully returns them. The confidence filter (`>= 0.5`) is applied by consumers, not by GraphStore itself (except in methods that accept `min_confidence` parameter).
- Schema drift. If the Go indexer schema changes (new columns, renamed tables), GraphStore may silently fail. The `is_graph_db()` function checks for required columns but not optional ones.

### 6. Token/bloat behavior
N/A — infrastructure layer, not token-producing.

### 7. Test dependencies
GraphStore stores and returns test nodes (`is_test` flag). The `get_hotspots()` method filters `WHERE n.is_test = 0` (line 434). `get_dead_code()` does not filter by test status. `get_functions_in_file()` returns ALL functions including test functions.

### 8. Generalization to real repos
- Fully language-agnostic. `_LABEL_TO_KIND` mapping (lines 34-47) covers: Function, Class, Method, Interface, Struct, Enum, Type, File, Variable, Constant, Property, Field.
- `_EDGE_TYPE_TO_REF` (lines 50-56): CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS.
- Handles both absolute and relative file paths via `_match_file_path()` (lines 355-381).
- **Weakness:** `_normalize_path()` (lines 347-352) strips `./` prefix and normalizes slashes. Does not handle Windows paths on Linux containers (irrelevant for SWE-bench but matters for local MCP usage).

### 9. GT-side metrics available
- `get_stats()` (lines 272-297): symbols_count, files_count, refs_count.
- `get_high_confidence_edge_ratio()` (lines 468-480): % of edges with confidence >= 0.7.
- `get_property_counts()` (lines 655-663): property kinds distribution.
- `get_assertion_count()` (lines 665-674): total assertions.

### 10. Agent-side reaction metrics available
None. Infrastructure layer.

### 11. Fake/weak evidence patterns
- **Usage cache stale after reindex:** `_build_usage_cache()` runs once at `initialize()` (line 139). After L6 reindexes a file, the cache is NOT rebuilt. This means `_usage_for()` returns stale counts for the rest of the task. Impact: `_top_functions()` in L1 and `get_hotspots()` may return wrong ordering.
- **LIKE suffix match:** Multiple methods use `WHERE file_path LIKE ?` with `f"%{norm_path}"` (e.g., `get_callers_from_graph` in post_edit.py line 231). This can false-match: `LIKE '%utils.py'` matches both `src/utils.py` and `tests/test_utils.py`. The confidence filter mitigates but doesn't eliminate.

---

## Summary: Critical Gaps

| Gap | Severity | Affected Layers |
|-----|----------|----------------|
| Reaction joiner produces 0 reactions because no events populate `next_action_type` | HIGH | L1, L3b, L4, L6, HYGIENE |
| L3b has no token cap — 1810 avg chars/fire, biggest bloat vector | MEDIUM | L3b |
| L4 tools installed but never used by agent (0 usage in smoke-1) | MEDIUM | L4 |
| GraphStore usage cache stale after L6 reindex | LOW | L1 (function ordering), L3b (hotspot ranking) |
| L5 governor fired 0 times in smoke-1 (agent went to collapsed state without triggering any anti-pattern) | HIGH | L5, L5b |
| No mechanism to measure whether agent READ any GT injection | HIGH | All |
| `_brief_max_tokens` in wrapper (500) differs from `MAX_BRIEF_TOKENS` in v1r_brief.py (400) — double cap | LOW | L1 |
| Low-signal brief fallback logged as `ok=True` in telemetry | LOW | L1 |
| L3b fires on every post_view but reaction is unmeasurable (no `next_action_type`) | HIGH | L3b |
| 2 deprecated hooks (`hook_patch_hypothesis`, `hook_symptom_convergence`) are dead code | LOW | L5b |
