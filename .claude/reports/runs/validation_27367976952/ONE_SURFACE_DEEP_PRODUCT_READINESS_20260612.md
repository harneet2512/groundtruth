# One-surface deep product readiness map - 2026-06-12

Branch: `gt-trial`

Code HEAD while writing: `0b093e1d`

This document is the deep pass requested after the high-level one-surface LIPI.
It treats GT as a product launch surface first and a benchmark system second.
The desired state is `gt_gt.md` plus `CLAUDE.md`; the current state is code.
Every gap below is a product bug, even when a benchmark task happens to pass.

## Launch Principle

Benchmarks are evidence after product readiness. They are not the design target.

The launch standard is:

```text
desired architecture contract
  -> exact current code owner
  -> exact boundary artifact / marker
  -> smallest split-truth or soft-pass
  -> deterministic test
  -> commit + checkpoint doc
  -> benchmark pressure run
```

A benchmark run is not allowed to define the fix. It can only reveal which
product boundary violated the architecture contract.

## Full Product Surface

GT is not one Python file and not one workflow. It is one product surface split
across ten boundaries:

```text
1. GHA orchestration
2. pinned substrate image
3. gt-run-proof
4. graph / LSP / embedder / brief / cert artifacts
5. host DeepSWE adapter
6. mini-swe-agent in-container patch
7. agent trajectory
8. artifact collection
9. outcome / task_truth / deep_metrics
10. paired scorecard
```

The main launch risk is not that one layer is missing. The risk is that two
layers each look locally green while their boundary is false.

## Boundary 1 - GHA Orchestration To Pinned Substrate

Desired contract:

- GHA only orchestrates.
- It passes a pinned substrate digest, a read-only task repo, issue text,
  language dependency stores, and proof env.
- It must not run host GT indexing, host brief generation, or host LSP as a
  substitute for the substrate.
- Every proof failure must emit a classified marker.

Current code:

- `.github/workflows/deepswe_full.yml`
- Prepare job verifies workflow shape, digest, cost guard, and bench checkout.
- `GT_REQUIRE_FULL_STACK=1`, `GT_REQUIRE_FULL_POTENTIAL=1`,
  `GT_REQUIRE_FTS5=1`, `GT_FORCE_ONNX_EMBEDDER=1`,
  `GT_REQUIRE_EMBEDDER=1`, `GT_REQUIRE_LSP=1`.
- `GT_FORBID_PREBUILT_GRAPH=1` prevents host prebuilt graph shortcuts.
- Task image source is copied out to `/tmp/gt/src`, then mounted read-only into
  the substrate at `/work`.
- Go module cache is copied to `/tmp/gt/deps/gomodcache`.
- Cargo and rustup stores are copied to `/tmp/gt/deps/cargo` and
  `/tmp/gt/deps/rustup`.
- Issue text is extracted from `instruction.md` first, then task metadata
  fallback; empty issue fails closed as `GT_ISSUE_MISSING`.
- The proof container runs:

```text
gt-run-proof --source-root /work --out /gt_artifacts
```

- The DeepSWE proof run now preserves the complete substrate PATH:

```text
/opt/gt/bin
/opt/gt/node/bin
/opt/gt/python/bin
/opt/gt/jre/bin
/opt/gt/go/bin
/root/.cargo/bin
```

Artifacts / markers:

- `GT_SUBSTRATE_PULL_FAIL`
- `TASK_IMAGE_PULL_FAIL`
- `GT_ISSUE_MISSING`
- `GT_RUN_PROOF_FAIL`
- `GT_PROOF_OOM`
- `GT_ARTIFACT_MISSING`

Already closed:

- `d1b20072` fixed the hidden substrate PATH split where GHA hid baked LSP
  binaries from `gt-run-proof`.
- `0b093e1d` fixed the smoke wording/test split around the 8-artifact contract.

Launch gaps:

- The language smoke workflow is a substrate preflight, not byte-identical to
  the DeepSWE real-task proof. Smoke does not mount issue text, dep stores, the
  task image source shape, or the same DeepSWE proof PATH. A green smoke cannot
  be used as full task proof.
- The current live run `27387470440` shows Go/Boa failures at `GT substrate
  proof`. That is a product-boundary failure before the agent runs, not an
  agent failure.
- The next product LIPI must read the full failed proof logs after the matrix
  completes and classify the failure into baked dep, PATH, dep-store mount,
  source extraction, issue extraction, LSP readiness, graph build, or artifact
  emission.

Readiness verdict:

Partial. The orchestration contract is much cleaner after the PATH fix, but
real-task substrate proof still has launch-blocking failures.

## Boundary 2 - Language Smoke To DeepSWE Proof

Desired contract:

- Smoke is a fast proof of substrate health.
- DeepSWE proof is the real task handoff proof.
- Docs and dashboards must not imply smoke proves all DeepSWE handoff inputs.

Current code:

- `.github/workflows/gt_language_smoke.yml`
- Runs five fixture proofs.
- Pulls the same pinned substrate digest.
- Fails on nonzero `gt-run-proof`.
- Requires the same 8 artifacts:

```text
graph.db
runtime_context.json
lsp_certificate.json
graph_certificate.json
embedder_certificate.json
foundational_gate_report.json
run_manifest.json
brief.txt
```

Launch gaps:

- Smoke lacks the task-image copy path, dependency-store copy path, issue-file
  path, matrix language budget path, and adapter handoff path.
- The old wording drift was fixed, but product docs should explicitly call this
  a preflight, not full parity.

Readiness verdict:

Usable as preflight only. Not a launch proof by itself.

## Boundary 3 - gt-run-proof Runtime

Desired contract:

- One portable command produces all substrate artifacts inside the pinned image.
- It validates the proof env before doing work.
- It fails closed on host-boundary leaks, missing baked dependencies, empty
  issue scope, and missing artifacts.

Current code:

- `scripts/swebench/gt_run_proof.py`
- Required artifacts are declared at the top as the 8-artifact proof set.
- `validate_proof_env()` checks:
  - proof mode
  - containerized mode
  - unified substrate strategy
  - baked LSP availability
  - baked embedder availability
  - `gt-index` availability
- `_baked_lsp_problems()` reads canonical LSP configuration from
  `groundtruth.lsp.config.LSP_SERVERS` and known aliases.
- `_baked_embedder_problems()` allows only the configured model and does not
  silently substitute a fallback.
- `eval_leakage()` forbids evaluator/gold artifacts.
- Main execution:
  - copies read-only source into writable scratch
  - runs `gt-index`
  - resolves LSP per detected language
  - writes per-language LSP certs plus aggregate cert
  - builds graph cert
  - builds foundational gates
  - probes/writes embedder cert if missing
  - emits `brief.txt`
  - writes `runtime_context.json`
  - writes `run_manifest.json`

Artifacts / markers:

- `run_manifest.json` schema `gt.run_manifest.v2`
- `runtime_context.json`
- `gt_lsp_metrics.txt`
- `lsp_certificate_*.json`
- `brief.txt`
- `GT_ARTIFACT_MISSING`
- `LSP_LIVENESS_FAIL`

Launch gaps:

- `GT_RUN_PROOF_FAIL rc=2` is still too broad for launch debugging. The marker
  identifies the boundary but not the sub-boundary. The proof output may contain
  detail, but the workflow marker itself still collapses multiple causes.
- The product should persist a small `proof_failure.json` when proof exits
  nonzero, with substage, language, exception class, and missing tool/artifact.
  Otherwise every launch failure requires full log spelunking.

Readiness verdict:

Architecture-aligned, but failure diagnostics are not launch-grade yet.

## Boundary 4 - Runtime Context / Proof Helpers

Desired contract:

- One runtime context distinguishes proof-in-container from host handoff.
- Proof mode rejects host aliases and host-generated fallbacks.
- The graph consumed later must hash to the same post-LSP graph.

Current code:

- `src/groundtruth/runtime/proof.py`
- `src/groundtruth/runtime/context.py`
- `HOST_HANDOFF = ("GT_HOST_SRC_ROOT", "GT_HOST_GRAPH_DB")`
- Host aliases are rejected in proof.
- `assert_container_boundary()` fails
  `FINAL_PIPELINE_HOST_SPLIT_FAIL` if proof is not inside the container.
- `GTRuntimeContext.from_env()`:
  - proof path consumes canonical `GT_SOURCE_ROOT` / `GT_GRAPH_DB`
  - non-proof host handoff can consume `GT_HOST_*`
- `graph_edges_hash()` is the canonical consumed-graph fingerprint.

Launch gaps:

- This layer is strong, but its final product truth depends on downstream
  surfaces reading the witness and `task_truth.json`, not raw pre-agent certs.

Readiness verdict:

Strong as a boundary primitive.

## Boundary 5 - Graph / LSP / Embedder / Gate Artifacts

Desired contract:

- Graph, LSP, FTS5, and embedder are one substrate capability surface.
- A gate must never call a no-op capability product-ready.
- A cert can be raw evidence, but the product verdict must reconcile runtime
  witness, not expose stale raw failure as final truth.

Current code:

- `src/groundtruth/resolve.py`
- `scripts/metrics/foundational_gates.py`
- `scripts/metrics/graph_certificate.py`
- `src/groundtruth/runtime/proof.py`
- `src/groundtruth/pretask/graph_localizer.py`

Already closed:

- LSP readiness was split from warm transport.
- Product readiness now needs effective work, not just server launch.
- Embedder truth was moved away from blindly trusting a separate failed import.
- Graph handoff reconciliation exists when runtime witness proves the graph was
  consumed and hash-matched.

Launch gaps:

- Raw graph cert can still say `GRAPH_FAIL_MISSING_HANDOFF` before the adapter
  witness is known. That raw cert is useful evidence, but it must not be the
  final user-facing truth when `task_truth.json` later reconciles it.
- B5/B9 are still partial until every reporting layer prefers normalized task
  truth over raw cert/outcome fragments.
- Product dashboards must display:

```text
transport_warm
project_ready
definition_resolution_active
effective_lsp_edges
reconciled_graph_handoff
```

not a single vague "LSP warm" or "all_on" surface.

Readiness verdict:

Mostly fixed internally, not fully unified at report/product surface.

## Boundary 6 - Substrate Artifacts To DeepSWE Adapter

Desired contract:

- The host adapter consumes substrate artifacts read-only.
- It must not build a second graph in substrate mode.
- It must not generate a host brief in proof/substrate mode.
- It must emit an agent-run witness proving graph consumption and hash parity.

Current code:

- `artifact_deepswe/gt_agent.py`
- `_substrate_active()` detects `GT_PORTABLE_SUBSTRATE` /
  `GT_HOST_GRAPH_DB` / `GT_CERT_DIR`.
- `install_spec()` skips `_BUILD_GRAPH_DB` when substrate is active.
- `_substrate_brief()` consumes `$GT_CERT_DIR/brief.txt` read-only.
- `_generate_brief()` only falls through to host `generate_v1r_brief` in legacy
  non-proof, non-substrate mode.
- `_prepend_brief()` prevents duplicate `<gt-task-brief>` wrappers.
- `_emit_gt_meta_witness()`:
  - resolves graph via `GTRuntimeContext.from_env()`
  - hashes consumed graph via `proof.graph_edges_hash()`
  - reads post-LSP hash from LSP/graph cert
  - emits `[GT_META] graph_witness ...`
  - fails closed on missing graph or hash mismatch in proof/substrate mode.

Artifacts / markers:

- `[GT_META] graph_witness`
- `gt_prebuilt_active=true`
- `hook_graph_hash_matches_post_lsp=True`
- `DEEPSWE_ADAPTER_FAIL`
- `GRAPH_FAIL_HASH_MISMATCH`
- `GT_ARTIFACT_NOT_CONSUMED`

Launch gaps:

- This boundary is only as strong as the GHA witness check. The workflow checks
  the witness after the trial, but if the proof stage fails no witness exists.
  That is expected; classify it as proof boundary, not adapter boundary.
- Product docs need to say adapter owns consumption, while `gt-run-proof` owns
  creation. Mixing those makes debugging ambiguous.

Readiness verdict:

Strong, assuming proof artifacts exist.

## Boundary 7 - Adapter To Mini-swe-agent Runtime Patch

Desired contract:

- The in-container patch sees the same read-only graph artifacts mounted at
  `/gt_artifacts`.
- It provides just-in-time context at phase boundaries.
- It remains benchmark-valid: no hidden tests, no exact test names, no gold.
- It should be correct-or-quiet.

Current code:

- `artifact_deepswe/gt_agent.py` injects payload files and `.pth` bootstrap.
- `artifact_deepswe/gt_mini_patch.py` is the live runtime observation patch.
- The workflow forwards env through `pier run --ae` into the task container:
  - `GT_HOST_GRAPH_DB=/gt_artifacts/graph.db`
  - `GT_CERT_DIR=/gt_artifacts`
  - `GT_HOST_SRC_ROOT=/gt_artifacts/src`
  - `GT_PORTABLE_SUBSTRATE=1`
  - `GT_FORBID_PREBUILT_GRAPH=1`
  - `GT_PROOF_MODE=1`
  - `GT_CONTAINERIZED=1`
  - `GT_ORACLE_ROUTE=1`
  - `GT_ORACLE_EVENTS=/gt_out/gt_oracle_events.jsonl`

Runtime patch code owns:

- delivery path filtering
- source/static/vendor/minified suppression
- view/edit event classification
- phase detection
- phase allowlist
- obligation tracker bridge
- graph-to-action translation
- context budget/dedupe
- consumption-ledger suppression/boost
- verification horizon
- one-block-per-turn oracle gate
- latch re-arm for candidates that lost the gate

Launch gaps:

- Docs currently name `gt_oracle.py` as the owner of phase/action/budget in
  some places. The live owner is mostly `gt_mini_patch.py`; `gt_oracle.py` owns
  obligation semantics and replay primitives. This doc drift is a product bug
  because it sends future fixes to the wrong surface.
- The phase policy exists as runtime globals:

```text
Phase.ORIENT -> consensus.scope
Phase.SEARCH -> l3b.evidence
Phase.EDIT -> evidence/spec/contract/cochange/coherence
Phase.VERIFY -> spec/l5/detect/verify horizon
Phase.SUBMIT -> spec/gate
```

  That is coded, but not yet a first-class shared policy object with its own
  product schema and tests across surfaces.
- `_ledger_note_delivery(kind, cmd)` marks consumption from the same command
  that triggered delivery. That is a useful in-memory suppression heuristic, but
  the stronger product metric still needs trajectory-level next-turn proof from
  `scripts/swebench/consumption_ledger.py`.
- Calibration defaults in verification horizon are explicitly from 9 frozen
  oracle trajectories. That is acceptable as a placeholder if documented, but
  it is not a launch-grade universal calibration corpus.

Readiness verdict:

Functional but needs ownership/doc cleanup and policy extraction for launch.

## Boundary 8 - Obligation Model / Pre-submit / Verification Horizon

Desired contract:

- Issue obligations are first-class objects with status and evidence.
- GT should fire near review/submit when obligations are unedited, untested, or
  contradicted.
- Verification guidance must be behavior/category level, not hidden test names.
- "Pre-submit gate" must be precisely defined: hard blocker or injected
  enforcement intervention.

Current code:

- `artifact_deepswe/gt_oracle.py`:
  - `Obligation`
  - `ObligationTracker`
  - status lifecycle
  - no-leak obligation render
- `artifact_deepswe/gt_mini_patch.py`:
  - `_get_obligation_tracker()`
  - `_obligation_nudge_block()`
  - review-transition predicate
  - verification horizon banding
  - `_render_verify_emission()` with no exact test names, file paths, or
    single-test commands
- `artifact_deepswe/gt_agent.py`:
  - retry loop prepends a GT pre-submit reminder after real test failure.

Already closed:

- Exact test-name leak path was removed from the DeepSWE runtime fork.
- Obligation status lifecycle exists.
- Review-transition obligation candidate can emit without the old once-per-task
  silent latch.

Launch gaps:

- The current "pre-submit gate" is an intervention in the runtime/attempt path.
  It is not a proven external hard blocker at mini-swe-agent finish/submit.
- If product docs say "agent could not submit," the code must hook the finish
  boundary. If code stays as-is, docs should say "pre-submit intervention with
  retry enforcement when repo-native tests fail."
- The obligation model is live, but cross-surface status persistence should be
  visible in task truth, not only runtime emissions.

Readiness verdict:

Good obligation core. Enforcement wording/mechanics still need exactness.

## Boundary 9 - Verifier-fail Retry

Desired contract:

- Official hidden verifier failure -> classify -> map to obligation/symbol ->
  inject repair context -> bounded retry, without leaking hidden tests.

Current code:

- `artifact_deepswe/gt_agent.py`
- `_retry_count()` reads `GT_SELF_VERIFY_ATTEMPTS` first, then legacy
  `GT_RETRY_ON_VERIFIER_FAIL`, cap 2.
- `_retry_test_command()` uses explicit `GT_RETRY_TEST_CMD` if set, else a
  generic repo-native auto-detected runner:
  - `go test ./...`
  - `cargo test`
  - `npm test --silent`
  - `python3 -m pytest -x -q`
- `_retry_verifier_check()` classifies pass/fail/unverifiable.
- `_format_test_feedback()` emits arm-neutral `<test-feedback>`.
- `_run_with_test_retry()` reruns the agent in the same container after real
  visible test failure.

Important code comment:

- The adapter explicitly does not call pier's official verifier mid-run because
  that would upload hidden tests and leak them into the agent context.

Launch gaps:

- This is not official hidden-verifier retry plumbing. It is repo-native
  self-verification retry.
- That is the correct anti-leak choice today, but architecture docs must not
  overclaim official verifier retry.
- If official retry is desired, it must happen after the official verifier in a
  separate repair attempt with hidden output sanitized to obligation categories.

Readiness verdict:

Repo-native retry is product-valid. Official-verifier retry remains unbuilt.

## Boundary 10 - Trajectory To Deep Metrics / Consumption

Desired contract:

- Metrics must read what the agent actually observed.
- Delivered, used, and enforced are separate states.
- Missing side-channel summaries must not become false zeroes.

Current code:

- `scripts/swebench/gt_deep_metrics.py`
- `_from_miniswe_trajectory()` parses `mini-swe-agent.trajectory.json`.
- Counts GT-visible content in observations:
  - `<gt-task-brief>`
  - `<gt-evidence>`
  - `<gt-graph>`
  - `<gt-nudge>`
  - understand/verify calls
  - GT observation chars
- `scripts/swebench/consumption_ledger.py` provides trajectory-derived
  consumption/enforcement fields.

Already closed:

- The false-zero injected-token / per-layer bug was addressed by trajectory
  fallback.
- Delivery is no longer treated as sufficient proof of usefulness.

Launch gaps:

- The runtime in-memory ledger and post-run consumption ledger are related but
  not the same. Docs and dashboards must distinguish:

```text
runtime suppression heuristic
post-run consumption evidence
```

- Deep metrics should prefer task truth where outcome/failure class are present,
  otherwise older report layers can still produce split truth.

Readiness verdict:

Much improved. Needs task-truth unification.

## Boundary 11 - Outcome / Task Truth

Desired contract:

- One normalized task truth record should reconcile:
  - reward
  - resolved bool
  - failure class
  - infra subtype
  - denominator inclusion
  - cert verdicts
  - runtime graph witness
  - trajectory integrity
  - deep metrics

Current code:

- `scripts/verify/deepswe_outcome.py`
- `scripts/swebench/task_truth.py`
- Outcome classification precedence:

```text
INFRA > GT > RESOLVED > missing witness > AGENT > UNKNOWN
```

- Known infra markers include:
  - digest/pull/proof/artifact/task-image failures
  - `GT_RUN_PROOF_FAIL`
  - `GT_PROOF_OOM`
  - `GT_ARTIFACT_MISSING`
  - `GT_ISSUE_MISSING`
- `detect_infra_subtype()` handles ENOSPC, trajectory fallback, missing artifact.
- `task_truth.py` reconciles graph handoff:
  - raw graph cert fail `GRAPH_FAIL_MISSING_HANDOFF`
  - runtime witness proves `gt_prebuilt_active=true`
  - hook hash matches post-LSP
  - status becomes witness-overrides.

Launch gaps:

- `task_truth.json` is the right product surface, but not every older metrics
  path is guaranteed to consume it.
- B5 and B9 remain partial until paired/deep/outcome tables all prefer the
  normalized task truth when present.
- The current `GT_RUN_PROOF_FAIL rc=2` subtype needs deeper proof-stage
  specificity for launch triage.

Readiness verdict:

Good canonical direction, partial adoption.

## Boundary 12 - Patch Hygiene / Submission

Desired contract:

- Source edits, generated files, lockfiles, and weird path artifacts must be
  separated before submission/reporting.
- Toolchain noise must not be counted as agent quality.

Current code:

- `scripts/swebench/package_submission.py`
- `scripts/swebench/convert_to_submission.py`
- Patch hygiene classifies source vs generated/lockfile/noise.

Launch gaps:

- This is fixed as a classifier, but future benchmark reporting must visibly
  include the hygiene verdict. Silent acceptance of noisy patches would reopen
  B8.

Readiness verdict:

Closed if all reports consume the hygiene verdict.

## Boundary 13 - Paired Scorecard

Desired contract:

- Stage 2 only, after Stage 1 readiness.
- Never rerun baseline.
- Scorecard must measure right trajectory, not just resolved.

Current code:

- `scripts/metrics/compute_paired_metrics.py`
- `compute_trajectory_scorecard()` computes post-hoc paired fields:
  - flips
  - regressions
  - gt-caused-flip heuristics
  - consumption/test evidence
  - obligation coverage

Launch gaps:

- This is post-hoc tooling and awaits fresh valid artifacts.
- It cannot prove product readiness if boundaries 1-12 are split.

Readiness verdict:

Tooling exists. Do not use until substrate proof and task truth are clean.

## B1-B11 Product Status From Code

| Bug | Product-readiness status | Reason |
|---|---|---|
| B1 deep metrics false zero | Closed | Trajectory fallback exists; GT observations are parsed from mini trajectory. |
| B2 embedder contradiction | Closed enough internally | Cert-aware truth path exists; remaining risk is report unification. |
| B3 exact test leak | Closed | DeepSWE runtime render avoids exact test names/paths/commands. |
| B4 LSP warm over-credit | Closed internally | Transport and effective readiness are separated. Report wording must stay precise. |
| B5 graph cert vs outcome truth | Partial | Runtime witness reconciliation exists, but raw cert/report split can still confuse. |
| B6 no consumption metric | Closed | Consumption ledger exists; runtime heuristic must not be confused with post-run proof. |
| B7 low-value surface leakage | Closed internally | Path filters exist across runtime; watch all brief/ranking surfaces. |
| B8 patch hygiene | Closed internally | Classifier exists; report adoption must remain mandatory. |
| B9 outcome schema confusion | Partial | `task_truth.json` is canonical, but old report consumers can still split truth. |
| B10 infra classification | Closed for known classes | ENOSPC/missing/partial/proof classes exist; `rc=2` needs proof subtyping. |
| B11 runtime evidence delivery | Closed | Event-bound evidence waiver exists in runtime patch. |

## Ten Architectural Pieces - Coded vs Product-Closed

| # | Piece | Coded? | Product-closed? | Deep reason |
|---|---|---:|---:|---|
| 1 | Trajectory-state controller | Yes | Partial | `_detect_phase()` exists, but controller is embedded in runtime patch, not a shared auditable policy surface. |
| 2 | Context selection policy | Yes | Partial | `_PHASE_POLICY` exists; full ORIENT/VIEW/EDIT/VERIFY/SUBMIT policy is not extracted as a product object. |
| 3 | Consumption feedback loop | Yes | Mostly | Post-run ledger exists; runtime in-memory ledger is only a heuristic. |
| 4 | First-class obligation model | Yes | Mostly | `ObligationTracker` exists; task-truth persistence of lifecycle should be made visible. |
| 5 | Pre-submit gate | Yes | Partial | It is an injected intervention/retry reminder, not a proven hard finish blocker. |
| 6 | Verifier-fail retry plumbing | Repo-native only | Partial | Visible repo-native retry exists; official hidden-verifier repair loop is intentionally not built. |
| 7 | Trust-gated context surfaces | Yes | Mostly | No-leak/path/LSP/embedder/graph fixes exist; reporting split-truth remains. |
| 8 | Context budgeting | Yes | Partial | Budget/dedupe exists; calibration quality and shared policy extraction remain. |
| 9 | Graph-to-action translation | Yes | Mostly | Deterministic templates exist; needs broader test coverage across graph fact shapes. |
| 10 | Flip/trajectory scorecard | Yes | Not launch proof | Post-hoc tooling exists; needs fresh valid artifacts after Stage 1 readiness. |

## Meniscule Launch Blockers

These are small enough to look like paperwork, but serious enough to block
product readiness because they create split truth.

1. `gt_gt.md` says CP013/014/015 primary surface is `gt_oracle.py`, but live
   phase/action/budget code is mostly `gt_mini_patch.py`.
2. Smoke and DeepSWE proof are described too similarly in some docs; their
   boundary inputs differ.
3. `GT_RUN_PROOF_FAIL rc=2` is too coarse for launch triage.
4. Raw graph cert failure can remain visible after runtime witness reconciles
   it.
5. "Pre-submit gate" wording overstates enforcement unless a true finish hook
   blocks submit.
6. "Verifier retry" wording overstates official hidden-verifier retry; current
   code is repo-native visible self-verification.
7. Runtime ledger and post-run consumption ledger are two different proofs and
   should not share vague "consumed" wording.
8. Verification horizon thresholds are from a tiny frozen corpus; acceptable as
   placeholder, not launch calibration.
9. Task truth is canonical, but older reports can still read raw outcome/deep
   metrics directly.
10. Recent proof-boundary code commits did not include checkpoint docs in the
    same commit, violating `gt_gt.md` section 17.6.

## Product Cleanup Order

1. Fetch and read failed proof logs from run `27387470440` when the matrix
   finishes. Do not infer from task names.
2. Add proof-stage subtyping for `GT_RUN_PROOF_FAIL rc=2`.
3. Fix the Go/Boa proof boundary generally, with deterministic workflow/proof
   tests. No task-name special cases.
4. Update `gt_gt.md` ownership map: `gt_mini_patch.py` owns live phase policy,
   graph-to-action, budget, and runtime gate; `gt_oracle.py` owns obligation
   semantics/replay.
5. Make all report surfaces prefer `task_truth.json` when present.
6. Rename or clarify:
   - pre-submit hard blocker vs pre-submit intervention
   - official verifier retry vs repo-native self-verifier retry
   - runtime consumption heuristic vs trajectory consumption proof
7. Extract context selection into a visible product policy object with tests.
8. Expand deterministic Stage-1 tests for graph-to-action templates and
   obligation lifecycle persistence.
9. Run benchmark pressure only after the above is clean.

## Launch Readiness Verdict

Not launch-ready yet.

The product is much closer than the original 10-task audit: the core broken
surfaces B1-B4, B6-B8, B10-B11 are substantially fixed in code. But the product
still has launch blockers:

- real-task substrate proof still fails before the agent on current GHA
- task truth is not yet the single reporting source everywhere
- some docs overclaim where enforcement/retry live
- runtime policy ownership is drifted between docs and code
- proof failure classification is too coarse for fast production triage

The next work should not be "make a task pass." It should be: make each boundary
truthful enough that a task failure has exactly one earliest violated contract.
