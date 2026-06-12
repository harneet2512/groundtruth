# Checkpoint - workspace metadata readiness probe - 2026-06-12

Branch: `gt-trial`  
Code head while writing: `0329d87b` + working tree follow-up

## Why this checkpoint exists

The substrate boundary had one remaining semantics bug after the dep-store truth work:

- Go readiness was still effectively inferred from copied cache presence
- Rust readiness was still mostly inferred from copied rustup/cargo/rust-src presence

Those are evidence surfaces.

They are not the actual product question.

The actual product question is:

```text
can the substrate load workspace/package metadata offline for this task image state?
```

## LIPI

```text
Symptom: Go/Rust readiness was still one layer too shallow - dep-store presence could fail or pass before proving whether the substrate could actually load workspace metadata offline.

Logic: Broken - copied cache/store presence is a proxy, not the real readiness contract.
Implementation: Clean after fix - gt_run_proof.py now probes offline metadata directly for Go/Rust.
Integration: Clean after fix - workflow still only extracts evidence, while substrate runtime owns the readiness verdict.
Plumbing: Clean after fix - failures now land in proof_progress/proof_failure with dedicated stage/code/message fields.

Root cause: Integration/logic split at the substrate boundary; readiness truth lived partly in dep_store_manifest.py instead of the proof runtime.
Fix: Move Go/Rust readiness truth into gt_run_proof.py via offline workspace metadata probes; keep dep_store_manifest.py as evidence-only for Go and evidence+hard requirement for Rust stores.
Re-checked: workflow ownership stays orchestration-only; dep-store evidence still preserved; proof tracker stages still write deterministically.
```

## Code change

### `scripts/swebench/gt_run_proof.py`

Added `workspace_metadata` proof stage and `probe_workspace_metadata(...)`:

- Go: `go list ./...`
- Rust: `cargo metadata --format-version=1 --no-deps`

If the probe fails, proof now stops with structured failure:

- `GO_WORKSPACE_METADATA_FAIL`
- `RUST_WORKSPACE_METADATA_FAIL`

This happens before the LSP pass, which is the correct owner boundary.

### `scripts/swebench/dep_store_manifest.py`

Changed Go dep-store validation from hard gate to evidence-only.

Rust still hard-requires:

- `cargo`
- `rustup`
- `rust_src`

## Deterministic receipts

```powershell
python -m pytest tests/test_dep_store_manifest.py tests/test_workspace_metadata_probe.py tests/test_proof_progress_json.py tests/fail_closed/test_portable_substrate.py -q
python -m py_compile scripts/swebench/dep_store_manifest.py scripts/swebench/gt_run_proof.py
```

Observed:

- `45 passed`
- py_compile clean

## What this closes

- the remaining P0-04 design-risk note about Go cache non-emptiness as the final truth
- the equivalent Rust readiness gap where copied stores existed but offline metadata truth was still implicit

## What is still open

This does **not** close `P0-01`.

We still need:

1. rebuilt `gt-substrate`
2. pinned new `GT_SUBSTRATE_DIGEST`
3. live Go/Rust re-proof on the real failing tasks
