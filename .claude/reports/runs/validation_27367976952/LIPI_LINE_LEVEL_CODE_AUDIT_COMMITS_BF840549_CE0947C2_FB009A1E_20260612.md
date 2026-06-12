# LIPI line-level code audit - commits bf840549 / ce0947c2 / fb009a1e

Date: 2026-06-12

Scope: code written in these behavior commits:

- `bf840549` - `fix(runtime): productize mini-swe control plane`
- `ce0947c2` - `fix(truth): propagate runtime control in task truth`
- `fb009a1e` - `fix(runtime): prove five-language controller behavior`

Method: `LIPI.md`.

Important correction: earlier checkpoint LIPI was too weak. It audited broad boundaries,
not each concrete symptom and changed code path. This document audits every changed code
hunk I wrote as an atomic behavior unit. Consecutive lines that implement one behavior are
grouped together; each group checks Logic, Implementation, Integration, and Plumbing.

## Benchmark Readiness

Not ready.

`CLAUDE.md` says Stage 1 must be deterministic, stable, architecture-adherent, and
general before Stage 2 benchmarking/flips. The mini-swe runtime parity code is now much
better covered, but `P0-01` remains live: rebuilt substrate + pinned digest + real Go/Rust
proof has not passed. Running tenpack now would skip Stage 1.

## Symptom Set

### Symptom A - product runtime behavior lived in adapter-only code

Exact failure/risk: `artifact_deepswe` owned phase policy, budget, graph-to-action,
verification horizon, and obligation behavior. That violated the desired architecture:
`src/groundtruth` owns GT product identity; `artifact_deepswe` is only mini-swe adapter
glue.

### Symptom B - task truth did not carry runtime-control truth

Exact failure/risk: `task_truth.json` could be authoritative for outcome/substrate while
runtime-control state still lived in deep metrics, adapter globals, or tests. That left
B5/B9 partially open: product truth existed, but did not contain the full product state.

### Symptom C - five-language controller proof exposed a real event classifier bug

Exact failure: `tests/test_runtime_five_language_fixtures.py` initially failed 5/5
because `trajectory_state.command_event()` misclassified write commands as views. The
substring `rg` inside `targetedBehavior` matched the old read-command regex.

## Commit `bf840549` - productize mini-swe control plane

### A1. `src/groundtruth/runtime/context_policy.py:17-164`

Changed code:

- `Phase`, `Event`, `PayloadKind`
- `PHASE_POLICY`
- `EVENT_BOUND_PAYLOADS`
- `PolicyDecision`
- `normalize_kind`, `allowed_payloads`, `phase_allows`, `should_emit`

```text
Symptom:   Product allow/deny policy existed as mini-swe adapter logic, so GT could not
           inspect one product policy surface.

Logic:           Checked phase model against desired ORIENT/VIEW/EDIT/VERIFY/SUBMIT.
                 Clean. The table encodes phase-local payloads and event-bound bypasses.
Implementation:  Checked enum values and `verify.horizon.*` prefix handling.
                 Clean for this slice. `should_emit` returns a reason for every deny.
Integration:     Checked adapter import path. Clean: artifact_deepswe/phase_policy.py
                 imports this module; gt_mini_patch uses `should_emit`.
Plumbing:        Checked if deny reason survives. Partial: reason is returned but not yet
                 stored in a run ledger for every suppression.

Root cause:  Integration - product policy was not centralized.
Fix:         Add product-owned policy table and decision helpers.
Re-checked:  Logic clean, implementation clean, integration clean for mini-swe, plumbing
             partial because suppression reasons are not fully persisted.
Verdict:     CLOSED for live mini-swe allow/deny behavior; PARTIAL for suppression telemetry.
```

### A2. `src/groundtruth/runtime/trajectory_state.py:10-165` as introduced in `bf840549`

Changed code:

- source extension list
- `Turn`, `TrajectoryState`
- regexes for files, GT markers, test runners, pass/fail/env failures
- `is_source_path`, `extract_files`, `command_event`
- `update_state`, `derive_state`, `derive_phase`, `detect_events`

```text
Symptom:   Phase was inferred from adapter globals, not a product trajectory-state object.

Logic:           Checked paper path: no edits => ORIENT/VIEW; edited => EDIT; tests/nonedit
                 streak => VERIFY; budget > 90% => SUBMIT. Clean for intended first slice.
Implementation:  Broken initially. `command_event` classified read commands before write
                 commands and substring-matched `rg` inside identifiers. Fixed later in
                 `fb009a1e`.
Integration:     Clean after fix. gt_mini_patch._detect_phase delegates here.
Plumbing:        Clean for task_truth: task_truth derives trajectory_state_summary from this
                 module after `ce0947c2`.

Root cause:  Implementation - command classification regex was too broad.
Fix:         `fb009a1e` reorders write detection before read detection and adds boundaries.
Re-checked:  Five-language receipt passes; task_truth summary still consumes same module.
Verdict:     CLOSED after `fb009a1e`.
```

### A3. `src/groundtruth/runtime/context_budget.py:1-82`

Changed code:

- `BudgetResult`
- `ContextBudgeter`
- `stable_fact_id`
- imperative-first, fact-second, other-last trim policy

```text
Symptom:   Payload budget/dedupe lived in gt_mini_patch and was not reusable product behavior.

Logic:           Checked desired state: small payloads, dedupe repeated facts, prefer next
                 action. Clean at current line-based policy level.
Implementation:  Clean. Stable fact IDs and delivered text sets are updated only for emitted
                 lines. Empty payload returns explicit metadata.
Integration:     Clean. gt_mini_patch._budget_trim delegates to ContextBudgeter.
Plumbing:        Clean after test reset fix. tests/test_verified_adapter.py clears both
                 `_DELIVERED_FACTS` and `_DELIVERED_FACT_IDS`.

Root cause:  Integration/plumbing - budget state was adapter-local and had two dedupe stores.
Fix:         Product-owned budgeter with shared sets passed from adapter.
Re-checked:  Adapter verified receipt caught and fixed the stable fact-id reset gap.
Verdict:     CLOSED for current mini-swe path.
```

### A4. `src/groundtruth/runtime/action_translation.py:1-36`

Changed code:

- `ACTION_TEMPLATES`
- `translate_to_action`

```text
Symptom:   Graph facts were delivered as facts, not consistently translated into next actions.

Logic:           Checked if witness/caller/sibling evidence maps to action language. Clean
                 for current evidence markers.
Implementation:  Clean for supported markers. Unknown lines are preserved.
Integration:     Clean. gt_mini_patch._translate_to_action delegates here.
Plumbing:        Clean for emitted text. No extra serialization boundary.

Root cause:  Logic/product gap - graph facts were not action-oriented enough.
Fix:         Add product-owned graph-to-action helper.
Re-checked:  Action-template and five-language receipts pass.
Verdict:     PARTIAL. Current markers covered; richer caller/callee risk reasoning still open.
```

### A5. `src/groundtruth/runtime/verification_horizon.py:1-94`

Changed code:

- `HorizonThresholds`
- `composite_severity`
- `verify_horizon_band`
- `render_verify_emission`

```text
Symptom:   Verification horizon semantics and wording lived inside the adapter, risking
           exact-test leakage and hard-gate overclaim.

Logic:           Clean. Bands are advisory/urgent/gate/pivot interventions, not hard blocks.
Implementation:  Clean. Rendering uses behavior-level target language and does not render
                 exact test function names.
Integration:     Clean. gt_mini_patch delegates severity, banding, and rendering here.
Plumbing:        Clean for agent-visible text; task_truth records hard_enforced separately.

Root cause:  Integration/logic - horizon behavior was adapter-owned and semantically easy to
             overclaim.
Fix:         Product-owned horizon functions and leak-free rendering.
Re-checked:  Stage A/B/C horizon receipts and five-language no-leak fixture pass.
Verdict:     CLOSED for intervention semantics. Hard submit gate remains intentionally absent.
```

### A6. `src/groundtruth/runtime/obligations.py:1-240`

Changed code:

- `ObligationLifecycle`
- compatibility statuses
- `ObligationRecord`
- `obligation_tested`, `overlap`, `obligation_statuses`
- status hash/order helpers
- `ObligationTracker`
- `render_obligation_status_block`

```text
Symptom:   Obligation lifecycle was fragmented between live runtime, oracle, and replay/truth.

Logic:           Clean for lifecycle vocabulary: unseen, edited, tested, satisfied,
                 contradicted, unverified.
Implementation:  Mostly clean. Deterministic token overlap is simple but faithful to current
                 available signals. Rendering suppresses exact test names.
Integration:     Clean. gt_oracle delegates constants, tracker wrapper, status hash/order,
                 and renderer to this module.
Plumbing:        Partial. task_truth summarizes lifecycle counts when obligation_status.json
                 exists, but richer per-obligation evidence is only as good as the source file.

Root cause:  Integration/plumbing - multiple obligation models could diverge.
Fix:         Product-owned lifecycle and adapter delegation.
Re-checked:  Obligation tracker and task_truth runtime-control receipts pass.
Verdict:     CLOSED for shared lifecycle vocabulary; PARTIAL for full evidence richness.
```

### A7. `artifact_deepswe/phase_policy.py:1-26`

Changed code:

- Replaced local enum/policy table with imports from `groundtruth.runtime.context_policy`.

```text
Symptom:   Adapter had its own phase policy, creating version skew with product architecture.

Logic:           Clean. Adapter should not own policy.
Implementation:  Clean. Shim exports the same public names used by existing imports.
Integration:     Clean. Existing tests import phase_policy and compare patch import table.
Plumbing:        Clean. No serialized data; pure import boundary.

Root cause:  Integration - duplicated policy table.
Fix:         Shim to product policy.
Re-checked:  tests/test_phase_policy_module.py passes.
Verdict:     CLOSED.
```

### A8. `artifact_deepswe/gt_agent.py:128,153-164,447-478`

Changed code:

- `_PRODUCT_RUNTIME_DIR`
- `_PRODUCT_RUNTIME_FILES`
- install steps that create `/opt/gt/groundtruth/runtime` and write product runtime files

```text
Symptom:   Product modules could import locally but fail inside mini-swe task containers
           unless injected beside gt_mini_patch.

Logic:           Clean. Adapter owns container injection; product owns behavior.
Implementation:  Clean for explicit allowlist. The exact runtime files are loaded and base64
                 chunked like existing injected files.
Integration:     Clean. /opt/gt is already inserted into sys.path by the patch loader.
Plumbing:        Clean for current file list. Future runtime files must be added to the list.

Root cause:  Plumbing - product code would not cross the container boundary.
Fix:         Inject product runtime package files into /opt/gt/groundtruth/runtime.
Re-checked:  py_compile and adapter fail-closed/verified receipts pass.
Verdict:     CLOSED with maintenance note: keep injection allowlist updated.
```

### A9. `artifact_deepswe/gt_mini_patch.py:44-53`

Changed code:

- Imports product action translation, budgeter, trajectory state, and horizon functions.

```text
Symptom:   gt_mini_patch could not delegate product behavior without importing product modules.

Logic:           Clean. Imports are dependencies for adapter delegation.
Implementation:  Clean after removing unused `_ProductEvent`.
Integration:     Clean when paired with gt_agent injection.
Plumbing:        Clean. Import path supplied by /opt/gt injection.

Root cause:  Integration - adapter needed product module references.
Fix:         Add product runtime imports.
Re-checked:  py_compile and adapter receipts pass.
Verdict:     CLOSED.
```

### A10. `artifact_deepswe/gt_mini_patch.py:494`

Changed code:

- `composite_severity` returns `_product_composite_severity(...)`.

```text
Symptom:   Severity formula could diverge between adapter and product horizon semantics.

Logic:           Clean. One formula should own severity.
Implementation:  Clean. Direct return preserves same signature.
Integration:     Clean. Existing callers still call `composite_severity`.
Plumbing:        Clean. No data movement.

Root cause:  Integration - duplicated formula.
Fix:         Delegate to product verification_horizon.
Re-checked:  Horizon receipts pass.
Verdict:     CLOSED.
```

### A11. `artifact_deepswe/gt_mini_patch.py:2440-2469`

Changed code:

- Imports product-backed phase policy shim.
- `_detect_phase` builds `TrajectoryState` and calls product `derive_phase`.
- `_current_event` maps adapter event kind to product `Event`.

```text
Symptom:   Phase detection and event mapping were adapter-local heuristics.

Logic:           Clean. Adapter converts observed mini-swe globals into product state.
Implementation:  Clean for current globals. `_current_event` handles post_view, post_edit,
                 review transition, and submit.
Integration:     Clean. Product `Phase`/`Event` are used by later `should_emit`.
Plumbing:        Clean for in-process data. No serialization boundary.

Root cause:  Integration - adapter heuristics instead of product controller.
Fix:         Build product TrajectoryState and delegate phase derivation.
Re-checked:  phase detection and five-language fixtures pass.
Verdict:     CLOSED for mini-swe phase detection.
```

### A12. `artifact_deepswe/gt_mini_patch.py:2550-2587`

Changed code:

- `_translate_to_action` delegates to product helper.
- `_PRODUCT_BUDGETER` shares delivered fact sets.
- `_budget_trim` delegates to product ContextBudgeter and records metadata.

```text
Symptom:   Action wording and budget/dedupe were adapter-owned.

Logic:           Clean. These are product payload semantics.
Implementation:  Mostly clean. Live path delegates immediately; old unreachable code remains
                 below the return in the file and should be mechanically removed later.
Integration:     Clean. Shared delivered sets preserve adapter state while product owns logic.
Plumbing:        Clean after stable fact-id reset receipt.

Root cause:  Integration with minor implementation debt - product behavior duplicated in adapter.
Fix:         Delegate live path to product modules.
Re-checked:  context budget, action template, verified adapter receipts pass.
Verdict:     PARTIAL because unreachable legacy code remains; live behavior is CLOSED.
```

### A13. `artifact_deepswe/gt_mini_patch.py:2852-2924`

Changed code:

- `verify_horizon_band` delegates to product banding with env-derived thresholds.
- `_render_verify_emission` delegates to product renderer.

```text
Symptom:   Verification guidance was adapter-local and could leak exact tests or overclaim gates.

Logic:           Clean. Existing env calibration is passed into product thresholds.
Implementation:  Partial cleanup. Live path delegates, but unreachable legacy render body remains
                 below early return.
Integration:     Clean. Existing callers keep same function names.
Plumbing:        Clean. Rendered text returns to same observation append path.

Root cause:  Integration plus implementation debt - old adapter-owned render code.
Fix:         Delegate banding/rendering to product module.
Re-checked:  verification horizon receipts pass and no-leak fixture passes.
Verdict:     PARTIAL due to dead legacy code; live semantics CLOSED.
```

### A14. `artifact_deepswe/gt_mini_patch.py:3484-3486`

Changed code:

- Candidate filter uses `_phase_should_emit(kind, phase, event, event_bound).allowed`.

```text
Symptom:   Event-bound candidate bypass/allow rules were hidden inside adapter filtering.

Logic:           Clean. Product policy decides allowed/denied; event-bound still explicit.
Implementation:  Clean. Empty candidates still filtered by `c[2]`.
Integration:     Clean. Uses product `Event` from `_current_event`.
Plumbing:        Partial. Deny reasons are not yet logged for all suppressions.

Root cause:  Integration - adapter-owned allow/deny.
Fix:         Use product policy decision.
Re-checked:  oracle review-transition receipts pass.
Verdict:     CLOSED for allow/deny behavior; PARTIAL for suppression observability.
```

### A15. `artifact_deepswe/gt_oracle.py:852-858`

Changed code:

- Product obligation constants/functions replace local names.
- Compatibility `ObligationTracker` subclasses product tracker.

```text
Symptom:   Live oracle obligation status could diverge from product obligation lifecycle.

Logic:           Clean. Live and truth should share vocabulary.
Implementation:  Clean. Compatibility wrapper converts raw dict obligations through existing
                 `_obligation_views`.
Integration:     Clean. Existing callers keep old names.
Plumbing:        Clean for in-process status/render path.

Root cause:  Integration - duplicate obligation model.
Fix:         Delegate obligation status/tracker/rendering to product module.
Re-checked:  obligation tracker and oracle receipts pass.
Verdict:     CLOSED.
```

### A16. Test code in `bf840549`

Changed code:

- `tests/test_phase_detection.py`
- `tests/test_phase_policy_module.py`
- `tests/test_runtime_product_controller.py`
- `tests/test_verified_adapter.py`

```text
Symptom:   There was no deterministic receipt that adapter shims matched product policy,
           runtime controller behavior, or stable fact-id reset.

Logic:           Clean. Tests assert architecture behavior, not benchmark outcome.
Implementation:  Clean after updating old ORIENT expectation from consensus.scope to
                 brief/orientation.
Integration:     Clean. Tests import real adapter modules by path.
Plumbing:        Clean. Verified adapter reset clears both delivered text and stable fact IDs.

Root cause:  Plumbing/receipt gap - behavior could regress silently.
Fix:         Add/update deterministic receipts.
Re-checked:  175 focused receipts pass.
Verdict:     CLOSED as receipts, not product proof of P0-01.
```

## Commit `ce0947c2` - propagate runtime control in task truth

### B1. `scripts/swebench/task_truth.py:21-28`

Changed code:

- Adds `src` to `sys.path`.
- Imports product runtime versions and trajectory helpers.

```text
Symptom:   task_truth could not use product runtime modules, so it could not own runtime
           control truth.

Logic:           Clean. task_truth is a reporting script, so adding repo `src` path is the
                 correct local import bridge.
Implementation:  Clean. `_SRC` is computed relative to scripts/swebench.
Integration:     Clean for local/GHA repo layout.
Plumbing:        Clean. Import path survives script execution.

Root cause:  Plumbing - product modules were not importable from script context.
Fix:         Add repo src path and imports.
Re-checked:  task_truth tests pass.
Verdict:     CLOSED.
```

### B2. `scripts/swebench/task_truth.py:94-139`

Changed code:

- `_first_str`
- `_turns_from_mini_trajectory`

```text
Symptom:   task_truth needed trajectory state but mini-swe trajectories have multiple possible
           command/observation shapes.

Logic:           Clean. Extract command and observation conservatively from known keys.
Implementation:  Partial. Best-effort parser covers `messages`, `trajectory`, and `steps`,
                 plus action/result dicts. It does not claim complete trajectory normalization.
Integration:     Clean. Output is product `Turn` objects for `derive_state`.
Plumbing:        Clean for existing mini trajectory fixtures.

Root cause:  Plumbing - raw trajectory shape needed conversion before product state derivation.
Fix:         Add deterministic mini trajectory to Turn adapter.
Re-checked:  task_truth runtime-control receipt passes.
Verdict:     PARTIAL because parser is best-effort; adequate for current pier/mini shapes.
```

### B3. `scripts/swebench/task_truth.py:142-166`

Changed code:

- `_trajectory_state_summary`

```text
Symptom:   task_truth did not expose phase/action/edit/test/delivered marker state.

Logic:           Clean. Summary is derived from product `derive_state` and `derive_phase`.
Implementation:  Clean. Handles invalid GT_STEP_LIMIT without crashing.
Integration:     Clean. Uses artifact resolver's mini trajectory path.
Plumbing:        Clean. Serialized into task_truth under `trajectory_state` and
                 `runtime_control.trajectory_state_summary`.

Root cause:  Plumbing - trajectory state was not carried to final truth.
Fix:         Add runtime trajectory summary.
Re-checked:  task_truth and deep-metrics authority receipts pass.
Verdict:     CLOSED.
```

### B4. `scripts/swebench/task_truth.py:170-194`

Changed code:

- `_obligation_lifecycle_summary`
- `_consumption_summary`

```text
Symptom:   task_truth did not summarize obligation lifecycle or consumption state.

Logic:           Clean. Product truth should expose counts and delivery/consumption fields.
Implementation:  Partial. Obligation summary counts statuses but does not reconstruct full
                 per-obligation evidence if source file is absent.
Integration:     Clean. Reads existing obligation_status/deep metrics blocks.
Plumbing:        Partial. If `obligation_status.json` is not produced, summary correctly says
                 source_present=false but cannot fill lifecycle.

Root cause:  Plumbing - existing obligation data was not carried into one truth surface.
Fix:         Add summaries with explicit source presence.
Re-checked:  task_truth receipt passes.
Verdict:     PARTIAL until live artifacts consistently produce obligation_status.json.
```

### B5. `scripts/swebench/task_truth.py:198-209`

Changed code:

- `_verification_horizon_summary`

```text
Symptom:   Truth surfaces could confuse intervention, hard enforcement, self retry, and
           official verifier repair.

Logic:           Clean. The code sets pre_submit_intervention=true but hard_enforced and
                 official_verifier_repair from explicit semantics only.
Implementation:  Clean for current semantics.
Integration:     Clean. Uses deep metrics enforcement semantics and verifier_semantics.
Plumbing:        Clean. Serialized into runtime_control.

Root cause:  Logic/plumbing - truth did not explicitly separate enforcement meanings.
Fix:         Add horizon summary with distinct booleans.
Re-checked:  task_truth receipt checks official_verifier_repair=false.
Verdict:     CLOSED.
```

### B6. `scripts/swebench/task_truth.py:328-353`

Changed code:

- Computes `trajectory_summary`, `obligation_summary`, `consumption_summary`,
  `horizon_summary`.
- Adds `trajectory_state` and `runtime_control` to returned task_truth.

```text
Symptom:   Product truth had outcome/substrate but not runtime-control state.

Logic:           Clean. task_truth is the final product surface.
Implementation:  Clean. Summaries are computed once and embedded.
Integration:     Clean. deep metrics forwards runtime_control in B8.
Plumbing:        Clean for generated task_truth; historical artifacts need regeneration.

Root cause:  Plumbing - runtime-control data was not serialized into task_truth.
Fix:         Add task_truth runtime_control block.
Re-checked:  task_truth and deep metrics receipts pass.
Verdict:     CLOSED for new artifacts; historical artifacts remain stale by definition.
```

### B7. `scripts/metrics/compute_paired_metrics.py:929`

Changed code:

- `if outcome.get("resolved") is not None:` instead of checking key presence.

```text
Symptom:   `resolved: null` in task_truth could be coerced to False by `bool(None)`.

Logic:           Clean. Unknown/null is not failure.
Implementation:  Clean. Non-null resolved is authoritative; otherwise fallback uses
                 failure_class/raw outcome.
Integration:     Clean. Paired metrics now respects task_truth authority without false failure.
Plumbing:        Clean. No serialization change.

Root cause:  Implementation - silent type coercion of null to false.
Fix:         Check non-null before bool conversion.
Re-checked:  baseline guard and scorecard receipts pass.
Verdict:     CLOSED.
```

### B8. `scripts/swebench/gt_deep_metrics.py:1117,1188-1189`

Changed code:

- Same non-null resolved check.
- Adds `task_truth_path` and `runtime_control` to deep metrics output.

```text
Symptom:   deep metrics preferred task_truth for outcome but did not expose task_truth runtime
           control; also had same null->False risk.

Logic:           Clean. Consumers should see task_truth path and runtime_control source.
Implementation:  Clean. Adds fields only when truth_data exists.
Integration:     Clean. Downstream can inspect runtime_control from deep metrics.
Plumbing:        Clean. The data is carried end-to-end into JSON.

Root cause:  Implementation/plumbing - null coercion and dropped runtime_control.
Fix:         Non-null check plus forwarded fields.
Re-checked:  tests/test_gt_deep_metrics_task_truth.py passes.
Verdict:     CLOSED.
```

### B9. Test code in `ce0947c2`

Changed code:

- `tests/test_task_truth.py`
- `tests/test_gt_deep_metrics_task_truth.py`

```text
Symptom:   No receipt proved task_truth carried runtime_control or deep metrics forwarded it.

Logic:           Clean. Tests assert product truth fields, not benchmark outcome.
Implementation:  Clean.
Integration:     Clean. Tests build real temporary artifact layouts.
Plumbing:        Clean. JSON is written/read through real code paths.

Root cause:  Receipt gap.
Fix:         Add task_truth runtime-control and deep metrics forwarding assertions.
Re-checked:  Receipts pass.
Verdict:     CLOSED as deterministic receipt.
```

## Commit `fb009a1e` - five-language controller behavior

### C1. `src/groundtruth/runtime/trajectory_state.py:56-61`

Changed code:

- `_TEST_PASS_RE` expanded to multiline and includes `BUILD SUCCESS`.

```text
Symptom:   Java/Maven fixture had `mvn test` with `BUILD SUCCESS`, but product controller
           did not count it as test evidence.

Logic:           Clean. Maven success is observed passing test evidence.
Implementation:  Clean. Adds a pass token without changing fail regex.
Integration:     Clean. derive_state sees Event.TEST_RESULT from `mvn ... test`.
Plumbing:        Clean. task_truth summaries will now mark test_evidence_seen when applicable.

Root cause:  Implementation - pass evidence regex missed Maven success wording.
Fix:         Add BUILD SUCCESS to pass regex.
Re-checked:  five-language fixture passes.
Verdict:     CLOSED.
```

### C2. `src/groundtruth/runtime/trajectory_state.py:77-82`

Changed code:

- Mutating command detection now runs before read detection.
- Read command regex uses command boundary/prefix.
- Shell redirection detection is separated.

```text
Symptom:   `targetedBehavior` contains `rg`, so old read regex matched inside the symbol and
           classified `python -c open(...write...)` as POST_VIEW instead of POST_EDIT.

Logic:           Clean. Mutations must be recognized before reads; read commands should match
                 commands, not substrings inside identifiers.
Implementation:  Clean for observed command shapes. Uses word/prefix boundaries.
Integration:     Clean. gt_mini_patch delegates phase detection here; task_truth summaries
                 use the same product controller.
Plumbing:        Clean. Edited files now travel to TrajectoryState.edited_files and
                 source_edit_count.

Root cause:  Implementation - over-broad regex and wrong branch order.
Fix:         Reorder and bound command_event matching.
Re-checked:  Initial 5/5 fixture failure became 15/15 pass; focused 175 receipts pass.
Verdict:     CLOSED.
```

### C3. `tests/test_runtime_five_language_fixtures.py:1-102`

Changed code:

- Five language fixtures for Python, Go, Rust, TypeScript, Java.
- Assertions for state, phase, policy, no-leak horizon, budget/dedupe, action translation,
  obligation lifecycle.

```text
Symptom:   Deterministic product proof did not cover five language families without benchmark
           execution.

Logic:           Clean. These are Stage 1 receipts, not Stage 2 benchmark claims.
Implementation:  Clean. Fixtures intentionally use generic command shapes and no task IDs.
Integration:     Clean. Tests call product modules directly, matching the mini-swe delegated path.
Plumbing:        Clean. The test caught real data movement failure into edited_files.

Root cause:  Receipt gap, which exposed an implementation bug.
Fix:         Add five-language deterministic receipts and fix controller bug.
Re-checked:  175 focused receipts pass.
Verdict:     CLOSED as deterministic Stage 1 receipt for runtime controller behavior.
```

## Overall Re-check After Fixes

```text
Symptom:   Need to know whether code changed in bf840549/ce0947c2/fb009a1e is truly LIPI
           covered and whether benchmarking is allowed.

Logic:           PARTIAL overall. Runtime controller/control-plane logic is now substantially
                 aligned; action_translation remains marker-template based, not a full graph
                 risk reasoner.
Implementation:  PARTIAL overall. Live behavior delegates correctly; gt_mini_patch still has
                 unreachable legacy code below early returns.
Integration:     PARTIAL overall. mini-swe integration delegates product behavior; other GT
                 delivery surfaces still need separate adoption audit.
Plumbing:        PARTIAL overall. task_truth now carries runtime_control, but live regenerated
                 artifacts are blocked until P0-01 substrate proof passes.

Root cause:  Remaining blocker is not these runtime commits. It is P0-01 live substrate proof:
             rebuilt image + pinned digest + real Go/Rust proof not yet green.
Fix:         Rebuild/pin/re-proof substrate before benchmark.
Re-checked:  Deterministic receipts pass; live proof not yet rerun.
```

## Next Steps

1. Do not run tenpack yet.
2. Clean up unreachable legacy branches in `artifact_deepswe/gt_mini_patch.py` only after
   guarding with the existing receipts.
3. Rebuild `gt-substrate`.
4. Pin `GT_SUBSTRATE_DIGEST`.
5. Re-dispatch Go/Rust proof jobs from run `27387470440`.
6. Inspect proof artifacts:
   - `proof_progress.json`
   - `proof_failure.json`
   - `dep_store_manifest.json`
   - `lsp_certificate.json`
   - `reconciled_substrate_verdict.json`
   - `task_truth.json`
7. If P0-01 is green and regenerated artifacts agree, then run the first GT-on benchmark.
