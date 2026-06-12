# LATEST_TASK.md — Session Handoff (2026-06-12)

## STATUS: STAGE 1 DOC/TRUTH SURFACES SYNCED; SUBSTRATE RE-PROOF STILL BLOCKING

Current audited HEAD: `0329d87b`

New architecture-first docs:

- `.claude/reports/runs/validation_27367976952/LIPI_64_ITEM_CLOSURE_AUDIT_20260612.md`
- `.claude/reports/runs/validation_27367976952/GT_GT_DESIRED_VS_CURRENT_GAP_AUDIT_20260612.md`
- `.claude/reports/runs/validation_27367976952/SUBSTRATE_BOUNDARY_LIPI_GO_RUST_20260612.md`
- `.claude/reports/runs/validation_27367976952/STAGE1_STATE_AND_SUBSTRATE_BOUNDARY_20260612_AFTER_6CAE3AB7.md`
- `.claude/reports/runs/validation_27367976952/SUBSTRATE_REBUILD_REPROOF_RUNBOOK_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_DEP_STORE_RUSTSRC_TRUTH_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_WORKSPACE_METADATA_PROBE_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_PRODUCT_RUNTIME_CONTROL_PLANE_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_TASK_TRUTH_RUNTIME_CONTROL_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_FIVE_LANGUAGE_RUNTIME_RECEIPTS_20260612.md`
- `.claude/reports/runs/validation_27367976952/LIPI_CODE_AUDIT_RUNTIME_PARITY_20260612.md`
- `.claude/reports/runs/validation_27367976952/LIPI_LINE_LEVEL_CODE_AUDIT_COMMITS_BF840549_CE0947C2_FB009A1E_20260612.md`
- `.claude/reports/runs/validation_27367976952/CHECKPOINT_RUNTIME_LEDGER_ACTION_TRANSLATION_20260612.md`

Audit result:

| Class | Count | Notes |
|---|---:|---|
| CLOSED | 47 | Code-read closed against desired behavior |
| PARTIAL | 16 | Code exists but semantics/live proof/docs remain incomplete |
| LIVE | 1 | `P0-01` substrate rebuild + Go/Rust re-proof |
| FALSELY CLOSED | 0 | `P1-09` and `P2-03` repaired in code/docs |

The audit treats tests as receipts only. Closure authority is current code versus
`gt_gt.md` + `CLAUDE.md`, checked through `LIPI.md` Logic / Implementation / Integration /
Plumbing.

Architecture gaps outside the 64:

- `ARCH-01` CLOSED: product truth authority contract added to `task_truth`.
- `ARCH-02` phase policy exists, but full trajectory-state controller is still partial.
- `ARCH-03` no single phase-to-payload contract across all surfaces.
- `ARCH-04` PARTIAL: oracle event payload hash/actionability metadata added; full cross-surface schema remains.
- `ARCH-05` CLOSED: obligation lifecycle now distinguishes `tested`, `satisfied`, and `contradicted`.
- `ARCH-06` CLOSED: enforcement metrics are hard-block-only; verification follow-up is separate.
- `ARCH-07` IMPROVED: product runtime control-plane modules now own phase/context,
  budget, graph-to-action, verification horizon, and obligation lifecycle behavior;
  DeepSWE delegates to them as adapter glue.
- `ARCH-08` IMPROVED: `task_truth.json` now carries runtime-control summaries and
  deep metrics forwards that surface instead of hiding it.
- `ARCH-09` CLOSED for deterministic receipts: five-language runtime fixtures now
  prove shared state/policy/verification/budget/action/obligation behavior without
  benchmark-shaped branches.
- `ARCH-10` IMPROVED: wrong-phase suppressions on the mini-swe path now persist to
  a runtime ledger and can surface through `task_truth.runtime_control`.
- `BENCHMARK READINESS` NO: `CLAUDE.md` Stage 1 is still blocked by `P0-01`
  until rebuilt Go/Rust substrate proof is green and regenerated artifacts agree.
- `SUBSTRATE READINESS LOGIC` UPDATED: Go/Rust product readiness is now owned by
  offline workspace metadata probes in `gt_run_proof.py`; dep-store manifests remain
  evidence, not the final verdict.
- `LIPI DEPTH` UPDATED: added line-level LIPI audit for the code behavior commits
  `bf840549`, `ce0947c2`, and `fb009a1e`, using concrete symptoms and all four
  LIPI avenues per changed code group.

Current delta after `0329d87b`:

- runtime phase-policy suppressions no longer disappear silently on the mini-swe path
- `gt_mini_patch.py` delegated helpers no longer carry dead fallback logic below live returns
- action translation is stronger for verified-caller contract facts
- Go dep-store evidence now preserves declared versus copied module-cache paths
- Go/Rust substrate readiness now probes offline workspace metadata before the LSP pass
- top-level truth pointers must now follow `0329d87b`, not `2ef22fcf`

Do not run tenpack until `P0-01` is green on rebuilt substrate. The repaired rows still
need a new live run to populate regenerated artifacts.

## STATUS: SUBSTRATE BOUNDARY CLEANED — PROOF SWEEP POLICY DEDUPED — LIVE RE-PROOF BLOCKED ON SUBSTRATE REBUILD

Branch: `gt-trial` (proof-sweep/runtime boundary synced through `2ef22fcf`)
Triage run: `27387470440`
Prior: CP011–015 @ `df4c37c5`; tenpack `27386082651` failed at substrate proof

### This session (LSP proof boundary)

| Bug | Status |
|-----|--------|
| P0-04 Go `GOMODCACHE` / dep manifest | **CLOSED (code)** — await live re-proof |
| P0-05 Rust RA + rust-src + gcc | **CLOSED (code)** — **substrate rebuild required** |
| Proof sweep LSP budget owner | **CLOSED (code)** — workflow no longer forks per-language policy |
| P0-02 `proof_progress.json` / `proof_failure.json` | **CLOSED** |
| P0-06/07 `task_truth.json` authority | **CLOSED** |
| P0-11 `phase_policy.py` extraction | **CLOSED** |
| Stage 2 tenpack GT-on | **NOT RUN** (blocked until re-proof green) |

### Handoff doc (start here)

`.claude/reports/runs/validation_27387470440/GT_LSP_PROOF_HANDOFF.md`

### Bug register + LIPI

`.claude/reports/runs/validation_27387470440/ATOMIC_PRODUCT_BUG_REGISTER_20260612.md`

### Fast verify (17 tests)

```powershell
Set-Location d:\Groundtruth
python -m pytest tests/test_proof_progress_json.py tests/test_dep_store_manifest.py tests/test_phase_policy_module.py tests/test_task_truth.py tests/test_gt_deep_metrics_task_truth.py -q
```

### Broader pre-flight (84 tests, CP011–015)

```powershell
python -m pytest tests/test_verified_adapter.py tests/fail_closed/test_lsp_liveness.py tests/test_task_truth.py tests/test_consumption_ledger.py tests/fail_closed/test_deepswe_outcome_classify.py tests/test_path_policy.py tests/test_patch_hygiene.py tests/test_obligation_tracker.py tests/test_phase_detection.py tests/test_action_templates.py tests/test_context_budget.py tests/test_ledger_suppression.py tests/test_gt_agent_retry.py tests/test_pier_retry_loop.py tests/test_trajectory_scorecard.py tests/test_delivery_stage2_obligation_status.py tests/test_delivery_stage3_detectors.py -q
```

### Frozen baseline (never rerun GT-OFF)

`.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`

### Next work (ordered)

1. Commit/push P0 changes.
2. Rebuild `gt-substrate` image; pin new `GT_SUBSTRATE_DIGEST`.
3. Re-proof: `abs-module-cache-flags`, `abs-stepped-slices`, `boa-hierarchical-evaluation-cancellation`.
4. If green → `./scripts/swebench/dispatch_tenpack_gt_on.sh` (GT-on only).
5. Pair with `compute_paired_metrics.py` vs frozen baseline.

### Key docs

- Architecture: `gt_gt.md` §17.8–17.10
- CP011–015 context: `.claude/reports/runs/validation_27367976952/GT_BUGFREE_HANDOFF_FULL.md`
- Methodology: `CLAUDE.md` (Stage 1 before Stage 2; trajectory > resolve)
