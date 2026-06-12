# Substrate Re-proof LIPI - 2026-06-12

Scope: Go/Rust substrate proof blockers on the mini-swe integration path.

This note records the product-owned fixes after the proof-sweep parity repair
(`42e27fce`). The key change in state is that the failures are now real and
stage-localized, not hidden by workflow blindness.

## Go

Symptom:
- `GO_WORKSPACE_METADATA_FAIL` on real tasks before the LSP pass.
- ABS tasks fail with `go.mod requires go >= 1.24 (running go 1.22.5; GOTOOLCHAIN=local)`.
- `arcane-drift-detection-baselines` fails with `-mod may only be set to readonly or vendor when in workspace mode, but it is set to "mod"`.

Logic:
- Broken. Product readiness for Go belongs to offline workspace metadata load, not
  "gopls launched" and not "some cache path existed".
- Forcing `GOFLAGS=-mod=mod` globally is the wrong product default because it breaks
  `go.work` repos before readiness can even be measured.

Implementation:
- Broken. `docker/Dockerfile.gt-substrate` baked Go `1.22.5`, below live task minimums,
  and exported `GOFLAGS=-mod=mod`.

Integration:
- Broken. The workflow handoff is now correct, so these Dockerfile choices directly
  reach `gt-run-proof` and fail on the live path.

Plumbing:
- Clean after the parity fix. `proof_failure.json` and `proof_progress.json` now show the
  real substage (`workspace_metadata`) and the real stderr.

Fix:
- Bump baked Go to `1.24.4`.
- Switch substrate default to `GOFLAGS=-mod=readonly`.

## Rust

Symptom:
- Rust proof sweep now fails honestly at dep-manifest time instead of later LSP noise.
- `boa-hierarchical-evaluation-cancellation` has non-empty cargo/rustup copies, but no
  stdlib source tree under the copied rustup path for the active toolchain.

Logic:
- Broken. The product question is not "is rust-src under exactly one layout?" It is:
  "does the copied task toolchain provide the stdlib source tree rust-analyzer needs,
  without mutating the task image or downloading at runtime?"

Implementation:
- Broken. Workflow extraction only looked under rustup's toolchain tree and ignored the
  task sysroot as another legitimate source location.

Integration:
- Broken. `deepswe_full.yml` and `deepswe_proof_sweep.yml` were not fully symmetric on
  Rust runtime env; the sweep also omitted `CARGO_HOME`, `RUSTUP_HOME`, and PATH parity.

Plumbing:
- Partial. `dep_store_manifest.json` correctly shows missing `rust_src`, but it could not
  distinguish "truly absent" from "present in sysroot, not copied into the expected tree".

Fix:
- Backfill rust stdlib source from `rustc --print sysroot` into the extracted rustup tree
  when the active toolchain path is otherwise missing it.
- Preserve truth with explicit `rust_src_host` / `rust_src_source` fields in
  `dep_store_manifest.py`.
- Make proof sweep pass the same Rust env as the paid path.

## Changed files

- `docker/Dockerfile.gt-substrate`
- `.github/workflows/deepswe_full.yml`
- `.github/workflows/deepswe_proof_sweep.yml`
- `scripts/swebench/dep_store_manifest.py`
- `tests/test_dep_store_manifest.py`
- `tests/fail_closed/test_portable_substrate.py`

## Receipt

```text
python -m pytest tests/test_dep_store_manifest.py tests/fail_closed/test_portable_substrate.py tests/test_workspace_metadata_probe.py -q
49 passed
```

## Next step

Rebuild the substrate image, pin the new digest, and rerun the capped proof sweep.

## Follow-up: Go/Rust LSP WARN soft-pass

Symptom:
- Go/Rust dependency mounts could be present, and LSP transport could warm, while the
  product still produced zero useful LSP conversions.
- `LSP_WARN_ZERO_CONVERSION` and `LSP_WARN_NOT_ATTEMPTED` were diagnostic labels but
  could still classify as `ok=True`.

Logic:
- Broken. A warning with residual LSP demand is not product readiness. For Stage 1,
  valid LSP states are only active useful work, valid warm no-op, or explicit unsupported
  language. Everything else must fail closed under `GT_REQUIRE_LSP=1`.

Implementation:
- Broken. `foundational_gates._classify_lsp()` returned `ok=True` for
  `LSP_WARN_ZERO_CONVERSION` and `LSP_WARN_NOT_ATTEMPTED`.
- `resolve.py` exited nonzero only for `LSP_FAIL_NO_WARM`, not warm-but-ineffective
  zero-conversion.

Integration:
- Broken. `gt_run_proof.aggregate_lsp_verdicts()` only treated `LSP_FAIL_*`,
  install-missing, and resolve errors as failures. A WARN verdict from one language
  could be masked by a sibling language's success.

Plumbing:
- Clean enough. The cert already carried `effective_work`, `project_ready`,
  `zero_conversion_reason`, and `failure_detail`; the bug was classification, not
  missing evidence.

Fix:
- `LSP_WARN_ZERO_CONVERSION` and `LSP_WARN_NOT_ATTEMPTED` now return `ok=False`.
- `resolve.py` exits fail-closed under `GT_REQUIRE_LSP=1` for `LSP_WARN_ZERO_CONVERSION`.
- `gt_run_proof.aggregate_lsp_verdicts()` now treats only these as valid:
  `LSP_ACTIVE_VALID`, `LSP_NO_OP_VALID_WITH_WARM_SERVER`, `LSP_UNSUPPORTED_EXPLICIT`.
- Go workspace metadata probe uses `go list -e ./...` so platform-specific packages
  like `syscall/js` do not fail the entire Go workspace readiness probe.

Receipt:

```text
python -m pytest tests/fail_closed/test_lsp_liveness.py tests/fail_closed/test_portable_substrate.py tests/test_workspace_metadata_probe.py tests/test_dep_store_manifest.py tests/test_lsp_product_verdict.py -q
77 passed
```
