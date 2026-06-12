# GT vs current code - thorough desired/current comparison - 2026-06-12

Audited local HEAD: `5d318828`

Purpose: compare the desired product architecture in [D:\Groundtruth\gt_gt.md](D:\Groundtruth\gt_gt.md)
and [D:\Groundtruth\CLAUDE.md](D:\Groundtruth\CLAUDE.md) against the current codebase, surface by
surface. This is not a benchmark readout and not a test-status summary. The comparison is:

`desired state -> live owner files -> current behavior -> product gap`

The goal is one product surface. If a behavior is split between substrate, GHA, `gt_run_proof`,
`gt_agent`, `gt_mini_patch`, `gt_oracle`, and reporting code, the split itself is the bug unless
the boundary is explicit and correct.

## Comparison frame

Desired state from `gt_gt.md` §1 and §17:

- one GT pipeline
- trusted substrate and truth surfaces
- phase-aware context provider
- obligation-aware intervention model
- small actionable payloads
- delivered / used / enforced measured separately
- benchmark runs only after product readiness

Desired state from `CLAUDE.md`:

- Stage 1 first: deterministic correctness and architecture adherence
- Stage 2 later: flips only after Stage 1 is stable
- right trajectory matters more than raw resolved

## Executive verdict

Current code is materially closer to the desired architecture than the original validation run,
but it is still not at full desired-state parity.

The repo now has:

- a much cleaner substrate boundary
- explicit truth authority surfaces
- an obligation tracker
- a phase policy module
- a consumption ledger
- context-budget logic
- verification-horizon intervention bands

But it still does **not** have:

- one shared trajectory-state controller
- one shared phase-to-payload policy across all delivery surfaces
- a true pre-submit hard gate
- official verifier-fail classify -> map -> inject -> retry plumbing
- a fully unified product path between `src/groundtruth` and the DeepSWE runtime fork

So the honest verdict is:

- substrate/truth boundary: **mostly aligned**
- delivery/runtime control plane: **partially aligned**
- product unification: **still split**
- benchmark readiness as proof of product: **still blocked**

## Surface-by-surface comparison

### S1. One pipeline

Desired state:

- `gt_gt.md` §1 and `CLAUDE.md` say GT is one product pipeline, not separate incompatible systems.
- Graph, LSP, semantic, brief, delivery hooks, metrics, truth surfaces should compose into one
  coherent runtime.

Current code:

- Core graph/resolve/brief substrate lives in:
  - [D:\Groundtruth\src\groundtruth\resolve.py](D:\Groundtruth\src\groundtruth\resolve.py)
  - [D:\Groundtruth\scripts\swebench\gt_run_proof.py](D:\Groundtruth\scripts\swebench\gt_run_proof.py)
  - [D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py](D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py)
- DeepSWE runtime control plane still lives in a forked surface:
  - [D:\Groundtruth\artifact_deepswe\gt_agent.py](D:\Groundtruth\artifact_deepswe\gt_agent.py)
  - [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
  - [D:\Groundtruth\artifact_deepswe\gt_oracle.py](D:\Groundtruth\artifact_deepswe\gt_oracle.py)
  - [D:\Groundtruth\artifact_deepswe\phase_policy.py](D:\Groundtruth\artifact_deepswe\phase_policy.py)

Verdict: `PARTIAL`

Gap:

- The data substrate is unified better than before.
- The delivery/governor/runtime logic is still duplicated into `artifact_deepswe/*` rather than
  being a cleanly shared product module under `src/groundtruth`.
- That means desired behavior can be documented in one place and executed in another.

Fix boundary:

- Reduce `artifact_deepswe/*` to an adapter/wiring layer.
- Move reusable controller/policy/state logic into shared product modules.

### S2. Trust-gated substrate

Desired state:

- `gt_gt.md` §7 and §17 require no silent fallback and reconciled substrate truth.
- Workflow orchestrates only; substrate runtime owns proof policy.

Current code:

- Substrate image build:
  - [D:\Groundtruth\docker\Dockerfile.gt-substrate](D:\Groundtruth\docker\Dockerfile.gt-substrate)
  - [D:\Groundtruth\.github\workflows\gt_substrate_image.yml](D:\Groundtruth\.github\workflows\gt_substrate_image.yml)
- Proof runtime:
  - [D:\Groundtruth\scripts\swebench\gt_run_proof.py](D:\Groundtruth\scripts\swebench\gt_run_proof.py)
- Paid path:
  - [D:\Groundtruth\.github\workflows\deepswe_full.yml](D:\Groundtruth\.github\workflows\deepswe_full.yml)
- Proof sweep:
  - [D:\Groundtruth\.github\workflows\deepswe_proof_sweep.yml](D:\Groundtruth\.github\workflows\deepswe_proof_sweep.yml)

What is now correct:

- paid workflow no longer provisions `rust-src`
- proof runtime owns per-language LSP budget defaults
- proof sweep no longer forks per-language budget policy
- task image dep-store extraction and substrate proof are separate concerns

What is still missing:

- live rebuilt substrate proof on Go/Rust task images

Verdict: `PARTIAL BUT STRUCTURALLY CORRECT`

Gap:

- Architecturally the owner boundary is now right.
- Product proof is still incomplete until rebuilt image + live re-proof confirm the runtime contract.

### S3. Truth authority

Desired state:

- one product truth surface for outcome, witness, substrate verdict, brief delivery, obligation
  status, patch hygiene, and consumption

Current code:

- [D:\Groundtruth\scripts\swebench\task_truth.py](D:\Groundtruth\scripts\swebench\task_truth.py)
- [D:\Groundtruth\scripts\verify\deepswe_outcome.py](D:\Groundtruth\scripts\verify\deepswe_outcome.py)
- [D:\Groundtruth\scripts\swebench\reconcile.py](D:\Groundtruth\scripts\swebench\reconcile.py)

What is correct:

- `task_truth.json` has explicit authority map
- `reconciled_substrate_verdict.json` exists
- runtime witness is distinct from raw certs

What is still incomplete:

- `gt_gt.md` already marks B5/B9 partial, and that is still the honest code verdict
- truth is better modeled, but not yet proven clean across fresh live runs

Verdict: `PARTIAL`

Gap:

- the authority contract exists, but cross-run consistency still depends on live artifact quality
- reporting surfaces are improved, not yet fully retired into one unquestioned truth consumer

### S4. Trajectory-state controller

Desired state:

- one controller asks: what phase is the agent in, what context is needed now, should GT speak or
  stay silent

Current code:

- phase allowlist module:
  - [D:\Groundtruth\artifact_deepswe\phase_policy.py](D:\Groundtruth\artifact_deepswe\phase_policy.py)
- runtime phase detection:
  - [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
    `_detect_phase`, `_oracle_nonedit_streak`, `_action_count`, `_oracle_edited_rels`
- replay/oracle phase model:
  - [D:\Groundtruth\artifact_deepswe\gt_oracle.py](D:\Groundtruth\artifact_deepswe\gt_oracle.py)

What exists:

- a shared allowlist table
- a runtime heuristic phase detector
- replay logic that reasons over review transitions and per-turn suppression

Why this is not the desired state yet:

- there is no single first-class `TrajectoryState` product object shared across runtime and replay
- phase detection still depends on module globals in `gt_mini_patch.py`
- runtime phase set is `ORIENT/SEARCH/EDIT/VERIFY/SUBMIT`, while replay logic still talks in
  `REVIEW` transitions from sensed history
- policy and state are connected, but not unified into one controller

Verdict: `PARTIAL`

Remaining bug:

- architecture has a phase policy, not a full trajectory-state controller

### S5. Context selection policy

Desired state:

- ORIENT, VIEW, EDIT, VERIFY, SUBMIT each have bounded allowed payload classes
- selection should be policy-driven and small

Current code:

- allowlist:
  - [D:\Groundtruth\artifact_deepswe\phase_policy.py](D:\Groundtruth\artifact_deepswe\phase_policy.py)
- event-bound filtering and live gate:
  - [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)

What exists:

- policy allowlist by phase
- event-bound bypass for post-view/post-edit/review-transition candidates
- budget trim and dedupe

Why this is still partial:

- the brief/orientation path is not governed by the same compact controller as later emissions
- ORIENT policy is minimal and does not yet encode "top files + first command" as the architecture
  describes
- VIEW/EDIT/VERIFY/SUBMIT payload semantics are spread across candidate producers rather than one
  declarative contract

Verdict: `PARTIAL`

### S6. Obligation model

Desired state:

- issue clauses become first-class obligations with lifecycle and evidence

Current code:

- live producer and tracker:
  - [D:\Groundtruth\artifact_deepswe\gt_oracle.py](D:\Groundtruth\artifact_deepswe\gt_oracle.py)
  - [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
- persisted truth:
  - [D:\Groundtruth\scripts\swebench\task_truth.py](D:\Groundtruth\scripts\swebench\task_truth.py)

What is correct:

- obligation status is explicit
- edited/tested/satisfied/contradicted distinctions are modeled
- status vector hashing and review-transition emission exist

Residual gap:

- lifecycle exists primarily on the DeepSWE runtime fork side, not as a shared core product model
- evidence plumbing is good enough to persist, but not yet centered in a shared domain object under
  `src/groundtruth`

Verdict: `MOSTLY SHIPPED, STILL SPLIT BY OWNER`

### S7. Consumption feedback loop

Desired state:

- Delivered, Used, Enforced are separate product states

Current code:

- offline ledger:
  - [D:\Groundtruth\scripts\swebench\consumption_ledger.py](D:\Groundtruth\scripts\swebench\consumption_ledger.py)
- runtime suppression heuristic:
  - [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)

What is correct:

- ledger separates delivered, consumed, verification-followup, hard-enforced
- enforcement semantics are explicitly `hard_block_only`

What is not yet ideal:

- runtime uses a heuristic suppression loop, and the file itself labels it as not authoritative
- the consumption ledger is still an after-the-fact trajectory pass, not one shared runtime and
  reporting contract

Verdict: `SHIPPED BUT NOT FULLY UNIFIED`

### S8. Context budgeting

Desired state:

- small payloads
- dedupe
- action-oriented ranking
- suppress repeated low-value facts

Current code:

- [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
  `_budget_trim`, `_stable_fact_id`, imperative-first ordering, delivered-fact dedupe

Verdict: `SHIPPED`

Residual gap:

- budgeting exists at runtime delivery, but the architecture still wants one shared phase-to-payload
  contract, not just one trimming layer

### S9. Graph-to-action translation

Desired state:

- graph facts should become direct next-step guidance, not raw witness dumps

Current code:

- [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
  `_translate_to_action`

What exists:

- evidence is translated toward action phrasing before budget trimming

Gap:

- action translation exists, but it is runtime-side string transformation rather than a shared
  product contract used across brief, view, edit, and verify surfaces

Verdict: `PARTIAL/SHIPPED`

### S10. Pre-submit semantics

Desired state:

- architecture originally wanted a pre-submit gate
- current corrected product semantics in `gt_gt.md` are stricter: if it is not a hard block, do not
  call it a hard gate

Current code:

- [D:\Groundtruth\artifact_deepswe\gt_mini_patch.py](D:\Groundtruth\artifact_deepswe\gt_mini_patch.py)
  verification horizon `gate` band
- [D:\Groundtruth\artifact_deepswe\gt_agent.py](D:\Groundtruth\artifact_deepswe\gt_agent.py)
- [D:\Groundtruth\scripts\swebench\task_truth.py](D:\Groundtruth\scripts\swebench\task_truth.py)
  `verifier_semantics`

What is correct:

- docs and truth surfaces now distinguish intervention from hard block
- enforcement metric no longer overclaims

What is still missing:

- there is still no true hard pre-submit block
- current product has a high-severity intervention ring, not a guaranteed stop

Verdict: `PARTIAL BY DESIGN`

### S11. Verifier-fail retry plumbing

Desired state:

- official verifier fail should classify -> map -> inject -> bounded retry

Current code:

- self-verifier retry exists:
  - [D:\Groundtruth\artifact_deepswe\gt_agent.py](D:\Groundtruth\artifact_deepswe\gt_agent.py)
  - pier retry wiring and semantics surfaced in docs/truth

What is correct:

- self-verifier retry is explicitly named and no longer mislabeled as official verifier repair

What is missing:

- no full official post-submit verifier feedback repair loop

Verdict: `PARTIAL`

### S12. Benchmark boundary

Desired state:

- benchmarks prove product readiness after architecture correctness, not define it

Current code and docs:

- [D:\Groundtruth\CLAUDE.md](D:\Groundtruth\CLAUDE.md)
- [D:\Groundtruth\gt_gt.md](D:\Groundtruth\gt_gt.md) §17
- [D:\Groundtruth\LATEST_TASK.md](D:\Groundtruth\LATEST_TASK.md)
- [D:\Groundtruth\.claude\CURRENT_VALIDATION_RUN.json](D:\Groundtruth\.claude\CURRENT_VALIDATION_RUN.json)

Verdict: `ALIGNED`

Reason:

- the current documentation and execution protocol now clearly hold tenpack behind Stage-1 proof and
  substrate re-validation

## Most important remaining product gaps

These are the current highest-value architecture gaps after the shipped fixes:

1. **No single trajectory-state controller**
   - policy exists
   - runtime heuristics exist
   - replay state exists
   - one shared controller object does not

2. **No single phase-to-payload contract**
   - allowlist exists
   - producer behavior is still distributed and partially implicit

3. **DeepSWE runtime logic is still product-forked**
   - `artifact_deepswe/*` still owns too much permanent product behavior

4. **Truth authority is modeled but not fully retired into one unquestioned live surface**
   - B5/B9 remain honestly partial

5. **Pre-submit semantics are intervention-strength, not hard-gate strength**
   - acceptable only if documented as such
   - not equivalent to original hard gate desire

6. **Official verifier-repair loop is still absent**
   - self-verifier retry is not the same thing

7. **Live proof is still the missing confirmation for the corrected substrate boundary**
   - architecture improved
   - proof still required

## Product verdict

If we compare desired state to current state honestly:

- The repo is no longer in the original fragmented/contradictory state that produced the first bug
  dossier.
- It is also not yet the fully unified GT product described by `gt_gt.md`.

The remaining work is no longer "random bug fixing." It is now mostly:

- unifying controller/policy/state into shared product modules
- reducing the DeepSWE runtime fork to a thin adapter
- finishing truth-surface consolidation
- proving the corrected substrate boundary on live task images

## Next fix order from this comparison

1. Build the shared trajectory-state controller surface
2. Consolidate phase-to-payload policy into one declarative contract
3. Pull long-lived runtime logic out of `artifact_deepswe/*` where practical
4. Finish truth-surface closure for B5/B9
5. Only then resume substrate rebuild + re-proof + tenpack gating
