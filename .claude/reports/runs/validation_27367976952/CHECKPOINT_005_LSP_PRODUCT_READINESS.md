# Checkpoint 005 — LSP product readiness (B4)

Date: 2026-06-11

## Boundary

Layer fixed: LSP certificate semantics + foundational gate classification (transport vs product).

**In scope:** `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py`, `tests/fail_closed/test_lsp_liveness.py`.

**Out of scope:** evidence delivery, task_truth, consumption ledger, path_policy.

## Bug addressed

**Symptom:** `lsp_warm=1` could pass while `effective_work=0` / `project_ready=false` (Go/Rust validation tasks).

**Root cause:** Gate treated warm transport as sufficient; `LSP_WARN_ZERO_CONVERSION` always soft-passed even when `project_ready=false` (workspace still indexing).

## Code changes

- `resolve.py`: emit `effective_work` on cert; `LSP_FAIL_NOT_READY` when warm + residual>0 + zero work + `project_ready=false`; `zero_conversion_reason` for dep-env WARN path.
- `foundational_gates.py`: `_classify_lsp` fails on `project_ready=false` + zero effective work; `_DEEP["gate_lsp"]` adds `lsp_transport_ok`, `lsp_product_ready`, `effective_work`, `project_ready`, `failed_breakdown`.
- `test_lsp_liveness.py`: `_base_cert` includes project fields; tests split WARN vs NOT_READY.

## Verification

```text
python -m pytest tests/fail_closed/test_lsp_liveness.py -q
22 passed in ~5s
```

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B4 | **FIXED** (transport vs product split; NOT_READY fail-closed) |
