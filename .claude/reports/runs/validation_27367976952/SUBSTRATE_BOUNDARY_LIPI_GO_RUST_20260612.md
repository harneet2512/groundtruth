# Substrate boundary LIPI - Go/Rust LSP - 2026-06-12

Audited HEAD: `323af3c7`

Purpose: before rebuilding `gt-substrate`, verify the Go/Rust LSP path against the
desired architecture. The rule is simple: if a concern belongs to the portable proof
runtime, it should live in `gt_run_proof.py` or the substrate image; GHA should only
orchestrate mounts, pinned image selection, and artifact collection.

## Boundary model

Desired state:

- Substrate image owns GT closure: binaries, Python, embedder, language servers.
- `gt_run_proof.py` owns proof policy: graph build, demand-scoped resolve, LSP readiness
  policy, cert production, fail-closed artifact contract.
- Workflow owns orchestration only: pull pinned image, materialize task repo, extract
  task dependency stores read-only, mount inputs, collect outputs.
- Task image owns task dependency state: Go module cache, Cargo home, Rustup/sysroot
  contents. Workflow must not "fix" them at runtime.

Current state before this patch series:

- Good: `gt_run_proof.py` already owned graph/index/resolve/gates and per-language LSP
  aggregation.
- Bad: `.github/workflows/deepswe_full.yml` still ran `rustup component add rust-src`
  inside the task container.
- Bad: per-language LSP ready-budget policy lived in workflow shell logic and was passed
  into the substrate as a precomputed value.
- Bad: `.github/workflows/deepswe_proof_sweep.yml` still had its own per-language
  `case "$TASK_LANG"` budget table, so the proof sweep could silently drift from the
  paid runtime contract.

## LIPI findings

### SB-01

```text
ID: SB-01
Desired state: Workflow extracts task dep stores read-only and fails closed on missing Rust sysroot data.
Current state: Workflow mutated the task container with `rustup component add rust-src` before copying rustup.
Logic: Broken - orchestration was provisioning substrate-critical state.
Implementation: Broken - best-effort mutation could hide task-image/runtime defects.
Integration: Risky - live proof could pass because GHA patched the task image rather than because the product boundary was correct.
Plumbing: Wrong layer - rust-src belongs to task image/sysroot provenance, not workflow repair.
Verdict: CLOSED
Remaining bug, if any: Live proof still required to confirm the existing task images already contain usable rustup/sysroot data.
Fix boundary: `.github/workflows/deepswe_full.yml`
```

### SB-02

```text
ID: SB-02
Desired state: LSP readiness budgets are substrate proof policy, with only an optional operator override from env.
Current state: Workflow selected go/rust/typescript budgets in shell and passed GT_LSP_READY_BUDGET_S into the container.
Logic: Broken - substrate policy was split across workflow and proof runtime.
Implementation: Partial - policy worked, but at the wrong layer.
Integration: Risky - other workflows could drift or reimplement different budgets.
Plumbing: Wrong owner - gt-run-proof should choose per-language defaults.
Verdict: CLOSED
Remaining bug, if any: None in code. Live proof still needed for P0-01.
Fix boundary: `scripts/swebench/gt_run_proof.py` + `.github/workflows/deepswe_full.yml`
```

## What changed

- Removed workflow-side `rustup component add rust-src`.
- Added `gt_run_proof.lsp_ready_budget_seconds()` and moved default per-language
  readiness budget policy into the proof runtime.
- Removed proof-sweep-side per-language LSP budget selection; the sweep now proves the
  same runtime-owned policy instead of restating it.
- `run_manifest.json` now records `lsp_ready_budgets`.
- Workflow now passes only `GT_LSP_READY_BUDGET_S_OVERRIDE`, not a precomputed
  per-language budget.
- Added guards:
  - `tests/fail_closed/test_portable_substrate.py`
  - `tests/test_lsp_product_verdict.py`

## What is still open

- `P0-01` is still LIVE. None of the above replaces a rebuilt substrate image and live
  Go/Rust re-proof.
- If re-proof still fails:
  - Go failure means the copied task-image module cache is still insufficient or the
    proof runtime/toolchain handoff is wrong.
  - Rust failure means the mounted cargo/rustup/sysroot contract is still incomplete or
    the task image itself lacks what rust-analyzer needs.

## Local receipts

```powershell
python -m pytest tests/fail_closed/test_portable_substrate.py tests/test_dep_store_manifest.py tests/test_lsp_product_verdict.py tests/test_proof_progress_json.py -q
```

Result at this head: `40 passed`
