# Checkpoint - task truth runtime control propagation - 2026-06-12

Scope: B5/B9 truth-surface closure for runtime-control semantics.

This checkpoint follows the product rule:

`task_truth.json` is the product truth surface; raw certs, deep metrics, and paired
metrics are evidence/consumers, not independent authorities.

## LIPI

```text
ID: B5-B9-RUNTIME-TRUTH-001
Desired state: task_truth.json carries the final product truth for outcome,
substrate reconciliation, trajectory state, phase policy, obligation lifecycle,
verification horizon, consumption, enforcement semantics, and adapter witness.
Current state: task_truth.py now imports the product runtime modules and emits a
runtime_control block with phase_policy_version, trajectory_state_summary,
obligation_lifecycle_summary, verification_horizon_summary, consumption_summary,
enforcement_semantics, and adapter_witness. gt_deep_metrics now forwards this
runtime_control block when task_truth exists, and paired metrics no longer coerces
resolved=null into resolved=false.
Logic: Clean. Product truth is authored once and consumers prefer it. Verification
horizon remains an intervention; hard_enforced and official_verifier_repair remain
false unless code actually implements those behaviors.
Implementation: Clean for the truth/reporting boundary. Trajectory state summary is
derived from the mini trajectory through src/groundtruth/runtime/trajectory_state.py.
Obligation and horizon versions come from product modules.
Integration: Clean for post-run truth generation and deep-metrics consumption. Live
GHA still has to regenerate artifacts after substrate proof.
Plumbing: Receipts cover task_truth shape, reconciliation, deep metrics authority,
artifact resolver, outcome unknown handling, baseline guard, and scorecard smoke.
Verdict: CLOSED for B5/B9 truth propagation in code.
Remaining bug, if any: P0-01 live substrate proof remains open; existing historical
artifacts must be labeled by commit/run and should not be read as current truth.
Fix boundary: Rebuild substrate, pin digest, re-run proof jobs, then regenerate
task_truth/deep metrics from live artifacts.
```

## Code Behavior Changed

- `scripts/swebench/task_truth.py` now adds product runtime summaries:
  - `trajectory_state`
  - `runtime_control.phase_policy_version`
  - `runtime_control.trajectory_state_summary`
  - `runtime_control.obligation_lifecycle_summary`
  - `runtime_control.verification_horizon_summary`
  - `runtime_control.consumption_summary`
  - `runtime_control.enforcement_semantics`
  - `runtime_control.adapter_witness`
- `scripts/swebench/gt_deep_metrics.py` now carries `runtime_control` forward from
  `task_truth.json` and records `task_truth_path`.
- `scripts/metrics/compute_paired_metrics.py` no longer treats `resolved: null` as
  `False`; it falls back to failure class or raw outcome when truth is incomplete.

## Receipts

```text
python -m pytest tests/test_task_truth.py tests/test_reconcile.py tests/test_gt_deep_metrics_task_truth.py tests/test_deepswe_outcome_unknown.py tests/test_artifact_resolver.py -q
13 passed

python -m pytest tests/test_baseline_guard.py tests/test_trajectory_scorecard.py -q
3 passed
```

## Non-Claims

- This does not prove Go/Rust LSP substrate readiness.
- This does not run or claim ten-task benchmark results.
- This does not implement a hard submit block.
- This does not implement official verifier-fail repair.
