# Checkpoint 001 - Layer 0 metrics truth

Date: 2026-06-11

## Boundary

Layer fixed: metrics/reporting truth only.

This checkpoint does not change GT delivery, DeepSWE runtime hooks, graph generation, LSP resolution, verifier gates, or patch capture. The goal is to stop the metrics layer from reporting false zero GT injection when the run summary is missing but the agent trajectory proves GT observations were delivered.

## Bug addressed

From the validation run, multiple tasks had:

- `gt_injected_tokens_total=0`
- `per_layer={}`
- `layers_active=[]`

while `mini-swe-agent.trajectory.json` contained large agent-visible GT observations.

Root cause: `scripts/swebench/gt_deep_metrics.py` only used `/tmp/gt_run_summary_<task>.json` for injected token/layer accounting. It already parsed the mini trajectory for delivery counts and observation chars, but did not use that as a fallback for token/layer fields.

## Code changes

- `scripts/swebench/gt_deep_metrics.py`
  - Added `_trajectory_layer_fallback()`.
  - If `gt_run_summary` is absent and trajectory GT observations exist:
    - `gt_injected_tokens_total` is estimated from observation chars using the existing chars/4 proxy.
    - `gt_injected_tokens_source="trajectory_proxy"` is emitted at top level and in `efficiency`.
    - `per_layer` and `layers_active` are populated from observed GT markers.
  - If `gt_run_summary` exists, its per-layer accounting remains authoritative.

- `tests/test_gt_deep_metrics_trajectory_fallback.py`
  - Adds a DeepSWE-shaped mini trajectory regression test proving the fallback.

## Verification

Passed:

```text
python -m pytest tests\test_gt_deep_metrics_trajectory_fallback.py -q
1 passed

python -m py_compile scripts\swebench\gt_deep_metrics.py
passed
```

Out-of-scope failure observed:

```text
python -m pytest tests\test_verified_adapter.py -q
2 failed, 20 passed
```

Both failures are in the DeepSWE patch evidence delivery path expecting `<gt-evidence>` to be appended by `artifact_deepswe/gt_mini_patch.py`. That is a separate runtime/hook layer and was not changed in this checkpoint.

## Product impact

This does not make GT stronger by itself. It makes GT debuggable: delivered context no longer disappears from the metrics ledger merely because a sidecar summary file was missing.

