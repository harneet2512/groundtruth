# LIPI code audit - runtime parity slices - 2026-06-12

Current git HEAD at audit time: `1966f041`

Latest behavior commit audited: `fb009a1e`

Goal source: `CLAUDE.md`

Core goal read from `CLAUDE.md`:

- Stage 1 comes before Stage 2.
- Stage 1 means deterministic, stable, architecture-adherent GT behavior.
- Correctness is judged by right trajectory and product behavior, not by a lucky
  `resolved` verdict.
- Benchmarks/flips are Stage 2 proof only after Stage 1 is stable.
- No GT-off baseline rerun.
- No task-specific or benchmark-shaped fixes.

## Benchmark Readiness Verdict

Not ready for true benchmarking.

Reason:

- The runtime mini-swe integration parity slices are now LIPI-covered and have
  deterministic receipts.
- Five-language controller behavior is now deterministic at product level.
- `task_truth.json` now carries runtime-control truth.
- But `P0-01` is still live: rebuilt substrate + pinned digest + real Go/Rust
  substrate re-proof has not passed.

Under `CLAUDE.md`, benchmarking before `P0-01` is green would skip Stage 1. That
would be benchmaxxing pressure, not product readiness.

## Code Boundary LIPI

### `src/groundtruth/runtime/context_policy.py`

```text
ID: RUNTIME-CONTEXT-POLICY
Desired state: One product-owned phase/event/payload policy controls when GT may speak.
Current state: Defines Phase, Event, PayloadKind, PHASE_POLICY, EVENT_BOUND_PAYLOADS,
allowed_payloads, phase_allows, and should_emit.
Logic: Clean. ORIENT/VIEW/EDIT/VERIFY/SUBMIT semantics are centralized. Event-bound
payloads are explicit, including review-transition scope completeness.
Implementation: Clean for the slice. Pure functions, inspectable table, no task IDs.
Integration: Clean for mini-swe. artifact_deepswe/phase_policy.py imports this module.
Plumbing: Covered by phase policy, phase detection, runtime controller, and adapter receipts.
Verdict: CLOSED for mini-swe runtime parity.
Remaining bug, if any: Full cross-surface policy adoption outside DeepSWE still needs separate audit.
Fix boundary: Expand callers to this module when other GT delivery surfaces are touched.
```

### `src/groundtruth/runtime/trajectory_state.py`

```text
ID: RUNTIME-TRAJECTORY-STATE
Desired state: Product-owned state controller derives phase from observed trajectory state,
not adapter-local heuristics.
Current state: Turn, TrajectoryState, command_event, update_state, derive_state,
derive_phase, and detect_events exist. Five-language fixtures cover view/edit/test sensing.
Logic: Clean after fix. Mutating commands are classified before read commands, so symbols
such as targetedBehavior no longer accidentally match `rg`.
Implementation: Mostly clean. General source extensions and standard test runners are used;
Java/Maven BUILD SUCCESS is recognized as test evidence.
Integration: Clean for mini-swe. gt_mini_patch._detect_phase delegates here.
Plumbing: Covered by runtime product controller, phase detection, five-language fixtures,
and task_truth runtime summaries.
Verdict: CLOSED for deterministic mini-swe controller behavior.
Remaining bug, if any: It does not yet parse every possible shell redirection/write idiom.
Fix boundary: Add general command-shape recognition here, not in artifact_deepswe.
```

### `src/groundtruth/runtime/context_budget.py`

```text
ID: RUNTIME-CONTEXT-BUDGET
Desired state: Product-owned payload budget trims, dedupes, and prioritizes action-oriented
facts consistently.
Current state: ContextBudgeter owns stable fact-id dedupe, imperative-first ordering, and
token/char budget metadata.
Logic: Clean. Delivery dedupe is product behavior, not adapter behavior.
Implementation: Clean for current payloads. No task-specific strings.
Integration: Clean for mini-swe. gt_mini_patch._budget_trim delegates here.
Plumbing: Covered by context budget and adapter verified receipts, including stable fact-id reset.
Verdict: CLOSED for current mini-swe delivery path.
Remaining bug, if any: Budget policy is still simple and line-based; richer payload schemas
could improve action density later.
Fix boundary: Extend ContextBudgeter, not per-adapter trimming.
```

### `src/groundtruth/runtime/action_translation.py`

```text
ID: RUNTIME-ACTION-TRANSLATION
Desired state: Graph facts become direct next-action guidance at the correct phase.
Current state: translate_to_action converts witnesses/callers/siblings into action wording
outside ORIENT/VIEW.
Logic: Clean for current evidence families.
Implementation: Partial product depth. It handles major existing markers but is not yet a
full graph-risk explainer.
Integration: Clean for mini-swe. gt_mini_patch._translate_to_action delegates here.
Plumbing: Covered by action template and runtime controller receipts.
Verdict: PARTIAL.
Remaining bug, if any: Graph-to-action translation is still marker-template based, not a
complete caller/callee risk reasoner.
Fix boundary: Enrich this module with graph risk categories; do not add action wording inside
artifact_deepswe.
```

### `src/groundtruth/runtime/verification_horizon.py`

```text
ID: RUNTIME-VERIFICATION-HORIZON
Desired state: Product-owned verification guidance is behavior-level, leak-free, and honest
about intervention versus hard enforcement.
Current state: HorizonThresholds, composite_severity, verify_horizon_band, and
render_verify_emission are shared product behavior.
Logic: Clean. advisory/urgent/gate/pivot are interventions; exact test names are not rendered.
Implementation: Clean for current horizon behavior.
Integration: Clean for mini-swe. gt_mini_patch delegates severity, banding, and rendering.
Plumbing: Covered by Stage A/B/C horizon receipts and five-language no-leak fixture.
Verdict: CLOSED for intervention semantics.
Remaining bug, if any: This is not a hard submit gate and must not be documented as one.
Fix boundary: A future hard gate must add real blocking semantics and set hard_enforced=true.
```

### `src/groundtruth/runtime/obligations.py`

```text
ID: RUNTIME-OBLIGATIONS
Desired state: Obligation lifecycle is first-class and shared by live runtime and post-run truth.
Current state: ObligationLifecycle, ObligationRecord, ObligationTracker, status vector helpers,
and leak-free obligation rendering live in product code.
Logic: Clean for lifecycle vocabulary: unseen, edited, tested, satisfied, contradicted,
unverified.
Implementation: Mostly clean. Status inference is token/symbol based and deterministic.
Integration: Clean for mini-swe. gt_oracle wraps/delegates to this product module.
Plumbing: Covered by obligation tracker, runtime controller, and task_truth runtime summary receipts.
Verdict: CLOSED for shared vocabulary and current live wiring.
Remaining bug, if any: Full issue-clause extraction and richer evidence attachment are still
broader product work.
Fix boundary: Add richer extraction/evidence here or in issue obligation producers, not in
adapter rendering.
```

### `artifact_deepswe/phase_policy.py`

```text
ID: ADAPTER-PHASE-SHIM
Desired state: artifact_deepswe is adapter glue, not product policy.
Current state: phase_policy.py is a compatibility shim importing groundtruth.runtime.context_policy.
Logic: Clean.
Implementation: Clean.
Integration: Clean for existing tests and gt_mini_patch imports.
Plumbing: Covered by phase policy sync receipt.
Verdict: CLOSED.
Remaining bug, if any: None for this boundary.
Fix boundary: None.
```

### `artifact_deepswe/gt_agent.py`

```text
ID: ADAPTER-RUNTIME-INJECTION
Desired state: mini-swe adapter injects product runtime files into the task container without
owning product behavior.
Current state: gt_agent.py loads src/groundtruth/runtime modules and writes them under
/opt/gt/groundtruth/runtime during install.
Logic: Clean. Injection belongs to adapter; decisions belong to product modules.
Implementation: Clean for current file list.
Integration: Clean. /opt/gt is already the import root for gt_mini_patch.
Plumbing: Covered by adapter fail-closed and verified adapter receipts.
Verdict: CLOSED.
Remaining bug, if any: Future product runtime modules must be added to this injection list.
Fix boundary: Keep injection manifest updated or make it directory-driven with deterministic allowlist.
```

### `artifact_deepswe/gt_mini_patch.py`

```text
ID: ADAPTER-MINI-PATCH-DELEGATION
Desired state: gt_mini_patch intercepts mini-swe command/output, converts it to product inputs,
and renders returned GT payloads. It must not own phase policy, budget, horizon, action
translation, or obligation semantics.
Current state: Phase detection, payload allow/deny, budget trimming, action translation,
severity, horizon banding, and horizon rendering delegate to product modules.
Logic: Mostly clean. Product decisions are delegated.
Implementation: Partial cleanup. Some legacy code remains below early returns for low-risk
migration, but live behavior for touched paths delegates.
Integration: Clean for mini-swe receipts.
Plumbing: Covered by runtime, adapter, horizon, action, budget, and verified adapter receipts.
Verdict: PARTIAL, not because behavior is wrong, but because dead legacy code still makes the
surface harder to audit.
Remaining bug, if any: Remove unreachable legacy policy/render branches once safe.
Fix boundary: Mechanical cleanup in gt_mini_patch only after receipts guard behavior.
```

### `artifact_deepswe/gt_oracle.py`

```text
ID: ADAPTER-ORACLE-OBLIGATION-DELEGATION
Desired state: oracle may route mini-swe runtime candidates but obligation lifecycle/rendering
comes from product code.
Current state: Obligation status constants, tracker wrapper, ordering, hashing, and rendering
delegate to groundtruth.runtime.obligations.
Logic: Clean.
Implementation: Mostly clean. Compatibility wrapper preserves old raw dict inputs.
Integration: Clean for live obligation wiring.
Plumbing: Covered by obligation and oracle LIPI receipts.
Verdict: CLOSED for current obligation lifecycle delegation.
Remaining bug, if any: Other oracle candidate routing logic remains adapter-owned and should be
audited separately if changed.
Fix boundary: Move product semantics to src/groundtruth/runtime; leave mini-swe routing here.
```

### `scripts/swebench/task_truth.py`

```text
ID: TRUTH-RUNTIME-CONTROL
Desired state: task_truth.json is the final product truth surface for outcome, substrate,
trajectory state, phase policy, obligation lifecycle, verification horizon, consumption,
enforcement semantics, and adapter witness.
Current state: task_truth.py emits runtime_control and trajectory_state blocks derived from
product runtime modules and artifacts.
Logic: Clean.
Implementation: Mostly clean. Mini trajectory parsing is best-effort and deterministic.
Integration: Clean for post-run truth; live artifacts need regeneration.
Plumbing: Covered by task_truth and deep-metrics authority receipts.
Verdict: CLOSED for B5/B9 code path.
Remaining bug, if any: Historical artifacts without regenerated task_truth still carry old truth.
Fix boundary: Regenerate truth artifacts after substrate re-proof.
```

### `scripts/swebench/gt_deep_metrics.py`

```text
ID: DEEP-METRICS-TASK-TRUTH-CONSUMER
Desired state: deep metrics consumes task_truth rather than recomputing contradictory product truth.
Current state: It prefers task_truth outcome and forwards runtime_control plus task_truth_path.
Logic: Clean for B5/B9.
Implementation: Clean.
Integration: Clean for metrics generation after task_truth exists.
Plumbing: Covered by gt_deep_metrics task_truth receipt.
Verdict: CLOSED.
Remaining bug, if any: If task_truth is missing, deep metrics still has fallbacks; reports must label
that source clearly.
Fix boundary: Fail closed or mark source when task_truth is absent in launch workflows.
```

### `scripts/metrics/compute_paired_metrics.py`

```text
ID: PAIRED-METRICS-TASK-TRUTH-CONSUMER
Desired state: paired metrics prefer task_truth and never coerce unknown/null truth into a false result.
Current state: resolved is read from task_truth only when non-null; otherwise failure_class/raw outcome
fallback is used.
Logic: Clean.
Implementation: Clean.
Integration: Clean for paired metrics.
Plumbing: Covered by baseline guard and scorecard smoke receipts.
Verdict: CLOSED.
Remaining bug, if any: No full live regenerated paired report yet after substrate proof.
Fix boundary: Run after P0-01 proof, not before.
```

## Tests/Receipts LIPI

```text
ID: RUNTIME-RECEIPTS
Desired state: Receipts prove architecture behavior after implementation, not replace code review.
Current state: Added product controller and five-language fixtures. Updated adapter/task_truth/deep
metrics receipts to assert delegation and truth propagation.
Logic: Clean.
Implementation: Clean.
Integration: Clean for deterministic Stage 1 proof.
Plumbing: 175 focused receipts passed in the last run.
Verdict: CLOSED as receipts, not as benchmark proof.
Remaining bug, if any: Receipts do not close P0-01 live substrate proof.
Fix boundary: Rebuild and re-proof substrate next.
```

## Final Audit Against `CLAUDE.md`

| Requirement | Current state | Ready? |
|---|---|---:|
| No GT-off rerun | Frozen baseline respected | Yes |
| Stage 1 before Stage 2 | Still in Stage 1 | Yes |
| Deterministic product behavior | Runtime mini-swe controller now has deterministic receipts | Partial |
| Architecture adherence | Improved; product identity moved from adapter to src/groundtruth/runtime | Partial |
| Generality, no task IDs | Current fixes are general and language-family fixtures are deterministic | Yes for this slice |
| One pipeline/truth | task_truth now carries runtime_control; substrate proof still pending | Partial |
| Go/Rust real substrate proof | P0-01 still live | No |
| True benchmarking | Blocked until P0-01 green and generated artifacts agree | No |

## Next Step

Do not run tenpack yet.

Next product step:

1. Rebuild the GT substrate image.
2. Pin `GT_SUBSTRATE_DIGEST`.
3. Re-dispatch the Go/Rust substrate proof jobs from run `27387470440`.
4. Inspect proof artifacts for:
   - `proof_progress.json`
   - `proof_failure.json`
   - `dep_store_manifest.json`
   - `lsp_certificate.json`
   - `reconciled_substrate_verdict.json`
   - `task_truth.json`
5. Only if P0-01 is green, regenerate task truth/deep metrics and then consider the
   first GT-on benchmark run.
