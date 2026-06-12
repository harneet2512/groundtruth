# Next session prompt - GT product bugfree depth pass

You are working in `D:\Groundtruth` on branch `gt-trial`.

Latest local handoff commit at the time this prompt was written:

`0fe7f784 docs(bugfree): map GT layer execution`

## Mission

Make GT product-ready before benchmark proof.

Do **not** optimize for one benchmark task. Do **not** benchmaxx. Benchmarks are
only pressure evidence after the product surface is correct.

The operating model is:

```text
desired state = architecture in gt_gt.md + CLAUDE.md
current state = actual code
gap = product bug
fix = deterministic boundary repair + test + docs
benchmark = proof after product readiness, not the source of the fix
```

Think of this like a product launch readiness review. Meniscule bugs matter:
split truth, stale docs, soft passes, misleading metric names, wrong ownership,
missing artifacts, and overclaimed enforcement are all launch blockers.

## Start Here

Read these docs first, in this order:

1. `.claude/reports/runs/validation_27367976952/GT_LAYER_EXECUTION_MAP_DEEP_20260612.md`
2. `.claude/reports/runs/validation_27367976952/ATOMIC_PRODUCT_BUG_REGISTER_20260612.md`
3. `.claude/reports/runs/validation_27367976952/ONE_SURFACE_DEEP_PRODUCT_READINESS_20260612.md`
4. `.claude/reports/runs/validation_27367976952/ONE_SURFACE_ARCHITECTURE_LIPI_20260612.md`
5. `gt_gt.md` section 17
6. `CLAUDE.md`
7. `.claude/reports/runs/validation_27367976952/GT_BUGFREE_HANDOFF_FULL.md`
8. `LATEST_TASK.md`

Then read code, not just grep. The product surface is:

```text
GHA workflow
  -> pinned substrate image
  -> gt-run-proof
  -> graph/LSP/embedder/brief/certs
  -> DeepSWE adapter
  -> mini-swe-agent runtime patch
  -> trajectory
  -> artifact collection
  -> deep metrics
  -> outcome/task_truth
  -> paired scorecard
```

## Required Framing

Every bug must be classified by the earliest violated boundary:

1. GHA orchestration
2. pinned substrate availability
3. proof environment / baked deps
4. graph build / FTS5 / LSP / embedder / gates
5. proof artifact contract
6. adapter consumption / graph witness
7. runtime patch delivery
8. trajectory consumption / enforcement
9. outcome / task truth / metrics
10. paired scorecard

If a benchmark task fails before the agent runs, it is not an agent/model issue.
If a report says fixed but another report reads stale truth, that is a product
bug. If code implements an intervention but docs call it a hard gate, that is a
product bug.

## Current Deep Findings

The latest deep map identifies GT as roughly 20 execution sublayers:

- `L0` GHA orchestration
- `L0b` language smoke preflight
- `L1` substrate proof runtime
- `L1a` runtime/proof boundary guards
- `L2` graph build and FTS5
- `L2b` LSP enrichment and closure freshness
- `L2c` embedder proof and semantic consumption
- `L2d` graph/foundational certificates
- `L3` issue anchors and L1 localization
- `L3b` curated task brief
- `L3c` path and fact delivery policy
- `L4` DeepSWE host adapter handoff
- `L4b` mini-swe-agent runtime patch
- `L5` oracle candidate/gate semantics
- `L5b` obligation lifecycle and review transition
- `L5c` verification horizon
- `L6` repo-native retry loop
- `L7` trajectory/artifact collection
- `L8` deep metrics and consumption ledger
- `L9` outcome/task truth reconciliation
- `L10` paired scorecard

The atomic register currently identifies **64 atomic launch bugs/gaps**:

- `P0` launch blockers: 14
- `P1` product correctness bugs: 29
- `P2` observability/doc truth bugs: 21

Do not treat those as final. Read code and refine them.

## Most Important Open Work

Start with these launch blockers:

1. **Read current failed proof logs end-to-end.**
   - Current live run discussed in the docs: `27387470440`.
   - Failures were at `GT substrate proof` for Go/Boa jobs.
   - Do not infer from task names. Fetch logs when available and classify the
     exact proof substage.

2. **Add proof-stage subtyping.**
   - Current marker `GT_RUN_PROOF_FAIL rc=2` is too coarse.
   - Add structured proof progress/failure artifact, e.g. `proof_progress.json`
     and/or `proof_failure.json`.
   - It should identify the failed substage:
     env validation, source copy, index, demand scope, LSP language pass,
     graph cert, foundational gates, embedder cert, brief emit, manifest,
     artifact contract.

3. **Make `task_truth.json` the product truth source.**
   - `task_truth.py` reconciles certs/witness/outcome.
   - Some reports still read `outcome.json` or deep metrics directly.
   - Update readers so paired/deep/reporting surfaces prefer task truth when
     present.

4. **Correct architecture ownership drift.**
   - Live phase/action/budget logic is mostly in `artifact_deepswe/gt_mini_patch.py`.
   - `artifact_deepswe/gt_oracle.py` owns obligation semantics and replay
     primitives.
   - Fix docs that say CP013/014/015 live primarily in `gt_oracle.py`.

5. **Clarify enforcement semantics.**
   - Current "pre-submit gate" is an injected intervention / retry reminder
     unless a true finish/submit hard blocker is wired.
   - Current verifier retry is repo-native visible self-verification, not
     official hidden-verifier retry.
   - Either implement the stronger semantics or rename the docs/fields.

6. **Extract context selection policy into a product object.**
   - `_PHASE_POLICY` exists in `gt_mini_patch.py`.
   - Product-ready version should be a visible, tested policy surface:
     ORIENT / SEARCH / EDIT / VERIFY / SUBMIT -> allowed payloads, budget,
     silence rules, and event-bound exceptions.

## Code Surfaces To Read Deeply

Do not only grep. Read these files in meaningful chunks:

- `.github/workflows/deepswe_full.yml`
- `.github/workflows/gt_language_smoke.yml`
- `scripts/swebench/gt_run_proof.py`
- `src/groundtruth/runtime/proof.py`
- `src/groundtruth/runtime/context.py`
- `src/groundtruth/resolve.py`
- `scripts/metrics/foundational_gates.py`
- `scripts/metrics/graph_certificate.py`
- `src/groundtruth/pretask/graph_localizer.py`
- `src/groundtruth/pretask/v1r_brief.py`
- `src/groundtruth/delivery/path_policy.py`
- `artifact_deepswe/gt_agent.py`
- `artifact_deepswe/gt_mini_patch.py`
- `artifact_deepswe/gt_oracle.py`
- `artifact_deepswe/gt_oracle_sense.py`
- `scripts/swebench/gt_deep_metrics.py`
- `scripts/swebench/consumption_ledger.py`
- `scripts/swebench/task_truth.py`
- `scripts/verify/deepswe_outcome.py`
- `scripts/swebench/package_submission.py`
- `scripts/swebench/convert_to_submission.py`
- `scripts/metrics/compute_paired_metrics.py`

## Working Rules

1. One boundary per commit.
2. No task-name hardcoding.
3. No hidden-test leakage.
4. No host fallback in proof/substrate mode.
5. No "warm" or "delivered" metric may imply product success.
6. Every fix needs a deterministic test.
7. Every checkpoint needs docs in the same commit, force-added if under the
   run folder.
8. Do not revert unrelated dirty-tree changes.
9. Do not rerun GT-OFF baseline.
10. Do not call a benchmark result a GT win unless the trajectory is right.

## Expected Output Of The Next Session

At the end, produce:

1. A code commit for each fixed boundary.
2. A checkpoint doc for each fixed boundary.
3. An updated bug register with closed/partial/open rows.
4. An updated layer map if ownership changes.
5. A final handoff listing:
   - commits
   - tests run
   - current GHA run status
   - remaining P0/P1/P2 bugs
   - exact next starting point.

The goal is not "get more flips." The goal is:

```text
current code surface == desired architecture surface
```

Only then use benchmarks to show the product works.
