# ORDER_1.0 Audit Map — FINAL_ARCH_V2 Context Freeze

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 0

## Rules

- Every claim cites file/function/line or exact artifact.
- Uncited = NOT PROVEN.
- This file maps; it does not diagnose.

---

## L0 — Graph Substrate

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| Go indexer defines schema with trust_tier columns | `gt-index/internal/store/sqlite.go` | 136–139 | `trust_tier TEXT DEFAULT 'SPECULATIVE', candidate_count INTEGER DEFAULT 1, evidence_type TEXT, verification_status TEXT DEFAULT 'unverified'` | PROVEN |
| Go indexer stamps schema_version in project_meta | `gt-index/cmd/gt-index/main.go` | ~393 | `const schemaVersion = "v15.1-trust-tier"` + `db.SetMeta("schema_version", schemaVersion)` | PROVEN |
| Python schema verifier exists | `src/groundtruth/index/schema_version.py` | 18,25,45,78 | `REQUIRED_EDGE_COLUMNS`, `REQUIRED_SCHEMA_VERSION = "v15.1-trust-tier"`, `probe()`, `verify_graph_db_schema()` | PROVEN |
| Deployed graph.db files lack trust_tier columns | `.tmp_holdout/bugs/axum-3661/graph.db` | PRAGMA | edges cols = `['id','source_id','target_id','type','source_line','source_file','resolution_method','confidence','metadata']` | PROVEN (stale artifact) |
| GHA always rebuilds gt-index from current Go source | `.github/actions/setup-eval/action.yml` | 54 | `GT_SRC_HASH=$(find ... -name '*.go' -exec sha256sum ...)` | PROVEN |
| graph.db is built per-task inside container | `scripts/swebench/oh_gt_full_wrapper.py` | 3437 | `l4_ok = install_graph_and_hook(runtime, config)` | PROVEN |
| B-7 pre-fetch downloads graph.db to host before first action | `scripts/swebench/oh_gt_full_wrapper.py` | 3439–3468 | `_download_graph_db_to_host(runtime, config.graph_db)` + `config._host_graph_db = _local_db` | PROVEN (code exists, GHA run 25996587814 pending artifact verification) |
| B-7 router cache reset after L6 reindex | `scripts/swebench/oh_gt_full_wrapper.py` | 2766–2767 | `config._router_v2 = None` after `_download_graph_db_to_host` | PROVEN |

## L1 — Pre-Task Seed

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| V1R brief is the active brief pipeline | `scripts/swebench/oh_gt_full_wrapper.py` | ~3483 (brief generation section) | Calls `generate_v1r_brief` or `generate_brief` from `v1r_brief` | PROVEN (per Decision 8, DECISIONS.md:187-201) |
| V1R generates candidates using hybrid scorer | `src/groundtruth/pretask/v1r_brief.py` | ~607 | `generate_v1r_brief()` calls `v7_4_brief.py` for scoring | PROVEN |
| V7.4 hybrid scorer uses sem+lex+reach+hub_pen | `src/groundtruth/pretask/v7_4_brief.py` | 272-273 | W_SEM=0.25, W_LEX=0.35, W_REACH=0.20, W_PROX=0.05, W_HUB=0.15; W_SEM=0 fallback when sentence-transformers missing | PROVEN |
| Brief injected as one-shot into agent instruction | `scripts/swebench/oh_gt_full_wrapper.py` | (brief injection section) | `<gt-task-brief>` tag in initial instruction | PROVEN |
| sentence-transformers unavailable in container → W_SEM=0 | `src/groundtruth/pretask/v7_4_brief.py` | 272-273 | `_ZeroEmbeddingModel` fallback | PROVEN |
| Brief never suppressed (modulus gate removed) | `src/groundtruth/pretask/v1r_brief.py` | ~413 | `Decision 29: redundancy suppression removed` | PROVEN (per jedi_WORK Phase 3) |

## L2 — Agent-State Tracker

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| AgentState class exists | `src/groundtruth/state/agent_state.py` | 575 | `AgentState` dataclass with task_id, repo_root, max_iterations, viewed_files, edited_files, searches, current_focus, pending_suggestions, etc. | PROVEN |
| AgentState lazy-initialized in wrapper | `scripts/swebench/oh_gt_full_wrapper.py` | (wrapper section) | `_ensure_agent_state(config)` helper | PROVEN |
| Backwards-compat shim preserves old imports | `src/groundtruth/trajectory/state.py` | re-exports | Re-exports L5TrajectoryState, FailureSnapshot, etc. from new module | PROVEN |
| PendingSuggestion lifecycle tracked | `src/groundtruth/state/agent_state.py` | ~759,783 | `register_pending_suggestion()`, `process_agent_action()` with TTL=3 actions | PROVEN |
| State persisted to task-scoped JSON | `src/groundtruth/state/agent_state.py` | (sidecar path) | `/tmp/gt_agent_state_<task>.json` | PROVEN |
| 31 unit tests pass | `tests/state/test_agent_state.py` | all | 31 tests covering path normalization, view/edit/search tracking, suggestion lifecycle, persistence | PROVEN |

## L3 — Collaboration Router

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| CollaborationRouter class exists | `src/groundtruth/router/router.py` | 56 | `CollaborationRouter` with `on_view()` at 99, `on_edit()` at 206 | PROVEN |
| Router has budget gates | `src/groundtruth/router/router.py` | 51-53 | `DEFAULT_VIEW_BUDGET=3`, `DEFAULT_EDIT_BUDGET=5`, `DEFAULT_TOTAL_BUDGET=5` | PROVEN |
| Router has debounce logic | `src/groundtruth/router/router.py` | 392 | `_debounced()` — same kind within 3 iterations | PROVEN |
| Router has late-band gate | `src/groundtruth/router/router.py` | 386 | `_is_late_band()` — iteration >= 75% max_iterations | PROVEN |
| Suppression reasons enumerated | `src/groundtruth/router/decisions.py` | (enum) | `SuppressionReason`: NO_GRAPH_DB, BUDGET, TOO_LATE, DEBOUNCE, NO_EVIDENCE, etc. | PROVEN |
| GT_ROUTER_V2 tri-state mode | `scripts/swebench/oh_gt_full_wrapper.py` | 1483-1505 | `_router_v2_mode()` returns off/shadow/live | PROVEN |
| Live mode bypasses legacy L3/L3b | `scripts/swebench/oh_gt_full_wrapper.py` | 2426-2446 (post_view), 2786-2803 (post_edit) | `_write_router_v2_legacy_skip()` + `return obs` before legacy hook | PROVEN |
| Router events persisted to disk | `scripts/swebench/oh_gt_full_wrapper.py` | 1655 | `_persist_router_v2_event()` writes to interaction_log + gt_interactions JSONL + gt_layer_events | PROVEN |
| L3_router_v2 in VALID_LAYERS enum | `src/groundtruth/telemetry/constants.py` | (enum addition) | `"L3_router_v2"` added | PROVEN |
| End-of-task fail-fast counter | `scripts/swebench/oh_gt_full_wrapper.py` | ~3070 | `[GT_META] router_v2 final: mode=... calls=N events_persisted=M` | PROVEN |

### Legacy L3/L3b (non-router path)

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| Legacy post_view runs graph_navigation in container | `scripts/swebench/oh_gt_full_wrapper.py` | 2462 | `_run_internal(orig_run_action, make_view_hook_command(event, config), 30)` | PROVEN |
| Legacy post_edit runs generate_improved_evidence in container | `scripts/swebench/oh_gt_full_wrapper.py` | 2826-2837 | `_run_internal(orig_run_action, make_edit_hook_command_with_artifacts(...), 45)` | PROVEN |
| Budget gate: L3b max 3 fires | `scripts/swebench/oh_gt_full_wrapper.py` | 2448 | `if config._l3b_fire_count >= 3: return obs` | PROVEN |
| Budget gate: L3 max 5 fires | `scripts/swebench/oh_gt_full_wrapper.py` | 2805 | `if config._l3_fire_count >= 5: return obs` | PROVEN |
| Budget gate: suppress L3b after 75% iteration | `scripts/swebench/oh_gt_full_wrapper.py` | 2450 | `if config.action_count > 0.75 * config.max_iter: return obs` | PROVEN |
| Budget gate: suppress L3 same-file 3+ edits | `scripts/swebench/oh_gt_full_wrapper.py` | 2807 | `if config._l5_edit_counts_per_file.get(...) >= 3: return obs` | PROVEN |

## L4 — Evidence Providers

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| Graph edge providers exist | `src/groundtruth/providers/graph_providers.py` | 85,124,159 | `caller_provider()`, `callee_provider()`, `importer_provider()` — all query graph.db with conf >= 0.5 | PROVEN |
| Evidence providers exist | `src/groundtruth/providers/evidence_providers.py` | 159,287,314,373,454 | `caller_code_provider()`, `contract_provider()`, `sibling_twin_provider()`, `test_provider()`, `co_change_provider()` | PROVEN |
| Legacy post_edit evidence generation | `src/groundtruth/hooks/post_edit.py` | 749 | `generate_improved_evidence()` — callers+siblings+signature+tests | PROVEN |
| Legacy post_view graph navigation | `src/groundtruth/hooks/post_view.py` | 232 | `graph_navigation()` — callers, callees, importers with issue-aware ranking | PROVEN |
| Providers are MIX (entangled with timing) | `src/groundtruth/hooks/post_view.py` | 232 | `graph_navigation()` decides WHEN (fires on every read, budgeted) AND WHAT (provider work) | PROVEN (DECISIONS.md:1889 identifies this as MIX) |

## L5 — Post-Edit Validator

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| PostEditWarning types defined | `src/groundtruth/validators/post_edit_validator.py` | 35,41 | `WarningKind` enum (SIGNATURE_BROKEN_CALLER, RETURN_TYPE_CHANGED, CO_CHANGE_MISSED), `PostEditWarning` dataclass | PROVEN |
| Signature break checker | `src/groundtruth/validators/post_edit_validator.py` | 104 | `check_signature_break()` | PROVEN |
| Co-change miss checker | `src/groundtruth/validators/post_edit_validator.py` | 152 | `check_co_change_miss()` | PROVEN |
| Orchestrator function | `src/groundtruth/validators/post_edit_validator.py` | 193,217 | `check_post_edit()` + `post_edit_validator()` alias | PROVEN |
| Validator is NOT wired into wrapper observation path | `scripts/swebench/oh_gt_full_wrapper.py` | (grep) | No call to `post_edit_validator` or `check_post_edit` in the wrapper | NOT PROVEN — needs verification |

## L6 — Metrics & Reindex

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| L6 reindex fires after post_edit | `scripts/swebench/oh_gt_full_wrapper.py` | 2700-2753 | `_run_internal(orig_run_action, reindex_cmd + "; echo __EXIT__$?", 120)` + mtime comparison | PROVEN |
| Reindex success/failure logged | `scripts/swebench/oh_gt_full_wrapper.py` | 2735 | `[GT_META] L6 reindex OK/FAIL` + structured event | PROVEN |
| graph.db refreshed to host after L6 | `scripts/swebench/oh_gt_full_wrapper.py` | 2755-2777 | `_download_graph_db_to_host` + `config._router_v2 = None` | PROVEN |
| Reindex helper exists in library | `src/groundtruth/runtime/reindex_helper.py` | 54 | `_try_incremental_reindex()` | PROVEN |
| Report builder exists | `src/groundtruth/runtime/report.py` | 13,72 | `build_benchmark_report()`, `write_benchmark_report()` | PROVEN |
| Canary metrics script | `scripts/compute_canary_metrics.py` | (full) | Computes per-task metrics for CANARY_COMPARISON.md | PROVEN |

## GHA / Workflow

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| Canary 3-arm workflow exists | `.github/workflows/canary_3arm.yml` | 1-283 | arms: baseline/old_gt/v2_live/v2_shadow; maps to GT_BASELINE + GT_ROUTER_V2 flags | PROVEN |
| Workflow passes GT_ROUTER_V2 to agent env | `.github/workflows/canary_3arm.yml` | 178 | `GT_ROUTER_V2: ${{ needs.prepare.outputs.router_v2_mode }}` | PROVEN |
| Pre-flight validates gt-index binary + schema | `.github/workflows/canary_3arm.yml` | 127-156 | CHECKs 1-5: binary exists, graph.db schema OK, Python verifier OK, router import OK, DeepSeek auth | PROVEN |
| Post-run fail-fast for V2 arms | `.github/workflows/canary_3arm.yml` | 204-217 | Greps for L3_router_v2 events + router_v2_legacy_skip | PROVEN |
| GT artifacts staged beside output.jsonl | `.github/workflows/canary_3arm.yml` | 226-232 | Copies gt_interactions, gt_layer_events, gt_belief, gt_hooks.log | PROVEN |
| graph.db pulled beside output.jsonl | `.github/workflows/canary_3arm.yml` | 234-245 | Copies from gt_debug | PROVEN |

## Canary Results (prior run — BEFORE B-7 fix)

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| V2_LIVE had 0 agent-visible router emissions | `reports/canary/V2_LIVE_DEEP_TELEMETRY_2026-05-17.md` | 15-19 | All 37 router calls suppressed with `no_graph_db` across 3 tasks | PROVEN |
| V2_LIVE was worse than OLD_GT on action-path metrics | `reports/canary/CANARY_COMPARISON.md` | 87-91 | V2 action_economy >= 1.00 on every task; V2 injections=0 on 2/3 tasks | PROVEN |
| Decision: do NOT continue V2 activation (pre-B-7) | `reports/canary/CANARY_COMPARISON.md` | 89 | "Do NOT continue V2 activation" | PROVEN |
| B-7 fix dispatched as re-canary | GHA run `25996587814` | n/a | v2_live with B-7 pre-fetch; completed successfully (all 3 tasks) | PROVEN (run completed; artifacts not yet downloaded) |

## Canary Results (B-7 fix run — pending artifact analysis)

| Claim | File | Lines | Evidence | Status |
|---|---|---:|---|---|
| B-7 pre-fetch produces graph_db_present=true | GHA run `25996587814` logs | n/a | Expected `[GT_META] B-7 pre-fetch: graph.db downloaded to host` | NOT PROVEN — artifacts not downloaded |
| Router emits real evidence (not all no_graph_db) | GHA run `25996587814` gt_interactions | n/a | Expected at least one `emit=True` with text_len > 0 | NOT PROVEN — artifacts not downloaded |
| output.jsonl contains [GT-router-v2] | GHA run `25996587814` output.jsonl | n/a | Expected `[GT-router-v2 on_view]` or `[GT-router-v2 on_edit]` in content | NOT PROVEN — artifacts not downloaded |
| Legacy bypassed in live mode | GHA run `25996587814` gt_interactions | n/a | Expected `router_v2_legacy_skip` rows | NOT PROVEN — artifacts not downloaded |

---

## Layer Integration Gaps (known)

| Gap | Layer | Description | Blocking? |
|---|---|---|---|
| L5 validator not wired into wrapper | L5 | `post_edit_validator.py` exists but is not called from the wrapper's observation path | YES for L5 delivery |
| Providers entangled with timing | L3/L4 | `graph_navigation()` and `generate_improved_evidence()` decide WHEN and WHAT in the same function | YES for clean router architecture |
| L5 governor hooks dead (0 fires on 30-task) | L3 (was L5) | Agent doesn't run FAIL_TO_PASS tests; precondition for hypothesis_falsified never holds | NO for router, YES for full L5 value |
| B-7 fix not yet verified in artifacts | L0→L3 | Code exists, run completed, artifacts not downloaded | BLOCKING re-canary analysis |
| Old FINAL_ARCH layer names in code comments | all | Many comments reference "Layer A/B/C/D" or "L3b/L5" | NO (naming, not logic) |

---

## Files Read for This Audit

- `CLAUDE.md` (project + .claude/)
- `DECISIONS.md` (full, 1900+ lines)
- `SESSION_SUMMARY.md` (3 sessions)
- `jedi_WORK.md` (Phases 0-4)
- `RUNTIME_PARITY_AUDIT.md` (B-1 through B-7)
- `reports/canary/V2_LIVE_DEEP_TELEMETRY_2026-05-17.md`
- `reports/canary/CANARY_COMPARISON.md`
- `docs/handoff/canary_v2_runbook.md`
- `scripts/swebench/oh_gt_full_wrapper.py` (lines 1460-1660, 2350-2550, 2700-2900, 3390-3490)
- `src/groundtruth/router/router.py` (via Explore agent)
- `src/groundtruth/router/decisions.py` (via Explore agent)
- `src/groundtruth/state/agent_state.py` (via Explore agent)
- `src/groundtruth/providers/graph_providers.py` (via Explore agent)
- `src/groundtruth/providers/evidence_providers.py` (via Explore agent)
- `src/groundtruth/validators/post_edit_validator.py` (via Explore agent)
- `src/groundtruth/hooks/post_view.py` (via Explore agent)
- `src/groundtruth/hooks/post_edit.py` (via Explore agent)
- `src/groundtruth/index/schema_version.py` (via Explore agent)
- `src/groundtruth/runtime/reindex_helper.py` (via Explore agent)
- `src/groundtruth/runtime/report.py` (via Explore agent)
- `src/groundtruth/pretask/v1r_brief.py` (via Explore agent)
- `src/groundtruth/pretask/v7_4_brief.py` (via Explore agent)
- `.github/workflows/canary_3arm.yml`
- `.github/actions/setup-eval/action.yml`
