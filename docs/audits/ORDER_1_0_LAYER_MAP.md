# ORDER_1.0 Layer Map — FINAL_ARCH_V2 Code Responsibility

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 1

---

## Layer Map Table

| Layer | Current files/functions | Intended job | Actual behavior | Timing point | GT-side metric | Agent-side metric | Failure modes |
|---|---|---|---|---|---|---|---|
| **L0 Graph Substrate** | `gt-index/internal/{parser,resolver,store}/`; `src/groundtruth/index/schema_version.py`; wrapper `install_graph_and_hook()` at `:3437` | Build trust-scored graph.db; invisible to agent | Go indexer produces graph.db per-task inside container. Schema version stamped. Python verifier can validate. B-7 pre-fetches to host. | Task init (before first action) | graph_db_exists, graph_schema_valid, graph_host_readable | N/A (invisible) | Stale deployed DBs missing trust_tier columns; graph.db build failure; pre-fetch failure leaves router blind |
| **L1 Pre-task Seed** | `src/groundtruth/pretask/v1r_brief.py` `generate_v1r_brief()`; `v7_4_brief.py` `run_v74()`; `hybrid.py` `lexical_file_search()`; `hub_penalty.py`; `graph_reach.py` | One-shot brief injection: ranked files + 1-hop neighbors + signatures | V1R brief using BM25+reach+hub_pen (W_SEM=0 in container). Injected as `<gt-task-brief>` in agent initial instruction. Never suppressed (modulus gate removed). | Once, at task start | l1_brief_emitted, l1_candidate_files, l1_candidate_symbols | l1_agent_opened_candidate, l1_candidate_before_first_gold_view | sentence-transformers missing (handled by W_SEM=0 fallback); hub-gate may suppress in rare dense repos |
| **L2 AgentState** | `src/groundtruth/state/agent_state.py` `AgentState`; `_ensure_agent_state()` in wrapper; `src/groundtruth/trajectory/{state,classifier,parsers}.py` | Track agent trajectory: viewed, edited, searches, focus, suggestions | AgentState class exists with full schema. Lazy-initialized in wrapper. Tracks views, edits, suggestions with TTL. Persisted to task-scoped JSON. Legacy L5TrajectoryState re-exported via shim. | Continuous (every action boundary) | viewed_files_recorded, edited_files_recorded, searches_recorded, state_matches_output_jsonl | N/A (internal tracking) | State not consulted by legacy L3/L3b hooks (they read /tmp files instead); state lost if wrapper crashes |
| **L3 Router (V2)** | `src/groundtruth/router/router.py` `CollaborationRouter`; `decisions.py` `RouterEmission`, `SuppressionReason`; wrapper `_router_v2_mode()`, `_ensure_v2_router()`, `_router_v2_on_view()`, `_router_v2_on_edit()` at `:1483-1652` | Decide WHEN to surface evidence; budget, debounce, band-gate; sole L3/L3b path in live mode | Router instantiated with AgentState + graph.db. Budget: view=3, edit=5, total=5. Debounce=3 iter. Late-band gate at 75%. Suppression reasons tracked. In live mode, legacy hooks bypassed. | Every post_view and post_edit event | router_called_count, router_emit_count, router_suppression_count_by_reason, router_no_graph_db_count | router_evidence_agent_visible, agent_followed_router_edge | B-7 (host graph.db unavailable pre-edit) — fixed in code, pending artifact verification; router blind when graph.db empty or missing |
| **L3 Router (Legacy)** | wrapper post_view at `:2448-2523` calling `make_view_hook_command()`; wrapper post_edit at `:2804-2898` calling `make_edit_hook_command_with_artifacts()` | Run graph_navigation/generate_improved_evidence in container subprocess | Fires on every view/edit up to budget caps (L3b<=3, L3<=5, 75% suppress, same-file-3+). Evidence markers: Called by, Calls into, SIGNATURE, CALLERS, TWINS, etc. Hash dedup. Curation gate: L3b suppressed after first source edit unless file is brief candidate. | Every post_view and post_edit event (except live mode) | injections_per_task, bridge_event_before_gold | agent_followed_gt_edge, stale_guidance_count, late_guidance_count | Over-injection (D35 regression); provider+timing entangled in same function; dedup may miss semantically-similar-but-different evidence |
| **L4 Providers** | `src/groundtruth/providers/graph_providers.py` `caller_provider()`, `callee_provider()`, `importer_provider()`; `evidence_providers.py` `caller_code_provider()`, `contract_provider()`, `sibling_twin_provider()`, `test_provider()`, `co_change_provider()` | Pure evidence extraction: given target+kind, return evidence list | Clean provider functions exist. Query graph.db with conf>=0.5. Issue-aware ranking via `_score_by_issue_relevance`. **BUT**: router.py `on_view()`/`on_edit()` calls these directly; legacy hooks call their own equivalent logic in `post_view.py:graph_navigation()` and `post_edit.py:generate_improved_evidence()`. | On demand from L3 router or legacy hooks | provider_request_count, provider_empty_count, evidence_type_count | provider_output_rendered_to_agent | test_provider returns empty (assertion target resolution broken per jedi_WORK:285); co_change_provider requires git history in container |
| **L5 Validator** | `src/groundtruth/validators/post_edit_validator.py` `check_post_edit()`, `check_signature_break()`, `check_co_change_miss()` | Detect actionable contradictions after edit. Silent on success. | Code exists: signature break detection, co-change miss detection. **NOT wired into wrapper.** No call from `oh_gt_full_wrapper.py` to any validator function. | Should fire after each edit (post L6 reindex) | validator_called_count, actionable_contradiction_count | agent_reedited_after_validator | **Completely unwired** — validator never fires in any run. Dead code in production path. |
| **L5 Governor (legacy Goku)** | `src/groundtruth/trajectory/governor.py` `L5Governor`; `hooks.py` 7 hook implementations; `L5bSafetyChecker`; wrapper Goku integration at `:2369-2400` | Detect trajectory risks (ignored suggestion, weak verification, finish unverified) | Governor infrastructure works: state tracking, parsers, classifiers. **0 new hook fires on 30-task run** (D31). Precondition gap: agent doesn't run FAIL_TO_PASS tests. D34§12 budget rule (max 2 L5b injections/task) locked. | CmdRunAction boundaries, post_edit, finish event | (should produce: l5_event_count, l5b_injection_count) | (should measure: agent_reedited_after_validator) | Hypothesis_falsified precondition doesn't hold; Goku fires 14 L5b interventions on beets → over-injection; max 2 injection rule applies |
| **L6 Metrics & Reindex** | wrapper L6 reindex at `:2700-2753`; `src/groundtruth/runtime/reindex_helper.py`; `report.py`; `scripts/compute_canary_metrics.py`; `scripts/localization_metrics.py` | Incremental reindex after edit; measure per-layer contribution | L6 reindex fires after every post_edit. Checks mtime delta. Downloads refreshed graph.db to host. Resets router cache (B-7 fix). Structured events logged. Canary metrics script computes per-task comparison. | After every post_edit (reindex); continuous (metrics) | graph_refreshed_after_l6, router_cache_reset_after_graph_refresh | N/A (metrics layer) | Reindex failure leaves stale graph; mtime comparison may be unreliable across filesystems |

---

## Layer Assignment Clarity

| Layer | Clearly assigned? | Ambiguity |
|---|---|---|
| L0 Graph Substrate | YES | None. Go indexer + schema version + pre-fetch all clearly L0. |
| L1 Pre-task Seed | YES | None. V1R brief is pure L1. |
| L2 AgentState | YES (class), PARTIAL (usage) | AgentState class is clean L2. But legacy hooks bypass it (read /tmp files directly). Router uses it via `_ensure_agent_state`. |
| L3 Router (V2) | YES (router class), NEEDS VALIDATION (integration) | Router exists with correct budgets/debounce. Live mode bypasses legacy. B-7 fix pending verification. |
| L3 Legacy | LAYER-CONFUSING | `graph_navigation()` and `generate_improved_evidence()` mix L3 (when) + L4 (what). Budget caps are in the wrapper, not in the provider functions. |
| L4 Providers | PARTIAL | Clean provider functions exist in `src/groundtruth/providers/`. But legacy hooks duplicate provider logic internally. Router calls the clean providers. |
| L5 Validator | NOT WIRED | Code exists. Zero integration into the wrapper observation path. |
| L5 Governor | LAYER-CONFUSING | Governor is architecturally L3 (decides when to intervene). Named L5 by position. Dead hooks. |
| L6 Metrics | YES | Reindex is clear L6. Metrics scripts exist. |

---

## Critical Blockers for ORDER_1.0 Stage 2+

1. **L5 Validator unwired** — `post_edit_validator.py` defines `check_post_edit()` but no call path exists from the wrapper. This means ORDER_1.0 Stage 2's L5 metrics (`validator_called_count`, `actionable_contradiction_count`) will all be 0 until wiring is added.

2. **Provider/timing entanglement** — `graph_navigation()` and `generate_improved_evidence()` in the legacy hooks mix L3 (timing decisions) with L4 (evidence extraction). The V2 router + clean providers solve this architecturally, but the legacy path is still the active path when `GT_ROUTER_V2=off`.

3. **B-7 fix verification pending** — The code exists and the GHA run completed, but artifacts haven't been downloaded and verified. Until proof points are checked (graph_db_present=true on first post_view, router emits non-no_graph_db, [GT-router-v2] in output.jsonl), B-7 cannot be marked resolved.

4. **AgentState bypassed by legacy hooks** — Legacy L3/L3b hooks read `/tmp/gt_viewed.txt`, `/tmp/gt_issue_terms.txt`, `/tmp/gt_brief_candidates.txt` directly via `_load_*` helpers in `post_view.py`. They do not use the canonical `AgentState` object. This means L2 state and legacy hook state can diverge.

---

## Focus Areas (per ORDER_1.0 Stage 1 requirements)

### wrapper post_view / post_edit
- **post_view**: Lines 2413-2523. Router V2 call → baseline check → live-mode bypass → budget gate → legacy `make_view_hook_command` → evidence check → curation gate → primary-edge extraction → observation append.
- **post_edit**: Lines 2700-2898. L6 reindex → graph.db host download → router cache reset → baseline check → live-mode bypass → budget gate → legacy `make_edit_hook_command_with_artifacts` → evidence check → observation append.

### graph.db build/copy/reindex
- Built at task init by `install_graph_and_hook()` (line 3437).
- Pre-fetched to host at line 3439-3468 (B-7 fix).
- Incrementally reindexed at lines 2700-2753 (L6).
- Downloaded to host after reindex at lines 2755-2777.

### AgentState persistence
- `AgentState` class at `src/groundtruth/state/agent_state.py:575`.
- Lazy-initialized by `_ensure_agent_state(config)` in wrapper.
- Persisted to `/tmp/gt_agent_state_<task>.json`.
- Views, edits, searches, suggestions tracked with TTL.

### CollaborationRouter lifecycle
- Created by `_ensure_v2_router(config)` at wrapper line 1518.
- Cached on `config._router_v2`.
- Reset to `None` after L6 reindex (B-7 fix, line 2767).
- Uses `db_path = getattr(config, "_host_graph_db", "") or config.graph_db`.

### Providers
- Clean providers in `src/groundtruth/providers/{graph_providers,evidence_providers}.py`.
- Legacy providers embedded in `hooks/{post_view,post_edit}.py`.
- Router calls clean providers; legacy path calls legacy providers.

### Validator
- Code at `src/groundtruth/validators/post_edit_validator.py`.
- Zero calls from wrapper. Dead in production.

### Metrics parser
- `scripts/compute_canary_metrics.py` — computes per-task metrics from output.jsonl.
- `scripts/localization_metrics.py` — computes L1 quality metrics.
- `src/groundtruth/runtime/report.py` — builds benchmark reports.

### GHA/local/VM artifacts
- GHA: `canary_3arm.yml` uploads `gt_debug/`, `output.jsonl`, `graph.db`, `gt_interactions`, `gt_hooks.log`, `eval_result.json`.
- Local: wrapper writes to `/tmp/gt_interactions_<task>.jsonl`, `/tmp/gt_layer_events_<task>.jsonl`, `/tmp/gt_debug/`.
- VM: same as GHA path but manual artifact collection.
