# GT Resolution Plan — fact-set + de-duplication (2026-06-15)

Grounded in CLAUDE.md (cited): GOAL = correct context → correct code → flips, **generalized**;
fact = receiver-proven only (`import/same_file/type_flow/lsp/verified_unique`); correct-or-quiet
(wrong info worse than no info); ONE PRODUCT RULE (one pipeline, never fragment). Two-stage
methodology: this is all **Stage 1 — deterministic correctness, provable with $0/no runs.**

## The diagnosis (5 reasons under ~24 bug surfaces, from the §4 trajectory read)

| reason | layer (LIPI) | root cause | DeepSWE-live copies | single-source? |
|---|---|---|---|---|
| **R1 impl_method = fact** | Integration | resolver tiers it CANDIDATE (resolver.go:1811-1821, "NEVER CERTIFIED"); consumer fact set lists it FACT. External libs unindexed → only same-named method is in-repo → `httpx.post`→`fastapi.post` stamped deterministic. **30.7% py / 44% js of CALLS.** | `curation_map` (imported by `gt_mini_patch`) | ✅ single-source |
| **R3 edit-risk on tokens** | Logic | `structural_edit_risk` runs over edited *tokens* not edited *symbols* → names hubs (`fastapi`/`push`/`run`) | `runtime/edit_risk.py` (imported) | ✅ single-source |
| **R4 test/demo filter** | Integration | filter in delivery producers, absent in localizer; path-only (misses test-string symbols) | `v1r_brief._is_test_path` **+** `gt_mini_patch._is_test_or_demo_path` | ❌ 2 copies |
| **R5 witness renderer** | Implementation | per-construct render bugs: direction inverted, method→class-name signature, docstring bleed | `graph_localizer.render_witness`/`v1r_brief` **+** `gt_mini_patch._resolved_witnesses_for_file` | ❌ 2 copies |
| **R2 localization rank** | Logic | localizer ranks existing call-graph degree, not the issue's new/leaf target | `graph_localizer.localize` + `run_v74` (brief-only) | ⚠️ 1 engine, split |

DeepSWE-live engines = **brief** (`v1r_brief`+`graph_localizer`+`curation_map`) + **per-turn** (`gt_mini_patch`).
OH twin (`oh_gt_full_wrapper`, `hooks/post_view`, `hooks/post_edit`), `v7_brief`, `edit_predicates` =
**dead for DeepSWE → out of scope.** Primitives (fact set, vendored, stdlib-shadow, cross-lang, edit-risk)
are already **imported** by `gt_mini_patch` (single-source). Only the **producers** (cochange/scope/
contract/witness/test-filter) are reimplemented → exactly 2 copies each.

## Resolution order (forced by single-source status, not by impact)

1. **R1 — fact-set cut (DO FIRST, single-source, generalized, correct-or-quiet).** Remove `impl_method` +
   `unique_method` from `curation_map.DETERMINISTIC_RESOLUTION_METHODS` (+ the `gt_mini_patch` fallback).
   Propagates to every importing consumer. Edges are NOT deleted — relabeled `(unverified)`, still
   delivered as leads. Update the ~6 tests that encode the old "impl_method = fact" premise (stand-in
   fixtures → `type_flow`; the one premise test asserts impl_method is now CANDIDATE).
2. **R3 — edit-risk to changed-symbols (single-source).** Scope `structural_edit_risk` to the symbols
   whose *definition lines* fall in the agent's edited hunks, not every diff token.
3. **R4/R5/producers — DE-DUPLICATE FIRST, then fix.** Make `gt_mini_patch` *import* the test-filter +
   witness renderer + cochange/scope/contract from the product instead of redefining. Then fix the one
   source. Guardrail: a byte-parity harness (one `graph.db` → both producers → assert identical bytes).
4. **R2 — localization ranking (LAST, research-gated).** BRIEFING invariants apply (measure
   `generate_v1r_brief`, semantic on, one weight at a time). Not a hygiene fix; the real lever.

## Status
- [in progress] R1 fact-set cut + test updates + byte-parity guardrail.
- All steps Stage-1 deterministic — verified by tests on the real substrate, zero agent runs.
