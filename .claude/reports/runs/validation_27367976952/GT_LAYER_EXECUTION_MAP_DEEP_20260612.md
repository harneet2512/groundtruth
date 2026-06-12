# GT layer execution map - deep code read - 2026-06-12

Branch: `gt-trial`

Code HEAD while writing: `0b093e1d`

Purpose: map **each GT layer and each thing GT actually does** from code, not
from benchmark outcomes. Desired state is `gt_gt.md` / `CLAUDE.md`. Current
state is the code. A gap is a product bug.

This document uses four words precisely:

- **produced**: GT computed or wrote something.
- **delivered**: the agent saw it in prompt/observation.
- **consumed**: the agent's later action followed or referenced it.
- **enforced**: a boundary prevented continuation or changed the retry/submit path.

## Surface Map

```text
L0  GHA orchestration
L0b language smoke preflight
L1  substrate proof runtime
L1a runtime/proof boundary guards
L2  graph build and FTS5
L2b LSP enrichment and closure freshness
L2c embedder proof and semantic consumption
L2d graph/foundational certificates
L3  issue anchors and L1 localization
L3b curated task brief
L3c path and fact delivery policy
L4  DeepSWE host adapter handoff
L4b mini-swe-agent injected runtime patch
L5  oracle candidate/gate semantics
L5b obligation lifecycle and review transition
L5c verification horizon
L6  repo-native retry loop
L7  trajectory/artifact collection
L8  deep metrics and consumption ledger
L9  outcome/task truth reconciliation
L10 paired scorecard
```

The important product point: these are not independent products. A layer can be
correct while its upstream/downstream boundary still lies.

## L0 - GHA Orchestration

Primary code:

- `.github/workflows/deepswe_full.yml`

What GT does:

1. Accepts benchmark inputs: model, language, task ids, max tasks, pinned
   substrate digest, assets repo, require-pinned-substrate flag.
2. Sets hard proof env:
   - `GT_REQUIRE_FULL_STACK=1`
   - `GT_REQUIRE_FULL_POTENTIAL=1`
   - `GT_REQUIRE_FTS5=1`
   - `GT_FORCE_ONNX_EMBEDDER=1`
   - `GT_REQUIRE_EMBEDDER=1`
   - `GT_REQUIRE_LSP=1`
   - `GT_FORBID_PREBUILT_GRAPH=1`
   - `GT_PROOF_MODE=1`
   - `GT_CONTAINERIZED=1`
   - `GT_RUNTIME_STRATEGY=unified_substrate`
3. Pulls task image with GHCR cache fallback and ECR-public fallback.
4. Pulls or loads the pinned substrate image by digest.
5. Runs task image as `gtsrc` and copies the task repo to `/tmp/gt/src`.
6. Copies dependency stores out of the task image:
   - Go module cache -> `/tmp/gt/deps/gomodcache`
   - Cargo home -> `/tmp/gt/deps/cargo`
   - Rustup -> `/tmp/gt/deps/rustup`
7. Extracts issue text:
   - `instruction.md`
   - fallback `task.toml` metadata
   - fail closed as `GT_ISSUE_MISSING` if empty.
8. Sets per-language LSP readiness budget:
   - Go 30s
   - Rust 45s
   - JS/TS 20s
   - default 20s
9. Runs `gt-run-proof` in the pinned substrate.
10. Checks the 8 artifact contract.
11. Exports host-to-agent artifact env:
    - `GT_CERT_DIR=/tmp/gt`
    - `GT_HOST_GRAPH_DB=/tmp/gt/graph.db`
    - `GT_HOST_SRC_ROOT=/tmp/gt/src`
12. Runs `pier run` with explicit `--ae` env forwarded into task container,
    where paths point at `/gt_artifacts`.
13. Mounts:
    - `/tmp/gt` -> `/gt_artifacts` read-only
    - `/tmp/gt_out` -> `/gt_out` writable for oracle events.
14. Greps for `DeepSweAdapterError` in jobs/log to catch swallowed adapter
    exceptions.
15. Verifies `[GT_META]` graph witness after the trial.
16. Collects logs, jobs, artifacts, oracle events, deep metrics, outcome.

What it produces:

- `trial_output.log`
- `trial_results/`
- `trial_results/gt_artifacts/*`
- `outcome.json`
- Deep metrics JSON/MD
- Oracle event JSONL if runtime writes it

What it enforces:

- task image pull failures classify as `TASK_IMAGE_PULL_FAIL`
- substrate pull failures classify as `GT_SUBSTRATE_PULL_FAIL`
- empty issue classifies as `GT_ISSUE_MISSING`
- proof nonzero classifies as `GT_RUN_PROOF_FAIL`
- proof OOM classifies as `GT_PROOF_OOM`
- missing proof artifact classifies as `GT_ARTIFACT_MISSING`
- adapter error classifies as `DEEPSWE_ADAPTER_FAIL`
- missing/mismatched witness fails the task.

Known product bugs:

- `GT_RUN_PROOF_FAIL rc=2` is too coarse. It names the boundary, not the
  substage.
- The proof stage can fail before artifacts that would explain it are collected.
- The root copy falls back to `/testbed`; if `.git` detection fails, product
  should prefer fail-closed or structured root proof.
- Dependency-store presence is echo-only, not a structured artifact.

## L0b - Language Smoke Preflight

Primary code:

- `.github/workflows/gt_language_smoke.yml`

What GT does:

1. Runs `gt-run-proof` on five fixture repos.
2. Uses the pinned substrate digest.
3. Fails if `gt-run-proof` exits nonzero.
4. Verifies the same 8 artifact names, including `brief.txt`.

What it does not do:

- Does not use DeepSWE task images.
- Does not copy Go/Cargo dep stores from task images.
- Does not run pier.
- Does not test adapter witness.
- Does not test `/gt_artifacts` mount into mini-swe-agent.

Known product bug:

- Treating smoke as byte-identical to DeepSWE proof is wrong. It is a substrate
  preflight, not the full product path.

## L1 - Substrate Proof Runtime

Primary code:

- `scripts/swebench/gt_run_proof.py`

What GT does:

1. Defines the 8 required proof artifacts:
   - `graph.db`
   - `runtime_context.json`
   - `lsp_certificate.json`
   - `graph_certificate.json`
   - `embedder_certificate.json`
   - `foundational_gate_report.json`
   - `run_manifest.json`
   - `brief.txt`
2. Validates proof env:
   - proof mode
   - containerized mode
   - required FTS5/embedder/LSP/full-stack flags
   - unified substrate strategy
   - baked LSP binaries
   - baked configured embedder
   - baked `gt-index`.
3. Rejects evaluator leakage:
   - env keys such as `FAIL_TO_PASS`, `GOLD_PATCH`, `TEST_PATCH`
   - top-level files such as `test_patch.diff`, `gold_patch.diff`.
4. Copies read-only `/work` to writable `/tmp/gt_work_src`.
5. Runs `gt-index -root work -output graph.db`.
6. Detects languages from graph.db.
7. Computes demand scope from issue terms through `nodes_fts`.
8. Computes dynamic LSP max edges from Gate-1 dominance gap.
9. Runs `groundtruth.resolve` for every detected LSP language.
10. Preserves per-language LSP certs and copies dominant language cert to
    canonical `lsp_certificate.json`.
11. Aggregates per-language LSP verdicts and fails closed if any known language
    fails under `GT_REQUIRE_LSP=1`.
12. Builds graph certificate.
13. Runs foundational gates.
14. Ensures embedder certificate exists by direct identity/cosine probe when
    gates did not write it.
15. Classifies embedder certificate and fails closed on bad verdict.
16. Emits `brief.txt` in-container through `generate_v1r_brief`.
17. Writes `runtime_context.json`.
18. Writes `run_manifest.json` with provenance:
    - GT commit
    - substrate digest
    - task repo commit
    - runtime flags
    - language distribution
    - graph hash
    - cert versions.

What it produces:

- The substrate artifact set under `/gt_artifacts`.
- `gt_scope_files.txt` if demand scope exists.
- per-language `lsp_certificate_<lang>.json`.
- `gt_lsp_metrics.txt`.
- `gt_issue_anchors.json` copied from brief generation when present.

What it enforces:

- No host GT execution.
- No model download.
- No per-task pip install.
- No hidden evaluator/gold/test patch leakage.
- No missing configured embedder.
- No missing known-language LSP server.
- No missing brief.

Known product bugs:

- Nonzero proof exit lacks structured substage artifact.
- A failed proof can lose the last successful stage.
- `emit_brief()` depends on the brief generator to write issue anchors at
  `/tmp/gt_issue_anchors.json`; that side effect should be explicit in a
  manifest.

## L1a - Runtime / Proof Boundary Guards

Primary code:

- `src/groundtruth/runtime/proof.py`
- `src/groundtruth/runtime/context.py`

What GT does:

1. Provides one proof-mode exception: `GTProofModeError`.
2. Defines proof mode as `GT_PROOF_MODE=1`.
3. Rejects noncanonical host aliases in proof mode.
4. Allows canonical host handoff vars:
   - `GT_HOST_SRC_ROOT`
   - `GT_HOST_GRAPH_DB`
5. Verifies `groundtruth` imports from runtime root, not host checkout.
6. Forbids `GT_PREBUILT_GRAPH_DB` in proof mode.
7. Stamps graph meta:
   - index build id
   - LSP enrichment timestamp
   - LSP metrics
   - closure rebuild timestamp
   - context id
   - embedder identity.
8. Computes canonical graph edge hash.
9. Asserts native FTS5 exists and is populated.
10. Asserts LSP ran before scoring.
11. Asserts closure is rebuilt after LSP.
12. Asserts semantic embedder is configured and consumed.
13. Builds runtime context from env.
14. Asserts container boundary in proof mode.
15. Exports canonical env.
16. Validates:
    - proof flags
    - inside container
    - import path
    - source root
    - model files
    - onnxruntime
    - forced ONNX
    - real embedder
    - LSP server.

What it does not do:

- Does not rank files.
- Does not deliver agent context.
- Does not decide outcome.

Known product bugs:

- These guards are strong, but downstream reports must not bypass their
  reconciled truth with raw cert fragments.

## L2 - Graph Build And FTS5

Primary code:

- `gt-index` binary invoked by `gt_run_proof.py`
- `src/groundtruth/pretask/graph_localizer.py` FTS5 read path
- `src/groundtruth/runtime/proof.py` FTS5 assertions

What GT does:

1. Builds graph.db using `gt-index`.
2. Requires FTS5 in proof mode.
3. Creates/uses `nodes_fts` for structured symbol/path/signature retrieval.
4. In non-proof fallback, Python can create `nodes_fts` if absent.
5. In proof mode, Python-side FTS5 creation is forbidden.

What it produces:

- `graph.db`
- `nodes`, `edges`, `properties`, `nodes_fts`, runtime meta tables.

What it enforces:

- `nodes_fts` must exist, be populated, and answer MATCH in proof mode.

Known product bugs:

- If `gt-index` fails, workflow only sees `GT_RUN_PROOF_FAIL rc=2`.
- If graph builds but later proof fails, partial graph stage status is not
  emitted as structured progress.

## L2b - LSP Enrichment And Closure Freshness

Primary code:

- `src/groundtruth/resolve.py`
- `scripts/swebench/gt_run_proof.py`
- `scripts/metrics/foundational_gates.py`

What GT does:

1. Detects known LSP servers.
2. Fails known-language install gaps as `LSP_INSTALL_MISSING`.
3. Treats genuinely unsupported languages as explicit no-op.
4. Restricts resolution to demand-scope files when available.
5. Normalizes absolute scope paths to repo-relative.
6. Counts residual method-call edges before mutation.
7. Launches the server even when residual is zero, so no-op is proved by warm
   server.
8. Opens source files.
9. Performs a one-time project readiness barrier.
10. Runs definition queries for ambiguous edges.
11. Verifies/corrects/deletes edges based on LSP definition.
12. Records failed breakdown:
    - `lsp_error`
    - `empty`
    - `exception`.
13. Records project readiness:
    - `project_ready`
    - wait ms
    - attempts.
14. Performs hover/type enrichment on top referenced nodes.
15. Sanitizes hover signatures before writing to nodes.
16. Stamps LSP enrichment timestamp.
17. Rebuilds closure after LSP mutation.
18. Writes LSP certificate with:
    - transport warm
    - project readiness
    - attempted/verified/corrected/deleted/failed/skipped edges
    - graph hash before/after
    - closure freshness
    - verdict hint.

Important verdicts:

- `LSP_INSTALL_MISSING`
- `LSP_FAIL_NO_WARM`
- `LSP_FAIL_NOT_READY`
- `LSP_WARN_ZERO_CONVERSION`
- `LSP_NO_OP_VALID_WITH_WARM_SERVER`
- `LSP_ACTIVE_VALID`
- `LSP_UNSUPPORTED_EXPLICIT`

Known product bugs:

- The code now distinguishes warm transport from effective work, but reports
  must never collapse it back to "LSP warm".
- Go/Rust real-task failures now likely live in this layer or its dependency
  handoff, but logs are needed before claiming cause.

## L2c - Embedder And Semantic Consumption

Primary code:

- `scripts/swebench/gt_run_proof.py`
- `src/groundtruth/runtime/proof.py`
- `scripts/swebench/gt_deep_metrics.py`
- `src/groundtruth/pretask/graph_localizer.py`

What GT does:

1. Requires configured embedder model to be baked.
2. Rejects silent e5 substitution in proof mode.
3. Forces ONNX embedder path.
4. Runs embedder identity/discrimination probe if gate did not write cert.
5. Classifies embedder certificate.
6. In metrics, prefers emitted proof certificate over local probe.
7. Marks local probe as fallback only.

What it proves:

- The model loads.
- Vector dimension exists.
- Nonzero semantic candidates/rendered semantic evidence exists.
- Effective semantic weight is nonzero.

Known product bugs:

- Fallback local probe is still a different process/environment. It must remain
  diagnostic-only, never product truth.

## L2d - Certificates And Foundational Gates

Primary code:

- `scripts/metrics/graph_certificate.py`
- `scripts/metrics/foundational_gates.py`
- `scripts/verify/deepswe_outcome.py`
- `scripts/swebench/task_truth.py`

What GT does:

1. Writes graph certificate.
2. Writes LSP certificate.
3. Writes embedder certificate.
4. Writes foundational gate report.
5. Later reconciles raw graph handoff failure with runtime witness.

Known product bugs:

- Raw graph cert is pre-agent. It can say `GRAPH_FAIL_MISSING_HANDOFF` before
  adapter witness exists.
- `task_truth.py` reconciles this, but older report surfaces can still show raw
  split truth.

## L3 - Issue Anchors And L1 Localization

Primary code:

- `src/groundtruth/pretask/anchors.py`
- `src/groundtruth/pretask/graph_localizer.py`
- `src/groundtruth/pretask/v1r_brief.py`

What GT does:

1. Extracts issue anchors and symbols.
2. Seeds localization on issue symbols, not just file blobs.
3. Walks graph.db edges from symbol nodes.
4. Records structural witness for candidate files.
5. Uses FTS5 over node fields.
6. Scores with:
   - BM25
   - path decay
   - witness strength
   - subject bonus
   - structured lexical signal
   - degree prior
   - generated/test demotion.
7. Distinguishes verified edge witnesses from name-match witnesses.
8. Caps lexical `DEFINES` witnesses below verified edges.
9. Filters stdlib-shadow name-match display for known spurious attributes.
10. Demotes generated/codegen files.
11. Demotes test files as edit targets.
12. Applies dynamic confidence cutoff.

What it produces:

- Candidate files with path, function names, witness, confidence, scope.
- Inputs for the rendered task brief.
- Issue anchors JSON side effect via brief path.

Known product bugs:

- This layer does many things; calling it "localization" hides sub-bugs.
- Generated/vendor filtering must be applied to every delivery surface, not only
  facts.
- Confidence can still be wrong if a hub creates many structural witnesses.

## L3b - Curated Task Brief

Primary code:

- `src/groundtruth/pretask/v1r_brief.py`

What GT does:

1. Receives candidate `FileEntry` objects.
2. Computes internal confidence tiers:
   - verified
   - warning
   - info.
3. Filters out `[INFO]` entries unless all entries are `[INFO]`.
4. Renders `<gt-task-brief>`.
5. Renders top files/functions.
6. Renders witness lines.
7. Renders contracts.
8. Renders issue-relevant specs.
9. Renders callers.
10. Renders context/patterns.
11. Renders non-test co-changes.
12. Renders callees.
13. Does not render test mapping names.
14. Extracts expected behavior from issue text.
15. Disables assertion/test-name rendering path.
16. Renders edit-target callee contracts for top file.
17. Renders multi-file scope hints after filtering vendored paths.
18. Renders graph-connected scope chains.
19. Renders a highest-confidence candidate note only when verified witness or
    safe legacy structural fact exists.
20. Appends `<gt-graph-map>` via deterministic one-hop curation map.

What it produces:

- `brief.txt`
- `gt_issue_anchors.json` side effect
- optionally graph map block.

Known product bugs:

- Brief has many independent render sub-surfaces; a path policy fix in one does
  not prove all are clean.
- Issue anchor output should be explicit in `run_manifest`, not an implicit
  side effect.
- Highest-confidence language must remain evidence, not command.

## L3c - Path And Fact Delivery Policy

Primary code:

- `src/groundtruth/delivery/path_policy.py`
- consumers in `graph_localizer.py`, `v1r_brief.py`, `post_view.py`,
  `gt_mini_patch.py`

What GT does:

1. Normalizes paths.
2. Marks vendored/static/generated/minified paths.
3. Hard-excludes delivery paths for:
   - vendor
   - third-party
   - node_modules
   - dist
   - generated
   - site-packages
   - static
   - assets
   - minified JS/CSS/maps.
4. Demotes generated files for ranking.
5. Detects minified files by mean line length.

Known product bugs:

- The central policy exists, but product readiness requires every delivery
  surface to use it. Any bypass reopens B7.

## L4 - DeepSWE Host Adapter Handoff

Primary code:

- `artifact_deepswe/gt_agent.py`

What GT does:

1. Loads injected payloads:
   - hook
   - mini patch
   - oracle
   - oracle sense.
2. Extends mini-swe-agent install spec.
3. Skips in-container graph build when substrate is active.
4. Consumes `brief.txt` from `GT_CERT_DIR`.
5. Fails closed on missing/empty substrate brief in proof/substrate mode.
6. Forbids host brief fallback in proof/substrate mode.
7. Prepends exactly one `<gt-task-brief>`.
8. Emits `[GT_META] graph_witness`.
9. Resolves consumed graph via runtime context.
10. Hashes consumed graph.
11. Compares consumed graph hash to post-LSP hash.
12. Raises `DeepSweAdapterError` on missing/mismatched graph in proof/substrate
    mode.
13. Persists `delivered_instruction.txt`.
14. Runs the agent with optional retry loop.

What it produces:

- Delivered instruction file.
- `[GT_META]` witness in `trial_output.log`.
- Adapter errors when boundary breaks.

Known product bugs:

- Witness is text-grep checked by workflow, not structured JSON.
- Delivered brief hash is not structurally compared to substrate brief hash.

## L4b - Mini-swe-agent Runtime Patch

Primary code:

- `artifact_deepswe/gt_mini_patch.py`

What GT does:

1. Intercepts command outputs.
2. Prints patch-loaded telemetry to stderr.
3. Tracks action count.
4. Classifies commands as view/edit/test/nonedit.
5. Tracks edited files.
6. Tracks edited tokens.
7. Tracks tested tokens from test command/output.
8. Tracks per-file edit evidence.
9. Tracks edit churn.
10. Tracks edit-to-test cycle spans.
11. Tracks last test outcome failed.
12. Invalidates stale caches after edits.
13. Produces L3 contracts on edit.
14. Produces cochange context on edit.
15. Produces L3b evidence on view/edit.
16. Collects consensus scope on view.
17. Produces scope completeness at review transition.
18. Produces obligation status at review transition.
19. Produces L5 stuck/failure/no-test nudges.
20. Produces loop/coherence detector candidates.
21. Produces verification horizon candidates.
22. Detects phase:
    - orient
    - search
    - edit
    - verify
    - submit.
23. Applies phase policy.
24. Applies oracle gate:
    - content dedupe
    - relevance/focus
    - distribution floor
    - severity/confidence/kind sort
    - one emission per turn.
25. Re-arms latches for candidates that lost the gate.
26. Appends winning GT block to the agent-visible output.
27. Notes runtime delivery for suppression heuristic.
28. Supports legacy path when `GT_ORACLE_ROUTE=0`.

What it produces:

- `<gt-evidence>`
- `<gt-contract>`
- `<gt-nudge>`
- `<gt-scope>`
- verification horizon blocks
- oracle events JSONL if configured.

Known product bugs:

- Runtime policy is embedded as globals, not a shared product policy object.
- Event-bound candidates bypass phase filtering; correct if classification is
  correct, dangerous if classification is wrong.
- Runtime ledger is a heuristic, not next-turn proof.
- Legacy route remains available.

## L5 - Oracle Candidate / Gate Semantics

Primary code:

- `artifact_deepswe/gt_oracle.py`
- `artifact_deepswe/gt_oracle_sense.py`
- live execution bridge in `gt_mini_patch.py`

What GT does:

1. Defines candidate schema:
   - id
   - layer
   - kind
   - content
   - confidence
   - severity
   - trigger
   - relevance keys
   - dose cost
   - decay mode.
2. Defines emission schema.
3. Defines suppression telemetry.
4. Computes distribution-derived confidence floor.
5. Ranks by severity, confidence, id.
6. Replays L5 decisions from trajectory prefixes.
7. Produces loop/scaffold/no-test/failure nudge candidates.
8. Separates parity mode from corrected mode for failure signatures.
9. Loads obligations from anchors.
10. Computes obligation views.
11. Computes edited/tested overlap.

Known product bugs:

- `gt_oracle.py` is replay/semantics-heavy. Live phase/action/budget ownership is
  mostly in `gt_mini_patch.py`; docs must not swap them.

## L5b - Obligation Lifecycle And Review Transition

Primary code:

- `artifact_deepswe/gt_oracle.py`
- `artifact_deepswe/gt_mini_patch.py`

What GT does:

1. Defines obligation statuses:
   - tested
   - edited_untested
   - unaddressed.
2. Defines lifecycle:
   - unedited
   - edited
   - tested
   - satisfied.
3. Tracks obligation transitions.
4. Stores evidence strings.
5. Computes coverage ratio.
6. Renders obligation status block without exact test/file/single-command names.
7. Orders unmet obligations with edited-but-untested first.
8. Uses status vector hash as dedupe key.
9. Raises severity near submit budget.

Known product bugs:

- Lifecycle evidence is not yet a first-class field in `task_truth.json`.
- Product cannot audit every obligation after the fact without event/status
  persistence.

## L5c - Verification Horizon

Primary code:

- `artifact_deepswe/gt_mini_patch.py`

What GT does:

1. Reads `GT_STEP_LIMIT`.
2. Reads `GT_VERIFICATION_CYCLE_COST`.
3. Computes verification cycle cost from:
   - edit-to-test spans
   - edit-to-edit spans for never-test agents
   - default.
4. Computes bands:
   - advisory
   - urgent
   - gate
   - pivot.
5. Uses budget fraction, remaining budget, test coverage, edit coverage, and
   last test failure.
6. Renders verification guidance without exact test names, file paths, or
   single-test commands.
7. Latches within band to avoid repeated spam.
8. Allows gate to persist with cap.

Known product bugs:

- Calibration defaults are from 9 frozen trajectories and are marked
  placeholder-quality.
- "Gate" is an emitted runtime intervention unless wired to hard finish block.

## L6 - Repo-native Retry Loop

Primary code:

- `artifact_deepswe/gt_agent.py`

What GT does:

1. Reads retry count from:
   - `GT_SELF_VERIFY_ATTEMPTS`
   - legacy `GT_RETRY_ON_VERIFIER_FAIL`.
2. Caps retries at 2.
3. Auto-detects visible repo-native test command:
   - Go -> `go test ./...`
   - Rust -> `cargo test`
   - JS/TS -> `npm test --silent`
   - Python -> `python3 -m pytest -x -q`.
4. Runs test command in same container after an agent attempt.
5. Classifies pass/fail/unverifiable.
6. Treats missing runner/env failure as unverifiable, not test failure.
7. Archives attempt trajectory before retry.
8. Prepends arm-neutral `<test-feedback>` after real visible test failure.
9. Adds GT pre-submit reminder in GT-on arm.
10. Runs another agent attempt with edits preserved.

What it does not do:

- Does not call official pier hidden verifier mid-run.
- Does not see hidden tests.

Known product bugs:

- Calling this "official verifier-fail retry" is wrong.
- GT-on adds an extra gate note, so it is not fully harness-neutral after
  failure.

## L7 - Trajectory And Artifact Collection

Primary code:

- `.github/workflows/deepswe_full.yml`
- `deepswe-pier/src/pier/trial/trial.py`

What GT does:

1. Copies trial log.
2. Copies graph.db.
3. Mirrors substrate artifacts.
4. Copies per-language LSP certs.
5. Copies issue text.
6. Copies delivered instruction.
7. Copies pier jobs directory.
8. Copies GT logs/interactions.
9. Copies oracle events from `/gt_out`.
10. Computes deep metrics.
11. Writes run provenance.
12. Writes outcome.

Known product bugs:

- Many copies are best-effort.
- If proof fails early, useful diagnostic artifacts can be absent.
- `task_truth.json` must be generated/copied reliably if it is canonical.

## L8 - Deep Metrics And Consumption Ledger

Primary code:

- `scripts/swebench/gt_deep_metrics.py`
- `scripts/swebench/consumption_ledger.py`

What GT does:

1. Finds `mini-swe-agent.trajectory.json`.
2. Extracts model name, exit status, submission, model stats.
3. Extracts action count from API calls or assistant messages.
4. Extracts edits and first edit action.
5. Extracts token usage and cost.
6. Counts GT content actually in agent observations:
   - brief
   - evidence
   - graph map
   - nudges
   - understand/verify calls
   - observation chars.
7. Falls back from missing run summary to trajectory-proxy layer accounting.
8. Reads graph.db for graph node/edge/FTS/LSP fields.
9. Reads embedder certificate before local probe.
10. Builds delivery summary.
11. Adds consumption/enforcement fields from ledger.

Known product bugs:

- Deep metrics still has fallback logic that can attach wrong trajectory if
  artifact layout is ambiguous.
- Deep metrics has its own resolved inference; should prefer task truth.
- Runtime consumption heuristic and post-run consumption proof must stay
  separate.

## L9 - Outcome And Task Truth

Primary code:

- `scripts/verify/deepswe_outcome.py`
- `scripts/swebench/task_truth.py`

What GT does:

1. Reads per-trial `result.json`.
2. Extracts reward, steps, exit status, instance id.
3. Reads trial log.
4. Finds infra markers.
5. Parses `[GT_META]` witness.
6. Reads cert verdicts.
7. Classifies:
   - INFRA
   - GT
   - RESOLVED
   - AGENT
   - UNKNOWN.
8. Excludes INFRA/UNKNOWN from resolved denominator.
9. Detects infra subtypes:
   - ENOSPC
   - trajectory fallback
   - missing artifact.
10. Reconciles `GRAPH_FAIL_MISSING_HANDOFF` when witness proves handoff.
11. Writes `task_truth.json` with:
    - certs
    - runtime witness
    - deep metrics subset
    - outcome
    - trajectory integrity
    - patch hygiene
    - reconciled graph handoff
    - raw signals.

Known product bugs:

- Paired metrics and other reports can bypass `task_truth.json`.
- `UNKNOWN` needs reason field.
- Adapter failure wording in outcome docstring has drift.

## L10 - Paired Scorecard

Primary code:

- `scripts/metrics/compute_paired_metrics.py`

What GT does:

1. Finds trajectory.
2. Loads deep metrics.
3. Loads brief.
4. Loads outcome.
5. Computes resolved from outcome reward.
6. Computes step and token metrics.
7. Extracts edited files from submission.
8. Computes:
   - total steps
   - steps to edited file read
   - steps to edited file write
   - pre-edit exploration
   - waste rates
   - brief precision/recall/hit@1
   - inverted confidence
   - obligation/consumption/flip heuristics.

Known product bugs:

- Uses edited files as proxy for gold files.
- Reads `outcome.json`, not canonical `task_truth.json`.
- Should not be used as Stage-1 product readiness proof.

## What GT Did In A Run, Chronologically

1. GHA selected task/language/model.
2. GHA pulled task image and pinned substrate.
3. GHA copied task repo to `/tmp/gt/src`.
4. GHA copied dependency stores.
5. GHA extracted issue text.
6. GHA launched substrate proof.
7. `gt-run-proof` validated proof env and baked deps.
8. `gt-run-proof` checked no evaluator/gold leakage.
9. `gt-run-proof` copied read-only source into writable scratch.
10. `gt-index` built graph.db.
11. `gt-run-proof` detected languages.
12. `gt-run-proof` computed issue demand scope.
13. `resolve.py` launched LSP per language.
14. `resolve.py` warmed server and waited for project readiness.
15. `resolve.py` resolved/corrected/deleted edges.
16. `resolve.py` enriched node signatures/return types.
17. `resolve.py` stamped LSP and closure freshness.
18. `gt-run-proof` wrote per-language/canonical LSP certs.
19. `gt-run-proof` wrote graph cert.
20. `gt-run-proof` ran foundational gates.
21. `gt-run-proof` wrote/probed embedder cert.
22. `v1r_brief.py` generated the task brief.
23. `gt-run-proof` wrote `brief.txt`, anchors, runtime context, manifest.
24. GHA verified 8 artifacts.
25. GHA mounted artifacts read-only into task container.
26. GHA launched pier with GT env forwarded.
27. `gt_agent.py` injected runtime patch and oracle files.
28. `gt_agent.py` skipped second graph build in substrate mode.
29. `gt_agent.py` emitted graph witness.
30. `gt_agent.py` consumed substrate brief.
31. `gt_agent.py` prepended brief to instruction exactly once.
32. Agent started.
33. `gt_mini_patch.py` intercepted command outputs.
34. Runtime patch tracked phase, edits, tests, tokens, loops.
35. Runtime patch produced candidate context blocks.
36. Oracle gate chose at most one GT block per turn.
37. Agent saw delivered GT block in observation.
38. Optional repo-native retry ran visible tests after attempt.
39. GHA verified adapter witness.
40. GHA collected artifacts.
41. Deep metrics parsed trajectory-observed GT.
42. Outcome classified failure.
43. Task truth reconciled certs/witness/outcome.
44. Paired scorecard may compute Stage-2 metrics later.

## The Core Product Gap

The code now contains most desired mechanisms, but they are not yet one clean
product surface. The remaining bugs are boundary bugs:

- proof can fail without structured substage truth
- smoke can look green while DeepSWE proof fails
- raw certs can disagree with reconciled task truth
- docs can name the wrong runtime owner
- pre-submit "gate" can mean emitted intervention, not hard finish block
- verifier retry can mean visible repo-native retry, not official verifier retry
- metrics can bypass task truth
- runtime consumption heuristic can be confused with trajectory consumption proof

Launch readiness means every one of those words has one owner, one artifact, and
one test.
