# Substrate rebuild and re-proof runbook - 2026-06-12

Branch: `gt-trial`  
Current doc-sync head while writing: `790ccd37`

## Purpose

This runbook is the exact next execution boundary for Stage 1.

We are **not** at benchmarking yet.

We are at:

```text
code/documentation truth mostly aligned
runtime control-plane productized enough for Stage 1
remaining launch blocker = P0-01
```

`P0-01` is only closed when a rebuilt pinned substrate proves the previously failing
Go/Rust DeepSWE tasks on the real proof path.

## Required proof claim

To say Stage 1 is substrate-green for this boundary, current evidence must prove:

1. new `gt-substrate` image was rebuilt from current code
2. new immutable `GT_SUBSTRATE_DIGEST` was pinned
3. the paid proof path used that digest
4. the real failing tasks now pass substrate proof:
   - `abs-module-cache-flags`
   - `abs-stepped-slices`
   - `boa-hierarchical-evaluation-cancellation`
5. proof artifacts show the failure class is gone for the reason we intended:
   - Go: no `LSP_FAIL_NOT_READY`
   - Rust: no `LSP_FAIL_NO_WARM`

Anything weaker than that is not closure.

## Current owner map

### Substrate image owns

- baked Go toolchain
- baked rust-analyzer
- baked gcc
- baked Python/embedder closure
- baked Node/LSP closure

File:

- `docker/Dockerfile.gt-substrate`

### Proof runtime owns

- graph build
- demand-scoped resolve
- per-language LSP readiness budget defaults
- fail-closed aggregation of per-language verdicts
- proof progress/failure truth

File:

- `scripts/swebench/gt_run_proof.py`

### Workflow owns

- task image materialization
- read-only extraction of task dependency stores
- pinned digest selection
- substrate container invocation
- artifact collection

File:

- `.github/workflows/deepswe_full.yml`

## What must not happen

These are architecture violations:

1. do not move substrate policy back into workflow shell logic
2. do not "fix" task images at runtime in GHA
3. do not declare proof green from language smoke/preflight
4. do not run the 10-task GT-on benchmark before re-proof is green

## Live tasks to re-proof

### Go

1. `abs-module-cache-flags`
2. `abs-stepped-slices`

Expected failure that must disappear:

- `LSP_FAIL_NOT_READY`

Main evidence to inspect:

- `proof_progress.json`
- `proof_failure.json`
- `dep_store_manifest.json`
- `lsp_certificate_go.json`
- `lsp_certificate.json`
- `run_manifest.json`

### Rust

1. `boa-hierarchical-evaluation-cancellation`

Expected failure that must disappear:

- `LSP_FAIL_NO_WARM`

Main evidence to inspect:

- `proof_progress.json`
- `proof_failure.json`
- `dep_store_manifest.json`
- `lsp_certificate_rust.json`
- `lsp_certificate.json`
- `run_manifest.json`

## What to watch during re-proof

### Go

If it still fails, separate the cause carefully:

1. dep-store extract problem
2. substrate Go toolchain handoff problem
3. `gopls` workspace-loading / package-metadata problem
4. manifest policy too strict for a valid empty-cache case

Important:

The current code still assumes a Go proof needs a non-empty copied `gomodcache`.
That may be right for the failing tasks, but if re-proof shows a valid Go task can
load packages without that copied cache, the product boundary should move from:

```text
non-empty gomodcache
```

to:

```text
package metadata/workspace readiness
```

### Rust

If it still fails, separate:

1. cargo home copied but insufficient
2. rustup/sysroot copied but missing needed `rust-src`
3. rust-analyzer still incompatible with task toolchain
4. proc-macro / linker boundary still incomplete

Current code improvement:

- `dep_store_manifest.json` now records `stores.rust_src` keyed to the active toolchain,
  so "rustup copied" and "usable rust-src exists" are no longer conflated.

## Minimal success evidence

The re-proof is only meaningful if the artifact set proves all of this:

1. `proof_progress.json` reaches `lsp_pass`
2. no `proof_failure.json` for these tasks
3. `lsp_certificate_<lang>.json` is not a fail verdict
4. canonical `lsp_certificate.json` is not masking another language failure
5. `task_truth.json` and `reconciled_substrate_verdict.json` agree

## After re-proof

Only after the above is green:

1. refresh `CURRENT_VALIDATION_RUN.json`
2. update `LATEST_TASK.md`
3. update `gt_gt.md` §17 blocker state
4. then prepare the 10-task GT-on run

## Not completion yet

Even after this runbook exists, Stage 1 is still incomplete until the rebuilt digest and
real re-proof artifacts exist.
