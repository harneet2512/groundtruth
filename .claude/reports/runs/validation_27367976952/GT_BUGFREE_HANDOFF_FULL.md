# GT bugfree handoff - validation_27367976952

Date: 2026-06-11
Branch: `gt-trial`

This is the single start-to-end handoff for the GT bugfree work from the 10-task validation run.

## Goal

Make GT a product-quality context provider for benchmarks, not a benchmark-maxxing patch set.

The working definition we agreed on:

- GT should provide the right context at the right time.
- GT should make the agent stronger by saving effort on easy localization/context work, so the agent can spend more steps on hard reasoning and fixing.
- GT should not leak hidden tests or benchmark-specific answers.
- GT should be debuggable: every surface must say what happened, where the truth came from, and whether the agent actually used it.
- A fix in one surface must not move the same bug to another surface.

## Core Product Model We Agreed On

GT should not be treated as "extra context." Agents ignore extra context when it is early, long, noisy, or not tied to the next decision.

GT should behave like a small runtime control system:

| State | Meaning | Current GT state |
|---|---|---|
| Delivered | Agent saw the GT text | Often true |
| Used | Agent's next action followed the GT text | Not measured strongly enough |
| Enforced | Agent could not submit without satisfying/checking the GT obligation | Rare / fragmented |

The target architecture is:

```text
graph / LSP / semantic / issue obligations
        -> trusted context store
        -> trajectory sensor
        -> phase detector
        -> context selector
        -> small GT intervention
        -> consumption/enforcement measurement
```

The product rule:

```text
right context
at the right moment
in the smallest useful form
with a clear next action
and proof that the agent used it
```

This is how GT should create benchmark/product value without benchmark-maxxing:

- early trajectory: reduce wasted search and make the agent faster to useful context
- middle trajectory: inject only local, action-relevant evidence
- late trajectory: enforce issue obligations, verification, and patch risk
- after verifier failure: classify and retry bounded repair when allowed

## Architecture GT Is Lacking

These are the missing architectural pieces that should guide future checkpoints.

1. **Trajectory-State Controller**
   - GT needs a controller that asks:
     - What phase is the agent in now?
     - What context is needed now?
     - Should GT speak or stay silent?
   - Current state: mostly initial brief plus appended evidence; not clearly phase-driven.

2. **Context Selection Policy**
   - Required policy:
     - ORIENT: top files/functions plus first useful command.
     - VIEW: only local caller/callee/invariant for viewed file.
     - EDIT: only contracts for edited symbols.
     - VERIFY: only obligation-level verification.
     - SUBMIT: unresolved obligation gate.
   - Current state: context exists but selection is inconsistent and often noisy.

3. **Consumption Feedback Loop**
   - Required record:
     - GT sent X at turn T.
     - Agent next did Y.
     - Did Y follow X?
   - This must drive suppression, scoring, and future policy. Delivery alone is not success.

4. **First-Class Obligation Model**
   - Needed object shape:
     ```json
     {
       "obligation": "invalid n less than 1 throws ParseError",
       "status": "unedited | edited | tested | satisfied | contradicted",
       "evidence": ["edited functions/multicolumn.ts", "no test output covers invalid n"]
     }
     ```
   - Current state: GT can nudge, but issue clauses are not robustly tracked through edit/test/submit.

5. **Pre-Submit Gate**
   - Before submit, GT should compute:
     - edited files
     - affected obligations
     - tests run
     - unverified risks
     - contract breaks
   - Then block or inject an urgent final checklist without leaking hidden tests.

6. **Verifier-Fail Retry Plumbing**
   - Missing loop:
     ```text
     official verifier fail
       -> classify failure
       -> map failure to obligation/edited symbol
       -> inject repair context
       -> bounded retry
     ```
   - Current state: close misses are not systematically recovered.

7. **Trust-Gated Context Surfaces**
   - Every surface must apply the same trust policy.
   - Current examples of broken trust:
     - vendor/static file leaked into brief
     - exact test names leaked in verify
     - graph cert says fail while gate says all_on
     - embedder cert and metrics disagree
     - LSP warm means transport warm, not useful resolution

8. **Context Budgeting**
   - Needed:
     - small payloads
     - dedupe repeated facts
     - prefer next action over background explanation
     - suppress low-confidence facts
   - Current state: GT has useful facts, but not enough reasoning-density control.

9. **Graph-To-Action Translation**
   - GT should translate graph facts into action.
   - Instead of:
     ```text
     resolved caller: X calls Y
     ```
   - Prefer:
     ```text
     Because X calls Y, changing Y risks behavior Z. Inspect X before editing Y.
     ```

10. **Flip/Trajectory Scorecard**
    - Needed metrics:
      - steps_saved_to_gold
      - wasted_file_views_reduced
      - first_correct_edit_earlier
      - obligation_coverage_at_submit
      - GT intervention consumed
      - GT intervention changed next action
      - gt_caused_flip
    - Current metrics overcount delivery and undercount behavior change.

One-sentence diagnosis:

GT has substrate and evidence generation, but lacks a strong just-in-time controller, obligation tracker, trust gate, and enforcement/retry loop that turn context into trajectory changes.

## Research Direction Already Considered

The research-backed direction is trajectory-aware intervention, not larger passive retrieval:

- SWE-agent / Agent-Computer Interfaces: coding agents need agent-optimized interfaces at the tool/action boundary.
- Wink / coding-agent misbehavior recovery: observe trajectory state and inject targeted corrective nudges.
- SWE-eval / trajectory-enhanced evaluation: pass/fail is insufficient; inspect actions, observations, and corrections.
- Reflexion: natural-language feedback helps when tied to decision-relevant memory.
- Coherence-collapse work: agents can reach relevant code and still fail by losing obligation coherence.
- Position/context bias: long early context is easy to underuse or ignore.

Implication for GT:

- place context at action boundaries
- make each message short and imperative
- detect trajectory misbehavior
- add pre-submit obligation checks
- measure delivered -> used -> enforced
- never leak hidden test names

## What We Agreed Is Already Present But Fragmented/Buggy

These are not new ideas to invent from scratch. They already exist in the architecture/codebase in some form, but are fragmented, under-instrumented, or not consistently enforced.

1. **Pre-task orientation / L1 brief**
   - Present in `src/groundtruth/pretask/v1r_brief.py`, `brief_v5.py`, `graph_localizer.py`, and DeepSWE wrappers.
   - Problem: can surface low-value/static paths and does not always reflect final substrate truth.

2. **Graph-based localization**
   - Present via `graph.db`, localizer, graph certificates, graph map rendering.
   - Problem: graph cert raw verdict and runtime witness can contradict each other.

3. **LSP-enriched graph**
   - Present in `src/groundtruth/resolve.py` and `scripts/metrics/foundational_gates.py`.
   - Problem: `lsp_warm=true` can be reported as pass when `project_ready=false` and useful conversion work is zero.

4. **Embedder / semantic ranker**
   - Present in `groundtruth.memory.enrich.embed`, `graph_localizer.py`, `runtime/proof.py`, and `embedder_certificate.py`.
   - Problem fixed partly: deep metrics was overriding emitted cert truth with local probe failure.

5. **Fail-closed certificates**
   - Present for graph, LSP, embedder, foundational gates.
   - Problem: raw certs are not reconciled into one final task truth.

6. **Deep metrics / GT orient metrics**
   - Present in `scripts/swebench/gt_deep_metrics.py`.
   - Problem fixed partly: summary-missing runs were reported as zero GT injection even when trajectory showed GT blocks.

7. **Per-layer GT delivery tracking**
   - Present as trajectory marker counts: `<gt-task-brief>`, `<gt-evidence>`, `<gt-graph-map>`, `<gt-nudge>`, hook calls.
   - Problem: delivery exists, but consumption/agreement is still missing.

8. **Layer 4 / MCP-style just-in-time tools**
   - Present through hooks, `gt_hook.py`, `gt_query`, `gt_search`, `gt_navigate`, `gt_validate`, wrapper integration.
   - Problem: runtime evidence delivery currently has failing tests in `tests/test_verified_adapter.py`.

9. **Post-edit evidence / contract guidance**
   - Present in `src/groundtruth/hooks/post_edit.py` and `artifact_deepswe/gt_mini_patch.py`.
   - Problem: needs evidence delivery fix and better consumption ledger.

10. **Verification horizon / pre-submit pressure**
    - Present in `artifact_deepswe/gt_mini_patch.py` and Stage B/C tests.
    - Problem fixed partly: it leaked exact test names/commands in DeepSWE runtime; now sanitized.

11. **Issue obligation coverage**
    - Present in `artifact_deepswe/gt_oracle.py`, obligation status rendering, anchors artifacts.
    - Problem fixed partly: exact covering test command rendering removed; still needs consumption and final truth ledger.

12. **Patch-risk / contract checks**
    - Present in `src/groundtruth/runtime/patch_auditor.py`, `src/groundtruth/mcp/endpoints/check.py`, `review_patch.py`, and post-edit logic.
    - Problem: patch hygiene is not yet summarized cleanly per task.

13. **Verifier gate / targeted verification**
    - Present in DeepSWE runtime and runtime test selection utilities.
    - Problem: agent-visible text must stay benchmark-valid; exact test identifiers should not be shown.

14. **Oracle telemetry**
    - Present as `gt_oracle_events_*.jsonl` and `artifact_deepswe/gt_oracle.py`.
    - Problem: telemetry exists but does not yet unify into a task-level truth ledger.

15. **Outcome classification**
    - Present in `scripts/verify/deepswe_outcome.py`.
    - Problem: infra/capture failures and partial artifacts are still weakly classified.

16. **Consumption/followed detection**
    - Partly present in `scripts/swebench/followed_detector.py` and metrics/reporting code.
    - Problem: not integrated into GT deep metrics as delivery-consumption-compliance.

17. **Low-value surface filtering**
    - Present in pieces: generated/test/vendor checks in localizer, post-view, brief code.
    - Problem: policy is not centralized, so static/vendor files can still surface.

18. **Checkpoint/reproducibility discipline**
    - Present through reports, tests, certs, and run folders.
    - Problem: ignored run docs must be force-added; fixes need explicit boundary docs to prevent bug migration.

## 10-Task Findings

Run root:

`D:\Groundtruth\.claude\reports\runs\validation_27367976952\`

Tasks:

1. `abs-module-cache-flags`
   - Complete artifact.
   - GT delivered.
   - Go LSP warm but `project_ready=false`, useful conversions zero.
   - Bug class: LSP readiness over-credit.

2. `abs-stepped-slices`
   - Complete artifact.
   - GT delivered.
   - Go LSP attempted conversions but all failed.
   - Bug class: LSP readiness over-credit.

3. `adaptix-name-mapping-aliases`
   - Complete artifact.
   - Certs/metrics do not agree cleanly.
   - Bug class: metrics/cert truth split.

4. `aiomonitor-task-snapshots-diff`
   - Complete artifact.
   - Static Tailwind asset surfaced in high-visibility context.
   - Bug class: low-value/static surface filtering.

5. `awilix-async-container-initialization`
   - Complete artifact.
   - `<gt-verify>` leaked exact Jest test target.
   - Bug class: DeepSWE runtime no-leak violation.
   - Fixed in checkpoint 002.

6. `boa-hierarchical-evaluation-cancellation`
   - Partial/corrupt artifact.
   - Canonical `trajectory.json` zero bytes, mini trajectory exists, no `model.patch`, verifier failed with Docker no-space.
   - Bug class: infra/capture classification.

7. `csstree-shorthand-expansion-compression`
   - Complete artifact.
   - GT delivered but deep metrics showed zero injected tokens/layers.
   - Bug class: metrics fallback broken.
   - Fixed in checkpoint 001.

8. `fd-deterministic-multi-key-sorting`
   - Complete artifact.
   - Rust LSP warm but no effective conversion work.
   - Bug class: LSP readiness over-credit.

9. `katex-multicolumn-array-spans`
   - Complete artifact.
   - `<gt-verify>` leaked exact test target.
   - Bug class: DeepSWE runtime no-leak violation.
   - Fixed in checkpoint 002.

10. `arktype`
    - Missing artifact.
    - Infra failure/no artifact.
    - Bug class: missing artifact classification.

## Bugs Found And Code Locations

### B1 - Deep metrics false zero for GT injection

Symptom:

- `gt_injected_tokens_total=0`
- `per_layer={}`
- `layers_active=[]`
- But trajectories contain large GT observations.

Code:

- `scripts/swebench/gt_deep_metrics.py`
  - `build()`
  - `_from_miniswe_trajectory()`

Root cause:

- Deep metrics parsed mini trajectory delivery chars/counts, but only used `/tmp/gt_run_summary_<task>.json` for layer/token accounting.

Status:

- Fixed in commit `060eccc9`.

### B2 - Embedder cert vs metrics contradiction

Symptom:

- Cert says embedder active/nonzero.
- Deep metrics says local import failed, often `ModuleNotFoundError: No module named 'numpy'`.

Code:

- `scripts/swebench/gt_deep_metrics.py::_from_embedder`
- `src/groundtruth/runtime/proof.py::build_embedder_certificate`
- `scripts/metrics/embedder_certificate.py`
- `scripts/swebench/gt_run_proof.py`

Root cause:

- Metrics process independently probed embedder and could fail in a different environment than the proof substrate.

Status:

- Fixed in commit `e013c7be`.

### B3 - Exact test leak in DeepSWE runtime

Symptom:

- `awilix` and `katex` received exact test names/commands in `<gt-verify>` or obligation nudges.

Code:

- `artifact_deepswe/gt_mini_patch.py`
  - `_render_verify_emission`
  - `_coherence_collapse_candidate`
  - `_covering_tests_for_symbols`
- `artifact_deepswe/gt_oracle.py`
  - `render_obligation_status_block`
- Tests:
  - `tests/test_verification_horizon_stage_c.py`
  - `tests/test_delivery_stage2_obligation_status.py`

Root cause:

- Main `src/groundtruth` had no-leak comments/disabled code, but the DeepSWE runtime fork still rendered exact test identifiers and tests required it.

Status:

- Fixed in commit `956c32e1`.

### B4 - LSP warm over-credit

Symptom:

- Go/Rust tasks show `lsp_warm=true`, but `project_ready=false` and effective conversions zero.

Code:

- `src/groundtruth/resolve.py`
  - project readiness and `verdict_hint`
- `scripts/metrics/foundational_gates.py`
  - `classify_lsp_cert`
- `tests/fail_closed/test_lsp_liveness.py`

Root cause:

- LSP transport liveness is being conflated with product-useful LSP enrichment.

Status:

- Not fixed this session.

### B5 - Graph cert and outcome truth contradiction

Symptom:

- Raw graph cert can report `GRAPH_FAIL_MISSING_HANDOFF`.
- Outcome later reconciles via `[GT_META] graph_witness`.

Code:

- `scripts/metrics/graph_certificate.py`
- `scripts/verify/deepswe_outcome.py`
- `tests/fail_closed/test_deepswe_outcome_blockers.py`

Root cause:

- Raw cert is exposed as final truth even though runtime witness can reconcile it.

Status:

- Not fixed this session.

### B6 - No agreement/consumption metric

Symptom:

- GT can deliver correct context but agent ignores it.
- Metrics can prove delivery, not agreement/following.

Code:

- `scripts/swebench/gt_deep_metrics.py`
- `scripts/swebench/followed_detector.py`
- possibly new ledger module needed.

Root cause:

- No block-level delivery-consumption-compliance record.

Status:

- Not fixed this session.

### B7 - Low-value/static surface leakage

Symptom:

- Static Tailwind asset surfaced for `aiomonitor`.

Code:

- `src/groundtruth/pretask/graph_localizer.py`
- `src/groundtruth/hooks/post_view.py`
- `src/groundtruth/pretask/v1r_brief.py`

Root cause:

- Vendor/static/generated filters exist but are fragmented by surface.

Status:

- Not fixed this session.

### B8 - Patch capture/hygiene missing

Symptom:

- Lockfiles/weird paths can pollute patch.
- Some tasks lack `model.patch`.

Code:

- `deepswe-pier/src/pier/trial/trial.py`
- `scripts/swebench/package_submission.py`
- `scripts/swebench/convert_to_submission.py`

Root cause:

- Patch artifact collection is generic; no GT-level patch hygiene classification.

Status:

- Not fixed this session.

### B9 - Outcome schema confusion

Symptom:

- Reward known, but some user-facing rows can show `resolved:null`.

Code:

- `scripts/metrics/compute_paired_metrics.py`
- `scripts/verify/deepswe_outcome.py`

Root cause:

- Different report layers carry different truth fields.

Status:

- Not fixed this session.

### B10 - Infra/capture classification weak

Symptom:

- `boa`: zero-byte canonical trajectory, valid mini trajectory, Docker no-space verifier failure.
- `arktype`: missing artifact.

Code:

- `scripts/verify/deepswe_outcome.py`
- `deepswe-pier/src/pier/verifier/verifier.py`
- `deepswe-pier/src/pier/trial/trial.py`

Root cause:

- Outcome classifier does not classify generic verifier exception chains and artifact integrity sharply enough.

Status:

- Not fixed this session.

### B11 - Runtime evidence delivery failing tests

Symptom found during this session:

```text
python -m pytest tests\test_verified_adapter.py -q
2 failed, 20 passed
```

Failures:

- `test_wrap_execute_appends_evidence_on_fake_env`
- `test_abs_testbed_view_resolves_same_pillar_as_relative`

Code:

- `artifact_deepswe/gt_mini_patch.py`
- `tests/test_verified_adapter.py`

Root cause:

- Not debugged yet. Expected `<gt-evidence>` is not appended in these fake-env/view paths.

Status:

- This should be the next session start.

## What Was Solved This Session

### Checkpoint 001 - Layer 0 metrics trajectory fallback

Commit:

`060eccc9 Fix GT deep metrics trajectory fallback`

Changed:

- `scripts/swebench/gt_deep_metrics.py`
- `tests/test_gt_deep_metrics_trajectory_fallback.py`
- checkpoint doc

Result:

- Missing run summary no longer means false zero GT injection.
- Trajectory-derived GT observations produce `gt_injected_tokens_source="trajectory_proxy"`.

Verification:

```text
python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q
python -m py_compile scripts\swebench\gt_deep_metrics.py
```

### Checkpoint 002 - Layer 4 no-leak verify surface

Commit:

`956c32e1 Sanitize DeepSWE verify guidance`

Changed:

- `artifact_deepswe/gt_mini_patch.py`
- `artifact_deepswe/gt_oracle.py`
- `tests/test_delivery_stage2_obligation_status.py`
- `tests/test_verification_horizon_stage_c.py`
- checkpoint doc

Result:

- Agent-visible DeepSWE verify/nudge text no longer renders exact test names, files, or single-test commands.
- Internal covering-test discovery still exists.

Verification:

```text
python -m pytest tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py tests\test_verification_horizon_stage_b.py -q
python -m py_compile artifact_deepswe\gt_mini_patch.py artifact_deepswe\gt_oracle.py tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py
```

Static searches over `artifact_deepswe` render files clean for:

- `pytest tests/test_x.py::test_foo`
- `pytest tests/test_m.py::test_capture`
- `Run the covering test now`
- `covering test:`

### Checkpoint 003 - Layer 0 embedder truth

Commit:

`e013c7be Use embedder cert as deep metrics source`

Changed:

- `scripts/swebench/gt_deep_metrics.py`
- `tests/test_gt_deep_metrics_trajectory_fallback.py`
- checkpoint doc

Result:

- Deep metrics now uses emitted `gt_artifacts/embedder_certificate.json` first.
- Local embedder probe remains fallback-only.
- Host/metrics dependency failure no longer overrides proof-substrate embedder truth.

Verification:

```text
python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q
python -m py_compile scripts\swebench\gt_deep_metrics.py
```

### Handoff commit

Commit:

`5b3a0d4e Document GT bugfix handoff`

Doc:

- `.claude/reports/runs/validation_27367976952/HANDOFF_AFTER_CHECKPOINT_003.md`

## Current Commit Stack

Latest relevant commits:

```text
5b3a0d4e Document GT bugfix handoff
e013c7be Use embedder cert as deep metrics source
956c32e1 Sanitize DeepSWE verify guidance
060eccc9 Fix GT deep metrics trajectory fallback
7a283c60 fix(lsp): mount task image dep stores for Go/Rust LSP resolution
```

## Next Session Should Start Here

Start with the runtime evidence delivery bug, not metrics.

Command:

```text
python -m pytest tests\test_verified_adapter.py -q
```

Expected current state from this session:

```text
2 failed, 20 passed
```

Focus:

- `artifact_deepswe/gt_mini_patch.py`
- evidence append path
- path normalization for relative vs `/testbed/...`
- read-only substrate graph connection

Boundary for checkpoint 004:

- Fix only DeepSWE runtime evidence delivery.
- Do not change metrics, cert generation, no-leak rendering, LSP, or outcome classification.
- Add/adjust focused tests in `tests/test_verified_adapter.py`.
- Write `.claude/reports/runs/validation_27367976952/CHECKPOINT_004_LAYER4_EVIDENCE_DELIVERY.md`.
- Commit as its own checkpoint.

## Recommended Plan After Checkpoint 004

1. **Checkpoint 004 - Layer 4 evidence delivery**
   - Fix failing `<gt-evidence>` append tests.
   - This is directly in the just-in-time context path.

2. **Checkpoint 005 - LSP product readiness**
   - Split transport liveness from product-useful LSP readiness.
   - Add fields/tests around `project_ready`, `effective_work`, zero conversions.

3. **Checkpoint 006 - Reconciled task truth ledger**
   - Build one final `task_truth.json` or equivalent per task.
   - Inputs: certs, runtime witness, deep metrics, outcome, trajectory integrity, patch hygiene.

4. **Checkpoint 007 - Consumption/agreement ledger**
   - Track whether agent listened to delivered GT.
   - Block id -> next agent turns -> reference/follow/conflict/patch alignment.

5. **Checkpoint 008 - Infra/capture classification**
   - Classify ENOSPC, zero-byte canonical trajectory, mini trajectory fallback, missing artifacts.

6. **Checkpoint 009 - Low-value surface policy**
   - Centralize vendor/static/generated path filtering.
   - Apply consistently across localization, brief, graph map, post-view, cochange.

7. **Checkpoint 010 - Patch hygiene**
   - Classify patch files and artifact integrity.
   - Separate source fix signal from lockfile/generated/noise and missing patch.

## Rules For Continuing

- Keep one boundary per commit.
- Every fix gets a regression test.
- Every checkpoint gets a doc.
- Force-add run-folder docs because `.claude/reports/runs/` is ignored.
- Do not clean unrelated dirty/untracked files.
- Avoid broad rewrites of `artifact_deepswe/*.py`; preserve UTF-8 and inspect `git diff --numstat`.
- If a fix changes one surface, search for the same bug pattern in adjacent surfaces before committing.
