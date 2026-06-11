# Checkpoint 003 - Layer 0 embedder truth

Date: 2026-06-11

## Boundary

Layer fixed: metrics/reporting truth only.

This checkpoint does not change embedder loading, semantic ranking, graph generation, or proof certificate generation. It only changes `gt_deep_metrics.py` so an emitted `gt_artifacts/embedder_certificate.json` is treated as the authoritative status source before attempting a local metrics-process probe.

## Bug addressed

The validation run showed a recurring contradiction:

- `gt_artifacts/embedder_certificate.json` reported a real embedder and nonzero semantic usage.
- `gt_deep_metrics_*.json` reported `ModuleNotFoundError: No module named 'numpy'`, `embedder_nonzero=false`, and `semantic_enabled=false`.

Root cause: deep metrics independently imported/probed the embedder in its own process. That process can lack runtime dependencies even when the proof substrate already emitted the certificate. The local probe was overriding the artifact truth.

## Code changes

- `scripts/swebench/gt_deep_metrics.py`
  - Added `_resolve_embedder_cert_path()`.
  - `_from_embedder()` now accepts a cert path and reads the emitted certificate first.
  - The local embedder probe remains as a fallback when no certificate exists.
  - Deep metrics now emits:
    - `embedder_status_source`
    - `embedder_certificate_path`
    - `embedder_class`
    - `semantic_candidate_count`
    - `rendered_semantic_nonzero_count`
    - `upstream_semantic_nonzero_count`
    - `effective_w_sem`

- `tests/test_gt_deep_metrics_trajectory_fallback.py`
  - Added a regression proving `gt_artifacts/embedder_certificate.json` overrides local probe availability.

## Verification

Passed:

```text
python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q
2 passed

python -m py_compile scripts\swebench\gt_deep_metrics.py
passed
```

## Product impact

This removes another false debugging signal. Deep metrics now reports what the proof substrate emitted instead of allowing a host/metrics dependency miss to make semantic GT look disabled.

