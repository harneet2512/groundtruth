# Checkpoint 002 - Layer 4 verify surface no-leak

Date: 2026-06-11

## Boundary

Layer fixed: DeepSWE agent-visible verification/nudge rendering.

This checkpoint does not change graph construction, covering-test discovery, verifier execution, patch capture, LSP, or metrics reconciliation. The internal graph query can still identify a covering test, but the agent-visible text no longer renders exact test names, test files, or single-test commands.

## Bug addressed

The validation run showed exact test leakage in `<gt-verify>` blocks for tasks such as `awilix` and `katex`.

Root cause: the main `src/groundtruth` brief code had disabled exact test naming, but the DeepSWE runtime fork still rendered exact test metadata:

- `artifact_deepswe/gt_mini_patch.py`
  - verification horizon rendering
  - coherence-collapse nudge
- `artifact_deepswe/gt_oracle.py`
  - obligation status block rendering

The tests also encoded the old behavior by expecting exact single-test commands.

## Code changes

- `artifact_deepswe/gt_mini_patch.py`
  - `_render_verify_emission()` now renders targeted verification guidance without exact test identifiers.
  - `_coherence_collapse_candidate()` can still detect that a graph-linked covering test exists, but only renders a generic targeted-verification hint.
  - `_covering_tests_for_symbols()` docstring clarifies that returned identifiers are internal targeting metadata.

- `artifact_deepswe/gt_oracle.py`
  - `render_obligation_status_block()` no longer renders `name`, `file`, or `run_cmd` from covering-test metadata.
  - Status labels were changed to ASCII to avoid encoding fragility in agent-visible text.

- Tests updated:
  - `tests/test_verification_horizon_stage_c.py`
  - `tests/test_delivery_stage2_obligation_status.py`

## Verification

Passed:

```text
python -m pytest tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py tests\test_verification_horizon_stage_b.py -q
53 passed

python -m py_compile artifact_deepswe\gt_mini_patch.py artifact_deepswe\gt_oracle.py tests\test_delivery_stage2_obligation_status.py tests\test_verification_horizon_stage_c.py
passed
```

Static searches over `artifact_deepswe/gt_mini_patch.py` and `artifact_deepswe/gt_oracle.py` found no occurrences of:

- `pytest tests/test_x.py::test_foo`
- `pytest tests/test_m.py::test_capture`
- `Run the covering test now`
- `covering test:`

## Product impact

GT still provides just-in-time verification pressure, but no longer hands the agent exact test identifiers on the benchmark-visible path. This keeps the feature product-shaped: "verify the edited obligation now" rather than "run this hidden-risk test name."

