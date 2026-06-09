# Ledger — aws-cloudformation__cfn-lint-4002  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4002"]`), baseline_pass=**no** (NOT in `full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json` `resolved_ids` — flip-CANDIDATE), flip=**no**. GOLD file = `src/cfnlint/rules/resources/codepipeline/PipelineArtifactNames.py` (E3701; gold changes `_output_artifact_names` from a flat `list` to a `dict` keyed by the pipeline logical name `validator.context.path.path[1]`, plus a `if not isinstance(resource_name, str): return` guard).

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT** — this is a clean **self-localization** case. GT MISLOCALIZED (L1 confidence=medium ranked 6 candidates — `update_schemas_manually.py`, `runner.py`, `conditions/Configuration.py`, `_rule.py`, `mappings/Configuration.py`, `template.py` — **none is the gold**). The agent ignored GT and ran `grep -r "E3701"` (the rule code is printed in the issue) at EVENT 6, landing on the gold file in ONE action. The agent then reasoned correctly and wrote a fix functionally equivalent to gold (dict keyed by `path[1]`). GT's only correct contact was a post-view `[GT]` header on the gold file AFTER the agent had already opened it (post-localization, inert). **Bad probe** (issue pre-localizes the gold via the E3701 code). GT delivered, did not misdirect, but did not cause anything.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-4002.json` + `gt/*_certificate.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 75.35426507` (deterministic_edges `2712.0` / calls_edges `3599.0`) | GREEN (`gate_resolution.pass=true`) | telemetry-only; reaches the agent only as the brief's resolved-edge lines (e.g. L1 `resolved caller: main() in scripts/update_schemas_format.py:468`) |
| **P1** name_match | `name_match_edges = 887` (24.65% of calls) | GREEN (non-dominant) | telemetry-only; only resolved (non-name_match) edges become brief "Callers:"/"Calls:" lines |
| **P1** typing tiers | `typing_fired=true`; `type_flow=244`, `impl_method=268`, `inherited=141`, `return_type=68` | GREEN | telemetry-only |
| **P2** calls_edges / breakdown | `calls_edges=3599`; `name_match=887, import=650, same_file=622, verified_unique=561, impl_method=268, type_flow=244, lsp=157, inherited=141, return_type=68, unique_method=1` | GREEN | telemetry-only; surfaces only as concrete call/caller lines in L1 + post-view `[GT]` headers |
| **P2** LSP enrichment | `lsp_certificate`: `server_launched=true`, `warm_probe_ok=true`, `attempted=213`, `verified=42`, `corrected=115`, `deleted=0`, `failed=45`, `residual=141`, `probe_latency_ms=1.39093399`, verdict `LSP_ACTIVE_VALID` | GREEN (`gate_lsp.pass=true`) | telemetry-only; the 157 `lsp` edges fold into resolved-edge lines |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN (`gate_embedder.present.pass=true`) | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=132/141`, `upstream_semantic_nonzero=342`, mode `present_and_consumption` | GREEN | telemetry-only; W_SEM only re-orders the L1 candidate list |

**Prereqs verdict:** gate-deep `verdict = {resolution_jarvis:true, lsp_enrichment:true, embedder:true, all_on:true}` — ALL GREEN. **Caveat (provenance):** `graph_certificate.json` verdict = `GRAPH_FAIL_MISSING_HANDOFF` (`hook_graph_hash=null`, `host_resolved_graph_db=""`) — the gate-deep graph at `/gt_artifacts/graph.db` could not be confirmed identical to the runtime-hook graph. The brief WAS delivered (below), so the runtime had a working graph; record the handoff-provenance gap but it did not void delivery. The substrate was healthy; it simply pointed at the wrong files (a ranking/recall problem, not a substrate failure).

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="medium">` / `Candidate edit targets (reason over these):` / `  1. scripts/update_schemas_manually.py — configure_logging, build_resource_type_patches, build_patches` / `  2. src/cfnlint/runner.py` / `  3. src/cfnlint/rules/conditions/Configuration.py` / `  4. src/cfnlint/rules/_rule.py` / `  5. src/cfnlint/rules/mappings/Configuration.py` / `  6. src/cfnlint/template/template.py` (the gold `PipelineArtifactNames.py` is **absent** from all 6) | EVENT 4 `[thought] I'll start by understanding the issue and exploring the codebase systematically.` → EVENT 5 lists the tree → EVENT 6 `grep -r "E3701" .../src/cfnlint/ --include="*.py" -l` → EVENT 7 obs: `.../codepipeline/PipelineArtifactNames.py` (agent self-localizes to GOLD, ignoring all 6 GT candidates). | **D**=Y · **C**=N (gold not in the list; 6/6 candidates non-gold) · **C**=N (agent did not open any GT candidate; used its own grep) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. L1 delivered but MISLOCALIZED. The agent self-localized via the E3701 code in the issue. L1 was inert (neither helped nor harmed).

## L3b post-view (`[GT]` file-view enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 9 | Prepended to the gold-file `cat -n` view: `[GT] PipelineArtifactNames:` / `[CATCHES] except Unsatisfiable -> handles | [CATCHES] except Unsatisfiable -> handles` | EVENT 10 `[thought] Now I can see the issue! The PipelineArtifactNames class has a _output_artifact_names list that is shared across ALL pipeline resources…` — the agent's reasoning derives entirely from the file BODY (its own `cat -n`), not the thin `[CATCHES]` header. | **D**=Y · **C**=Y (the `[CATCHES]` claim is real; no fabrication, no leak) · **C**=N (agent reasoned from the file body; the GT header added nothing and post-dates self-localization) |
| EVENT 15 | On viewing `CfnLintKeyword.py`: `[GT] CfnLintKeyword:` / `Called by: …PipelineFirstStageHasSource.py::validate,_is_first_stage (1x), …SnapStartSupported.py:27 super().__init__(...), …iam/Permissions.py:28 super().__init__, …` | EVENT 16-17: agent greps `context.path` in the codepipeline dir; the caller list neither redirected nor was cited. | **D**=Y · **C**=Y (callers are real subclasses of CfnLintKeyword) · **C**=N (not acted on) |
| EVENT 83 | On the final post-edit view of the gold file: `[GT] PipelineArtifactNames:` / `[CATCHES] except Unsatisfiable -> handles | …` (same header) | EVENT 84-91: agent runs `gt_validate`, then `git diff`; the header was not used. | **D**=Y · **C**=Y · **C**=N |

**L3b verdict:** D/C/C = **Y/Y/N**, leak=0. Post-view delivered correct, non-fabricated headers on the gold file, but every firing was post-localization and inert (the agent's reasoning came from the file body it had already fetched).

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | No `<gt-scope>` consensus block appears anywhere in `output.jsonl` for this task. | n/a | **DELIVERED=NO** — consensus did not fire (no agent-visible `<gt-scope>` bytes). |

**consensus verdict:** DELIVERED=NO.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 87 | `# gt_validate: src/cfnlint/rules/resources/codepipeline/PipelineArtifactNames.py` / `CALLER-BLIND-EDIT    symbol=validate  callers=5  (no test file edited alongside this change)` / `# (total flags: 1 — informational; the pre-finish gate is authoritative)` (the agent itself invoked `gt_validate`) | EVENT 88 `[thought] The validation tool flags that validate has 5 callers and no test file was edited alongside. But the user said "I've already taken care of all changes to any of the test files…" So I should not modify test files. The tests already pass…` — agent read it, correctly declined to edit tests. | **D**=Y · **C**=Y (5 callers is real; informational; **no test name / FAIL_TO_PASS leaked**) · **C**=Y (agent reasoned about it) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. The caller-blind-edit advisory was correct and correctly informational; it did not leak the grader test and did not misdirect.

## L4 / L5 / L5b / L6

| layer | status |
|---|---|
| L4 (orientation) | The `<gt-orientation>` block was delivered in the EVENT 1 prepend (`Issue references: lint() in api.py (3 callers), main() in update_schemas_manually.py …; Related (by graph): match() in UniqueNames.py`). CORRECT-ish but non-gold; not consumed. D=Y/C=partial/C=N. |
| L5 / L5b / L6 (governor/turn-level) | `gt_run_summary` lists them active (telemetry). No agent-visible governor nudge/redirect text in `output.jsonl` changed the trajectory; the agent ran its own loop. DELIVERED-to-agent=NO observable effect. |

**L4/L5/L5b/L6 verdict:** no agent-observable consumption.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** across ALL components (verified by chronological read of the full 93-event `output.jsonl`). The FAIL_TO_PASS tests (`test_pipeline_artifact_names.py::test_validate[instances2/3]`) were NEVER surfaced by GT.
- **Consumed-count = 0** of GT's localization/evidence drove a decision. The only GT block the agent reasoned *about* (the `gt_validate` caller-blind advisory) was agent-invoked and changed nothing.
- **Fair-probe = NO (BAD PROBE).** The issue prints the rule code `E3701`; `grep E3701` lands on the gold rule file in one action. GT cannot earn causal credit on this task even if it had ranked correctly.

## Failure locus (why it did not resolve)
Post-localization **implementation-correctness edge case**, NOT a GT/localization failure. The agent's patch is functionally equivalent to gold (dict keyed by `path[1]`) BUT omits the gold's `if not isinstance(resource_name, str): return` guard. The gold `test_patch` adds FAIL_TO_PASS cases (`instances2`, `instances3`) that exercise non-string / `Fn::If`-shaped path keys; without the isinstance guard the agent's `path[1]` can be a non-string (int) → `setdefault` on an unhashable/divergent key path differs from gold behavior. The agent ran only the 5 PRE-EXISTING unit tests (all passed) — the gold's new FAIL_TO_PASS cases were absent from its environment, so it had false confidence.

**Environment caveat (run-level, see cfn-lint-4023 ledger):** the agent ran tests under the OpenHands poetry env (`python3.12.13`, pytest freshly `pip install`ed), NOT the repo's prepared `cpython-39` env. The agent/eval env mismatch is a run-wide reliability concern affecting all 28 cfn-lint tasks (0 resolved).

**right_trajectory (GT) = FALSE** (localization wrong + not consumed). gt_caused = FALSE. flip = no.
