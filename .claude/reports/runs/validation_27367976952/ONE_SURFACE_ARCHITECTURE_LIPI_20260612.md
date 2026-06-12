# One-surface architecture LIPI - 2026-06-12

Branch: `gt-trial`

Current HEAD while writing: `0b093e1d`

Purpose: make the whole GT product understandable as one surface, so bugs are
classified by boundary instead of by benchmark task. Desired state is `gt_gt.md`
and `CLAUDE.md`; current state is the code.

## Principle

Benchmarks are not the product target. They are pressure demonstrations after
the product boundary is correct.

The product target is:

```text
gt_gt.md desired state
  -> actual code current state
  -> gap = product bug
  -> deterministic boundary fix
  -> deterministic test
  -> benchmark run only as demonstration / stress evidence
```

This avoids benchmaxxing. A fix is valid only when it removes a general boundary
bug, not when it helps one named task.

## One Product Surface

GT is currently spread across separate runtime surfaces. Those surfaces must be
understood together:

```text
GHA workflow
  -> pinned substrate image
  -> gt-run-proof
  -> graph/LSP/embedder/brief/certs
  -> DeepSWE adapter
  -> mini-swe-agent runtime patch
  -> agent trajectory
  -> artifacts/outcome/task_truth/deep_metrics
  -> paired scorecard
```

Any one layer can be locally correct while the boundary to the next layer is
wrong. That is the main failure class.

## Desired-state Boundary Contracts

| Boundary | Desired state |
|---|---|
| GHA -> substrate | GHA only orchestrates. It passes a pinned digest, read-only repo, issue text, dependency stores, and full baked-tool PATH. It never runs host GT logic. |
| substrate -> artifacts | `gt-run-proof` produces the authoritative 8 artifacts: `graph.db`, runtime context, LSP cert, graph cert, embedder cert, foundational gates, run manifest, `brief.txt`. |
| artifacts -> adapter | Adapter consumes the substrate artifacts read-only. No second graph, no host-generated brief, no fallback in proof/substrate mode. |
| adapter -> mini-swe-agent | The agent receives exactly one GT brief and the in-container patch is loaded. The adapter emits a `[GT_META]` witness proving graph consumption and hash parity. |
| mini patch -> trajectory | Turn-level GT evidence is appended to observations at phase-appropriate decision points. The payload must be small, trusted, deduped, and action-oriented. |
| trajectory -> metrics | Metrics must read the agent-observation truth, not an assumed side channel. Delivered, consumed, enforced are separate states. |
| outcome -> truth | Outcome classification reconciles certs, witness, infra subtype, reward, and trajectory integrity into one task truth record. |

## Current Code Ownership

| Surface | Primary files | What it owns now |
|---|---|---|
| GHA DeepSWE orchestration | `.github/workflows/deepswe_full.yml` | Task matrix, digest precheck, task image/source extraction, issue extraction, dependency-store extraction, substrate proof run, env handoff into pier, artifact collection, outcome extraction. |
| Language substrate smoke | `.github/workflows/gt_language_smoke.yml` | Five fixture proof check. Same pinned substrate runtime, 8-artifact check, nonzero proof fails. It is a preflight, not equivalent to real task repo proof. |
| Proof runtime entrypoint | `scripts/swebench/gt_run_proof.py` | Proof-only entrypoint, baked dependency checks, graph build, issue-scoped LSP resolve, brief emit, manifest, artifact contract. |
| Proof helpers/context | `src/groundtruth/runtime/proof.py`, `src/groundtruth/runtime/context.py` | Single proof-mode contract: container boundary, import root, host alias handling, graph hash, runtime env export, fail-closed behavior. |
| LSP/product readiness | `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py` | LSP enrichment and cert/gate status. CP005 split transport warm from effective product readiness. |
| DeepSWE adapter | `artifact_deepswe/gt_agent.py` | Injects payload files, prevents dual graph in substrate mode, consumes substrate brief, emits graph witness, runs bounded retry verifier. |
| Runtime observation patch | `artifact_deepswe/gt_mini_patch.py` | In-container observation interception, phase detection, phase policy, obligation tracker bridge, graph-to-action translation, context budget/dedupe, verification horizon. |
| Obligation semantics | `artifact_deepswe/gt_oracle.py`, `artifact_deepswe/gt_oracle_sense.py` | Obligation extraction/status lifecycle, no-leak render, candidate/oracle replay primitives. |
| Delivery/consumption metrics | `scripts/swebench/gt_deep_metrics.py`, `scripts/swebench/consumption_ledger.py` | Trajectory-derived delivery, consumption and enforcement counters, token/cost fields. |
| Task truth / outcome | `scripts/swebench/task_truth.py`, `scripts/verify/deepswe_outcome.py` | Cert/witness/outcome reconciliation, infra subtype classification, denominator policy. |
| Patch hygiene/submission | `scripts/swebench/package_submission.py`, `scripts/swebench/convert_to_submission.py` | Source vs noise patch classification before submission/reporting. |
| Paired scorecard | `scripts/metrics/compute_paired_metrics.py` | Post-hoc Stage-2 scorecard, flip/regression/gt-caused-flip heuristics over paired runs. |

## What Is Already Coded

| Architecture item | Current state |
|---|---|
| B1 deep metrics false zero | Fixed by trajectory fallback and deep metric delivery fields. |
| B2 embedder cert contradiction | Fixed by cert-aware truth path; metrics no longer blindly trust a separate failed import as product truth. |
| B3 exact test leak | Fixed in DeepSWE runtime render path: agent-visible obligation status uses behavior/category language, not exact test names. |
| B4 LSP warm over-credit | Fixed at gate level: product readiness separates warm transport from effective LSP work. |
| B5 graph cert vs outcome truth | Partial: task truth reconciles known pre-agent graph handoff false fail when runtime witness holds. Cross-run audit still open. |
| B6 consumption metric | Fixed: consumption ledger and deep metrics distinguish delivery from consumption/enforcement. |
| B7 low-value surfaces | Fixed: shared path policy and delivery filters suppress vendor/static/generated/minified surfaces. |
| B8 patch hygiene | Fixed: patch hygiene classification separates source edits from lockfile/generated/noise. |
| B9 outcome schema confusion | Partial: `task_truth.json` normalizes one task truth surface, but paired/deep reports can still expose older fields. |
| B10 infra classification | Fixed for known classes: ENOSPC, trajectory fallback, missing artifact, proof/digest/artifact failures. |
| B11 runtime evidence delivery | Fixed by event-bound delivery waiver for view/edit evidence. |
| Trajectory-state controller | Coded in `gt_mini_patch.py` as `Phase` and `_detect_phase`. |
| Context selection policy | Partial: phase gates exist, but full ORIENT/VIEW/EDIT/VERIFY/SUBMIT selection policy is still not a single explicit product object. |
| First-class obligation model | Coded in `gt_oracle.py` with `ObligationTracker`; bridged into runtime patch. |
| Pre-submit gate | Coded as verification-horizon/gate behavior plus retry-loop pre-submit note. It is not a hard external submit blocker in mini-swe-agent; it is an injected/enforced runtime intervention. |
| Verifier-fail retry | Coded in `gt_agent.py` as bounded in-container repo-native test retry. It is harness-level feedback, arm-neutral except GT note. |
| Trust-gated surfaces | Mostly coded: no-leak render, path policy, LSP readiness split, embedder truth, graph witness reconciliation. |
| Context budgeting | Coded in `gt_mini_patch.py` as line ranking, char/token cap, cross-turn dedupe. |
| Graph-to-action translation | Coded in `gt_mini_patch.py` as deterministic templates. |
| Flip/trajectory scorecard | Coded post-hoc in `compute_paired_metrics.py`; needs fresh artifacts to populate. |

## Gaps / Bugs Still Visible From Code-vs-Architecture

These are product gaps, not benchmark-task patches.

| Gap | Current state | Desired state | Failure concern |
|---|---|---|---|
| G1: docs say CP013/014/015 live in `gt_oracle.py`, but runtime code lives in `gt_mini_patch.py` | `gt_gt.md` and session doc name `gt_oracle.py` for phase/action/budget; implementation is mostly in `gt_mini_patch.py` | Docs should reflect active ownership so future bugs go to the correct surface | Debugging goes to wrong file; fixes land in inactive/partial surface |
| G2: smoke is described as byte-identical proof, but DeepSWE adds real-task boundary inputs | Smoke mounts only fixture and artifact dir; DeepSWE adds issue file, dep stores, PATH, budgets, commit provenance | Document smoke as substrate preflight and DeepSWE as substrate+task-handoff proof | A green smoke can be misread as proof that real task handoff is green |
| G3: process rule says checkpoint doc in same commit, but recent proof commits were code/test-only | `d1b20072` and `0b093e1d` have no paired checkpoint doc in the same commit | Either backfill docs or mark exception; future boundary commits should include docs | Handoff trail drifts from code reality |
| G4: B5/B9 remain partial by architecture | `task_truth.json` exists, but paired metrics/outcome surfaces are not fully unified | Every reporting layer should read one normalized task truth when present | Split-brain reports reappear |
| G5: context selection policy is not yet one explicit object | Phase allowlist exists inside runtime patch | A shared phase -> payload -> budget -> silence policy object should be visible/tested | Different surfaces can grow independent speak/silence behavior |
| G6: pre-submit gate is not an external hard submit blocker in mini-swe-agent | It is implemented as runtime injected gate/retry note | Architecture should call this "enforced intervention" unless the harness can block finish | Overstating enforcement; agent may still submit if runtime hook misses |
| G7: verifier retry is repo-native, not official hidden verifier retry | `gt_agent.py` runs a detected repo-native test command between attempts | Docs should distinguish self-verifier retry from official verifier-fail retry | Misclassifies hidden-verifier misses as covered |
| G8: DeepSWE current failures are at substrate real-task proof boundary | Latest run passes several full tasks but Go/Boa fail at proof | Boundary classifier should isolate missing baked dep vs dep-store mount vs repo/LSP readiness vs graph build | Without logs, fixes risk becoming task-specific |

## Latest Boundary Fixes

### `d1b20072 fix(proof): preserve substrate PATH in DeepSWE proof`

Fixed a GHA -> substrate boundary bug:

- The pinned substrate had Node/Python/JRE/Go tools baked.
- DeepSWE proof overrode `PATH` and hid some baked dirs.
- `gt-run-proof` correctly failed closed because LSP binaries were not visible.
- Fix preserved the complete substrate closure:
  `/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:...`
- Added workflow contract tests.

This was a product boundary fix, not task-specific.

### `0b093e1d test(proof): align language smoke contract wording`

Post-commit LIPI cleanup:

- Smoke executable gate was 8-artifact.
- Summary text still said 7-artifact.
- Fixed wording and test coverage.

This removed split-truth documentation inside the smoke surface.

## Current Live Run Evidence

Run `27387470440` is a pressure demonstration of the current code, not the
architecture source of truth.

Observed while this doc was being written:

- Prepare succeeded.
- Several Python/JS/TS/Rust tasks reached and completed full harness path.
- Go tasks and Boa failed at `GT substrate proof`.
- Arktype was still running at last poll.

Interpretation:

- The earlier TypeScript/PATH proof blocker is closed.
- The next active failure boundary is real-task substrate proof for Go/Boa.
- Do not classify that as agent behavior. The agent did not run on those jobs.

## Cleanup Order

1. **Documentation truth cleanup**
   - Update `gt_gt.md` §17.9 so CP013/014/015 ownership names `gt_mini_patch.py` for runtime policy/action/budget and `gt_oracle.py` for obligation semantics.
   - Backfill the proof-boundary doc entries for `d1b20072` and `0b093e1d`.

2. **Real-task substrate proof LIPI**
   - After run logs are available, read Go and Boa proof logs end-to-end.
   - Classify failure into one of: baked dep, hidden PATH, dep-store mount, repo source extraction, issue extraction, LSP readiness, graph build, cert/artifact emission.
   - Fix the boundary generally, with a deterministic workflow/proof test.

3. **B5/B9 unification**
   - Make paired/deep reports prefer `task_truth.json` when present.
   - Ensure `resolved`, `failure_class`, `infra_subtype`, `denominator` have one normalized source.

4. **Context-selection product object**
   - Extract the phase policy into a visible, tested policy surface instead of leaving it embedded in runtime patch globals.
   - Keep `gt_mini_patch.py` as the in-container executor, but make the policy auditable as an architecture object.

5. **Enforcement wording and mechanics**
   - Decide whether mini-swe-agent has a true pre-submit block point.
   - If not, document the current behavior as "pre-submit intervention/retry enforcement", not "hard blocker".
   - If yes, wire and test the hard blocker at the finish/submit boundary.

6. **Verifier retry terminology**
   - Separate repo-native self-verifier retry from official hidden verifier retry.
   - Current code implements the former. The latter remains an architecture item unless a post-official-verifier repair loop is actually wired.

## Failure Classifier For Future Debugging

When a run fails, classify by the earliest violated boundary:

1. GHA orchestration
2. pinned substrate availability
3. proof environment / baked deps
4. graph build / LSP / embedder / gates
5. artifact contract
6. adapter consumption / graph witness
7. runtime patch delivery
8. trajectory consumption / enforcement
9. outcome / task truth / metrics
10. paired scorecard

Only after boundaries 1-9 are correct should a benchmark outcome be discussed as
Stage-2 flip evidence.
