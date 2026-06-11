# Handoff after checkpoint 003

Date: 2026-06-11
Branch observed: `gt-trial`

## Session objective

Start making GT bug-free layer by layer, with explicit boundaries so a fix on one surface does not move the bug to another surface.

The approach used:

1. Diagnose from trajectories and artifacts.
2. Map bug to code.
3. Patch one layer only.
4. Add or update focused tests.
5. Write checkpoint doc.
6. Commit before moving to the next layer.

## Commits made this session

### `060eccc9` - `Fix GT deep metrics trajectory fallback`

Layer: Layer 0 metrics truth.

Fixed:

- `gt_deep_metrics.py` no longer reports false zero GT injection when `/tmp/gt_run_summary_<task>.json` is missing but `mini-swe-agent.trajectory.json` proves GT reached the agent.
- Adds `gt_injected_tokens_source="trajectory_proxy"` when using trajectory-derived accounting.
- Populates `per_layer` and `layers_active` from observed GT markers as a proxy fallback.

Verification:

- `python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q`
- `python -m py_compile scripts\swebench\gt_deep_metrics.py`

Checkpoint doc:

- `.claude/reports/runs/validation_27367976952/CHECKPOINT_001_LAYER0_METRICS.md`

### `956c32e1` - `Sanitize DeepSWE verify guidance`

Layer: Layer 4 agent-visible verification/nudge surface.

Fixed:

- DeepSWE runtime fork no longer renders exact test names, test files, or single-test commands in agent-visible GT blocks.
- Internal covering-test discovery remains available, but rendering is sanitized.
- Updated old tests that previously required the leak.

Touched:

- `artifact_deepswe/gt_mini_patch.py`
- `artifact_deepswe/gt_oracle.py`
- `tests/test_delivery_stage2_obligation_status.py`
- `tests/test_verification_horizon_stage_c.py`

Verification:

- `python -m pytest tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py tests\test_verification_horizon_stage_b.py -q`
- `python -m py_compile artifact_deepswe\gt_mini_patch.py artifact_deepswe\gt_oracle.py tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py`
- Static searches clean in DeepSWE render files for:
  - `pytest tests/test_x.py::test_foo`
  - `pytest tests/test_m.py::test_capture`
  - `Run the covering test now`
  - `covering test:`

Checkpoint doc:

- `.claude/reports/runs/validation_27367976952/CHECKPOINT_002_LAYER4_VERIFY_NO_LEAK.md`

### `e013c7be` - `Use embedder cert as deep metrics source`

Layer: Layer 0 metrics truth.

Fixed:

- `gt_deep_metrics.py` now treats emitted `gt_artifacts/embedder_certificate.json` as authoritative for embedder status.
- Local metrics-process embedder probing remains fallback-only.
- Prevents a host/metrics dependency miss such as `ModuleNotFoundError: No module named 'numpy'` from overriding a valid proof substrate certificate.

New emitted fields:

- `embedder_status_source`
- `embedder_certificate_path`
- `embedder_class`
- `semantic_candidate_count`
- `rendered_semantic_nonzero_count`
- `upstream_semantic_nonzero_count`
- `effective_w_sem`

Verification:

- `python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q`
- `python -m py_compile scripts\swebench\gt_deep_metrics.py`

Checkpoint doc:

- `.claude/reports/runs/validation_27367976952/CHECKPOINT_003_LAYER0_EMBEDDER_TRUTH.md`

## Main dossier

Initial bug/code map:

- `.claude/reports/runs/validation_27367976952/GT_CODE_BUG_DOSSIER_INITIAL.md`

This dossier maps the validation-run symptoms to code locations and should remain the guide for the next layer fixes.

## Remaining high-priority bugs

### 1. Layer 4 runtime evidence delivery failure

Observed during this session:

```text
python -m pytest tests\test_verified_adapter.py -q
2 failed, 20 passed
```

Failures:

- `test_wrap_execute_appends_evidence_on_fake_env`
- `test_abs_testbed_view_resolves_same_pillar_as_relative`

Symptom:

- Expected `<gt-evidence>` is not appended by `artifact_deepswe/gt_mini_patch.py`.

Why next:

- This is directly in the just-in-time context-provider path.
- It is separate from metrics/no-leak and should be its own checkpoint.

Likely files:

- `artifact_deepswe/gt_mini_patch.py`
- `tests/test_verified_adapter.py`

Boundary:

- Fix evidence delivery only.
- Do not touch metrics, certificates, or no-leak rendering in the same checkpoint.

### 2. Layer 1/2 LSP readiness over-credit

Validation symptom:

- Go/Rust tasks report `lsp_warm=true` but `project_ready=false` and zero useful conversions.
- Foundational gate treats `LSP_WARN_ZERO_CONVERSION` as pass.

Likely files:

- `src/groundtruth/resolve.py`
- `scripts/metrics/foundational_gates.py`
- `tests/fail_closed/test_lsp_liveness.py`

Product direction:

- Preserve transport-liveness proof if needed.
- Add product-facing readiness/effectiveness status:
  - `transport_warm`
  - `project_ready`
  - `definition_resolution_active`
  - `effective_lsp_edges`
- Dashboards should not show product-ready when `project_ready=false` and effective work is zero.

### 3. Graph cert final truth reconciliation

Validation symptom:

- Raw `graph_certificate.json` reports `GRAPH_FAIL_MISSING_HANDOFF`.
- Outcome later reconciles it via runtime witness.
- Artifacts expose contradictory truth.

Likely files:

- `scripts/metrics/graph_certificate.py`
- `scripts/verify/deepswe_outcome.py`
- reporting/dashboard readers

Product direction:

- Add a post-agent reconciled task/substrate truth artifact.
- Keep raw cert as raw evidence, not final user-facing verdict.

### 4. Delivery vs consumption/agreement

Current state:

- Metrics can prove GT was delivered.
- Metrics still cannot prove whether the agent listened, agreed, contradicted, or followed.

Likely files:

- `scripts/swebench/gt_deep_metrics.py`
- `scripts/swebench/followed_detector.py`
- maybe new module for D/C/C ledger

Product direction:

- Add block-level ledger:
  - GT block id/type/content hash
  - delivered turn
  - next N agent turns
  - reference/agreement/conflict signals
  - edit/test/patch alignment

### 5. Static/vendor surface filtering

Validation symptom:

- `aiomonitor` surfaced static Tailwind asset in high-visibility context.

Likely files:

- `src/groundtruth/pretask/graph_localizer.py`
- `src/groundtruth/hooks/post_view.py`
- `src/groundtruth/pretask/v1r_brief.py`

Product direction:

- Centralize `is_low_value_surface(path)`.
- Apply consistently across localization, brief render, graph map, post-view, and cochange surfaces.

### 6. Infra/capture classification

Validation symptom:

- `boa` had zero-byte canonical trajectory, valid mini trajectory, no `model.patch`, and Docker no-space verifier failure.
- `arktype` artifact missing.

Likely files:

- `scripts/verify/deepswe_outcome.py`
- `deepswe-pier/src/pier/verifier/verifier.py`
- `deepswe-pier/src/pier/trial/trial.py`

Product direction:

- Classify ENOSPC / Docker storage as INFRA.
- Classify zero-byte canonical trajectory with valid mini trajectory as capture partial.
- Classify missing task artifact as infra missing artifact.

## Important caveats

- The worktree has many unrelated dirty/untracked files that predated this session. Do not clean or revert them casually.
- `.claude/reports/runs/` is ignored, so checkpoint docs must be force-added when they should be committed.
- Avoid broad rewrites of `artifact_deepswe/*.py` with tools that change encoding. These files contain non-ASCII historical comments; use UTF-8 preserving edits and inspect `git diff --numstat`.

## Recommended next session first command

```text
git log --oneline -5
python -m pytest tests\test_verified_adapter.py -q
```

Then fix the two evidence-delivery failures as checkpoint 004.

